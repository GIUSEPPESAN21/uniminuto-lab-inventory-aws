# -*- coding: utf-8 -*-
"""
core/barcode.py - Validacion, construccion, interpretacion y resolucion de
codigos de inventario (ver core/labels.py para la generacion visual de
etiquetas y los nombres de tipo de item que ve el usuario).

Formatos aceptados para items NUEVOS, cada uno resuelto por regex.

Nomenclatura del laboratorio (Canvas de Estructura y Codificacion GLIOPS V3):

1. Estandar de 5 niveles, 100% numerico:
   [ESTANTERIA]-[PISO]-[CONTENEDOR]-[CAJA]-[ITEM]
   Solo digitos separados por guion. Estanteria: 1 a 3. Piso: 1 a 6.
   Contenedor: entero positivo. Caja e item: enteros positivos cuando aplican;
   los ceros finales representan niveles que no aplican:
   - 2-1-01-00-000: nivel contenedor (caja e item no aplican).
   - 2-1-01-01-000: nivel caja/subcontenedor (item no aplica).
   - 2-1-01-01-001: nivel item.
   No se permite un item positivo cuando la caja es 00.

2. Mesas de trabajo (equipos de alto valor), alfanumerico:
   M[1|2]-E[n]
   Ejemplo: M1-E2

3. Exhibicion Lego, alfanumerico:
   E3-LM[n]
   Ejemplo: E3-LM07

Codigos libres, para equipos que ya traen su propio codigo o laboratorios que
no usan GLIOPS:

4. Numerico libre: solo digitos 0-9, hasta NUMERIC_MAX_LEN. Los ceros a la
   izquierda son parte del codigo: el id siempre se guarda como texto.
   Ejemplo: 0012345

5. Alfanumerico libre: letras A-Z/a-z sin tildes ni ñ, digitos y los
   separadores - _ . entre ellos (sin espacios, sin separadores repetidos ni
   al inicio o al final), con al menos una letra y hasta ALNUM_MAX_LEN
   caracteres. El modo manual respeta exactamente mayusculas y minusculas;
   el generador normaliza su prefijo a mayusculas.
   Ejemplo: LAB-MIC-01

Las ubicaciones fisicas (estanterias, pisos, mesas y zonas) tienen sus propios
codigos, que solo se aceptan para items de ubicacion (ver "Codigos de
ubicacion" al final del modulo y core/places.py).

Un codigo con la FORMA de uno de GLIOPS que no cumple sus reglas se rechaza en
vez de aceptarse como codigo libre, para que un error de digitacion
(4-2-05-12-001, 2-1-01-00-001, M3-E1, m1-e2, E4-LM01) no cree un item fuera
de la nomenclatura. Todos los caracteres permitidos existen en Code 128 y los
limites de longitud garantizan que los codigos libres quepan legibles en la
etiqueta de 50x25 mm (ver core/labels.py).
"""

import logging
import re

logger = logging.getLogger(__name__)

FORMAT_STANDARD = "estandar_numerico"
FORMAT_MESA = "mesa_trabajo"
FORMAT_LEGO = "exhibicion_lego"
FORMAT_NUMERIC = "numerico_libre"
FORMAT_ALNUM = "alfanumerico_libre"

LEVEL_CONTAINER = "contenedor"
LEVEL_BOX = "caja"
LEVEL_ITEM = "item"

ESTANTERIA_MIN, ESTANTERIA_MAX = 1, 3
PISO_MIN, PISO_MAX = 1, 6

# Con estos limites el Code 128 de un codigo libre tiene como maximo 13
# simbolos de datos, que caben en la etiqueta de 50x25mm @ 203dpi con modulos
# de 2 puntos (0,25 mm), el ancho minimo recomendado para lectores USB.
NUMERIC_MAX_LEN = 20
ALNUM_MAX_LEN = 13

_SEPARATORS = "-_."

