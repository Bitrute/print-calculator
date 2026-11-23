# /// script
# [tool.orcaslicer.plugin]
# name = "Print Calculator"
# description = "FDM selling price of every sliced plate: material per colour, prime tower, electricity, printer wear, failures, labour, margin and tax. Preço de venda FDM de cada placa fatiada."
# author = "Print & Cut"
# version = "2.10.0"
# ///
"""Print Calculator para o OrcaSlicer (FDM), independente de outros programas.

Depois de cada fatiamento, o separador lê o G-code gerado pelo OrcaSlicer (tempo, gramas, tipo, cor e custo por kg de cada
filamento, impressora e objetos na placa) e calcula o preço com a fórmula FDM do Print & Cut. As definições (IVA, margem,
eletricidade, mão de obra, perfis das impressoras e preços dos filamentos) ficam guardadas pelo próprio OrcaSlicer
(orca_plugins/config.json), por isso não se perdem quando o plugin é atualizado. Interface em português e inglês.
A leitura e o cálculo não dependem do OrcaSlicer, para poderem ser testados fora dele.
"""
import hashlib
import json
import math
import os
import re

try:
    import orca  # disponível só dentro do OrcaSlicer
except ImportError:  # testes fora do OrcaSlicer
    orca = None

# ---------------------------------------------------------------- leitura do G-code
VERSION = "2.10.0"  # igual ao "version" do cabeçalho
# Ficheiro público com o número da última versão publicada ({"version": "x.y.z"}); só se lê o número, nada é enviado.
UPDATE_URL = "https://raw.githubusercontent.com/Bitrute/print-calculator/refs/heads/main/version.json"
_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
EDGE = 4 * 1024 * 1024


def js_round(x):
    """Math.round do JavaScript (metades para cima), igual ao Print & Cut."""
    return math.floor(x + 0.5)


def duration(text):
    s = str(text or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    found = re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", s, re.I)
    return sum(float(n) * _UNITS[u.lower()] for n, u in found) if found else None


def _list(v):
    return [x.strip().strip('"') for x in re.split(r"[;,]", str(v or "")) if x.strip().strip('"')]


def _strings(v):
    return [x.strip().strip('"') for x in re.split(r'";\s*"|;', str(v or "")) if x.strip().strip('"')]


def _float(x):
    try:
        n = float(str(x).replace(",", "."))
        return n if math.isfinite(n) else 0.0
    except (TypeError, ValueError):
        return 0.0


def hex_color(v):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", str(v or "").strip())
    return "#" + m.group(1).lower() if m else ""


def model_name(name):
    n = str(name or "").strip().strip("'\"")
    n = re.sub(r"_id_\d+_copy_\d+$", "", n, flags=re.I)
    return re.sub(r"\s+id:\d+\s+copy\s+\d+$", "", n, flags=re.I)


def parse_gcode(text):
    t = str(text or "")

    def get(pattern, flags=re.I):
        m = re.search(pattern, t, flags)
        return m.group(1).strip() if m else None

    names = [m.group(1) for m in re.finditer(r"EXCLUDE_OBJECT_DEFINE\s+NAME=(\"[^\"]+\"|'[^']+'|\S+)", t, re.I)]
    if not names:
        names = [m.group(1).strip() for m in re.finditer(r"^;\s*printing object\s+([^\r\n]+)", t, re.I | re.M)]
    time = (get(r";\s*total estimated time\s*[:=]\s*([^;\r\n]+)") or get(r";\s*estimated printing time \(normal mode\)\s*=\s*([^\r\n]+)")
            or get(r";\s*estimated printing time\s*[:=]\s*([^\r\n]+)") or get(r";\s*TIME\s*:\s*(\d+)", 0))
    grams = [_float(x) for x in _list(get(r";\s*filament used \[g\]\s*=\s*([^\r\n]+)"))]
    if not grams:
        total = _float(get(r";\s*total filament weight \[g\]\s*[:=]\s*([^\r\n;]+)"))
        if total > 0:
            grams = [total]
    types = _list(get(r";\s*filament_type\s*=\s*([^\r\n]+)"))
    colors = [hex_color(c) for c in _list(get(r";\s*filament_colou?r\s*=\s*([^\r\n]+)"))]
    costs = [_float(c) for c in _list(get(r";\s*filament_cost\s*=\s*([^\r\n]+)"))]
    presets = _strings(get(r";\s*filament_settings_id\s*=\s*([^\r\n]+)"))
    tower = _float(get(r";\s*total filament used for wipe tower \[g\]\s*=\s*([\d.]+)"))
    seconds = duration(time) if time is not None else None
    if seconds is None and not grams:
        raise ValueError("empty")
    unique = list(dict.fromkeys(n for n in names if n))
    models = len(set(model_name(n) for n in unique))
    named = sorted(set(model_name(n) for n in unique))
    header_ids = _list(get(r";\s*model label id\s*:\s*([^\r\n]+)"))
    if len(header_ids) > len(unique):
        # Bambu com ficheiro grande: os nomes lidos no fim são só de alguns objetos; a lista do cabeçalho tem todos
        unique, named, models = [], [], 0
    if not unique:
        # Bambu Lab: o G-code só traz números de objeto, sem nomes ("; model label id: 1,2,3" no cabeçalho e
        # "; start printing object, unique label id: N"). Sem nomes não se sabe se são iguais: contam como peças iguais.
        ids = _list(get(r";\s*model label id\s*:\s*([^\r\n]+)"))
        if not ids:
            ids = [m.group(1) for m in re.finditer(r";\s*start printing object, unique label id:\s*(\d+)", t, re.I)]
        unique = list(dict.fromkeys(ids))
        models = 1 if unique else 0
    pick = lambda arr, i, default="": arr[i] if i < len(arr) else (arr[0] if arr else default)  # noqa: E731
    return {
        "seconds": seconds,
        "tower": tower if tower > 0 else None,
        "objects": len(unique),
        "models": models,
        "names": named,
        "printer": (get(r";\s*printer_settings_id\s*=\s*([^\r\n]+)") or "").strip('"'),
        "printer_model": (get(r";\s*printer_model\s*=\s*([^\r\n]+)") or "").strip('"'),
        "filaments": [{"grams": g, "type": pick(types, i), "color": colors[i] if i < len(colors) else "",
                       "cost": costs[i] if i < len(costs) else (costs[0] if costs else 0.0), "preset": pick(presets, i)}
                      for i, g in enumerate(grams)],
    }


def read_gcode(path):
    """Início e fim do ficheiro (onde estão o tempo, os gramas e as definições), como o Print & Cut."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size <= EDGE * 2:
            return f.read().decode("utf-8", "replace")
        head = f.read(EDGE)
        f.seek(size - EDGE)
        return (head + b"\n" + f.read(EDGE)).decode("utf-8", "replace")


_E = re.compile(rb"\bE(-?\d*\.?\d+)")


def object_extrusion(data):
    """Filamento (mm, com retrações) de cada objeto e ferramenta, a partir do G-code inteiro.
    Objetos: EXCLUDE_OBJECT_START/END (Klipper) ou "; start/stop printing object" (Bambu). Nas Bambu, o nome vem na linha
    "; printing object NOME id:.. copy .." logo antes do número; devolve também esses nomes."""
    absolute, last_e, current, tool, per, pending, label_names = False, 0.0, None, 0, {}, None, {}
    # Marlin, RepRap e outras sem Klipper nem Bambu: o objeto só vem nos comentários "; printing object" / "; stop printing object"
    comments_only = b"EXCLUDE_OBJECT_START" not in data and b"unique label id" not in data
    for line in data.split(b"\n"):
        if not line:
            continue
        c = line[0]
        if c == 71:  # G
            if line.startswith((b"G1 ", b"G0 ", b"G2 ", b"G3 ")) and b"E" in line:
                m = _E.search(line.split(b";", 1)[0])
                if m:
                    e = float(m.group(1))
                    d = e - last_e if absolute else e
                    if absolute:
                        last_e = e
                    if d and current is not None:
                        tools = per.setdefault(current, {})
                        tools[tool] = tools.get(tool, 0.0) + d
            elif line.startswith(b"G92") and b"E" in line:
                m = _E.search(line)
                last_e = float(m.group(1)) if m else 0.0
        elif c == 69:  # E
            if line.startswith(b"EXCLUDE_OBJECT_START"):
                m = re.search(rb"NAME=(\"[^\"]+\"|'[^']+'|\S+)", line)
                current = m.group(1).decode("utf-8", "replace").strip("'\"") if m else None
            elif line.startswith(b"EXCLUDE_OBJECT_END"):
                current = None
        elif c == 77:  # M
            if line.startswith(b"M82"):
                absolute = True
            elif line.startswith(b"M83"):
                absolute = False
        elif c == 84:  # T0, T1...
            m = re.match(rb"T(\d+)\s*$", line.split(b";", 1)[0].strip())
            if m:
                tool = int(m.group(1))
        elif c == 59:  # ;
            if line.startswith(b"; printing object "):
                pending = line[len(b"; printing object "):].decode("utf-8", "replace").strip()
                if comments_only:
                    current = pending
            elif line.startswith(b"; start printing object, unique label id:"):
                current = line.rsplit(b":", 1)[-1].strip().decode()
                if pending:
                    label_names.setdefault(current, pending)
            elif line.startswith(b"; stop printing object"):
                current = None
    return per, label_names


def group_parts(per, named, label_names=None, exact=True):
    """Junta objetos do mesmo modelo: pelo nome e filamento igual (±5 %) ou, sem nomes (Bambu), só por filamento igual (±1,5 %).
    Dois objetos com o mesmo nome mas gramas diferentes (cópia com outra orientação, escala ou suportes) ficam separados,
    só com exact (Klipper e Bambu): nos comentários "; printing object" (Marlin...) o enchimento de um objeto pode vir dentro de outro."""
    groups = []
    if named:
        for label, tools in per.items():
            key = model_name((label_names or {}).get(label, label))
            total = sum(tools.values())
            g = next((x for x in groups if x["key"] == key and (not exact or abs(x["each"] - total) <= 0.05 * max(x["each"], total))), None)
            if g is None:
                name = re.sub(r"\.(stl|3mf|obj|step|stp|amf)$", "", re.sub(r"^obj_\d+_", "", key), flags=re.I)
                same = sum(1 for x in groups if x["key"] == key)
                g = {"key": key, "name": name + (" #%d" % (same + 1) if same else ""), "count": 0, "tools": {}, "each": total}
                groups.append(g)
            g["count"] += 1
            for t, mm in tools.items():
                g["tools"][t] = g["tools"].get(t, 0.0) + mm
    else:
        for label, tools in sorted(per.items(), key=lambda kv: sum(kv[1].values())):
            total = sum(tools.values())
            g = next((x for x in groups if abs(x["each"] - total) <= 0.015 * max(x["each"], total)), None)
            if g is None:
                g = {"key": label, "name": "", "count": 0, "tools": {}, "each": total}
                groups.append(g)
            g["count"] += 1
            for t, mm in tools.items():
                g["tools"][t] = g["tools"].get(t, 0.0) + mm
    return [{"name": g["name"], "count": g["count"], "tools": g["tools"]} for g in groups if sum(g["tools"].values()) > 0]


def load_job(path):
    """Lê o G-code: início e fim chegam para o preço; com mais de um objeto lê o ficheiro inteiro para repartir por modelo."""
    text = read_gcode(path)
    job = parse_gcode(text)

    def whole():
        with open(path, "rb") as f:
            return f.read()
    if os.path.getsize(path) > EDGE * 2 and "EXCLUDE_OBJECT_DEFINE" not in text and not re.search(r";\s*model label id\s*:", text, re.I):
        # ficheiro grande sem lista de objetos no início (Marlin, RepRap...): os objetos estão a meio, lê-se tudo
        job = parse_gcode(whole().decode("utf-8", "replace"))
    return _with_parts(job, text, whole)


def job_from_bytes(data):
    """O mesmo que load_job, para um G-code que já está em memória (ficheiro importado)."""
    text = data.decode("utf-8", "replace")
    return _with_parts(parse_gcode(text), text, lambda: data)


def _with_parts(job, text, whole):
    """Reparte por modelo quando há mais de um objeto; whole() dá o G-code inteiro em bytes, só se for preciso."""
    # Klipper e Bambu marcam cada objeto com exatidão: aí também se lê com o mesmo nome, porque as cópias podem ter
    # orientação, escala ou suportes diferentes
    exact = "EXCLUDE_OBJECT_DEFINE" in text or "unique label id" in text or bool(re.search(r";\s*model label id\s*:", text, re.I))
    if job["objects"] > 1 and (job["models"] > 1 or not job["names"] or exact):
        per, label_names = object_extrusion(whole())
        parts = group_parts(per, bool(job["names"] or label_names), label_names, exact)
        if len(parts) > 1:
            job["parts"], job["models"] = parts, len(parts)
    return job


MAX_IMPORT = 200 * 1024 * 1024


def import_jobs(raw):
    """Placas de um ficheiro fatiado: .gcode.3mf (Metadata/plate_N.gcode, N a começar em 1) ou um .gcode. {placa: dados}."""
    import io
    import zipfile
    out = {}
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                m = re.fullmatch(r"Metadata/plate_(\d+)\.gcode", name, re.I)
                if m and z.getinfo(name).file_size <= MAX_IMPORT:
                    out[int(m.group(1)) - 1] = job_from_bytes(z.read(name))
    elif raw:
        out[0] = job_from_bytes(raw)
    if not out:
        raise ValueError("empty")
    return out


# ---------------------------------------------------------------- definições e perfis de impressora
CATALOG = {
    "Bambu Lab": [["A1 mini", 249, 20, 5, 80, 130], ["A1", 349, 25, 5, 80, 150], ["P1S", 599, 30, 5, 80, 150], ["P2S", 749, 30, 5, 80, 150], ["X1 Carbon", 999, 40, 5, 80, 150], ["H2C", 2249, 50, 5, 80, 350]],
    "Prusa Research": [["MINI+", 459, 25, 5, 80, 80], ["MK4S", 1099, 35, 5, 80, 120], ["CORE One+", 1349, 40, 5, 80, 150], ["Original Prusa XL", 3999, 80, 5, 80, 300]],
    "Creality": [["Ender-3", 189, 20, 4, 70, 120], ["Ender-3 V2", 229, 22, 4, 70, 120], ["Ender-3 S1", 349, 25, 5, 75, 150], ["Ender-3 S1 Pro", 399, 28, 5, 75, 150], ["Ender-3 S1 Plus", 499, 32, 5, 75, 200], ["Ender-3 S1 Max", 599, 35, 5, 75, 250], ["Ender-3 V3 SE", 199, 25, 4, 70, 120], ["Ender-3 V3 KE", 279, 25, 5, 75, 150], ["Ender-3 V3", 389, 30, 5, 75, 200], ["Ender-3 V3 Plus", 469, 32, 5, 75, 250], ["Ender-5 S1", 499, 30, 5, 75, 250], ["Ender-6", 699, 40, 5, 75, 350], ["CR-10 Smart", 499, 30, 5, 75, 250], ["CR-M4", 1099, 50, 5, 75, 350], ["Sermoon D3", 1499, 60, 5, 75, 400], ["K1", 499, 32, 5, 75, 350], ["K1C", 599, 35, 5, 75, 350], ["K1 Max", 899, 45, 5, 75, 350], ["K2 Plus", 1499, 55, 5, 75, 500], ["Creality Hi", 1299, 50, 5, 75, 500]],
    "Anycubic": [["Kobra 2 Neo", 199, 20, 4, 70, 120], ["Kobra 3 Combo", 549, 30, 5, 75, 200]],
    "Elegoo": [["Neptune 4 Pro", 299, 25, 4, 70, 150], ["Neptune 4 Max", 470, 30, 4, 70, 200]],
    "FlashForge": [["Adventurer 5M", 399, 30, 5, 75, 150], ["Adventurer 5M Pro", 599, 35, 5, 75, 200], ["Guider 3 Plus", 899, 45, 5, 75, 300]],
    "QIDI Tech": [["Q1 Pro", 469, 30, 5, 75, 350], ["X-Max 3", 999, 45, 5, 75, 600], ["Plus4", 1099, 45, 5, 75, 600]],
    "Raise3D": [["E2", 3499, 90, 6, 75, 300], ["Pro3", 5999, 120, 6, 75, 600]],
    "UltiMaker": [["S3", 4495, 100, 6, 75, 350], ["S7", 7995, 150, 6, 75, 500]],
    "Snapmaker": [["Artisan", 2799, 70, 5, 75, 350], ["J1s", 1399, 50, 5, 75, 300]],
    "Sovol": [["SV06", 239, 20, 4, 70, 120], ["SV08", 699, 35, 5, 75, 350]],
    "Artillery": [["Sidewinder X3 Pro", 299, 25, 4, 70, 150], ["X4 Plus", 499, 30, 5, 70, 250]],
    "Voron": [["Voron 2.4 250", 900, 45, 5, 70, 350], ["Voron Trident 300", 950, 45, 5, 70, 350]],
    "Geeetech": [["A20M", 299, 25, 4, 70, 150], ["Mizar S", 399, 30, 5, 70, 200]],
    "Kingroon": [["KP3S", 199, 20, 4, 70, 120], ["KP5L", 399, 25, 4, 70, 200]],
    "Tronxy": [["XY-2 Pro", 299, 25, 4, 70, 150], ["X5SA", 499, 30, 5, 70, 250]],
    "Zortrax": [["M200 Plus", 2499, 75, 5, 70, 300], ["M300 Dual", 3999, 100, 5, 70, 400]],
    "MakerBot": [["Sketch", 1499, 55, 5, 70, 150], ["Method X", 6999, 150, 5, 70, 400]],
    "BCN3D": [["Epsilon W27", 7995, 160, 6, 70, 500], ["Omega I60", 9995, 200, 6, 70, 800]],
    "Modix": [["BIG-60 V4", 4900, 110, 6, 70, 700], ["BIG-120X", 8500, 170, 6, 70, 900]],
    "AnkerMake": [["M5C", 399, 25, 5, 75, 150], ["M5", 699, 35, 5, 75, 200]],
    "Monoprice": [["Mini Delta V2", 199, 20, 4, 70, 100], ["Maker Select Plus", 299, 25, 4, 70, 150]],
    "XYZprinting": [["da Vinci Jr. 1.0", 349, 25, 4, 70, 150], ["da Vinci Color", 3499, 80, 5, 70, 250]],
}  # marca: [[modelo, preço, manutenção/ano, vida (anos), utilização %, potência W], ...] (catálogo do Print & Cut)
FILAMENT_PRICES = {"pla": 22, "pla+": 24, "pla matte": 24, "pla silk": 27, "petg": 24, "petg-cf": 40, "abs": 21, "asa": 26, "tpu": 32,
                   "pa": 38, "nylon": 38, "pa-cf": 60, "pa-gf": 50, "pc": 42, "pc-abs": 40, "hips": 24, "pva": 55, "bvoh": 80, "pp": 35,
                   "pctg": 28, "peek": 350, "pei": 180}
# ---------------------------------------------------------------- línguas e moedas
# Textos de todas as línguas (separador, definições do OrcaSlicer e notificações). Chaves cfg_ = definições, py_ = Python.
I18N = json.loads(r'''{"pt":{"imp":"Importar ficheiro fatiado","impBig":"Este ficheiro é demasiado grande (máximo 200 MB).","impFail":"Não foi possível ler este ficheiro.","sliceAllP":"Várias placas? Usa \"Fatiar tudo\" no OrcaSlicer e todas aparecem aqui. Também podes importar um .gcode.3mf ou .gcode.","chT":"Canais de venda","chP":"O preço de cada canal já cobre a taxa e o IVA: ficas com o mesmo lucro em todos.","chDirect":"Venda direta","chN":"Canal","chFee":"Taxa","chPiece":"Por peça","qChannel":"Preço para este canal","chAdd":"Adicionar canal","chName":"Nome (ex.: Etsy)","chNote":"Taxa em % sobre o preço com IVA e taxa fixa por venda. O plugin sobe o preço para ficares com o mesmo lucro.","withShip":"com portes ({s})","q_ship":"Portes","q_grand":"Total com portes","qSave":"Guardar orçamento","qSaved":"Orçamento guardado","qsT":"Orçamentos guardados","qsCopy":"Copiar","qsPdf":"Imprimir / PDF","qsDel":"Apagar","qsCsv":"Exportar CSV","qsFile":"Guardado em {p}","qsFileOpen":"Guardado em {p}. Abre-o para imprimir ou guardar em PDF.","spoolBtn":"Ler preços do Spoolman","spoolPick":"Spoolman…","spoolErr":"Não foi possível ler o Spoolman: {e}","spoolOk":"{n} filamentos do Spoolman. Escolhe um ao lado de cada filamento e guarda.","spoolNone":"O Spoolman não tem filamentos com preço e peso.","cfg_shipping":"Portes por encomenda","cfg_q_name":"Nome no orçamento (empresa)","cfg_q_footer":"Nota no fim do orçamento","cfg_spoolman":"Endereço do Spoolman (opcional)","qh_item":"Artigo","qh_qty":"Qtd.","qh_unit":"Preço unitário","qh_vat":"IVA incluído ({v}%)","qh_date":"Data","buf_note":"Fator de reserva: soma uma margem aos custos por hora da impressora (desgaste, manutenção e eletricidade) para o que não está previsto: avarias, peças (hotend, bico, placa, correias), tempo parado e poupar para a próxima impressora. 1,3 = +30%. Não mexe no material. Normal: 1,2 a 1,3; impressora com muitos problemas: 1,4 a 1,5; 1 = sem reserva.","eff_note":"Fator de perdas de material: multiplica só o custo do filamento, para o plástico que o G-code não conta (pontas de bobine, trocas de filamento, testes, filamento estragado). 1,10 = +10% no material. Não contes a torre e a purga (já estão nos gramas) nem as falhas (têm a reserva própria). Normal: 1,05 a 1,15; 1 = sem perdas.","sub":"Preço de venda FDM da placa fatiada","readyT":"Pronto para calcular","readyP":"Fatia uma placa no OrcaSlicer e o preço de venda aparece aqui, com o material, a eletricidade, o desgaste da impressora e a tua margem.","s1":"Escolhe impressora e filamentos","s2":"Fatia a placa","s3":"Vê o preço","reading":"A ler o G-code","failT":"Não foi possível calcular","warnT":"Atenção","noPrinter":"Impressora sem nome","updated":"Atualizado às {t}","priceT":"Preço por peça com IVA","mine":"a tua margem","sim":"margem desta placa","profit":"lucro por peça","margin":"margem","plate":"placa inteira ({n} peças)","time":"tempo da placa","grams":"filamento por peça","copy1":"peça na placa","copyN":"peças iguais na placa","unit":"custo por peça","fils":"Filamentos","color1":"cor","colorN":"cores","perKg":"/kg","tower":"Torre e purga, já incluídas nos gramas","costT":"Custo por peça","profitT":"Lucro com a margem escolhida","c_material":"Material","c_energy":"Eletricidade","c_machine":"Desgaste da impressora","c_reserve":"Reserva da máquina","c_failure":"Reserva para falhas","c_labor":"Mão de obra","c_pack":"Embalagem","src_custom":"Definido por ti","src_orca":"Perfil do OrcaSlicer","src_average":"Preço médio","src_generic":"Valores genéricos","cfgT":"Definições de preço","cfgS":"geral, impressora e filamentos","general":"Geral","printer":"Impressora","p_price":"Preço da impressora","p_life":"Vida útil","years":"anos","p_uptime":"Utilização anual","p_maint":"Manutenção anual","p_power":"Consumo médio","p_buffer":"Fator de reserva","f_note":"O preço por kg vem do campo \"Custo do filamento\" do perfil do OrcaSlicer. Se o mudares aqui, fica guardado para esse filamento.","scan":"Procurar placas já fatiadas","scanP":"Fatiaste antes de ativar o plugin? Escolhe no OrcaSlicer a última placa que fatiaste e o plugin lê o G-code dela, sem fatiar de novo.","platesCost":"Custo total para ti","plateN":"Placa {n}","platesTotal":"Total das {n} placas, com IVA","notSliced":"A {n} ainda não foi fatiada","notSlicedP":"Fatia esta placa para ver o preço. O preço de cada placa fatiada fica guardado enquanto o projeto estiver aberto.","customM":"Margem personalizada","save":"Guardar e recalcular","reset":"Repor valores da impressora","h":"h","min":"min","cfg_intro":"Valores usados no preço de venda de cada placa fatiada. Também os podes mudar no separador Print Calculator, com o perfil de cada impressora e o preço dos filamentos.","cfg_ro":"Só leitura.","cfg_currency":"Moeda","cfg_search":"Procurar moeda (nome ou código)","cfg_vat":"IVA (%)","cfg_margin":"Margem (%)","cfg_failure":"Taxa de falha (%)","cfg_electricity":"Eletricidade (por kWh)","cfg_rate":"Mão de obra (por hora)","cfg_setup":"Preparação por placa (min)","cfg_finish":"Acabamento por peça (min)","cfg_pack":"Embalagem por peça","cfg_efficiency":"Fator de perdas de material (×)","py_perm":"O OrcaSlicer não deixou ler o G-code. Quando ele perguntar, escolhe Sim e fatia de novo.","py_missing":"O G-code já não existe. Fatia de novo.","py_empty":"Este G-code não tem tempo nem gramas que o Print Calculator consiga ler.","py_fail":"Não foi possível calcular: {e}","py_unit":"{p} por peça com IVA","py_plate":" ({n} peças, {t})","py_hint":"Print Calculator: abre o separador Print Calculator para ver o preço desta placa.","py_none":"Não encontrei placas fatiadas nesta sessão do OrcaSlicer. Fatia a placa, ou exporta o G-code (Ctrl+G) se ela já estiver fatiada.","cfg_language":"Idioma","cfg_auto":"Igual ao OrcaSlicer","cfg_minimum":"Preço mínimo por peça, com IVA","cfg_rounding":"Arredondar o preço por peça para cima","cfg_round_none":"Não arredondar","cfg_round_99":"Até ,99","cfg_discount_qty":"Desconto de quantidade a partir de (peças na placa)","cfg_discount_pct":"Desconto de quantidade (%)","adj_disc":"desconto de quantidade −{v}%","adj_min":"preço mínimo","adj_round":"arredondado","refShort":"referência em €","refNote":"Os preços marcados \"referência em €\" são médias em euros, não convertidas para {c}. Confirma-os nas definições abaixo.","extrasT":"Extras","extrasP":"Ímanes, parafusos, insertos, tinta… Escolhe se cada extra conta em cada peça ou uma vez na placa. Ficam guardados para este trabalho.","extraName":"Artigo","extraCost":"Custo","extraAdd":"Adicionar extra","c_extras":"Extras","quoteBtn":"Copiar orçamento","quoteAll":"Copiar orçamento de todas as placas","copied":"Orçamento copiado. Cola-o na tua mensagem.","q_title":"Orçamento","q_qty":"Quantidade","q_unit":"Preço por peça","q_total":"Total","q_time":"Tempo de impressão","q_material":"Material","q_taxinc":"IVA incluído","byModelT":"Preço por modelo","byModelP":"O material é repartido pelo filamento de cada modelo; o tempo, a máquina e a preparação pela parte do filamento que cada um usa. É uma estimativa.","modelN":"Modelo {n}","mdlCost":"custo/peça","mdlPrice":"preço/peça","cfg_updates":"Procurar versões novas ao abrir o separador","upd_new":"Há uma versão nova do Print Calculator ({v}). Atualiza-a no gestor de plugins do OrcaSlicer.","allBtn":"Todas as placas","allCost":"Custo total","priceSetT":"Preço da placa com IVA","profitSet":"lucro da placa","gramsSet":"filamento da placa","partsN":"peças na placa","unitSet":"custo da placa","costSetT":"Custo da placa","py_set":"{p} pela placa com IVA ({n} peças)","jobN":"Trabalho {n}","jobNameP":"Nome (cliente, projeto…)","jobP":"Escolhe o trabalho de cada placa. Cada trabalho tem o seu total, a sua margem e o seu orçamento.","jobMargin":"Margem do trabalho","jobTime":"tempo total","jobProfit":"lucro","sumCost":"Custo","sumTax":"IVA","sumProfit":"Lucro","setupT":"Primeiro, confirma os teus valores","setupP":"O preço só aparece depois de confirmares as definições abaixo: moeda, IVA, eletricidade, o preço da tua impressora e o preço por kg de cada filamento. Os valores já preenchidos são de referência e podem não ser os teus. Depois carrega em \"Guardar e recalcular\".","repBtn":"Reportar um problema","repBusy":"A preparar o G-code…","repDone":"G-code guardado em {p}. Arrasta este ficheiro para o issue que abriu no browser e descreve o problema.","repNoG":"Esta placa não tem G-code. O issue abriu sem ficheiro: descreve o problema e, se puderes, fatia e tenta outra vez.","repLink":"Se o browser não abriu, abre este link:","noLabels":"Este G-code não diz quantas peças há na placa, por isso o preço é o da placa inteira. Para contar as peças, liga \"Label objects\" (Etiquetar objetos) no processo do OrcaSlicer, em Outros, e fatia outra vez.","exPart":"por peça","exPlate":"por placa"},"en":{"imp":"Import sliced file","impBig":"This file is too big (maximum 200 MB).","impFail":"Couldn't read this file.","sliceAllP":"Several plates? Use \"Slice all\" in OrcaSlicer and every plate shows up here. You can also import a .gcode.3mf or .gcode.","chT":"Sales channels","chP":"Each channel's price already covers its fee and the tax, so your profit is the same everywhere.","chDirect":"Direct sale","chN":"Channel","chFee":"Fee","chPiece":"Per part","qChannel":"Price for this channel","chAdd":"Add channel","chName":"Name (e.g. Etsy)","chNote":"Fee in % of the price with tax, plus a fixed fee per sale. The plugin raises the price so you keep the same profit.","withShip":"with shipping ({s})","q_ship":"Shipping","q_grand":"Total with shipping","qSave":"Save quote","qSaved":"Quote saved","qsT":"Saved quotes","qsCopy":"Copy","qsPdf":"Print / PDF","qsDel":"Delete","qsCsv":"Export CSV","qsFile":"Saved to {p}","qsFileOpen":"Saved to {p}. Open it to print or save as PDF.","spoolBtn":"Read prices from Spoolman","spoolPick":"Spoolman…","spoolErr":"Couldn't read Spoolman: {e}","spoolOk":"{n} Spoolman filaments. Pick one next to each filament and save.","spoolNone":"Spoolman has no filaments with price and weight.","cfg_shipping":"Shipping per order","cfg_q_name":"Name on quotes (business)","cfg_q_footer":"Note at the end of quotes","cfg_spoolman":"Spoolman address (optional)","qh_item":"Item","qh_qty":"Qty","qh_unit":"Unit price","qh_vat":"Tax included ({v}%)","qh_date":"Date","buf_note":"Reserve factor: adds a margin to the printer's hourly costs (wear, maintenance and electricity) for what isn't planned: breakdowns, parts (hotend, nozzle, build plate, belts), downtime and saving for the next printer. 1.3 = +30%. It doesn't change the material. Typical: 1.2 to 1.3; troublesome printer: 1.4 to 1.5; 1 = no reserve.","eff_note":"Material waste factor: multiplies only the filament cost, for plastic the G-code doesn't count (spool ends, filament changes, test prints, spoiled filament). 1.10 = +10% on material. Don't count the prime tower and purge (already in the grams) or failed prints (they have their own reserve). Typical: 1.05 to 1.15; 1 = no waste.","sub":"FDM selling price of the sliced plate","readyT":"Ready to price","readyP":"Slice a plate in OrcaSlicer and its selling price shows up here, with material, electricity, printer wear and your margin.","s1":"Pick printer and filaments","s2":"Slice the plate","s3":"See the price","reading":"Reading the G-code","failT":"Could not calculate","warnT":"Heads up","noPrinter":"Unnamed printer","updated":"Updated at {t}","priceT":"Price per part incl. tax","mine":"your margin","sim":"this plate's margin","profit":"profit per part","margin":"margin","plate":"whole plate ({n} parts)","time":"plate time","grams":"filament per part","copy1":"part on the plate","copyN":"identical parts on the plate","unit":"cost per part","fils":"Filaments","color1":"colour","colorN":"colours","perKg":"/kg","tower":"Prime tower and purge, already in the grams","costT":"Cost per part","profitT":"Profit at the chosen margin","c_material":"Material","c_energy":"Electricity","c_machine":"Printer wear","c_reserve":"Machine reserve","c_failure":"Failure reserve","c_labor":"Labour","c_pack":"Packaging","src_custom":"Set by you","src_orca":"OrcaSlicer profile","src_average":"Average price","src_generic":"Generic values","cfgT":"Pricing settings","cfgS":"general, printer and filaments","general":"General","printer":"Printer","p_price":"Printer price","p_life":"Service life","years":"years","p_uptime":"Yearly usage","p_maint":"Yearly maintenance","p_power":"Average power","p_buffer":"Reserve factor","f_note":"The price per kg comes from the \"Filament cost\" field of the OrcaSlicer profile. Change it here to keep your own price for that filament.","scan":"Find already sliced plates","scanP":"Sliced before turning the plugin on? Select the last plate you sliced in OrcaSlicer and the plugin reads its G-code, without slicing again.","platesCost":"Your total cost","plateN":"Plate {n}","platesTotal":"Total of {n} plates, incl. tax","notSliced":"{n} has not been sliced yet","notSlicedP":"Slice this plate to see its price. The price of each sliced plate is kept while the project is open.","customM":"Custom margin","save":"Save and recalculate","reset":"Reset printer values","h":"h","min":"min","cfg_intro":"Values used for the selling price of every sliced plate. You can also change them in the Print Calculator tab, with each printer's cost profile and the filament prices.","cfg_ro":"Read only.","cfg_currency":"Currency","cfg_search":"Search currency (name or code)","cfg_vat":"Sales tax / VAT (%)","cfg_margin":"Margin (%)","cfg_failure":"Failure rate (%)","cfg_electricity":"Electricity (per kWh)","cfg_rate":"Labour (per hour)","cfg_setup":"Setup per plate (min)","cfg_finish":"Finishing per part (min)","cfg_pack":"Packaging per part","cfg_efficiency":"Material waste factor (×)","py_perm":"OrcaSlicer did not allow reading the G-code. When it asks, choose Yes and slice again.","py_missing":"The G-code no longer exists. Slice again.","py_empty":"This G-code has no print time or filament weight that Print Calculator can read.","py_fail":"Could not calculate: {e}","py_unit":"{p} per part incl. tax","py_plate":" ({n} parts, {t})","py_hint":"Print Calculator: open the Print Calculator tab to see the price of this plate.","py_none":"No sliced plates found in this OrcaSlicer session. Slice the plate, or export the G-code (Ctrl+G) if it is already sliced.","cfg_language":"Language","cfg_auto":"Same as OrcaSlicer","cfg_minimum":"Minimum price per part, incl. tax","cfg_rounding":"Round price per part up","cfg_round_none":"Don't round","cfg_round_99":"Up to .99","cfg_discount_qty":"Quantity discount from (parts on the plate)","cfg_discount_pct":"Quantity discount (%)","adj_disc":"quantity discount −{v}%","adj_min":"minimum price","adj_round":"rounded","refShort":"€ reference","refNote":"Prices marked “€ reference” are averages in euros, not converted to {c}. Check them in the settings below.","extrasT":"Extras","extrasP":"Magnets, screws, inserts, paint… Choose whether each extra counts for every part or once for the plate. Remembered for this job.","extraName":"Item","extraCost":"Cost","extraAdd":"Add extra","c_extras":"Extras","quoteBtn":"Copy quote","quoteAll":"Copy quote for all plates","copied":"Quote copied. Paste it in your message.","q_title":"Quote","q_qty":"Quantity","q_unit":"Price per part","q_total":"Total","q_time":"Print time","q_material":"Material","q_taxinc":"tax included","byModelT":"Price per model","byModelP":"Material is split by each model's filament; time, machine and setup costs by its share of the filament. Estimate.","modelN":"Model {n}","mdlCost":"cost/part","mdlPrice":"price/part","cfg_updates":"Check for new versions when the tab opens","upd_new":"Print Calculator {v} is available. Update it in OrcaSlicer's plugin manager.","allBtn":"All plates","allCost":"Total cost","priceSetT":"Plate price incl. tax","profitSet":"profit for the plate","gramsSet":"filament for the plate","partsN":"parts on the plate","unitSet":"cost of the plate","costSetT":"Cost of the plate","py_set":"{p} for the plate incl. tax ({n} parts)","jobN":"Job {n}","jobNameP":"Name (customer, project…)","jobP":"Choose the job of each plate. Each job has its own total, margin and quote.","jobMargin":"Job margin","jobTime":"total time","jobProfit":"profit","sumCost":"Cost","sumTax":"Sales tax","sumProfit":"Profit","setupT":"First, confirm your values","setupP":"The price appears only after you confirm the settings below: currency, tax, electricity, the price of your printer and the price per kg of each filament. The values already filled in are reference values and may not be yours. Then press \"Save and recalculate\".","repBtn":"Report a problem","repBusy":"Preparing the G-code…","repDone":"G-code saved to {p}. Drag this file into the issue that opened in your browser and describe the problem.","repNoG":"This plate has no G-code. The issue opened without a file: describe the problem and, if you can, slice and try again.","repLink":"If the browser didn't open, open this link:","noLabels":"This G-code doesn't say how many parts are on the plate, so the price is for the whole plate. To count the parts, turn on \"Label objects\" in the OrcaSlicer process settings (Others) and slice again.","exPart":"per part","exPlate":"per plate"},"es":{"imp":"Importar archivo laminado","impBig":"Este archivo es demasiado grande (máximo 200 MB).","impFail":"No se pudo leer este archivo.","sliceAllP":"¿Varias placas? Usa \"Laminar todo\" en OrcaSlicer y aparecen todas aquí. También puedes importar un .gcode.3mf o .gcode.","chT":"Canales de venta","chP":"El precio de cada canal ya cubre la comisión y el IVA: tu beneficio es el mismo en todos.","chDirect":"Venta directa","chN":"Canal","chFee":"Comisión","chPiece":"Por pieza","qChannel":"Precio para este canal","chAdd":"Añadir canal","chName":"Nombre (p. ej. Etsy)","chNote":"Comisión en % sobre el precio con IVA y comisión fija por venta. El plugin sube el precio para que tengas el mismo beneficio.","withShip":"con envío ({s})","q_ship":"Envío","q_grand":"Total con envío","qSave":"Guardar presupuesto","qSaved":"Presupuesto guardado","qsT":"Presupuestos guardados","qsCopy":"Copiar","qsPdf":"Imprimir / PDF","qsDel":"Borrar","qsCsv":"Exportar CSV","qsFile":"Guardado en {p}","qsFileOpen":"Guardado en {p}. Ábrelo para imprimir o guardar en PDF.","spoolBtn":"Leer precios de Spoolman","spoolPick":"Spoolman…","spoolErr":"No se pudo leer Spoolman: {e}","spoolOk":"{n} filamentos de Spoolman. Elige uno junto a cada filamento y guarda.","spoolNone":"Spoolman no tiene filamentos con precio y peso.","cfg_shipping":"Envío por pedido","cfg_q_name":"Nombre en el presupuesto (empresa)","cfg_q_footer":"Nota al final del presupuesto","cfg_spoolman":"Dirección de Spoolman (opcional)","qh_item":"Artículo","qh_qty":"Cant.","qh_unit":"Precio unitario","qh_vat":"IVA incluido ({v}%)","qh_date":"Fecha","buf_note":"Factor de reserva: suma un margen a los costes por hora de la impresora (desgaste, mantenimiento y electricidad) para lo imprevisto: averías, piezas (hotend, boquilla, placa, correas), tiempo parado y ahorrar para la próxima impresora. 1,3 = +30 %. No cambia el material. Normal: 1,2 a 1,3; impresora con muchos problemas: 1,4 a 1,5; 1 = sin reserva.","eff_note":"Factor de pérdida de material: multiplica solo el coste del filamento, para el plástico que el G-code no cuenta (restos de bobina, cambios de filamento, pruebas, filamento estropeado). 1,10 = +10 % en material. No cuentes la torre y la purga (ya están en los gramos) ni los fallos (tienen su propia reserva). Normal: 1,05 a 1,15; 1 = sin pérdidas.","sub":"Precio de venta FDM de la placa laminada","readyT":"Listo para calcular","readyP":"Lamina una placa en OrcaSlicer y aquí aparecerá su precio de venta, con material, electricidad, desgaste de la impresora y tu margen.","s1":"Elige impresora y filamentos","s2":"Lamina la placa","s3":"Mira el precio","reading":"Leyendo el G-code","failT":"No se pudo calcular","warnT":"Atención","noPrinter":"Impresora sin nombre","updated":"Actualizado a las {t}","priceT":"Precio por pieza con impuestos","mine":"tu margen","sim":"margen de esta placa","profit":"beneficio por pieza","margin":"margen","plate":"placa completa ({n} piezas)","time":"tiempo de la placa","grams":"filamento por pieza","copy1":"pieza en la placa","copyN":"piezas iguales en la placa","unit":"coste por pieza","fils":"Filamentos","color1":"color","colorN":"colores","perKg":"/kg","tower":"Torre de purga y purgas, ya incluidas en los gramos","costT":"Coste por pieza","profitT":"Beneficio con el margen elegido","c_material":"Material","c_energy":"Electricidad","c_machine":"Desgaste de la impresora","c_reserve":"Reserva de la máquina","c_failure":"Reserva por fallos","c_labor":"Mano de obra","c_pack":"Embalaje","src_custom":"Definido por ti","src_orca":"Perfil de OrcaSlicer","src_average":"Precio medio","src_generic":"Valores genéricos","cfgT":"Ajustes de precio","cfgS":"general, impresora y filamentos","general":"General","printer":"Impresora","p_price":"Precio de la impresora","p_life":"Vida útil","years":"años","p_uptime":"Uso anual","p_maint":"Mantenimiento anual","p_power":"Consumo medio","p_buffer":"Factor de reserva","f_note":"El precio por kg viene del campo \"Coste del filamento\" del perfil de OrcaSlicer. Si lo cambias aquí, se guarda para ese filamento.","scan":"Buscar placas ya laminadas","scanP":"¿Laminaste antes de activar el plugin? Selecciona en OrcaSlicer la última placa que laminaste y el plugin lee su G-code sin volver a laminar.","platesCost":"Tu coste total","plateN":"Placa {n}","platesTotal":"Total de {n} placas, con impuestos","notSliced":"{n} aún no se ha laminado","notSlicedP":"Lamina esta placa para ver su precio. El precio de cada placa laminada se guarda mientras el proyecto esté abierto.","customM":"Margen personalizado","save":"Guardar y recalcular","reset":"Restablecer valores de la impresora","h":"h","min":"min","cfg_intro":"Valores usados para el precio de venta de cada placa laminada. También puedes cambiarlos, junto con el perfil de cada impresora y el precio de los filamentos, en la pestaña Print Calculator.","cfg_ro":"Solo lectura.","cfg_currency":"Moneda","cfg_search":"Buscar moneda (nombre o código)","cfg_vat":"IVA / impuesto (%)","cfg_margin":"Margen (%)","cfg_failure":"Tasa de fallos (%)","cfg_electricity":"Electricidad (por kWh)","cfg_rate":"Mano de obra (por hora)","cfg_setup":"Preparación por placa (min)","cfg_finish":"Acabado por pieza (min)","cfg_pack":"Embalaje por pieza","cfg_efficiency":"Factor de pérdida de material (×)","cfg_language":"Idioma","cfg_auto":"Igual que OrcaSlicer","py_perm":"OrcaSlicer no permitió leer el G-code. Cuando lo pregunte, elige Sí y vuelve a laminar.","py_missing":"El G-code ya no existe. Vuelve a laminar.","py_empty":"Este G-code no tiene tiempo ni gramos que Print Calculator pueda leer.","py_fail":"No se pudo calcular: {e}","py_unit":"{p} por pieza con impuestos","py_plate":" ({n} piezas, {t})","py_hint":"Print Calculator: abre la pestaña Print Calculator para ver el precio de esta placa.","py_none":"No se encontraron placas laminadas en esta sesión de OrcaSlicer. Lamina la placa o exporta el G-code (Ctrl+G) si ya está laminada.","cfg_minimum":"Precio mínimo por pieza, con impuestos","cfg_rounding":"Redondear el precio por pieza hacia arriba","cfg_round_none":"No redondear","cfg_round_99":"Hasta ,99","cfg_discount_qty":"Descuento por cantidad desde (piezas en la placa)","cfg_discount_pct":"Descuento por cantidad (%)","adj_disc":"descuento por cantidad −{v}%","adj_min":"precio mínimo","adj_round":"redondeado","refShort":"referencia en €","refNote":"Los precios marcados \"referencia en €\" son medias en euros, no convertidas a {c}. Revísalos en los ajustes de abajo.","extrasT":"Extras","extrasP":"Imanes, tornillos, insertos, pintura… Elige si cada extra cuenta en cada pieza o una vez en la placa. Se guardan para este trabajo.","extraName":"Artículo","extraCost":"Coste","extraAdd":"Añadir extra","c_extras":"Extras","quoteBtn":"Copiar presupuesto","quoteAll":"Copiar presupuesto de todas las placas","copied":"Presupuesto copiado. Pégalo en tu mensaje.","q_title":"Presupuesto","q_qty":"Cantidad","q_unit":"Precio por pieza","q_total":"Total","q_time":"Tiempo de impresión","q_material":"Material","q_taxinc":"impuestos incluidos","byModelT":"Precio por modelo","byModelP":"El material se reparte según el filamento de cada modelo; tiempo, máquina y preparación según su parte del filamento. Es una estimación.","modelN":"Modelo {n}","mdlCost":"coste/pieza","mdlPrice":"precio/pieza","cfg_updates":"Buscar versiones nuevas al abrir la pestaña","upd_new":"Print Calculator {v} está disponible. Actualízalo en el gestor de plugins de OrcaSlicer.","allBtn":"Todas las placas","allCost":"Coste total","priceSetT":"Precio de la placa con impuestos","profitSet":"beneficio de la placa","gramsSet":"filamento de la placa","partsN":"piezas en la placa","unitSet":"coste de la placa","costSetT":"Coste de la placa","py_set":"{p} por la placa con impuestos ({n} piezas)","jobN":"Trabajo {n}","jobNameP":"Nombre (cliente, proyecto…)","jobP":"Elige el trabajo de cada placa. Cada trabajo tiene su total, su margen y su presupuesto.","jobMargin":"Margen del trabajo","jobTime":"tiempo total","jobProfit":"beneficio","sumCost":"Coste","sumTax":"IVA","sumProfit":"Beneficio","setupT":"Primero, confirma tus valores","setupP":"El precio solo aparece después de confirmar los ajustes de abajo: moneda, impuestos, electricidad, el precio de tu impresora y el precio por kg de cada filamento. Los valores ya rellenados son de referencia y puede que no sean los tuyos. Después pulsa el botón de guardar.","repBtn":"Informar de un problema","repBusy":"Preparando el G-code…","repDone":"G-code guardado en {p}. Arrastra este archivo al issue que se abrió en el navegador y describe el problema.","repNoG":"Esta placa no tiene G-code. El issue se abrió sin archivo: describe el problema y, si puedes, lamina y vuelve a intentarlo.","repLink":"Si el navegador no se abrió, abre este enlace:","noLabels":"Este G-code no indica cuántas piezas hay en la placa, así que el precio es el de la placa entera. Para contar las piezas, activa \"Label objects\" (Etiquetar objetos) en el proceso de OrcaSlicer, en Otros, y vuelve a laminar.","exPart":"por pieza","exPlate":"por placa"},"fr":{"imp":"Importer un fichier tranché","impBig":"Ce fichier est trop gros (200 Mo maximum).","impFail":"Impossible de lire ce fichier.","sliceAllP":"Plusieurs plateaux ? Utilisez « Tout trancher » dans OrcaSlicer et ils apparaissent tous ici. Vous pouvez aussi importer un .gcode.3mf ou .gcode.","chT":"Canaux de vente","chP":"Le prix de chaque canal couvre déjà sa commission et la TVA : votre bénéfice est le même partout.","chDirect":"Vente directe","chN":"Canal","chFee":"Commission","chPiece":"Par pièce","qChannel":"Prix pour ce canal","chAdd":"Ajouter un canal","chName":"Nom (ex. Etsy)","chNote":"Commission en % du prix TTC et frais fixes par vente. Le plugin augmente le prix pour garder le même bénéfice.","withShip":"avec livraison ({s})","q_ship":"Livraison","q_grand":"Total avec livraison","qSave":"Enregistrer le devis","qSaved":"Devis enregistré","qsT":"Devis enregistrés","qsCopy":"Copier","qsPdf":"Imprimer / PDF","qsDel":"Supprimer","qsCsv":"Exporter en CSV","qsFile":"Enregistré dans {p}","qsFileOpen":"Enregistré dans {p}. Ouvrez-le pour l'imprimer ou l'enregistrer en PDF.","spoolBtn":"Lire les prix de Spoolman","spoolPick":"Spoolman…","spoolErr":"Impossible de lire Spoolman : {e}","spoolOk":"{n} filaments Spoolman. Choisissez-en un à côté de chaque filament et enregistrez.","spoolNone":"Spoolman n'a aucun filament avec prix et poids.","cfg_shipping":"Livraison par commande","cfg_q_name":"Nom sur les devis (entreprise)","cfg_q_footer":"Note en bas des devis","cfg_spoolman":"Adresse de Spoolman (facultatif)","qh_item":"Article","qh_qty":"Qté","qh_unit":"Prix unitaire","qh_vat":"TVA incluse ({v} %)","qh_date":"Date","buf_note":"Facteur de réserve : ajoute une marge aux coûts horaires de l'imprimante (usure, entretien et électricité) pour l'imprévu : pannes, pièces (hotend, buse, plateau, courroies), immobilisation et épargne pour la prochaine imprimante. 1,3 = +30 %. Ne change pas la matière. Habituel : 1,2 à 1,3 ; imprimante capricieuse : 1,4 à 1,5 ; 1 = aucune réserve.","eff_note":"Facteur de perte de matière : multiplie seulement le coût du filament, pour le plastique que le G-code ne compte pas (fins de bobine, changements de filament, tests, filament abîmé). 1,10 = +10 % sur la matière. Ne comptez pas la tour et la purge (déjà dans les grammes) ni les échecs (ils ont leur propre réserve). Habituel : 1,05 à 1,15 ; 1 = aucune perte.","sub":"Prix de vente FDM du plateau tranché","readyT":"Prêt à calculer","readyP":"Tranchez un plateau dans OrcaSlicer et son prix de vente s'affiche ici, avec la matière, l'électricité, l'usure de l'imprimante et votre marge.","s1":"Choisissez imprimante et filaments","s2":"Tranchez le plateau","s3":"Voyez le prix","reading":"Lecture du G-code","failT":"Calcul impossible","warnT":"Attention","noPrinter":"Imprimante sans nom","updated":"Mis à jour à {t}","priceT":"Prix par pièce TTC","mine":"votre marge","sim":"marge de ce plateau","profit":"bénéfice par pièce","margin":"marge","plate":"plateau entier ({n} pièces)","time":"durée du plateau","grams":"filament par pièce","copy1":"pièce sur le plateau","copyN":"pièces identiques sur le plateau","unit":"coût par pièce","fils":"Filaments","color1":"couleur","colorN":"couleurs","perKg":"/kg","tower":"Tour de purge et purges, déjà incluses dans les grammes","costT":"Coût par pièce","profitT":"Bénéfice avec la marge choisie","c_material":"Matière","c_energy":"Électricité","c_machine":"Usure de l'imprimante","c_reserve":"Réserve machine","c_failure":"Réserve pour échecs","c_labor":"Main-d'œuvre","c_pack":"Emballage","src_custom":"Défini par vous","src_orca":"Profil OrcaSlicer","src_average":"Prix moyen","src_generic":"Valeurs génériques","cfgT":"Réglages de prix","cfgS":"général, imprimante et filaments","general":"Général","printer":"Imprimante","p_price":"Prix de l'imprimante","p_life":"Durée de vie","years":"ans","p_uptime":"Utilisation annuelle","p_maint":"Entretien annuel","p_power":"Consommation moyenne","p_buffer":"Facteur de réserve","f_note":"Le prix au kg vient du champ « Coût du filament » du profil OrcaSlicer. Si vous le modifiez ici, il est gardé pour ce filament.","scan":"Trouver les plateaux déjà tranchés","scanP":"Tranché avant d'activer le plugin ? Sélectionnez dans OrcaSlicer le dernier plateau tranché et le plugin lit son G-code sans retrancher.","platesCost":"Votre coût total","plateN":"Plateau {n}","platesTotal":"Total de {n} plateaux, TTC","notSliced":"{n} n'est pas encore tranché","notSlicedP":"Tranchez ce plateau pour voir son prix. Le prix de chaque plateau tranché est gardé tant que le projet est ouvert.","customM":"Marge personnalisée","save":"Enregistrer et recalculer","reset":"Réinitialiser l'imprimante","h":"h","min":"min","cfg_intro":"Valeurs utilisées pour le prix de vente de chaque plateau tranché. Vous pouvez aussi les modifier, ainsi que le profil de chaque imprimante et le prix des filaments, dans l'onglet Print Calculator.","cfg_ro":"Lecture seule.","cfg_currency":"Devise","cfg_search":"Chercher une devise (nom ou code)","cfg_vat":"TVA / taxe (%)","cfg_margin":"Marge (%)","cfg_failure":"Taux d'échec (%)","cfg_electricity":"Électricité (par kWh)","cfg_rate":"Main-d'œuvre (par heure)","cfg_setup":"Préparation par plateau (min)","cfg_finish":"Finition par pièce (min)","cfg_pack":"Emballage par pièce","cfg_efficiency":"Facteur de perte de matière (×)","cfg_language":"Langue","cfg_auto":"Comme OrcaSlicer","py_perm":"OrcaSlicer n'a pas autorisé la lecture du G-code. Quand il le demande, choisissez Oui et retranchez.","py_missing":"Le G-code n'existe plus. Retranchez.","py_empty":"Ce G-code n'a ni durée ni grammes lisibles par Print Calculator.","py_fail":"Calcul impossible : {e}","py_unit":"{p} par pièce TTC","py_plate":" ({n} pièces, {t})","py_hint":"Print Calculator : ouvrez l'onglet Print Calculator pour voir le prix de ce plateau.","py_none":"Aucun plateau tranché dans cette session d'OrcaSlicer. Tranchez le plateau, ou exportez le G-code (Ctrl+G) s'il est déjà tranché.","cfg_minimum":"Prix minimum par pièce, TTC","cfg_rounding":"Arrondir le prix par pièce au-dessus","cfg_round_none":"Ne pas arrondir","cfg_round_99":"Jusqu'à ,99","cfg_discount_qty":"Remise de quantité à partir de (pièces sur le plateau)","cfg_discount_pct":"Remise de quantité (%)","adj_disc":"remise de quantité −{v} %","adj_min":"prix minimum","adj_round":"arrondi","refShort":"référence en €","refNote":"Les prix marqués « référence en € » sont des moyennes en euros, non converties en {c}. Vérifiez-les dans les réglages ci-dessous.","extrasT":"Extras","extrasP":"Aimants, vis, inserts, peinture… Choisissez si chaque extra compte pour chaque pièce ou une fois pour le plateau. Mémorisés pour ce travail.","extraName":"Article","extraCost":"Coût","extraAdd":"Ajouter un extra","c_extras":"Extras","quoteBtn":"Copier le devis","quoteAll":"Copier le devis de tous les plateaux","copied":"Devis copié. Collez-le dans votre message.","q_title":"Devis","q_qty":"Quantité","q_unit":"Prix par pièce","q_total":"Total","q_time":"Durée d'impression","q_material":"Matière","q_taxinc":"TTC","byModelT":"Prix par modèle","byModelP":"La matière est répartie selon le filament de chaque modèle ; le temps, la machine et la préparation selon sa part du filament. C'est une estimation.","modelN":"Modèle {n}","mdlCost":"coût/pièce","mdlPrice":"prix/pièce","cfg_updates":"Chercher les nouvelles versions à l'ouverture de l'onglet","upd_new":"Print Calculator {v} est disponible. Mettez-le à jour dans le gestionnaire de plugins d'OrcaSlicer.","allBtn":"Tous les plateaux","allCost":"Coût total","priceSetT":"Prix du plateau TTC","profitSet":"bénéfice du plateau","gramsSet":"filament du plateau","partsN":"pièces sur le plateau","unitSet":"coût du plateau","costSetT":"Coût du plateau","py_set":"{p} pour le plateau TTC ({n} pièces)","jobN":"Travail {n}","jobNameP":"Nom (client, projet…)","jobP":"Choisissez le travail de chaque plateau. Chaque travail a son total, sa marge et son devis.","jobMargin":"Marge du travail","jobTime":"durée totale","jobProfit":"bénéfice","sumCost":"Coût","sumTax":"TVA","sumProfit":"Bénéfice","setupT":"D'abord, confirmez vos valeurs","setupP":"Le prix n'apparaît qu'après avoir confirmé les réglages ci-dessous : devise, taxe, électricité, le prix de votre imprimante et le prix au kg de chaque filament. Les valeurs déjà remplies sont des valeurs de référence et ne sont peut-être pas les vôtres. Appuyez ensuite sur le bouton d'enregistrement.","repBtn":"Signaler un problème","repBusy":"Préparation du G-code…","repDone":"G-code enregistré dans {p}. Glissez ce fichier dans l'issue ouverte dans votre navigateur et décrivez le problème.","repNoG":"Ce plateau n'a pas de G-code. L'issue s'est ouverte sans fichier : décrivez le problème et, si possible, tranchez et réessayez.","repLink":"Si le navigateur ne s'est pas ouvert, ouvrez ce lien :","noLabels":"Ce G-code n'indique pas combien de pièces il y a sur le plateau : le prix est celui du plateau entier. Pour compter les pièces, activez \"Label objects\" dans les réglages de traitement d'OrcaSlicer (Autres) et retranchez.","exPart":"par pièce","exPlate":"par plateau"},"de":{"imp":"Geslicte Datei importieren","impBig":"Diese Datei ist zu groß (maximal 200 MB).","impFail":"Diese Datei konnte nicht gelesen werden.","sliceAllP":"Mehrere Platten? Nutze „Alle slicen“ in OrcaSlicer, dann erscheinen alle hier. Du kannst auch eine .gcode.3mf oder .gcode importieren.","chT":"Verkaufskanäle","chP":"Der Preis jedes Kanals deckt Gebühr und Steuer bereits ab: dein Gewinn ist überall gleich.","chDirect":"Direktverkauf","chN":"Kanal","chFee":"Gebühr","chPiece":"Pro Teil","qChannel":"Preis für diesen Kanal","chAdd":"Kanal hinzufügen","chName":"Name (z. B. Etsy)","chNote":"Gebühr in % vom Preis inkl. Steuer plus feste Gebühr pro Verkauf. Das Plugin erhöht den Preis, damit dein Gewinn gleich bleibt.","withShip":"mit Versand ({s})","q_ship":"Versand","q_grand":"Gesamt mit Versand","qSave":"Angebot speichern","qSaved":"Angebot gespeichert","qsT":"Gespeicherte Angebote","qsCopy":"Kopieren","qsPdf":"Drucken / PDF","qsDel":"Löschen","qsCsv":"Als CSV exportieren","qsFile":"Gespeichert in {p}","qsFileOpen":"Gespeichert in {p}. Öffne es zum Drucken oder Speichern als PDF.","spoolBtn":"Preise aus Spoolman lesen","spoolPick":"Spoolman…","spoolErr":"Spoolman konnte nicht gelesen werden: {e}","spoolOk":"{n} Filamente aus Spoolman. Wähle eins neben jedem Filament und speichere.","spoolNone":"Spoolman hat keine Filamente mit Preis und Gewicht.","cfg_shipping":"Versand pro Bestellung","cfg_q_name":"Name im Angebot (Firma)","cfg_q_footer":"Hinweis am Ende des Angebots","cfg_spoolman":"Spoolman-Adresse (optional)","qh_item":"Artikel","qh_qty":"Menge","qh_unit":"Stückpreis","qh_vat":"Inkl. Steuer ({v} %)","qh_date":"Datum","buf_note":"Reservefaktor: schlägt auf die Stundenkosten des Druckers (Verschleiß, Wartung und Strom) eine Marge für Unvorhergesehenes auf: Defekte, Teile (Hotend, Düse, Druckplatte, Riemen), Ausfallzeit und Rücklage für den nächsten Drucker. 1,3 = +30 %. Ändert das Material nicht. Üblich: 1,2 bis 1,3; problematischer Drucker: 1,4 bis 1,5; 1 = keine Reserve.","eff_note":"Materialverlustfaktor: multipliziert nur die Filamentkosten, für Kunststoff, den der G-Code nicht zählt (Spulenreste, Filamentwechsel, Testdrucke, verdorbenes Filament). 1,10 = +10 % auf das Material. Prime-Turm und Spülung nicht mitzählen (schon in den Gramm), Fehldrucke auch nicht (eigene Reserve). Üblich: 1,05 bis 1,15; 1 = kein Verlust.","sub":"FDM-Verkaufspreis der geslicten Platte","readyT":"Bereit zur Berechnung","readyP":"Slice eine Platte in OrcaSlicer und hier erscheint ihr Verkaufspreis, mit Material, Strom, Druckerverschleiß und deiner Marge.","s1":"Drucker und Filamente wählen","s2":"Platte slicen","s3":"Preis ansehen","reading":"G-Code wird gelesen","failT":"Berechnung nicht möglich","warnT":"Hinweis","noPrinter":"Unbenannter Drucker","updated":"Aktualisiert um {t}","priceT":"Preis pro Teil inkl. Steuer","mine":"deine Marge","sim":"Marge dieser Platte","profit":"Gewinn pro Teil","margin":"Marge","plate":"ganze Platte ({n} Teile)","time":"Druckzeit der Platte","grams":"Filament pro Teil","copy1":"Teil auf der Platte","copyN":"gleiche Teile auf der Platte","unit":"Kosten pro Teil","fils":"Filamente","color1":"Farbe","colorN":"Farben","perKg":"/kg","tower":"Reinigungsturm und Spülen, bereits in den Gramm enthalten","costT":"Kosten pro Teil","profitT":"Gewinn bei gewählter Marge","c_material":"Material","c_energy":"Strom","c_machine":"Druckerverschleiß","c_reserve":"Maschinenreserve","c_failure":"Reserve für Fehldrucke","c_labor":"Arbeitszeit","c_pack":"Verpackung","src_custom":"Von dir festgelegt","src_orca":"OrcaSlicer-Profil","src_average":"Durchschnittspreis","src_generic":"Standardwerte","cfgT":"Preiseinstellungen","cfgS":"Allgemein, Drucker und Filamente","general":"Allgemein","printer":"Drucker","p_price":"Druckerpreis","p_life":"Lebensdauer","years":"Jahre","p_uptime":"Jährliche Nutzung","p_maint":"Jährliche Wartung","p_power":"Durchschnittliche Leistung","p_buffer":"Reservefaktor","f_note":"Der Preis pro kg kommt aus dem Feld „Filamentkosten“ des OrcaSlicer-Profils. Änderst du ihn hier, bleibt er für dieses Filament gespeichert.","scan":"Bereits geslicte Platten suchen","scanP":"Vor dem Aktivieren des Plugins geslict? Wähle in OrcaSlicer die zuletzt geslicte Platte und das Plugin liest ihren G-Code, ohne neu zu slicen.","platesCost":"Deine Gesamtkosten","plateN":"Platte {n}","platesTotal":"Summe von {n} Platten, inkl. Steuer","notSliced":"{n} wurde noch nicht geslict","notSlicedP":"Slice diese Platte, um ihren Preis zu sehen. Der Preis jeder geslicten Platte bleibt erhalten, solange das Projekt offen ist.","customM":"Eigene Marge","save":"Speichern und neu berechnen","reset":"Druckerwerte zurücksetzen","h":"h","min":"min","cfg_intro":"Werte für den Verkaufspreis jeder geslicten Platte. Du kannst sie auch, zusammen mit dem Kostenprofil jedes Druckers und den Filamentpreisen, im Tab Print Calculator ändern.","cfg_ro":"Nur lesen.","cfg_currency":"Währung","cfg_search":"Währung suchen (Name oder Code)","cfg_vat":"MwSt. / Steuer (%)","cfg_margin":"Marge (%)","cfg_failure":"Fehlerquote (%)","cfg_electricity":"Strom (pro kWh)","cfg_rate":"Arbeitszeit (pro Stunde)","cfg_setup":"Vorbereitung pro Platte (min)","cfg_finish":"Nachbearbeitung pro Teil (min)","cfg_pack":"Verpackung pro Teil","cfg_efficiency":"Materialverlustfaktor (×)","cfg_language":"Sprache","cfg_auto":"Wie OrcaSlicer","py_perm":"OrcaSlicer hat das Lesen des G-Codes nicht erlaubt. Wähle bei der Nachfrage Ja und slice erneut.","py_missing":"Der G-Code existiert nicht mehr. Slice erneut.","py_empty":"Dieser G-Code enthält keine Druckzeit oder Gramm, die Print Calculator lesen kann.","py_fail":"Berechnung nicht möglich: {e}","py_unit":"{p} pro Teil inkl. Steuer","py_plate":" ({n} Teile, {t})","py_hint":"Print Calculator: Öffne den Tab Print Calculator, um den Preis dieser Platte zu sehen.","py_none":"Keine geslicten Platten in dieser OrcaSlicer-Sitzung gefunden. Slice die Platte oder exportiere den G-Code (Strg+G), wenn sie schon geslict ist.","cfg_minimum":"Mindestpreis pro Teil, inkl. Steuer","cfg_rounding":"Preis pro Teil aufrunden","cfg_round_none":"Nicht runden","cfg_round_99":"Auf ,99","cfg_discount_qty":"Mengenrabatt ab (Teile auf der Platte)","cfg_discount_pct":"Mengenrabatt (%)","adj_disc":"Mengenrabatt −{v} %","adj_min":"Mindestpreis","adj_round":"gerundet","refShort":"€-Richtwert","refNote":"Als „€-Richtwert“ markierte Preise sind Durchschnittswerte in Euro, nicht in {c} umgerechnet. Prüfe sie unten in den Einstellungen.","extrasT":"Extras","extrasP":"Magnete, Schrauben, Gewindeeinsätze, Farbe… Wähle, ob jedes Extra pro Teil oder einmal pro Platte zählt. Wird für diesen Auftrag gespeichert.","extraName":"Artikel","extraCost":"Kosten","extraAdd":"Extra hinzufügen","c_extras":"Extras","quoteBtn":"Angebot kopieren","quoteAll":"Angebot für alle Platten kopieren","copied":"Angebot kopiert. Füge es in deine Nachricht ein.","q_title":"Angebot","q_qty":"Menge","q_unit":"Preis pro Teil","q_total":"Summe","q_time":"Druckzeit","q_material":"Material","q_taxinc":"inkl. Steuer","byModelT":"Preis pro Modell","byModelP":"Material wird nach dem Filament jedes Modells aufgeteilt; Zeit, Maschine und Vorbereitung nach seinem Filamentanteil. Schätzung.","modelN":"Modell {n}","mdlCost":"Kosten/Teil","mdlPrice":"Preis/Teil","cfg_updates":"Beim Öffnen des Tabs nach neuen Versionen suchen","upd_new":"Print Calculator {v} ist verfügbar. Aktualisiere es in der Plugin-Verwaltung von OrcaSlicer.","allBtn":"Alle Platten","allCost":"Gesamtkosten","priceSetT":"Preis der Platte inkl. Steuer","profitSet":"Gewinn der Platte","gramsSet":"Filament der Platte","partsN":"Teile auf der Platte","unitSet":"Kosten der Platte","costSetT":"Kosten der Platte","py_set":"{p} für die Platte inkl. Steuer ({n} Teile)","jobN":"Auftrag {n}","jobNameP":"Name (Kunde, Projekt…)","jobP":"Wähle den Auftrag jeder Platte. Jeder Auftrag hat seine eigene Summe, Marge und sein eigenes Angebot.","jobMargin":"Marge des Auftrags","jobTime":"Gesamtzeit","jobProfit":"Gewinn","sumCost":"Kosten","sumTax":"MwSt.","sumProfit":"Gewinn","setupT":"Bestätige zuerst deine Werte","setupP":"Der Preis erscheint erst, nachdem du die Einstellungen unten bestätigt hast: Währung, Steuer, Strom, den Preis deines Druckers und den Preis pro kg jedes Filaments. Die bereits eingetragenen Werte sind Richtwerte und müssen nicht deine sein. Drücke danach auf Speichern.","repBtn":"Problem melden","repBusy":"G-Code wird vorbereitet…","repDone":"G-Code gespeichert unter {p}. Zieh diese Datei in das Issue, das sich im Browser geöffnet hat, und beschreibe das Problem.","repNoG":"Diese Platte hat keinen G-Code. Das Issue wurde ohne Datei geöffnet: Beschreibe das Problem und slice wenn möglich erneut.","repLink":"Falls sich der Browser nicht geöffnet hat, öffne diesen Link:","noLabels":"Dieser G-Code sagt nicht, wie viele Teile auf der Platte sind, daher gilt der Preis für die ganze Platte. Um die Teile zu zählen, aktiviere \"Label objects\" in den Prozesseinstellungen von OrcaSlicer (Sonstiges) und slice erneut.","exPart":"pro Teil","exPlate":"pro Platte"},"it":{"imp":"Importa file elaborato","impBig":"Questo file è troppo grande (massimo 200 MB).","impFail":"Impossibile leggere questo file.","sliceAllP":"Più piatti? Usa \"Elabora tutto\" in OrcaSlicer e compaiono tutti qui. Puoi anche importare un .gcode.3mf o .gcode.","chT":"Canali di vendita","chP":"Il prezzo di ogni canale copre già la commissione e l'IVA: il tuo guadagno è uguale ovunque.","chDirect":"Vendita diretta","chN":"Canale","chFee":"Commissione","chPiece":"Per pezzo","qChannel":"Prezzo per questo canale","chAdd":"Aggiungi canale","chName":"Nome (es. Etsy)","chNote":"Commissione in % sul prezzo con IVA più una commissione fissa per vendita. Il plugin alza il prezzo per mantenere lo stesso guadagno.","withShip":"con spedizione ({s})","q_ship":"Spedizione","q_grand":"Totale con spedizione","qSave":"Salva preventivo","qSaved":"Preventivo salvato","qsT":"Preventivi salvati","qsCopy":"Copia","qsPdf":"Stampa / PDF","qsDel":"Elimina","qsCsv":"Esporta CSV","qsFile":"Salvato in {p}","qsFileOpen":"Salvato in {p}. Aprilo per stamparlo o salvarlo in PDF.","spoolBtn":"Leggi i prezzi da Spoolman","spoolPick":"Spoolman…","spoolErr":"Impossibile leggere Spoolman: {e}","spoolOk":"{n} filamenti da Spoolman. Scegline uno accanto a ogni filamento e salva.","spoolNone":"Spoolman non ha filamenti con prezzo e peso.","cfg_shipping":"Spedizione per ordine","cfg_q_name":"Nome nel preventivo (azienda)","cfg_q_footer":"Nota in fondo al preventivo","cfg_spoolman":"Indirizzo di Spoolman (facoltativo)","qh_item":"Articolo","qh_qty":"Q.tà","qh_unit":"Prezzo unitario","qh_vat":"IVA inclusa ({v}%)","qh_date":"Data","buf_note":"Fattore di riserva: aggiunge un margine ai costi orari della stampante (usura, manutenzione ed elettricità) per l'imprevisto: guasti, ricambi (hotend, ugello, piatto, cinghie), fermi macchina e risparmio per la prossima stampante. 1,3 = +30%. Non cambia il materiale. Tipico: da 1,2 a 1,3; stampante problematica: da 1,4 a 1,5; 1 = nessuna riserva.","eff_note":"Fattore di perdita materiale: moltiplica solo il costo del filamento, per la plastica che il G-code non conta (fine bobina, cambi di filamento, prove, filamento rovinato). 1,10 = +10% sul materiale. Non contare la torre e lo spurgo (già nei grammi) né le stampe fallite (hanno la loro riserva). Tipico: da 1,05 a 1,15; 1 = nessuna perdita.","sub":"Prezzo di vendita FDM del piatto slicato","readyT":"Pronto per il calcolo","readyP":"Esegui lo slicing di un piatto in OrcaSlicer e qui compare il suo prezzo di vendita, con materiale, elettricità, usura della stampante e il tuo margine.","s1":"Scegli stampante e filamenti","s2":"Esegui lo slicing del piatto","s3":"Guarda il prezzo","reading":"Lettura del G-code","failT":"Impossibile calcolare","warnT":"Attenzione","noPrinter":"Stampante senza nome","updated":"Aggiornato alle {t}","priceT":"Prezzo per pezzo IVA inclusa","mine":"il tuo margine","sim":"margine di questo piatto","profit":"profitto per pezzo","margin":"margine","plate":"piatto intero ({n} pezzi)","time":"tempo del piatto","grams":"filamento per pezzo","copy1":"pezzo sul piatto","copyN":"pezzi uguali sul piatto","unit":"costo per pezzo","fils":"Filamenti","color1":"colore","colorN":"colori","perKg":"/kg","tower":"Torre di spurgo e spurghi, già inclusi nei grammi","costT":"Costo per pezzo","profitT":"Profitto con il margine scelto","c_material":"Materiale","c_energy":"Elettricità","c_machine":"Usura della stampante","c_reserve":"Riserva macchina","c_failure":"Riserva per errori","c_labor":"Manodopera","c_pack":"Imballaggio","src_custom":"Impostato da te","src_orca":"Profilo OrcaSlicer","src_average":"Prezzo medio","src_generic":"Valori generici","cfgT":"Impostazioni prezzo","cfgS":"generale, stampante e filamenti","general":"Generale","printer":"Stampante","p_price":"Prezzo della stampante","p_life":"Vita utile","years":"anni","p_uptime":"Utilizzo annuo","p_maint":"Manutenzione annua","p_power":"Consumo medio","p_buffer":"Fattore di riserva","f_note":"Il prezzo al kg viene dal campo \"Costo filamento\" del profilo OrcaSlicer. Se lo cambi qui, resta salvato per quel filamento.","scan":"Cerca piatti già slicati","scanP":"Hai fatto lo slicing prima di attivare il plugin? Seleziona in OrcaSlicer l'ultimo piatto slicato e il plugin legge il suo G-code senza rifare lo slicing.","platesCost":"Il tuo costo totale","plateN":"Piatto {n}","platesTotal":"Totale di {n} piatti, IVA inclusa","notSliced":"{n} non è ancora slicato","notSlicedP":"Esegui lo slicing di questo piatto per vederne il prezzo. Il prezzo di ogni piatto slicato resta finché il progetto è aperto.","customM":"Margine personalizzato","save":"Salva e ricalcola","reset":"Ripristina valori stampante","h":"h","min":"min","cfg_intro":"Valori usati per il prezzo di vendita di ogni piatto slicato. Puoi cambiarli anche nella scheda Print Calculator, insieme al profilo di ogni stampante e al prezzo dei filamenti.","cfg_ro":"Sola lettura.","cfg_currency":"Valuta","cfg_search":"Cerca valuta (nome o codice)","cfg_vat":"IVA / imposta (%)","cfg_margin":"Margine (%)","cfg_failure":"Tasso di errori (%)","cfg_electricity":"Elettricità (per kWh)","cfg_rate":"Manodopera (per ora)","cfg_setup":"Preparazione per piatto (min)","cfg_finish":"Finitura per pezzo (min)","cfg_pack":"Imballaggio per pezzo","cfg_efficiency":"Fattore di perdita materiale (×)","cfg_language":"Lingua","cfg_auto":"Come OrcaSlicer","py_perm":"OrcaSlicer non ha permesso di leggere il G-code. Quando lo chiede, scegli Sì e rifai lo slicing.","py_missing":"Il G-code non esiste più. Rifai lo slicing.","py_empty":"Questo G-code non ha tempo né grammi leggibili da Print Calculator.","py_fail":"Impossibile calcolare: {e}","py_unit":"{p} per pezzo IVA inclusa","py_plate":" ({n} pezzi, {t})","py_hint":"Print Calculator: apri la scheda Print Calculator per vedere il prezzo di questo piatto.","py_none":"Nessun piatto slicato in questa sessione di OrcaSlicer. Esegui lo slicing del piatto o esporta il G-code (Ctrl+G) se è già slicato.","cfg_minimum":"Prezzo minimo per pezzo, IVA inclusa","cfg_rounding":"Arrotonda per eccesso il prezzo per pezzo","cfg_round_none":"Non arrotondare","cfg_round_99":"Fino a ,99","cfg_discount_qty":"Sconto quantità da (pezzi sul piatto)","cfg_discount_pct":"Sconto quantità (%)","adj_disc":"sconto quantità −{v}%","adj_min":"prezzo minimo","adj_round":"arrotondato","refShort":"riferimento in €","refNote":"I prezzi segnati \"riferimento in €\" sono medie in euro, non convertite in {c}. Controllali nelle impostazioni qui sotto.","extrasT":"Extra","extrasP":"Magneti, viti, inserti, vernice… Scegli se ogni extra conta per ogni pezzo o una volta per il piatto. Salvati per questo lavoro.","extraName":"Articolo","extraCost":"Costo","extraAdd":"Aggiungi extra","c_extras":"Extra","quoteBtn":"Copia preventivo","quoteAll":"Copia preventivo di tutti i piatti","copied":"Preventivo copiato. Incollalo nel tuo messaggio.","q_title":"Preventivo","q_qty":"Quantità","q_unit":"Prezzo per pezzo","q_total":"Totale","q_time":"Tempo di stampa","q_material":"Materiale","q_taxinc":"IVA inclusa","byModelT":"Prezzo per modello","byModelP":"Il materiale è ripartito in base al filamento di ogni modello; tempo, macchina e preparazione in base alla sua quota di filamento. È una stima.","modelN":"Modello {n}","mdlCost":"costo/pezzo","mdlPrice":"prezzo/pezzo","cfg_updates":"Cerca nuove versioni all'apertura della scheda","upd_new":"Print Calculator {v} è disponibile. Aggiornalo nel gestore dei plugin di OrcaSlicer.","allBtn":"Tutti i piatti","allCost":"Costo totale","priceSetT":"Prezzo del piatto IVA inclusa","profitSet":"profitto del piatto","gramsSet":"filamento del piatto","partsN":"pezzi sul piatto","unitSet":"costo del piatto","costSetT":"Costo del piatto","py_set":"{p} per il piatto IVA inclusa ({n} pezzi)","jobN":"Lavoro {n}","jobNameP":"Nome (cliente, progetto…)","jobP":"Scegli il lavoro di ogni piatto. Ogni lavoro ha il suo totale, il suo margine e il suo preventivo.","jobMargin":"Margine del lavoro","jobTime":"tempo totale","jobProfit":"profitto","sumCost":"Costo","sumTax":"IVA","sumProfit":"Profitto","setupT":"Prima conferma i tuoi valori","setupP":"Il prezzo appare solo dopo aver confermato le impostazioni qui sotto: valuta, imposta, elettricità, il prezzo della tua stampante e il prezzo al kg di ogni filamento. I valori già inseriti sono di riferimento e potrebbero non essere i tuoi. Poi premi il pulsante di salvataggio.","repBtn":"Segnala un problema","repBusy":"Preparazione del G-code…","repDone":"G-code salvato in {p}. Trascina questo file nell'issue aperta nel browser e descrivi il problema.","repNoG":"Questo piatto non ha G-code. L'issue si è aperta senza file: descrivi il problema e, se puoi, esegui lo slicing e riprova.","repLink":"Se il browser non si è aperto, apri questo link:","noLabels":"Questo G-code non indica quanti pezzi ci sono sul piatto, quindi il prezzo è quello del piatto intero. Per contare i pezzi, attiva \"Label objects\" nelle impostazioni del processo di OrcaSlicer (Altro) ed esegui di nuovo lo slicing.","exPart":"per pezzo","exPlate":"per piatto"},"nl":{"imp":"Gesliced bestand importeren","impBig":"Dit bestand is te groot (maximaal 200 MB).","impFail":"Dit bestand kan niet worden gelezen.","sliceAllP":"Meerdere platen? Gebruik \"Alles slicen\" in OrcaSlicer en ze verschijnen hier allemaal. Je kunt ook een .gcode.3mf of .gcode importeren.","chT":"Verkoopkanalen","chP":"De prijs van elk kanaal dekt de kosten en de btw al: je winst is overal gelijk.","chDirect":"Directe verkoop","chN":"Kanaal","chFee":"Kosten","chPiece":"Per onderdeel","qChannel":"Prijs voor dit kanaal","chAdd":"Kanaal toevoegen","chName":"Naam (bijv. Etsy)","chNote":"Kosten in % van de prijs incl. btw plus vaste kosten per verkoop. De plugin verhoogt de prijs zodat je dezelfde winst houdt.","withShip":"met verzending ({s})","q_ship":"Verzending","q_grand":"Totaal met verzending","qSave":"Offerte opslaan","qSaved":"Offerte opgeslagen","qsT":"Opgeslagen offertes","qsCopy":"Kopiëren","qsPdf":"Afdrukken / PDF","qsDel":"Verwijderen","qsCsv":"CSV exporteren","qsFile":"Opgeslagen in {p}","qsFileOpen":"Opgeslagen in {p}. Open het om af te drukken of als PDF op te slaan.","spoolBtn":"Prijzen uit Spoolman lezen","spoolPick":"Spoolman…","spoolErr":"Spoolman kan niet worden gelezen: {e}","spoolOk":"{n} filamenten uit Spoolman. Kies er een naast elk filament en sla op.","spoolNone":"Spoolman heeft geen filamenten met prijs en gewicht.","cfg_shipping":"Verzending per bestelling","cfg_q_name":"Naam op offertes (bedrijf)","cfg_q_footer":"Notitie onderaan de offerte","cfg_spoolman":"Spoolman-adres (optioneel)","qh_item":"Artikel","qh_qty":"Aantal","qh_unit":"Stukprijs","qh_vat":"Incl. btw ({v}%)","qh_date":"Datum","buf_note":"Reservefactor: telt een marge op bij de uurkosten van de printer (slijtage, onderhoud en elektriciteit) voor het onvoorziene: storingen, onderdelen (hotend, nozzle, printplaat, riemen), stilstand en sparen voor de volgende printer. 1,3 = +30%. Verandert het materiaal niet. Gebruikelijk: 1,2 tot 1,3; lastige printer: 1,4 tot 1,5; 1 = geen reserve.","eff_note":"Materiaalverliesfactor: vermenigvuldigt alleen de filamentkosten, voor plastic dat de G-code niet telt (spoelresten, filamentwissels, testprints, bedorven filament). 1,10 = +10% op materiaal. Tel de prime-toren en purge niet mee (zitten al in de grammen) en ook geen mislukte prints (die hebben een eigen reserve). Gebruikelijk: 1,05 tot 1,15; 1 = geen verlies.","sub":"FDM-verkoopprijs van de gesliceerde plaat","readyT":"Klaar om te berekenen","readyP":"Slice een plaat in OrcaSlicer en hier verschijnt de verkoopprijs, met materiaal, stroom, slijtage van de printer en je marge.","s1":"Kies printer en filamenten","s2":"Slice de plaat","s3":"Bekijk de prijs","reading":"G-code lezen","failT":"Kon niet berekenen","warnT":"Let op","noPrinter":"Naamloze printer","updated":"Bijgewerkt om {t}","priceT":"Prijs per onderdeel incl. btw","mine":"jouw marge","sim":"marge van deze plaat","profit":"winst per onderdeel","margin":"marge","plate":"hele plaat ({n} onderdelen)","time":"printtijd van de plaat","grams":"filament per onderdeel","copy1":"onderdeel op de plaat","copyN":"gelijke onderdelen op de plaat","unit":"kosten per onderdeel","fils":"Filamenten","color1":"kleur","colorN":"kleuren","perKg":"/kg","tower":"Purge-toren en purges, al in de grammen meegeteld","costT":"Kosten per onderdeel","profitT":"Winst bij de gekozen marge","c_material":"Materiaal","c_energy":"Stroom","c_machine":"Slijtage printer","c_reserve":"Machinereserve","c_failure":"Reserve voor mislukte prints","c_labor":"Arbeid","c_pack":"Verpakking","src_custom":"Door jou ingesteld","src_orca":"OrcaSlicer-profiel","src_average":"Gemiddelde prijs","src_generic":"Standaardwaarden","cfgT":"Prijsinstellingen","cfgS":"algemeen, printer en filamenten","general":"Algemeen","printer":"Printer","p_price":"Prijs van de printer","p_life":"Levensduur","years":"jaar","p_uptime":"Jaarlijks gebruik","p_maint":"Jaarlijks onderhoud","p_power":"Gemiddeld vermogen","p_buffer":"Reservefactor","f_note":"De prijs per kg komt uit het veld \"Filamentkosten\" van het OrcaSlicer-profiel. Pas je hem hier aan, dan wordt hij voor dat filament bewaard.","scan":"Al gesliceerde platen zoeken","scanP":"Gesliced voordat de plugin aan stond? Kies in OrcaSlicer de laatst gesliceerde plaat en de plugin leest de G-code zonder opnieuw te slicen.","platesCost":"Jouw totale kosten","plateN":"Plaat {n}","platesTotal":"Totaal van {n} platen, incl. btw","notSliced":"{n} is nog niet gesliced","notSlicedP":"Slice deze plaat om de prijs te zien. De prijs van elke gesliceerde plaat blijft bewaard zolang het project open is.","customM":"Eigen marge","save":"Opslaan en herberekenen","reset":"Printerwaarden herstellen","h":"u","min":"min","cfg_intro":"Waarden voor de verkoopprijs van elke gesliceerde plaat. Je kunt ze ook, samen met het kostenprofiel van elke printer en de filamentprijzen, aanpassen in het tabblad Print Calculator.","cfg_ro":"Alleen lezen.","cfg_currency":"Valuta","cfg_search":"Valuta zoeken (naam of code)","cfg_vat":"Btw / belasting (%)","cfg_margin":"Marge (%)","cfg_failure":"Mislukkingspercentage (%)","cfg_electricity":"Stroom (per kWh)","cfg_rate":"Arbeid (per uur)","cfg_setup":"Voorbereiding per plaat (min)","cfg_finish":"Afwerking per onderdeel (min)","cfg_pack":"Verpakking per onderdeel","cfg_efficiency":"Materiaalverliesfactor (×)","cfg_language":"Taal","cfg_auto":"Zoals OrcaSlicer","py_perm":"OrcaSlicer stond het lezen van de G-code niet toe. Kies Ja als het wordt gevraagd en slice opnieuw.","py_missing":"De G-code bestaat niet meer. Slice opnieuw.","py_empty":"Deze G-code heeft geen printtijd of grammen die Print Calculator kan lezen.","py_fail":"Kon niet berekenen: {e}","py_unit":"{p} per onderdeel incl. btw","py_plate":" ({n} onderdelen, {t})","py_hint":"Print Calculator: open het tabblad Print Calculator om de prijs van deze plaat te zien.","py_none":"Geen gesliceerde platen gevonden in deze OrcaSlicer-sessie. Slice de plaat, of exporteer de G-code (Ctrl+G) als die al gesliced is.","cfg_minimum":"Minimumprijs per onderdeel, incl. btw","cfg_rounding":"Prijs per onderdeel naar boven afronden","cfg_round_none":"Niet afronden","cfg_round_99":"Tot ,99","cfg_discount_qty":"Staffelkorting vanaf (onderdelen op de plaat)","cfg_discount_pct":"Staffelkorting (%)","adj_disc":"staffelkorting −{v}%","adj_min":"minimumprijs","adj_round":"afgerond","refShort":"€-richtprijs","refNote":"Prijzen gemarkeerd als \"€-richtprijs\" zijn gemiddelden in euro, niet omgerekend naar {c}. Controleer ze hieronder in de instellingen.","extrasT":"Extra's","extrasP":"Magneten, schroeven, inserts, verf… Kies of elk extra per onderdeel telt of één keer per plaat. Onthouden voor deze opdracht.","extraName":"Artikel","extraCost":"Kosten","extraAdd":"Extra toevoegen","c_extras":"Extra's","quoteBtn":"Offerte kopiëren","quoteAll":"Offerte voor alle platen kopiëren","copied":"Offerte gekopieerd. Plak hem in je bericht.","q_title":"Offerte","q_qty":"Aantal","q_unit":"Prijs per onderdeel","q_total":"Totaal","q_time":"Printtijd","q_material":"Materiaal","q_taxinc":"incl. btw","byModelT":"Prijs per model","byModelP":"Materiaal wordt verdeeld naar het filament van elk model; tijd, machine en voorbereiding naar zijn aandeel in het filament. Schatting.","modelN":"Model {n}","mdlCost":"kosten/onderdeel","mdlPrice":"prijs/onderdeel","cfg_updates":"Zoek naar nieuwe versies bij het openen van het tabblad","upd_new":"Print Calculator {v} is beschikbaar. Werk hem bij in OrcaSlicer's pluginbeheer.","allBtn":"Alle platen","allCost":"Totale kosten","priceSetT":"Prijs van de plaat incl. btw","profitSet":"winst van de plaat","gramsSet":"filament van de plaat","partsN":"onderdelen op de plaat","unitSet":"kosten van de plaat","costSetT":"Kosten van de plaat","py_set":"{p} voor de plaat incl. btw ({n} onderdelen)","jobN":"Opdracht {n}","jobNameP":"Naam (klant, project…)","jobP":"Kies de opdracht van elke plaat. Elke opdracht heeft een eigen totaal, marge en offerte.","jobMargin":"Marge van de opdracht","jobTime":"totale tijd","jobProfit":"winst","sumCost":"Kosten","sumTax":"Btw","sumProfit":"Winst","setupT":"Bevestig eerst je waarden","setupP":"De prijs verschijnt pas nadat je de instellingen hieronder hebt bevestigd: valuta, belasting, elektriciteit, de prijs van je printer en de prijs per kg van elk filament. De ingevulde waarden zijn referentiewaarden en hoeven niet de jouwe te zijn. Druk daarna op opslaan.","repBtn":"Probleem melden","repBusy":"G-code voorbereiden…","repDone":"G-code opgeslagen in {p}. Sleep dit bestand naar de issue die in je browser is geopend en beschrijf het probleem.","repNoG":"Deze plaat heeft geen G-code. De issue is zonder bestand geopend: beschrijf het probleem en slice zo mogelijk opnieuw.","repLink":"Als de browser niet is geopend, open deze link:","noLabels":"Deze G-code vermeldt niet hoeveel onderdelen er op de plaat staan, dus de prijs geldt voor de hele plaat. Zet \"Label objects\" aan in de procesinstellingen van OrcaSlicer (Overige) en slice opnieuw om de onderdelen te tellen.","exPart":"per onderdeel","exPlate":"per plaat"},"pt_BR":{"imp":"Importar arquivo fatiado","impBig":"Este arquivo é grande demais (máximo 200 MB).","impFail":"Não foi possível ler este arquivo.","sliceAllP":"Várias mesas? Use \"Fatiar tudo\" no OrcaSlicer e todas aparecem aqui. Você também pode importar um .gcode.3mf ou .gcode.","chT":"Canais de venda","chP":"O preço de cada canal já cobre a taxa e o imposto: você fica com o mesmo lucro em todos.","chDirect":"Venda direta","chN":"Canal","chFee":"Taxa","chPiece":"Por peça","qChannel":"Preço para este canal","chAdd":"Adicionar canal","chName":"Nome (ex.: Shopee)","chNote":"Taxa em % sobre o preço com imposto e taxa fixa por venda. O plugin aumenta o preço para você manter o mesmo lucro.","withShip":"com frete ({s})","q_ship":"Frete","q_grand":"Total com frete","qSave":"Salvar orçamento","qSaved":"Orçamento salvo","qsT":"Orçamentos salvos","qsCopy":"Copiar","qsPdf":"Imprimir / PDF","qsDel":"Excluir","qsCsv":"Exportar CSV","qsFile":"Salvo em {p}","qsFileOpen":"Salvo em {p}. Abra para imprimir ou salvar em PDF.","spoolBtn":"Ler preços do Spoolman","spoolPick":"Spoolman…","spoolErr":"Não foi possível ler o Spoolman: {e}","spoolOk":"{n} filamentos do Spoolman. Escolha um ao lado de cada filamento e salve.","spoolNone":"O Spoolman não tem filamentos com preço e peso.","cfg_shipping":"Frete por pedido","cfg_q_name":"Nome no orçamento (empresa)","cfg_q_footer":"Observação no fim do orçamento","cfg_spoolman":"Endereço do Spoolman (opcional)","qh_item":"Item","qh_qty":"Qtd.","qh_unit":"Preço unitário","qh_vat":"Impostos incluídos ({v}%)","qh_date":"Data","buf_note":"Fator de reserva: soma uma margem aos custos por hora da impressora (desgaste, manutenção e eletricidade) para o que não está previsto: defeitos, peças (hotend, bico, mesa, correias), tempo parado e poupar para a próxima impressora. 1,3 = +30%. Não mexe no material. Normal: 1,2 a 1,3; impressora com muitos problemas: 1,4 a 1,5; 1 = sem reserva.","eff_note":"Fator de perda de material: multiplica só o custo do filamento, para o plástico que o G-code não conta (pontas de carretel, trocas de filamento, testes, filamento estragado). 1,10 = +10% no material. Não conte a torre e a purga (já estão nos gramas) nem as falhas (têm reserva própria). Normal: 1,05 a 1,15; 1 = sem perdas.","sub":"Preço de venda FDM da placa fatiada","readyT":"Pronto para calcular","readyP":"Fatie uma placa no OrcaSlicer e o preço de venda aparece aqui, com material, eletricidade, desgaste da impressora e sua margem.","s1":"Escolha impressora e filamentos","s2":"Fatie a placa","s3":"Veja o preço","reading":"Lendo o G-code","failT":"Não foi possível calcular","warnT":"Atenção","noPrinter":"Impressora sem nome","updated":"Atualizado às {t}","priceT":"Preço por peça com impostos","mine":"sua margem","sim":"margem desta placa","profit":"lucro por peça","margin":"margem","plate":"placa inteira ({n} peças)","time":"tempo da placa","grams":"filamento por peça","copy1":"peça na placa","copyN":"peças iguais na placa","unit":"custo por peça","fils":"Filamentos","color1":"cor","colorN":"cores","perKg":"/kg","tower":"Torre de purga e purgas, já incluídas nos gramas","costT":"Custo por peça","profitT":"Lucro com a margem escolhida","c_material":"Material","c_energy":"Eletricidade","c_machine":"Desgaste da impressora","c_reserve":"Reserva da máquina","c_failure":"Reserva para falhas","c_labor":"Mão de obra","c_pack":"Embalagem","src_custom":"Definido por você","src_orca":"Perfil do OrcaSlicer","src_average":"Preço médio","src_generic":"Valores genéricos","cfgT":"Configurações de preço","cfgS":"geral, impressora e filamentos","general":"Geral","printer":"Impressora","p_price":"Preço da impressora","p_life":"Vida útil","years":"anos","p_uptime":"Uso anual","p_maint":"Manutenção anual","p_power":"Consumo médio","p_buffer":"Fator de reserva","f_note":"O preço por kg vem do campo \"Custo do filamento\" do perfil do OrcaSlicer. Se você mudar aqui, ele fica salvo para esse filamento.","scan":"Procurar placas já fatiadas","scanP":"Fatiou antes de ativar o plugin? Selecione no OrcaSlicer a última placa fatiada e o plugin lê o G-code dela, sem fatiar de novo.","platesCost":"Seu custo total","plateN":"Placa {n}","platesTotal":"Total de {n} placas, com impostos","notSliced":"A {n} ainda não foi fatiada","notSlicedP":"Fatie esta placa para ver o preço. O preço de cada placa fatiada fica salvo enquanto o projeto estiver aberto.","customM":"Margem personalizada","save":"Salvar e recalcular","reset":"Restaurar valores da impressora","h":"h","min":"min","cfg_intro":"Valores usados no preço de venda de cada placa fatiada. Você também pode alterá-los, junto com o perfil de cada impressora e o preço dos filamentos, na aba Print Calculator.","cfg_ro":"Somente leitura.","cfg_currency":"Moeda","cfg_search":"Procurar moeda (nome ou código)","cfg_vat":"Impostos (%)","cfg_margin":"Margem (%)","cfg_failure":"Taxa de falha (%)","cfg_electricity":"Eletricidade (por kWh)","cfg_rate":"Mão de obra (por hora)","cfg_setup":"Preparação por placa (min)","cfg_finish":"Acabamento por peça (min)","cfg_pack":"Embalagem por peça","cfg_efficiency":"Fator de perda de material (×)","cfg_language":"Idioma","cfg_auto":"Igual ao OrcaSlicer","py_perm":"O OrcaSlicer não permitiu ler o G-code. Quando ele perguntar, escolha Sim e fatie de novo.","py_missing":"O G-code não existe mais. Fatie de novo.","py_empty":"Este G-code não tem tempo nem gramas que o Print Calculator consiga ler.","py_fail":"Não foi possível calcular: {e}","py_unit":"{p} por peça com impostos","py_plate":" ({n} peças, {t})","py_hint":"Print Calculator: abra a aba Print Calculator para ver o preço desta placa.","py_none":"Nenhuma placa fatiada nesta sessão do OrcaSlicer. Fatie a placa, ou exporte o G-code (Ctrl+G) se ela já estiver fatiada.","cfg_minimum":"Preço mínimo por peça, com impostos","cfg_rounding":"Arredondar o preço por peça para cima","cfg_round_none":"Não arredondar","cfg_round_99":"Até ,99","cfg_discount_qty":"Desconto por quantidade a partir de (peças na placa)","cfg_discount_pct":"Desconto por quantidade (%)","adj_disc":"desconto por quantidade −{v}%","adj_min":"preço mínimo","adj_round":"arredondado","refShort":"referência em €","refNote":"Os preços marcados \"referência em €\" são médias em euros, não convertidas para {c}. Confira nas configurações abaixo.","extrasT":"Extras","extrasP":"Ímãs, parafusos, insertos, tinta… Escolha se cada extra conta em cada peça ou uma vez na mesa. Ficam salvos para este trabalho.","extraName":"Item","extraCost":"Custo","extraAdd":"Adicionar extra","c_extras":"Extras","quoteBtn":"Copiar orçamento","quoteAll":"Copiar orçamento de todas as placas","copied":"Orçamento copiado. Cole na sua mensagem.","q_title":"Orçamento","q_qty":"Quantidade","q_unit":"Preço por peça","q_total":"Total","q_time":"Tempo de impressão","q_material":"Material","q_taxinc":"impostos incluídos","byModelT":"Preço por modelo","byModelP":"O material é dividido pelo filamento de cada modelo; tempo, máquina e preparação pela parte do filamento que cada um usa. É uma estimativa.","modelN":"Modelo {n}","mdlCost":"custo/peça","mdlPrice":"preço/peça","cfg_updates":"Procurar versões novas ao abrir a aba","upd_new":"Há uma versão nova do Print Calculator ({v}). Atualize no gerenciador de plugins do OrcaSlicer.","allBtn":"Todas as placas","allCost":"Custo total","priceSetT":"Preço da placa com impostos","profitSet":"lucro da placa","gramsSet":"filamento da placa","partsN":"peças na placa","unitSet":"custo da placa","costSetT":"Custo da placa","py_set":"{p} pela placa com impostos ({n} peças)","jobN":"Trabalho {n}","jobNameP":"Nome (cliente, projeto…)","jobP":"Escolha o trabalho de cada placa. Cada trabalho tem o seu total, a sua margem e o seu orçamento.","jobMargin":"Margem do trabalho","jobTime":"tempo total","jobProfit":"lucro","sumCost":"Custo","sumTax":"Impostos","sumProfit":"Lucro","setupT":"Primeiro, confirme os seus valores","setupP":"O preço só aparece depois de confirmar as definições abaixo: moeda, impostos, eletricidade, o preço da sua impressora e o preço por kg de cada filamento. Os valores já preenchidos são de referência e podem não ser os seus. Depois clique no botão de salvar.","repBtn":"Relatar um problema","repBusy":"Preparando o G-code…","repDone":"G-code salvo em {p}. Arraste este arquivo para o issue que abriu no navegador e descreva o problema.","repNoG":"Esta placa não tem G-code. O issue abriu sem arquivo: descreva o problema e, se puder, fatie e tente de novo.","repLink":"Se o navegador não abriu, abra este link:","noLabels":"Este G-code não informa quantas peças há na placa, por isso o preço é o da placa inteira. Para contar as peças, ative \"Label objects\" (Rotular objetos) no processo do OrcaSlicer, em Outros, e fatie de novo.","exPart":"por peça","exPlate":"por mesa"},"pl":{"imp":"Importuj pocięty plik","impBig":"Ten plik jest za duży (maksymalnie 200 MB).","impFail":"Nie udało się odczytać tego pliku.","sliceAllP":"Kilka płyt? Użyj „Pokrój wszystko” w OrcaSlicer, a wszystkie pojawią się tutaj. Możesz też zaimportować .gcode.3mf lub .gcode.","chT":"Kanały sprzedaży","chP":"Cena w każdym kanale pokrywa już prowizję i podatek: twój zysk jest wszędzie taki sam.","chDirect":"Sprzedaż bezpośrednia","chN":"Kanał","chFee":"Prowizja","chPiece":"Za część","qChannel":"Cena dla tego kanału","chAdd":"Dodaj kanał","chName":"Nazwa (np. Allegro)","chNote":"Prowizja w % od ceny z podatkiem plus stała opłata za sprzedaż. Wtyczka podnosi cenę, aby zysk pozostał taki sam.","withShip":"z wysyłką ({s})","q_ship":"Wysyłka","q_grand":"Razem z wysyłką","qSave":"Zapisz wycenę","qSaved":"Wycena zapisana","qsT":"Zapisane wyceny","qsCopy":"Kopiuj","qsPdf":"Drukuj / PDF","qsDel":"Usuń","qsCsv":"Eksportuj CSV","qsFile":"Zapisano w {p}","qsFileOpen":"Zapisano w {p}. Otwórz, aby wydrukować lub zapisać jako PDF.","spoolBtn":"Wczytaj ceny ze Spoolmana","spoolPick":"Spoolman…","spoolErr":"Nie udało się odczytać Spoolmana: {e}","spoolOk":"{n} filamentów ze Spoolmana. Wybierz jeden przy każdym filamencie i zapisz.","spoolNone":"Spoolman nie ma filamentów z ceną i wagą.","cfg_shipping":"Wysyłka za zamówienie","cfg_q_name":"Nazwa na wycenie (firma)","cfg_q_footer":"Uwaga na końcu wyceny","cfg_spoolman":"Adres Spoolmana (opcjonalnie)","qh_item":"Pozycja","qh_qty":"Ilość","qh_unit":"Cena jedn.","qh_vat":"Zawiera podatek ({v}%)","qh_date":"Data","buf_note":"Współczynnik rezerwy: dodaje margines do godzinowych kosztów drukarki (zużycie, konserwacja i prąd) na nieprzewidziane: awarie, części (hotend, dysza, płyta, paski), przestoje i oszczędzanie na kolejną drukarkę. 1,3 = +30%. Nie zmienia materiału. Typowo: 1,2 do 1,3; problematyczna drukarka: 1,4 do 1,5; 1 = bez rezerwy.","eff_note":"Współczynnik strat materiału: mnoży tylko koszt filamentu, za plastik, którego G-code nie liczy (końcówki szpul, zmiany filamentu, wydruki testowe, zepsuty filament). 1,10 = +10% do materiału. Nie licz wieży i przedmuchu (są już w gramach) ani nieudanych wydruków (mają własną rezerwę). Typowo: 1,05 do 1,15; 1 = bez strat.","sub":"Cena sprzedaży FDM pociętej płyty","readyT":"Gotowe do wyceny","readyP":"Potnij płytę w OrcaSlicer, a tutaj pojawi się jej cena sprzedaży z materiałem, prądem, zużyciem drukarki i twoją marżą.","s1":"Wybierz drukarkę i filamenty","s2":"Potnij płytę","s3":"Zobacz cenę","reading":"Odczyt G-code","failT":"Nie udało się obliczyć","warnT":"Uwaga","noPrinter":"Drukarka bez nazwy","updated":"Zaktualizowano o {t}","priceT":"Cena za sztukę z podatkiem","mine":"twoja marża","sim":"marża tej płyty","profit":"zysk na sztuce","margin":"marża","plate":"cała płyta ({n} szt.)","time":"czas płyty","grams":"filament na sztukę","copy1":"sztuka na płycie","copyN":"jednakowe sztuki na płycie","unit":"koszt sztuki","fils":"Filamenty","color1":"kolor","colorN":"kolory","perKg":"/kg","tower":"Wieża czyszcząca i przedmuchy, już w gramach","costT":"Koszt sztuki","profitT":"Zysk przy wybranej marży","c_material":"Materiał","c_energy":"Prąd","c_machine":"Zużycie drukarki","c_reserve":"Rezerwa maszyny","c_failure":"Rezerwa na błędy","c_labor":"Robocizna","c_pack":"Opakowanie","src_custom":"Ustawione przez ciebie","src_orca":"Profil OrcaSlicer","src_average":"Średnia cena","src_generic":"Wartości domyślne","cfgT":"Ustawienia ceny","cfgS":"ogólne, drukarka i filamenty","general":"Ogólne","printer":"Drukarka","p_price":"Cena drukarki","p_life":"Żywotność","years":"lat","p_uptime":"Roczne użycie","p_maint":"Roczna konserwacja","p_power":"Średni pobór mocy","p_buffer":"Współczynnik rezerwy","f_note":"Cena za kg pochodzi z pola „Koszt filamentu” profilu OrcaSlicer. Jeśli zmienisz ją tutaj, zostanie zapisana dla tego filamentu.","scan":"Znajdź już pocięte płyty","scanP":"Cięto przed włączeniem wtyczki? Wybierz w OrcaSlicer ostatnio pociętą płytę, a wtyczka odczyta jej G-code bez ponownego cięcia.","platesCost":"Twój łączny koszt","plateN":"Płyta {n}","platesTotal":"Suma {n} płyt, z podatkiem","notSliced":"{n} nie jest jeszcze pocięta","notSlicedP":"Potnij tę płytę, aby zobaczyć cenę. Cena każdej pociętej płyty jest zachowana, dopóki projekt jest otwarty.","customM":"Własna marża","save":"Zapisz i przelicz","reset":"Przywróć wartości drukarki","h":"h","min":"min","cfg_intro":"Wartości używane do ceny sprzedaży każdej pociętej płyty. Możesz je też zmienić, razem z profilem kosztów każdej drukarki i cenami filamentów, w karcie Print Calculator.","cfg_ro":"Tylko do odczytu.","cfg_currency":"Waluta","cfg_search":"Szukaj waluty (nazwa lub kod)","cfg_vat":"VAT / podatek (%)","cfg_margin":"Marża (%)","cfg_failure":"Odsetek błędów (%)","cfg_electricity":"Prąd (za kWh)","cfg_rate":"Robocizna (za godzinę)","cfg_setup":"Przygotowanie płyty (min)","cfg_finish":"Wykończenie sztuki (min)","cfg_pack":"Opakowanie na sztukę","cfg_efficiency":"Współczynnik strat materiału (×)","cfg_language":"Język","cfg_auto":"Jak w OrcaSlicer","py_perm":"OrcaSlicer nie pozwolił odczytać G-code. Gdy zapyta, wybierz Tak i potnij ponownie.","py_missing":"G-code już nie istnieje. Potnij ponownie.","py_empty":"Ten G-code nie ma czasu ani gramów, które Print Calculator może odczytać.","py_fail":"Nie udało się obliczyć: {e}","py_unit":"{p} za sztukę z podatkiem","py_plate":" ({n} szt., {t})","py_hint":"Print Calculator: otwórz kartę Print Calculator, aby zobaczyć cenę tej płyty.","py_none":"Brak pociętych płyt w tej sesji OrcaSlicer. Potnij płytę lub wyeksportuj G-code (Ctrl+G), jeśli jest już pocięta.","cfg_minimum":"Minimalna cena za sztukę z podatkiem","cfg_rounding":"Zaokrąglaj cenę za sztukę w górę","cfg_round_none":"Nie zaokrąglaj","cfg_round_99":"Do ,99","cfg_discount_qty":"Rabat ilościowy od (sztuk na płycie)","cfg_discount_pct":"Rabat ilościowy (%)","adj_disc":"rabat ilościowy −{v}%","adj_min":"cena minimalna","adj_round":"zaokrąglono","refShort":"orientacyjnie w €","refNote":"Ceny oznaczone \"orientacyjnie w €\" to średnie w euro, nieprzeliczone na {c}. Sprawdź je w ustawieniach poniżej.","extrasT":"Dodatki","extrasP":"Magnesy, śruby, wkładki, farba… Wybierz, czy każdy dodatek liczy się dla każdej części, czy raz dla płyty. Zapamiętane dla tego zlecenia.","extraName":"Pozycja","extraCost":"Koszt","extraAdd":"Dodaj dodatek","c_extras":"Dodatki","quoteBtn":"Kopiuj wycenę","quoteAll":"Kopiuj wycenę wszystkich płyt","copied":"Wycena skopiowana. Wklej ją do wiadomości.","q_title":"Wycena","q_qty":"Ilość","q_unit":"Cena za sztukę","q_total":"Razem","q_time":"Czas druku","q_material":"Materiał","q_taxinc":"z podatkiem","byModelT":"Cena za model","byModelP":"Materiał dzielony jest według filamentu każdego modelu; czas, maszyna i przygotowanie według jego udziału w filamencie. To szacunek.","modelN":"Model {n}","mdlCost":"koszt/szt.","mdlPrice":"cena/szt.","cfg_updates":"Szukaj nowych wersji przy otwieraniu karty","upd_new":"Dostępny jest Print Calculator {v}. Zaktualizuj go w menedżerze wtyczek OrcaSlicer.","allBtn":"Wszystkie płyty","allCost":"Łączny koszt","priceSetT":"Cena płyty z podatkiem","profitSet":"zysk z płyty","gramsSet":"filament na płytę","partsN":"sztuk na płycie","unitSet":"koszt płyty","costSetT":"Koszt płyty","py_set":"{p} za płytę z podatkiem ({n} szt.)","jobN":"Zlecenie {n}","jobNameP":"Nazwa (klient, projekt…)","jobP":"Wybierz zlecenie dla każdej płyty. Każde zlecenie ma własną sumę, marżę i wycenę.","jobMargin":"Marża zlecenia","jobTime":"łączny czas","jobProfit":"zysk","sumCost":"Koszt","sumTax":"VAT","sumProfit":"Zysk","setupT":"Najpierw potwierdź swoje wartości","setupP":"Cena pojawi się dopiero po potwierdzeniu ustawień poniżej: waluta, podatek, prąd, cena twojej drukarki i cena za kg każdego filamentu. Wpisane wartości są orientacyjne i mogą nie być twoje. Następnie naciśnij przycisk zapisu.","repBtn":"Zgłoś problem","repBusy":"Przygotowywanie G-code…","repDone":"G-code zapisano w {p}. Przeciągnij ten plik do zgłoszenia otwartego w przeglądarce i opisz problem.","repNoG":"Ta płyta nie ma G-code. Zgłoszenie otwarto bez pliku: opisz problem i, jeśli możesz, pokrój ponownie.","repLink":"Jeśli przeglądarka się nie otworzyła, otwórz ten link:","noLabels":"Ten G-code nie podaje, ile części jest na płycie, więc cena dotyczy całej płyty. Aby liczyć części, włącz \"Label objects\" w ustawieniach procesu OrcaSlicer (Inne) i pokrój ponownie.","exPart":"za część","exPlate":"za płytę"},"cs":{"imp":"Importovat slicovaný soubor","impBig":"Soubor je příliš velký (maximálně 200 MB).","impFail":"Tento soubor nelze přečíst.","sliceAllP":"Více podložek? Použijte v OrcaSliceru „Slicovat vše“ a všechny se zobrazí zde. Můžete také importovat .gcode.3mf nebo .gcode.","chT":"Prodejní kanály","chP":"Cena v každém kanálu už pokrývá poplatek i daň: váš zisk je všude stejný.","chDirect":"Přímý prodej","chN":"Kanál","chFee":"Poplatek","chPiece":"Za díl","qChannel":"Cena pro tento kanál","chAdd":"Přidat kanál","chName":"Název (např. Etsy)","chNote":"Poplatek v % z ceny s daní a pevný poplatek za prodej. Plugin zvýší cenu, abyste měli stejný zisk.","withShip":"s dopravou ({s})","q_ship":"Doprava","q_grand":"Celkem s dopravou","qSave":"Uložit nabídku","qSaved":"Nabídka uložena","qsT":"Uložené nabídky","qsCopy":"Kopírovat","qsPdf":"Tisk / PDF","qsDel":"Smazat","qsCsv":"Exportovat CSV","qsFile":"Uloženo do {p}","qsFileOpen":"Uloženo do {p}. Otevřete jej pro tisk nebo uložení do PDF.","spoolBtn":"Načíst ceny ze Spoolmanu","spoolPick":"Spoolman…","spoolErr":"Spoolman nelze načíst: {e}","spoolOk":"{n} filamentů ze Spoolmanu. Vyberte jeden u každého filamentu a uložte.","spoolNone":"Spoolman nemá filamenty s cenou a hmotností.","cfg_shipping":"Doprava za objednávku","cfg_q_name":"Název v nabídce (firma)","cfg_q_footer":"Poznámka na konci nabídky","cfg_spoolman":"Adresa Spoolmanu (volitelné)","qh_item":"Položka","qh_qty":"Množ.","qh_unit":"Jedn. cena","qh_vat":"Včetně daně ({v} %)","qh_date":"Datum","buf_note":"Faktor rezervy: přidává rezervu k hodinovým nákladům tiskárny (opotřebení, údržba a elektřina) na nečekané: poruchy, díly (hotend, tryska, podložka, řemeny), prostoje a spoření na další tiskárnu. 1,3 = +30 %. Nemění materiál. Obvykle: 1,2 až 1,3; problémová tiskárna: 1,4 až 1,5; 1 = bez rezervy.","eff_note":"Faktor ztrát materiálu: násobí jen cenu filamentu, za plast, který G-code nepočítá (zbytky cívek, výměny filamentu, testovací tisky, zkažený filament). 1,10 = +10 % na materiál. Nepočítejte čisticí věž a purge (už jsou v gramech) ani nepovedené tisky (mají vlastní rezervu). Obvykle: 1,05 až 1,15; 1 = bez ztrát.","sub":"Prodejní cena FDM naslicované podložky","readyT":"Připraveno k výpočtu","readyP":"Naslicujte podložku v OrcaSliceru a zde se zobrazí její prodejní cena s materiálem, elektřinou, opotřebením tiskárny a vaší marží.","s1":"Vyberte tiskárnu a filamenty","s2":"Naslicujte podložku","s3":"Podívejte se na cenu","reading":"Čtení G-kódu","failT":"Nelze vypočítat","warnT":"Pozor","noPrinter":"Tiskárna bez názvu","updated":"Aktualizováno v {t}","priceT":"Cena za kus s daní","mine":"vaše marže","sim":"marže této podložky","profit":"zisk na kus","margin":"marže","plate":"celá podložka ({n} ks)","time":"čas podložky","grams":"filament na kus","copy1":"kus na podložce","copyN":"stejné kusy na podložce","unit":"náklad na kus","fils":"Filamenty","color1":"barva","colorN":"barvy","perKg":"/kg","tower":"Čisticí věž a čištění, již v gramech","costT":"Náklad na kus","profitT":"Zisk při zvolené marži","c_material":"Materiál","c_energy":"Elektřina","c_machine":"Opotřebení tiskárny","c_reserve":"Rezerva stroje","c_failure":"Rezerva na chyby","c_labor":"Práce","c_pack":"Balení","src_custom":"Nastaveno vámi","src_orca":"Profil OrcaSliceru","src_average":"Průměrná cena","src_generic":"Obecné hodnoty","cfgT":"Nastavení ceny","cfgS":"obecné, tiskárna a filamenty","general":"Obecné","printer":"Tiskárna","p_price":"Cena tiskárny","p_life":"Životnost","years":"let","p_uptime":"Roční využití","p_maint":"Roční údržba","p_power":"Průměrný příkon","p_buffer":"Faktor rezervy","f_note":"Cena za kg pochází z pole „Cena filamentu“ v profilu OrcaSliceru. Pokud ji změníte zde, uloží se pro tento filament.","scan":"Najít již naslicované podložky","scanP":"Slicovali jste před zapnutím pluginu? Vyberte v OrcaSliceru naposledy naslicovanou podložku a plugin načte její G-kód bez nového slicování.","platesCost":"Vaše celkové náklady","plateN":"Podložka {n}","platesTotal":"Celkem {n} podložek, s daní","notSliced":"{n} ještě není naslicovaná","notSlicedP":"Naslicujte tuto podložku a uvidíte její cenu. Cena každé naslicované podložky zůstává, dokud je projekt otevřený.","customM":"Vlastní marže","save":"Uložit a přepočítat","reset":"Obnovit hodnoty tiskárny","h":"h","min":"min","cfg_intro":"Hodnoty pro prodejní cenu každé naslicované podložky. Můžete je změnit i na kartě Print Calculator, spolu s profilem nákladů každé tiskárny a cenami filamentů.","cfg_ro":"Jen pro čtení.","cfg_currency":"Měna","cfg_search":"Hledat měnu (název nebo kód)","cfg_vat":"DPH / daň (%)","cfg_margin":"Marže (%)","cfg_failure":"Míra chyb (%)","cfg_electricity":"Elektřina (za kWh)","cfg_rate":"Práce (za hodinu)","cfg_setup":"Příprava na podložku (min)","cfg_finish":"Dokončení na kus (min)","cfg_pack":"Balení na kus","cfg_efficiency":"Faktor ztrát materiálu (×)","cfg_language":"Jazyk","cfg_auto":"Jako OrcaSlicer","py_perm":"OrcaSlicer nepovolil čtení G-kódu. Až se zeptá, zvolte Ano a naslicujte znovu.","py_missing":"G-kód už neexistuje. Naslicujte znovu.","py_empty":"Tento G-kód nemá čas ani gramy, které Print Calculator umí přečíst.","py_fail":"Nelze vypočítat: {e}","py_unit":"{p} za kus s daní","py_plate":" ({n} ks, {t})","py_hint":"Print Calculator: otevřete kartu Print Calculator a uvidíte cenu této podložky.","py_none":"V této relaci OrcaSliceru nejsou žádné naslicované podložky. Naslicujte podložku, nebo exportujte G-kód (Ctrl+G), pokud už je naslicovaná.","cfg_minimum":"Minimální cena za kus s daní","cfg_rounding":"Zaokrouhlit cenu za kus nahoru","cfg_round_none":"Nezaokrouhlovat","cfg_round_99":"Na ,99","cfg_discount_qty":"Množstevní sleva od (kusů na podložce)","cfg_discount_pct":"Množstevní sleva (%)","adj_disc":"množstevní sleva −{v} %","adj_min":"minimální cena","adj_round":"zaokrouhleno","refShort":"orientačně v €","refNote":"Ceny označené „orientačně v €“ jsou průměry v eurech, nepřepočtené na {c}. Zkontrolujte je v nastavení níže.","extrasT":"Doplňky","extrasP":"Magnety, šrouby, insertní matice, barva… Zvolte, zda se každý doplněk počítá u každého dílu, nebo jednou za podložku. Uloží se pro tuto zakázku.","extraName":"Položka","extraCost":"Cena","extraAdd":"Přidat doplněk","c_extras":"Doplňky","quoteBtn":"Kopírovat nabídku","quoteAll":"Kopírovat nabídku všech podložek","copied":"Nabídka zkopírována. Vložte ji do zprávy.","q_title":"Nabídka","q_qty":"Množství","q_unit":"Cena za kus","q_total":"Celkem","q_time":"Doba tisku","q_material":"Materiál","q_taxinc":"včetně daně","byModelT":"Cena za model","byModelP":"Materiál se dělí podle filamentu každého modelu; čas, stroj a příprava podle jeho podílu na filamentu. Jde o odhad.","modelN":"Model {n}","mdlCost":"náklad/ks","mdlPrice":"cena/ks","cfg_updates":"Hledat nové verze při otevření karty","upd_new":"Je k dispozici Print Calculator {v}. Aktualizujte ho ve správci pluginů OrcaSliceru.","allBtn":"Všechny podložky","allCost":"Celkové náklady","priceSetT":"Cena podložky s daní","profitSet":"zisk z podložky","gramsSet":"filament na podložku","partsN":"kusů na podložce","unitSet":"náklad na podložku","costSetT":"Náklad na podložku","py_set":"{p} za podložku s daní ({n} ks)","jobN":"Zakázka {n}","jobNameP":"Název (zákazník, projekt…)","jobP":"Vyberte zakázku pro každou podložku. Každá zakázka má vlastní součet, marži a nabídku.","jobMargin":"Marže zakázky","jobTime":"celkový čas","jobProfit":"zisk","sumCost":"Náklady","sumTax":"DPH","sumProfit":"Zisk","setupT":"Nejprve potvrďte své hodnoty","setupP":"Cena se zobrazí až po potvrzení nastavení níže: měna, daň, elektřina, cena vaší tiskárny a cena za kg každého filamentu. Předvyplněné hodnoty jsou orientační a nemusí být vaše. Poté stiskněte tlačítko uložení.","repBtn":"Nahlásit problém","repBusy":"Připravuji G-code…","repDone":"G-code uložen do {p}. Přetáhněte tento soubor do hlášení otevřeného v prohlížeči a popište problém.","repNoG":"Tato podložka nemá G-code. Hlášení se otevřelo bez souboru: popište problém a pokud můžete, slicujte znovu.","repLink":"Pokud se prohlížeč neotevřel, otevřete tento odkaz:","noLabels":"Tento G-code neuvádí, kolik dílů je na podložce, takže cena platí pro celou podložku. Pro počítání dílů zapněte \"Label objects\" v nastavení procesu OrcaSliceru (Ostatní) a slicujte znovu.","exPart":"za díl","exPlate":"za podložku"},"hu":{"imp":"Szeletelt fájl importálása","impBig":"A fájl túl nagy (legfeljebb 200 MB).","impFail":"Ez a fájl nem olvasható.","sliceAllP":"Több tálca? Használd az OrcaSlicer „Összes szeletelése” gombját, és mind megjelenik itt. .gcode.3mf vagy .gcode fájlt is importálhatsz.","chT":"Értékesítési csatornák","chP":"Minden csatorna ára már fedezi a díjat és az adót: a hasznod mindenhol ugyanannyi.","chDirect":"Közvetlen eladás","chN":"Csatorna","chFee":"Díj","chPiece":"Darabonként","qChannel":"Ár ehhez a csatornához","chAdd":"Csatorna hozzáadása","chName":"Név (pl. Etsy)","chNote":"Díj az adóval növelt ár %-ában, plusz fix díj eladásonként. A bővítmény megemeli az árat, hogy a hasznod ugyanannyi maradjon.","withShip":"szállítással ({s})","q_ship":"Szállítás","q_grand":"Összesen szállítással","qSave":"Árajánlat mentése","qSaved":"Árajánlat mentve","qsT":"Mentett árajánlatok","qsCopy":"Másolás","qsPdf":"Nyomtatás / PDF","qsDel":"Törlés","qsCsv":"Exportálás CSV-be","qsFile":"Mentve ide: {p}","qsFileOpen":"Mentve ide: {p}. Nyisd meg a nyomtatáshoz vagy PDF-be mentéshez.","spoolBtn":"Árak beolvasása a Spoolmanből","spoolPick":"Spoolman…","spoolErr":"A Spoolman nem olvasható: {e}","spoolOk":"{n} filament a Spoolmanből. Válassz egyet minden filament mellett, és ments.","spoolNone":"A Spoolmanben nincs filament árral és tömeggel.","cfg_shipping":"Szállítás rendelésenként","cfg_q_name":"Név az árajánlaton (cég)","cfg_q_footer":"Megjegyzés az árajánlat végén","cfg_spoolman":"Spoolman címe (nem kötelező)","qh_item":"Tétel","qh_qty":"Menny.","qh_unit":"Egységár","qh_vat":"Az adót tartalmazza ({v}%)","qh_date":"Dátum","buf_note":"Tartalékszorzó: tartalékot ad a nyomtató óradíjas költségeihez (kopás, karbantartás és áram) a váratlanra: meghibásodások, alkatrészek (hotend, fúvóka, tálca, szíjak), állásidő és félretétel a következő nyomtatóra. 1,3 = +30%. Az anyagot nem változtatja. Szokásos: 1,2–1,3; problémás nyomtató: 1,4–1,5; 1 = nincs tartalék.","eff_note":"Anyagveszteség-szorzó: csak a filament költségét szorozza, a G-kód által nem számolt műanyagért (orsómaradékok, filamentcserék, tesztnyomtatások, tönkrement filament). 1,10 = +10% az anyagra. A tornyot és az öblítést ne számold bele (már benne vannak a grammokban), a hibás nyomtatásokat se (saját tartalékuk van). Szokásos: 1,05–1,15; 1 = nincs veszteség.","sub":"A szeletelt tálca FDM eladási ára","readyT":"Számolásra kész","readyP":"Szeletelj egy tálcát az OrcaSlicerben, és itt megjelenik az eladási ára anyaggal, árammal, a nyomtató kopásával és a haszonkulcsoddal.","s1":"Válassz nyomtatót és filamenteket","s2":"Szeleteld a tálcát","s3":"Nézd meg az árat","reading":"G-kód olvasása","failT":"Nem sikerült számolni","warnT":"Figyelem","noPrinter":"Névtelen nyomtató","updated":"Frissítve: {t}","priceT":"Darabár adóval","mine":"a te haszonkulcsod","sim":"a tálca haszonkulcsa","profit":"haszon darabonként","margin":"haszonkulcs","plate":"teljes tálca ({n} db)","time":"tálca ideje","grams":"filament darabonként","copy1":"darab a tálcán","copyN":"azonos darab a tálcán","unit":"darabköltség","fils":"Filamentek","color1":"szín","colorN":"szín","perKg":"/kg","tower":"Tisztítótorony és öblítés, már benne a grammokban","costT":"Darabköltség","profitT":"Haszon a választott kulccsal","c_material":"Anyag","c_energy":"Áram","c_machine":"Nyomtató kopása","c_reserve":"Géptartalék","c_failure":"Tartalék hibákra","c_labor":"Munka","c_pack":"Csomagolás","src_custom":"Általad megadva","src_orca":"OrcaSlicer-profil","src_average":"Átlagár","src_generic":"Általános értékek","cfgT":"Árbeállítások","cfgS":"általános, nyomtató és filamentek","general":"Általános","printer":"Nyomtató","p_price":"A nyomtató ára","p_life":"Élettartam","years":"év","p_uptime":"Éves használat","p_maint":"Éves karbantartás","p_power":"Átlagos fogyasztás","p_buffer":"Tartalékszorzó","f_note":"A kg-ár az OrcaSlicer-profil „Filament költsége” mezőjéből jön. Ha itt módosítod, ehhez a filamenthez elmentődik.","scan":"Már szeletelt tálcák keresése","scanP":"A bővítmény bekapcsolása előtt szeleteltél? Válaszd ki az OrcaSlicerben az utoljára szeletelt tálcát, és a bővítmény újraszeletelés nélkül beolvassa a G-kódját.","platesCost":"Teljes költséged","plateN":"{n}. tálca","platesTotal":"{n} tálca összesen, adóval","notSliced":"{n} még nincs szeletelve","notSlicedP":"Szeleteld ezt a tálcát az ár megtekintéséhez. Minden szeletelt tálca ára megmarad, amíg a projekt nyitva van.","customM":"Egyéni haszonkulcs","save":"Mentés és újraszámolás","reset":"Nyomtató értékeinek visszaállítása","h":"ó","min":"perc","cfg_intro":"Az egyes szeletelt tálcák eladási árához használt értékek. A Print Calculator fülön is módosíthatod őket, a nyomtatók költségprofiljával és a filamentárakkal együtt.","cfg_ro":"Csak olvasható.","cfg_currency":"Pénznem","cfg_search":"Pénznem keresése (név vagy kód)","cfg_vat":"ÁFA / adó (%)","cfg_margin":"Haszonkulcs (%)","cfg_failure":"Hibaarány (%)","cfg_electricity":"Áram (kWh-nként)","cfg_rate":"Munka (óránként)","cfg_setup":"Előkészítés tálcánként (perc)","cfg_finish":"Utómunka darabonként (perc)","cfg_pack":"Csomagolás darabonként","cfg_efficiency":"Anyagveszteség-szorzó (×)","cfg_language":"Nyelv","cfg_auto":"Mint az OrcaSlicer","py_perm":"Az OrcaSlicer nem engedte a G-kód olvasását. Ha rákérdez, válaszd az Igent, és szeletelj újra.","py_missing":"A G-kód már nem létezik. Szeletelj újra.","py_empty":"Ebben a G-kódban nincs olyan idő vagy gramm, amit a Print Calculator be tud olvasni.","py_fail":"Nem sikerült számolni: {e}","py_unit":"{p} darabonként adóval","py_plate":" ({n} db, {t})","py_hint":"Print Calculator: nyisd meg a Print Calculator fület a tálca árához.","py_none":"Ebben az OrcaSlicer-munkamenetben nincs szeletelt tálca. Szeleteld a tálcát, vagy exportáld a G-kódot (Ctrl+G), ha már szeletelve van.","cfg_minimum":"Minimális darabár adóval","cfg_rounding":"Darabár felfelé kerekítése","cfg_round_none":"Nincs kerekítés","cfg_round_99":",99-re","cfg_discount_qty":"Mennyiségi kedvezmény ettől (darab a tálcán)","cfg_discount_pct":"Mennyiségi kedvezmény (%)","adj_disc":"mennyiségi kedvezmény −{v}%","adj_min":"minimális ár","adj_round":"kerekítve","refShort":"€ irányár","refNote":"Az „€ irányár” jelölésű árak euróban számolt átlagok, nincsenek {c} pénznemre átváltva. Ellenőrizd őket lent a beállításokban.","extrasT":"Extrák","extrasP":"Mágnesek, csavarok, menetes betétek, festék… Válaszd ki, hogy minden extra darabonként vagy tálcánként egyszer számít. Ehhez a munkához megmarad.","extraName":"Tétel","extraCost":"Költség","extraAdd":"Extra hozzáadása","c_extras":"Extrák","quoteBtn":"Árajánlat másolása","quoteAll":"Minden tálca árajánlatának másolása","copied":"Árajánlat kimásolva. Illeszd be az üzenetedbe.","q_title":"Árajánlat","q_qty":"Mennyiség","q_unit":"Darabár","q_total":"Összesen","q_time":"Nyomtatási idő","q_material":"Anyag","q_taxinc":"adóval","byModelT":"Ár modellenként","byModelP":"Az anyag az egyes modellek filamentje szerint oszlik meg; az idő, a gép és az előkészítés a filamentrészesedés szerint. Becslés.","modelN":"{n}. modell","mdlCost":"költség/db","mdlPrice":"ár/db","cfg_updates":"Új verziók keresése a fül megnyitásakor","upd_new":"Elérhető a Print Calculator {v}. Frissítsd az OrcaSlicer bővítménykezelőjében.","allBtn":"Minden tálca","allCost":"Teljes költség","priceSetT":"A tálca ára adóval","profitSet":"haszon a tálcán","gramsSet":"filament a tálcához","partsN":"darab a tálcán","unitSet":"a tálca költsége","costSetT":"A tálca költsége","py_set":"{p} a tálcáért adóval ({n} db)","jobN":"{n}. munka","jobNameP":"Név (ügyfél, projekt…)","jobP":"Válaszd ki minden tálca munkáját. Minden munkának saját összege, haszonkulcsa és árajánlata van.","jobMargin":"A munka haszonkulcsa","jobTime":"teljes idő","jobProfit":"haszon","sumCost":"Költség","sumTax":"ÁFA","sumProfit":"Haszon","setupT":"Először erősítsd meg az értékeidet","setupP":"Az ár csak azután jelenik meg, hogy megerősítetted a lenti beállításokat: pénznem, adó, áram, a nyomtatód ára és az egyes filamentek kilónkénti ára. A kitöltött értékek tájékoztató jellegűek, nem biztos, hogy a tieid. Ezután nyomd meg a mentés gombot.","repBtn":"Hiba jelentése","repBusy":"G-kód előkészítése…","repDone":"A G-kód mentve ide: {p}. Húzd ezt a fájlt a böngészőben megnyílt hibajegybe, és írd le a problémát.","repNoG":"Ennek a tálcának nincs G-kódja. A hibajegy fájl nélkül nyílt meg: írd le a problémát, és ha tudod, szeleteld újra.","repLink":"Ha a böngésző nem nyílt meg, nyisd meg ezt a linket:","noLabels":"Ez a G-kód nem mondja meg, hány darab van a tálcán, ezért az ár az egész tálcára vonatkozik. A darabok számolásához kapcsold be a \"Label objects\" opciót az OrcaSlicer folyamatbeállításaiban (Egyéb), és szeleteld újra.","exPart":"darabonként","exPlate":"tálcánként"},"sv":{"imp":"Importera slicad fil","impBig":"Filen är för stor (högst 200 MB).","impFail":"Filen kunde inte läsas.","sliceAllP":"Flera plattor? Använd ”Slica alla” i OrcaSlicer så visas alla här. Du kan också importera en .gcode.3mf eller .gcode.","chT":"Försäljningskanaler","chP":"Priset i varje kanal täcker redan avgift och moms: din vinst är densamma överallt.","chDirect":"Direktförsäljning","chN":"Kanal","chFee":"Avgift","chPiece":"Per del","qChannel":"Pris för den här kanalen","chAdd":"Lägg till kanal","chName":"Namn (t.ex. Etsy)","chNote":"Avgift i % av priset inkl. moms plus fast avgift per försäljning. Pluginet höjer priset så att du behåller samma vinst.","withShip":"med frakt ({s})","q_ship":"Frakt","q_grand":"Totalt med frakt","qSave":"Spara offert","qSaved":"Offert sparad","qsT":"Sparade offerter","qsCopy":"Kopiera","qsPdf":"Skriv ut / PDF","qsDel":"Ta bort","qsCsv":"Exportera CSV","qsFile":"Sparad i {p}","qsFileOpen":"Sparad i {p}. Öppna den för att skriva ut eller spara som PDF.","spoolBtn":"Läs priser från Spoolman","spoolPick":"Spoolman…","spoolErr":"Spoolman kunde inte läsas: {e}","spoolOk":"{n} filament från Spoolman. Välj ett bredvid varje filament och spara.","spoolNone":"Spoolman har inga filament med pris och vikt.","cfg_shipping":"Frakt per beställning","cfg_q_name":"Namn på offerten (företag)","cfg_q_footer":"Anteckning sist i offerten","cfg_spoolman":"Spoolman-adress (valfritt)","qh_item":"Artikel","qh_qty":"Antal","qh_unit":"Styckpris","qh_vat":"Inkl. moms ({v} %)","qh_date":"Datum","buf_note":"Reservfaktor: lägger en marginal på skrivarens timkostnader (slitage, underhåll och el) för det oplanerade: haverier, delar (hotend, munstycke, byggplatta, remmar), stilleståndstid och sparande till nästa skrivare. 1,3 = +30 %. Ändrar inte materialet. Normalt: 1,2 till 1,3; krånglig skrivare: 1,4 till 1,5; 1 = ingen reserv.","eff_note":"Materialförlustfaktor: multiplicerar bara filamentkostnaden, för plast som G-koden inte räknar (spolrester, filamentbyten, testutskrifter, förstört filament). 1,10 = +10 % på materialet. Räkna inte med primetornet och rensningen (redan i grammen) eller misslyckade utskrifter (de har en egen reserv). Normalt: 1,05 till 1,15; 1 = ingen förlust.","sub":"FDM-försäljningspris för den slicade plattan","readyT":"Redo att räkna","readyP":"Slica en platta i OrcaSlicer så visas försäljningspriset här, med material, el, slitage på skrivaren och din marginal.","s1":"Välj skrivare och filament","s2":"Slica plattan","s3":"Se priset","reading":"Läser G-koden","failT":"Kunde inte räkna","warnT":"Observera","noPrinter":"Namnlös skrivare","updated":"Uppdaterad {t}","priceT":"Pris per del inkl. moms","mine":"din marginal","sim":"plattans marginal","profit":"vinst per del","margin":"marginal","plate":"hela plattan ({n} delar)","time":"plattans tid","grams":"filament per del","copy1":"del på plattan","copyN":"likadana delar på plattan","unit":"kostnad per del","fils":"Filament","color1":"färg","colorN":"färger","perKg":"/kg","tower":"Rensningstorn och rensningar, redan i grammen","costT":"Kostnad per del","profitT":"Vinst med vald marginal","c_material":"Material","c_energy":"El","c_machine":"Slitage på skrivaren","c_reserve":"Maskinreserv","c_failure":"Reserv för misslyckade utskrifter","c_labor":"Arbete","c_pack":"Förpackning","src_custom":"Satt av dig","src_orca":"OrcaSlicer-profil","src_average":"Snittpris","src_generic":"Standardvärden","cfgT":"Prisinställningar","cfgS":"allmänt, skrivare och filament","general":"Allmänt","printer":"Skrivare","p_price":"Skrivarens pris","p_life":"Livslängd","years":"år","p_uptime":"Årlig användning","p_maint":"Årligt underhåll","p_power":"Genomsnittlig effekt","p_buffer":"Reservfaktor","f_note":"Priset per kg kommer från fältet ”Filamentkostnad” i OrcaSlicer-profilen. Ändrar du det här sparas det för det filamentet.","scan":"Hitta redan slicade plattor","scanP":"Slicade du innan pluginet var på? Välj den senast slicade plattan i OrcaSlicer så läser pluginet dess G-kod utan att slica igen.","platesCost":"Din totala kostnad","plateN":"Platta {n}","platesTotal":"Summa för {n} plattor, inkl. moms","notSliced":"{n} är inte slicad än","notSlicedP":"Slica plattan för att se priset. Priset för varje slicad platta sparas medan projektet är öppet.","customM":"Egen marginal","save":"Spara och räkna om","reset":"Återställ skrivarens värden","h":"h","min":"min","cfg_intro":"Värden för försäljningspriset på varje slicad platta. Du kan också ändra dem, tillsammans med varje skrivares kostnadsprofil och filamentpriserna, på fliken Print Calculator.","cfg_ro":"Skrivskyddad.","cfg_currency":"Valuta","cfg_search":"Sök valuta (namn eller kod)","cfg_vat":"Moms / skatt (%)","cfg_margin":"Marginal (%)","cfg_failure":"Andel misslyckade (%)","cfg_electricity":"El (per kWh)","cfg_rate":"Arbete (per timme)","cfg_setup":"Förberedelse per platta (min)","cfg_finish":"Efterbearbetning per del (min)","cfg_pack":"Förpackning per del","cfg_efficiency":"Materialspillfaktor (×)","cfg_language":"Språk","cfg_auto":"Samma som OrcaSlicer","py_perm":"OrcaSlicer tillät inte att G-koden lästes. Välj Ja när den frågar och slica igen.","py_missing":"G-koden finns inte längre. Slica igen.","py_empty":"Den här G-koden har ingen tid eller gram som Print Calculator kan läsa.","py_fail":"Kunde inte räkna: {e}","py_unit":"{p} per del inkl. moms","py_plate":" ({n} delar, {t})","py_hint":"Print Calculator: öppna fliken Print Calculator för att se plattans pris.","py_none":"Inga slicade plattor i den här OrcaSlicer-sessionen. Slica plattan, eller exportera G-koden (Ctrl+G) om den redan är slicad.","cfg_minimum":"Lägsta pris per del, inkl. moms","cfg_rounding":"Avrunda priset per del uppåt","cfg_round_none":"Avrunda inte","cfg_round_99":"Till ,99","cfg_discount_qty":"Mängdrabatt från (delar på plattan)","cfg_discount_pct":"Mängdrabatt (%)","adj_disc":"mängdrabatt −{v} %","adj_min":"lägsta pris","adj_round":"avrundat","refShort":"€-riktpris","refNote":"Priser märkta ”€-riktpris” är snitt i euro, inte omräknade till {c}. Kontrollera dem i inställningarna nedan.","extrasT":"Tillägg","extrasP":"Magneter, skruvar, gänginsatser, färg… Välj om varje tillägg räknas per del eller en gång per platta. Sparas för det här jobbet.","extraName":"Artikel","extraCost":"Kostnad","extraAdd":"Lägg till extra","c_extras":"Extra","quoteBtn":"Kopiera offert","quoteAll":"Kopiera offert för alla plattor","copied":"Offerten är kopierad. Klistra in den i ditt meddelande.","q_title":"Offert","q_qty":"Antal","q_unit":"Pris per del","q_total":"Summa","q_time":"Utskriftstid","q_material":"Material","q_taxinc":"inkl. moms","byModelT":"Pris per modell","byModelP":"Material fördelas efter varje modells filament; tid, maskin och förberedelse efter dess andel av filamentet. Uppskattning.","modelN":"Modell {n}","mdlCost":"kostnad/del","mdlPrice":"pris/del","cfg_updates":"Sök efter nya versioner när fliken öppnas","upd_new":"Print Calculator {v} finns. Uppdatera det i OrcaSlicers pluginhanterare.","allBtn":"Alla plattor","allCost":"Total kostnad","priceSetT":"Pris för plattan inkl. moms","profitSet":"vinst för plattan","gramsSet":"filament för plattan","partsN":"delar på plattan","unitSet":"kostnad för plattan","costSetT":"Kostnad för plattan","py_set":"{p} för plattan inkl. moms ({n} delar)","jobN":"Jobb {n}","jobNameP":"Namn (kund, projekt…)","jobP":"Välj jobb för varje platta. Varje jobb har egen summa, marginal och offert.","jobMargin":"Jobbets marginal","jobTime":"total tid","jobProfit":"vinst","sumCost":"Kostnad","sumTax":"Moms","sumProfit":"Vinst","setupT":"Bekräfta dina värden först","setupP":"Priset visas först när du har bekräftat inställningarna nedan: valuta, moms, el, priset på din skrivare och priset per kg för varje filament. De ifyllda värdena är referensvärden och behöver inte vara dina. Tryck sedan på spara.","repBtn":"Rapportera ett problem","repBusy":"Förbereder G-koden…","repDone":"G-koden sparades i {p}. Dra filen till ärendet som öppnades i webbläsaren och beskriv problemet.","repNoG":"Plattan har ingen G-kod. Ärendet öppnades utan fil: beskriv problemet och slica igen om du kan.","repLink":"Om webbläsaren inte öppnades, öppna den här länken:","noLabels":"Den här G-koden anger inte hur många delar som finns på plattan, så priset gäller hela plattan. Slå på \"Label objects\" i OrcaSlicers processinställningar (Övrigt) och slica igen för att räkna delarna.","exPart":"per del","exPlate":"per platta"},"ca":{"imp":"Importa un fitxer laminat","impBig":"Aquest fitxer és massa gran (màxim 200 MB).","impFail":"No s'ha pogut llegir aquest fitxer.","sliceAllP":"Diverses plaques? Fes servir \"Lamina-ho tot\" a l'OrcaSlicer i apareixen totes aquí. També pots importar un .gcode.3mf o .gcode.","chT":"Canals de venda","chP":"El preu de cada canal ja cobreix la comissió i l'IVA: el teu benefici és el mateix a tots.","chDirect":"Venda directa","chN":"Canal","chFee":"Comissió","chPiece":"Per peça","qChannel":"Preu per a aquest canal","chAdd":"Afegeix un canal","chName":"Nom (p. ex. Etsy)","chNote":"Comissió en % sobre el preu amb IVA i comissió fixa per venda. El connector apuja el preu perquè mantinguis el mateix benefici.","withShip":"amb enviament ({s})","q_ship":"Enviament","q_grand":"Total amb enviament","qSave":"Desa el pressupost","qSaved":"Pressupost desat","qsT":"Pressupostos desats","qsCopy":"Copia","qsPdf":"Imprimeix / PDF","qsDel":"Esborra","qsCsv":"Exporta CSV","qsFile":"Desat a {p}","qsFileOpen":"Desat a {p}. Obre'l per imprimir-lo o desar-lo en PDF.","spoolBtn":"Llegeix els preus de Spoolman","spoolPick":"Spoolman…","spoolErr":"No s'ha pogut llegir Spoolman: {e}","spoolOk":"{n} filaments de Spoolman. Tria'n un al costat de cada filament i desa.","spoolNone":"Spoolman no té filaments amb preu i pes.","cfg_shipping":"Enviament per comanda","cfg_q_name":"Nom al pressupost (empresa)","cfg_q_footer":"Nota al final del pressupost","cfg_spoolman":"Adreça de Spoolman (opcional)","qh_item":"Article","qh_qty":"Quant.","qh_unit":"Preu unitari","qh_vat":"IVA inclòs ({v}%)","qh_date":"Data","buf_note":"Factor de reserva: suma un marge als costos per hora de la impressora (desgast, manteniment i electricitat) per a l'imprevist: avaries, peces (hotend, broquet, placa, corretges), temps aturat i estalviar per a la propera impressora. 1,3 = +30%. No canvia el material. Normal: 1,2 a 1,3; impressora amb molts problemes: 1,4 a 1,5; 1 = sense reserva.","eff_note":"Factor de pèrdua de material: multiplica només el cost del filament, pel plàstic que el G-code no compta (restes de bobina, canvis de filament, proves, filament fet malbé). 1,10 = +10% en material. No hi comptis la torre i la purga (ja són als grams) ni les fallades (tenen la seva reserva). Normal: 1,05 a 1,15; 1 = sense pèrdues.","sub":"Preu de venda FDM de la placa laminada","readyT":"A punt per calcular","readyP":"Lamina una placa a l'OrcaSlicer i aquí apareixerà el preu de venda, amb material, electricitat, desgast de la impressora i el teu marge.","s1":"Tria impressora i filaments","s2":"Lamina la placa","s3":"Mira el preu","reading":"Llegint el G-code","failT":"No s'ha pogut calcular","warnT":"Atenció","noPrinter":"Impressora sense nom","updated":"Actualitzat a les {t}","priceT":"Preu per peça amb impostos","mine":"el teu marge","sim":"marge d'aquesta placa","profit":"benefici per peça","margin":"marge","plate":"placa sencera ({n} peces)","time":"temps de la placa","grams":"filament per peça","copy1":"peça a la placa","copyN":"peces iguals a la placa","unit":"cost per peça","fils":"Filaments","color1":"color","colorN":"colors","perKg":"/kg","tower":"Torre de purga i purgues, ja incloses en els grams","costT":"Cost per peça","profitT":"Benefici amb el marge triat","c_material":"Material","c_energy":"Electricitat","c_machine":"Desgast de la impressora","c_reserve":"Reserva de la màquina","c_failure":"Reserva per errors","c_labor":"Mà d'obra","c_pack":"Embalatge","src_custom":"Definit per tu","src_orca":"Perfil de l'OrcaSlicer","src_average":"Preu mitjà","src_generic":"Valors genèrics","cfgT":"Configuració del preu","cfgS":"general, impressora i filaments","general":"General","printer":"Impressora","p_price":"Preu de la impressora","p_life":"Vida útil","years":"anys","p_uptime":"Ús anual","p_maint":"Manteniment anual","p_power":"Consum mitjà","p_buffer":"Factor de reserva","f_note":"El preu per kg ve del camp \"Cost del filament\" del perfil de l'OrcaSlicer. Si el canvies aquí, es desa per a aquest filament.","scan":"Cerca plaques ja laminades","scanP":"Has laminat abans d'activar el connector? Tria a l'OrcaSlicer l'última placa laminada i el connector en llegeix el G-code sense tornar a laminar.","platesCost":"El teu cost total","plateN":"Placa {n}","platesTotal":"Total de {n} plaques, amb impostos","notSliced":"La {n} encara no s'ha laminat","notSlicedP":"Lamina aquesta placa per veure'n el preu. El preu de cada placa laminada es manté mentre el projecte és obert.","customM":"Marge personalitzat","save":"Desa i recalcula","reset":"Restableix la impressora","h":"h","min":"min","cfg_intro":"Valors per al preu de venda de cada placa laminada. També els pots canviar, amb el perfil de cada impressora i el preu dels filaments, a la pestanya Print Calculator.","cfg_ro":"Només lectura.","cfg_currency":"Moneda","cfg_search":"Cerca moneda (nom o codi)","cfg_vat":"IVA / impost (%)","cfg_margin":"Marge (%)","cfg_failure":"Taxa d'errors (%)","cfg_electricity":"Electricitat (per kWh)","cfg_rate":"Mà d'obra (per hora)","cfg_setup":"Preparació per placa (min)","cfg_finish":"Acabat per peça (min)","cfg_pack":"Embalatge per peça","cfg_efficiency":"Factor de pèrdua de material (×)","cfg_language":"Idioma","cfg_auto":"Com l'OrcaSlicer","py_perm":"L'OrcaSlicer no ha permès llegir el G-code. Quan ho demani, tria Sí i torna a laminar.","py_missing":"El G-code ja no existeix. Torna a laminar.","py_empty":"Aquest G-code no té temps ni grams que el Print Calculator pugui llegir.","py_fail":"No s'ha pogut calcular: {e}","py_unit":"{p} per peça amb impostos","py_plate":" ({n} peces, {t})","py_hint":"Print Calculator: obre la pestanya Print Calculator per veure el preu d'aquesta placa.","py_none":"No hi ha plaques laminades en aquesta sessió de l'OrcaSlicer. Lamina la placa o exporta el G-code (Ctrl+G) si ja està laminada.","cfg_minimum":"Preu mínim per peça, amb impostos","cfg_rounding":"Arrodonir el preu per peça cap amunt","cfg_round_none":"No arrodonir","cfg_round_99":"Fins a ,99","cfg_discount_qty":"Descompte per quantitat a partir de (peces a la placa)","cfg_discount_pct":"Descompte per quantitat (%)","adj_disc":"descompte per quantitat −{v}%","adj_min":"preu mínim","adj_round":"arrodonit","refShort":"referència en €","refNote":"Els preus marcats \"referència en €\" són mitjanes en euros, no convertides a {c}. Revisa-les a la configuració de sota.","extrasT":"Extres","extrasP":"Imants, cargols, insercions, pintura… Tria si cada extra compta a cada peça o un cop a la placa. Es desen per a aquesta feina.","extraName":"Article","extraCost":"Cost","extraAdd":"Afegeix un extra","c_extras":"Extres","quoteBtn":"Copia el pressupost","quoteAll":"Copia el pressupost de totes les plaques","copied":"Pressupost copiat. Enganxa'l al teu missatge.","q_title":"Pressupost","q_qty":"Quantitat","q_unit":"Preu per peça","q_total":"Total","q_time":"Temps d'impressió","q_material":"Material","q_taxinc":"impostos inclosos","byModelT":"Preu per model","byModelP":"El material es reparteix segons el filament de cada model; temps, màquina i preparació segons la seva part del filament. És una estimació.","modelN":"Model {n}","mdlCost":"cost/peça","mdlPrice":"preu/peça","cfg_updates":"Cerca versions noves en obrir la pestanya","upd_new":"Print Calculator {v} està disponible. Actualitza'l al gestor de connectors de l'OrcaSlicer.","allBtn":"Totes les plaques","allCost":"Cost total","priceSetT":"Preu de la placa amb impostos","profitSet":"benefici de la placa","gramsSet":"filament de la placa","partsN":"peces a la placa","unitSet":"cost de la placa","costSetT":"Cost de la placa","py_set":"{p} per la placa amb impostos ({n} peces)","jobN":"Feina {n}","jobNameP":"Nom (client, projecte…)","jobP":"Tria la feina de cada placa. Cada feina té el seu total, el seu marge i el seu pressupost.","jobMargin":"Marge de la feina","jobTime":"temps total","jobProfit":"benefici","sumCost":"Cost","sumTax":"IVA","sumProfit":"Benefici","setupT":"Primer, confirma els teus valors","setupP":"El preu només apareix després de confirmar la configuració de sota: moneda, impostos, electricitat, el preu de la teva impressora i el preu per kg de cada filament. Els valors ja emplenats són de referència i potser no són els teus. Després prem el botó de desar.","repBtn":"Informa d'un problema","repBusy":"Preparant el G-code…","repDone":"G-code desat a {p}. Arrossega aquest fitxer a l'issue que s'ha obert al navegador i descriu el problema.","repNoG":"Aquesta placa no té G-code. L'issue s'ha obert sense fitxer: descriu el problema i, si pots, lamina i torna-ho a provar.","repLink":"Si el navegador no s'ha obert, obre aquest enllaç:","noLabels":"Aquest G-code no indica quantes peces hi ha a la placa, així que el preu és el de la placa sencera. Per comptar les peces, activa \"Label objects\" al procés d'OrcaSlicer, a Altres, i torna a laminar.","exPart":"per peça","exPlate":"per placa"},"eu":{"imp":"Inportatu xerratutako fitxategia","impBig":"Fitxategi hau handiegia da (gehienez 200 MB).","impFail":"Ezin izan da fitxategi hau irakurri.","sliceAllP":"Hainbat plater? Erabili \"Xerratu dena\" OrcaSlicer-en eta denak hemen agertuko dira. .gcode.3mf edo .gcode bat ere inporta dezakezu.","chT":"Salmenta-kanalak","chP":"Kanal bakoitzaren prezioak komisioa eta BEZa estaltzen ditu jada: irabazia berdina da guztietan.","chDirect":"Salmenta zuzena","chN":"Kanala","chFee":"Komisioa","chPiece":"Piezako","qChannel":"Kanal honetarako prezioa","chAdd":"Gehitu kanala","chName":"Izena (adib. Etsy)","chNote":"Komisioa BEZdun prezioaren %-tan eta salmenta bakoitzeko komisio finkoa. Pluginak prezioa igotzen du irabazi bera izan dezazun.","withShip":"bidalketarekin ({s})","q_ship":"Bidalketa","q_grand":"Guztira bidalketarekin","qSave":"Gorde aurrekontua","qSaved":"Aurrekontua gordeta","qsT":"Gordetako aurrekontuak","qsCopy":"Kopiatu","qsPdf":"Inprimatu / PDF","qsDel":"Ezabatu","qsCsv":"Esportatu CSV","qsFile":"Hemen gordeta: {p}","qsFileOpen":"Hemen gordeta: {p}. Ireki inprimatzeko edo PDF gisa gordetzeko.","spoolBtn":"Irakurri prezioak Spoolman-etik","spoolPick":"Spoolman…","spoolErr":"Ezin izan da Spoolman irakurri: {e}","spoolOk":"Spoolman-eko {n} harizpi. Aukeratu bat harizpi bakoitzaren ondoan eta gorde.","spoolNone":"Spoolman-ek ez du prezio eta pisudun harizpirik.","cfg_shipping":"Bidalketa eskaera bakoitzeko","cfg_q_name":"Izena aurrekontuan (enpresa)","cfg_q_footer":"Oharra aurrekontuaren amaieran","cfg_spoolman":"Spoolman-en helbidea (aukerakoa)","qh_item":"Artikulua","qh_qty":"Kop.","qh_unit":"Unitate-prezioa","qh_vat":"BEZa barne (% {v})","qh_date":"Data","buf_note":"Erreserba-faktorea: inprimagailuaren orduko kostuei (higadura, mantentze-lanak eta elektrizitatea) marjina bat gehitzen die ustekabekoetarako: matxurak, piezak (hotend, tobera, plaka, uhalak), geldialdiak eta hurrengo inprimagailurako aurreztea. 1,3 = +% 30. Ez du materiala aldatzen. Ohikoa: 1,2 eta 1,3 artean; arazo askoko inprimagailua: 1,4 eta 1,5 artean; 1 = erreserbarik ez.","eff_note":"Material-galeren faktorea: harizpiaren kostua bakarrik biderkatzen du, G-codeak kontatzen ez duen plastikoagatik (bobina-hondarrak, harizpi-aldaketak, probak, hondatutako harizpia). 1,10 = +% 10 materialean. Ez kontatu dorrea eta purga (gramoetan daude jada) ezta hutsegiteak ere (beren erreserba dute). Ohikoa: 1,05 eta 1,15 artean; 1 = galerarik ez.","sub":"Xerratutako platerraren FDM salmenta-prezioa","readyT":"Kalkulatzeko prest","readyP":"Xerratu plater bat OrcaSlicer-en eta hemen agertuko da salmenta-prezioa: materiala, elektrizitatea, inprimagailuaren higadura eta zure marjina.","s1":"Aukeratu inprimagailua eta harizpiak","s2":"Xerratu platerra","s3":"Ikusi prezioa","reading":"G-kodea irakurtzen","failT":"Ezin izan da kalkulatu","warnT":"Kontuz","noPrinter":"Izenik gabeko inprimagailua","updated":"Eguneratua: {t}","priceT":"Pieza bakoitzeko prezioa zergekin","mine":"zure marjina","sim":"plater honen marjina","profit":"irabazia piezako","margin":"marjina","plate":"plater osoa ({n} pieza)","time":"platerraren denbora","grams":"harizpia piezako","copy1":"pieza platerrean","copyN":"pieza berdin platerrean","unit":"kostua piezako","fils":"Harizpiak","color1":"kolore","colorN":"kolore","perKg":"/kg","tower":"Purga-dorrea eta purgak, gramoetan sartuta","costT":"Kostua piezako","profitT":"Irabazia aukeratutako marjinarekin","c_material":"Materiala","c_energy":"Elektrizitatea","c_machine":"Inprimagailuaren higadura","c_reserve":"Makinaren erreserba","c_failure":"Hutsegiteen erreserba","c_labor":"Eskulana","c_pack":"Ontziratzea","src_custom":"Zuk ezarria","src_orca":"OrcaSlicer profila","src_average":"Batez besteko prezioa","src_generic":"Balio orokorrak","cfgT":"Prezioaren ezarpenak","cfgS":"orokorra, inprimagailua eta harizpiak","general":"Orokorra","printer":"Inprimagailua","p_price":"Inprimagailuaren prezioa","p_life":"Bizitza erabilgarria","years":"urte","p_uptime":"Urteko erabilera","p_maint":"Urteko mantentzea","p_power":"Batez besteko kontsumoa","p_buffer":"Erreserba-faktorea","f_note":"Kg-ko prezioa OrcaSlicer profileko \"Harizpiaren kostua\" eremutik dator. Hemen aldatzen baduzu, harizpi horretarako gordeko da.","scan":"Bilatu dagoeneko xerratutako platerrak","scanP":"Plugina aktibatu aurretik xerratu duzu? Aukeratu OrcaSlicer-en azken xerratutako platerra eta pluginak bere G-kodea irakurriko du berriro xerratu gabe.","platesCost":"Zure kostu osoa","plateN":"{n}. platerra","platesTotal":"{n} platerren guztira, zergekin","notSliced":"{n} ez da oraindik xerratu","notSlicedP":"Xerratu plater hau prezioa ikusteko. Xerratutako plater bakoitzaren prezioa gordetzen da proiektua irekita dagoen bitartean.","customM":"Marjina pertsonalizatua","save":"Gorde eta birkalkulatu","reset":"Berrezarri inprimagailuaren balioak","h":"h","min":"min","cfg_intro":"Xerratutako plater bakoitzaren salmenta-prezioan erabilitako balioak. Print Calculator fitxan ere alda ditzakezu, inprimagailu bakoitzaren kostu-profilarekin eta harizpien prezioekin batera.","cfg_ro":"Irakurtzeko soilik.","cfg_currency":"Moneta","cfg_search":"Bilatu moneta (izena edo kodea)","cfg_vat":"BEZ / zerga (%)","cfg_margin":"Marjina (%)","cfg_failure":"Hutsegite-tasa (%)","cfg_electricity":"Elektrizitatea (kWh-ko)","cfg_rate":"Eskulana (orduko)","cfg_setup":"Prestaketa platerreko (min)","cfg_finish":"Akabera piezako (min)","cfg_pack":"Ontziratzea piezako","cfg_efficiency":"Material-galeren faktorea (×)","cfg_language":"Hizkuntza","cfg_auto":"OrcaSlicer bezala","py_perm":"OrcaSlicer-ek ez du G-kodea irakurtzen utzi. Galdetzen duenean, aukeratu Bai eta xerratu berriro.","py_missing":"G-kodea ez dago jada. Xerratu berriro.","py_empty":"G-kode honek ez du Print Calculator-ek irakur dezakeen denborarik edo gramorik.","py_fail":"Ezin izan da kalkulatu: {e}","py_unit":"{p} piezako zergekin","py_plate":" ({n} pieza, {t})","py_hint":"Print Calculator: ireki Print Calculator fitxa plater honen prezioa ikusteko.","py_none":"Ez dago xerratutako platerrik OrcaSlicer saio honetan. Xerratu platerra, edo esportatu G-kodea (Ctrl+G) dagoeneko xerratuta badago.","cfg_minimum":"Pieza bakoitzeko gutxieneko prezioa, zergekin","cfg_rounding":"Biribildu gora pieza bakoitzeko prezioa","cfg_round_none":"Ez biribildu","cfg_round_99":",99 arte","cfg_discount_qty":"Kantitate-deskontua hemendik aurrera (pieza platerrean)","cfg_discount_pct":"Kantitate-deskontua (%)","adj_disc":"kantitate-deskontua −{v}%","adj_min":"gutxieneko prezioa","adj_round":"biribildua","refShort":"€-ko erreferentzia","refNote":"\"€-ko erreferentzia\" markatutako prezioak euroko batez bestekoak dira, ez daude {c}-ra bihurtuta. Egiaztatu beheko ezarpenetan.","extrasT":"Gehigarriak","extrasP":"Imanak, torlojuak, txertaketak, pintura… Aukeratu gehigarri bakoitza pieza bakoitzean ala platerean behin kontatzen den. Lan honetarako gordetzen dira.","extraName":"Elementua","extraCost":"Kostua","extraAdd":"Gehitu gehigarria","c_extras":"Gehigarriak","quoteBtn":"Kopiatu aurrekontua","quoteAll":"Kopiatu plater guztien aurrekontua","copied":"Aurrekontua kopiatuta. Itsatsi zure mezuan.","q_title":"Aurrekontua","q_qty":"Kantitatea","q_unit":"Prezioa piezako","q_total":"Guztira","q_time":"Inprimatze-denbora","q_material":"Materiala","q_taxinc":"zergak barne","byModelT":"Prezioa modeloko","byModelP":"Materiala modelo bakoitzaren harizpiaren arabera banatzen da; denbora, makina eta prestaketa harizpiaren zatiaren arabera. Estimazio bat da.","modelN":"{n}. modeloa","mdlCost":"kostua/pieza","mdlPrice":"prezioa/pieza","cfg_updates":"Bilatu bertsio berriak fitxa irekitzean","upd_new":"Print Calculator {v} eskuragarri dago. Eguneratu OrcaSlicer-en plugin-kudeatzailean.","allBtn":"Plater guztiak","allCost":"Kostu osoa","priceSetT":"Platerraren prezioa zergekin","profitSet":"platerraren irabazia","gramsSet":"platerraren harizpia","partsN":"pieza platerrean","unitSet":"platerraren kostua","costSetT":"Platerraren kostua","py_set":"{p} platerragatik zergekin ({n} pieza)","jobN":"{n}. lana","jobNameP":"Izena (bezeroa, proiektua…)","jobP":"Aukeratu plater bakoitzaren lana. Lan bakoitzak bere guztizkoa, marjina eta aurrekontua ditu.","jobMargin":"Lanaren marjina","jobTime":"denbora osoa","jobProfit":"irabazia","sumCost":"Kostua","sumTax":"BEZ","sumProfit":"Irabazia","setupT":"Lehenik, berretsi zure balioak","setupP":"Prezioa beheko ezarpenak berretsi ondoren bakarrik agertzen da: moneta, zerga, elektrizitatea, zure inprimagailuaren prezioa eta harizpi bakoitzaren kg-ko prezioa. Beteta dauden balioak erreferentziazkoak dira eta agian ez dira zureak. Ondoren sakatu gordetzeko botoia.","repBtn":"Arazo baten berri eman","repBusy":"G-code prestatzen…","repDone":"G-code hemen gorde da: {p}. Arrastatu fitxategi hau nabigatzailean ireki den issue-ra eta deskribatu arazoa.","repNoG":"Plater honek ez du G-code-rik. Issue-a fitxategirik gabe ireki da: deskribatu arazoa eta, ahal baduzu, xerratu berriro.","repLink":"Nabigatzailea ireki ez bada, ireki esteka hau:","noLabels":"G-code honek ez du esaten platerrean zenbat pieza dauden, beraz prezioa plater osoarena da. Piezak zenbatzeko, aktibatu \"Label objects\" OrcaSlicer-en prozesu-ezarpenetan (Besteak) eta xerratu berriro.","exPart":"piezako","exPlate":"platereko"},"lt":{"imp":"Importuoti supjaustytą failą","impBig":"Failas per didelis (daugiausia 200 MB).","impFail":"Nepavyko perskaityti šio failo.","sliceAllP":"Kelios plokštės? OrcaSlicer spauskite „Supjaustyti viską“ ir visos atsiras čia. Taip pat galite importuoti .gcode.3mf arba .gcode.","chT":"Pardavimo kanalai","chP":"Kiekvieno kanalo kaina jau padengia mokestį ir PVM: jūsų pelnas visur toks pat.","chDirect":"Tiesioginis pardavimas","chN":"Kanalas","chFee":"Mokestis","chPiece":"Už detalę","qChannel":"Kaina šiam kanalui","chAdd":"Pridėti kanalą","chName":"Pavadinimas (pvz., Etsy)","chNote":"Mokestis % nuo kainos su PVM ir fiksuotas mokestis už pardavimą. Įskiepis padidina kainą, kad pelnas liktų toks pat.","withShip":"su pristatymu ({s})","q_ship":"Pristatymas","q_grand":"Iš viso su pristatymu","qSave":"Išsaugoti pasiūlymą","qSaved":"Pasiūlymas išsaugotas","qsT":"Išsaugoti pasiūlymai","qsCopy":"Kopijuoti","qsPdf":"Spausdinti / PDF","qsDel":"Ištrinti","qsCsv":"Eksportuoti CSV","qsFile":"Išsaugota: {p}","qsFileOpen":"Išsaugota: {p}. Atidarykite, kad atspausdintumėte arba išsaugotumėte PDF.","spoolBtn":"Nuskaityti kainas iš Spoolman","spoolPick":"Spoolman…","spoolErr":"Nepavyko nuskaityti Spoolman: {e}","spoolOk":"{n} gijų iš Spoolman. Pasirinkite vieną prie kiekvienos gijos ir išsaugokite.","spoolNone":"Spoolman neturi gijų su kaina ir svoriu.","cfg_shipping":"Pristatymas už užsakymą","cfg_q_name":"Pavadinimas pasiūlyme (įmonė)","cfg_q_footer":"Pastaba pasiūlymo pabaigoje","cfg_spoolman":"Spoolman adresas (nebūtina)","qh_item":"Prekė","qh_qty":"Kiekis","qh_unit":"Vieneto kaina","qh_vat":"Su PVM ({v} %)","qh_date":"Data","buf_note":"Rezervo koeficientas: prideda atsargą prie spausdintuvo valandinių išlaidų (nusidėvėjimas, priežiūra ir elektra) nenumatytiems atvejams: gedimams, detalėms (hotend, antgalis, plokštė, diržai), prastovoms ir taupymui kitam spausdintuvui. 1,3 = +30 %. Medžiagos nekeičia. Įprastai: 1,2–1,3; problemiškas spausdintuvas: 1,4–1,5; 1 = be rezervo.","eff_note":"Medžiagos nuostolių koeficientas: daugina tik gijos kainą, už plastiką, kurio G-kodas neskaičiuoja (ričių likučiai, gijos keitimai, bandomieji spaudiniai, sugadinta gija). 1,10 = +10 % medžiagai. Neįskaičiuokite bokšto ir prapūtimo (jau gramuose) ir nepavykusių spaudinių (jiems skirtas atskiras rezervas). Įprastai: 1,05–1,15; 1 = be nuostolių.","sub":"Supjaustytos plokštės FDM pardavimo kaina","readyT":"Paruošta skaičiuoti","readyP":"Supjaustykite plokštę OrcaSlicer ir čia pamatysite jos pardavimo kainą su medžiaga, elektra, spausdintuvo nusidėvėjimu ir jūsų marža.","s1":"Pasirinkite spausdintuvą ir gijas","s2":"Supjaustykite plokštę","s3":"Pažiūrėkite kainą","reading":"Skaitomas G-kodas","failT":"Nepavyko apskaičiuoti","warnT":"Dėmesio","noPrinter":"Bevardis spausdintuvas","updated":"Atnaujinta {t}","priceT":"Detalės kaina su mokesčiais","mine":"jūsų marža","sim":"šios plokštės marža","profit":"pelnas už detalę","margin":"marža","plate":"visa plokštė ({n} vnt.)","time":"plokštės laikas","grams":"gija detalei","copy1":"detalė plokštėje","copyN":"vienodos detalės plokštėje","unit":"detalės savikaina","fils":"Gijos","color1":"spalva","colorN":"spalvos","perKg":"/kg","tower":"Valymo bokštas ir valymai, jau įskaičiuoti gramuose","costT":"Detalės savikaina","profitT":"Pelnas su pasirinkta marža","c_material":"Medžiaga","c_energy":"Elektra","c_machine":"Spausdintuvo nusidėvėjimas","c_reserve":"Įrangos rezervas","c_failure":"Rezervas klaidoms","c_labor":"Darbas","c_pack":"Pakuotė","src_custom":"Nustatyta jūsų","src_orca":"OrcaSlicer profilis","src_average":"Vidutinė kaina","src_generic":"Bendrosios reikšmės","cfgT":"Kainos nustatymai","cfgS":"bendra, spausdintuvas ir gijos","general":"Bendra","printer":"Spausdintuvas","p_price":"Spausdintuvo kaina","p_life":"Tarnavimo laikas","years":"m.","p_uptime":"Metinis naudojimas","p_maint":"Metinė priežiūra","p_power":"Vidutinė galia","p_buffer":"Rezervo koeficientas","f_note":"Kaina už kg imama iš OrcaSlicer profilio lauko „Gijos kaina“. Jei pakeisite ją čia, ji bus išsaugota tai gijai.","scan":"Rasti jau supjaustytas plokštes","scanP":"Pjaustėte prieš įjungdami įskiepį? OrcaSlicer pasirinkite paskutinę supjaustytą plokštę ir įskiepis nuskaitys jos G-kodą nepjaustydamas iš naujo.","platesCost":"Jūsų bendra savikaina","plateN":"{n} plokštė","platesTotal":"Iš viso {n} plokščių, su mokesčiais","notSliced":"{n} dar nesupjaustyta","notSlicedP":"Supjaustykite šią plokštę, kad pamatytumėte kainą. Kiekvienos supjaustytos plokštės kaina išlieka, kol projektas atidarytas.","customM":"Savo marža","save":"Išsaugoti ir perskaičiuoti","reset":"Atkurti spausdintuvo reikšmes","h":"val.","min":"min","cfg_intro":"Reikšmės, naudojamos kiekvienos supjaustytos plokštės pardavimo kainai. Jas taip pat galite keisti Print Calculator kortelėje, kartu su kiekvieno spausdintuvo savikainos profiliu ir gijų kainomis.","cfg_ro":"Tik skaitymui.","cfg_currency":"Valiuta","cfg_search":"Ieškoti valiutos (pavadinimas ar kodas)","cfg_vat":"PVM / mokestis (%)","cfg_margin":"Marža (%)","cfg_failure":"Klaidų dažnis (%)","cfg_electricity":"Elektra (už kWh)","cfg_rate":"Darbas (už valandą)","cfg_setup":"Paruošimas plokštei (min)","cfg_finish":"Apdaila detalei (min)","cfg_pack":"Pakuotė detalei","cfg_efficiency":"Medžiagos nuostolių koeficientas (×)","cfg_language":"Kalba","cfg_auto":"Kaip OrcaSlicer","py_perm":"OrcaSlicer neleido nuskaityti G-kodo. Kai paklaus, pasirinkite Taip ir supjaustykite iš naujo.","py_missing":"G-kodo nebėra. Supjaustykite iš naujo.","py_empty":"Šiame G-kode nėra laiko ar gramų, kuriuos Print Calculator galėtų nuskaityti.","py_fail":"Nepavyko apskaičiuoti: {e}","py_unit":"{p} už detalę su mokesčiais","py_plate":" ({n} vnt., {t})","py_hint":"Print Calculator: atidarykite Print Calculator kortelę, kad pamatytumėte šios plokštės kainą.","py_none":"Šioje OrcaSlicer sesijoje nėra supjaustytų plokščių. Supjaustykite plokštę arba eksportuokite G-kodą (Ctrl+G), jei ji jau supjaustyta.","cfg_minimum":"Mažiausia detalės kaina su mokesčiais","cfg_rounding":"Apvalinti detalės kainą į viršų","cfg_round_none":"Neapvalinti","cfg_round_99":"Iki ,99","cfg_discount_qty":"Kiekio nuolaida nuo (detalių plokštėje)","cfg_discount_pct":"Kiekio nuolaida (%)","adj_disc":"kiekio nuolaida −{v} %","adj_min":"mažiausia kaina","adj_round":"suapvalinta","refShort":"orientacinė € kaina","refNote":"Kainos, pažymėtos „orientacinė € kaina“, yra vidurkiai eurais, neperskaičiuoti į {c}. Patikrinkite jas toliau esančiuose nustatymuose.","extrasT":"Priedai","extrasP":"Magnetai, varžtai, įvorės, dažai… Pasirinkite, ar kiekvienas priedas skaičiuojamas kiekvienai detalei, ar vieną kartą plokštei. Išsaugoma šiam darbui.","extraName":"Prekė","extraCost":"Kaina","extraAdd":"Pridėti priedą","c_extras":"Priedai","quoteBtn":"Kopijuoti pasiūlymą","quoteAll":"Kopijuoti visų plokščių pasiūlymą","copied":"Pasiūlymas nukopijuotas. Įklijuokite jį į žinutę.","q_title":"Pasiūlymas","q_qty":"Kiekis","q_unit":"Detalės kaina","q_total":"Iš viso","q_time":"Spausdinimo laikas","q_material":"Medžiaga","q_taxinc":"su mokesčiais","byModelT":"Kaina pagal modelį","byModelP":"Medžiaga skirstoma pagal kiekvieno modelio giją; laikas, įranga ir paruošimas pagal jo gijos dalį. Tai įvertis.","modelN":"{n} modelis","mdlCost":"savikaina/vnt.","mdlPrice":"kaina/vnt.","cfg_updates":"Ieškoti naujų versijų atidarant kortelę","upd_new":"Yra Print Calculator {v}. Atnaujinkite jį OrcaSlicer įskiepių tvarkytuvėje.","allBtn":"Visos plokštės","allCost":"Bendra savikaina","priceSetT":"Plokštės kaina su mokesčiais","profitSet":"pelnas už plokštę","gramsSet":"gija plokštei","partsN":"detalės plokštėje","unitSet":"plokštės savikaina","costSetT":"Plokštės savikaina","py_set":"{p} už plokštę su mokesčiais ({n} vnt.)","jobN":"Darbas {n}","jobNameP":"Pavadinimas (klientas, projektas…)","jobP":"Pasirinkite kiekvienos plokštės darbą. Kiekvienas darbas turi savo sumą, maržą ir pasiūlymą.","jobMargin":"Darbo marža","jobTime":"bendras laikas","jobProfit":"pelnas","sumCost":"Savikaina","sumTax":"PVM","sumProfit":"Pelnas","setupT":"Pirmiausia patvirtinkite savo reikšmes","setupP":"Kaina rodoma tik patvirtinus žemiau esančius nustatymus: valiutą, mokestį, elektrą, spausdintuvo kainą ir kiekvienos gijos kainą už kg. Įrašytos reikšmės yra orientacinės ir gali būti ne jūsų. Tada paspauskite išsaugojimo mygtuką.","repBtn":"Pranešti apie problemą","repBusy":"Ruošiamas G-kodas…","repDone":"G-kodas išsaugotas: {p}. Nutempkite šį failą į naršyklėje atsidariusį pranešimą ir aprašykite problemą.","repNoG":"Ši plokštė neturi G-kodo. Pranešimas atidarytas be failo: aprašykite problemą ir, jei galite, supjaustykite iš naujo.","repLink":"Jei naršyklė neatsidarė, atidarykite šią nuorodą:","noLabels":"Šis G-kodas nenurodo, kiek detalių yra plokštėje, todėl kaina skirta visai plokštei. Norėdami skaičiuoti detales, įjunkite \"Label objects\" OrcaSlicer proceso nustatymuose (Kita) ir supjaustykite iš naujo.","exPart":"už detalę","exPlate":"už plokštę"},"ru":{"imp":"Импортировать нарезанный файл","impBig":"Файл слишком большой (максимум 200 МБ).","impFail":"Не удалось прочитать этот файл.","sliceAllP":"Несколько пластин? Нажмите «Нарезать всё» в OrcaSlicer, и все появятся здесь. Можно также импортировать .gcode.3mf или .gcode.","chT":"Каналы продаж","chP":"Цена в каждом канале уже покрывает комиссию и налог: ваша прибыль везде одинакова.","chDirect":"Прямая продажа","chN":"Канал","chFee":"Комиссия","chPiece":"За деталь","qChannel":"Цена для этого канала","chAdd":"Добавить канал","chName":"Название (напр. Ozon)","chNote":"Комиссия в % от цены с налогом плюс фиксированная плата за продажу. Плагин повышает цену, чтобы прибыль осталась прежней.","withShip":"с доставкой ({s})","q_ship":"Доставка","q_grand":"Итого с доставкой","qSave":"Сохранить расчёт","qSaved":"Расчёт сохранён","qsT":"Сохранённые расчёты","qsCopy":"Копировать","qsPdf":"Печать / PDF","qsDel":"Удалить","qsCsv":"Экспорт в CSV","qsFile":"Сохранено: {p}","qsFileOpen":"Сохранено: {p}. Откройте, чтобы распечатать или сохранить в PDF.","spoolBtn":"Загрузить цены из Spoolman","spoolPick":"Spoolman…","spoolErr":"Не удалось прочитать Spoolman: {e}","spoolOk":"{n} филаментов из Spoolman. Выберите один рядом с каждым филаментом и сохраните.","spoolNone":"В Spoolman нет филаментов с ценой и весом.","cfg_shipping":"Доставка за заказ","cfg_q_name":"Название в расчёте (компания)","cfg_q_footer":"Примечание в конце расчёта","cfg_spoolman":"Адрес Spoolman (необязательно)","qh_item":"Позиция","qh_qty":"Кол-во","qh_unit":"Цена за шт.","qh_vat":"Налог включён ({v}%)","qh_date":"Дата","buf_note":"Коэффициент резерва: добавляет запас к почасовым расходам принтера (износ, обслуживание и электричество) на непредвиденное: поломки, запчасти (хотэнд, сопло, стол, ремни), простой и накопление на следующий принтер. 1,3 = +30%. Материал не меняет. Обычно: 1,2–1,3; проблемный принтер: 1,4–1,5; 1 = без резерва.","eff_note":"Коэффициент потерь материала: умножает только стоимость филамента — за пластик, который G-код не учитывает (остатки катушек, смена филамента, тестовые печати, испорченный филамент). 1,10 = +10% к материалу. Не учитывайте башню и продувку (уже в граммах) и неудачные печати (для них есть свой резерв). Обычно: 1,05–1,15; 1 = без потерь.","sub":"Цена продажи FDM нарезанной пластины","readyT":"Готово к расчёту","readyP":"Нарежьте пластину в OrcaSlicer, и здесь появится её цена продажи с учётом материала, электричества, износа принтера и вашей наценки.","s1":"Выберите принтер и филаменты","s2":"Нарежьте пластину","s3":"Посмотрите цену","reading":"Чтение G-кода","failT":"Не удалось рассчитать","warnT":"Внимание","noPrinter":"Принтер без имени","updated":"Обновлено в {t}","priceT":"Цена за деталь с налогом","mine":"ваша маржа","sim":"маржа этой пластины","profit":"прибыль с детали","margin":"маржа","plate":"вся пластина ({n} шт.)","time":"время пластины","grams":"филамент на деталь","copy1":"деталь на пластине","copyN":"одинаковых деталей на пластине","unit":"себестоимость детали","fils":"Филаменты","color1":"цвет","colorN":"цветов","perKg":"/кг","tower":"Башня очистки и продувки, уже в граммах","costT":"Себестоимость детали","profitT":"Прибыль при выбранной марже","c_material":"Материал","c_energy":"Электричество","c_machine":"Износ принтера","c_reserve":"Резерв оборудования","c_failure":"Резерв на брак","c_labor":"Работа","c_pack":"Упаковка","src_custom":"Задано вами","src_orca":"Профиль OrcaSlicer","src_average":"Средняя цена","src_generic":"Общие значения","cfgT":"Настройки цены","cfgS":"общие, принтер и филаменты","general":"Общие","printer":"Принтер","p_price":"Цена принтера","p_life":"Срок службы","years":"лет","p_uptime":"Годовая загрузка","p_maint":"Годовое обслуживание","p_power":"Средняя мощность","p_buffer":"Коэффициент резерва","f_note":"Цена за кг берётся из поля «Стоимость филамента» профиля OrcaSlicer. Если изменить её здесь, она сохранится для этого филамента.","scan":"Найти уже нарезанные пластины","scanP":"Нарезали до включения плагина? Выберите в OrcaSlicer последнюю нарезанную пластину, и плагин прочитает её G-код без повторной нарезки.","platesCost":"Ваша общая себестоимость","plateN":"Пластина {n}","platesTotal":"Итого {n} пластин, с налогом","notSliced":"{n} ещё не нарезана","notSlicedP":"Нарежьте эту пластину, чтобы увидеть цену. Цена каждой нарезанной пластины сохраняется, пока проект открыт.","customM":"Своя маржа","save":"Сохранить и пересчитать","reset":"Сбросить значения принтера","h":"ч","min":"мин","cfg_intro":"Значения для цены продажи каждой нарезанной пластины. Их также можно изменить во вкладке Print Calculator вместе с профилем затрат каждого принтера и ценами филаментов.","cfg_ro":"Только чтение.","cfg_currency":"Валюта","cfg_search":"Найти валюту (название или код)","cfg_vat":"НДС / налог (%)","cfg_margin":"Маржа (%)","cfg_failure":"Доля брака (%)","cfg_electricity":"Электричество (за кВт·ч)","cfg_rate":"Работа (в час)","cfg_setup":"Подготовка пластины (мин)","cfg_finish":"Обработка детали (мин)","cfg_pack":"Упаковка на деталь","cfg_efficiency":"Коэффициент потерь материала (×)","cfg_language":"Язык","cfg_auto":"Как в OrcaSlicer","py_perm":"OrcaSlicer не разрешил читать G-код. Когда он спросит, выберите «Да» и нарежьте снова.","py_missing":"G-код больше не существует. Нарежьте снова.","py_empty":"В этом G-коде нет времени или граммов, которые может прочитать Print Calculator.","py_fail":"Не удалось рассчитать: {e}","py_unit":"{p} за деталь с налогом","py_plate":" ({n} шт., {t})","py_hint":"Print Calculator: откройте вкладку Print Calculator, чтобы увидеть цену этой пластины.","py_none":"В этом сеансе OrcaSlicer нет нарезанных пластин. Нарежьте пластину или экспортируйте G-код (Ctrl+G), если она уже нарезана.","cfg_minimum":"Минимальная цена за деталь с налогом","cfg_rounding":"Округлять цену за деталь вверх","cfg_round_none":"Не округлять","cfg_round_99":"До ,99","cfg_discount_qty":"Скидка за количество от (деталей на пластине)","cfg_discount_pct":"Скидка за количество (%)","adj_disc":"скидка за количество −{v}%","adj_min":"минимальная цена","adj_round":"округлено","refShort":"ориентир в €","refNote":"Цены с пометкой «ориентир в €» — средние значения в евро, не пересчитанные в {c}. Проверьте их в настройках ниже.","extrasT":"Дополнительно","extrasP":"Магниты, винты, закладные гайки, краска… Выберите, считается ли каждая позиция на каждую деталь или один раз на пластину. Сохраняется для этого заказа.","extraName":"Позиция","extraCost":"Стоимость","extraAdd":"Добавить","c_extras":"Дополнительно","quoteBtn":"Скопировать предложение","quoteAll":"Скопировать предложение по всем пластинам","copied":"Предложение скопировано. Вставьте его в сообщение.","q_title":"Коммерческое предложение","q_qty":"Количество","q_unit":"Цена за деталь","q_total":"Итого","q_time":"Время печати","q_material":"Материал","q_taxinc":"с налогом","byModelT":"Цена по моделям","byModelP":"Материал распределяется по филаменту каждой модели; время, оборудование и подготовка — по её доле филамента. Это оценка.","modelN":"Модель {n}","mdlCost":"себест./шт.","mdlPrice":"цена/шт.","cfg_updates":"Проверять новые версии при открытии вкладки","upd_new":"Доступен Print Calculator {v}. Обновите его в менеджере плагинов OrcaSlicer.","allBtn":"Все пластины","allCost":"Общая себестоимость","priceSetT":"Цена пластины с налогом","profitSet":"прибыль с пластины","gramsSet":"филамент на пластину","partsN":"деталей на пластине","unitSet":"себестоимость пластины","costSetT":"Себестоимость пластины","py_set":"{p} за пластину с налогом ({n} шт.)","jobN":"Заказ {n}","jobNameP":"Название (клиент, проект…)","jobP":"Выберите заказ для каждой пластины. У каждого заказа свой итог, своя маржа и своё предложение.","jobMargin":"Маржа заказа","jobTime":"общее время","jobProfit":"прибыль","sumCost":"Себестоимость","sumTax":"НДС","sumProfit":"Прибыль","setupT":"Сначала подтвердите свои значения","setupP":"Цена появится только после подтверждения настроек ниже: валюта, налог, электричество, цена вашего принтера и цена за кг каждого филамента. Уже заполненные значения — справочные и могут не совпадать с вашими. Затем нажмите кнопку сохранения.","repBtn":"Сообщить о проблеме","repBusy":"Подготовка G-кода…","repDone":"G-код сохранён в {p}. Перетащите этот файл в issue, открывшийся в браузере, и опишите проблему.","repNoG":"У этой пластины нет G-кода. Issue открыт без файла: опишите проблему и, если можете, нарежьте заново.","repLink":"Если браузер не открылся, откройте эту ссылку:","noLabels":"В этом G-коде не указано, сколько деталей на пластине, поэтому цена — за всю пластину. Чтобы считать детали, включите \"Label objects\" в настройках процесса OrcaSlicer (Прочее) и нарежьте заново.","exPart":"за деталь","exPlate":"за пластину"},"uk":{"imp":"Імпортувати нарізаний файл","impBig":"Файл завеликий (максимум 200 МБ).","impFail":"Не вдалося прочитати цей файл.","sliceAllP":"Кілька пластин? Натисніть «Нарізати все» в OrcaSlicer, і всі з'являться тут. Можна також імпортувати .gcode.3mf або .gcode.","chT":"Канали продажу","chP":"Ціна в кожному каналі вже покриває комісію та податок: ваш прибуток скрізь однаковий.","chDirect":"Пряма продаж","chN":"Канал","chFee":"Комісія","chPiece":"За деталь","qChannel":"Ціна для цього каналу","chAdd":"Додати канал","chName":"Назва (напр. Prom)","chNote":"Комісія у % від ціни з податком плюс фіксована плата за продаж. Плагін підвищує ціну, щоб прибуток залишився тим самим.","withShip":"з доставкою ({s})","q_ship":"Доставка","q_grand":"Разом із доставкою","qSave":"Зберегти розрахунок","qSaved":"Розрахунок збережено","qsT":"Збережені розрахунки","qsCopy":"Копіювати","qsPdf":"Друк / PDF","qsDel":"Видалити","qsCsv":"Експорт у CSV","qsFile":"Збережено: {p}","qsFileOpen":"Збережено: {p}. Відкрийте, щоб надрукувати або зберегти в PDF.","spoolBtn":"Завантажити ціни зі Spoolman","spoolPick":"Spoolman…","spoolErr":"Не вдалося прочитати Spoolman: {e}","spoolOk":"{n} філаментів зі Spoolman. Виберіть один біля кожного філаменту й збережіть.","spoolNone":"У Spoolman немає філаментів із ціною та вагою.","cfg_shipping":"Доставка за замовлення","cfg_q_name":"Назва в розрахунку (компанія)","cfg_q_footer":"Примітка в кінці розрахунку","cfg_spoolman":"Адреса Spoolman (необов'язково)","qh_item":"Позиція","qh_qty":"К-сть","qh_unit":"Ціна за шт.","qh_vat":"Податок включено ({v}%)","qh_date":"Дата","buf_note":"Коефіцієнт резерву: додає запас до погодинних витрат принтера (знос, обслуговування й електрика) на непередбачене: поломки, запчастини (хотенд, сопло, стіл, ремені), простій і накопичення на наступний принтер. 1,3 = +30%. Матеріал не змінює. Зазвичай: 1,2–1,3; проблемний принтер: 1,4–1,5; 1 = без резерву.","eff_note":"Коефіцієнт втрат матеріалу: множить лише вартість філаменту — за пластик, який G-код не враховує (залишки котушок, зміна філаменту, тестові друки, зіпсований філамент). 1,10 = +10% до матеріалу. Не враховуйте вежу та продувку (вже в грамах) і невдалі друки (для них є окремий резерв). Зазвичай: 1,05–1,15; 1 = без втрат.","sub":"Ціна продажу FDM нарізаної пластини","readyT":"Готово до розрахунку","readyP":"Наріжте пластину в OrcaSlicer, і тут з'явиться її ціна продажу з урахуванням матеріалу, електрики, зносу принтера та вашої маржі.","s1":"Виберіть принтер і філаменти","s2":"Наріжте пластину","s3":"Подивіться ціну","reading":"Читання G-коду","failT":"Не вдалося розрахувати","warnT":"Увага","noPrinter":"Принтер без назви","updated":"Оновлено о {t}","priceT":"Ціна за деталь з податком","mine":"ваша маржа","sim":"маржа цієї пластини","profit":"прибуток з деталі","margin":"маржа","plate":"уся пластина ({n} шт.)","time":"час пластини","grams":"філамент на деталь","copy1":"деталь на пластині","copyN":"однакових деталей на пластині","unit":"собівартість деталі","fils":"Філаменти","color1":"колір","colorN":"кольорів","perKg":"/кг","tower":"Вежа очищення та продувки, вже у грамах","costT":"Собівартість деталі","profitT":"Прибуток за вибраної маржі","c_material":"Матеріал","c_energy":"Електрика","c_machine":"Знос принтера","c_reserve":"Резерв обладнання","c_failure":"Резерв на брак","c_labor":"Робота","c_pack":"Пакування","src_custom":"Задано вами","src_orca":"Профіль OrcaSlicer","src_average":"Середня ціна","src_generic":"Загальні значення","cfgT":"Налаштування ціни","cfgS":"загальні, принтер і філаменти","general":"Загальні","printer":"Принтер","p_price":"Ціна принтера","p_life":"Строк служби","years":"років","p_uptime":"Річне використання","p_maint":"Річне обслуговування","p_power":"Середня потужність","p_buffer":"Коефіцієнт резерву","f_note":"Ціна за кг береться з поля «Вартість філаменту» профілю OrcaSlicer. Якщо змінити її тут, вона збережеться для цього філаменту.","scan":"Знайти вже нарізані пластини","scanP":"Нарізали до ввімкнення плагіна? Виберіть в OrcaSlicer останню нарізану пластину, і плагін прочитає її G-код без повторного нарізання.","platesCost":"Ваша загальна собівартість","plateN":"Пластина {n}","platesTotal":"Разом {n} пластин, з податком","notSliced":"{n} ще не нарізана","notSlicedP":"Наріжте цю пластину, щоб побачити ціну. Ціна кожної нарізаної пластини зберігається, поки проєкт відкритий.","customM":"Своя маржа","save":"Зберегти й перерахувати","reset":"Скинути значення принтера","h":"год","min":"хв","cfg_intro":"Значення для ціни продажу кожної нарізаної пластини. Їх також можна змінити на вкладці Print Calculator разом із профілем витрат кожного принтера та цінами філаментів.","cfg_ro":"Лише читання.","cfg_currency":"Валюта","cfg_search":"Знайти валюту (назва або код)","cfg_vat":"ПДВ / податок (%)","cfg_margin":"Маржа (%)","cfg_failure":"Частка браку (%)","cfg_electricity":"Електрика (за кВт·год)","cfg_rate":"Робота (за годину)","cfg_setup":"Підготовка пластини (хв)","cfg_finish":"Обробка деталі (хв)","cfg_pack":"Пакування на деталь","cfg_efficiency":"Коефіцієнт втрат матеріалу (×)","cfg_language":"Мова","cfg_auto":"Як в OrcaSlicer","py_perm":"OrcaSlicer не дозволив читати G-код. Коли він запитає, виберіть «Так» і наріжте знову.","py_missing":"G-коду більше немає. Наріжте знову.","py_empty":"У цьому G-коді немає часу чи грамів, які може прочитати Print Calculator.","py_fail":"Не вдалося розрахувати: {e}","py_unit":"{p} за деталь з податком","py_plate":" ({n} шт., {t})","py_hint":"Print Calculator: відкрийте вкладку Print Calculator, щоб побачити ціну цієї пластини.","py_none":"У цьому сеансі OrcaSlicer немає нарізаних пластин. Наріжте пластину або експортуйте G-код (Ctrl+G), якщо вона вже нарізана.","cfg_minimum":"Мінімальна ціна за деталь з податком","cfg_rounding":"Округлювати ціну за деталь угору","cfg_round_none":"Не округлювати","cfg_round_99":"До ,99","cfg_discount_qty":"Знижка за кількість від (деталей на пластині)","cfg_discount_pct":"Знижка за кількість (%)","adj_disc":"знижка за кількість −{v}%","adj_min":"мінімальна ціна","adj_round":"округлено","refShort":"орієнтир у €","refNote":"Ціни з позначкою «орієнтир у €» — середні значення в євро, не перераховані в {c}. Перевірте їх у налаштуваннях нижче.","extrasT":"Додатково","extrasP":"Магніти, гвинти, закладні гайки, фарба… Виберіть, чи рахується кожна позиція на кожну деталь чи один раз на пластину. Зберігається для цього замовлення.","extraName":"Позиція","extraCost":"Вартість","extraAdd":"Додати","c_extras":"Додатково","quoteBtn":"Скопіювати пропозицію","quoteAll":"Скопіювати пропозицію для всіх пластин","copied":"Пропозицію скопійовано. Вставте її в повідомлення.","q_title":"Комерційна пропозиція","q_qty":"Кількість","q_unit":"Ціна за деталь","q_total":"Разом","q_time":"Час друку","q_material":"Матеріал","q_taxinc":"з податком","byModelT":"Ціна за моделлю","byModelP":"Матеріал розподіляється за філаментом кожної моделі; час, обладнання та підготовка — за її часткою філаменту. Це оцінка.","modelN":"Модель {n}","mdlCost":"собів./шт.","mdlPrice":"ціна/шт.","cfg_updates":"Перевіряти нові версії під час відкриття вкладки","upd_new":"Доступний Print Calculator {v}. Оновіть його в менеджері плагінів OrcaSlicer.","allBtn":"Усі пластини","allCost":"Загальна собівартість","priceSetT":"Ціна пластини з податком","profitSet":"прибуток з пластини","gramsSet":"філамент на пластину","partsN":"деталей на пластині","unitSet":"собівартість пластини","costSetT":"Собівартість пластини","py_set":"{p} за пластину з податком ({n} шт.)","jobN":"Замовлення {n}","jobNameP":"Назва (клієнт, проєкт…)","jobP":"Виберіть замовлення для кожної пластини. Кожне замовлення має власний підсумок, маржу та пропозицію.","jobMargin":"Маржа замовлення","jobTime":"загальний час","jobProfit":"прибуток","sumCost":"Собівартість","sumTax":"ПДВ","sumProfit":"Прибуток","setupT":"Спершу підтвердьте свої значення","setupP":"Ціна з'явиться лише після підтвердження налаштувань нижче: валюта, податок, електрика, ціна вашого принтера та ціна за кг кожного філаменту. Уже заповнені значення — довідкові й можуть не збігатися з вашими. Потім натисніть кнопку збереження.","repBtn":"Повідомити про проблему","repBusy":"Підготовка G-коду…","repDone":"G-код збережено в {p}. Перетягніть цей файл у issue, що відкрився в браузері, і опишіть проблему.","repNoG":"У цієї пластини немає G-коду. Issue відкрито без файлу: опишіть проблему і, якщо можете, наріжте знову.","repLink":"Якщо браузер не відкрився, відкрийте це посилання:","noLabels":"У цьому G-коді не вказано, скільки деталей на пластині, тому ціна — за всю пластину. Щоб рахувати деталі, увімкніть \"Label objects\" у налаштуваннях процесу OrcaSlicer (Інше) і наріжте знову.","exPart":"за деталь","exPlate":"за пластину"},"tr":{"imp":"Dilimlenmiş dosya içe aktar","impBig":"Bu dosya çok büyük (en fazla 200 MB).","impFail":"Bu dosya okunamadı.","sliceAllP":"Birden fazla plaka mı var? OrcaSlicer'da \"Tümünü dilimle\"yi kullanın, hepsi burada görünür. Bir .gcode.3mf veya .gcode da içe aktarabilirsiniz.","chT":"Satış kanalları","chP":"Her kanalın fiyatı komisyonu ve vergiyi zaten karşılar: kârınız her yerde aynıdır.","chDirect":"Doğrudan satış","chN":"Kanal","chFee":"Komisyon","chPiece":"Parça başı","qChannel":"Bu kanal için fiyat","chAdd":"Kanal ekle","chName":"Ad (ör. Trendyol)","chNote":"Vergili fiyatın %'si olarak komisyon ve satış başına sabit ücret. Eklenti, aynı kârı korumanız için fiyatı yükseltir.","withShip":"kargo ile ({s})","q_ship":"Kargo","q_grand":"Kargo dahil toplam","qSave":"Teklifi kaydet","qSaved":"Teklif kaydedildi","qsT":"Kayıtlı teklifler","qsCopy":"Kopyala","qsPdf":"Yazdır / PDF","qsDel":"Sil","qsCsv":"CSV olarak dışa aktar","qsFile":"Kaydedildi: {p}","qsFileOpen":"Kaydedildi: {p}. Yazdırmak veya PDF olarak kaydetmek için açın.","spoolBtn":"Fiyatları Spoolman'dan oku","spoolPick":"Spoolman…","spoolErr":"Spoolman okunamadı: {e}","spoolOk":"Spoolman'dan {n} filament. Her filamentin yanında birini seçip kaydedin.","spoolNone":"Spoolman'da fiyatı ve ağırlığı olan filament yok.","cfg_shipping":"Sipariş başına kargo","cfg_q_name":"Teklifteki ad (işletme)","cfg_q_footer":"Teklifin sonundaki not","cfg_spoolman":"Spoolman adresi (isteğe bağlı)","qh_item":"Ürün","qh_qty":"Adet","qh_unit":"Birim fiyat","qh_vat":"Vergi dahil (%{v})","qh_date":"Tarih","buf_note":"Rezerv çarpanı: yazıcının saatlik maliyetlerine (aşınma, bakım ve elektrik) öngörülmeyenler için pay ekler: arızalar, parçalar (hotend, nozul, tabla, kayışlar), duruş süresi ve sonraki yazıcı için birikim. 1,3 = +%30. Malzemeyi değiştirmez. Normal: 1,2 ile 1,3; sorunlu yazıcı: 1,4 ile 1,5; 1 = rezerv yok.","eff_note":"Malzeme kayıp çarpanı: yalnızca filament maliyetini çarpar; G-code'un saymadığı plastik için (makara artıkları, filament değişimleri, test baskıları, bozulmuş filament). 1,10 = malzemeye +%10. Prime kulesini ve temizlemeyi (zaten gramlarda) ve başarısız baskıları (kendi rezervleri var) buraya katmayın. Normal: 1,05 ile 1,15; 1 = kayıp yok.","sub":"Dilimlenen plakanın FDM satış fiyatı","readyT":"Hesaplamaya hazır","readyP":"OrcaSlicer'da bir plaka dilimleyin; malzeme, elektrik, yazıcı aşınması ve kâr marjınızla birlikte satış fiyatı burada görünür.","s1":"Yazıcı ve filament seçin","s2":"Plakayı dilimleyin","s3":"Fiyatı görün","reading":"G-code okunuyor","failT":"Hesaplanamadı","warnT":"Dikkat","noPrinter":"Adsız yazıcı","updated":"{t} itibarıyla güncellendi","priceT":"Vergi dahil parça fiyatı","mine":"sizin marjınız","sim":"bu plakanın marjı","profit":"parça başı kâr","margin":"marj","plate":"tüm plaka ({n} parça)","time":"plaka süresi","grams":"parça başı filament","copy1":"plakada parça","copyN":"plakada aynı parça","unit":"parça başı maliyet","fils":"Filamentler","color1":"renk","colorN":"renk","perKg":"/kg","tower":"Temizleme kulesi ve temizlemeler, gramlara dahil","costT":"Parça başı maliyet","profitT":"Seçilen marjla kâr","c_material":"Malzeme","c_energy":"Elektrik","c_machine":"Yazıcı aşınması","c_reserve":"Makine rezervi","c_failure":"Hata rezervi","c_labor":"İşçilik","c_pack":"Ambalaj","src_custom":"Sizin belirlediğiniz","src_orca":"OrcaSlicer profili","src_average":"Ortalama fiyat","src_generic":"Genel değerler","cfgT":"Fiyat ayarları","cfgS":"genel, yazıcı ve filamentler","general":"Genel","printer":"Yazıcı","p_price":"Yazıcı fiyatı","p_life":"Kullanım ömrü","years":"yıl","p_uptime":"Yıllık kullanım","p_maint":"Yıllık bakım","p_power":"Ortalama güç","p_buffer":"Rezerv katsayısı","f_note":"Kg fiyatı OrcaSlicer profilindeki \"Filament maliyeti\" alanından gelir. Burada değiştirirseniz o filament için kaydedilir.","scan":"Önceden dilimlenmiş plakaları bul","scanP":"Eklentiyi açmadan önce mi dilimlediniz? OrcaSlicer'da son dilimlenen plakayı seçin; eklenti yeniden dilimlemeden G-code'unu okur.","platesCost":"Toplam maliyetiniz","plateN":"Plaka {n}","platesTotal":"{n} plakanın toplamı, vergi dahil","notSliced":"{n} henüz dilimlenmedi","notSlicedP":"Fiyatını görmek için bu plakayı dilimleyin. Dilimlenen her plakanın fiyatı proje açık kaldıkça saklanır.","customM":"Özel marj","save":"Kaydet ve yeniden hesapla","reset":"Yazıcı değerlerini sıfırla","h":"sa","min":"dk","cfg_intro":"Dilimlenen her plakanın satış fiyatında kullanılan değerler. Bunları, her yazıcının maliyet profili ve filament fiyatlarıyla birlikte Print Calculator sekmesinde de değiştirebilirsiniz.","cfg_ro":"Salt okunur.","cfg_currency":"Para birimi","cfg_search":"Para birimi ara (ad veya kod)","cfg_vat":"KDV / vergi (%)","cfg_margin":"Marj (%)","cfg_failure":"Hata oranı (%)","cfg_electricity":"Elektrik (kWh başına)","cfg_rate":"İşçilik (saat başına)","cfg_setup":"Plaka başı hazırlık (dk)","cfg_finish":"Parça başı son işlem (dk)","cfg_pack":"Parça başı ambalaj","cfg_efficiency":"Malzeme kayıp katsayısı (×)","cfg_language":"Dil","cfg_auto":"OrcaSlicer ile aynı","py_perm":"OrcaSlicer G-code'un okunmasına izin vermedi. Sorduğunda Evet'i seçin ve yeniden dilimleyin.","py_missing":"G-code artık yok. Yeniden dilimleyin.","py_empty":"Bu G-code'da Print Calculator'ın okuyabileceği süre veya gram yok.","py_fail":"Hesaplanamadı: {e}","py_unit":"Parça başı {p}, vergi dahil","py_plate":" ({n} parça, {t})","py_hint":"Print Calculator: bu plakanın fiyatını görmek için Print Calculator sekmesini açın.","py_none":"Bu OrcaSlicer oturumunda dilimlenmiş plaka yok. Plakayı dilimleyin veya zaten dilimlendiyse G-code'u dışa aktarın (Ctrl+G).","cfg_minimum":"Parça başı en düşük fiyat, vergi dahil","cfg_rounding":"Parça fiyatını yukarı yuvarla","cfg_round_none":"Yuvarlama","cfg_round_99":",99'a","cfg_discount_qty":"Miktar indirimi başlangıcı (plakadaki parça)","cfg_discount_pct":"Miktar indirimi (%)","adj_disc":"miktar indirimi −%{v}","adj_min":"en düşük fiyat","adj_round":"yuvarlandı","refShort":"€ referans","refNote":"\"€ referans\" işaretli fiyatlar euro cinsinden ortalamalardır, {c} birimine çevrilmemiştir. Aşağıdaki ayarlardan kontrol edin.","extrasT":"Ekstralar","extrasP":"Mıknatıslar, vidalar, dişli insertler, boya… Her ekstranın parça başına mı yoksa plaka başına bir kez mi sayılacağını seçin. Bu iş için saklanır.","extraName":"Kalem","extraCost":"Maliyet","extraAdd":"Ekstra ekle","c_extras":"Ekstralar","quoteBtn":"Teklifi kopyala","quoteAll":"Tüm plakaların teklifini kopyala","copied":"Teklif kopyalandı. Mesajınıza yapıştırın.","q_title":"Teklif","q_qty":"Adet","q_unit":"Parça fiyatı","q_total":"Toplam","q_time":"Baskı süresi","q_material":"Malzeme","q_taxinc":"vergi dahil","byModelT":"Model başı fiyat","byModelP":"Malzeme her modelin filamentine göre; süre, makine ve hazırlık filament payına göre bölünür. Tahmindir.","modelN":"Model {n}","mdlCost":"maliyet/parça","mdlPrice":"fiyat/parça","cfg_updates":"Sekme açılınca yeni sürüm ara","upd_new":"Print Calculator {v} çıktı. OrcaSlicer eklenti yöneticisinden güncelleyin.","allBtn":"Tüm plakalar","allCost":"Toplam maliyet","priceSetT":"Vergi dahil plaka fiyatı","profitSet":"plaka kârı","gramsSet":"plaka filamenti","partsN":"plakadaki parça","unitSet":"plaka maliyeti","costSetT":"Plaka maliyeti","py_set":"Plaka {p}, vergi dahil ({n} parça)","jobN":"İş {n}","jobNameP":"Ad (müşteri, proje…)","jobP":"Her plakanın işini seçin. Her işin kendi toplamı, marjı ve teklifi vardır.","jobMargin":"İş marjı","jobTime":"toplam süre","jobProfit":"kâr","sumCost":"Maliyet","sumTax":"KDV","sumProfit":"Kâr","setupT":"Önce değerlerinizi onaylayın","setupP":"Fiyat, aşağıdaki ayarları onayladıktan sonra görünür: para birimi, vergi, elektrik, yazıcınızın fiyatı ve her filamentin kg fiyatı. Dolu gelen değerler referans değerlerdir ve sizin değerleriniz olmayabilir. Sonra kaydet düğmesine basın.","repBtn":"Sorun bildir","repBusy":"G-code hazırlanıyor…","repDone":"G-code şuraya kaydedildi: {p}. Bu dosyayı tarayıcıda açılan bildirime sürükleyin ve sorunu anlatın.","repNoG":"Bu plakanın G-code'u yok. Bildirim dosyasız açıldı: sorunu anlatın ve mümkünse yeniden dilimleyin.","repLink":"Tarayıcı açılmadıysa bu bağlantıyı açın:","noLabels":"Bu G-code plakada kaç parça olduğunu belirtmiyor, bu yüzden fiyat tüm plaka içindir. Parçaları saymak için OrcaSlicer işlem ayarlarında (Diğer) \"Label objects\" seçeneğini açın ve yeniden dilimleyin.","exPart":"parça başına","exPlate":"plaka başına"},"ja":{"imp":"スライス済みファイルを読み込む","impBig":"ファイルが大きすぎます(最大200 MB)。","impFail":"このファイルを読み込めませんでした。","sliceAllP":"プレートが複数ありますか?OrcaSlicerで「すべてスライス」を使うと、すべてここに表示されます。.gcode.3mfや.gcodeも読み込めます。","chT":"販売チャネル","chP":"各チャネルの価格には手数料と税が含まれているので、利益はどこでも同じです。","chDirect":"直接販売","chN":"チャネル","chFee":"手数料","chPiece":"1個あたり","qChannel":"このチャネルの価格","chAdd":"チャネルを追加","chName":"名前(例:minne)","chNote":"税込価格に対する手数料(%)と販売ごとの固定手数料。同じ利益になるようにプラグインが価格を上げます。","withShip":"送料込み({s})","q_ship":"送料","q_grand":"送料込み合計","qSave":"見積もりを保存","qSaved":"見積もりを保存しました","qsT":"保存した見積もり","qsCopy":"コピー","qsPdf":"印刷 / PDF","qsDel":"削除","qsCsv":"CSVに書き出す","qsFile":"{p} に保存しました","qsFileOpen":"{p} に保存しました。開いて印刷するか PDF に保存してください。","spoolBtn":"Spoolmanから価格を読み込む","spoolPick":"Spoolman…","spoolErr":"Spoolmanを読み込めませんでした:{e}","spoolOk":"Spoolmanのフィラメント {n} 件。各フィラメントの横で選んで保存してください。","spoolNone":"Spoolmanに価格と重量のあるフィラメントがありません。","cfg_shipping":"注文ごとの送料","cfg_q_name":"見積もりの名前(事業者名)","cfg_q_footer":"見積もり末尾のメモ","cfg_spoolman":"Spoolmanのアドレス(任意)","qh_item":"品目","qh_qty":"数量","qh_unit":"単価","qh_vat":"税込({v}%)","qh_date":"日付","buf_note":"予備係数:プリンターの時間あたりの費用(消耗・メンテナンス・電気代)に、予定外の出費のための余裕を加えます:故障、部品(ホットエンド、ノズル、ビルドプレート、ベルト)、停止時間、次のプリンターへの積み立て。1.3 = +30%。材料費は変わりません。目安:1.2〜1.3、トラブルの多いプリンター:1.4〜1.5、1 = 予備なし。","eff_note":"材料ロス係数:フィラメント費用だけに掛けます。G-codeに含まれないプラスチック(スプールの残り、フィラメント交換、テスト印刷、劣化したフィラメント)の分です。1.10 = 材料費 +10%。プライムタワーとパージ(グラムに含まれています)や失敗した印刷(別の予備費があります)はここに含めないでください。目安:1.05〜1.15、1 = ロスなし。","sub":"スライスしたプレートのFDM販売価格","readyT":"計算の準備ができました","readyP":"OrcaSlicerでプレートをスライスすると、材料・電気代・プリンター摩耗・利益率を含む販売価格がここに表示されます。","s1":"プリンターとフィラメントを選ぶ","s2":"プレートをスライス","s3":"価格を見る","reading":"G-codeを読み込み中","failT":"計算できませんでした","warnT":"注意","noPrinter":"名前のないプリンター","updated":"{t} に更新","priceT":"1個あたりの価格(税込)","mine":"あなたの利益率","sim":"このプレートの利益率","profit":"1個あたりの利益","margin":"利益率","plate":"プレート全体({n}個)","time":"プレートの時間","grams":"1個あたりのフィラメント","copy1":"個(プレート上)","copyN":"個の同じ部品(プレート上)","unit":"1個あたりのコスト","fils":"フィラメント","color1":"色","colorN":"色","perKg":"/kg","tower":"プライムタワーとパージ(グラムに含まれます)","costT":"1個あたりのコスト","profitT":"選んだ利益率での利益","c_material":"材料","c_energy":"電気代","c_machine":"プリンターの摩耗","c_reserve":"機械の予備費","c_failure":"失敗の予備費","c_labor":"作業費","c_pack":"梱包","src_custom":"自分で設定","src_orca":"OrcaSlicerのプロファイル","src_average":"平均価格","src_generic":"一般的な値","cfgT":"価格設定","cfgS":"一般、プリンター、フィラメント","general":"一般","printer":"プリンター","p_price":"プリンター価格","p_life":"耐用年数","years":"年","p_uptime":"年間稼働率","p_maint":"年間保守費","p_power":"平均消費電力","p_buffer":"予備係数","f_note":"kg単価はOrcaSlicerプロファイルの「フィラメントコスト」から取得します。ここで変更すると、そのフィラメントに保存されます。","scan":"スライス済みのプレートを探す","scanP":"プラグインを有効にする前にスライスしましたか?OrcaSlicerで最後にスライスしたプレートを選ぶと、再スライスせずにそのG-codeを読み込みます。","platesCost":"合計コスト","plateN":"プレート {n}","platesTotal":"{n} プレートの合計(税込)","notSliced":"{n} はまだスライスされていません","notSlicedP":"価格を見るにはこのプレートをスライスしてください。スライスした各プレートの価格はプロジェクトを開いている間保持されます。","customM":"カスタム利益率","save":"保存して再計算","reset":"プリンターの値をリセット","h":"時間","min":"分","cfg_intro":"スライスした各プレートの販売価格に使う値です。Print Calculatorタブでも、各プリンターのコストプロファイルやフィラメント価格とあわせて変更できます。","cfg_ro":"読み取り専用。","cfg_currency":"通貨","cfg_search":"通貨を検索(名前またはコード)","cfg_vat":"消費税・付加価値税 (%)","cfg_margin":"利益率 (%)","cfg_failure":"失敗率 (%)","cfg_electricity":"電気代 (kWhあたり)","cfg_rate":"作業費 (1時間あたり)","cfg_setup":"プレートごとの準備 (分)","cfg_finish":"1個あたりの仕上げ (分)","cfg_pack":"1個あたりの梱包","cfg_efficiency":"材料ロス係数 (×)","cfg_language":"言語","cfg_auto":"OrcaSlicerと同じ","py_perm":"OrcaSlicerがG-codeの読み込みを許可しませんでした。確認されたら「はい」を選び、もう一度スライスしてください。","py_missing":"G-codeが存在しません。もう一度スライスしてください。","py_empty":"このG-codeにはPrint Calculatorが読める時間やグラムがありません。","py_fail":"計算できませんでした: {e}","py_unit":"1個あたり {p}(税込)","py_plate":"({n}個、{t})","py_hint":"Print Calculator:このプレートの価格はPrint Calculatorタブで確認できます。","py_none":"このOrcaSlicerセッションにスライス済みのプレートはありません。プレートをスライスするか、スライス済みならG-codeをエクスポート (Ctrl+G) してください。","cfg_minimum":"1個あたりの最低価格(税込)","cfg_rounding":"1個あたりの価格を切り上げ","cfg_round_none":"切り上げない","cfg_round_99":".99 に","cfg_discount_qty":"数量割引の開始数(プレート上の個数)","cfg_discount_pct":"数量割引 (%)","adj_disc":"数量割引 −{v}%","adj_min":"最低価格","adj_round":"切り上げ済み","refShort":"€の参考値","refNote":"「€の参考値」と表示された価格はユーロでの平均値で、{c} に換算されていません。下の設定で確認してください。","extrasT":"追加費用","extrasP":"マグネット、ネジ、インサート、塗料など。各項目を部品ごとに数えるか、プレートごとに1回数えるかを選べます。このジョブ用に保存されます。","extraName":"項目","extraCost":"費用","extraAdd":"追加","c_extras":"追加費用","quoteBtn":"見積もりをコピー","quoteAll":"全プレートの見積もりをコピー","copied":"見積もりをコピーしました。メッセージに貼り付けてください。","q_title":"見積もり","q_qty":"数量","q_unit":"1個あたりの価格","q_total":"合計","q_time":"印刷時間","q_material":"材料","q_taxinc":"税込","byModelT":"モデル別の価格","byModelP":"材料は各モデルのフィラメントで、時間・機械・準備はフィラメントの割合で配分します。概算です。","modelN":"モデル {n}","mdlCost":"原価/個","mdlPrice":"価格/個","cfg_updates":"タブを開いたときに新しいバージョンを確認","upd_new":"Print Calculator {v} が利用できます。OrcaSlicerのプラグイン管理で更新してください。","allBtn":"すべてのプレート","allCost":"合計コスト","priceSetT":"プレートの価格(税込)","profitSet":"プレートの利益","gramsSet":"プレートのフィラメント","partsN":"個の部品(プレート上)","unitSet":"プレートのコスト","costSetT":"プレートのコスト","py_set":"プレート {p}(税込、{n}個)","jobN":"ジョブ {n}","jobNameP":"名前(顧客、プロジェクト…)","jobP":"各プレートのジョブを選んでください。ジョブごとに合計、利益率、見積もりがあります。","jobMargin":"ジョブの利益率","jobTime":"合計時間","jobProfit":"利益","sumCost":"コスト","sumTax":"消費税・付加価値税","sumProfit":"利益","setupT":"まず自分の値を確認してください","setupP":"価格は、下の設定(通貨、税、電気代、プリンターの価格、各フィラメントの1kgあたりの価格)を確認した後に表示されます。入力済みの値は参考値で、あなたの値とは異なる場合があります。確認したら保存ボタンを押してください。","repBtn":"問題を報告","repBusy":"G-codeを準備中…","repDone":"G-codeを {p} に保存しました。ブラウザで開いたissueにこのファイルをドラッグし、問題を説明してください。","repNoG":"このプレートにはG-codeがありません。ファイルなしでissueを開きました。問題を説明し、可能ならスライスし直してください。","repLink":"ブラウザが開かない場合は、このリンクを開いてください:","noLabels":"このG-codeにはプレート上の部品数が含まれていないため、価格はプレート全体の価格です。部品を数えるには、OrcaSlicerのプロセス設定(その他)で「Label objects」をオンにして、もう一度スライスしてください。","exPart":"部品ごと","exPlate":"プレートごと"},"ko":{"imp":"슬라이스된 파일 가져오기","impBig":"파일이 너무 큽니다(최대 200 MB).","impFail":"이 파일을 읽을 수 없습니다.","sliceAllP":"플레이트가 여러 개인가요? OrcaSlicer에서 \"모두 슬라이스\"를 사용하면 모두 여기에 표시됩니다. .gcode.3mf 또는 .gcode를 가져올 수도 있습니다.","chT":"판매 채널","chP":"각 채널의 가격에는 수수료와 세금이 이미 포함되어 있어 이익은 어디서나 같습니다.","chDirect":"직접 판매","chN":"채널","chFee":"수수료","chPiece":"개당","qChannel":"이 채널의 가격","chAdd":"채널 추가","chName":"이름(예: 스마트스토어)","chNote":"세금 포함 가격의 % 수수료와 판매당 고정 수수료. 같은 이익이 남도록 플러그인이 가격을 올립니다.","withShip":"배송비 포함({s})","q_ship":"배송비","q_grand":"배송비 포함 합계","qSave":"견적 저장","qSaved":"견적을 저장했습니다","qsT":"저장된 견적","qsCopy":"복사","qsPdf":"인쇄 / PDF","qsDel":"삭제","qsCsv":"CSV 내보내기","qsFile":"{p}에 저장했습니다","qsFileOpen":"{p}에 저장했습니다. 열어서 인쇄하거나 PDF로 저장하세요.","spoolBtn":"Spoolman에서 가격 읽기","spoolPick":"Spoolman…","spoolErr":"Spoolman을 읽을 수 없습니다: {e}","spoolOk":"Spoolman 필라멘트 {n}개. 각 필라멘트 옆에서 하나를 골라 저장하세요.","spoolNone":"Spoolman에 가격과 무게가 있는 필라멘트가 없습니다.","cfg_shipping":"주문당 배송비","cfg_q_name":"견적서 이름(사업자)","cfg_q_footer":"견적서 끝 메모","cfg_spoolman":"Spoolman 주소(선택)","qh_item":"품목","qh_qty":"수량","qh_unit":"단가","qh_vat":"세금 포함({v}%)","qh_date":"날짜","buf_note":"예비 계수: 프린터의 시간당 비용(마모, 유지보수, 전기)에 예상치 못한 지출을 위한 여유를 더합니다: 고장, 부품(핫엔드, 노즐, 빌드 플레이트, 벨트), 가동 중단, 다음 프린터를 위한 적립. 1.3 = +30%. 재료비는 바뀌지 않습니다. 보통: 1.2~1.3, 문제가 많은 프린터: 1.4~1.5, 1 = 예비 없음.","eff_note":"재료 손실 계수: 필라멘트 비용에만 곱합니다. G-code가 세지 않는 플라스틱(스풀 끝부분, 필라멘트 교체, 테스트 출력, 상한 필라멘트)을 위한 것입니다. 1.10 = 재료비 +10%. 프라임 타워와 퍼지(이미 그램에 포함)나 실패한 출력(별도 예비비가 있음)은 넣지 마세요. 보통: 1.05~1.15, 1 = 손실 없음.","sub":"슬라이스한 플레이트의 FDM 판매 가격","readyT":"계산 준비 완료","readyP":"OrcaSlicer에서 플레이트를 슬라이스하면 재료, 전기, 프린터 마모, 마진을 포함한 판매 가격이 여기에 표시됩니다.","s1":"프린터와 필라멘트 선택","s2":"플레이트 슬라이스","s3":"가격 확인","reading":"G-code 읽는 중","failT":"계산할 수 없음","warnT":"주의","noPrinter":"이름 없는 프린터","updated":"{t}에 업데이트됨","priceT":"부품당 가격(세금 포함)","mine":"내 마진","sim":"이 플레이트의 마진","profit":"부품당 이익","margin":"마진","plate":"플레이트 전체({n}개)","time":"플레이트 시간","grams":"부품당 필라멘트","copy1":"개 부품(플레이트)","copyN":"개 동일 부품(플레이트)","unit":"부품당 원가","fils":"필라멘트","color1":"색상","colorN":"색상","perKg":"/kg","tower":"프라임 타워와 퍼지(그램에 포함됨)","costT":"부품당 원가","profitT":"선택한 마진의 이익","c_material":"재료","c_energy":"전기","c_machine":"프린터 마모","c_reserve":"장비 예비비","c_failure":"실패 예비비","c_labor":"인건비","c_pack":"포장","src_custom":"직접 설정","src_orca":"OrcaSlicer 프로필","src_average":"평균 가격","src_generic":"일반 값","cfgT":"가격 설정","cfgS":"일반, 프린터, 필라멘트","general":"일반","printer":"프린터","p_price":"프린터 가격","p_life":"사용 수명","years":"년","p_uptime":"연간 사용률","p_maint":"연간 유지보수","p_power":"평균 전력","p_buffer":"예비 계수","f_note":"kg당 가격은 OrcaSlicer 프로필의 \"필라멘트 비용\" 항목에서 가져옵니다. 여기서 바꾸면 해당 필라멘트에 저장됩니다.","scan":"이미 슬라이스한 플레이트 찾기","scanP":"플러그인을 켜기 전에 슬라이스했나요? OrcaSlicer에서 마지막으로 슬라이스한 플레이트를 선택하면 다시 슬라이스하지 않고 G-code를 읽습니다.","platesCost":"총 원가","plateN":"플레이트 {n}","platesTotal":"플레이트 {n}개 합계(세금 포함)","notSliced":"{n}는 아직 슬라이스되지 않았습니다","notSlicedP":"가격을 보려면 이 플레이트를 슬라이스하세요. 슬라이스한 각 플레이트의 가격은 프로젝트가 열려 있는 동안 유지됩니다.","customM":"사용자 마진","save":"저장 후 다시 계산","reset":"프린터 값 초기화","h":"시간","min":"분","cfg_intro":"슬라이스한 각 플레이트의 판매 가격에 쓰이는 값입니다. Print Calculator 탭에서도 각 프린터의 원가 프로필, 필라멘트 가격과 함께 바꿀 수 있습니다.","cfg_ro":"읽기 전용.","cfg_currency":"통화","cfg_search":"통화 검색(이름 또는 코드)","cfg_vat":"부가세 / 세금 (%)","cfg_margin":"마진 (%)","cfg_failure":"실패율 (%)","cfg_electricity":"전기 (kWh당)","cfg_rate":"인건비 (시간당)","cfg_setup":"플레이트당 준비 (분)","cfg_finish":"부품당 후가공 (분)","cfg_pack":"부품당 포장","cfg_efficiency":"재료 손실 계수 (×)","cfg_language":"언어","cfg_auto":"OrcaSlicer와 동일","py_perm":"OrcaSlicer가 G-code 읽기를 허용하지 않았습니다. 물어보면 예를 선택하고 다시 슬라이스하세요.","py_missing":"G-code가 더 이상 없습니다. 다시 슬라이스하세요.","py_empty":"이 G-code에는 Print Calculator가 읽을 수 있는 시간이나 그램이 없습니다.","py_fail":"계산할 수 없음: {e}","py_unit":"부품당 {p}(세금 포함)","py_plate":" ({n}개, {t})","py_hint":"Print Calculator: 이 플레이트의 가격은 Print Calculator 탭에서 확인하세요.","py_none":"이 OrcaSlicer 세션에 슬라이스한 플레이트가 없습니다. 플레이트를 슬라이스하거나, 이미 슬라이스했다면 G-code를 내보내세요(Ctrl+G).","cfg_minimum":"부품당 최저 가격(세금 포함)","cfg_rounding":"부품당 가격 올림","cfg_round_none":"올리지 않음","cfg_round_99":".99로","cfg_discount_qty":"수량 할인 시작(플레이트 위 부품 수)","cfg_discount_pct":"수량 할인 (%)","adj_disc":"수량 할인 −{v}%","adj_min":"최저 가격","adj_round":"올림 적용","refShort":"€ 참고값","refNote":"\"€ 참고값\"으로 표시된 가격은 유로 기준 평균값이며 {c}(으)로 환산되지 않았습니다. 아래 설정에서 확인하세요.","extrasT":"추가 비용","extrasP":"자석, 나사, 인서트, 페인트… 각 항목을 부품마다 셀지 플레이트당 한 번 셀지 고르세요. 이 작업에 저장됩니다.","extraName":"항목","extraCost":"비용","extraAdd":"추가","c_extras":"추가 비용","quoteBtn":"견적 복사","quoteAll":"모든 플레이트 견적 복사","copied":"견적을 복사했습니다. 메시지에 붙여 넣으세요.","q_title":"견적","q_qty":"수량","q_unit":"부품당 가격","q_total":"합계","q_time":"출력 시간","q_material":"재료","q_taxinc":"세금 포함","byModelT":"모델별 가격","byModelP":"재료는 각 모델의 필라멘트로, 시간·장비·준비 비용은 필라멘트 비율로 나눕니다. 추정치입니다.","modelN":"모델 {n}","mdlCost":"원가/개","mdlPrice":"가격/개","cfg_updates":"탭을 열 때 새 버전 확인","upd_new":"Print Calculator {v}을(를) 사용할 수 있습니다. OrcaSlicer 플러그인 관리자에서 업데이트하세요.","allBtn":"모든 플레이트","allCost":"총 원가","priceSetT":"플레이트 가격(세금 포함)","profitSet":"플레이트 이익","gramsSet":"플레이트 필라멘트","partsN":"개 부품(플레이트)","unitSet":"플레이트 원가","costSetT":"플레이트 원가","py_set":"플레이트 {p}(세금 포함, {n}개)","jobN":"작업 {n}","jobNameP":"이름(고객, 프로젝트…)","jobP":"각 플레이트의 작업을 선택하세요. 작업마다 합계, 마진, 견적이 따로 있습니다.","jobMargin":"작업 마진","jobTime":"총 시간","jobProfit":"이익","sumCost":"원가","sumTax":"부가세","sumProfit":"이익","setupT":"먼저 내 값을 확인하세요","setupP":"가격은 아래 설정(통화, 세금, 전기 요금, 프린터 가격, 필라멘트별 kg당 가격)을 확인한 뒤에 표시됩니다. 이미 입력된 값은 참고값이며 실제 값과 다를 수 있습니다. 확인한 뒤 저장 버튼을 누르세요.","repBtn":"문제 신고","repBusy":"G-code 준비 중…","repDone":"G-code를 {p}에 저장했습니다. 브라우저에서 열린 이슈에 이 파일을 끌어다 놓고 문제를 설명하세요.","repNoG":"이 플레이트에는 G-code가 없습니다. 파일 없이 이슈가 열렸습니다. 문제를 설명하고 가능하면 다시 슬라이스하세요.","repLink":"브라우저가 열리지 않았다면 이 링크를 여세요:","noLabels":"이 G-code에는 플레이트의 부품 수가 없어 가격은 플레이트 전체 기준입니다. 부품을 세려면 OrcaSlicer 프로세스 설정(기타)에서 \"Label objects\"를 켜고 다시 슬라이스하세요.","exPart":"부품당","exPlate":"플레이트당"},"zh_CN":{"imp":"导入已切片文件","impBig":"文件太大（最大 200 MB）。","impFail":"无法读取此文件。","sliceAllP":"有多个打印板？在 OrcaSlicer 中使用“全部切片”，所有打印板都会显示在这里。也可以导入 .gcode.3mf 或 .gcode。","chT":"销售渠道","chP":"每个渠道的价格已包含平台费用和税费：在哪里卖利润都一样。","chDirect":"直接销售","chN":"渠道","chFee":"费用","chPiece":"每件","qChannel":"此渠道的价格","chAdd":"添加渠道","chName":"名称（如 淘宝）","chNote":"按含税价格收取的百分比费用，加上每笔销售的固定费用。插件会提高价格，让你的利润保持不变。","withShip":"含运费（{s}）","q_ship":"运费","q_grand":"含运费合计","qSave":"保存报价","qSaved":"报价已保存","qsT":"已保存的报价","qsCopy":"复制","qsPdf":"打印 / PDF","qsDel":"删除","qsCsv":"导出 CSV","qsFile":"已保存到 {p}","qsFileOpen":"已保存到 {p}。打开它即可打印或另存为 PDF。","spoolBtn":"从 Spoolman 读取价格","spoolPick":"Spoolman…","spoolErr":"无法读取 Spoolman：{e}","spoolOk":"Spoolman 中有 {n} 种耗材。在每种耗材旁选择一个并保存。","spoolNone":"Spoolman 中没有带价格和重量的耗材。","cfg_shipping":"每单运费","cfg_q_name":"报价单名称（商家）","cfg_q_footer":"报价单末尾备注","cfg_spoolman":"Spoolman 地址（可选）","qh_item":"项目","qh_qty":"数量","qh_unit":"单价","qh_vat":"含税（{v}%）","qh_date":"日期","buf_note":"储备系数：在打印机的每小时成本（折旧、维护和电费）上加一笔余量，用于意外支出：故障、配件（热端、喷嘴、打印板、皮带）、停机时间，以及为下一台打印机攒钱。1.3 = +30%。不影响材料。常用：1.2 到 1.3；问题较多的打印机：1.4 到 1.5；1 = 无储备。","eff_note":"材料损耗系数：只乘以耗材成本，用于 G-code 没有计入的塑料（料盘余料、换料、测试打印、受潮报废的耗材）。1.10 = 材料 +10%。不要把擦料塔和冲刷算进来（已包含在克数中），也不要算打印失败（有单独的储备）。常用：1.05 到 1.15；1 = 无损耗。","sub":"已切片打印板的 FDM 售价","readyT":"准备计算","readyP":"在 OrcaSlicer 中切片一块打印板，售价会显示在这里，包含材料、电费、打印机损耗和你的利润率。","s1":"选择打印机和耗材","s2":"切片打印板","s3":"查看价格","reading":"正在读取 G-code","failT":"无法计算","warnT":"注意","noPrinter":"未命名打印机","updated":"{t} 更新","priceT":"每件含税价格","mine":"你的利润率","sim":"此打印板的利润率","profit":"每件利润","margin":"利润率","plate":"整块打印板（{n} 件）","time":"打印板时间","grams":"每件耗材","copy1":"件（打印板上）","copyN":"件相同零件（打印板上）","unit":"每件成本","fils":"耗材","color1":"种颜色","colorN":"种颜色","perKg":"/kg","tower":"擦拭塔和冲刷，已计入克数","costT":"每件成本","profitT":"所选利润率下的利润","c_material":"材料","c_energy":"电费","c_machine":"打印机损耗","c_reserve":"设备储备","c_failure":"失败储备","c_labor":"人工","c_pack":"包装","src_custom":"你设置的","src_orca":"OrcaSlicer 配置","src_average":"平均价格","src_generic":"通用数值","cfgT":"价格设置","cfgS":"通用、打印机和耗材","general":"通用","printer":"打印机","p_price":"打印机价格","p_life":"使用寿命","years":"年","p_uptime":"年使用率","p_maint":"年维护费","p_power":"平均功率","p_buffer":"储备系数","f_note":"每公斤价格来自 OrcaSlicer 配置中的“耗材成本”。在这里修改后，会为该耗材保存。","scan":"查找已切片的打印板","scanP":"在启用插件前就切片了？在 OrcaSlicer 中选择最后切片的打印板，插件会读取它的 G-code，无需重新切片。","platesCost":"你的总成本","plateN":"打印板 {n}","platesTotal":"{n} 块打印板合计（含税）","notSliced":"{n} 尚未切片","notSlicedP":"切片此打印板即可查看价格。项目打开期间，每块已切片打印板的价格都会保留。","customM":"自定义利润率","save":"保存并重新计算","reset":"重置打印机数值","h":"小时","min":"分钟","cfg_intro":"用于每块已切片打印板售价的数值。你也可以在 Print Calculator 标签页中修改它们，以及每台打印机的成本配置和耗材价格。","cfg_ro":"只读。","cfg_currency":"货币","cfg_search":"搜索货币（名称或代码）","cfg_vat":"增值税 / 税率 (%)","cfg_margin":"利润率 (%)","cfg_failure":"失败率 (%)","cfg_electricity":"电费（每 kWh）","cfg_rate":"人工（每小时）","cfg_setup":"每块打印板准备 (分钟)","cfg_finish":"每件后处理 (分钟)","cfg_pack":"每件包装","cfg_efficiency":"材料损耗系数 (×)","cfg_language":"语言","cfg_auto":"与 OrcaSlicer 相同","py_perm":"OrcaSlicer 不允许读取 G-code。询问时请选择“是”，然后重新切片。","py_missing":"G-code 已不存在。请重新切片。","py_empty":"此 G-code 中没有 Print Calculator 能读取的时间或克数。","py_fail":"无法计算：{e}","py_unit":"每件 {p}（含税）","py_plate":"（{n} 件，{t}）","py_hint":"Print Calculator：打开 Print Calculator 标签页查看此打印板的价格。","py_none":"此 OrcaSlicer 会话中没有已切片的打印板。请切片打印板；如果已切片，请导出 G-code (Ctrl+G)。","cfg_minimum":"每件最低价格（含税）","cfg_rounding":"每件价格向上取整","cfg_round_none":"不取整","cfg_round_99":"到 .99","cfg_discount_qty":"数量折扣起始（打印板上的件数）","cfg_discount_pct":"数量折扣 (%)","adj_disc":"数量折扣 −{v}%","adj_min":"最低价格","adj_round":"已取整","refShort":"€ 参考价","refNote":"标为“€ 参考价”的价格是欧元平均值，未换算为 {c}。请在下方设置中核对。","extrasT":"附加费用","extrasP":"磁铁、螺丝、嵌件、油漆……选择每项按每个零件计算，还是每块打印板只算一次。会为此作业保存。","extraName":"项目","extraCost":"费用","extraAdd":"添加额外费用","c_extras":"额外费用","quoteBtn":"复制报价","quoteAll":"复制所有打印板的报价","copied":"报价已复制。粘贴到你的消息中即可。","q_title":"报价","q_qty":"数量","q_unit":"每件价格","q_total":"合计","q_time":"打印时间","q_material":"材料","q_taxinc":"含税","byModelT":"按模型的价格","byModelP":"材料按每个模型的耗材分摊；时间、设备和准备按其耗材占比分摊。仅为估算。","modelN":"模型 {n}","mdlCost":"成本/件","mdlPrice":"价格/件","cfg_updates":"打开标签页时检查新版本","upd_new":"Print Calculator {v} 已发布。请在 OrcaSlicer 的插件管理器中更新。","allBtn":"所有打印板","allCost":"总成本","priceSetT":"整板含税价格","profitSet":"整板利润","gramsSet":"整板耗材","partsN":"件零件（打印板上）","unitSet":"整板成本","costSetT":"整板成本","py_set":"整板 {p}（含税，{n} 件）","jobN":"任务 {n}","jobNameP":"名称（客户、项目…）","jobP":"为每块打印板选择任务。每个任务有自己的合计、利润率和报价。","jobMargin":"任务利润率","jobTime":"总时间","jobProfit":"利润","sumCost":"成本","sumTax":"增值税","sumProfit":"利润","setupT":"请先确认你的数值","setupP":"确认下方设置后才会显示价格：货币、税率、电费、打印机价格以及每种耗材的每公斤价格。已填入的是参考值，可能与你的实际情况不同。确认后请点击保存按钮。","repBtn":"报告问题","repBusy":"正在准备 G-code…","repDone":"G-code 已保存到 {p}。请把这个文件拖到浏览器中打开的 issue 里，并描述问题。","repNoG":"这块打印板没有 G-code。issue 已在没有文件的情况下打开：请描述问题，如果可以请重新切片。","repLink":"如果浏览器没有打开，请打开这个链接：","noLabels":"此 G-code 没有写明打印板上有几个零件，所以价格是整块打印板的价格。要统计零件，请在 OrcaSlicer 工艺设置（其他）中开启“Label objects”，然后重新切片。","exPart":"每件","exPlate":"每板"},"zh_TW":{"imp":"匯入已切片檔案","impBig":"檔案太大（最大 200 MB）。","impFail":"無法讀取此檔案。","sliceAllP":"有多個列印板？在 OrcaSlicer 中使用「全部切片」，所有列印板都會顯示在這裡。也可以匯入 .gcode.3mf 或 .gcode。","chT":"銷售通路","chP":"每個通路的價格已包含平台費用和稅金：在哪裡賣利潤都一樣。","chDirect":"直接銷售","chN":"通路","chFee":"費用","chPiece":"每件","qChannel":"此通路的價格","chAdd":"新增通路","chName":"名稱（如 蝦皮）","chNote":"按含稅價格收取的百分比費用，加上每筆銷售的固定費用。外掛會提高價格，讓你的利潤保持不變。","withShip":"含運費（{s}）","q_ship":"運費","q_grand":"含運費合計","qSave":"儲存報價","qSaved":"報價已儲存","qsT":"已儲存的報價","qsCopy":"複製","qsPdf":"列印 / PDF","qsDel":"刪除","qsCsv":"匯出 CSV","qsFile":"已儲存至 {p}","qsFileOpen":"已儲存至 {p}。開啟即可列印或另存為 PDF。","spoolBtn":"從 Spoolman 讀取價格","spoolPick":"Spoolman…","spoolErr":"無法讀取 Spoolman：{e}","spoolOk":"Spoolman 中有 {n} 種線材。在每種線材旁選擇一個並儲存。","spoolNone":"Spoolman 中沒有帶價格和重量的線材。","cfg_shipping":"每筆訂單運費","cfg_q_name":"報價單名稱（商家）","cfg_q_footer":"報價單末尾備註","cfg_spoolman":"Spoolman 位址（選填）","qh_item":"項目","qh_qty":"數量","qh_unit":"單價","qh_vat":"含稅（{v}%）","qh_date":"日期","buf_note":"儲備係數：在印表機的每小時成本（折舊、維護和電費）上加一筆餘量，用於意外支出：故障、零件（熱端、噴嘴、列印板、皮帶）、停機時間，以及為下一台印表機存錢。1.3 = +30%。不影響材料。常用：1.2 到 1.3；問題較多的印表機：1.4 到 1.5；1 = 無儲備。","eff_note":"材料損耗係數：只乘以線材成本，用於 G-code 沒有計入的塑料（料盤餘料、換料、測試列印、受潮報廢的線材）。1.10 = 材料 +10%。不要把擦料塔和沖刷算進來（已包含在克數中），也不要算列印失敗（有單獨的儲備）。常用：1.05 到 1.15；1 = 無損耗。","sub":"已切片列印板的 FDM 售價","readyT":"準備計算","readyP":"在 OrcaSlicer 中切片一塊列印板，售價會顯示在這裡，包含材料、電費、印表機損耗和你的利潤率。","s1":"選擇印表機和線材","s2":"切片列印板","s3":"查看價格","reading":"正在讀取 G-code","failT":"無法計算","warnT":"注意","noPrinter":"未命名印表機","updated":"{t} 更新","priceT":"每件含稅價格","mine":"你的利潤率","sim":"此列印板的利潤率","profit":"每件利潤","margin":"利潤率","plate":"整塊列印板（{n} 件）","time":"列印板時間","grams":"每件線材","copy1":"件（列印板上）","copyN":"件相同零件（列印板上）","unit":"每件成本","fils":"線材","color1":"種顏色","colorN":"種顏色","perKg":"/kg","tower":"擦拭塔和沖刷，已計入克數","costT":"每件成本","profitT":"所選利潤率下的利潤","c_material":"材料","c_energy":"電費","c_machine":"印表機損耗","c_reserve":"設備儲備","c_failure":"失敗儲備","c_labor":"人工","c_pack":"包裝","src_custom":"你設定的","src_orca":"OrcaSlicer 設定檔","src_average":"平均價格","src_generic":"通用數值","cfgT":"價格設定","cfgS":"一般、印表機和線材","general":"一般","printer":"印表機","p_price":"印表機價格","p_life":"使用壽命","years":"年","p_uptime":"年使用率","p_maint":"年維護費","p_power":"平均功率","p_buffer":"儲備係數","f_note":"每公斤價格來自 OrcaSlicer 設定檔中的「線材成本」。在這裡修改後，會為該線材儲存。","scan":"尋找已切片的列印板","scanP":"在啟用外掛前就切片了？在 OrcaSlicer 中選擇最後切片的列印板，外掛會讀取它的 G-code，無需重新切片。","platesCost":"你的總成本","plateN":"列印板 {n}","platesTotal":"{n} 塊列印板合計（含稅）","notSliced":"{n} 尚未切片","notSlicedP":"切片此列印板即可查看價格。專案開啟期間，每塊已切片列印板的價格都會保留。","customM":"自訂利潤率","save":"儲存並重新計算","reset":"重設印表機數值","h":"小時","min":"分鐘","cfg_intro":"用於每塊已切片列印板售價的數值。你也可以在 Print Calculator 分頁中修改它們，以及每台印表機的成本設定和線材價格。","cfg_ro":"唯讀。","cfg_currency":"貨幣","cfg_search":"搜尋貨幣（名稱或代碼）","cfg_vat":"營業稅 / 稅率 (%)","cfg_margin":"利潤率 (%)","cfg_failure":"失敗率 (%)","cfg_electricity":"電費（每 kWh）","cfg_rate":"人工（每小時）","cfg_setup":"每塊列印板準備 (分鐘)","cfg_finish":"每件後處理 (分鐘)","cfg_pack":"每件包裝","cfg_efficiency":"材料損耗係數 (×)","cfg_language":"語言","cfg_auto":"與 OrcaSlicer 相同","py_perm":"OrcaSlicer 不允許讀取 G-code。詢問時請選擇「是」，然後重新切片。","py_missing":"G-code 已不存在。請重新切片。","py_empty":"此 G-code 中沒有 Print Calculator 能讀取的時間或克數。","py_fail":"無法計算：{e}","py_unit":"每件 {p}（含稅）","py_plate":"（{n} 件，{t}）","py_hint":"Print Calculator：開啟 Print Calculator 分頁查看此列印板的價格。","py_none":"此 OrcaSlicer 工作階段中沒有已切片的列印板。請切片列印板；若已切片，請匯出 G-code (Ctrl+G)。","cfg_minimum":"每件最低價格（含稅）","cfg_rounding":"每件價格無條件進位","cfg_round_none":"不進位","cfg_round_99":"到 .99","cfg_discount_qty":"數量折扣起始（列印板上的件數）","cfg_discount_pct":"數量折扣 (%)","adj_disc":"數量折扣 −{v}%","adj_min":"最低價格","adj_round":"已進位","refShort":"€ 參考價","refNote":"標示為「€ 參考價」的價格是歐元平均值，未換算為 {c}。請在下方設定中確認。","extrasT":"附加費用","extrasP":"磁鐵、螺絲、埋入件、油漆……選擇每項按每個零件計算，還是每塊列印板只算一次。會為此工作儲存。","extraName":"項目","extraCost":"費用","extraAdd":"新增額外費用","c_extras":"額外費用","quoteBtn":"複製報價","quoteAll":"複製所有列印板的報價","copied":"報價已複製。貼到你的訊息中即可。","q_title":"報價","q_qty":"數量","q_unit":"每件價格","q_total":"合計","q_time":"列印時間","q_material":"材料","q_taxinc":"含稅","byModelT":"依模型的價格","byModelP":"材料依每個模型的線材分攤；時間、設備和準備依其線材占比分攤。僅為估算。","modelN":"模型 {n}","mdlCost":"成本/件","mdlPrice":"價格/件","cfg_updates":"開啟分頁時檢查新版本","upd_new":"Print Calculator {v} 已推出。請在 OrcaSlicer 的外掛管理員中更新。","allBtn":"所有列印板","allCost":"總成本","priceSetT":"整板含稅價格","profitSet":"整板利潤","gramsSet":"整板線材","partsN":"件零件（列印板上）","unitSet":"整板成本","costSetT":"整板成本","py_set":"整板 {p}（含稅，{n} 件）","jobN":"工作 {n}","jobNameP":"名稱（客戶、專案…）","jobP":"為每塊列印板選擇工作。每個工作有自己的合計、利潤率和報價。","jobMargin":"工作利潤率","jobTime":"總時間","jobProfit":"利潤","sumCost":"成本","sumTax":"營業稅","sumProfit":"利潤","setupT":"請先確認你的數值","setupP":"確認下方設定後才會顯示價格：貨幣、稅率、電費、列印機價格以及每種線材的每公斤價格。已填入的是參考值，可能與你的實際情況不同。確認後請按儲存按鈕。","repBtn":"回報問題","repBusy":"正在準備 G-code…","repDone":"G-code 已儲存到 {p}。請把這個檔案拖到瀏覽器中開啟的 issue 裡，並描述問題。","repNoG":"這塊列印板沒有 G-code。issue 已在沒有檔案的情況下開啟：請描述問題，如果可以請重新切片。","repLink":"如果瀏覽器沒有開啟，請開啟這個連結：","noLabels":"此 G-code 沒有寫明列印板上有幾個零件，所以價格是整塊列印板的價格。要統計零件，請在 OrcaSlicer 製程設定（其他）中開啟「Label objects」，然後重新切片。","exPart":"每件","exPlate":"每板"},"th":{"imp":"นำเข้าไฟล์ที่สไลซ์แล้ว","impBig":"ไฟล์ใหญ่เกินไป (สูงสุด 200 MB)","impFail":"อ่านไฟล์นี้ไม่ได้","sliceAllP":"มีหลายเพลต? ใช้ \"สไลซ์ทั้งหมด\" ใน OrcaSlicer แล้วทุกเพลตจะแสดงที่นี่ หรือนำเข้า .gcode.3mf หรือ .gcode ก็ได้","chT":"ช่องทางการขาย","chP":"ราคาของแต่ละช่องทางรวมค่าธรรมเนียมและภาษีแล้ว กำไรของคุณจึงเท่ากันทุกช่องทาง","chDirect":"ขายตรง","chN":"ช่องทาง","chFee":"ค่าธรรมเนียม","chPiece":"ต่อชิ้น","qChannel":"ราคาสำหรับช่องทางนี้","chAdd":"เพิ่มช่องทาง","chName":"ชื่อ (เช่น Shopee)","chNote":"ค่าธรรมเนียมเป็น % ของราคารวมภาษี บวกค่าธรรมเนียมคงที่ต่อการขาย ปลั๊กอินจะขึ้นราคาเพื่อให้กำไรเท่าเดิม","withShip":"รวมค่าส่ง ({s})","q_ship":"ค่าส่ง","q_grand":"รวมทั้งหมดพร้อมค่าส่ง","qSave":"บันทึกใบเสนอราคา","qSaved":"บันทึกใบเสนอราคาแล้ว","qsT":"ใบเสนอราคาที่บันทึกไว้","qsCopy":"คัดลอก","qsPdf":"พิมพ์ / PDF","qsDel":"ลบ","qsCsv":"ส่งออก CSV","qsFile":"บันทึกไว้ที่ {p}","qsFileOpen":"บันทึกไว้ที่ {p} เปิดเพื่อพิมพ์หรือบันทึกเป็น PDF","spoolBtn":"อ่านราคาจาก Spoolman","spoolPick":"Spoolman…","spoolErr":"อ่าน Spoolman ไม่ได้: {e}","spoolOk":"เส้นพลาสติก {n} รายการจาก Spoolman เลือกหนึ่งรายการข้างเส้นแต่ละเส้นแล้วบันทึก","spoolNone":"Spoolman ไม่มีเส้นพลาสติกที่มีราคาและน้ำหนัก","cfg_shipping":"ค่าส่งต่อคำสั่งซื้อ","cfg_q_name":"ชื่อในใบเสนอราคา (ร้าน)","cfg_q_footer":"หมายเหตุท้ายใบเสนอราคา","cfg_spoolman":"ที่อยู่ Spoolman (ไม่บังคับ)","qh_item":"รายการ","qh_qty":"จำนวน","qh_unit":"ราคาต่อหน่วย","qh_vat":"รวมภาษี ({v}%)","qh_date":"วันที่","buf_note":"ตัวคูณสำรอง: เพิ่มส่วนเผื่อให้ค่าใช้จ่ายรายชั่วโมงของเครื่องพิมพ์ (ค่าเสื่อม ค่าบำรุงรักษา และค่าไฟ) สำหรับสิ่งที่ไม่คาดคิด: เครื่องเสีย อะไหล่ (hotend หัวฉีด แผ่นพิมพ์ สายพาน) เวลาที่เครื่องหยุด และเก็บเงินซื้อเครื่องใหม่ 1.3 = +30% ไม่เปลี่ยนค่าวัสดุ ปกติ: 1.2 ถึง 1.3; เครื่องที่มีปัญหาบ่อย: 1.4 ถึง 1.5; 1 = ไม่มีสำรอง","eff_note":"ตัวคูณการสูญเสียวัสดุ: คูณเฉพาะค่าเส้นพลาสติก สำหรับพลาสติกที่ G-code ไม่ได้นับ (เศษปลายม้วน การเปลี่ยนเส้น การพิมพ์ทดสอบ เส้นที่เสีย) 1.10 = ค่าวัสดุ +10% อย่านับไพรม์ทาวเวอร์และการไล่เส้น (รวมในกรัมแล้ว) หรืองานพิมพ์ที่ล้มเหลว (มีเงินสำรองแยก) ปกติ: 1.05 ถึง 1.15; 1 = ไม่มีการสูญเสีย","sub":"ราคาขาย FDM ของเพลตที่สไลซ์แล้ว","readyT":"พร้อมคำนวณ","readyP":"สไลซ์เพลตใน OrcaSlicer แล้วราคาขายจะแสดงที่นี่ รวมวัสดุ ค่าไฟ การสึกหรอของเครื่องพิมพ์ และมาร์จิ้นของคุณ","s1":"เลือกเครื่องพิมพ์และเส้นพลาสติก","s2":"สไลซ์เพลต","s3":"ดูราคา","reading":"กำลังอ่าน G-code","failT":"คำนวณไม่ได้","warnT":"โปรดทราบ","noPrinter":"เครื่องพิมพ์ไม่มีชื่อ","updated":"อัปเดตเมื่อ {t}","priceT":"ราคาต่อชิ้นรวมภาษี","mine":"มาร์จิ้นของคุณ","sim":"มาร์จิ้นของเพลตนี้","profit":"กำไรต่อชิ้น","margin":"มาร์จิ้น","plate":"ทั้งเพลต ({n} ชิ้น)","time":"เวลาของเพลต","grams":"เส้นพลาสติกต่อชิ้น","copy1":"ชิ้นบนเพลต","copyN":"ชิ้นที่เหมือนกันบนเพลต","unit":"ต้นทุนต่อชิ้น","fils":"เส้นพลาสติก","color1":"สี","colorN":"สี","perKg":"/กก.","tower":"ไพรม์ทาวเวอร์และการไล่เส้น รวมในกรัมแล้ว","costT":"ต้นทุนต่อชิ้น","profitT":"กำไรตามมาร์จิ้นที่เลือก","c_material":"วัสดุ","c_energy":"ค่าไฟ","c_machine":"การสึกหรอของเครื่องพิมพ์","c_reserve":"เงินสำรองเครื่อง","c_failure":"เงินสำรองงานเสีย","c_labor":"ค่าแรง","c_pack":"บรรจุภัณฑ์","src_custom":"คุณตั้งเอง","src_orca":"โปรไฟล์ OrcaSlicer","src_average":"ราคาเฉลี่ย","src_generic":"ค่าทั่วไป","cfgT":"การตั้งค่าราคา","cfgS":"ทั่วไป เครื่องพิมพ์ และเส้นพลาสติก","general":"ทั่วไป","printer":"เครื่องพิมพ์","p_price":"ราคาเครื่องพิมพ์","p_life":"อายุการใช้งาน","years":"ปี","p_uptime":"การใช้งานต่อปี","p_maint":"ค่าบำรุงรักษาต่อปี","p_power":"กำลังไฟเฉลี่ย","p_buffer":"ตัวคูณสำรอง","f_note":"ราคาต่อกิโลกรัมมาจากช่อง \"ต้นทุนเส้นพลาสติก\" ในโปรไฟล์ OrcaSlicer หากแก้ไขที่นี่ ค่าจะถูกบันทึกสำหรับเส้นพลาสติกนั้น","scan":"ค้นหาเพลตที่สไลซ์แล้ว","scanP":"สไลซ์ก่อนเปิดปลั๊กอินใช่ไหม? เลือกเพลตที่สไลซ์ล่าสุดใน OrcaSlicer แล้วปลั๊กอินจะอ่าน G-code ของเพลตนั้นโดยไม่ต้องสไลซ์ใหม่","platesCost":"ต้นทุนรวมของคุณ","plateN":"เพลต {n}","platesTotal":"รวม {n} เพลต รวมภาษี","notSliced":"{n} ยังไม่ได้สไลซ์","notSlicedP":"สไลซ์เพลตนี้เพื่อดูราคา ราคาของแต่ละเพลตที่สไลซ์แล้วจะเก็บไว้ตราบที่โปรเจกต์ยังเปิดอยู่","customM":"มาร์จิ้นกำหนดเอง","save":"บันทึกและคำนวณใหม่","reset":"รีเซ็ตค่าเครื่องพิมพ์","h":"ชม.","min":"นาที","cfg_intro":"ค่าที่ใช้คำนวณราคาขายของแต่ละเพลตที่สไลซ์ คุณยังแก้ไขได้ในแท็บ Print Calculator พร้อมโปรไฟล์ต้นทุนของเครื่องพิมพ์แต่ละเครื่องและราคาเส้นพลาสติก","cfg_ro":"อ่านอย่างเดียว","cfg_currency":"สกุลเงิน","cfg_search":"ค้นหาสกุลเงิน (ชื่อหรือรหัส)","cfg_vat":"ภาษีมูลค่าเพิ่ม / ภาษี (%)","cfg_margin":"มาร์จิ้น (%)","cfg_failure":"อัตรางานเสีย (%)","cfg_electricity":"ค่าไฟ (ต่อ kWh)","cfg_rate":"ค่าแรง (ต่อชั่วโมง)","cfg_setup":"การเตรียมต่อเพลต (นาที)","cfg_finish":"การเก็บงานต่อชิ้น (นาที)","cfg_pack":"บรรจุภัณฑ์ต่อชิ้น","cfg_efficiency":"ตัวคูณการสูญเสียวัสดุ (×)","cfg_language":"ภาษา","cfg_auto":"เหมือน OrcaSlicer","py_perm":"OrcaSlicer ไม่อนุญาตให้อ่าน G-code เมื่อถาม ให้เลือกใช่ แล้วสไลซ์ใหม่","py_missing":"ไม่มี G-code แล้ว โปรดสไลซ์ใหม่","py_empty":"G-code นี้ไม่มีเวลาหรือกรัมที่ Print Calculator อ่านได้","py_fail":"คำนวณไม่ได้: {e}","py_unit":"{p} ต่อชิ้นรวมภาษี","py_plate":" ({n} ชิ้น, {t})","py_hint":"Print Calculator: เปิดแท็บ Print Calculator เพื่อดูราคาของเพลตนี้","py_none":"ไม่พบเพลตที่สไลซ์แล้วในเซสชัน OrcaSlicer นี้ สไลซ์เพลต หรือส่งออก G-code (Ctrl+G) หากสไลซ์แล้ว","cfg_minimum":"ราคาขั้นต่ำต่อชิ้นรวมภาษี","cfg_rounding":"ปัดราคาต่อชิ้นขึ้น","cfg_round_none":"ไม่ปัด","cfg_round_99":"เป็น .99","cfg_discount_qty":"ส่วนลดตามจำนวนตั้งแต่ (ชิ้นบนเพลต)","cfg_discount_pct":"ส่วนลดตามจำนวน (%)","adj_disc":"ส่วนลดตามจำนวน −{v}%","adj_min":"ราคาขั้นต่ำ","adj_round":"ปัดแล้ว","refShort":"ราคาอ้างอิง €","refNote":"ราคาที่ติดป้าย \"ราคาอ้างอิง €\" เป็นค่าเฉลี่ยในหน่วยยูโร ยังไม่แปลงเป็น {c} โปรดตรวจสอบในการตั้งค่าด้านล่าง","extrasT":"รายการเพิ่มเติม","extrasP":"แม่เหล็ก สกรู อินเสิร์ต สี… เลือกว่าแต่ละรายการนับต่อชิ้นหรือนับครั้งเดียวต่อเพลต บันทึกไว้สำหรับงานนี้","extraName":"รายการ","extraCost":"ต้นทุน","extraAdd":"เพิ่มรายการ","c_extras":"ค่าใช้จ่ายเพิ่ม","quoteBtn":"คัดลอกใบเสนอราคา","quoteAll":"คัดลอกใบเสนอราคาทุกเพลต","copied":"คัดลอกใบเสนอราคาแล้ว วางในข้อความของคุณได้เลย","q_title":"ใบเสนอราคา","q_qty":"จำนวน","q_unit":"ราคาต่อชิ้น","q_total":"รวม","q_time":"เวลาพิมพ์","q_material":"วัสดุ","q_taxinc":"รวมภาษี","byModelT":"ราคาตามโมเดล","byModelP":"วัสดุแบ่งตามเส้นพลาสติกของแต่ละโมเดล ส่วนเวลา เครื่อง และการเตรียมแบ่งตามสัดส่วนเส้นพลาสติก เป็นค่าประมาณ","modelN":"โมเดล {n}","mdlCost":"ต้นทุน/ชิ้น","mdlPrice":"ราคา/ชิ้น","cfg_updates":"ตรวจหาเวอร์ชันใหม่เมื่อเปิดแท็บ","upd_new":"มี Print Calculator {v} แล้ว อัปเดตได้ในตัวจัดการปลั๊กอินของ OrcaSlicer","allBtn":"ทุกเพลต","allCost":"ต้นทุนรวม","priceSetT":"ราคาทั้งเพลตรวมภาษี","profitSet":"กำไรทั้งเพลต","gramsSet":"เส้นพลาสติกทั้งเพลต","partsN":"ชิ้นบนเพลต","unitSet":"ต้นทุนทั้งเพลต","costSetT":"ต้นทุนทั้งเพลต","py_set":"{p} ทั้งเพลตรวมภาษี ({n} ชิ้น)","jobN":"งาน {n}","jobNameP":"ชื่อ (ลูกค้า, โปรเจกต์…)","jobP":"เลือกงานของแต่ละเพลต แต่ละงานมียอดรวม มาร์จิ้น และใบเสนอราคาของตัวเอง","jobMargin":"มาร์จิ้นของงาน","jobTime":"เวลารวม","jobProfit":"กำไร","sumCost":"ต้นทุน","sumTax":"ภาษีมูลค่าเพิ่ม","sumProfit":"กำไร","setupT":"ยืนยันค่าของคุณก่อน","setupP":"ราคาจะแสดงหลังจากคุณยืนยันการตั้งค่าด้านล่าง: สกุลเงิน ภาษี ค่าไฟ ราคาเครื่องพิมพ์ และราคาต่อกก.ของเส้นพลาสติกแต่ละชนิด ค่าที่กรอกไว้แล้วเป็นค่าอ้างอิงและอาจไม่ตรงกับของคุณ จากนั้นกดปุ่มบันทึก","repBtn":"รายงานปัญหา","repBusy":"กำลังเตรียม G-code…","repDone":"บันทึก G-code ไว้ที่ {p} แล้ว ลากไฟล์นี้ไปใส่ใน issue ที่เปิดในเบราว์เซอร์ และอธิบายปัญหา","repNoG":"เพลตนี้ไม่มี G-code issue เปิดโดยไม่มีไฟล์: อธิบายปัญหา และถ้าทำได้ให้สไลซ์ใหม่","repLink":"ถ้าเบราว์เซอร์ไม่เปิด ให้เปิดลิงก์นี้:","noLabels":"G-code นี้ไม่ได้บอกจำนวนชิ้นบนเพลต ราคาจึงเป็นราคาทั้งเพลต หากต้องการนับชิ้น ให้เปิด \"Label objects\" ในการตั้งค่ากระบวนการของ OrcaSlicer (อื่นๆ) แล้วสไลซ์ใหม่","exPart":"ต่อชิ้น","exPlate":"ต่อเพลต"},"vi":{"imp":"Nhập tệp đã cắt lớp","impBig":"Tệp quá lớn (tối đa 200 MB).","impFail":"Không đọc được tệp này.","sliceAllP":"Có nhiều bàn in? Dùng \"Cắt lớp tất cả\" trong OrcaSlicer và tất cả sẽ hiện ở đây. Bạn cũng có thể nhập .gcode.3mf hoặc .gcode.","chT":"Kênh bán hàng","chP":"Giá ở mỗi kênh đã bao gồm phí và thuế: lợi nhuận của bạn ở đâu cũng như nhau.","chDirect":"Bán trực tiếp","chN":"Kênh","chFee":"Phí","chPiece":"Mỗi chi tiết","qChannel":"Giá cho kênh này","chAdd":"Thêm kênh","chName":"Tên (vd. Shopee)","chNote":"Phí tính theo % giá đã gồm thuế cộng phí cố định mỗi lần bán. Plugin tăng giá để bạn giữ nguyên lợi nhuận.","withShip":"gồm phí vận chuyển ({s})","q_ship":"Vận chuyển","q_grand":"Tổng gồm vận chuyển","qSave":"Lưu báo giá","qSaved":"Đã lưu báo giá","qsT":"Báo giá đã lưu","qsCopy":"Sao chép","qsPdf":"In / PDF","qsDel":"Xóa","qsCsv":"Xuất CSV","qsFile":"Đã lưu vào {p}","qsFileOpen":"Đã lưu vào {p}. Mở tệp để in hoặc lưu thành PDF.","spoolBtn":"Đọc giá từ Spoolman","spoolPick":"Spoolman…","spoolErr":"Không đọc được Spoolman: {e}","spoolOk":"{n} loại sợi từ Spoolman. Chọn một loại cạnh mỗi sợi rồi lưu.","spoolNone":"Spoolman không có sợi nào có giá và trọng lượng.","cfg_shipping":"Phí vận chuyển mỗi đơn","cfg_q_name":"Tên trên báo giá (cửa hàng)","cfg_q_footer":"Ghi chú cuối báo giá","cfg_spoolman":"Địa chỉ Spoolman (tùy chọn)","qh_item":"Mặt hàng","qh_qty":"SL","qh_unit":"Đơn giá","qh_vat":"Đã gồm thuế ({v}%)","qh_date":"Ngày","buf_note":"Hệ số dự phòng: cộng thêm một khoản vào chi phí theo giờ của máy in (hao mòn, bảo trì và điện) cho những việc ngoài dự tính: hỏng hóc, linh kiện (hotend, đầu phun, bàn in, dây đai), thời gian máy dừng và tiết kiệm cho máy in tiếp theo. 1,3 = +30%. Không thay đổi vật liệu. Thông thường: 1,2 đến 1,3; máy hay trục trặc: 1,4 đến 1,5; 1 = không dự phòng.","eff_note":"Hệ số hao hụt vật liệu: chỉ nhân với chi phí sợi nhựa, cho phần nhựa mà G-code không tính (đầu cuộn, thay sợi, in thử, sợi bị hỏng). 1,10 = +10% vật liệu. Đừng tính tháp mồi và xả nhựa (đã có trong số gam) hay bản in lỗi (đã có khoản dự phòng riêng). Thông thường: 1,05 đến 1,15; 1 = không hao hụt.","sub":"Giá bán FDM của bàn in đã cắt lớp","readyT":"Sẵn sàng tính giá","readyP":"Cắt lớp một bàn in trong OrcaSlicer và giá bán sẽ hiện ở đây, gồm vật liệu, điện, hao mòn máy in và biên lợi nhuận của bạn.","s1":"Chọn máy in và sợi nhựa","s2":"Cắt lớp bàn in","s3":"Xem giá","reading":"Đang đọc G-code","failT":"Không thể tính","warnT":"Lưu ý","noPrinter":"Máy in chưa đặt tên","updated":"Cập nhật lúc {t}","priceT":"Giá mỗi chi tiết gồm thuế","mine":"biên lợi nhuận của bạn","sim":"biên lợi nhuận của bàn in này","profit":"lợi nhuận mỗi chi tiết","margin":"biên lợi nhuận","plate":"cả bàn in ({n} chi tiết)","time":"thời gian bàn in","grams":"sợi nhựa mỗi chi tiết","copy1":"chi tiết trên bàn in","copyN":"chi tiết giống nhau trên bàn in","unit":"chi phí mỗi chi tiết","fils":"Sợi nhựa","color1":"màu","colorN":"màu","perKg":"/kg","tower":"Tháp xả và xả nhựa, đã tính trong số gam","costT":"Chi phí mỗi chi tiết","profitT":"Lợi nhuận với biên đã chọn","c_material":"Vật liệu","c_energy":"Điện","c_machine":"Hao mòn máy in","c_reserve":"Dự phòng máy","c_failure":"Dự phòng lỗi in","c_labor":"Nhân công","c_pack":"Đóng gói","src_custom":"Do bạn đặt","src_orca":"Hồ sơ OrcaSlicer","src_average":"Giá trung bình","src_generic":"Giá trị chung","cfgT":"Cài đặt giá","cfgS":"chung, máy in và sợi nhựa","general":"Chung","printer":"Máy in","p_price":"Giá máy in","p_life":"Tuổi thọ","years":"năm","p_uptime":"Mức dùng hằng năm","p_maint":"Bảo trì hằng năm","p_power":"Công suất trung bình","p_buffer":"Hệ số dự phòng","f_note":"Giá mỗi kg lấy từ ô \"Chi phí sợi nhựa\" trong hồ sơ OrcaSlicer. Nếu đổi ở đây, giá sẽ được lưu cho loại sợi đó.","scan":"Tìm bàn in đã cắt lớp","scanP":"Đã cắt lớp trước khi bật plugin? Chọn bàn in cắt lớp gần nhất trong OrcaSlicer và plugin sẽ đọc G-code của nó mà không cần cắt lại.","platesCost":"Tổng chi phí của bạn","plateN":"Bàn in {n}","platesTotal":"Tổng {n} bàn in, gồm thuế","notSliced":"{n} chưa được cắt lớp","notSlicedP":"Cắt lớp bàn in này để xem giá. Giá của mỗi bàn in đã cắt lớp được giữ khi dự án còn mở.","customM":"Biên lợi nhuận tùy chỉnh","save":"Lưu và tính lại","reset":"Đặt lại giá trị máy in","h":"giờ","min":"phút","cfg_intro":"Các giá trị dùng cho giá bán của mỗi bàn in đã cắt lớp. Bạn cũng có thể đổi chúng trong thẻ Print Calculator, cùng hồ sơ chi phí của từng máy in và giá sợi nhựa.","cfg_ro":"Chỉ đọc.","cfg_currency":"Tiền tệ","cfg_search":"Tìm tiền tệ (tên hoặc mã)","cfg_vat":"VAT / thuế (%)","cfg_margin":"Biên lợi nhuận (%)","cfg_failure":"Tỷ lệ lỗi (%)","cfg_electricity":"Điện (mỗi kWh)","cfg_rate":"Nhân công (mỗi giờ)","cfg_setup":"Chuẩn bị mỗi bàn in (phút)","cfg_finish":"Hoàn thiện mỗi chi tiết (phút)","cfg_pack":"Đóng gói mỗi chi tiết","cfg_efficiency":"Hệ số hao hụt vật liệu (×)","cfg_language":"Ngôn ngữ","cfg_auto":"Giống OrcaSlicer","py_perm":"OrcaSlicer không cho phép đọc G-code. Khi được hỏi, hãy chọn Có và cắt lớp lại.","py_missing":"G-code không còn nữa. Hãy cắt lớp lại.","py_empty":"G-code này không có thời gian hay số gam mà Print Calculator đọc được.","py_fail":"Không thể tính: {e}","py_unit":"{p} mỗi chi tiết gồm thuế","py_plate":" ({n} chi tiết, {t})","py_hint":"Print Calculator: mở thẻ Print Calculator để xem giá của bàn in này.","py_none":"Không có bàn in đã cắt lớp trong phiên OrcaSlicer này. Hãy cắt lớp bàn in, hoặc xuất G-code (Ctrl+G) nếu đã cắt lớp.","cfg_minimum":"Giá tối thiểu mỗi chi tiết, gồm thuế","cfg_rounding":"Làm tròn lên giá mỗi chi tiết","cfg_round_none":"Không làm tròn","cfg_round_99":"Đến ,99","cfg_discount_qty":"Giảm giá theo số lượng từ (chi tiết trên bàn in)","cfg_discount_pct":"Giảm giá theo số lượng (%)","adj_disc":"giảm giá số lượng −{v}%","adj_min":"giá tối thiểu","adj_round":"đã làm tròn","refShort":"tham khảo theo €","refNote":"Giá đánh dấu \"tham khảo theo €\" là mức trung bình bằng euro, chưa quy đổi sang {c}. Hãy kiểm tra trong phần cài đặt bên dưới.","extrasT":"Phụ phí","extrasP":"Nam châm, ốc vít, insert, sơn… Chọn mỗi khoản tính cho từng chi tiết hay một lần cho cả bàn in. Được lưu cho công việc này.","extraName":"Mục","extraCost":"Chi phí","extraAdd":"Thêm phụ phí","c_extras":"Phụ phí","quoteBtn":"Sao chép báo giá","quoteAll":"Sao chép báo giá tất cả bàn in","copied":"Đã sao chép báo giá. Hãy dán vào tin nhắn của bạn.","q_title":"Báo giá","q_qty":"Số lượng","q_unit":"Giá mỗi chi tiết","q_total":"Tổng","q_time":"Thời gian in","q_material":"Vật liệu","q_taxinc":"đã gồm thuế","byModelT":"Giá theo mẫu","byModelP":"Vật liệu chia theo sợi nhựa của từng mẫu; thời gian, máy và chuẩn bị chia theo tỷ lệ sợi nhựa. Đây là ước tính.","modelN":"Mẫu {n}","mdlCost":"chi phí/chi tiết","mdlPrice":"giá/chi tiết","cfg_updates":"Kiểm tra phiên bản mới khi mở thẻ","upd_new":"Đã có Print Calculator {v}. Hãy cập nhật trong trình quản lý plugin của OrcaSlicer.","allBtn":"Tất cả bàn in","allCost":"Tổng chi phí","priceSetT":"Giá cả bàn in gồm thuế","profitSet":"lợi nhuận cả bàn in","gramsSet":"sợi nhựa cả bàn in","partsN":"chi tiết trên bàn in","unitSet":"chi phí cả bàn in","costSetT":"Chi phí cả bàn in","py_set":"{p} cho cả bàn in gồm thuế ({n} chi tiết)","jobN":"Công việc {n}","jobNameP":"Tên (khách hàng, dự án…)","jobP":"Chọn công việc cho từng bàn in. Mỗi công việc có tổng, biên lợi nhuận và báo giá riêng.","jobMargin":"Biên lợi nhuận của công việc","jobTime":"tổng thời gian","jobProfit":"lợi nhuận","sumCost":"Chi phí","sumTax":"VAT","sumProfit":"Lợi nhuận","setupT":"Trước tiên, hãy xác nhận các giá trị của bạn","setupP":"Giá chỉ hiển thị sau khi bạn xác nhận các cài đặt bên dưới: tiền tệ, thuế, điện, giá máy in và giá mỗi kg của từng loại sợi nhựa. Các giá trị điền sẵn chỉ là tham khảo và có thể không đúng với bạn. Sau đó nhấn nút lưu.","repBtn":"Báo lỗi","repBusy":"Đang chuẩn bị G-code…","repDone":"Đã lưu G-code vào {p}. Kéo tệp này vào issue vừa mở trong trình duyệt và mô tả vấn đề.","repNoG":"Bàn in này không có G-code. Issue đã mở mà không có tệp: hãy mô tả vấn đề và nếu có thể, cắt lớp lại.","repLink":"Nếu trình duyệt không mở, hãy mở liên kết này:","noLabels":"G-code này không cho biết có bao nhiêu chi tiết trên bàn in, nên giá là của cả bàn in. Để đếm chi tiết, hãy bật \"Label objects\" trong cài đặt quy trình của OrcaSlicer (Khác) rồi cắt lớp lại.","exPart":"mỗi chi tiết","exPlate":"mỗi bàn in"}}''')
LANGS = [["en", "English"], ["pt", "Português (Portugal)"], ["pt_BR", "Português (Brasil)"], ["es", "Español"], ["ca", "Català"], ["eu", "Euskara"], ["fr", "Français"], ["de", "Deutsch"], ["it", "Italiano"], ["nl", "Nederlands"], ["sv", "Svenska"], ["pl", "Polski"], ["cs", "Čeština"], ["hu", "Magyar"], ["lt", "Lietuvių"], ["tr", "Türkçe"], ["ru", "Русский"], ["uk", "Українська"], ["ja", "日本語"], ["ko", "한국어"], ["zh_CN", "简体中文"], ["zh_TW", "繁體中文"], ["th", "ไทย"], ["vi", "Tiếng Việt"]]  # código e nome na própria língua
# Moedas ISO 4217 em circulação (sem metais, fundos e códigos de teste); o browser junta as que conhecer a mais.
CURRENCIES = ["AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY", "COP", "CRC", "CUC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SLL", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VED", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWG"]
COMMA_DECIMAL = {"pt", "pt_BR", "es", "ca", "eu", "fr", "de", "it", "nl", "sv", "pl", "cs", "hu", "lt", "tr", "ru", "uk", "vi"}


DEFAULT_GENERAL = {"currency": "EUR", "language": "auto", "vat": 23.0, "margin": 40.0, "failure": 0.0, "electricity": 0.16, "rate": 15.0, "setup": 0.0,
                   "finish": 0.0, "pack": 0.0, "efficiency": 1.0,
                   "minimum": 0.0, "rounding": "none", "discount_qty": 0.0, "discount_pct": 0.0, "updates": True,
                   "shipping": 0.0, "spoolman": "", "q_name": "", "q_footer": ""}
DEFAULT_PRINTER = {"price": 600.0, "maintenance": 75.0, "life": 3.0, "uptime": 50.0, "power": 150.0, "buffer": 1.3}
LIMITS = {"vat": (0, 100), "margin": (0, 95), "failure": (0, 90), "electricity": (0, 10000), "rate": (0, 1e5), "setup": (0, 1e5), "finish": (0, 1e5),
          "pack": (0, 1e8), "efficiency": (1, 10), "price": (0, 1e9), "maintenance": (0, 1e9), "life": (.1, 100), "uptime": (.1, 100),
          "power": (0, 1e6), "buffer": (1, 10), "minimum": (0, 1e8), "discount_qty": (0, 1e6), "discount_pct": (0, 90),
          "shipping": (0, 1e8), "fee_pct": (0, 45)}
ROUNDINGS = ("none", "0.05", "0.1", "0.5", "1", "99")  # arredondar para cima ao múltiplo, ou para ,99
TEXT_KEYS = ("currency", "language", "rounding", "updates", "spoolman", "q_name", "q_footer")  # definições gerais que não são números
TEXT_LIMITS = {"q_name": 120, "q_footer": 300}


def _num(v, key=None, default=0.0):
    lo, hi = LIMITS.get(key, (0, 1e9))
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n)) if math.isfinite(n) else default


def _words(s):
    return [w for w in re.split(r"[^a-z0-9]+", str(s or "").lower()) if w]


def catalog_profile(printer_name):
    """Perfil de custos do catálogo para o nome da impressora do OrcaSlicer (todas as palavras do modelo têm de aparecer)."""
    target, best = set(_words(printer_name)), None
    for brand, models in CATALOG.items():
        for model, price, maint, life, uptime, power in models:
            words = _words(model)
            if words and all(w in target for w in words):
                score = len(words) * 2 + (1 if all(w in target for w in _words(brand)) else 0)
                if best is None or score > best[0]:
                    best = (score, {"price": price, "maintenance": maint, "life": life, "uptime": uptime, "power": power, "buffer": 1.3,
                                    "source": {"kind": "catalog", "name": f"{brand} {model}"}})
    return best[1] if best else None


def settings_path():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        here = os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer", "orca_plugins", "print_calculator")
    return os.path.join(here, "settings.json")


def clean_general(raw):
    """Definições gerais (página de definições do plugin no OrcaSlicer ou settings.json), dentro dos limites."""
    out = {}
    for k, v in (raw if isinstance(raw, dict) else {}).items():
        if k == "currency" and re.fullmatch(r"[A-Z]{3}", str(v or "")):
            out[k] = v
        elif k == "language" and (v == "auto" or v in I18N):
            out[k] = v
        elif k == "rounding" and v in ROUNDINGS:
            out[k] = v
        elif k == "updates" and isinstance(v, bool):
            out[k] = v
        elif k == "spoolman" and isinstance(v, str):
            out[k] = clean_url(v)
        elif k in TEXT_LIMITS and isinstance(v, str):
            out[k] = _text(v, TEXT_LIMITS[k])
        elif k in DEFAULT_GENERAL and k not in TEXT_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = _num(v, k, DEFAULT_GENERAL[k])
    return out


def _clean_printers(raw):
    out = {}
    for name, prof in (raw if isinstance(raw, dict) else {}).items():
        if name and isinstance(prof, dict):
            out[str(name)] = {k: _num(v, k, DEFAULT_PRINTER[k]) for k, v in prof.items() if k in DEFAULT_PRINTER}
    return out


def _clean_filaments(raw):
    return {str(k): _num(v) for k, v in (raw if isinstance(raw, dict) else {}).items() if k and isinstance(v, (int, float)) and not isinstance(v, bool)}


def clean_extras(raw):
    """Extras (ímanes, parafusos...): até 20, com nome curto, custo dentro dos limites e se contam em cada peça ("part")
    ou uma vez na placa ("plate")."""
    out = []
    for e in (raw if isinstance(raw, list) else [])[:20]:
        if isinstance(e, dict):
            name, cost = str(e.get("name") or "").strip()[:60], _num(e.get("cost"), "pack")
            if name or cost > 0:
                out.append({"name": name, "cost": cost, "per": "plate" if e.get("per") == "plate" else "part"})
    return out


def _text(v, size):
    """Texto numa linha, sem espaços a mais, com tamanho máximo."""
    return " ".join(str(v or "").split())[:size]


def clean_url(v):
    """Endereço http(s) do Spoolman, sem a barra final; vazio se não for um endereço."""
    v = str(v or "").strip().rstrip("/")
    return v if re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/[^\s]*)?", v) and len(v) <= 200 else ""


MAX_CHANNELS = 8


def clean_channels(raw):
    """Canais de venda (Etsy, loja online...): nome, taxa em % sobre o preço com IVA e taxa fixa por venda."""
    out = []
    for c in (raw if isinstance(raw, list) else [])[:MAX_CHANNELS]:
        if isinstance(c, dict):
            name, pct, fixed = _text(c.get("name"), 40), _num(c.get("pct"), "fee_pct"), _num(c.get("fixed"), "pack")
            if name or pct > 0 or fixed > 0:
                out.append({"name": name, "pct": pct, "fixed": fixed})
    return out


def channel_price(gross, vat, pct, fixed):
    """Preço com IVA num canal que fica com pct % do preço e uma taxa fixa por venda. Depois da taxa e do IVA sobra o
    mesmo que na venda direta, por isso o lucro não muda."""
    v = _num(vat, "vat") / 100
    keep = max(0.05, 1 - _num(pct, "fee_pct") / 100 - v / (1 + v))
    return (gross / (1 + v) + _num(fixed, "pack")) / keep


MAX_QUOTES = 100


def clean_quote(q):
    """Um orçamento guardado: as linhas e os totais como estavam quando foi feito."""
    if not isinstance(q, dict):
        return None
    lines = []
    for x in (q.get("lines") if isinstance(q.get("lines"), list) else [])[:100]:
        if isinstance(x, dict):
            lines.append({"name": _text(x.get("name"), 120), "qty": _num(x.get("qty"), "discount_qty"),
                          "unit": _num(x.get("unit"), "pack"), "total": _num(x.get("total"), "pack")})
    currency = str(q.get("currency") or "")
    if not lines or not re.fullmatch(r"[A-Z]{3}", currency):
        return None
    qid = re.sub(r"[^0-9a-z]", "", str(q.get("id") or "").lower())[:24] or hashlib.sha1(json.dumps(lines).encode()).hexdigest()[:12]
    return {"id": qid, "date": _text(q.get("date"), 40), "title": _text(q.get("title"), 120), "currency": currency, "lines": lines,
            "total": _num(q.get("total"), "pack"), "shipping": _num(q.get("shipping"), "shipping"), "vat": _num(q.get("vat"), "vat"),
            "seconds": _num(q.get("seconds")), "material": _text(q.get("material"), 200), "channel": _text(q.get("channel"), 40)}


def clean_quotes(raw):
    out = [q for q in (clean_quote(x) for x in (raw if isinstance(raw, list) else [])) if q]
    return out[-MAX_QUOTES:]


MEMORY_LIMIT = 300


def clean_memory(raw):
    """Margem e extras lembrados por trabalho (assinatura do G-code), os mais recentes no fim."""
    out = {}
    for sig, v in (raw if isinstance(raw, dict) else {}).items():
        if isinstance(v, dict) and re.fullmatch(r"[0-9a-f]{16}", str(sig)):
            item = {}
            if isinstance(v.get("margin"), (int, float)) and not isinstance(v.get("margin"), bool):
                item["margin"] = _num(v["margin"], "margin")
            if clean_extras(v.get("extras")):
                item["extras"] = clean_extras(v.get("extras"))
            if item:
                out[sig] = item
    return dict(list(out.items())[-MEMORY_LIMIT:])


def load_settings(path=None, config=None):
    """Definições: valores de origem, depois o settings.json antigo (versões 2.0/2.1), depois o que o OrcaSlicer guarda."""
    try:
        with open(path or settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    config = config if isinstance(config, dict) else {}
    general = {**DEFAULT_GENERAL, **clean_general(data.get("general")), **clean_general(config)}
    return {"general": general, "printers": {**_clean_printers(data.get("printers")), **_clean_printers(config.get("printers"))},
            "filaments": {**_clean_filaments(data.get("filaments")), **_clean_filaments(config.get("filaments"))},
            "memory": clean_memory(config.get("memory")), "channels": clean_channels(config.get("channels")),
            "quotes": clean_quotes(config.get("quotes"))}


def to_config(settings):
    """Tudo num só objeto JSON, para o OrcaSlicer guardar (save_config)."""
    return {**{k: settings["general"][k] for k in DEFAULT_GENERAL}, "printers": settings["printers"], "filaments": settings["filaments"],
            "memory": settings.get("memory", {}), "channels": settings.get("channels", []), "quotes": settings.get("quotes", [])}


def save_settings(settings, path=None):
    path = path or settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def printer_profile(settings, name):
    saved = (settings.get("printers") or {}).get(name)
    if saved:
        return {**DEFAULT_PRINTER, **saved, "source": {"kind": "custom"}}
    return catalog_profile(name) or {**DEFAULT_PRINTER, "source": {"kind": "generic"}}


def filament_price(settings, f):
    saved = (settings.get("filaments") or {}).get(f.get("preset") or f.get("type") or "")
    if saved is not None:
        return _float(saved), "custom"
    if f.get("cost", 0) > 0:
        return f["cost"], "orca"
    t = str(f.get("type") or "").lower()
    if t in FILAMENT_PRICES:
        return FILAMENT_PRICES[t], "average"
    first = _words(t)[0] if _words(t) else ""
    return FILAMENT_PRICES.get(first, 22), "average"


# ---------------------------------------------------------------- cálculo FDM (igual à calculadora do Print & Cut, modo avançado)
def round_up(gross, rounding):
    if rounding == "99":
        up = math.ceil(gross) - 0.01
        return up + 1 if up < gross - 1e-9 else up
    step = float(rounding)
    return math.ceil(gross / step - 1e-9) * step


def sale_price(unit, margin, copies, general):
    """Preço com a margem sobre o preço sem IVA; depois desconto de quantidade, preço mínimo e arredondamento (por esta ordem)."""
    vat = _num(general.get("vat"), "vat", DEFAULT_GENERAL["vat"])
    gross = unit / (1 - _num(margin, "margin") / 100) * (1 + vat / 100)
    adj = []
    qty, pct = _num(general.get("discount_qty"), "discount_qty"), _num(general.get("discount_pct"), "discount_pct")
    if pct > 0 and qty > 0 and copies >= qty:
        gross *= 1 - pct / 100
        adj.append("disc")
    minimum = _num(general.get("minimum"), "minimum")
    if minimum > 0 and gross < minimum:
        gross = minimum
        adj.append("min")
    rounding = general.get("rounding", "none")
    if rounding in ROUNDINGS and rounding != "none":
        up = round_up(gross, rounding)
        if abs(up - gross) > 1e-9:
            gross = up
            adj.append("round")
    net = gross / (1 + vat / 100)
    return {"net": net, "gross": gross, "profit": net - unit, "totalGross": gross * copies, "adj": adj}


MAX_JOBS = 9  # trabalhos por projeto


def job_total(gross, general):
    """Total de um trabalho com várias placas: o preço mínimo e o arredondamento contam uma vez, na soma das placas."""
    minimum = _num(general.get("minimum"), "minimum")
    if minimum > 0 and gross < minimum:
        gross = minimum
    rounding = general.get("rounding", "none")
    if rounding in ROUNDINGS and rounding != "none":
        gross = round_up(gross, rounding)
    return gross


def item_name(job):
    """Nome para o orçamento: o modelo (sem "obj_1_" nem extensão); com vários, o primeiro e quantos mais."""
    clean = [re.sub(r"\.(stl|3mf|obj|step|stp|amf)$", "", re.sub(r"^obj_\d+_", "", n), flags=re.I) for n in job.get("names") or []]
    clean = [n for n in clean if n]
    return "" if not clean else clean[0] + (f" +{len(clean) - 1}" if len(clean) > 1 else "")


def signature(job):
    """Identifica o mesmo trabalho noutra sessão: modelos, peças, gramas e tempo."""
    raw = json.dumps([job.get("names") or [], job.get("objects", 0), round(sum(f["grams"] for f in job["filaments"]), 1), int(job.get("seconds") or 0)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def quote(job, settings, extras=None):
    g = {k: _num(v, k, DEFAULT_GENERAL.get(k, 0.0)) if k not in TEXT_KEYS else v for k, v in settings["general"].items()}
    profile = printer_profile(settings, job.get("printer") or job.get("printer_model") or "")
    p = {k: _num(profile.get(k), k, DEFAULT_PRINTER[k]) for k in DEFAULT_PRINTER}
    copies = job["objects"] if job.get("objects", 0) > 1 and job.get("models") == 1 else 1
    q = copies
    rows = []
    for tool, f in ((i, x) for i, x in enumerate(job["filaments"]) if x["grams"] > 0):
        price, source = filament_price(settings, f)
        rows.append({"name": f.get("preset") or f["type"] or "Filament", "type": f["type"], "color": f["color"], "price": price, "source": source, "tool": tool,
                     "grams": js_round(f["grams"] / copies * 100) / 100, "key": f.get("preset") or f.get("type") or ""})
    minutes = js_round((job["seconds"] or 0) / 60)
    hours = (minutes // 60) + (minutes % 60) / 60  # tempo total da placa, contado uma vez
    parts = job.get("parts") if copies == 1 else None
    pieces = sum(x["count"] for x in parts) if parts else q  # peças a contar no acabamento, embalagem e extras
    yearly = 8760 * p["uptime"] / 100
    dep, maint, elec = p["price"] / (p["life"] * yearly), p["maintenance"] / yearly, p["power"] / 1000 * g["electricity"]
    costs = {"material": sum(r["price"] * r["grams"] / 1000 for r in rows) * q * g["efficiency"], "energy": hours * elec,
             "machine": hours * (dep + maint), "reserve": hours * (dep + maint + elec) * (p["buffer"] - 1),
             "labor": (g["setup"] + g["finish"] * pieces) / 60 * g["rate"], "pack": g["pack"] * pieces}
    if g["failure"] > 0:
        costs["failure"] = (costs["material"] + costs["energy"] + costs["machine"] + costs["reserve"]) * (1 / (1 - g["failure"] / 100) - 1)
    extras = clean_extras(extras)
    each_extra = sum(e["cost"] for e in extras if e["per"] == "part")  # em cada peça
    plate_extra = sum(e["cost"] for e in extras if e["per"] == "plate")  # uma vez na placa
    if each_extra * pieces + plate_extra > 0:
        costs["extras"] = each_extra * pieces + plate_extra
    total = sum(costs.values())
    unit = total / q
    breakdown = []
    if parts:
        # material pela parte de cada filamento; tempo, máquina e preparação pela parte do filamento total
        tool_total = {}
        for x in parts:
            for t, mm in x["tools"].items():
                tool_total[t] = tool_total.get(t, 0.0) + mm
        everything = sum(tool_total.values()) or 1
        mat_tool = {r["tool"]: r["price"] * r["grams"] / 1000 * g["efficiency"] for r in rows}
        timed = costs["energy"] + costs["machine"] + costs["reserve"]
        for x in parts:
            share = sum(x["tools"].values()) / everything
            mat = sum(mat_tool.get(t, 0.0) * mm / tool_total[t] for t, mm in x["tools"].items() if tool_total.get(t, 0) > 0)
            tot = mat + timed * share
            if g["failure"] > 0:
                tot *= 1 / (1 - g["failure"] / 100)
            tot += (g["setup"] * share + g["finish"] * x["count"]) / 60 * g["rate"] + (g["pack"] + each_extra) * x["count"] + plate_extra * share
            breakdown.append({"name": x["name"], "count": x["count"], "unit": tot / x["count"], "total": tot})

    def price(margin):
        return sale_price(unit, margin, q, {**DEFAULT_GENERAL, **g})

    return {"copies": copies, "seconds": job["seconds"], "tower": job.get("tower"), "labelled": job.get("objects", 0) > 0, "filaments": rows, "extras": extras, "item": item_name(job), "breakdown": breakdown,
            "costs": {k: v / q for k, v in costs.items() if v > 0}, "unit": unit, "chosen": price(g["margin"]), "margin": g["margin"],
            "prices": {str(m): price(m) for m in (25, 40, 60, 80)}, "currency": settings["general"].get("currency") or "EUR",
            "printer": job.get("printer") or job.get("printer_model") or "", "profile": {**{k: p[k] for k in p}, "source": profile.get("source", "")},
            "general": {k: settings["general"].get(k, DEFAULT_GENERAL[k]) for k in DEFAULT_GENERAL},
            "channels": settings.get("channels", [])}


# ---------------------------------------------------------------- separador (HTML com as cores do tema do OrcaSlicer)
PAGE_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:var(--orca-bg,#1b1d20);--fg:var(--orca-fg,#e8eaed);--mut:var(--orca-muted,#9aa0a6);--ac:var(--orca-accent,#009688);--acf:var(--orca-accent-fg,#fff);--bd:var(--orca-border,#3a3d42);
--card:color-mix(in srgb,var(--fg) 4%,var(--bg));--soft:color-mix(in srgb,var(--fg) 8%,var(--bg));--tint:color-mix(in srgb,var(--ac) 14%,var(--bg));--line:color-mix(in srgb,var(--bd) 70%,transparent);
--c-material:var(--ac);--c-energy:#e6a23c;--c-machine:#4c8dff;--c-reserve:#9b72e8;--c-failure:#e5534b;--c-labor:#e86fa8;--c-pack:#7d8a96;--c-extras:#26c6da}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);font:13.5px/1.45 var(--orca-font,"Segoe UI",system-ui,sans-serif)}
main{max-width:1040px;margin:0 auto;padding:18px 20px 28px}.mut{color:var(--mut)}.num{font-variant-numeric:tabular-nums}
header{display:flex;align-items:center;gap:12px;margin-bottom:16px}.logo{width:34px;height:34px;border-radius:10px;background:var(--ac);display:grid;place-items:center;flex:none}
.logo svg{width:20px;height:20px}header h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}header .sub{font-size:12px;color:var(--mut)}
.chips{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;background:var(--soft);font-size:12px;white-space:nowrap}
.chip .dot{width:7px;height:7px;border-radius:50%;background:var(--ac)}.chip.busy .dot{animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.25}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}.stack{display:grid;gap:14px}
.title{font-size:11.5px;font-weight:650;letter-spacing:.6px;text-transform:uppercase;color:var(--mut);margin:0 0 12px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.hero{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:14px}
.price-card{background:linear-gradient(135deg,var(--tint),var(--card) 70%);border-color:color-mix(in srgb,var(--ac) 35%,var(--line));position:relative;overflow:hidden}
.price-card:after{content:"";position:absolute;right:-60px;top:-60px;width:200px;height:200px;border-radius:50%;background:radial-gradient(color-mix(in srgb,var(--ac) 22%,transparent),transparent 70%)}
.big{font-size:44px;font-weight:750;letter-spacing:-1px;line-height:1.05;margin:2px 0 4px;color:var(--fg)}.big small{font-size:14px;font-weight:500;color:var(--mut);letter-spacing:0;margin-left:6px}
.facts{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}.facts div{display:flex;flex-direction:column}.facts b{font-size:15px}.facts span{font-size:11.5px;color:var(--mut)}
.seg{display:flex;gap:4px;padding:3px;background:color-mix(in srgb,var(--bg) 55%,transparent);border:1px solid var(--line);border-radius:10px;margin-top:16px;position:relative;z-index:1}
.seg button{flex:1;border:0;background:transparent;color:var(--fg);padding:7px 4px;border-radius:7px;cursor:pointer;font:inherit;display:flex;flex-direction:column;align-items:center;gap:1px}
.seg button span{font-size:11px;color:var(--mut)}.seg button b{font-size:13px}.seg button.on{background:var(--ac);color:var(--acf)}.seg button.on span{color:color-mix(in srgb,var(--acf) 80%,transparent)}
.seg button:not(.on):hover{background:var(--soft)}
.tag.warn{background:color-mix(in srgb,#e6a23c 18%,var(--bg));color:#e6a23c}.warnTxt{color:#e6a23c!important}.adj{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;position:relative;z-index:1}
.bdr{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:10px;padding:6px 0;align-items:center}.bdr+.bdr{border-top:1px solid var(--line)}.bdr.head{font-size:11.5px;padding-top:0}
.bdr span:not(.n),.bdr b{text-align:right}.bdr .n{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.alert.upd{margin-bottom:14px;background:color-mix(in srgb,var(--ac) 14%,var(--bg));border-color:color-mix(in srgb,var(--ac) 45%,transparent)}.alert.upd svg{color:var(--ac)}
label.chk{flex-direction:row;align-items:center;gap:8px;color:var(--fg);font-size:12.5px}label.chk input{accent-color:var(--ac);width:16px;height:16px}
.pick{display:grid;gap:6px}.pick .prow{display:grid;grid-template-columns:1fr auto 190px;gap:12px;align-items:center}
.inp select{flex:1;min-width:0;border:0;background:transparent;color:var(--fg);font:inherit;padding:7px 8px;outline:0}.inp select option{background:var(--card,var(--bg));color:var(--fg)}
.jhead{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.jhead .jn{flex:1;min-width:180px}.jhead .crow{gap:10px}
.sum{display:grid;gap:5px;margin-top:12px;max-width:340px;font-size:13px}.sum div{display:flex;justify-content:space-between;gap:16px}.sum b{font-weight:600}
.sum .pr b{color:var(--ac)}.sum .tot{border-top:1px solid var(--line);padding-top:6px;font-weight:700}.sum .tot b{font-weight:700}
.jobsec{display:grid;gap:14px;padding-top:6px}details.card .dbody{padding:0 18px 14px}.ptabs button.all{border-style:dashed}
.foot{margin-top:18px;padding-top:12px;border-top:1px solid var(--line);display:grid;gap:8px;justify-items:start}.foot #rep{font-size:12.5px;padding:7px 12px}
.foot a{color:var(--ac);word-break:break-all}
.exrow{display:grid;grid-template-columns:2fr 1fr auto;gap:8px;margin-bottom:8px}.exrow.ex4{grid-template-columns:2fr 1fr 1fr auto}.btn.x{padding:6px 12px}.qcard .actions{align-items:center}.exrow.chrow{grid-template-columns:2fr 1fr 1fr auto}
.qrow{display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}.qrow .actions{flex-wrap:nowrap}
.spsel select{max-width:100%}.qcard{padding:12px 18px}
.inp select{flex:1;min-width:0;border:0;background:transparent;color:var(--fg);font:inherit;padding:8px 10px;outline:0}.inp select option{background:var(--bg);color:var(--fg)}
.cur{display:grid;grid-template-columns:1fr 1.6fr;gap:6px}label.span2{grid-column:span 2}@media(max-width:760px){label.span2{grid-column:1/-1}}
.scan{display:flex;flex-direction:column;align-items:center;gap:6px;margin-top:20px}.scan .mut{font-size:12px;max-width:420px}
.plates{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}.ptabs{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.ptabs button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:10px;padding:6px 10px;font:inherit;cursor:pointer;display:flex;flex-direction:column;align-items:flex-start;gap:1px;min-width:76px}
.ptabs button span{font-size:11.5px;color:var(--mut)}.ptabs button b{font-size:13px}.ptabs button.on{border-color:var(--ac);background:var(--tint)}.ptabs button.on span{color:var(--fg);font-weight:600}
.ptotals{display:flex;gap:18px}.ptotal{display:flex;flex-direction:column;align-items:flex-end;font-size:11.5px;color:var(--mut)}.ptotal b{font-size:16px;color:var(--fg)}
.custom{position:relative;z-index:1;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}.crow{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:12.5px;font-weight:600}
.crow .inp{width:104px}.crow .inp input{padding:6px 8px}.custom input[type=range]{width:100%;margin:10px 0 2px;accent-color:var(--ac);cursor:pointer}
.ticks{position:relative;height:14px;margin:0 8px;font-size:10.5px;color:var(--mut)}.ticks span{position:absolute;transform:translateX(-50%)}.custom p{font-size:11.5px;margin:6px 0 0}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:10px}.kpi{background:var(--soft);border-radius:12px;padding:12px 14px;display:flex;gap:10px;align-items:center}
.kpi svg{width:20px;height:20px;flex:none;color:var(--ac)}.kpi b{display:block;font-size:17px}.kpi span{font-size:11.5px;color:var(--mut)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.bar{display:flex;height:12px;border-radius:999px;overflow:hidden;background:var(--soft);margin-bottom:14px}.bar i{display:block;height:100%}.bar i+i{box-shadow:-2px 0 0 var(--card)}
.legend{display:grid;gap:7px}.legend .r{display:grid;grid-template-columns:10px 1fr auto 48px;gap:10px;align-items:center}.legend .k{width:10px;height:10px;border-radius:3px}.legend .p{color:var(--mut);text-align:right;font-size:12px}
.total{display:flex;justify-content:space-between;border-top:1px solid var(--line);margin-top:12px;padding-top:10px;font-weight:650}
.fil{display:grid;grid-template-columns:30px 1fr auto;gap:12px;align-items:center;padding:9px 0}.fil+.fil{border-top:1px solid var(--line)}
.sw{width:30px;height:30px;border-radius:50%;border:2px solid color-mix(in srgb,var(--fg) 18%,transparent);box-shadow:inset 0 -6px 10px rgba(0,0,0,.18)}
.fil .n{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.fil .m{font-size:11.5px;color:var(--mut);display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.tag{font-size:10.5px;padding:1px 7px;border-radius:999px;background:var(--soft);color:var(--mut)}.tag.ac{background:var(--tint);color:var(--fg)}
.meter{height:4px;border-radius:9px;background:var(--soft);margin-top:6px;overflow:hidden}.meter i{display:block;height:100%;border-radius:9px}
.fil .g{text-align:right}.fil .g b{display:block;font-size:14px}.fil .g span{font-size:11.5px;color:var(--mut)}
.tower{display:flex;align-items:center;gap:10px;margin-top:8px;padding:9px 12px;border-radius:10px;background:var(--soft);font-size:12.5px}.tower svg{width:16px;height:16px;color:var(--mut);flex:none}.tower b{margin-left:auto;white-space:nowrap}
details.card{padding:0}details summary{list-style:none;cursor:pointer;padding:14px 18px;display:flex;align-items:center;gap:10px;font-weight:600}details summary::-webkit-details-marker{display:none}
details summary svg{width:18px;height:18px;color:var(--mut)}details summary .chev{margin-left:auto;transition:transform .2s}details[open] summary .chev{transform:rotate(180deg)}
.cfg{padding:0 18px 18px;display:grid;gap:18px}.cfg h3{font-size:12.5px;margin:0 0 8px;display:flex;gap:8px;align-items:center}
.form{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}label{display:flex;flex-direction:column;gap:5px;font-size:11.5px;color:var(--mut)}
.inp{display:flex;align-items:center;border:1px solid var(--bd);border-radius:9px;background:var(--bg);transition:border-color .15s,box-shadow .15s}
.inp:focus-within{border-color:var(--ac);box-shadow:0 0 0 3px color-mix(in srgb,var(--ac) 22%,transparent)}
.inp input{flex:1;min-width:0;border:0;background:transparent;color:var(--fg);font:inherit;padding:8px 10px;outline:0}.inp em{font-style:normal;color:var(--mut);padding-right:10px;font-size:12px}
.summary{display:flex;flex-wrap:wrap;gap:6px}.note{font-size:12px;color:var(--mut);margin:8px 0 0}
.actions{display:flex;gap:8px;flex-wrap:wrap}button.btn{padding:9px 16px;border:0;border-radius:9px;background:var(--ac);color:var(--acf);font:inherit;font-weight:600;cursor:pointer}
button.btn.ghost{background:transparent;color:var(--fg);border:1px solid var(--bd)}button.btn:hover{filter:brightness(1.08)}
.empty{text-align:center;padding:44px 20px}.empty svg{width:120px;height:90px;color:var(--ac);margin-bottom:10px}.empty h2{font-size:17px;margin:0 0 6px}.empty p{margin:0 auto;max-width:430px;color:var(--mut)}
.steps{display:flex;justify-content:center;gap:10px;margin-top:18px;flex-wrap:wrap}.steps div{display:flex;align-items:center;gap:8px;background:var(--soft);border-radius:999px;padding:6px 12px 6px 6px;font-size:12.5px}
.steps b{width:22px;height:22px;border-radius:50%;background:var(--ac);color:var(--acf);display:grid;place-items:center;font-size:11.5px}
.sk{border-radius:8px;background:linear-gradient(90deg,var(--soft),color-mix(in srgb,var(--fg) 14%,var(--bg)),var(--soft));background-size:200% 100%;animation:sh 1.2s infinite}@keyframes sh{to{background-position:-200% 0}}
.alert{display:flex;gap:12px;align-items:flex-start;border-radius:12px;padding:14px 16px;background:color-mix(in srgb,#e6a23c 13%,var(--bg));border:1px solid color-mix(in srgb,#e6a23c 40%,transparent)}
.alert.err{background:color-mix(in srgb,#e5534b 13%,var(--bg));border-color:color-mix(in srgb,#e5534b 40%,transparent)}.alert svg{width:20px;height:20px;flex:none;color:#e6a23c}.alert.err svg{color:#e5534b}
@media(max-width:760px){.hero,.cols{grid-template-columns:1fr}.form{grid-template-columns:1fr 1fr}.big{font-size:36px}}
</style></head><body><main>
<header><div class="logo"><svg viewBox="0 0 20 20" fill="none" stroke="#fff" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10.5 2.5h6v6l-8 8.5-6.5-6.5z"/><circle cx="13.6" cy="5.4" r="1.1"/><path d="M10.9 9.2c-.6-.6-1.9-.5-2.3.2-.4.8.3 1.3 1.1 1.5.9.2 1.5.8 1.1 1.6-.4.8-1.8.8-2.4.1M9.6 8.2l.5.5M10.4 13.2l.5.5"/></svg></div>
<div><h1>Print Calculator</h1><div class="sub" id="sub"></div></div><div class="chips" id="chips"></div></header>
<div id="upd"></div><div id="plates"></div><div id="out"></div><div id="quotes" style="margin-top:14px"></div><div id="cfg" style="margin-top:14px"></div><input type="file" id="impF" accept=".3mf,.gcode,.gco" hidden><div class="foot"><button class="btn ghost" id="rep"></button><div id="repMsg" class="note"></div></div></main>
<script>
__CURRENCY_JS__
const I18N=__I18N__,LANGS=__LANGS__;
let LANG=I18N['__LANG__']?'__LANG__':'en',LOC=locOf(LANG),CL=null,CLL=null;
const t=(k,v={})=>String((I18N[LANG]||{})[k]??I18N.en[k]??k).replace(/\{(\w+)\}/g,(_,x)=>v[x]??'');
const curList=()=>{if(CLL!==LOC){CL=currencyList(LOC);CLL=LOC;}return CL;};
function setLang(l){if(!l||l===LANG||!I18N[l])return false;LANG=l;LOC=locOf(l);$('#sub').textContent=t('sub');$('#rep').textContent=t('repBtn');document.documentElement.lang=LANG;return true;}
const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const I={clock:'<path d="M10 3.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zM10 6.5V10l2.5 1.5"/>',weight:'<path d="M6.5 7h7l2 9h-11zM8 7a2 2 0 1 1 4 0"/>',copies:'<rect x="3.5" y="6.5" width="8" height="8" rx="1.5"/><path d="M7.5 4h7a1.5 1.5 0 0 1 1.5 1.5v7"/>',coin:'<circle cx="10" cy="10" r="6.5"/><path d="M12 7.6c-.5-.6-1.2-.9-2-.9-1.2 0-2 .6-2 1.5 0 2 4 1 4 3 0 .9-.9 1.5-2 1.5-.8 0-1.6-.3-2-.9M10 5.5v1.2M10 13.3v1.2"/>',
 tower:'<path d="M7 16.5h6M8 16.5l.8-10h2.4l.8 10M8.8 6.5V3.5h2.4v3"/>',gear:'<circle cx="10" cy="10" r="2.3"/><path d="M10 2.8v2M10 15.2v2M2.8 10h2M15.2 10h2M4.9 4.9l1.4 1.4M13.7 13.7l1.4 1.4M4.9 15.1l1.4-1.4M13.7 6.3l1.4-1.4"/>',chev:'<path d="M5.5 8l4.5 4.5L14.5 8"/>',
 warn:'<path d="M10 3l7.5 13h-15zM10 8.2v3.6M10 14v.1"/>',printer:'<path d="M4 3.5h12v13H4zM4 7h12M8 10.5h4l-1 2.5H9z"/>',spool:'<circle cx="10" cy="10" r="6.5"/><circle cx="10" cy="10" r="2"/>',tag:'<path d="M3.5 10.5V3.5h7l6 6-7 7z"/><circle cx="7" cy="7" r="1"/>'};
const ic=(n,cls='',size)=>'<svg class="'+cls+'"'+(size?' width="'+size+'" height="'+size+'"':'')+' viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'+I[n]+'</svg>';
let last=null,sel=null,QUOTES=[],SPOOL=null,QCH=-1;
const fmt=c=>{try{return new Intl.NumberFormat(LOC,{style:'currency',currency:c||'EUR'})}catch(e){return new Intl.NumberFormat(LOC,{style:'currency',currency:'EUR'})}};
const money=(v,c)=>fmt(c).format(v||0);
const symbol=c=>(fmt(c).formatToParts(0).find(x=>x.type==='currency')||{}).value||c;
const dur=s=>{s=Math.round((s||0)/60);const h=Math.floor(s/60);return (h?h+' '+t('h')+' ':'')+(s%60)+' '+t('min')};
const num=v=>new Intl.NumberFormat(LOC,{maximumFractionDigits:2}).format(v||0);
const srcText=s=>s&&s.kind==='catalog'?s.name:t('src_'+(s&&s.kind||s));
function chips(html){$('#chips').innerHTML=html}
function field(scope,key,label,value,unit){return '<label>'+label+'<span class="inp"><input class="num" data-scope="'+scope+'" data-key="'+esc(key)+'" type="number" step="any" min="0" value="'+esc(value)+'" data-orig="'+esc(value)+'">'+(unit?'<em>'+esc(unit)+'</em>':'')+'</span></label>'}
function empty(){chips('');$('#cfg').innerHTML='';$('#out').innerHTML='<div class="card empty"><svg viewBox="0 0 120 90" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="14" y="8" width="92" height="74" rx="8" opacity=".35"/><path d="M14 24h92" opacity=".35"/><path d="M36 66h48" /><path d="M44 66V50h32v16" opacity=".8"/><path d="M48 50l4-12h16l4 12" opacity=".6"/><path d="M60 30v-6"/><circle cx="60" cy="38" r="2" fill="currentColor"/></svg><h2>'+t('readyT')+'</h2><p>'+t('readyP')+'</p><div class="steps"><div><b>1</b>'+t('s1')+'</div><div><b>2</b>'+t('s2')+'</div><div><b>3</b>'+t('s3')+'</div></div>'+scanBox()+'</div>';bindScan();}
function scanBox(){return '<div class="scan"><div class="actions"><button class="btn ghost" id="scan">'+t('scan')+'</button><button class="btn ghost" id="imp">'+t('imp')+'</button></div><span class="mut">'+t('scanP')+'</span><span class="mut">'+t('sliceAllP')+'</span></div>';}
function bindScan(){const b=$('#scan');if(b)b.onclick=()=>{loading();orca.postMessage({type:'scan'});};const i=$('#imp');if(i)i.onclick=pickImport;}
// ficheiro já fatiado (.gcode.3mf ou .gcode): vai para o plugin em base64, que lê as placas que lá estiverem
function pickImport(){const f=$('#impF');f.value='';f.onchange=()=>{const file=f.files[0];if(!file)return;
 if(file.size>200*1024*1024){alertBox({level:'warn',text:t('impBig')});return;}
 const r=new FileReader();r.onload=()=>{const s=String(r.result);orca.postMessage({type:'import',name:file.name.replace(/(\.gcode)?\.3mf$|\.gcode$|\.gco$/i,''),b64:s.slice(s.indexOf(',')+1)});};
 r.onerror=()=>alertBox({level:'err',text:t('impFail')});loading();r.readAsDataURL(file);};f.click();}
function loading(){chips('<span class="chip busy"><span class="dot"></span>'+t('reading')+'</span>');$('#out').innerHTML='<div class="hero"><div class="card"><div class="sk" style="height:14px;width:40%"></div><div class="sk" style="height:46px;width:65%;margin:12px 0"></div><div class="sk" style="height:38px"></div></div><div class="kpis">'+'<div class="kpi sk" style="height:64px"></div>'.repeat(4)+'</div></div>';}
function alertBox(d){chips('');$('#out').innerHTML='<div class="alert '+(d.level==='err'?'err':'')+'">'+ic('warn')+'<div><b>'+t(d.level==='err'?'failT':'warnT')+'</b><div class="mut">'+esc(d.text)+'</div></div></div>';}
// mesma fórmula do Python (margem sobre o preço sem IVA), para a barra responder logo
function roundUp(g,r){if(r==='99'){const up=Math.ceil(g)-0.01;return up<g-1e-9?up+1:up;}const st=Number(r);return st>0?Math.ceil(g/st-1e-9)*st:g;}
function priceAt(d,m){return salePrice(d.general,d.unit,d.copies,m);}
function salePrice(G,unit,q,m){const vat=Number(G.vat)||0,adj=[];let gross=unit/(1-m/100)*(1+vat/100);
 if(G.discount_pct>0&&G.discount_qty>0&&q>=G.discount_qty){gross*=1-G.discount_pct/100;adj.push('disc');}
 if(G.minimum>0&&gross<G.minimum){gross=G.minimum;adj.push('min');}
 if(G.rounding&&G.rounding!=='none'){const up=roundUp(gross,G.rounding);if(Math.abs(up-gross)>1e-9){gross=up;adj.push('round');}}
 const net=gross/(1+vat/100);return {net,gross,profit:net-unit,totalGross:gross*q,adj};}
const modelName=(x,i)=>x.name||t('modelN',{n:i+1});
const mixed=d=>!!(d.breakdown&&d.breakdown.length>1),partsOf=d=>mixed(d)?d.breakdown.reduce((a,x)=>a+x.count,0):d.copies;
function breakdownRows(d,m){const c=d.currency;let sum=0;
 const rows=(d.breakdown||[]).map((x,i)=>{const sp=salePrice(d.general,x.unit,x.count,m);sum+=sp.totalGross;
  return '<div class="bdr"><span class="n">'+esc(modelName(x,i))+' <span class="mut">×'+x.count+'</span></span><span class="num mut">'+money(x.unit,c)+'</span><span class="num">'+money(sp.gross,c)+'</span><b class="num">'+money(sp.totalGross,c)+'</b></div>';}).join('');
 return '<div class="bdr head mut"><span></span><span>'+t('mdlCost')+'</span><span>'+t('mdlPrice')+'</span><span>'+t('q_total')+'</span></div>'+rows
  +'<div class="total"><span>'+t('q_total')+'</span><span class="num">'+money(sum,c)+'</span></div>';}
function breakdownCard(d){if(!d.breakdown||d.breakdown.length<2)return '';
 return '<div class="card"><div class="title">'+t('byModelT')+'</div><div id="bd"></div><p class="note">'+t('byModelP')+'</p></div>';}
const warnRef=(d,src)=>d.currency!=='EUR'&&(src==='average'||!!(src&&(src.kind==='catalog'||src.kind==='generic')));
const refTag=()=>' <span class="tag warn">'+esc(t('refShort'))+'</span>';
function extrasRows(d){return (d.extras||[]).map((e,i)=>'<div class="exrow ex4"><span class="inp"><input data-ex="name" data-i="'+i+'" value="'+esc(e.name)+'" placeholder="'+esc(t('extraName'))+'"></span><span class="inp"><input class="num" data-ex="cost" data-i="'+i+'" type="number" min="0" step="any" value="'+esc(e.cost)+'" placeholder="'+esc(t('extraCost'))+'"><em>'+esc(symbol(d.currency))+'</em></span><span class="inp"><select data-ex="per" data-i="'+i+'"><option value="part"'+(e.per!=='plate'?' selected':'')+'>'+esc(t('exPart'))+'</option><option value="plate"'+(e.per==='plate'?' selected':'')+'>'+esc(t('exPlate'))+'</option></select></span><button class="btn ghost x" data-del="'+i+'">×</button></div>').join('');}
function readExtras(){const list=(last.extras||[]).map(e=>({...e}));document.querySelectorAll('[data-ex]').forEach(i=>{const e=list[+i.dataset.i];if(!e)return;if(i.dataset.ex==='name')e.name=i.value;else if(i.dataset.ex==='per')e.per=i.value;else e.cost=Number(i.value)||0;});return list;}
function sendExtras(list){loading();orca.postMessage({type:'plate_extras',index:last.plate.index,extras:list});}
function bindExtras(){document.querySelectorAll('[data-ex]').forEach(i=>i.onchange=()=>sendExtras(readExtras()));
 document.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>{const l=readExtras();l.splice(+b.dataset.del,1);sendExtras(l);});
 const add=$('#exAdd');if(add)add.onclick=()=>{last.extras=readExtras().concat([{name:'',cost:0,per:'part'}]);$('#ex').innerHTML=extrasRows(last);bindExtras();const n=document.querySelectorAll('[data-ex=name]');if(n.length)n[n.length-1].focus();};}
// preço num canal de venda: depois da taxa e do IVA fica o mesmo que na venda direta (channel_price no Python)
function chPrice(gross,vat,ch){const v=(Number(vat)||0)/100,keep=Math.max(.05,1-Math.min(45,Math.max(0,Number(ch.pct)||0))/100-v/(1+v));return (gross/(1+v)+(Number(ch.fixed)||0))/keep;}
const ship=d=>Number(((d||{}).general||{}).shipping)||0;
const plateGross=(d,m)=>mixed(d)?d.breakdown.reduce((a,x)=>a+salePrice(d.general,x.unit,x.count,m).totalGross,0):priceAt(d,m).totalGross;
const types=fl=>fl.map(f=>f.type||f.name).filter((x,i,a)=>x&&a.indexOf(x)===i).join(', ');
function channelsCard(d){return (d.channels||[]).length?'<div class="card"><div class="title">'+t('chT')+'</div><div id="ch"></div><p class="note">'+t('chP')+'</p></div>':'';}
function channelRows(){const d=last,box=$('#ch');if(!box)return;const m=current().cur.m,base=plateGross(d,m),qty=partsOf(d)||1,c=d.currency;
 const rows=[{name:t('chDirect'),price:base,fee:0}].concat(d.channels.map(ch=>{const p=chPrice(base,d.general.vat,ch);return {name:ch.name||t('chN'),price:p,fee:p*ch.pct/100+ch.fixed};}));
 box.innerHTML='<div class="bdr head mut"><span></span><span>'+t('chFee')+'</span><span>'+t('chPiece')+'</span><span>'+t('q_total')+'</span></div>'
  +rows.map(r=>'<div class="bdr"><span class="n">'+esc(r.name)+'</span><span class="num mut">'+(r.fee?money(r.fee,c):'—')+'</span><span class="num">'+money(r.price/qty,c)+'</span><b class="num">'+money(r.price,c)+'</b></div>').join('');}
function chSelect(){const ch=(last&&last.channels)||[];if(!ch.length)return '';
 return '<span class="inp"><select class="qch" title="'+esc(t('qChannel'))+'"><option value="-1">'+esc(t('chDirect'))+'</option>'+ch.map((x,i)=>'<option value="'+i+'"'+(i===QCH?' selected':'')+'>'+esc(x.name||t('chN'))+'</option>').join('')+'</select></span>';}
function bindChSelect(){document.querySelectorAll('.qch').forEach(s=>s.onchange=()=>{QCH=+s.value;document.querySelectorAll('.qch').forEach(o=>{o.value=s.value;});});}
const stamp=()=>{const d=new Date(),p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());};
// orçamento pronto a copiar ou guardar; num canal de venda, os preços sobem na mesma proporção do total
function finishQuote(q,chi){const ch=((last&&last.channels)||[])[chi],sum=q.lines.reduce((a,x)=>a+x.total,0);let total=q.total??sum;
 if(ch&&total>0){const up=chPrice(total,q.vat,ch),f=up/total;q.lines=q.lines.map(x=>({...x,unit:x.unit*f,total:x.total*f}));total=up;q.channel=ch.name||t('chN');}
 return {id:Date.now().toString(36),date:stamp(),...q,total};}
function plateQuote(chi){const d=last,m=current().cur.m;let lines;
 if(mixed(d))lines=d.breakdown.map((x,i)=>{const sp=salePrice(d.general,x.unit,x.count,m);return {name:modelName(x,i),qty:x.count,unit:sp.gross,total:sp.totalGross};});
 else{const p=priceAt(d,m);lines=[{name:d.item||plateName(d.plate),qty:d.copies,unit:p.gross,total:p.totalGross}];}
 const grams=d.filaments.reduce((a,f)=>a+f.grams,0)*(mixed(d)?1:d.copies);
 return finishQuote({title:d.item||plateName(d.plate),currency:d.currency,lines,vat:Number(d.general.vat)||0,seconds:d.seconds||0,material:types(d.filaments)+' · '+num(grams)+' g',shipping:ship(d)},chi);}
function jobQuote(j,chi){const rows=jobRows(j),r0=rows[0],fl=[];rows.forEach(p=>(p.filaments||[]).forEach(f=>fl.push(f)));
 return finishQuote({title:r0.jobName||t('jobN',{n:j}),currency:r0.currency||'EUR',lines:rows.map(p=>({name:plateName(p)+(p.item?' — '+p.item:''),qty:p.copies,unit:p.gross,total:p.total})),
  total:r0.jobTotal,vat:Number(r0.vat)||0,seconds:rows.reduce((a,p)=>a+(p.seconds||0),0),material:types(fl)+' · '+num(fl.reduce((a,f)=>a+f.grams,0))+' g',shipping:ship(last)},chi);}
function quoteTextOf(q){const c=q.currency,L=[t('q_title')+(q.title?' — '+q.title:'')+(q.channel?' ('+q.channel+')':'')];
 q.lines.forEach(x=>L.push(x.name+': '+num(x.qty)+' × '+money(x.unit,c)+' = '+money(x.total,c)));
 L.push(t('q_total')+': '+money(q.total,c)+' ('+t('q_taxinc')+')');
 if(q.shipping>0){L.push(t('q_ship')+': '+money(q.shipping,c));L.push(t('q_grand')+': '+money(q.total+q.shipping,c));}
 if(q.seconds)L.push(t('q_time')+': '+dur(q.seconds));if(q.material)L.push(t('q_material')+': '+q.material);
 return L.join('\n');}
function renderQuotes(msg){const box=$('#quotes');if(!box)return;if(!QUOTES.length){box.innerHTML='';return;}
 const open=!!(msg||($('#quotes details')||{}).open);
 box.innerHTML='<details class="card"'+(open?' open':'')+'><summary>'+ic('tag')+t('qsT')+'<span class="tag">'+QUOTES.length+'</span>'+ic('chev','chev')+'</summary><div class="dbody">'
 +QUOTES.slice().reverse().map(q=>'<div class="qrow"><div style="min-width:0"><b>'+esc(q.title||t('q_title'))+'</b>'+(q.channel?' <span class="tag">'+esc(q.channel)+'</span>':'')+'<div class="mut">'+esc(q.date)+'</div></div><b class="num">'+money(q.total+q.shipping,q.currency)+'</b>'
  +'<div class="actions"><button class="btn ghost" data-qc="'+esc(q.id)+'">'+t('qsCopy')+'</button><button class="btn ghost" data-qp="'+esc(q.id)+'">'+t('qsPdf')+'</button><button class="btn ghost x" data-qd="'+esc(q.id)+'" title="'+esc(t('qsDel'))+'">×</button></div></div>').join('')
 +'<div class="actions" style="margin-top:10px;align-items:center"><button class="btn ghost" id="qsCsv">'+t('qsCsv')+'</button><span class="mut" id="qsMsg">'+esc(msg||'')+'</span></div></div></details>';
 const byId=id=>QUOTES.find(q=>q.id===id),say=x=>{$('#qsMsg').textContent=x;};
 document.querySelectorAll('[data-qc]').forEach(b=>b.onclick=()=>{copyText(quoteTextOf(byId(b.dataset.qc)));say(t('copied'));});
 document.querySelectorAll('[data-qp]').forEach(b=>b.onclick=()=>{say(t('reading'));orca.postMessage({type:'quote_print',id:b.dataset.qp});});
 document.querySelectorAll('[data-qd]').forEach(b=>b.onclick=()=>orca.postMessage({type:'quote_delete',id:b.dataset.qd}));
 $('#qsCsv').onclick=()=>{say(t('reading'));orca.postMessage({type:'quotes_csv'});};}
function copyText(text){const done=()=>{const m=$('#qMsg');if(m)m.textContent=t('copied');};
 const fallback=()=>{const a=document.createElement('textarea');a.value=text;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();try{document.execCommand('copy');done();}catch(e){}a.remove();};
 try{navigator.clipboard.writeText(text).then(done,fallback);}catch(e){fallback();}}
function extrasCard(d){const c=d.currency,sum=(d.extras||[]).reduce((a,e)=>a+e.cost*(e.per==='plate'?1:partsOf(d)),0);  // total da placa
 return '<div class="card"><div class="title">'+t('extrasT')+'<span class="num" style="text-transform:none;letter-spacing:0;color:var(--fg)">'+money(sum,c)+'</span></div><div id="ex">'+extrasRows(d)+'</div>'
 +'<div class="actions"><button class="btn ghost" id="exAdd">+ '+t('extraAdd')+'</button></div><p class="note">'+t('extrasP')+'</p></div>';}
function quoteCard(d){const ready=(d.plates||[]).filter(p=>p.ready).length;
 return '<div class="card qcard"><div class="actions">'+chSelect()+'<button class="btn" id="qOne">'+t('quoteBtn')+'</button>'+(ready>1?'<button class="btn ghost" id="qAll">'+t('quoteAll')+'</button>':'')+'<button class="btn ghost" id="qSave">'+t('qSave')+'</button><span class="mut" id="qMsg"></span></div></div>';}
function options(d){const o=[{m:d.margin,p:d.chosen,mine:true}];Object.entries(d.prices).forEach(([m,p])=>{if(Math.abs(+m-d.margin)>.001)o.push({m:+m,p});});return o.sort((a,b)=>a.m-b.m);}
function current(){const d=last,opts=options(d),cur=(sel==null?opts.find(o=>o.mine):opts.find(o=>Math.abs(o.m-sel)<.001))||{m:sel,p:priceAt(d,sel),custom:true};return {opts,cur};}
// a conta do preço: custo + lucro + IVA = preço (a linha do IVA só aparece quando há IVA)
function sumBox(cost,net,gross,c){const profit=net-cost,tax=gross-net,row=(k,v,cls)=>'<div class="'+(cls||'')+'"><span>'+k+'</span><b class="num">'+money(v,c)+'</b></div>';
 return '<div class="sum">'+row(t('sumCost'),cost)+row(t('sumProfit')+' <span class="mut">('+num(net>0?Math.round(profit/net*1000)/10:0)+'%)</span>',profit,'pr')
 +(tax>.0005?row(t('sumTax')+' <span class="mut">('+num(Math.round(tax/net*1000)/10)+'%)</span>',tax):'')+row(t('q_total'),gross,'tot')+'</div>';}
function heroInner(){
 const d=last,c=d.currency,{opts,cur}=current(),p=cur.p;
 return '<div class="title" style="margin:0">'+t(mixed(d)?'priceSetT':'priceT')+(cur.mine?'<span class="tag ac">'+t('mine')+'</span>':'<span class="tag">'+t('sim')+'</span>')+'</div>'
 +'<div class="big num">'+money(p.gross,c)+'</div>'
 +(p.adj&&p.adj.length?'<div class="adj">'+p.adj.map(a=>'<span class="tag">'+esc(t('adj_'+a,{v:num(d.general.discount_pct)}))+'</span>').join('')+'</div>':'')
 +sumBox(d.unit,p.net,p.gross,c)+(d.copies>1||ship(d)>0?'<div class="facts">'+(d.copies>1?'<div><b class="num">'+money(p.totalGross,c)+'</b><span>'+t('plate',{n:d.copies})+'</span></div>':'')
  +(ship(d)>0?'<div><b class="num">'+money(plateGross(d,cur.m)+ship(d),c)+'</b><span>'+esc(t('withShip',{s:money(ship(d),c)}))+'</span></div>':'')+'</div>':'')
 +'<div class="seg">'+opts.map(o=>'<button data-m="'+o.m+'" class="'+(o===cur?'on':'')+'"><span>'+num(o.m)+'%'+(o.mine?' ★':'')+'</span><b class="num">'+money(o.p.gross,c)+'</b></button>').join('')+'</div>';
}
function refresh(fromInput){
 const {cur}=current();$('#hp').innerHTML=heroInner();if($('#bd'))$('#bd').innerHTML=breakdownRows(last,cur.m);channelRows();$('#pf').textContent='+'+money(cur.p.profit,last.currency);
 if(fromInput!=='range')$('#mr').value=cur.m;if(fromInput!=='number')$('#mn').value=Math.round(cur.m*100)/100;
 document.querySelectorAll('.seg button').forEach(b=>b.onclick=()=>pick(+b.dataset.m));
}
// margem escolhida: fica nesta placa (botões das placas e total atualizam logo; o plugin guarda depois de parar de mexer)
let marginTimer=null;
function pick(m,from){
 sel=m;refresh(from);
 const d=last,index=d.plate.index,row=(d.plates||[]).find(p=>p.index===index);
 if(row){const cp=current().cur.p;row.total=cp.totalGross;row.gross=cp.gross;plates(d);}
 clearTimeout(marginTimer);marginTimer=setTimeout(()=>orca.postMessage({type:'plate_margin',index,margin:m}),250);
}
const plateName=p=>p.name?t('plateN',{n:p.index+1})+' · '+p.name:t('plateN',{n:p.index+1});
let allView=false,plist=[],curIdx=0;  // vista "Todas as placas"
const jobOf=p=>p.job||1,jobRows=j=>plist.filter(p=>p.ready&&jobOf(p)===j),jobIds=()=>[...new Set(plist.filter(p=>p.ready).map(jobOf))].sort((a,b)=>a-b);
const jobsTotal=()=>jobIds().reduce((a,j)=>a+(jobRows(j)[0].jobTotal??0),0);
function plates(d){
 const list=d.plates||[];if(list.length<2&&!(list.length===1&&list[0].index!==d.plate.index)){$('#plates').innerHTML='';return;}
 const ready=list.filter(p=>p.ready),cur=(ready[0]||{}).currency||'EUR',sum=jobsTotal(),cost=ready.reduce((a,p)=>a+p.cost,0);
 $('#plates').innerHTML='<div class="plates"><div class="ptabs">'+list.map(p=>'<button data-i="'+p.index+'" class="'+(!allView&&p.index===d.plate.index?'on':'')+'"><span>'+esc(plateName(p))+'</span><b class="num">'+(p.ready?money(p.total,p.currency):'—')+'</b></button>').join('')
 +(ready.length>1?'<button id="pAll" class="all'+(allView?' on':'')+'"><span>'+t('allBtn')+'</span><b class="num">Σ</b></button>':'')+'</div>'
 +(ready.length>1?'<div class="ptotals"><div class="ptotal"><span>'+t('platesCost')+'</span><b class="num">'+money(cost,cur)+'</b></div><div class="ptotal"><span>'+t('platesTotal',{n:ready.length})+'</span><b class="num">'+money(sum,cur)+'</b></div></div>':'')+'</div>';
 document.querySelectorAll('.ptabs button[data-i]').forEach(b=>b.onclick=()=>{allView=false;loading();orca.postMessage({type:'show_plate',index:+b.dataset.i});});
 const pa=$('#pAll');if(pa)pa.onclick=()=>{allView=true;plates({plates:plist,plate:{index:curIdx}});allPlatesView();};
}
function jobSection(j,rows,c,open){
 const name=rows[0].jobName||'',price=rows[0].jobTotal,cost=rows.reduce((a,p)=>a+p.cost,0),secs=rows.reduce((a,p)=>a+(p.seconds||0),0);
 const net=price/(1+(rows[0].vat||0)/100),same=rows.every(p=>Math.abs(p.margin-rows[0].margin)<.001);
 const fil={};rows.forEach(p=>(p.filaments||[]).forEach(f=>{const k=f.name+'|'+(f.color||'');const e=fil[k]||(fil[k]={name:f.name,type:f.type,color:f.color,grams:0,cost:0});e.grams+=f.grams;e.cost+=f.cost;}));
 const fl=Object.values(fil).sort((a,b)=>b.grams-a.grams),maxg=Math.max(1,...fl.map(f=>f.grams)),grams=fl.reduce((a,f)=>a+f.grams,0);
 const costs={};rows.forEach(p=>Object.entries(p.costs||{}).forEach(([k,v])=>{costs[k]=(costs[k]||0)+v;}));
 const ce=Object.entries(costs),csum=ce.reduce((a,[,v])=>a+v,0)||1;
 const mrows=[];rows.forEach(p=>(p.models||[]).forEach(x=>mrows.push({...x,plate:p.index})));
 return '<div class="jobsec"><div class="card"><div class="jhead"><span class="tag ac">'+esc(t('jobN',{n:j}))+'</span><span class="inp jn"><input data-jn="'+j+'" value="'+esc(name)+'" placeholder="'+esc(t('jobNameP'))+'" maxlength="60"></span>'
 +'<div class="crow"><label for="jm'+j+'">'+t('jobMargin')+'</label><span class="inp"><input id="jm'+j+'" data-jm="'+j+'" class="num" type="number" min="0" max="95" step="any" value="'+(same?Math.round(rows[0].margin*100)/100:'')+'"><em>%</em></span></div></div>'
 +'<div class="big num" style="margin-top:12px">'+money(price,c)+'</div><div class="mut">'+t('platesTotal',{n:rows.length})+'</div>'+sumBox(cost,net,price,c)
 +'<div class="facts"><div><b class="num">'+num(grams)+' g</b><span>'+t('fils')+'</span></div><div><b class="num">'+dur(secs)+'</b><span>'+t('jobTime')+'</span></div>'+(ship(last)>0?'<div><b class="num">'+money(price+ship(last),c)+'</b><span>'+esc(t('withShip',{s:money(ship(last),c)}))+'</span></div>':'')+'</div></div>'
 +'<div class="cols"><div class="card"><div class="title">'+t('fils')+'<span class="tag">'+fl.length+' '+t(fl.length===1?'color1':'colorN')+'</span></div>'
 +fl.map(f=>'<div class="fil"><div class="sw" style="background:'+esc(f.color||'#888')+'"></div><div style="min-width:0"><div class="n">'+esc(f.name)+'</div><div class="m">'+(f.type?'<span class="tag">'+esc(f.type)+'</span>':'')+'</div><div class="meter"><i style="width:'+(f.grams/maxg*100)+'%;background:'+esc(f.color||'var(--ac)')+'"></i></div></div><div class="g"><b class="num">'+num(f.grams)+' g</b><span class="num">'+money(f.cost,c)+'</span></div></div>').join('')+'</div>'
 +'<div class="card"><div class="title">'+t('allCost')+'<span class="num" style="text-transform:none;letter-spacing:0;color:var(--fg)">'+money(cost,c)+'</span></div><div class="bar">'+ce.map(([k,v])=>'<i title="'+esc(t('c_'+k))+'" style="width:'+(v/csum*100)+'%;background:var(--c-'+k+',var(--mut))"></i>').join('')+'</div>'
 +'<div class="legend">'+ce.map(([k,v])=>'<div class="r"><span class="k" style="background:var(--c-'+k+',var(--mut))"></span><span>'+t('c_'+k)+'</span><span class="num">'+money(v,c)+'</span><span class="p num">'+Math.round(v/csum*100)+'%</span></div>').join('')+'</div></div></div>'
 +(mrows.length>1?'<details class="card" data-jd="'+j+'"'+(open?' open':'')+'><summary>'+t('byModelT')+'<span class="tag">'+mrows.reduce((a,x)=>a+x.count,0)+'</span>'+ic('chev','chev')+'</summary><div class="dbody"><div class="bdr head mut"><span></span><span>'+t('mdlCost')+'</span><span>'+t('mdlPrice')+'</span><span>'+t('q_total')+'</span></div>'
 +mrows.map((x,i)=>'<div class="bdr"><span class="n">'+esc(modelName(x,i))+' <span class="mut">×'+x.count+' · '+esc(t('plateN',{n:x.plate+1}))+'</span></span><span class="num mut">'+money(x.unit,c)+'</span><span class="num">'+money(x.gross,c)+'</span><b class="num">'+money(x.total,c)+'</b></div>').join('')
 +'<p class="note">'+t('byModelP')+'</p></div></details>':'')
 +'<div class="card qcard"><div class="actions">'+chSelect()+'<button class="btn" data-jq="'+j+'">'+t('quoteBtn')+'</button><button class="btn ghost" data-js="'+j+'">'+t('qSave')+'</button><span class="mut qmsg" data-jqm="'+j+'"></span></div></div></div>';
}
function allPlatesView(){
 const ready=plist.filter(p=>p.ready),c=(ready[0]||{}).currency||'EUR',ids=jobIds(),top=Math.min(9,Math.max(1,...ids)+1);
 chips('');$('#cfg').innerHTML='';
 const names={};ready.forEach(p=>{if(p.jobName)names[jobOf(p)]=p.jobName;});
 const open=new Set([...document.querySelectorAll('details[data-jd]')].filter(x=>x.open).map(x=>+x.dataset.jd));
 const opts=sel=>{let o='';for(let n=1;n<=top;n++)o+='<option value="'+n+'"'+(n===sel?' selected':'')+'>'+esc(t('jobN',{n})+(names[n]?' · '+names[n]:''))+'</option>';return o;};
 $('#out').innerHTML='<div class="stack"><div class="card"><div class="title">'+t('allBtn')+'</div><div class="pick">'
 +ready.map(p=>'<div class="prow"><span>'+esc(plateName(p))+(p.item?' — '+esc(p.item):'')+'</span><span class="mut num">'+money(p.total,p.currency)+'</span><span class="inp"><select data-pj="'+p.index+'">'+opts(jobOf(p))+'</select></span></div>').join('')
 +'</div><p class="note">'+t('jobP')+'</p></div>'+ids.map(j=>jobSection(j,jobRows(j),c,open.has(j))).join('')+'</div>';
 document.querySelectorAll('[data-pj]').forEach(i=>i.onchange=()=>orca.postMessage({type:'plate_job',index:+i.dataset.pj,job:+i.value}));
 document.querySelectorAll('[data-jn]').forEach(i=>i.onchange=()=>orca.postMessage({type:'job_name',job:+i.dataset.jn,name:i.value}));
 document.querySelectorAll('[data-jm]').forEach(i=>i.onchange=()=>{const m=Number(i.value);if(i.value!==''&&m>=0&&m<=95)orca.postMessage({type:'job_margin',job:+i.dataset.jm,margin:m});});
 document.querySelectorAll('[data-jq]').forEach(b=>b.onclick=()=>{copyText(quoteTextOf(jobQuote(+b.dataset.jq,QCH)));const m=document.querySelector('[data-jqm="'+b.dataset.jq+'"]');if(m)m.textContent=t('copied');});
 document.querySelectorAll('[data-js]').forEach(b=>b.onclick=()=>orca.postMessage({type:'quote_save',quote:jobQuote(+b.dataset.js,QCH)}));bindChSelect();
}
function show(d){
 if(setLang(d.lang)&&!last&&!['result','status','noplate'].includes(d.type))empty();
 if(d.type==='report'){const m=$('#repMsg');$('#rep').disabled=false;
  m.innerHTML=(d.path?esc(t('repDone',{p:d.path})):esc(t('repNoG')))+(d.error?'<br>'+esc(d.error):'')
   +(d.opened?'':'<br>'+esc(t('repLink'))+' <a href="'+esc(d.url)+'" target="_blank">github.com/Bitrute/print-calculator/issues</a>');return;}
 if(d.type==='quotes'){QUOTES=d.quotes||[];renderQuotes(d.saved?t('qSaved'):'');if(d.saved)document.querySelectorAll('#qMsg,.qmsg').forEach(m=>{m.textContent=t('qSaved');});return;}
 if(d.type==='exported'){renderQuotes(d.error?d.error:t(d.opened?'qsFile':'qsFileOpen',{p:d.path}));return;}
 if(d.type==='spoolman'){SPOOL=d.items||[];addSpool();const m=$('#spMsg');if(m)m.textContent=d.error?t('spoolErr',{e:d.error}):SPOOL.length?t('spoolOk',{n:SPOOL.length}):t('spoolNone');return;}
 if(d.type==='update'){$('#upd').innerHTML='<div class="alert upd">'+ic('warn')+'<div><b>'+esc(t('upd_new',{v:d.version}))+'</b></div></div>';return;}
 if(d.type==='reset'){last=null;allView=false;plist=[];$('#plates').innerHTML='';empty();return;}
 if(d.type==='sliced'){if(!allView&&(d.current==null||d.plate.index===d.current))loading();orca.postMessage({type:'compute'});return;}
 if(d.plates)plist=d.plates;if(d.plate)curIdx=d.plate.index;
 if(d.type==='plates'){if(last)last.plates=d.plates;plates({plates:plist,plate:{index:curIdx}});if(allView)allPlatesView();return;}
 if(d.plates)plates(d);
 if(allView&&['result','noplate'].includes(d.type)){if(d.type==='result'){sel=d.selected??null;last=d;}allPlatesView();return;}
 if(d.type==='noplate'){last=null;$('#cfg').innerHTML='';chips('');$('#out').innerHTML='<div class="card empty"><h2>'+t('notSliced',{n:esc(plateName(d.plate))})+'</h2><p>'+t('notSlicedP')+'</p>'+scanBox()+'</div>';bindScan();return;}
 if(d.type==='status'){alertBox(d);if(!last){$('#out').insertAdjacentHTML('beforeend','<div class="card empty" style="margin-top:14px">'+scanBox()+'</div>');bindScan();}return;}
 if(d.type!=='result')return;
 sel=d.selected??null;last=d;
 if(needSetup(d)){setupView(d);renderSettings(d,true);return;}
 render();renderSettings(d);
}
// Configuração inicial: a impressora e cada filamento da placa têm de ser confirmados uma vez (ficam guardados ao guardar);
// até lá o preço não aparece, porque os valores de referência podem estar noutra moeda ou não ser os do utilizador.
function needSetup(d){return !!(d.printer&&!(d.profile.source&&d.profile.source.kind==='custom'))||d.filaments.some(f=>f.key&&f.source!=='custom');}
function setupView(d){
 chips('<span class="chip">'+ic('printer','',13)+esc(d.printer||t('noPrinter'))+'</span>');
 $('#out').innerHTML='<div class="alert">'+ic('gear')+'<div><b>'+t('setupT')+'</b><div class="mut">'+esc(t('setupP'))+'</div></div></div>';
}
function render(){
 const d=last,c=d.currency,{cur}=current();
 const tm=new Date().toLocaleTimeString(LOC,{hour:'2-digit',minute:'2-digit'});
 chips('<span class="chip">'+ic('printer','',13)+esc(d.printer||t('noPrinter'))+'</span><span class="chip"><span class="dot"></span>'+t('updated',{t:tm})+'</span>');
 const grams=d.filaments.reduce((a,f)=>a+f.grams,0),max=Math.max(...d.filaments.map(f=>f.grams),1);
 const costs=Object.entries(d.costs),sum=costs.reduce((a,[,v])=>a+v,0)||1;
 $('#out').innerHTML='<div class="stack"><div class="hero"><div class="card price-card"><div id="hp">'+heroInner()+'</div>'
 +'<div class="custom"><div class="crow"><label for="mn">'+t('customM')+'</label><span class="inp"><input id="mn" class="num" type="number" min="0" max="95" step="any"><em>%</em></span></div>'
 +'<input id="mr" type="range" min="0" max="95" step="1"><div class="ticks">'+[0,25,50,75,95].map(m=>'<span style="left:'+(m/95*100)+'%">'+m+'%</span>').join('')+'</div></div></div>'
 +'<div class="kpis"><div class="kpi">'+ic('clock')+'<div><b class="num">'+dur(d.seconds)+'</b><span>'+t('time')+'</span></div></div><div class="kpi">'+ic('weight')+'<div><b class="num">'+num(grams)+' g</b><span>'+t(mixed(d)?'gramsSet':'grams')+'</span></div></div>'
 +'<div class="kpi">'+ic('copies')+'<div><b class="num">'+partsOf(d)+'</b><span>'+t(mixed(d)?'partsN':d.copies>1?'copyN':'copy1')+'</span></div></div><div class="kpi">'+ic('coin')+'<div><b class="num">'+money(d.unit,c)+'</b><span>'+t(mixed(d)?'unitSet':'unit')+'</span></div></div></div></div>'
 +'<div class="cols"><div class="card"><div class="title">'+t('fils')+'<span class="tag">'+d.filaments.length+' '+t(d.filaments.length===1?'color1':'colorN')+'</span></div>'
 +d.filaments.map(f=>'<div class="fil"><div class="sw" style="background:'+esc(f.color||'#888')+'"></div><div style="min-width:0"><div class="n">'+esc(f.name)+'</div><div class="m">'+(f.type?'<span class="tag">'+esc(f.type)+'</span>':'')+'<span>'+money(f.price,c)+t('perKg')+'</span><span>· '+esc(srcText(f.source))+'</span>'+(warnRef(d,f.source)?refTag():'')+'</div><div class="meter"><i style="width:'+(f.grams/max*100)+'%;background:'+esc(f.color||'var(--ac)')+'"></i></div></div><div class="g"><b class="num">'+num(f.grams)+' g</b><span class="num">'+money(f.price*f.grams/1000,c)+'</span></div></div>').join('')
 +(d.labelled===false?'<p class="note warnTxt">'+esc(t('noLabels'))+'</p>':'')
 +(d.tower!=null?'<div class="tower">'+ic('tower')+'<span>'+t('tower')+'</span><b class="num">'+num(d.tower)+' g</b></div>':'')
 +(d.filaments.some(f=>warnRef(d,f.source))||warnRef(d,d.profile.source)?'<p class="note warnTxt">'+esc(t('refNote',{c:d.currency}))+'</p>':'')+'</div>'
 +'<div class="card"><div class="title">'+t(mixed(d)?'costSetT':'costT')+'<span class="num" style="text-transform:none;letter-spacing:0;color:var(--fg)">'+money(d.unit,c)+'</span></div><div class="bar">'+costs.map(([k,v])=>'<i title="'+esc(t('c_'+k))+'" style="width:'+(v/sum*100)+'%;background:var(--c-'+k+',var(--mut))"></i>').join('')+'</div>'
 +'<div class="legend">'+costs.map(([k,v])=>'<div class="r"><span class="k" style="background:var(--c-'+k+',var(--mut))"></span><span>'+t('c_'+k)+'</span><span class="num">'+money(v,c)+'</span><span class="p num">'+Math.round(v/sum*100)+'%</span></div>').join('')+'</div>'
 +'<div class="total"><span>'+t('profitT')+'</span><span class="num" id="pf" style="color:var(--ac)"></span></div></div></div>'+breakdownCard(d)+channelsCard(d)+extrasCard(d)+quoteCard(d)+'</div>';
 $('#mr').oninput=e=>pick(+e.target.value,'range');
 $('#mn').oninput=e=>{const v=Number(e.target.value);if(e.target.value===''||!isFinite(v))return;pick(Math.max(0,Math.min(95,v)),'number');};
 $('#mn').onchange=()=>refresh();
 bindExtras();
 $('#qOne').onclick=()=>copyText(quoteTextOf(plateQuote(QCH)));
 if($('#qAll'))$('#qAll').onclick=()=>copyText(jobIds().map(j=>quoteTextOf(jobQuote(j,QCH))).join('\n\n'));
 $('#qSave').onclick=()=>orca.postMessage({type:'quote_save',quote:plateQuote(QCH)});bindChSelect();
 refresh();
}
function tfield(key,label,value,ph,cls){return '<label class="'+(cls||'')+'">'+label+'<span class="inp"><input data-scope="general" data-key="'+key+'" data-text="1" value="'+esc(value||'')+'" placeholder="'+esc(ph||'')+'"></span></label>';}
function chRows(list,sym){return list.map((c,i)=>'<div class="exrow chrow"><span class="inp"><input data-ch="name" value="'+esc(c.name)+'" placeholder="'+esc(t('chName'))+'" maxlength="40"></span><span class="inp"><input class="num" data-ch="pct" type="number" min="0" max="45" step="any" value="'+esc(c.pct)+'"><em>%</em></span><span class="inp"><input class="num" data-ch="fixed" type="number" min="0" step="any" value="'+esc(c.fixed)+'"><em>'+esc(sym)+'</em></span><button class="btn ghost x" data-chdel="'+i+'">×</button></div>').join('');}
function readCh(){return [...document.querySelectorAll('#chs .chrow')].map(r=>({name:r.querySelector('[data-ch=name]').value,pct:Number(r.querySelector('[data-ch=pct]').value)||0,fixed:Number(r.querySelector('[data-ch=fixed]').value)||0}));}
// filamentos do Spoolman: os do mesmo material primeiro; escolher um põe o preço por kg no campo
function spoolSelect(f,c){if(!SPOOL||!SPOOL.length)return '';const mt=String(f.type||'').toLowerCase(),list=SPOOL.slice().sort((a,b)=>(b.material.toLowerCase()===mt)-(a.material.toLowerCase()===mt)||a.name.localeCompare(b.name));
 return '<span class="inp spsel"><select data-sp="'+esc(f.key)+'"><option value="">'+esc(t('spoolPick'))+'</option>'+list.map(x=>'<option value="'+x.perKg+'">'+esc(x.name+(x.material?' · '+x.material:'')+' · '+money(x.perKg,c)+t('perKg'))+'</option>').join('')+'</select></span>';}
// a lista do Spoolman entra ao lado de cada filamento sem refazer as definições (o que ainda não foi guardado fica)
function addSpool(){if(!last)return;document.querySelectorAll('#cfg [data-scope=filament]').forEach(i=>{const lab=i.closest('label'),old=lab.querySelector('.spsel');if(old)old.remove();
 lab.insertAdjacentHTML('beforeend',spoolSelect((last.filaments||[]).find(f=>f.key===i.dataset.key)||{key:i.dataset.key},last.currency));});bindSpool();}
function bindSpool(){document.querySelectorAll('[data-sp]').forEach(x=>x.onchange=()=>{if(!x.value)return;const i=[...document.querySelectorAll('#cfg [data-scope=filament]')].find(e=>e.dataset.key===x.dataset.sp);if(i)i.value=x.value;});}
function renderSettings(d,force){
 const g=d.general,p=d.profile,c=d.currency,sym=symbol(c),open=force||$('#cfg details')?.open;
  $('#cfg').innerHTML='<details class="card"'+(open?' open':'')+'><summary>'+ic('gear')+t('cfgT')+'<span class="mut" style="font-weight:400;font-size:12px">'+t('cfgS')+'</span>'+ic('chev','chev')+'</summary><div class="cfg">'
 +'<div><h3>'+ic('tag','',15)+t('general')+'</h3><div class="form">'
 +'<label>'+t('cfg_language')+'<span class="inp"><select data-scope="general" data-key="language">'+langOptions(LANGS,g.language||'auto',esc(t('cfg_auto')))+'</select></span></label>'
 +'<label class="span2">'+t('cfg_currency')+'<span class="cur"><span class="inp"><input id="cq" placeholder="'+esc(t('cfg_search'))+'"></span><span class="inp"><select data-scope="general" data-key="currency" id="ccur">'+currencyOptions(curList(),'',c)+'</select></span></span></label>'
 +['vat','margin','failure','electricity','rate','setup','finish','pack','shipping','efficiency','minimum','discount_qty','discount_pct'].map(k=>field('general',k,t('cfg_'+k),g[k]??0,'')).join('')
 +tfield('q_name',t('cfg_q_name'),g.q_name,'','span2')+tfield('q_footer',t('cfg_q_footer'),g.q_footer,'','span2')
 +'<label>'+t('cfg_rounding')+'<span class="inp"><select data-scope="general" data-key="rounding">'+roundOptions(g.rounding||'none',esc(t('cfg_round_none')),esc(t('cfg_round_99')),LOC)+'</select></span></label>'
 +'<label class="chk span2"><input type="checkbox" data-scope="general" data-key="updates"'+(g.updates===false?'':' checked')+'>'+t('cfg_updates')+'</label>'+'</div><p class="note">'+t('eff_note')+'</p></div>'
 +'<div><h3>'+ic('printer','',15)+esc(d.printer||t('printer'))+'<span class="tag ac">'+esc(srcText(p.source))+'</span>'+(warnRef(d,p.source)?refTag():'')+'</h3><div class="form">'+field('printer','price',t('p_price'),p.price,sym)+field('printer','life',t('p_life'),p.life,t('years'))
 +field('printer','uptime',t('p_uptime'),p.uptime,'%')+field('printer','maintenance',t('p_maint'),p.maintenance,sym)+field('printer','power',t('p_power'),p.power,'W')+field('printer','buffer',t('p_buffer'),p.buffer,'×')+'</div><p class="note">'+t('buf_note')+'</p></div>'
 +'<div><h3>'+ic('spool','',15)+t('fils')+'</h3><div class="form">'+d.filaments.map(f=>field('filament',f.key,esc(f.name),f.price,sym+t('perKg')).replace(/<\/label>$/,spoolSelect(f,c)+'</label>')).join('')
 +tfield('spoolman',t('cfg_spoolman'),g.spoolman,'http://192.168.1.10:7912','span2')+'</div><div class="actions" style="margin-top:8px;align-items:center"><button class="btn ghost" id="spBtn">'+t('spoolBtn')+'</button><span class="mut" id="spMsg"></span></div><p class="note">'+t('f_note')+'</p></div>'
 +'<div><h3>'+ic('coin','',15)+t('chT')+'</h3><div id="chs">'+chRows(d.channels||[],sym)+'</div><div class="actions"><button class="btn ghost" id="chAdd">+ '+t('chAdd')+'</button></div><p class="note">'+t('chNote')+'</p></div>'
 +'<div class="actions"><button class="btn" id="save">'+t('save')+'</button><button class="btn ghost" id="reset">'+t('reset')+'</button></div></div></details>';
 $('#save').onclick=()=>{const out={type:'save',printer:d.printer,plate:d.plate.index,general:{},profile:{},filaments:{}};document.querySelectorAll('#cfg [data-scope]').forEach(i=>{const k=i.dataset.key,sc=i.dataset.scope;if(sc==='general'){if(k==='updates')out.general[k]=i.checked;else if(k==='currency'||k==='language'||k==='rounding'||i.dataset.text)out.general[k]=i.value;else if(i.value!==''&&isFinite(Number(i.value)))out.general[k]=Number(i.value);}else{const v=Number(i.value);if(sc==='printer')out.profile[k]=v;else if(needSetup(d)||i.dataset.orig!==i.value)out.filaments[k]=v;}});out.channels=readCh();loading();orca.postMessage(out);};
 const chBind=()=>{document.querySelectorAll('[data-chdel]').forEach(b=>b.onclick=()=>{const l=readCh();l.splice(+b.dataset.chdel,1);$('#chs').innerHTML=chRows(l,sym);chBind();});};chBind();
 $('#chAdd').onclick=()=>{$('#chs').innerHTML=chRows(readCh().concat([{name:'',pct:0,fixed:0}]),sym);chBind();const n=document.querySelectorAll('#chs [data-ch=name]');if(n.length)n[n.length-1].focus();};
 $('#spBtn').onclick=()=>{$('#spMsg').textContent=t('reading');orca.postMessage({type:'spoolman',url:$('#cfg [data-key=spoolman]').value});};
 bindSpool();
 $('#reset').onclick=()=>{loading();orca.postMessage({type:'reset_printer',printer:d.printer,plate:d.plate.index});};
 $('#cq').oninput=e=>{const s=$('#ccur');s.innerHTML=currencyOptions(curList(),e.target.value,s.value);};
}
$('#sub').textContent=t('sub');document.documentElement.lang=LANG;
$('#rep').textContent=t('repBtn');$('#rep').onclick=()=>{$('#rep').disabled=true;$('#repMsg').textContent=t('repBusy');orca.postMessage({type:'report'});};
empty();orca.onMessage(show);orca.postMessage({type:'ready'});
</script></body></html>"""


CONFIG_HTML = r"""<style>
*{box-sizing:border-box}body{margin:0;padding:14px 16px;background:var(--orca-bg);color:var(--orca-fg);font:13px var(--orca-font,system-ui,sans-serif)}
.form{display:grid;grid-template-columns:1fr 1fr;gap:10px}label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--orca-muted)}label.wide{grid-column:1/-1}
input,select{padding:6px 8px;border-radius:6px;border:1px solid var(--orca-border);background:var(--orca-bg);color:var(--orca-fg);font:inherit;width:100%}
p{color:var(--orca-muted);font-size:12px;line-height:1.4}h3{margin:0 0 6px}.cur{display:grid;grid-template-columns:1fr 2fr;gap:6px}
</style><h3>Print Calculator</h3><p id="intro"></p>
<div class="form" id="f"></div><p id="ro" hidden></p>
<script>
(function(){
 var I18N=__I18N__,LANGS=__LANGS__,SYS="__LANG__",defaults=__DEFAULTS__;
 __CURRENCY_JS__
 var fields=["vat","margin","failure","electricity","rate","setup","finish","pack","efficiency","minimum","discount_qty","discount_pct"],full={},settings=Object.assign({},defaults),f=document.getElementById("f"),lang="en";
 function T(k){return (I18N[lang]||{})["cfg_"+k]||I18N.en["cfg_"+k]||k;}
 function build(){
  lang=I18N[settings.language]?settings.language:(I18N[SYS]?SYS:"en");var list=currencyList(locOf(lang));document.documentElement.lang=lang;
  document.getElementById("intro").textContent=T("intro");document.getElementById("ro").textContent=T("ro");
  f.innerHTML='<label class="wide">'+T("language")+'<select data-key="language">'+langOptions(LANGS,settings.language,T("auto"))+'</select></label>'
   +'<label class="wide">'+T("currency")+'<span class="cur"><input id="q" placeholder="'+T("search")+'"><select data-key="currency" id="cur">'+currencyOptions(list,"",settings.currency)+'</select></span></label>'
   +fields.map(function(k){return '<label>'+T(k)+'<input type="number" min="0" step="any" data-key="'+k+'" value="'+settings[k]+'"></label>';}).join("")
   +'<label>'+T("rounding")+'<select data-key="rounding">'+roundOptions(settings.rounding,T("round_none"),T("round_99"),locOf(lang))+'</select></label>'
   +'<label class="wide" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" data-key="updates" style="width:auto"'+(settings.updates===false?'':' checked')+'>'+T("updates")+'</label>';
  var ro=!!((window.orca.getContext&&window.orca.getContext())||{}).readOnly;document.getElementById("ro").hidden=!ro;
  f.querySelectorAll("input,select").forEach(function(i){i.disabled=ro;});
  var q=document.getElementById("q"),sel=document.getElementById("cur");
  q.addEventListener("input",function(){sel.innerHTML=currencyOptions(list,q.value,sel.value);});
  f.querySelectorAll("[data-key]").forEach(function(i){i.addEventListener("change",function(){
   var k=i.dataset.key;if(k==="updates")settings[k]=i.checked;else if(k==="currency"||k==="language"||k==="rounding")settings[k]=i.value;else{var n=Number(i.value);if(!isFinite(n)||i.value===""){build();return;}settings[k]=n;}
   full=Object.assign({},full,settings);window.orca.saveConfig(full);if(k==="language")build();
  });});
 }
 function render(config){
  full=(config&&typeof config==="object")?config:{};settings=Object.assign({},defaults);
  Object.keys(defaults).forEach(function(k){if(typeof full[k]===typeof defaults[k])settings[k]=full[k];});
  build();
 }
 window.orca.onConfig(render);
})();
</script>"""

ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 1024 1024"><defs><linearGradient id="b" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#14c4ad"/><stop offset=".55" stop-color="#009688"/><stop offset="1" stop-color="#005b52"/></linearGradient></defs><rect x="32" y="32" width="960" height="960" rx="220" fill="url(#b)"/><g transform="rotate(-32 512 512)"><path fill="#fff" d="M156 512 L290 340 Q306 320 332 320 L812 320 Q872 320 872 380 L872 644 Q872 704 812 704 L332 704 Q306 704 290 684 Z"/><circle cx="330" cy="512" r="50" fill="#009688"/></g><g fill="#00897b"><rect x="430" y="560" width="290" height="58" rx="29"/><rect x="458" y="478" width="234" height="58" rx="29"/><rect x="490" y="396" width="170" height="58" rx="29"/></g><circle cx="760" cy="760" r="170" fill="#ffb627" stroke="#fff" stroke-width="40"/><path d="M800 700 C770 668 700 676 700 724 C700 776 820 752 820 806 C820 852 748 860 712 826 M760 640 V672 M760 852 V884" fill="none" stroke="#7a4b00" stroke-width="40" stroke-linecap="round"/></svg>'  # versão pequena do ícone do Orca Cloud (sem filtros nem texto, que o OrcaSlicer não desenha)


def icon_path():
    """Ícone do separador (o OrcaSlicer pede um ficheiro); escrito na pasta do plugin se faltar ou for de outra versão."""
    path = os.path.join(os.path.dirname(settings_path()), "print_calculator.svg")
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        current = None
    if current != ICON_SVG:  # em falta ou de uma versão anterior
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(ICON_SVG)
        except OSError:
            return path if current is not None else ""
    return path


SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "BRL": "R$", "JPY": "¥", "CNY": "¥", "INR": "₹", "KRW": "₩", "CHF": "CHF", "AUD": "A$", "CAD": "C$",
           "MXN": "MX$", "PLN": "zł", "TRY": "₺", "UAH": "₴", "ILS": "₪", "VND": "₫", "PHP": "₱", "THB": "฿", "NGN": "₦", "ZAR": "R"}
NO_DECIMALS = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD", "IDR", "COP", "PYG", "UGX", "XAF", "XOF"}


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3]) or (0,)


def check_update(url=UPDATE_URL, current=VERSION, timeout=2.0):
    """Número da versão publicada, se for mais nova do que esta; None sem internet, sem autorização ou se já está atualizado."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            latest = str(json.loads(r.read(4096).decode("utf-8")).get("version") or "")
    except Exception:  # noqa: BLE001 - sem rede, recusado pelo OrcaSlicer ou ficheiro inválido: sem aviso
        return None
    return latest if version_tuple(latest) > version_tuple(current) else None


def norm_language(raw):
    """Código de língua do OrcaSlicer ou do sistema (pt_BR, zh_CN, de_DE, Portuguese_Portugal...) para uma língua do plugin."""
    low = str(raw or "").replace("-", "_").lower()
    if low.startswith(("pt_br", "portuguese_brazil")):
        return "pt_BR"
    if low.startswith(("pt", "portug")):
        return "pt"
    if low.startswith(("zh", "chinese")):
        return "zh_TW" if any(x in low for x in ("tw", "hk", "mo", "hant", "traditional", "taiwan")) else "zh_CN"
    first = low.split("_")[0]
    return next((code for code in I18N if code.lower() == first), "en")


ISSUES_URL = "https://github.com/Bitrute/print-calculator/issues/new"


def report_folder():
    """Pasta onde fica o .zip do relatório: Transferências, senão o Ambiente de trabalho, senão a pasta do plugin."""
    home = os.path.expanduser("~")
    for sub in ("Downloads", "Desktop"):
        folder = os.path.join(home, sub)
        if os.path.isdir(folder):
            return folder
    return os.path.dirname(settings_path())


def report_info(result, orca_version=""):
    """Dados técnicos para o issue (sem nomes de ficheiros nem dados pessoais)."""
    r = result or {}
    lines = [f"Print Calculator: {VERSION}", f"OrcaSlicer: {orca_version or '?'}", f"Printer: {r.get('printer') or '?'}",
             f"Parts counted on the plate: {r.get('copies', '?')}", f"Models on the plate: {len(r.get('breakdown') or []) or 1}",
             f"Filaments: {', '.join(f.get('type') or '?' for f in r.get('filaments') or []) or '?'}", f"Currency: {r.get('currency') or '?'}"]
    return "\n".join(lines)


def issue_url(info, has_file):
    from urllib.parse import quote as q
    body = ("**What went wrong?**\n\n\n**What did you expect?**\n\n\n"
            + ("**G-code:** drag the file Print-Calculator-report-*.zip (in your Downloads folder) here.\n\n" if has_file else "")
            + "---\n```\n" + info + "\n```\n")
    return ISSUES_URL + "?title=" + q("Problem: ") + "&body=" + q(body)


def write_report(gcode, info, folder=None, stamp=None):
    """Junta o G-code e os dados técnicos num .zip e devolve o caminho."""
    import time
    import zipfile
    folder = folder or report_folder()
    path = os.path.join(folder, f"Print-Calculator-report-{stamp or time.strftime('%Y%m%d-%H%M%S')}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(gcode, "plate.gcode")
        z.writestr("info.txt", info + "\n")
    return path


def tr(lang, key, **values):
    """Texto da tabela de traduções (a mesma do separador), em inglês quando falta; {nome} como no separador."""
    s = str((I18N.get(lang) or {}).get(key) or I18N["en"].get(key) or key)
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), "")), s)


def _hours(seconds):
    m = js_round((seconds or 0) / 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def quote_html(q, general, lang="pt"):
    """Página do orçamento guardado, pronta a imprimir ou guardar em PDF no browser."""
    from html import escape as e
    c = q["currency"]
    money = lambda v: e(money_text(v, c, lang))  # noqa: E731
    rows = "".join(f"<tr><td>{e(x['name'])}</td><td class=n>{x['qty']:g}</td><td class=n>{money(x['unit'])}</td><td class=n>{money(x['total'])}</td></tr>"
                   for x in q["lines"])
    foot = f"<tr class=t><td colspan=3>{e(tr(lang, 'q_total'))}</td><td class=n>{money(q['total'])}</td></tr>"
    if q["shipping"] > 0:
        foot += (f"<tr><td colspan=3>{e(tr(lang, 'q_ship'))}</td><td class=n>{money(q['shipping'])}</td></tr>"
                 f"<tr class=t><td colspan=3>{e(tr(lang, 'q_grand'))}</td><td class=n>{money(q['total'] + q['shipping'])}</td></tr>")
    info = [tr(lang, "qh_vat", v=f"{q['vat']:g}")] if q["vat"] > 0 else []
    if q["seconds"]:
        info.append(tr(lang, "q_time") + ": " + _hours(q["seconds"]))
    if q["material"]:
        info.append(tr(lang, "q_material") + ": " + q["material"])
    name, note = _text(general.get("q_name"), 120), _text(general.get("q_footer"), 300)
    return ("<!doctype html><html lang='" + e(lang.replace("_", "-")) + "'><head><meta charset='utf-8'><title>" + e(tr(lang, "q_title") + " " + q["title"])
            + "</title><style>body{font:14px system-ui,sans-serif;color:#222;max-width:760px;margin:40px auto;padding:0 20px}"
            "h1{font-size:22px;margin:0}header{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid #009688;padding-bottom:12px;margin-bottom:20px}"
            ".m{color:#666}table{width:100%;border-collapse:collapse}th,td{padding:8px 6px;border-bottom:1px solid #ddd;text-align:left}"
            ".n{text-align:right;white-space:nowrap}tr.t td{font-weight:700}footer{margin-top:28px;color:#555}@media print{body{margin:0}}</style></head>"
            "<body onload='window.print()'><header><div><h1>" + e(tr(lang, "q_title")) + "</h1><div class=m>" + e(q["title"]) + "</div></div><div class='m n'>"
            + (f"<b>{e(name)}</b><br>" if name else "") + e(q["date"]) + "</div></header><table><thead><tr><th>" + e(tr(lang, "qh_item"))
            + "</th><th class=n>" + e(tr(lang, "qh_qty")) + "</th><th class=n>" + e(tr(lang, "qh_unit")) + "</th><th class=n>" + e(tr(lang, "q_total"))
            + "</th></tr></thead><tbody>" + rows + foot + "</tbody></table><p class=m>" + "<br>".join(e(x) for x in info) + "</p>"
            + (f"<footer>{e(note)}</footer>" if note else "") + "</body></html>")


def quotes_csv(quotes, lang="pt"):
    """Todos os orçamentos guardados, uma linha por artigo. Separador e decimais como o Excel da língua escolhida."""
    comma = lang in COMMA_DECIMAL
    sep = ";" if comma else ","
    number = (lambda v: f"{v:.2f}".replace(".", ",")) if comma else (lambda v: f"{v:.2f}")  # noqa: E731

    def cell(v):
        v = str(v)
        if v[:1] in ("=", "+", "-", "@"):  # fórmulas não correm quando o ficheiro abre no Excel
            v = "'" + v
        return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in (sep, '"', "\n")) else v
    head = [tr(lang, k) for k in ("qh_date", "q_title", "qh_item", "qh_qty", "qh_unit", "q_total", "q_ship", "q_grand")] + ["Currency"]
    rows = [sep.join(cell(h) for h in head)]
    for q in quotes:
        for x in q["lines"]:
            rows.append(sep.join([cell(q["date"]), cell(q["title"]), cell(x["name"]), f"{x['qty']:g}", number(x["unit"]), number(x["total"]),
                                  number(q["shipping"]), number(q["total"] + q["shipping"]), q["currency"]]))
    return "\r\n".join(rows) + "\r\n"


def write_export(name, content, folder=None):
    """Guarda um orçamento exportado nas Transferências e devolve o caminho."""
    safe = re.sub(r"[^\w\- .]", "_", name)[:90].strip() or "Print-Calculator"
    path = os.path.join(folder or report_folder(), safe)
    with open(path, "w", encoding="utf-8-sig" if safe.endswith(".csv") else "utf-8", newline="") as f:
        f.write(content)
    return path


def spoolman_filaments(url, timeout=4.0, opener=None):
    """Filamentos do Spoolman com preço por kg (preço da bobina a dividir pelo peso). Só se lê; nada é gravado no Spoolman."""
    import urllib.request
    base = clean_url(url)
    if not base:
        raise ValueError("url")
    with (opener or urllib.request.urlopen)(base + "/api/v1/filament", timeout=timeout) as r:
        data = json.loads(r.read(8 * 1024 * 1024).decode("utf-8"))
    out = []
    for f in data if isinstance(data, list) else []:
        if not isinstance(f, dict):
            continue
        price, weight = _float(f.get("price")), _float(f.get("weight"))
        if price <= 0 or weight <= 0:
            continue
        vendor = f.get("vendor") if isinstance(f.get("vendor"), dict) else {}
        out.append({"name": _text(" ".join(str(x) for x in (vendor.get("name"), f.get("name")) if x), 80), "material": _text(f.get("material"), 20),
                    "color": hex_color(f.get("color_hex")), "perKg": round(price / weight * 1000, 2)})
    return out


def open_link(url):
    """Abre o link no browser do sistema; False se o OrcaSlicer não deixar (o separador mostra o link)."""
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - autorização recusada ou sem browser
        return False


def orca_version():
    try:
        info = orca.host.app_info()
        return str((info.get("version") if isinstance(info, dict) else getattr(info, "version", "")) or "")
    except Exception:  # noqa: BLE001
        return ""


def ui_language():
    """Língua do OrcaSlicer; só se lê na linha principal."""
    lang = ""
    try:
        info = orca.host.app_info()
        lang = (info.get("language") if isinstance(info, dict) else getattr(info, "language", "")) or ""
    except Exception:  # noqa: BLE001 - OrcaSlicer sem app_info
        try:
            import locale
            lang = (locale.getlocale()[0] or "")
        except Exception:  # noqa: BLE001
            lang = ""
    return norm_language(lang)


def text(lang, key, **values):
    return (I18N.get(lang, {}).get("py_" + key) or I18N["en"]["py_" + key]).format(**values)


def _inject(html, lang, keys=None):
    table = I18N if keys is None else {code: {k: v for k, v in d.items() if k.startswith(keys)} for code, d in I18N.items()}
    js = CURRENCY_JS.replace("__CURRENCIES__", json.dumps(CURRENCIES))
    return (html.replace("__CURRENCY_JS__", js).replace("__I18N__", json.dumps(table, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__LANGS__", json.dumps(LANGS, ensure_ascii=False)).replace("__LANG__", lang))


CURRENCY_JS = r"""function locOf(l){return l==="pt"?"pt-PT":String(l||"en").replace("_","-");}
function currencyList(loc){var codes=__CURRENCIES__.slice();try{if(Intl.supportedValuesOf)Intl.supportedValuesOf("currency").forEach(function(c){if(codes.indexOf(c)<0)codes.push(c);});}catch(e){}
 var names=null;try{names=new Intl.DisplayNames([loc],{type:"currency"});}catch(e){}
 return codes.map(function(c){var n=c;try{n=(names&&names.of(c))||c;}catch(e){}return {c:c,n:String(n)};}).sort(function(a,b){return a.n.localeCompare(b.n,loc);});}
function currencyOptions(list,q,cur){q=String(q||"").trim().toLowerCase();
 var shown=list.filter(function(x){return !q||x.c.toLowerCase().indexOf(q)>=0||x.n.toLowerCase().indexOf(q)>=0||x.c===cur;});
 if(!shown.some(function(x){return x.c===cur;}))shown.unshift({c:cur,n:cur});
 return shown.map(function(x){return '<option value="'+x.c+'"'+(x.c===cur?' selected':'')+'>'+x.c+(x.n!==x.c?' — '+x.n.replace(/</g,"&lt;"):"")+'</option>';}).join("");}
function roundOptions(cur,noneText,x99Text,loc){return [["none",noneText],["0.05"],["0.1"],["0.5"],["1"],["99",x99Text]].map(function(x){
 var label=x[1]||Number(x[0]).toLocaleString(loc,{minimumFractionDigits:2});return '<option value="'+x[0]+'"'+(x[0]===cur?' selected':'')+'>'+label+'</option>';}).join("");}
function langOptions(langs,cur,autoText){return '<option value="auto"'+(cur==="auto"?' selected':'')+'>'+autoText+'</option>'+langs.map(function(x){return '<option value="'+x[0]+'"'+(x[0]===cur?' selected':'')+'>'+x[1]+'</option>';}).join("");}"""


def config_page(lang="pt"):
    return _inject(CONFIG_HTML.replace("__DEFAULTS__", json.dumps(DEFAULT_GENERAL)), lang, "cfg_")


def page_html(lang="pt"):
    return _inject(PAGE_HTML, lang)


def money_text(v, currency, lang="pt"):
    """Valor com moeda para as notificações (qualquer código ISO; símbolo para as mais comuns)."""
    digits = 0 if currency in NO_DECIMALS else 2
    n = f"{js_round(v * 10 ** digits) / 10 ** digits:,.{digits}f}"
    sym = SYMBOLS.get(currency, currency)
    if lang in COMMA_DECIMAL:
        return n.replace(",", " ").replace(".", ",") + " " + sym
    return sym + ("" if len(sym) == 1 else " ") + n


def price_at(result, margin):
    """Preço de um resultado com outra margem (a mesma conta de quote)."""
    return sale_price(result["unit"], margin, result["copies"], result["general"])


def price_notice(result, lang="pt"):
    """Nível e texto da notificação do OrcaSlicer depois de cada cálculo."""
    if result.get("type") != "result":
        return "WarningNotificationLevel", "Print Calculator: " + str(result.get("text") or "")
    c = result["currency"]
    chosen = result["chosen"] if result.get("selected") is None else price_at(result, result["selected"])
    if len(result.get("breakdown") or []) > 1:  # modelos diferentes: o preço é o da placa inteira
        return "RegularNotificationLevel", "Print Calculator: " + text(lang, "set", p=money_text(chosen["gross"], c, lang),
                                                                       n=sum(x["count"] for x in result["breakdown"]))
    msg = "Print Calculator: " + text(lang, "unit", p=money_text(chosen["gross"], c, lang))
    if result["copies"] > 1:
        msg += text(lang, "plate", n=result["copies"], t=money_text(chosen["totalGross"], c, lang))
    return "RegularNotificationLevel", msg


def notify(level_name, text):
    ui = getattr(getattr(orca, "host", None), "ui", None)
    push, level = getattr(ui, "push_notification", None), getattr(getattr(ui, "NotificationLevel", None), level_name, None)
    if callable(push) and level is not None and text:
        try:
            push(level, text)
        except Exception:  # noqa: BLE001 - a notificação é só um extra
            pass


def host_plate():
    """Placa escolhida no OrcaSlicer (0 = primeira), ou None se o OrcaSlicer não a disser."""
    try:
        index = int(orca.host.model().current_plate_index)
    except Exception:  # noqa: BLE001 - versões sem esta função: fica a placa do último aviso PlateSelected
        return None
    return index if index >= 0 else None


def friendly_error(err, lang="pt"):
    for kind, key in ((PermissionError, "perm"), (FileNotFoundError, "missing"), (ValueError, "empty")):
        if isinstance(err, kind):
            return {"type": "status", "level": "warn", "text": text(lang, key)}
    return {"type": "status", "level": "err", "text": text(lang, "fail", e=err)}


def compute(path, settings_file=None, config=None, settings=None):
    job = parse_gcode(read_gcode(path))
    result = quote(job, settings or load_settings(settings_file, config))
    result["type"] = "result"
    return result


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def find_session_gcodes(pid=None, temp=None, now=None):
    """G-code que o OrcaSlicer já gerou nesta sessão, do mais recente para o mais antigo:
    <temp>/orcaslicer_model/<Sun_Oct_25>/<hora>#<pid>#<n>/Metadata/.<pid>.<n>.gcode.
    O último número do nome é o da placa (0 = primeira); ver session_plates.
    Só abre as pastas de hoje e de ontem, para o OrcaSlicer perguntar o mínimo."""
    import time
    pid = os.getpid() if pid is None else pid
    base = os.path.join(temp or os.environ.get("TEMP") or os.environ.get("TMP") or "", "orcaslicer_model")
    now = time.time() if now is None else now
    found = []
    for day in (now, now - 86400):
        t = time.localtime(day)
        folder = os.path.join(base, f"{_DAYS[t.tm_wday]}_{_MONTHS[t.tm_mon - 1]}_{t.tm_mday:02d}")
        try:
            sessions = [n for n in os.listdir(folder) if f"#{pid}#" in n]
        except FileNotFoundError:
            continue
        for name in sessions:
            meta = os.path.join(folder, name, "Metadata")
            try:
                files = os.listdir(meta)
            except FileNotFoundError:
                continue
            found += [os.path.join(meta, f) for f in files if re.fullmatch(rf"\.{pid}\.\d+\.gcode", f, re.I)]
        if found:
            break
    return sorted(found, key=os.path.getmtime, reverse=True)


def session_plates(found):
    """{placa: ficheiro} da pasta do projeto aberto (a do G-code mais recente). O OrcaSlicer guarda o G-code da placa N
    em .<pid>.<N>.gcode, com N a começar em 0."""
    out = {}
    for path in found:
        m = re.search(r"\.(\d+)\.gcode$", os.path.basename(path), re.I)
        if m and os.path.dirname(path) == os.path.dirname(found[0]):
            out.setdefault(int(m.group(1)), path)
    return out


class PlateBook:
    """Preço de cada placa fatiada. O OrcaSlicer fatia uma placa de cada vez e avisa quando outra placa é escolhida.
    O G-code pertence à placa que estava escolhida quando o fatiamento começou. O OrcaSlicer reutiliza os mesmos ficheiros
    temporários para placas diferentes, por isso cada placa guarda os dados lidos e não volta a ler o ficheiro."""

    def __init__(self, load, price, fail, recall=None):
        self.load = load    # caminho do G-code -> dados do trabalho (pode falhar)
        self.price = price  # dados do trabalho, extras -> resultado com as definições atuais
        self.fail = fail    # erro -> mensagem para o separador
        self.recall = recall or (lambda job: {})  # dados do trabalho -> margem e extras lembrados
        self.reset()

    def reset(self):
        self.plates, self.names, self.margins, self.extras, self.current, self.slicing = {}, {}, {}, {}, 0, None
        self.jobs, self.job_names = {}, {}  # placa -> trabalho (1 por omissão); trabalho -> nome

    def set_job(self, index, job):
        self.jobs[int(index)] = int(max(1, min(MAX_JOBS, _num(job, None, 1))))

    def set_job_name(self, job, name):
        self.job_names[int(_num(job, None, 1))] = " ".join(str(name or "").split())[:60]

    def set_job_margin(self, job, margin):
        """A margem do trabalho passa a ser a de todas as suas placas. Devolve essas placas."""
        job = int(_num(job, None, 1))
        done = [i for i in self.plates if self.jobs.get(i, 1) == job]
        for i in done:
            self.set_margin(i, margin)
        return done

    def set_extras(self, index, extras):
        index = int(index)
        self.extras[index] = clean_extras(extras)
        entry = self.plates.get(index)
        if entry and entry["job"] is not None:
            entry["result"] = self.price(entry["job"], self.extras[index])

    def set_margin(self, index, margin):
        self.margins[int(index)] = _num(margin, "margin")

    def select(self, index, name=""):
        self.current = int(index)
        self.names[self.current] = str(name or "")
        return self.current

    def slice_started(self):
        self.slicing = self.current

    def imported(self, jobs, name):
        """Placas de um ficheiro importado: substituem as do separador e já vêm lidas."""
        self.reset()
        for index, job in sorted(jobs.items()):
            self.plates[index] = {"path": "", "job": job, "result": None}
            self.names[index] = _text(name, 60)
        self.current = min(jobs)
        return self.current

    def sliced(self, path, index=None):
        if index is None:
            index = self.current if self.slicing is None else self.slicing
        self.slicing = None
        path = str(path)
        for other in [i for i, e in self.plates.items() if i != index and e["path"] == path and e["job"] is None]:
            del self.plates[other]  # o ficheiro já tem outra placa: esta tem de ser fatiada de novo
        self.plates[index] = {"path": path, "job": None, "result": None}
        return index

    def compute(self, only=None, force=False):
        """Lê as placas ainda não lidas e calcula as que não têm preço (ou todas, com force). Devolve as placas calculadas agora."""
        done = []
        for index, entry in sorted(self.plates.items()):
            if only is not None and index != only:
                continue
            if entry["job"] is None:
                try:
                    entry["job"] = self.load(entry["path"])
                except Exception as err:  # noqa: BLE001 - mostrado no separador
                    entry["result"] = self.fail(err)
                    done.append(index)
                    continue
                entry["result"] = None
                memory = self.recall(entry["job"]) or {}
                if index not in self.margins and memory.get("margin") is not None:
                    self.margins[index] = _num(memory["margin"], "margin")
                if index not in self.extras and memory.get("extras"):
                    self.extras[index] = clean_extras(memory["extras"])
            if force or entry["result"] is None:
                entry["result"] = self.price(entry["job"], self.extras.get(index, []))
                done.append(index)
        return done

    def summary(self):
        out, generals = [], {}
        for index, entry in sorted(self.plates.items()):
            r = entry["result"] or {}
            ready = r.get("type") == "result"
            margin = self.margins.get(index)
            chosen = None if not ready else (r["chosen"] if margin is None else price_at(r, margin))
            total = None if not ready else chosen["totalGross"]
            job, plain, used = self.jobs.get(index, 1), None, None
            if ready:  # preço sem mínimo nem arredondamento: esses contam uma vez no total do trabalho
                used = r["margin"] if margin is None else margin
                generals[job] = {**DEFAULT_GENERAL, **r["general"]}
                plain = sale_price(r["unit"], used, r["copies"], {**generals[job], "minimum": 0, "rounding": "none"})["totalGross"]
            models = []
            if ready and len(r.get("breakdown") or []) > 1:  # modelos diferentes: cada um com a margem desta placa
                for x in r["breakdown"]:
                    sp = sale_price(x["unit"], r["margin"] if margin is None else margin, x["count"], {**DEFAULT_GENERAL, **r["general"]})
                    models.append({"name": x["name"], "count": x["count"], "unit": x["unit"], "gross": sp["gross"], "total": sp["totalGross"]})
            elif ready:
                models.append({"name": r.get("item", ""), "count": r["copies"], "unit": r["unit"], "gross": chosen["gross"], "total": total})
            out.append({"index": index, "name": self.names.get(index, ""), "ready": ready, "total": total,
                        "gross": chosen["gross"] if ready else None, "copies": r.get("copies"), "item": r.get("item", ""),
                        "cost": r["unit"] * r["copies"] if ready else None, "currency": r.get("currency"), "models": models,
                        "job": job, "jobName": self.job_names.get(job, ""), "plain": plain, "margin": used, "seconds": r.get("seconds"),
                        "vat": _num(r["general"].get("vat"), "vat") if ready else None,
                        "filaments": [{"name": f["name"], "type": f["type"], "color": f["color"], "grams": f["grams"] * r["copies"],
                                       "cost": f["price"] * f["grams"] / 1000 * r["copies"] * _num(r["general"].get("efficiency"), "efficiency", 1.0)}
                                      for f in r["filaments"]] if ready else [],
                        "costs": {k: v * r["copies"] for k, v in r["costs"].items()} if ready else {}})
        for job, general in generals.items():  # uma placa: o preço dela; várias: a soma, com mínimo e arredondamento uma vez
            rows = [e for e in out if e["job"] == job and e["ready"]]
            total = rows[0]["total"] if len(rows) == 1 else job_total(sum(e["plain"] for e in rows), general)
            for e in rows:
                e["jobTotal"] = total
        return out

    def payload(self, index=None):
        index = self.current if index is None else index
        entry = self.plates.get(index)
        base = {"plate": {"index": index, "name": self.names.get(index, "")}, "plates": self.summary()}
        if entry is None or entry["result"] is None:
            return {**base, "type": "noplate" if entry is None else "sliced"}
        return {**entry["result"], **base, "selected": self.margins.get(index)}


def apply_save(msg, settings):
    """Aplica o que o separador enviou (perfil da impressora e preços de filamento) e devolve as definições."""
    s = {**settings, "general": {**settings["general"], **clean_general(msg.get("general"))}, "printers": dict(settings["printers"]),
         "filaments": dict(settings["filaments"])}
    name = str(msg.get("printer") or "")
    if name and msg.get("profile"):
        s["printers"][name] = _clean_printers({name: msg["profile"]})[name]
    if isinstance(msg.get("channels"), list):
        s["channels"] = clean_channels(msg["channels"])
    s["filaments"].update(_clean_filaments(msg.get("filaments")))
    return s


def reset_printer(name, settings):
    s = {**settings, "printers": dict(settings["printers"])}
    s["printers"].pop(str(name or ""), None)
    return s


if orca is not None:
    class PrintCalculatorPage(orca.pages.PagesPluginCapabilityBase):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._book = PlateBook(load_job, self._price, lambda err: friendly_error(err, self.lang()), self._recall)
            self._page = False  # o separador já foi aberto e recebe mensagens
            self._hinted = False
            self._lang = None
            self._update_checked = False

        def get_name(self):
            return "Print Calculator"

        def lang(self):
            """Língua escolhida nas definições; "auto" segue o OrcaSlicer."""
            chosen = self._settings()["general"].get("language", "auto")
            if chosen in I18N:
                return chosen
            if self._lang is None:
                self._lang = ui_language()
            return self._lang

        def get_ui(self):
            return page_html(self.lang())

        def get_icon(self):
            return icon_path()

        # Definições no gestor de plugins do OrcaSlicer; o OrcaSlicer guarda tudo e mantém-no entre atualizações do plugin
        def has_config_ui(self):
            return True

        def get_config_ui(self):
            return config_page(self.lang())

        def get_default_config(self):
            return {**DEFAULT_GENERAL, "printers": {}, "filaments": {}}

        def _config(self):
            try:
                return json.loads(self.get_config())
            except Exception:  # noqa: BLE001 - OrcaSlicer sem definições guardadas
                return {}

        def _settings(self):
            return load_settings(config=self._config())

        def _store(self, settings):
            """Guarda no OrcaSlicer; se não der, fica no settings.json da pasta do plugin."""
            try:
                if self.save_config(json.dumps(to_config(settings))) is not False:
                    return
            except Exception:  # noqa: BLE001 - OrcaSlicer sem save_config
                pass
            try:
                save_settings(settings)
            except OSError:
                pass

        def _check_update(self):
            """Uma vez por sessão: ao abrir o separador ou no primeiro fatiamento, o que vier primeiro."""
            if self._update_checked or self._settings()["general"].get("updates") is False:
                return
            self._update_checked = True
            latest = check_update()
            if latest:
                self._send({"type": "update", "version": latest})
                notify("HintNotificationLevel", (I18N.get(self.lang(), {}).get("upd_new") or I18N["en"]["upd_new"]).format(v=latest))

        def _send(self, data):
            try:
                self.post_message({**data, "lang": self.lang()})
            except Exception:  # noqa: BLE001 - separador ainda não aberto
                pass

        def _price(self, job, extras=None):
            return {**quote(job, self._settings(), extras), "type": "result"}

        def _recall(self, job):
            return self._settings()["memory"].get(signature(job), {})

        def _remember(self, index):
            """Guarda a margem e os extras desta placa para o mesmo trabalho noutra sessão."""
            entry = self._book.plates.get(index)
            if not entry or entry["job"] is None:
                return
            s = self._settings()
            memory, sig = dict(s["memory"]), signature(entry["job"])
            item = {}
            if index in self._book.margins:
                item["margin"] = self._book.margins[index]
            if self._book.extras.get(index):
                item["extras"] = self._book.extras[index]
            memory.pop(sig, None)
            if item:
                memory[sig] = item
            s["memory"] = dict(list(memory.items())[-MEMORY_LIMIT:])
            self._store(s)

        def _show(self, index=None, fresh=False):
            data = self._book.payload(index)
            self._send(data)
            if fresh and data.get("type") in ("result", "status"):
                notify(*price_notice(data, self.lang()))

        # Corre na linha principal do OrcaSlicer: se for preciso ler o G-code, o OrcaSlicer pergunta em vez de bloquear.
        def on_message(self, data):
            kind = (data or {}).get("type")
            if kind == "ready":
                self._page = True
                self._show(fresh=self._book.current in self._book.compute(only=self._book.current))
                self._send({"type": "quotes", "quotes": self._settings()["quotes"]})
                self._check_update()
            elif kind in ("quote_save", "quote_delete"):  # orçamentos guardados com os seus números
                s = self._settings()
                if kind == "quote_save":
                    q = clean_quote(data.get("quote"))
                    if q:
                        s["quotes"] = ([x for x in s["quotes"] if x["id"] != q["id"]] + [q])[-MAX_QUOTES:]
                else:
                    s["quotes"] = [x for x in s["quotes"] if x["id"] != str(data.get("id") or "")]
                self._store(s)
                self._send({"type": "quotes", "quotes": s["quotes"], "saved": kind == "quote_save"})
            elif kind in ("quote_print", "quotes_csv"):  # ficheiro nas Transferências, aberto no browser ou no Excel
                s, lang, out = self._settings(), self.lang(), {"type": "exported", "path": "", "error": ""}
                try:
                    if kind == "quote_print":
                        q = next((x for x in s["quotes"] if x["id"] == str(data.get("id") or "")), None)
                        if q is None:
                            raise FileNotFoundError(str(data.get("id")))
                        out["path"] = write_export(f"{tr(lang, 'q_title')} {q['date'][:10]} {q['title']}.html", quote_html(q, s["general"], lang))
                    else:
                        out["path"] = write_export("Print-Calculator-quotes.csv", quotes_csv(s["quotes"], lang))
                    import pathlib
                    out["opened"] = open_link(pathlib.Path(out["path"]).as_uri())
                except Exception as err:  # noqa: BLE001 - autorização recusada, disco cheio...
                    out["error"] = str(err)
                self._send(out)
            elif kind == "spoolman":  # preços do Spoolman para os filamentos (só leitura)
                try:
                    items = spoolman_filaments(data.get("url") or self._settings()["general"].get("spoolman"))
                    self._send({"type": "spoolman", "items": items})
                except Exception as err:  # noqa: BLE001 - endereço errado, sem rede ou recusado pelo OrcaSlicer
                    self._send({"type": "spoolman", "items": [], "error": str(err) or type(err).__name__})
            elif kind == "import":  # ficheiro já fatiado (.gcode.3mf ou .gcode) escolhido no separador
                import base64
                try:
                    b64 = str(data.get("b64") or "")
                    if len(b64) > MAX_IMPORT * 4 // 3 + 4:
                        raise ValueError("too big")
                    jobs = import_jobs(base64.b64decode(b64, validate=True))
                except Exception as err:  # noqa: BLE001 - ficheiro estragado, zip inválido, sem placas...
                    self._send({**friendly_error(ValueError(str(err)), self.lang()), "plate": {"index": self._book.current}, "plates": self._book.summary()})
                    return
                index = self._book.imported(jobs, data.get("name"))
                self._book.compute()
                self._show(index, fresh=True)
            elif kind == "compute":  # depois de fatiar: a placa vista primeiro, depois as outras para o total
                fresh = self._book.current in self._book.compute(only=self._book.current)
                self._book.compute()
                self._show(fresh=fresh)
            elif kind == "scan":  # placas fatiadas antes de o plugin estar ativo
                try:
                    found = find_session_gcodes()
                except Exception as err:  # noqa: BLE001 - autorização recusada, por exemplo
                    self._send({**friendly_error(err, self.lang()), "plate": {"index": self._book.current}, "plates": self._book.summary()})
                    return
                self._sync_plate()
                for index, path in sorted(session_plates(found).items()):  # todas as placas já fatiadas, não só a última
                    if index not in self._book.plates:
                        self._book.sliced(path, index)
                if not found:
                    self._send({"type": "status", "level": "warn", "text": text(self.lang(), "none"), "plate": {"index": self._book.current},
                                "plates": self._book.summary()})
                    return
                fresh = self._book.current in self._book.compute(only=self._book.current)
                self._book.compute()
                self._show(fresh=fresh)
            elif kind == "plate_margin":  # margem escolhida no separador: fica nesta placa e entra no total
                self._book.set_margin(data.get("index") or 0, data.get("margin"))
                self._remember(int(data.get("index") or 0))
                self._send({"type": "plates", "plates": self._book.summary()})
            elif kind == "plate_job":  # a placa passa para outro trabalho
                self._book.set_job(data.get("index") or 0, data.get("job"))
                self._send({"type": "plates", "plates": self._book.summary()})
            elif kind == "job_name":
                self._book.set_job_name(data.get("job"), data.get("name"))
                self._send({"type": "plates", "plates": self._book.summary()})
            elif kind == "job_margin":  # uma margem para o trabalho inteiro
                for index in self._book.set_job_margin(data.get("job"), data.get("margin")):
                    self._remember(index)
                self._send({"type": "plates", "plates": self._book.summary()})
            elif kind == "report":  # G-code da placa num .zip e issue do GitHub aberto no browser
                entry = self._book.plates.get(self._book.current) or {}
                info = report_info(entry.get("result"), orca_version())
                out = {"type": "report", "path": "", "error": ""}
                if entry.get("path") and os.path.isfile(entry["path"]):
                    try:
                        out["path"] = write_report(entry["path"], info)
                    except Exception as err:  # noqa: BLE001 - autorização recusada, disco cheio...
                        out["error"] = str(err)
                out["url"] = issue_url(info, bool(out["path"]))
                out["opened"] = open_link(out["url"])
                self._send(out)
            elif kind == "plate_extras":  # extras desta placa (por peça ou uma vez na placa)
                index = int(data.get("index") or 0)
                self._book.set_extras(index, data.get("extras"))
                self._remember(index)
                self._show(index)
            elif kind == "show_plate":  # escolhida no próprio separador
                index = int(data.get("index") or 0)
                self._book.compute(only=index)
                self._show(index)
            elif kind in ("save", "reset_printer"):
                s = self._settings()
                self._store(apply_save(data, s) if kind == "save" else reset_printer(data.get("printer"), s))
                self._book.compute(force=True)
                self._show(data.get("plate"))

        def _sync_plate(self):
            """O OrcaSlicer nem sempre avisa quando a placa escolhida muda (placa apagada ou criada): pergunta-lhe."""
            index = host_plate()
            if index is not None and index != self._book.current:
                self._book.select(index, self._book.names.get(index, ""))

        def on_lifecycle_event(self, event, ctx):
            ev = orca.LifecycleEvent
            if event == getattr(ev, "PlateSelected", None):
                self._book.select(getattr(ctx, "index", 0) or 0, getattr(ctx, "name", ""))
                self._show()
            elif event in (getattr(ev, "NewProject", None), getattr(ev, "ProjectOpened", None), getattr(ev, "ProjectClosed", None),
                           getattr(ev, "PlateDeleted", None)):  # os números das placas mudam: recomeça
                self._book.reset()
                self._sync_plate()
                self._send({"type": "reset"})
            elif event == getattr(ev, "PlateCreated", None):
                self._sync_plate()
            elif event == getattr(ev, "SliceStarted", None):
                self._sync_plate()
                self._book.slice_started()
            elif event == ev.GCodeExportFinished and ctx.code == orca.LifecycleEvtCode.Ok and ctx.msg:
                path = str(ctx.msg)
                if path.lower().endswith((".gcode", ".gco")):
                    index = self._book.sliced(path)
                    self._send({"type": "sliced", "plate": {"index": index}, "current": self._book.current})
                    self._check_update()  # também com o separador fechado: a notificação aparece no OrcaSlicer
                    if not self._page and not self._hinted:
                        self._hinted = True
                        notify("HintNotificationLevel", text(self.lang(), "hint"))

    @orca.plugin
    class PrintCalculatorPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(PrintCalculatorPage)
