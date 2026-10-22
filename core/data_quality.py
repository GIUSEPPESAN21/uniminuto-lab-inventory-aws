# -*- coding: utf-8 -*-
"""
core/data_quality.py - Auditoria de calidad del inventario (funciones puras).

No lee ni escribe la base: recibe la lista de items (tal como la devuelve
`LabStorage.get_all_items(include_retired=True)` o filas crudas del Excel) y
devuelve hallazgos (`Finding`). Sirve para cualquier inventario.

Severidades:
- error: rompe reglas que la app asume (codigos, jerarquia, cantidades).
- aviso: probablemente es un error humano; conviene revisarlo.
- info:  mejora recomendada (formato, datos incompletos).

Algunos hallazgos traen una correccion SEGURA (`Finding.fix`): solo cambia
texto derivable sin ambiguedad (ubicacion desde el codigo, un error de
digitacion de un vocabulario conocido, una categoria escrita de varias formas).
La vista la aplica con confirmacion mediante `apply_fix`, que se niega si el
item cambio despues del analisis.
"""

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core import barcode

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "aviso"
SEVERITY_INFO = "info"
SEVERITIES = (SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)
SEVERITY_LABELS = {SEVERITY_ERROR: "Error", SEVERITY_WARNING: "Aviso", SEVERITY_INFO: "Sugerencia"}
SEVERITY_TONES = {SEVERITY_ERROR: "danger", SEVERITY_WARNING: "warning", SEVERITY_INFO: "info"}
SEVERITY_ICONS = {SEVERITY_ERROR: "⛔", SEVERITY_WARNING: "⚠️", SEVERITY_INFO: "💡"}

FIELD_LABELS = {
    "id": "Código", "name": "Nombre", "category": "Categoría", "description": "Descripción",
    "item_type": "Tipo", "parent_id": "Contenedor", "quantity": "Cantidad",
    "min_stock_alert": "Umbral de alerta", "location": "Ubicación", "status": "Estado",
    "inventario": "Inventario",
}

VALID_ITEM_TYPES = ("master", "child", "standalone", "location")
LOCATION_TYPE = "location"  # estanteria, piso, mesa o zona (ver core/places.py)
VALID_STATUSES = ("active", "retired")

# Nivel de codigo GLIOPS esperado para cada tipo (igual que views/code_input.py).
EXPECTED_LEVEL = {
    "master": barcode.LEVEL_CONTAINER,
    "child": barcode.LEVEL_BOX,
    "standalone": barcode.LEVEL_ITEM,
}
_TYPE_NAMES = {
    "master": "Contenedor Principal",
    "child": "Contenedor de Característica",
    "standalone": "Ítem Individual",
    "location": "Ubicación",
}
_LEVEL_NAMES = {
    barcode.LEVEL_CONTAINER: "contenedor",
    barcode.LEVEL_BOX: "caja",
    barcode.LEVEL_ITEM: "ítem",
}
_STRUCTURED_FORMATS = (barcode.FORMAT_STANDARD, barcode.FORMAT_MESA, barcode.FORMAT_LEGO)

# Palabras frecuentes en nombres del laboratorio, con su escritura correcta.
# Incluye las formas cercanas que SON validas (liso/lisos, piezo, puerto) para
# que no se marquen como error de digitacion.
VOCABULARY = (
    "Contenedor", "Contenedores", "Caja", "Cajas", "Pieza", "Piezas", "Piezo", "Piezos", "Lego",
    "Estantería", "Estanterías", "Estante", "Piso", "Ítem", "Ítems", "Ladrillo", "Ladrillos",
    "Placa", "Placas", "Lisa", "Lisas", "Liso", "Lisos", "Bisel", "Biseles", "Base", "Bases",
    "Pines", "Separador", "Separadores", "Puerta", "Puertas", "Puerto", "Puertos", "Ventana",
    "Ventanas", "Ficha", "Fichas", "Bandeja", "Bandejas", "Organizador", "Organizadores",
    "Multímetro", "Multímetros", "Resistencia", "Resistencias", "Tornillo", "Tornillos",
    "Tuerca", "Tuercas", "Cable", "Cables", "Sensor", "Sensores", "Motor", "Motores",
    "Engranaje", "Engranajes", "Rueda", "Ruedas", "Conector", "Conectores", "Herramienta",
    "Herramientas", "Característica", "Principal", "Laboratorio", "Electrónica", "Mecánica",
    "Robótica", "Exhibición", "Modelo", "Modelos", "Equipo", "Equipos", "Tarjeta", "Tarjetas",
    "Protoboard", "Arduino", "Batería", "Baterías", "Soldador", "Kit", "Mesa",
)

# Palabras que indican que una descripcion enumera contenido.
_CONTENT_VERBS = ("contiene", "contienen", "incluye", "incluyen", "hay", "guarda", "almacena", "trae")
_BOX_WORDS = ("caja", "cajas", "bolsa", "bolsas", "bandeja", "bandejas", "estuche", "organizador",
              "recipiente", "compartimento", "compartimiento", "gaveta", "cajon", "sobre")
_LEADING_FILLER = ("ademas", "contiene", "contienen", "incluye", "incluyen", "hay", "tiene", "tienen",
                   "trae", "guarda", "con", "un", "una", "unos", "unas", "el", "la", "los", "las",
                   "y", "de", "del", "tambien")
_TRAILING_FILLER = ("de", "del", "con", "y", "en")
_GENERIC_MASTER_NAME_RE = re.compile(
    r"^(contenedor|contendor|caja|kit|gabinete|bandeja|organizador)(\s*(n[o°º]\.?|#))?\s*\d*$"
)