# re.ASCII: \d solo acepta 0-9 (no digitos Unicode como "１", que Code 128 no
# puede codificar).
_STANDARD_RE = re.compile(r"^(\d+)-(\d+)-(\d+)-(\d+)-(\d+)$", re.ASCII)
_MESA_RE = re.compile(r"^M([12])-E(\d+)$", re.ASCII)
_LEGO_RE = re.compile(r"^E3-LM(\d+)$", re.ASCII)
_NUMERIC_RE = re.compile(r"^\d+$", re.ASCII)
_ALNUM_RE = re.compile(r"^[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*$", re.ASCII)
_HAS_LETTER_RE = re.compile(r"[A-Za-z]", re.ASCII)

# "Forma" de los formatos GLIOPS (ver el docstring del modulo).
_STANDARD_SHAPE_RE = re.compile(r"^[\d-]*-[\d-]*$", re.ASCII)
_MESA_SHAPE_RE = re.compile(r"^M\d+-?E", re.ASCII | re.IGNORECASE)
_LEGO_SHAPE_RE = re.compile(r"^E\d+-?LM", re.ASCII | re.IGNORECASE)

FORMAT_HELP = (
    "Formatos aceptados: "
    "[ESTANTERIA]-[PISO]-[CONTENEDOR]-[CAJA]-[ITEM] 100% numerico; "
    "usa CAJA=00 e ITEM=000 para nivel contenedor (ej. 2-1-01-00-000), "
    "o ITEM=000 para nivel caja (ej. 2-1-01-01-000) · "
    "M1-E[n] o M2-E[n] para mesas de trabajo (ej. M1-E2) · "
    "E3-LM[n] para exhibicion Lego (ej. E3-LM07) · "
    f"numerico libre de hasta {NUMERIC_MAX_LEN} digitos (ej. 0012345) · "
    f"alfanumerico libre de hasta {ALNUM_MAX_LEN} caracteres con letras sin tildes, "
    "numeros y los separadores - _ . (ej. LAB-MIC-01)."
)


def _as_int(value, field: str, minimum: int = None, maximum: int = None) -> int:
    """Convierte un valor de formulario a entero y aplica limites con errores
    aptos para mostrar directamente en la interfaz."""
    if isinstance(value, bool):
        raise ValueError(f"{field} debe ser un numero entero.")
    try:
        if isinstance(value, str):
            raw = value.strip()
            if not re.fullmatch(r"\d+", raw, re.ASCII):
                raise ValueError
            number = int(raw)
        elif isinstance(value, float):
            if not value.is_integer():
                raise ValueError
            number = int(value)
        else:
            number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} debe ser un numero entero.") from None

    if minimum is not None and number < minimum:
        raise ValueError(f"{field} debe ser mayor o igual a {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} debe ser menor o igual a {maximum}.")
    return number


def _zero_padded(value, digits, field: str, max_digits: int) -> str:
    digits = _as_int(digits, f"Digitos de {field}", minimum=1, maximum=max_digits)
    number = _as_int(value, field, minimum=0)
    raw = str(number)
    if len(raw) > digits:
        raise ValueError(
            f"{field} ({raw}) ocupa {len(raw)} digitos y no cabe en el ancho configurado ({digits})."
        )
    return raw.zfill(digits)


def build_standard_code(estanteria, piso, contenedor, caja=0, item=0) -> str:
    """Construye el codigo GLIOPS canonico con relleno 2/2/3.

    `caja=0, item=0` representa el nivel contenedor; `item=0` con caja
    positiva representa el nivel caja/subcontenedor. Un item positivo exige
    una caja positiva."""
    estanteria = _as_int(
        estanteria, "Estanteria", minimum=ESTANTERIA_MIN, maximum=ESTANTERIA_MAX
    )
    piso = _as_int(piso, "Piso", minimum=PISO_MIN, maximum=PISO_MAX)
    contenedor = _as_int(contenedor, "Contenedor", minimum=1)
    caja = _as_int(caja, "Caja", minimum=0)
    item = _as_int(item, "Item", minimum=0)
    if caja == 0 and item != 0:
        raise ValueError("Item debe ser 000 cuando Caja es 00 (no aplica).")
    code = f"{estanteria}-{piso}-{contenedor:02d}-{caja:02d}-{item:03d}"
    validate_code_format(code)
    return code