# Medidas NxM; no toma decimales (2.5x1, 2,5x1) pero si acepta la coma o el
# punto que cierran una enumeracion ("4x2, 2x2." ).
_DIM_RE = re.compile(r"(?<!\d)(?<!\d[.,])(\d+)\s*[x×]\s*(\d+)(?!\d|[.,]\d)", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")
_LETTER_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]")


# ---------------------------------------------------------------------------
# Hallazgos
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """Un problema o mejora detectada en el inventario.

    `fix`, si existe, es una correccion segura: {"label": str,
    "changes": {campo: valor_nuevo}, "before": {campo: valor_actual}}."""

    severity: str
    rule: str
    item_id: str
    field: str
    message: str
    suggestion: str = ""
    item_name: str = ""
    fix: dict = None
    details: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.item_id}|{self.field}"

    @property
    def field_label(self) -> str:
        return FIELD_LABELS.get(self.field, self.field)

    def as_dict(self) -> dict:
        return {
            "severity": self.severity, "rule": self.rule, "item_id": self.item_id,
            "item_name": self.item_name, "field": self.field, "message": self.message,
            "suggestion": self.suggestion, "fix": self.fix, "details": list(self.details),
        }


def _fix(label: str, item: dict, changes: dict) -> dict:
    return {
        "label": label,
        "changes": dict(changes),
        "before": {name: _text(item.get(name)) for name in changes},
    }


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def _text(value) -> str:
    """Texto de una celda: None/NaN/"None" -> "" (filas crudas del Excel)."""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    text = str(value)
    return "" if text in ("nan", "None", "NaT") else text


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))


def normalize_text(text) -> str:
    """Minusculas, sin tildes y con espacios simples (para comparar)."""
    return " ".join(strip_accents(_text(text)).lower().split())


def tidy_spaces(text) -> str:
    """Quita espacios al inicio/fin y espacios repetidos."""
    return " ".join(_text(text).split())


def _match_case(word: str, template: str) -> str:
    if template.isupper() and len(template) > 1:
        return word.upper()
    if template[:1].isupper():
        return word[:1].upper() + word[1:]
    return word[:1].lower() + word[1:]