def build_mesa_code(mesa, equipo) -> str:
    mesa = _as_int(mesa, "Mesa", minimum=1, maximum=2)
    equipo = _as_int(equipo, "Equipo", minimum=1)
    code = f"M{mesa}-E{equipo}"
    validate_code_format(code)
    return code


def build_lego_code(modelo, digits=2) -> str:
    modelo = _zero_padded(modelo, digits, "Modelo", max_digits=8)
    code = f"E3-LM{modelo}"
    validate_code_format(code)
    return code


def build_numeric_code(number, digits=7) -> str:
    code = _zero_padded(number, digits, "Numeracion", max_digits=NUMERIC_MAX_LEN)
    validate_code_format(code)
    return code


def build_prefixed_code(prefix: str, number, digits=2, separator: str = "-") -> str:
    """Construye un codigo alfanumerico canonico PREFIJO-NUMERO. El prefijo
    generado se normaliza a mayusculas; el modo manual conserva el texto."""
    prefix = (prefix or "").strip().upper()
    if not (_ALNUM_RE.fullmatch(prefix) and _HAS_LETTER_RE.search(prefix)):
        raise ValueError(
            "El prefijo debe contener letras sin tildes y solo puede usar letras, "
            "numeros y los separadores - _ . entre segmentos."
        )
    if separator not in _SEPARATORS:
        raise ValueError("El separador debe ser -, _ o .")
    serial = _zero_padded(number, digits, "Numeracion", max_digits=ALNUM_MAX_LEN)
    code = f"{prefix}{separator}{serial}"
    validate_code_format(code)
    return code


def parse_code(code: str) -> dict:
    """Identifica el formato de `code` (sin espacios alrededor) y lo
    descompone. Lanza ValueError si no coincide o un componente es invalido."""
    code = (code or "").strip()

    m = _STANDARD_RE.match(code)
    if m:
        estanteria, piso, contenedor, caja, item = (int(g) for g in m.groups())
        if not (ESTANTERIA_MIN <= estanteria <= ESTANTERIA_MAX):
            raise ValueError(
                f"Estanteria invalida en '{code}': debe estar entre "
                f"{ESTANTERIA_MIN} y {ESTANTERIA_MAX}."
            )
        if not (PISO_MIN <= piso <= PISO_MAX):
            raise ValueError(
                f"Piso invalido en '{code}': debe estar entre {PISO_MIN} y {PISO_MAX}."
            )
        if contenedor < 1:
            raise ValueError(f"Contenedor invalido en '{code}': debe ser un numero positivo.")
        if caja == 0 and item != 0:
            raise ValueError(
                f"Jerarquia invalida en '{code}': Item debe ser 000 cuando Caja es 00 (no aplica)."
            )
        return {
            "format": FORMAT_STANDARD,
            "estanteria": estanteria,
            "piso": piso,
            "contenedor": contenedor,
            "caja": caja,
            "item": item,
        }

    m = _MESA_RE.match(code)
    if m:
        mesa, equipo = int(m.group(1)), int(m.group(2))
        if equipo < 1:
            raise ValueError(f"Numero de equipo invalido en '{code}'.")
        return {"format": FORMAT_MESA, "mesa": mesa, "equipo": equipo}

    m = _LEGO_RE.match(code)
    if m:
        modelo = int(m.group(1))
        if modelo < 1:
            raise ValueError(f"Numero de modelo invalido en '{code}'.")
        return {"format": FORMAT_LEGO, "estanteria": 3, "modelo": modelo}

    # Tiene la forma de un formato GLIOPS pero no cumple sus reglas.
    if _STANDARD_SHAPE_RE.match(code):
        raise ValueError(
            f"'{code}' parece un codigo estandar pero no tiene los 5 niveles numericos "
            f"[ESTANTERIA]-[PISO]-[CONTENEDOR]-[CAJA]-[ITEM] (ej. 2-1-01-00-000)."
        )
    if _MESA_SHAPE_RE.match(code):
        raise ValueError(
            f"'{code}' usa el prefijo reservado de las mesas de trabajo: debe ser "
            f"M1-E[n] o M2-E[n], en mayusculas (ej. M1-E2)."
        )
    if _LEGO_SHAPE_RE.match(code):
        raise ValueError(
            f"'{code}' usa el prefijo reservado de la exhibicion Lego: debe ser "
            f"E3-LM[n], en mayusculas (ej. E3-LM07)."
        )

    if _NUMERIC_RE.match(code):
        if len(code) > NUMERIC_MAX_LEN:
            raise ValueError(
                f"El codigo numerico '{code}' tiene {len(code)} digitos; el maximo es "
                f"{NUMERIC_MAX_LEN} para que el codigo de barras quepa legible en la etiqueta."
            )
        return {"format": FORMAT_NUMERIC}

    if _ALNUM_RE.match(code) and _HAS_LETTER_RE.search(code):
        if len(code) > ALNUM_MAX_LEN:
            raise ValueError(
                f"El codigo '{code}' tiene {len(code)} caracteres; el maximo para un codigo "
                f"alfanumerico es {ALNUM_MAX_LEN} para que el codigo de barras quepa legible "
                f"en la etiqueta."
            )
        return {"format": FORMAT_ALNUM}

    raise ValueError(_invalid_code_message(code))


def standard_code_level(code_or_parsed):
    """Devuelve contenedor/caja/item para un codigo estandar ya analizado o
    para su texto; None para los demas formatos."""
    parsed = parse_code(code_or_parsed) if isinstance(code_or_parsed, str) else code_or_parsed
    if not parsed or parsed.get("format") != FORMAT_STANDARD:
        return None
    if parsed["caja"] == 0:
        return LEVEL_CONTAINER
    if parsed["item"] == 0:
        return LEVEL_BOX
    return LEVEL_ITEM


def _invalid_code_message(code: str) -> str:
    """Mensaje para un codigo que no cumple ningun formato. Nombra los
    caracteres no permitidos, si los hay, porque es el error mas comun al
    escribir a mano (espacios, tildes, ñ)."""
    forbidden = sorted({ch for ch in code if not (ch.isascii() and (ch.isalnum() or ch in _SEPARATORS))})
    if forbidden:
        shown = ", ".join(dict.fromkeys("espacio" if ch.isspace() else f"'{ch}'" for ch in forbidden))
        return f"'{code}' contiene caracteres no permitidos ({shown}). {FORMAT_HELP}"
    return f"'{code}' no coincide con ningun formato de codigo valido. {FORMAT_HELP}"


def detect_format(code: str):
    """Version silenciosa de parse_code: devuelve el formato o None."""
    try:
        return parse_code(code)["format"]
    except ValueError:
        return None


def is_valid_code(code: str) -> bool:
    return detect_format(code) is not None


def validate_code_format(code: str) -> str:
    """Valida `code` y devuelve el formato detectado. La capa de storage la
    invoca al crear items y labels antes de generar una etiqueta."""
    return parse_code(code)["format"]


def describe_parsed(parsed: dict) -> str:
    """Texto legible en español de los componentes interpretados."""
    if not parsed:
        return ""
    fmt = parsed.get("format")
    if fmt == FORMAT_STANDARD:
        caja = "N/A" if parsed["caja"] == 0 else f"{parsed['caja']:02d}"
        item = "N/A" if parsed["item"] == 0 else f"{parsed['item']:03d}"
        return (
            f"Estantería {parsed['estanteria']} · Piso {parsed['piso']} · "
            f"Contenedor {parsed['contenedor']:02d} · Caja {caja} · Ítem {item}"
        )
    if fmt == FORMAT_MESA:
        return f"Mesa de trabajo {parsed['mesa']} · Equipo {parsed['equipo']}"
    if fmt == FORMAT_LEGO:
        return f"Estantería 3 (Exhibición Lego) · Modelo {parsed['modelo']}"
    if fmt == FORMAT_NUMERIC:
        return "Código numérico libre (sin ubicación codificada)"
    if fmt == FORMAT_ALNUM:
        return "Código alfanumérico libre (sin ubicación codificada)"
    if fmt == FORMAT_LOCATION:
        return describe_location(parsed)
    return ""