def _edit_distance(a: str, b: str, limit: int = 2) -> int:
    """Distancia de Damerau-Levenshtein (transposiciones adyacentes), con
    corte temprano cuando supera `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _vocabulary_index(vocabulary) -> dict:
    return {normalize_text(word): word for word in vocabulary}


_DEFAULT_VOCABULARY_INDEX = _vocabulary_index(VOCABULARY)


def find_typos(text, vocabulary=None) -> list:
    """Errores de digitacion probables: [(palabra_escrita, correccion)].

    Solo se marcan palabras de 5 o mas letras a distancia 1 (2 si tienen 10 o
    mas) de una palabra del vocabulario. Las diferencias solo de tildes o de
    mayusculas NO se marcan, ni las palabras que ya estan en el vocabulario."""
    index = _DEFAULT_VOCABULARY_INDEX if vocabulary is None else _vocabulary_index(vocabulary)
    typos, seen = [], set()
    for match in _WORD_RE.finditer(_text(text)):
        word = match.group(0)
        norm = normalize_text(word)
        if len(norm) < 5 or norm in index or word in seen:
            continue
        limit = 2 if len(norm) >= 10 else 1
        best, best_score = None, (limit + 1, True, 0)
        for candidate_norm, candidate in index.items():
            if len(candidate_norm) < 5:
                continue
            distance = _edit_distance(norm, candidate_norm, limit)
            # En empate gana la que conserva singular/plural y luego la de
            # largo mas parecido ("Resistencis" -> "Resistencias").
            plural_mismatch = norm.endswith("s") != candidate_norm.endswith("s")
            score = (distance, plural_mismatch, abs(len(candidate_norm) - len(norm)))
            if distance <= limit and score < best_score:
                best, best_score = candidate, score
        if best is not None:
            seen.add(word)
            typos.append((word, _match_case(best, word)))
    return typos


def fix_typos(text, typos: list) -> str:
    """Reemplaza las palabras de `typos` conservando el resto del texto."""
    mapping = dict(typos)
    if not mapping:
        return _text(text)
    return _WORD_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), _text(text))


def invalid_dimensions(text) -> list:
    """Medidas tipo NxM con alguna dimension en cero (p. ej. '1x0')."""
    bad = []
    for match in _DIM_RE.finditer(_text(text)):
        if int(match.group(1)) == 0 or int(match.group(2)) == 0:
            token = f"{match.group(1)}x{match.group(2)}"
            if token not in bad:
                bad.append(token)
    return bad


def _dimension_key(a, b) -> tuple:
    """4x2 y 2x4 son la misma pieza."""
    return tuple(sorted((int(a), int(b))))


# ---------------------------------------------------------------------------
# Ubicacion
# ---------------------------------------------------------------------------

_LOCATION_KEYWORDS = (
    ("estanteria", r"estanterias?|estantes?|est"),
    ("piso", r"pisos?|nivel|repisa|entrepano"),
    ("contenedor", r"contenedor|contendor|contenedr|cont"),
    ("caja", r"cajas?|cj"),
    ("item", r"items?|articulo"),
    ("mesa", r"mesa(?:\s+de\s+trabajo)?"),
    ("equipo", r"equipo|eq"),
    ("modelo", r"modelo|lm"),
)
_LOCATION_RE = re.compile(
    r"\b(?:" + "|".join(f"(?P<{name}>{pattern})" for name, pattern in _LOCATION_KEYWORDS) + r")"
    r"(?![a-z])\.?[\s\-:#=°º]*(?:no\.?\s*|n[°º]\s*)?(?P<num>\d+)\b"
)
_COMPACT_RE = re.compile(r"\b(?P<kind>cj|e|p|c|i|m)(?P<num>\d+)\b")
_COMPACT_FIELDS = {"e": "estanteria", "p": "piso", "c": "contenedor", "cj": "caja", "i": "item", "m": "mesa"}
_CONNECTORS = ("y", "de", "del", "en", "la", "el", "ruta")


def parse_location_text(text, compact: bool = True) -> dict:
    """Extrae los niveles que menciona una ubicacion escrita a mano.

    'Estantería- 2; Piso-1; Contenedor-1.' -> {"fields": {"estanteria": 2,
    "piso": 1, "contenedor": 1}, "leftover": ""}. `leftover` es el texto que no
    se pudo interpretar (referencias adicionales como 'lado izquierdo')."""
    norm = strip_accents(_text(text)).lower()
    fields, spans = {}, []
    for match in _LOCATION_RE.finditer(norm):
        name = next(key for key, _ in _LOCATION_KEYWORDS if match.group(key))
        fields.setdefault(name, int(match.group("num")))
        spans.append(match.span())
    if compact:
        for match in _COMPACT_RE.finditer(norm):
            if any(start <= match.start() < end for start, end in spans):
                continue
            name = _COMPACT_FIELDS[match.group("kind")]
            if name not in fields:
                fields[name] = int(match.group("num"))
                spans.append(match.span())
    leftover = norm
    for start, end in sorted(spans, reverse=True):
        leftover = leftover[:start] + " " + leftover[end:]
    words = [w for w in re.findall(r"[a-z0-9]+", leftover) if w not in _CONNECTORS]
    return {"fields": fields, "leftover": " ".join(words)}


def _parse_structured(code: str):
    try:
        parsed = barcode.parse_code(code)
    except ValueError:
        return None
    return parsed if parsed.get("format") in _STRUCTURED_FORMATS else None


def suggest_location_from_code(code: str):
    """Ubicacion legible derivada de un codigo estructurado, o None.

    2-1-01-01-000 -> 'Estantería 2 · Piso 1 · Contenedor 01 · Caja 01'."""
    parsed = _parse_structured((code or "").strip())
    if not parsed:
        return None
    fmt = parsed["format"]
    if fmt == barcode.FORMAT_STANDARD:
        parts = [f"Estantería {parsed['estanteria']}", f"Piso {parsed['piso']}",
                 f"Contenedor {parsed['contenedor']:02d}"]
        if parsed["caja"]:
            parts.append(f"Caja {parsed['caja']:02d}")
        if parsed["item"]:
            parts.append(f"Ítem {parsed['item']:03d}")
        return " · ".join(parts)
    if fmt == barcode.FORMAT_MESA:
        return f"Mesa de trabajo {parsed['mesa']} · Equipo {parsed['equipo']}"
    return f"Estantería 3 · Exhibición Lego · Modelo {parsed['modelo']:02d}"


def location_conflicts(code: str, location: str, compare_zero_levels: bool = True,
                       compact: bool = True) -> list:
    """Niveles en los que la ubicacion escrita contradice el codigo:
    [(nivel, valor_en_texto, valor_en_codigo)]. Un codigo libre no se compara.

    `compare_zero_levels=False` ignora caja/item cuando el codigo no los usa;
    `compact=False` no interpreta abreviaturas tipo E2/P1/C01 (para nombres)."""
    parsed = _parse_structured((code or "").strip())
    if not parsed:
        return []
    found = parse_location_text(location, compact=compact)["fields"]
    conflicts = []
    for name, value in found.items():
        if name not in parsed:
            continue
        expected = parsed[name]
        if expected == 0 and (not compare_zero_levels or name not in ("caja", "item")):
            continue
        if value != expected:
            conflicts.append((name, value, expected))
    return conflicts


_LEVEL_TITLES = {
    "estanteria": "Estantería", "piso": "Piso", "contenedor": "Contenedor", "caja": "Caja",
    "item": "Ítem", "mesa": "Mesa", "equipo": "Equipo", "modelo": "Modelo",
}


def _describe_conflicts(conflicts: list) -> str:
    parts = []
    for name, in_text, in_code in conflicts:
        code_value = "no aplica" if in_code == 0 else str(in_code)
        parts.append(f"{_LEVEL_TITLES[name]} {in_text} (el código dice {code_value})")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Contenido descrito
# ---------------------------------------------------------------------------

def _clean_label(text: str) -> str:
    words = re.split(r"\s+", re.sub(r"[\s\-–:;,.]+", " ", text or "").strip())
    words = [w for w in words if w]
    while words and normalize_text(words[0]).strip(",") in _LEADING_FILLER:
        words.pop(0)
    while words and normalize_text(words[-1]) in _TRAILING_FILLER:
        words.pop()
    label = " ".join(words)
    return label[:1].upper() + label[1:] if label else ""


def _has_words(text: str) -> bool:
    return bool(_clean_label(text)) and bool(_LETTER_RE.search(_clean_label(text)))


def _mention(text: str):
    label = _clean_label(text)
    if not label or not _LETTER_RE.search(label):
        return None
    kind = "caja" if any(w in _BOX_WORDS for w in normalize_text(label).split()) else "piezas"
    return {"kind": kind, "label": label, "sizes": []}


def extract_described_contents(text) -> list:
    """Interpreta una descripcion que enumera contenido.

    Devuelve grupos {"kind": "piezas"|"caja", "label", "sizes"}; p. ej.
    'Contiene Piezas Lego de Pines 4x2 - 2x2' -> [{"kind": "piezas",
    "label": "Piezas Lego de Pines", "sizes": ["4x2", "2x2"]}]. Es heuristico:
    sirve para avisar que hay contenido sin registrar, no para crearlo."""
    groups = []
    clauses = re.split(r"[\n;.]+|,(?!\s*\d+\s*[x×]\s*\d)", _text(text), flags=re.IGNORECASE)
    for clause in clauses:
        matches = list(_DIM_RE.finditer(clause))
        if not matches:
            mention = _mention(clause)
            if mention:
                groups.append(mention)
            continue
        cursor, current = 0, None
        for match in matches:
            between = clause[cursor:match.start()]
            if current is None or _has_words(between):
                parts = re.split(r"\s+y\s+", between)
                if len(parts) > 1:
                    mention = _mention(" y ".join(parts[:-1]))
                    if mention:
                        groups.append(mention)
                current = {"kind": "piezas", "label": _clean_label(parts[-1]) or "Piezas", "sizes": []}
                groups.append(current)
            size = f"{match.group(1)}x{match.group(2)}"
            if size not in current["sizes"]:
                current["sizes"].append(size)
            cursor = match.end()
        mention = _mention(clause[cursor:])
        if mention:
            groups.append(mention)
    return groups


def describes_contents(text) -> bool:
    """True si la descripcion enumera contenido (medidas o verbos como 'contiene')."""
    norm = normalize_text(text)
    if not norm:
        return False
    if _DIM_RE.search(norm):
        return True
    return any(re.search(rf"\b{verb}\b", norm) for verb in _CONTENT_VERBS)


def summarize_contents(groups: list) -> dict:
    pieces = [g for g in groups if g["kind"] == "piezas"]
    return {
        "piece_types": sum(len(g["sizes"]) or 1 for g in pieces),
        "boxes": len([g for g in groups if g["kind"] == "caja"]),
    }


def format_content_group(group: dict) -> str:
    prefix = "📦 " if group["kind"] == "caja" else ""
    sizes = f": {', '.join(group['sizes'])}" if group["sizes"] else ""
    return f"{prefix}{group['label']}{sizes}"


def suggest_child_codes(master_code: str, existing_ids, count: int = 3) -> list:
    """Siguientes codigos libres de nivel caja dentro de un contenedor GLIOPS."""
    try:
        parsed = barcode.parse_code((master_code or "").strip())
    except ValueError:
        return []
    if parsed.get("format") != barcode.FORMAT_STANDARD:
        return []
    taken = {str(i).strip() for i in existing_ids}
    codes, box = [], 1
    while len(codes) < count and box < 100:
        code = barcode.build_standard_code(parsed["estanteria"], parsed["piso"], parsed["contenedor"], box, 0)
        if code not in taken:
            codes.append(code)
        box += 1
    return codes


# ---------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------

def _as_int(value):
    """Entero de una celda o None si no es numerica."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value).strip()
    if text == "":
        return 0
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def _canonical_spelling(spellings: Counter) -> str:
    """La escritura mas usada; en empate, la que tiene tildes y mayusculas."""
    def score(text):
        accents = sum(1 for c in text if unicodedata.normalize("NFKD", c) != c)
        capitals = sum(1 for w in text.split() if w[:1].isupper())
        return (spellings[text], accents, capitals, text)
    return max(spellings, key=score)


def _sort_key(finding: Finding) -> tuple:
    # Dentro de cada severidad, primero los hallazgos generales del inventario.
    return (SEVERITIES.index(finding.severity), finding.item_id != "", finding.item_id,
            finding.rule, finding.field)


def audit_items(items: list, include_retired: bool = False) -> list:
    """Audita el inventario completo y devuelve hallazgos ordenados por
    severidad. Conviene pasar TODOS los items (tambien los dados de baja) para
    poder validar la jerarquia; los dados de baja no se auditan salvo que
    `include_retired` sea True."""
    items = [dict(item) for item in (items or [])]
    for item in items:
        item["id"] = _text(item.get("id"))
        item["status"] = _text(item.get("status")).strip() or "active"
    by_id = {}
    for item in items:
        by_id.setdefault(item["id"], item)

    audited = [i for i in items if include_retired or i["status"] != "retired"]
    findings = []

    def add(severity, rule, item, field_name, message, suggestion="", fix=None, details=None):
        findings.append(Finding(
            severity=severity, rule=rule, item_id=(item or {}).get("id", ""), field=field_name,
            message=message, suggestion=suggestion, item_name=_text((item or {}).get("name")).strip(),
            fix=fix, details=list(details or []),
        ))

    # --- codigos repetidos (antes que nada: rompen todas las busquedas) ---
    id_counts = Counter(i["id"].strip() for i in audited if i["id"].strip())
    reported_ids = set()
    for item in audited:
        code = item["id"].strip()
        if id_counts.get(code, 0) > 1 and code not in reported_ids:
            reported_ids.add(code)
            add(SEVERITY_ERROR, "duplicate_id", item, "id",
                f"El código {code} está registrado {id_counts[code]} veces.",
                "Cada producto debe tener un código único: elimina o cambia el código de los repetidos.")
    folded = defaultdict(set)
    for code in id_counts:
        folded[code.lower()].add(code)
    for variants in folded.values():
        if len(variants) > 1:
            first = by_id.get(sorted(variants)[0]) or {"id": sorted(variants)[0]}
            add(SEVERITY_WARNING, "id_case_collision", first, "id",
                f"Hay códigos que solo se diferencian en mayúsculas: {', '.join(sorted(variants))}.",
                "Un lector de código de barras puede leerlos igual; usa códigos claramente distintos.")

    children_of = defaultdict(list)
    for item in items:
        parent = _text(item.get("parent_id")).strip()
        if parent and item["status"] != "retired":
            children_of[parent].append(item)

    for item in audited:
        _audit_item(item, by_id, children_of, add)

    _audit_names(audited, add)
    _audit_categories(audited, add)

    active = [i for i in audited if i["status"] != "retired" and _text(i.get("item_type")) != LOCATION_TYPE]
    if active:
        lendable = [i for i in active if _text(i.get("item_type")) != "master" and (_as_int(i.get("quantity")) or 0) > 0]
        if not lendable:
            add(SEVERITY_WARNING, "nothing_lendable", None, "inventario",
                "Ningún producto se puede prestar todavía: el inventario activo solo tiene contenedores "
                "principales o productos sin unidades.",
                "Registra dentro de cada contenedor los productos que guarda, con su cantidad.")

    return sorted(findings, key=_sort_key)