# ---------------------------------------------------------------------------
# Codigos de ubicacion (estanterias, pisos, mesas de trabajo y zonas)
# ---------------------------------------------------------------------------
# Una ubicacion fisica (item_type "location", ver core/places.py) lleva su
# propia etiqueta para escanearla. Sus codigos reutilizan los formatos GLIOPS
# con ceros en los niveles que no aplican, combinaciones que `parse_code`
# RECHAZA (piso 0, contenedor 00, equipo 0, modelo 00). Por eso nunca chocan con
# un contenedor, una caja o un producto, y solo se aceptan para ubicaciones:
#
#   2-0-00-00-000   Estanteria 2
#   2-1-00-00-000   Estanteria 2 · Piso 1
#   M1-E0           Mesa de trabajo 1
#   E3-LM00         Exhibicion Lego (zona de la Estanteria 3)
#
# Una zona, sala o subnivel sin nivel GLIOPS usa un codigo libre (numerico o
# alfanumerico, ej. SALA-A o E2-P1-IZQ).

FORMAT_LOCATION = "ubicacion"
LOCATION_SHELF = "estanteria"
LOCATION_FLOOR = "piso"
LOCATION_TABLE = "mesa"
LOCATION_ZONE = "zona"
LOCATION_KINDS = (LOCATION_SHELF, LOCATION_FLOOR, LOCATION_TABLE, LOCATION_ZONE)
LOCATION_KIND_NAMES = {
    LOCATION_SHELF: "Estantería",
    LOCATION_FLOOR: "Piso",
    LOCATION_TABLE: "Mesa de trabajo",
    LOCATION_ZONE: "Zona",
}
LEGO_ZONE_CODE = "E3-LM00"

LOCATION_FORMAT_HELP = (
    "Códigos de ubicación: [ESTANTERIA]-0-00-00-000 para una estantería (ej. 2-0-00-00-000) · "
    "[ESTANTERIA]-[PISO]-00-00-000 para un piso (ej. 2-1-00-00-000) · "
    "M1-E0 o M2-E0 para una mesa de trabajo · E3-LM00 para la exhibición Lego · "
    f"o un código libre (hasta {ALNUM_MAX_LEN} caracteres, ej. SALA-A) para una zona, sala o subnivel."
)


def build_shelf_code(estanteria) -> str:
    """Codigo de la estanteria: 2 -> 2-0-00-00-000."""
    estanteria = _as_int(estanteria, "Estanteria", minimum=ESTANTERIA_MIN, maximum=ESTANTERIA_MAX)
    return f"{estanteria}-0-00-00-000"


def build_floor_code(estanteria, piso) -> str:
    """Codigo de un piso de la estanteria: (2, 1) -> 2-1-00-00-000."""
    estanteria = _as_int(estanteria, "Estanteria", minimum=ESTANTERIA_MIN, maximum=ESTANTERIA_MAX)
    piso = _as_int(piso, "Piso", minimum=PISO_MIN, maximum=PISO_MAX)
    return f"{estanteria}-{piso}-00-00-000"


def build_table_code(mesa) -> str:
    """Codigo de la mesa de trabajo (equipo 0 = la mesa misma): 1 -> M1-E0."""
    mesa = _as_int(mesa, "Mesa", minimum=1, maximum=2)
    return f"M{mesa}-E0"


def parse_location_code(code: str) -> dict:
    """Interpreta un codigo de ubicacion ESTRUCTURADO (estanteria, piso, mesa o
    exhibicion Lego). Lanza ValueError para cualquier otro codigo, incluidos los
    de contenedores, cajas, productos y los codigos libres."""
    code = (code or "").strip()
    m = _STANDARD_RE.match(code)
    if m:
        estanteria, piso, contenedor, caja, item = (int(g) for g in m.groups())
        if (contenedor, caja, item) != (0, 0, 0):
            raise ValueError(
                f"'{code}' no es un código de ubicación: en una estantería o un piso los niveles "
                "contenedor, caja e ítem son 00-00-000."
            )
        if not (ESTANTERIA_MIN <= estanteria <= ESTANTERIA_MAX):
            raise ValueError(
                f"Estanteria invalida en '{code}': debe estar entre {ESTANTERIA_MIN} y {ESTANTERIA_MAX}."
            )
        if piso == 0:
            return {"format": FORMAT_LOCATION, "kind": LOCATION_SHELF, "estanteria": estanteria}
        if not (PISO_MIN <= piso <= PISO_MAX):
            raise ValueError(f"Piso invalido en '{code}': debe estar entre {PISO_MIN} y {PISO_MAX}.")
        return {"format": FORMAT_LOCATION, "kind": LOCATION_FLOOR, "estanteria": estanteria, "piso": piso}

    m = _MESA_RE.match(code)
    if m and int(m.group(2)) == 0:
        return {"format": FORMAT_LOCATION, "kind": LOCATION_TABLE, "mesa": int(m.group(1))}

    m = _LEGO_RE.match(code)
    if m and int(m.group(1)) == 0:
        return {"format": FORMAT_LOCATION, "kind": LOCATION_ZONE, "estanteria": 3, "lego": True}

    raise ValueError(f"'{code}' no es un código de ubicación. {LOCATION_FORMAT_HELP}")