def _audit_item(item: dict, by_id: dict, children_of: dict, add) -> None:
    raw_id = item["id"]
    code = raw_id.strip()
    item_type = _text(item.get("item_type")).strip()
    parent_id = _text(item.get("parent_id")).strip()
    name = _text(item.get("name"))
    location = _text(item.get("location"))
    description = _text(item.get("description"))
    status = item["status"]

    if item_type == LOCATION_TYPE:
        _audit_place(item, code, raw_id, parent_id, by_id, name, description, add)
        return

    # --- codigo ---
    parsed = None
    if not code:
        add(SEVERITY_ERROR, "missing_id", item, "id", "El producto no tiene código.",
            "Elimínalo y regístralo de nuevo con un código válido.")
    else:
        if raw_id != code:
            add(SEVERITY_ERROR, "id_whitespace", item, "id",
                f"El código '{raw_id}' tiene espacios al inicio o al final: el escáner no lo encontrará.",
                "Vuelve a registrarlo con el código sin espacios.")
        try:
            parsed = barcode.parse_code(code)
        except ValueError as exc:
            add(SEVERITY_WARNING, "code_format", item, "id",
                f"El código {code} está fuera de la nomenclatura: {exc}",
                "Los productos nuevos deben usar un formato válido; si es un código heredado, "
                "considera registrarlo de nuevo con un código GLIOPS.")

    # --- tipo, estado y jerarquia ---
    if item_type not in VALID_ITEM_TYPES:
        add(SEVERITY_ERROR, "item_type", item, "item_type",
            f"Tipo de producto desconocido: '{item_type or 'vacío'}'.",
            "Usa Contenedor Principal, Contenedor de Característica o Ítem Individual.")
    if status not in VALID_STATUSES:
        add(SEVERITY_WARNING, "status", item, "status", f"Estado desconocido: '{status}'.",
            "Los estados válidos son activo (active) o dado de baja (retired).")

    parent = by_id.get(parent_id) if parent_id else None
    if item_type == "child":
        if not parent_id:
            add(SEVERITY_ERROR, "orphan_child", item, "parent_id",
                "Es un Contenedor de Característica pero no tiene Contenedor Principal.",
                "Elimínalo y regístralo de nuevo dentro de un Contenedor Principal.")
        elif parent_id == code:
            add(SEVERITY_ERROR, "self_parent", item, "parent_id", "El producto figura dentro de sí mismo.",
                "Elimínalo y regístralo de nuevo dentro del contenedor correcto.")
        elif parent is None:
            add(SEVERITY_ERROR, "missing_parent", item, "parent_id",
                f"Su Contenedor Principal ({parent_id}) no existe.",
                "Registra ese contenedor con el mismo código o vuelve a registrar el producto.")
        elif _text(parent.get("item_type")) != "master":
            add(SEVERITY_ERROR, "parent_not_master", item, "parent_id",
                f"Está dentro de {parent_id}, que no es un Contenedor Principal.",
                "Solo un Contenedor Principal puede contener productos.")
        elif parent["status"] == "retired" and status != "retired":
            add(SEVERITY_WARNING, "parent_retired", item, "parent_id",
                f"Su Contenedor Principal ({parent_id}) está dado de baja pero este producto sigue activo.",
                "Da de baja o elimina el producto, o vuelve a registrar el contenedor.")
    elif parent_id:
        add(SEVERITY_ERROR, "unexpected_parent", item, "parent_id",
            f"Solo un Contenedor de Característica puede estar dentro de otro, pero este "
            f"{_TYPE_NAMES.get(item_type, 'producto')} figura dentro de {parent_id}.",
            "Revisa el tipo del producto.")

    if parsed and parsed.get("format") == barcode.FORMAT_STANDARD:
        level = barcode.standard_code_level(parsed)
        expected = EXPECTED_LEVEL.get(item_type)
        if expected and level != expected:
            add(SEVERITY_WARNING, "code_level", item, "id",
                f"El código {code} es de nivel {_LEVEL_NAMES[level]}, pero el producto es "
                f"{_TYPE_NAMES[item_type]} (se espera nivel {_LEVEL_NAMES[expected]}).",
                "Confirma que el tipo y el código sean los correctos.")
        if item_type == "child" and parent is not None:
            parent_parsed = _parse_structured(_text(parent.get("id")).strip())
            if parent_parsed and parent_parsed.get("format") == barcode.FORMAT_STANDARD:
                own = (parsed["estanteria"], parsed["piso"], parsed["contenedor"])
                theirs = (parent_parsed["estanteria"], parent_parsed["piso"], parent_parsed["contenedor"])
                if own != theirs:
                    add(SEVERITY_WARNING, "child_outside_parent", item, "id",
                        f"Su código indica {suggest_location_from_code(code)}, pero está registrado dentro de "
                        f"{parent_id} ({suggest_location_from_code(parent_id)}).",
                        "El código de un producto debe empezar por la estantería, piso y contenedor de su "
                        "Contenedor Principal.")

    # --- cantidades ---
    quantity = _as_int(item.get("quantity"))
    if quantity is None:
        add(SEVERITY_ERROR, "quantity_type", item, "quantity",
            f"La cantidad '{_text(item.get('quantity'))}' no es un número entero.",
            "Corrige la cantidad desde Inventario.")
    elif quantity < 0:
        add(SEVERITY_ERROR, "quantity_negative", item, "quantity", f"La cantidad es negativa ({quantity}).",
            "Corrige la cantidad desde Inventario.")
    elif item_type == "master" and quantity > 0:
        add(SEVERITY_INFO, "master_quantity", item, "quantity",
            f"Tiene cantidad {quantity}, pero la cantidad de un Contenedor Principal no se usa.",
            "Registra las unidades en los productos que guarda.")
    elif item_type in ("child", "standalone") and quantity == 0 and status != "retired":
        add(SEVERITY_WARNING, "zero_quantity", item, "quantity",
            "No tiene unidades registradas: no se puede prestar.",
            "Actualiza la cantidad desde Inventario si el producto sí existe.")
    min_alert = _as_int(item.get("min_stock_alert"))
    if min_alert is None or min_alert < 0:
        add(SEVERITY_WARNING, "min_stock_alert", item, "min_stock_alert",
            f"El umbral de alerta '{_text(item.get('min_stock_alert'))}' no es válido.",
            "Usa un número entero mayor o igual a 0.")

    # --- ubicacion ---
    _audit_location(item, code, parsed, parent, location, add)

    # --- nombre frente al codigo ---
    if parsed and name.strip():
        conflicts = [c for c in location_conflicts(code, name, compare_zero_levels=False, compact=False)
                     if c[0] != "item"]
        if conflicts:
            add(SEVERITY_WARNING, "name_code_mismatch", item, "name",
                f"El nombre menciona {_describe_conflicts(conflicts)}.",
                "Corrige el nombre o confirma que el producto tenga el código correcto.")

    # --- descripcion ---
    bad_sizes = invalid_dimensions(description)
    if bad_sizes:
        add(SEVERITY_WARNING, "invalid_dimension", item, "description",
            f"La descripción menciona medidas imposibles: {', '.join(bad_sizes)}.",
            "Revisa la pieza física y corrige la medida (ninguna dimensión puede ser 0).")
    typos = find_typos(description)
    if typos:
        add(SEVERITY_INFO, "description_typo", item, "description",
            "Posibles errores de digitación en la descripción: "
            + ", ".join(f"'{wrong}' → '{right}'" for wrong, right in typos) + ".",
            "Corrígelos para que las búsquedas encuentren el producto.",
            fix=_fix("Corregir la descripción", item, {"description": fix_typos(description, typos)}))

    if item_type == "master" and status != "retired":
        _audit_master_contents(item, code, description, children_of.get(code, []), by_id, add)


def _audit_place(item, code, raw_id, parent_id, by_id, name, description, add) -> None:
    """Reglas de una ubicacion (estanteria, piso, mesa o zona): su codigo es de
    ubicacion, solo puede estar dentro de otra ubicacion compatible y no tiene
    stock. No se le exige ubicacion escrita ni categoria: el codigo ES el lugar."""
    if not code:
        add(SEVERITY_ERROR, "missing_id", item, "id", "La ubicación no tiene código.",
            "Elimínala y regístrala de nuevo desde Inventario → Ubicaciones.")
    else:
        if raw_id != code:
            add(SEVERITY_ERROR, "id_whitespace", item, "id",
                f"El código '{raw_id}' tiene espacios al inicio o al final: el escáner no lo encontrará.",
                "Vuelve a registrarla con el código sin espacios.")
        try:
            barcode.validate_location_code(code)
        except ValueError as exc:
            add(SEVERITY_WARNING, "code_format", item, "id",
                f"El código {code} no es un código de ubicación: {exc}",
                "Regístrala de nuevo desde Inventario → Ubicaciones con un código de ubicación.")

    parent = by_id.get(parent_id) if parent_id else None
    if parent_id:
        if parent_id == code:
            add(SEVERITY_ERROR, "self_parent", item, "parent_id", "La ubicación figura dentro de sí misma.",
                "Elimínala y regístrala de nuevo.")
        elif parent is None:
            add(SEVERITY_WARNING, "missing_parent", item, "parent_id",
                f"La ubicación que la contiene ({parent_id}) no existe.",
                "Registra esa ubicación con el mismo código o vuelve a registrar esta.")
        elif _text(parent.get("item_type")) != LOCATION_TYPE:
            add(SEVERITY_ERROR, "parent_not_location", item, "parent_id",
                f"Está dentro de {parent_id}, que no es una ubicación.",
                "Una ubicación solo puede estar dentro de otra ubicación (estantería, piso, mesa o zona).")
        else:
            problem = barcode.location_parent_problem(code, parent_id)
            if problem:
                add(SEVERITY_WARNING, "location_parent", item, "parent_id", problem,
                    "Revisa en qué ubicación está registrada.")
            elif parent["status"] == "retired" and item["status"] != "retired":
                add(SEVERITY_WARNING, "parent_retired", item, "parent_id",
                    f"La ubicación que la contiene ({parent_id}) está dada de baja pero esta sigue activa.",
                    "Elimina esta ubicación o vuelve a registrar la que la contiene.")

    quantity = _as_int(item.get("quantity"))
    if quantity is None or quantity != 0:
        add(SEVERITY_INFO, "location_quantity", item, "quantity",
            f"Tiene cantidad '{_text(item.get('quantity'))}', pero una ubicación no tiene stock.",
            "La cantidad de una ubicación no se usa: los productos que guarda tienen la suya.")

    # El nombre no debe contradecir el codigo ("Estantería 3" con el código de la 2).
    place = barcode.location_code(code)
    if place and name.strip():
        found = parse_location_text(name, compact=False)["fields"]
        conflicts = [(level, found[level], place[level]) for level in ("estanteria", "piso", "mesa")
                     if level in found and level in place and found[level] != place[level]]
        if conflicts:
            add(SEVERITY_WARNING, "name_code_mismatch", item, "name",
                f"El nombre menciona {_describe_conflicts(conflicts)}.",
                "Corrige el nombre para que coincida con la etiqueta.")

    typos = find_typos(description)
    if typos:
        add(SEVERITY_INFO, "description_typo", item, "description",
            "Posibles errores de digitación en la descripción: "
            + ", ".join(f"'{wrong}' → '{right}'" for wrong, right in typos) + ".",
            "Corrígelos para que las búsquedas encuentren la ubicación.",
            fix=_fix("Corregir la descripción", item, {"description": fix_typos(description, typos)}))