def location_code(code: str):
    """Version silenciosa de parse_location_code: dict o None."""
    try:
        return parse_location_code(code)
    except ValueError:
        return None


def is_location_code(code: str) -> bool:
    """True si `code` es un codigo de ubicacion estructurado (no libre)."""
    return location_code(code) is not None


def validate_location_code(code: str) -> dict:
    """Valida el codigo de una ubicacion NUEVA y devuelve su interpretacion.

    Acepta los codigos de ubicacion estructurados y, para zonas, salas o
    subniveles, un codigo libre (numerico o alfanumerico). Rechaza los codigos
    de contenedores, cajas, productos, equipos de mesa y modelos Lego: una
    ubicacion nunca puede tomar el codigo de algo que se guarda en ella."""
    code = (code or "").strip()
    place = location_code(code)
    if place:
        return place
    try:
        parsed = parse_code(code)
    except ValueError:
        if _STANDARD_RE.match(code):
            # Forma estandar con niveles de ubicacion fuera de rango: el mensaje especifico ayuda mas.
            parse_location_code(code)
        forbidden = sorted({ch for ch in code if not (ch.isascii() and (ch.isalnum() or ch in _SEPARATORS))})
        detail = ""
        if forbidden:
            shown = ", ".join(dict.fromkeys("espacio" if ch.isspace() else f"'{ch}'" for ch in forbidden))
            detail = f" Contiene caracteres no permitidos ({shown})."
        raise ValueError(f"'{code}' no es un código de ubicación válido.{detail} {LOCATION_FORMAT_HELP}") from None
    fmt = parsed["format"]
    if fmt in (FORMAT_NUMERIC, FORMAT_ALNUM):
        return {"format": FORMAT_LOCATION, "kind": LOCATION_ZONE, "free": True}
    if fmt == FORMAT_STANDARD:
        level = {LEVEL_CONTAINER: "un contenedor", LEVEL_BOX: "una caja", LEVEL_ITEM: "un ítem"}[
            standard_code_level(parsed)]
        what = f"{level} ({describe_parsed(parsed)})"
    elif fmt == FORMAT_MESA:
        what = f"un equipo de la Mesa de trabajo {parsed['mesa']}"
    else:
        what = "un modelo de la exhibición Lego"
    raise ValueError(
        f"'{code}' es el código de {what} y no puede usarse para una ubicación. {LOCATION_FORMAT_HELP}"
    )


def describe_location(place: dict) -> str:
    """Texto legible de una ubicacion ya interpretada."""
    if not place:
        return ""
    kind = place.get("kind")
    if kind == LOCATION_SHELF:
        return f"Estantería {place['estanteria']}"
    if kind == LOCATION_FLOOR:
        return f"Estantería {place['estanteria']} · Piso {place['piso']}"
    if kind == LOCATION_TABLE:
        return f"Mesa de trabajo {place['mesa']}"
    if place.get("lego"):
        return "Estantería 3 · Exhibición Lego"
    return "Zona, sala o subnivel (código libre)"


def location_kind(code: str, is_location_item: bool = False) -> str:
    """Tipo de ubicacion de un codigo: el de un codigo estructurado o, para el
    codigo libre de un item de ubicacion, "zona". "" si no es una ubicacion."""
    place = location_code(code)
    if place:
        return place["kind"]
    return LOCATION_ZONE if is_location_item else ""


def location_phrase(place: dict) -> str:
    """La ubicacion con articulo, para frases: "la Estanteria 2", "el Piso 1 de
    la Estanteria 2", "la Mesa de trabajo 1", "la exhibicion Lego"."""
    kind = (place or {}).get("kind")
    if kind == LOCATION_SHELF:
        return f"la Estantería {place['estanteria']}"
    if kind == LOCATION_FLOOR:
        return f"el Piso {place['piso']} de la Estantería {place['estanteria']}"
    if kind == LOCATION_TABLE:
        return f"la Mesa de trabajo {place['mesa']}"
    if (place or {}).get("lego"):
        return "la exhibición Lego de la Estantería 3"
    return "esa zona"


def location_parent_problem(child_code: str, parent_code: str) -> str:
    """Por que `child_code` no puede estar dentro de la ubicacion `parent_code`
    ("" si puede). Un piso va en su estanteria; la exhibicion Lego, en la
    Estanteria 3; una estanteria o una mesa solo dentro de una zona o sala (codigo
    libre). Una zona con codigo libre puede estar dentro de cualquier ubicacion, y
    cualquier ubicacion dentro de una zona con codigo libre."""
    child, parent = location_code(child_code), location_code(parent_code)
    if child is None or parent is None:
        return ""
    where = location_phrase(parent)
    if child["kind"] == LOCATION_FLOOR:
        if parent["kind"] == LOCATION_SHELF and parent["estanteria"] == child["estanteria"]:
            return ""
        return (f"El Piso {child['piso']} de la Estantería {child['estanteria']} va dentro de la "
                f"Estantería {child['estanteria']}, no en {where}.")
    if child.get("lego"):
        if parent["kind"] in (LOCATION_SHELF, LOCATION_FLOOR) and parent["estanteria"] == 3:
            return ""
        return f"La exhibición Lego está en la Estantería 3, no en {where}."
    own = location_phrase(child)
    return f"{own[0].upper()}{own[1:]} solo puede estar dentro de una zona o sala con código libre, no en {where}."


def scan(storage, code: str) -> dict:
    code = (code or "").strip()
    if not code:
        return {"status": "error", "message": "El codigo de barras no puede estar vacio."}

    parsed = None
    try:
        parsed = parse_code(code)
    except ValueError:
        parsed = None  # ids heredados fuera de los formatos siguen pudiendo buscarse

    try:
        item = storage.get_item(code)
        if not item:
            result = {"status": "not_found", "barcode": code, "parsed": parsed}
            place = location_code(code)
            if place:
                # Codigo de una ubicacion aun no registrada: la vista igual muestra lo que guarda.
                result["place"] = place
            return result

        if item.get("item_type") == "location" and item.get("status") != "retired":
            # Estanteria, piso, mesa o zona: no tiene stock; la vista muestra lo que guarda.
            return {
                "status": "found_location", "item": item, "parsed": parsed,
                "place": location_code(code) or {"format": FORMAT_LOCATION, "kind": LOCATION_ZONE, "free": True},
            }

        if item.get("status") == "retired":
            # El codigo de un item dado de baja queda libre: `retired` permite a la
            # interfaz ofrecer registrarlo de nuevo en vez de dejar un callejon sin salida.
            return {
                "status": "error", "retired": True, "barcode": code, "parsed": parsed,
                "message": f"El item '{item.get('name')}' fue dado de baja.",
            }

        if item.get("item_type") == "master":
            children = storage.get_children(code)
            for child in children:
                child["available"] = storage.get_available_quantity(child["id"])
            return {"status": "found_master", "item": item, "children": children, "parsed": parsed}

        item["available"] = storage.get_available_quantity(code)
        parent = None
        if item.get("parent_id"):
            parent = storage.get_item(item["parent_id"])
        return {"status": "found_item", "item": item, "parent": parent, "parsed": parsed}

    except Exception as e:
        logger.error(f"Error al escanear el codigo '{code}': {e}")
        return {"status": "error", "message": str(e)}