def _audit_location(item, code, parsed, parent, location, add) -> None:
    status = item["status"]
    structured = parsed if parsed and parsed.get("format") in _STRUCTURED_FORMATS else None
    suggested = suggest_location_from_code(code) if structured else None
    stripped = location.strip()

    if structured:
        info = parse_location_text(location)
        conflicts = location_conflicts(code, location)
        if conflicts:
            add(SEVERITY_WARNING, "location_conflict", item, "location",
                f"La ubicación escrita contradice el código: {_describe_conflicts(conflicts)}.",
                "La etiqueta y el escáner usan el código. Si el producto está donde dice el código, usa "
                "su ruta; si está en otro lugar, debe registrarse con el código correcto.",
                fix=_fix("Usar la ubicación del código", item, {"location": suggested}))
        elif not stripped:
            if status != "retired":
                add(SEVERITY_INFO, "location_missing", item, "location",
                    "No tiene ubicación escrita.",
                    f"Se puede completar desde el código: {suggested}.",
                    fix=_fix("Completar ubicación desde el código", item, {"location": suggested}))
        elif not info["fields"]:
            add(SEVERITY_INFO, "location_unstructured", item, "location",
                f"La ubicación '{stripped}' no menciona la ruta del código ({suggested}).",
                "Agrega la estantería, el piso y el contenedor para que coincida con la etiqueta.")
        elif not info["leftover"] and location != suggested:
            add(SEVERITY_INFO, "location_format", item, "location",
                f"La ubicación '{stripped}' coincide con el código pero está escrita a mano.",
                f"Formato estándar: {suggested}. Así todas las ubicaciones se leen y filtran igual.",
                fix=_fix("Normalizar ubicación", item, {"location": suggested}))
        return

    parent_location = _text((parent or {}).get("location")).strip()
    parent_structured = _parse_structured(_text((parent or {}).get("id")).strip()) if parent else None
    if not stripped and not parent_location and not parent_structured and status != "retired":
        add(SEVERITY_WARNING, "location_unknown", item, "location",
            "Ni el código ni la ubicación indican dónde está el producto.",
            "Escribe la ubicación física (estantería, piso, contenedor o mesa).")


def _audit_master_contents(item, code, description, children, by_id, add) -> None:
    if not children:
        if describes_contents(description):
            groups = extract_described_contents(description)
            summary = summarize_contents(groups)
            parts = []
            if summary["piece_types"]:
                parts.append(f"{summary['piece_types']} tipo(s) de pieza")
            if summary["boxes"]:
                parts.append(f"{summary['boxes']} caja(s)")
            described = " y ".join(parts) or "contenido"
            codes = suggest_child_codes(code, by_id.keys())
            code_hint = f" (códigos sugeridos: {', '.join(codes)}, …)" if codes else ""
            add(SEVERITY_WARNING, "undeclared_contents", item, "description",
                f"La descripción menciona {described}, pero el contenedor no tiene productos registrados "
                "dentro: ese material no se puede prestar ni contar.",
                f"Registra cada tipo como Contenedor de Característica dentro de este contenedor{code_hint}, "
                "con su cantidad.",
                details=[format_content_group(g) for g in groups])
        else:
            add(SEVERITY_INFO, "empty_master", item, "description",
                "El contenedor no tiene productos registrados dentro"
                + (" ni descripción." if not description.strip() else "."),
                "Registra los productos que guarda o describe su contenido.")
        return

    # Medidas descritas que no aparecen en ningun producto de adentro.
    described = {}
    for match in _DIM_RE.finditer(description):
        if int(match.group(1)) and int(match.group(2)):
            described.setdefault(_dimension_key(match.group(1), match.group(2)),
                                 f"{match.group(1)}x{match.group(2)}")
    registered = {
        _dimension_key(m.group(1), m.group(2))
        for child in children for m in _DIM_RE.finditer(_text(child.get("name")))
    }
    missing = [text for key, text in described.items() if key not in registered]
    if described and missing:
        add(SEVERITY_INFO, "contents_partially_registered", item, "description",
            f"La descripción menciona medidas que no aparecen en ningún producto de adentro: "
            f"{', '.join(missing)}.",
            "Registra esas piezas o actualiza la descripción.")


def _audit_names(items: list, add) -> None:
    groups = defaultdict(list)
    for item in items:
        name = _text(item.get("name"))
        if not name.strip():
            add(SEVERITY_ERROR, "missing_name", item, "name", "El producto no tiene nombre.",
                "Escribe un nombre desde Inventario.")
            continue
        typos = find_typos(name)
        tidy = tidy_spaces(name)
        fixed = fix_typos(tidy, typos)
        if typos:
            add(SEVERITY_WARNING, "name_typo", item, "name",
                "Posible error de digitación en el nombre: "
                + ", ".join(f"'{wrong}' → '{right}'" for wrong, right in typos) + ".",
                f"Nombre sugerido: {fixed}.",
                fix=_fix(f"Corregir a «{fixed}»", item, {"name": fixed}))
        elif tidy != name:
            add(SEVERITY_INFO, "name_spaces", item, "name",
                "El nombre tiene espacios sobrantes.", f"Nombre sugerido: {tidy}.",
                fix=_fix("Quitar espacios sobrantes", item, {"name": tidy}))
        if _text(item.get("item_type")) == "master" and _GENERIC_MASTER_NAME_RE.match(normalize_text(name)):
            example = f"{fixed} · {tidy_spaces(item.get('category')) or 'Ladrillos Lego'}"
            add(SEVERITY_INFO, "generic_name", item, "name",
                "El nombre no dice qué guarda el contenedor.",
                f"Un nombre como «{example}» ayuda a encontrarlo en búsquedas y etiquetas.")
        is_place = _text(item.get("item_type")) == LOCATION_TYPE
        groups[(_text(item.get("parent_id")).strip(), normalize_text(name), is_place)].append(item)

    for (parent, _, _), group in groups.items():
        if len(group) > 1:
            codes = ", ".join(i["id"] for i in group)
            where = f"dentro de {parent}" if parent else "fuera de contenedores"
            for item in group:
                add(SEVERITY_WARNING, "duplicate_name", item, "name",
                    f"Hay {len(group)} productos activos con el mismo nombre {where} ({codes}).",
                    "Si son productos distintos, diferéncialos en el nombre (medida, color, referencia); "
                    "si son el mismo, elimina el repetido.")


def _audit_categories(items: list, add) -> None:
    spellings = defaultdict(Counter)
    for item in items:
        category = _text(item.get("category"))
        if not category.strip():
            if _text(item.get("item_type")) != LOCATION_TYPE:  # una ubicacion no necesita categoria
                add(SEVERITY_INFO, "missing_category", item, "category", "No tiene categoría.",
                    "Asigna una categoría para agruparlo en los reportes y filtros.")
            continue
        spellings[normalize_text(category)][category] += 1

    canonical = {norm: _canonical_spelling(counter) for norm, counter in spellings.items() if len(counter) > 1}
    for item in items:
        category = _text(item.get("category"))
        target = canonical.get(normalize_text(category))
        if target and category != target:
            add(SEVERITY_WARNING, "category_variant", item, "category",
                f"La categoría '{category}' está escrita de otra forma que '{target}'.",
                "Usa una sola escritura para que reportes y filtros la cuenten como una sola categoría.",
                fix=_fix(f"Unificar como «{target}»", item, {"category": target}))


# ---------------------------------------------------------------------------
# Resumen y correcciones
# ---------------------------------------------------------------------------

def summarize(findings: list, items: list = None) -> dict:
    counts = Counter(f.severity for f in findings)
    affected = {f.item_id for f in findings if f.item_id and f.severity in (SEVERITY_ERROR, SEVERITY_WARNING)}
    audited = [i for i in (items or []) if _text(i.get("status")) != "retired"]
    return {
        "items": len(audited),
        "errors": counts.get(SEVERITY_ERROR, 0),
        "warnings": counts.get(SEVERITY_WARNING, 0),
        "infos": counts.get(SEVERITY_INFO, 0),
        "fixable": len([f for f in findings if f.fix]),
        "items_with_problems": len(affected),
        "healthy_items": max(len(audited) - len(affected), 0),
    }


def group_by_severity(findings: list) -> dict:
    grouped = {severity: [] for severity in SEVERITIES}
    for finding in findings:
        grouped.setdefault(finding.severity, []).append(finding)
    return grouped


# Si dos correcciones tocan el mismo campo de un item, gana la de mayor prioridad.
_FIX_PRIORITY = ("location_conflict", "name_typo", "location_format", "location_missing",
                 "category_variant", "name_spaces", "description_typo")


def merge_fixes(findings: list) -> dict:
    """Agrupa las correcciones seguras por item: {item_id: fix_combinado}."""
    merged = {}
    ordered = sorted((f for f in findings if f.fix),
                     key=lambda f: _FIX_PRIORITY.index(f.rule) if f.rule in _FIX_PRIORITY else len(_FIX_PRIORITY))
    for finding in ordered:
        entry = merged.setdefault(finding.item_id, {
            "item_name": finding.item_name, "labels": [], "changes": {}, "before": {},
        })
        new_fields = [name for name in finding.fix["changes"] if name not in entry["changes"]]
        if not new_fields:
            continue
        entry["labels"].append(finding.fix["label"])
        for name in new_fields:
            entry["changes"][name] = finding.fix["changes"][name]
            entry["before"][name] = finding.fix["before"][name]
    for entry in merged.values():
        entry["label"] = "; ".join(entry["labels"])
    return merged


class StaleFixError(ValueError):
    """El item cambio despues del analisis: la correccion ya no es segura."""


def apply_fix(current_item: dict, fix: dict) -> dict:
    """Datos completos del item con la correccion aplicada, listos para
    `storage.save_item(data, item_id, is_new=False, ...)`. Lanza StaleFixError
    si algun campo ya no tiene el valor que se analizo."""
    if not current_item:
        raise StaleFixError("El producto ya no existe.")
    changed = [name for name, before in fix.get("before", {}).items() if _text(current_item.get(name)) != before]
    if changed:
        labels = ", ".join(FIELD_LABELS.get(name, name) for name in changed)
        raise StaleFixError(f"El producto cambió después del análisis ({labels}). Vuelve a revisarlo.")
    data = dict(current_item)
    data.update(fix["changes"])
    return data
