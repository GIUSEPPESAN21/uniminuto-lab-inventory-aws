# -*- coding: utf-8 -*-
"""
core/labels.py - Nomenclatura unica para los tipos de item, usada por todas
las vistas para que el lenguaje sea consistente en toda la aplicacion.

Los valores internos ("master", "child", "standalone") se mantienen tal cual
en core/storage.py y en los CSV de importacion (son claves tecnicas, no
texto para el usuario). Este modulo es la UNICA fuente de los nombres que
ve el usuario, para poder evolucionarlos sin tocar la logica de negocio.

- Contenedor Principal ("master"): la caja, kit o gabinete fisico que agrupa
  productos relacionados.
- Contenedor de Caracteristica ("child"): una subdivision con nombre propio
  DENTRO de un Contenedor Principal, que agrupa unidades que comparten una
  caracteristica (ej. "Resistencias 220 ohm", "Tornillos M4 x 20mm").
- Item Individual ("standalone"): un producto con codigo propio que no
  pertenece a ningun Contenedor Principal.
- Ubicacion ("location"): una estanteria, un piso, una mesa de trabajo o una
  zona con etiqueta propia; no tiene stock (ver core/places.py).
"""

ITEM_TYPE_ICONS = {
    "master": "🗄️",
    "child": "🧩",
    "standalone": "🔹",
    "location": "📍",
}

ITEM_TYPE_NAMES = {
    "master": "Contenedor Principal",
    "child": "Contenedor de Característica",
    "standalone": "Ítem Individual",
    "location": "Ubicación",
}

ITEM_TYPE_LABELS = {k: f"{ITEM_TYPE_ICONS[k]} {v}" for k, v in ITEM_TYPE_NAMES.items()}

# Opciones en el orden en que se muestran en los formularios de creacion.
ITEM_TYPE_CHOICES = [
    ITEM_TYPE_NAMES["standalone"],
    ITEM_TYPE_NAMES["master"],
    ITEM_TYPE_NAMES["child"],
]

ITEM_TYPE_BY_CHOICE = {
    ITEM_TYPE_NAMES["standalone"]: "standalone",
    ITEM_TYPE_NAMES["master"]: "master",
    ITEM_TYPE_NAMES["child"]: "child",
}

ITEM_TYPE_HELP = {
    "standalone": "Un producto con su propio codigo, sin contenedor (ej. un multimetro).",
    "master": "La caja, kit o gabinete fisico que va a agrupar productos (ej. 'Caja de Electronica').",
    "child": "Una subdivision DENTRO de un Contenedor Principal ya creado, para una caracteristica "
             "especifica (ej. 'Resistencias 220 ohm', 'Tornillos M4').",
    "location": "Una estanteria, un piso, una mesa de trabajo o una zona con su propia etiqueta "
                "(se crean en Inventario > Ubicaciones).",
}


def type_label(item_type: str) -> str:
    return ITEM_TYPE_LABELS.get(item_type, item_type)


def type_name(item_type: str) -> str:
    return ITEM_TYPE_NAMES.get(item_type, item_type)


# ---------------------------------------------------------------------------
# Generacion visual de etiquetas imprimibles (marca + producto + Code 128)
# ---------------------------------------------------------------------------
# La etiqueta se compone para su TAMAÑO REAL (ancho x alto en mm, configurable
# por el laboratorio) y la resolucion REAL de la impresora (SAT TT460: 203
# dpi). Cada pixel del raster es un punto del cabezal y el PDF mide
# exactamente lo mismo que la etiqueta: impreso "al 100 %" no se reduce ni se
# amplia. Si la etiqueta sale pequeña, casi siempre el tamaño configurado (o el
# papel del driver) no coincide con el rollo real: ver la hoja de prueba.
#
# Formato clasico del laboratorio (encabezado con logo, banda negra con el
# nombre, linea de datos, barras y codigo), escalado al tamaño configurado;
# solo el contenido se ajusta para caber (ver "Formato de la etiqueta"). Las
# letras se rasterizan en monocromo con hinting: trazos uniformes y letras
# separadas, sin el "empaste" que produce suavizar y luego umbralizar a 1 bit.
#
# Code 128 codifica todo el ASCII imprimible, asi que sirve tal cual para los
# codigos numericos y alfanumericos (ver core/barcode.py) y lo leen los
# lectores USB 1D que ya usa el laboratorio (un QR exigiria lectores 2D).
# Las barras se dibujan con un numero ENTERO de puntos de impresora por
# modulo y sin reescalar: un modulo fraccionario produce barras de anchos
# desiguales que un lector puede no reconocer.

import functools
import io
import json
import math
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from barcode.charsets import code128 as _code128_tables
from PIL import Image, ImageDraw, ImageFont, ImageOps

from core import barcode as barcode_rules
from core.barcode import is_valid_code, validate_code_format


LABEL_DPI = 203  # resolucion nativa de la SAT TT460
SUPPORTED_DPI = (203, 300)
LABEL_WIDTH_MM = 50
LABEL_HEIGHT_MM = 25
# La SAT TT460 acepta rollos de 20 a 112 mm de ancho; su cabezal de 4 pulgadas
# imprime hasta ~104 mm. El alto cubre desde etiquetas pequeñas hasta 4 x 6 in
# (152,4 mm).
MIN_LABEL_WIDTH_MM, MAX_LABEL_WIDTH_MM = 20, 104
MIN_LABEL_HEIGHT_MM, MAX_LABEL_HEIGHT_MM = 15, 160
LABEL_SPEC_SETTING_KEY = "label_spec"

# Tamaños de rollo habituales (ancho x alto en mm).
LABEL_SIZE_PRESETS = {
    "50x25": (50, 25),
    "50x30": (50, 30),
    "60x40": (60, 40),
    "100x50": (100, 50),
    "100x75": (100, 75),
    "100x100": (100, 100),
    "100x150": (100, 150),
}

# Contenido opcional que el laboratorio puede activar o desactivar. El nombre,
# el codigo de barras y el codigo legible siempre se imprimen.
LABEL_CONTENT_OPTIONS = {
    "brand": "Logo y «Laboratorio de Ingeniería»",
    "notice": "Aviso «No retirar sin préstamo»",
    "route": "Ruta física (estantería › piso › contenedor…)",
    "location": "Ubicación registrada",
    "type": "Tipo de activo",
    "category": "Categoría",
}

LABEL_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "uniminuto-logo.png"
LABEL_INSTITUTION_TEXT = "LABORATORIO DE INGENIERÍA"
# Para omitir todo el encabezado institucional, pasa notice=None (o "").
LABEL_NOTICE_TEXT = "ACTIVO INSTITUCIONAL · NO RETIRAR SIN PRÉSTAMO"
LABEL_NOTICE_SHORT_TEXT = "NO RETIRAR SIN PRÉSTAMO"
# Aviso de las etiquetas de ubicacion (estanteria, piso, mesa, zona): no se
# prestan, son puntos de control de la ruta verificable.
LOCATION_NOTICE_TEXT = "PUNTO DE CONTROL · ESCANÉALO AL LLEGAR"

# Barras: modulo entre 0,25 mm (minimo recomendado para lectores USB) y
# 0,5 mm; zona de silencio de 10 modulos si cabe (ISO/IEC 15417), nunca menos
# de 6. El alto (9 a 10 mm a 50 x 25) lo fija el formato de la etiqueta.
_MIN_QUIET_MODULES = 6
_PREFERRED_QUIET_MODULES = 10
_MAX_MODULE_DOTS = 4  # 0,5 mm a 203 dpi
_MIN_MODULE_MM = 0.25
_MAX_MODULE_MM = 0.5
_SHRINK_LIMIT = 0.72  # un texto de la hoja de prueba se reduce como mucho al 72 %
# En el PDF el raster empieza 0,01 puntos de impresora hacia adentro y termina
# 0,001 antes de su ultimo punto (ver _label_image_to_pdf). Ambos superan con
# holgura el error numerico de los visores y ninguno mueve el centro de un pixel.
_PDF_INSET_START_DOTS = 0.01
_PDF_INSET_END_DOTS = 0.001


@dataclass(frozen=True)
class _TextStyle:
    pt: float          # tamaño preferido en la etiqueta de referencia (50 x 25 mm)
    floor_pt: float    # piso absoluto de legibilidad: nunca se imprime mas pequeño
    tracking: float    # espacio extra entre letras, en fraccion del tamaño (em)
    word_spacing: float = 0.0
    shrink: float = _SHRINK_LIMIT  # cuanto puede reducirse para caber (0 = hasta el piso)


# Estilos de la hoja de prueba de impresion (y del respaldo sin logo).
_STYLES = {
    "lab": _TextStyle(6.6, 5.4, 0.07, 0.10, shrink=0.0),
    "notice": _TextStyle(5.8, 5.0, 0.05, 0.10, shrink=0.0),
    "name": _TextStyle(9.0, 7.0, 0.03, 0.08),
    "meta": _TextStyle(6.2, 5.2, 0.03, 0.10),
    "code": _TextStyle(9.5, 6.0, 0.06, 0.0),
}

_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

_START_B, _START_C = 104, 105
_TO_B_FROM_C, _TO_C_FROM_B = 100, 99
_STOP_PATTERN = _code128_tables.STOP + "11"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _mm_to_dots(mm: float, dpi: int) -> float:
    return mm * dpi / 25.4


def _pt_to_px(pt: float, dpi: int) -> float:
    return pt * dpi / 72


def _format_mm(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def _format_inches(mm: float) -> str:
    return f"{round(mm / 25.4, 2):g}".replace(".", ",")


# ---------------------------------------------------------------------------
# Especificacion de la etiqueta (tamaño real, resolucion y contenido)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LabelSpec:
    """Tamaño fisico real de la etiqueta, resolucion de la impresora y
    contenido opcional a imprimir. Valida sus valores al construirse."""

    width_mm: float = LABEL_WIDTH_MM
    height_mm: float = LABEL_HEIGHT_MM
    dpi: int = LABEL_DPI
    content: tuple = tuple(LABEL_CONTENT_OPTIONS)

    def __post_init__(self):
        try:
            width = round(float(self.width_mm), 1)
            height = round(float(self.height_mm), 1)
            dpi = int(self.dpi)
        except (TypeError, ValueError):
            raise ValueError("El ancho, el alto y la resolución deben ser números.") from None
        if not MIN_LABEL_WIDTH_MM <= width <= MAX_LABEL_WIDTH_MM:
            raise ValueError(
                f"El ancho de la etiqueta debe estar entre {MIN_LABEL_WIDTH_MM} y "
                f"{MAX_LABEL_WIDTH_MM} mm (la SAT TT460 imprime hasta ~104 mm)."
            )
        if not MIN_LABEL_HEIGHT_MM <= height <= MAX_LABEL_HEIGHT_MM:
            raise ValueError(
                f"El alto de la etiqueta debe estar entre {MIN_LABEL_HEIGHT_MM} y "
                f"{MAX_LABEL_HEIGHT_MM} mm."
            )
        if dpi not in SUPPORTED_DPI:
            raise ValueError("La resolución debe ser 203 dpi (SAT TT460) o 300 dpi.")
        content = self.content
        if isinstance(content, str):
            content = (content,)
        wanted = set(content or ())
        object.__setattr__(self, "width_mm", width)
        object.__setattr__(self, "height_mm", height)
        object.__setattr__(self, "dpi", dpi)
        object.__setattr__(self, "content", tuple(k for k in LABEL_CONTENT_OPTIONS if k in wanted))

    @property
    def canvas_size(self) -> tuple:
        """Puntos de impresora que caben en la etiqueta (sin pasarse del borde)."""
        return (
            int(_mm_to_dots(self.width_mm, self.dpi) + 1e-6),
            int(_mm_to_dots(self.height_mm, self.dpi) + 1e-6),
        )

    @property
    def size_text(self) -> str:
        return f"{_format_mm(self.width_mm)} × {_format_mm(self.height_mm)} mm"

    @property
    def inches_text(self) -> str:
        """La misma medida en pulgadas, como la muestran los drivers (4 × 3 in)."""
        return f"{_format_inches(self.width_mm)} × {_format_inches(self.height_mm)} in"

    def describe(self) -> str:
        return f"{self.size_text} · {self.dpi} dpi"

    @property
    def preset_key(self) -> str:
        for key, (width, height) in LABEL_SIZE_PRESETS.items():
            if (width, height) == (self.width_mm, self.height_mm):
                return key
        return "custom"

    def shows(self, key: str) -> bool:
        return key in self.content

    def to_dict(self) -> dict:
        return {
            "width_mm": self.width_mm, "height_mm": self.height_mm,
            "dpi": self.dpi, "content": list(self.content),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LabelSpec":
        """Construye desde datos guardados. Lanza ValueError si no son validos."""
        if not isinstance(data, dict):
            raise ValueError("Configuración de etiqueta inválida.")
        content = data.get("content", tuple(LABEL_CONTENT_OPTIONS))
        if not isinstance(content, (list, tuple)):
            raise ValueError("El contenido de la etiqueta debe ser una lista.")
        return cls(
            width_mm=data.get("width_mm", LABEL_WIDTH_MM),
            height_mm=data.get("height_mm", LABEL_HEIGHT_MM),
            dpi=data.get("dpi", LABEL_DPI),
            content=tuple(content),
        )


LABEL_CANVAS_SIZE = LabelSpec().canvas_size


def load_label_spec(storage) -> LabelSpec:
    """Configuracion de etiqueta guardada por el laboratorio, o la de fabrica
    (50 x 25 mm, 203 dpi) si no hay ninguna o el dato esta dañado: imprimir una
    etiqueta nunca debe fallar por la configuracion."""
    getter = getattr(storage, "get_setting", None)
    if getter is None:
        return LabelSpec()
    try:
        raw = getter(LABEL_SPEC_SETTING_KEY)
        return LabelSpec.from_dict(json.loads(raw)) if raw else LabelSpec()
    except Exception:
        return LabelSpec()


def save_label_spec(storage, spec: LabelSpec, actor_email: str = "") -> None:
    storage.set_setting(LABEL_SPEC_SETTING_KEY, json.dumps(spec.to_dict()), actor_email=actor_email)


# ---------------------------------------------------------------------------
# Fuentes, logo y medicion de texto
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def _load_font(size: int, bold: bool = True):
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR + _FONT_CANDIDATES_BOLD
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


@functools.lru_cache(maxsize=1)
def _load_brand_logo():
    """Logo versionado, monocromatico y recortado; None activa fallback."""
    try:
        with Image.open(LABEL_LOGO_PATH) as source:
            rgba = source.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            gray = ImageOps.grayscale(white.convert("RGB"))
            mono = gray.point(lambda value: 0 if value < 248 else 255)
            bbox = ImageOps.invert(mono).getbbox()
            return mono.crop(bbox).copy() if bbox else None
    except (OSError, ValueError):
        return None


@functools.lru_cache(maxsize=16384)
def _advance(char: str, size: int) -> float:
    return _load_font(size).getlength(char, mode="1")


@functools.lru_cache(maxsize=65536)
def _measure(text: str, size: int, tracking_em: float, word_spacing_em: float) -> float:
    """Ancho de un texto en la fuente de la etiqueta (cacheado: el diseño
    adaptable prueba muchas combinaciones con los mismos textos)."""
    return (
        sum(_advance(char, size) for char in text)
        + max(0, len(text) - 1) * tracking_em * size
        + text.count(" ") * word_spacing_em * size
    )


def _text_width(text: str, font, tracking: float = 0, word_spacing: float = 0) -> float:
    """Ancho visual con espaciado independiente de letras y palabras. Suma los
    avances letra por letra, igual que se dibuja (el espaciado no admite kerning)."""
    return (
        sum(font.getlength(char, mode="1") for char in text)
        + max(0, len(text) - 1) * tracking
        + text.count(" ") * word_spacing
    )


@functools.lru_cache(maxsize=4096)
def _ink_extent(text: str, size: int) -> tuple:
    """(arriba, abajo) de la tinta del texto respecto al origen de dibujo."""
    _, top, _, bottom = _load_font(size).getbbox(text, mode="1")
    return top, bottom


def _ink_height(text: str, font) -> int:
    _, top, _, bottom = font.getbbox(text, mode="1")
    return max(1, bottom - top)


def _clean_text(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _ellipsize(text: str, font, max_width: float, tracking: float = 0,
               word_spacing: float = 0) -> str:
    if _text_width(text, font, tracking, word_spacing) <= max_width:
        return text
    while text and _text_width(text.rstrip() + "…", font, tracking, word_spacing) > max_width:
        text = text[:-1]
    return text.rstrip() + "…" if text.strip() else ""


# ---------------------------------------------------------------------------
# Code 128
# ---------------------------------------------------------------------------

def _code128_values(code: str) -> list:
    """Valores Code 128: inicio, datos y checksum, preservando el texto."""
    if not code or any(not (32 <= ord(ch) <= 126) for ch in code):
        raise ValueError(f"Code 128 solo admite texto ASCII imprimible: {code!r}")
    values = []
    charset = None

    def use(target: str) -> None:
        nonlocal charset
        if charset == target:
            return
        if charset is None:
            values.append(_START_B if target == "B" else _START_C)
        else:
            values.append(_TO_B_FROM_C if target == "B" else _TO_C_FROM_B)
        charset = target

    i, n = 0, len(code)
    while i < n:
        j = i
        while j < n and code[j] in "0123456789":
            j += 1
        digits = j - i
        if digits >= 4 or (digits == 2 and i == 0 and j == n):
            if digits % 2:
                use("B")
                values.append(ord(code[i]) - 32)
                i += 1
            use("C")
            while i < j:
                values.append(int(code[i:i + 2]))
                i += 2
        else:
            use("B")
            values.append(ord(code[i]) - 32)
            i += 1
    checksum = values[0] + sum(pos * value for pos, value in enumerate(values[1:], start=1))
    return values + [checksum % 103]


def _code128_modules(code: str) -> str:
    return "".join(_code128_tables.CODES[v] for v in _code128_values(code)) + _STOP_PATTERN


def _module_dots(n_modules: int, width_px: int, max_dots: int = _MAX_MODULE_DOTS,
                 quiet_modules: int = _MIN_QUIET_MODULES) -> int:
    for dots in range(max_dots, 0, -1):
        if (n_modules + 2 * quiet_modules) * dots <= width_px:
            return dots
    return 0


def _barcode_geometry(n_modules: int, width_px: int, dpi: int) -> tuple:
    """(puntos por modulo, modulos de zona de silencio). Modulo de hasta
    0,5 mm; la zona de silencio sube a 10 modulos cuando cabe sin achicarlo."""
    max_dots = max(1, round(_mm_to_dots(_MAX_MODULE_MM, dpi)))
    dots = _module_dots(n_modules, width_px, max_dots, _MIN_QUIET_MODULES)
    if not dots:
        return 0, _MIN_QUIET_MODULES
    quiet = _PREFERRED_QUIET_MODULES
    if (n_modules + 2 * quiet) * dots > width_px:
        quiet = _MIN_QUIET_MODULES
    return dots, quiet


# ---------------------------------------------------------------------------
# Composicion
# ---------------------------------------------------------------------------

def _place_guide(code: str) -> str:
    """Ruta compacta de un codigo de ubicacion (estanteria, piso, mesa o zona Lego)."""
    place = barcode_rules.location_code(code)
    if not place:
        return ""
    kind = place["kind"]
    if kind == barcode_rules.LOCATION_SHELF:
        return f"RUTA: E{place['estanteria']}"
    if kind == barcode_rules.LOCATION_FLOOR:
        return f"RUTA: E{place['estanteria']} › P{place['piso']}"
    if kind == barcode_rules.LOCATION_TABLE:
        return f"RUTA: MESA {place['mesa']}"
    return "RUTA: E3 › LEGO"


def _is_printable(code: str) -> bool:
    """Codigo que puede ir en una etiqueta: de inventario o de ubicacion."""
    return is_valid_code(code) or barcode_rules.is_location_code(code)


def _validate_printable(code: str) -> None:
    """Como validate_code_format, pero tambien acepta los codigos de ubicacion."""
    if not barcode_rules.is_location_code(code):
        validate_code_format(code)


def _type_text(item_type: str, code: str) -> str:
    """Tipo de activo del encabezado; para una ubicacion, "Ubicación · Piso"."""
    if item_type == "location":
        kind = barcode_rules.LOCATION_KIND_NAMES.get(barcode_rules.location_kind(code, True), "")
        return f"{type_name(item_type)} · {kind}" if kind else type_name(item_type)
    return type_name(item_type) or "Activo de laboratorio"


def _location_guide(code: str) -> str:
    """Ruta fisica compacta derivada del codigo, sin cambiar el Code 128."""
    try:
        parsed = barcode_rules.parse_code(code)
    except ValueError:
        return _place_guide(code)
    fmt = parsed.get("format")
    if fmt == barcode_rules.FORMAT_STANDARD:
        parts = [
            f"E{parsed['estanteria']}",
            f"P{parsed['piso']}",
            f"C{parsed['contenedor']:02d}",
        ]
        if parsed["caja"]:
            parts.append(f"CJ{parsed['caja']:02d}")
        if parsed["item"]:
            parts.append(f"I{parsed['item']:03d}")
        return "RUTA: " + " › ".join(parts)
    if fmt == barcode_rules.FORMAT_MESA:
        return f"RUTA: MESA {parsed['mesa']} › EQ{parsed['equipo']}"
    if fmt == barcode_rules.FORMAT_LEGO:
        return f"RUTA: E3 › LEGO {parsed['modelo']:02d}"
    return ""


@dataclass
class LabelLine:
    """Una linea de texto ya ubicada: `x`/`top` son el borde izquierdo y la
    parte superior de su tinta, en puntos de impresora."""

    role: str
    text: str
    size: int
    tracking: float = 0.0
    word_spacing: float = 0.0
    x: int = 0
    top: int = 0
    inverse: bool = False

    @property
    def font(self):
        return _load_font(self.size)

    @property
    def ink_height(self) -> int:
        top, bottom = _ink_extent(self.text, self.size)
        return max(1, bottom - top)

    @property
    def width(self) -> float:
        return (
            sum(_advance(char, self.size) for char in self.text)
            + max(0, len(self.text) - 1) * self.tracking
            + self.text.count(" ") * self.word_spacing
        )

    @property
    def box(self) -> tuple:
        return (self.x, self.top, self.x + math.ceil(self.width), self.top + self.ink_height)

    def pt(self, dpi: int) -> float:
        return self.size * 72 / dpi


@dataclass
class LabelLayout:
    """Etiqueta ya compuesta: todo lo necesario para dibujarla y para explicar
    en la interfaz que se mostro, que se omitio y por que."""

    spec: LabelSpec
    size: tuple
    lines: list
    logo_box: tuple
    divider_box: tuple
    band_box: tuple
    bar_box: tuple
    modules: str
    module_dots: int
    quiet_modules: int
    shown: tuple
    omitted: tuple
    shortened: tuple
    scale: float

    @property
    def module_mm(self) -> float:
        return self.module_dots * 25.4 / self.spec.dpi

    @property
    def bar_height_mm(self) -> float:
        return (self.bar_box[3] - self.bar_box[1]) * 25.4 / self.spec.dpi

    @property
    def min_text_pt(self) -> float:
        return min(line.pt(self.spec.dpi) for line in self.lines)

    def lines_for(self, role: str) -> list:
        return [line for line in self.lines if line.role == role]

    @property
    def warnings(self) -> list:
        notes = []
        if self.module_mm < _MIN_MODULE_MM - 0.01:
            notes.append(
                "Las barras quedan muy finas para este ancho: usa una etiqueta más ancha "
                "o un código más corto para que el lector las lea sin esfuerzo."
            )
        return notes


# Estilos de texto de la hoja de prueba de impresion (escalan con el tamaño).

def _type_scale(spec: LabelSpec) -> float:
    """Factor de tamaño del texto respecto a la etiqueta de referencia 50 x 25.
    Crece mas despacio que la etiqueta (exponente < 1): el espacio extra se usa
    para mostrar mas datos y dar aire, no solo para agrandar las letras."""
    return _clamp(min((spec.height_mm / 25) ** 0.55, 1.4 * (spec.width_mm / 50) ** 0.8), 0.8, 2.2)


def _style_sizes(role: str, dpi: int, scale: float) -> tuple:
    style = _STYLES[role]
    floor = math.ceil(_pt_to_px(style.floor_pt, dpi))
    preferred = max(floor, round(_pt_to_px(style.pt * scale, dpi)))
    low = max(floor, math.floor(preferred * style.shrink))
    return preferred, low


def _make_line(role: str, text: str, size: int, **extra) -> LabelLine:
    style = _STYLES[role]
    return LabelLine(role, text, size, style.tracking * size, style.word_spacing * size, **extra)


@functools.lru_cache(maxsize=65536)
def _fit_size(role: str, text: str, max_width: float, preferred: int, low: int):
    style = _STYLES[role]
    for size in range(preferred, low - 1, -1):
        if _measure(text, size, style.tracking, style.word_spacing) <= max_width:
            return size
    return None


def _fit(role: str, text: str, max_width: float, preferred: int, low: int):
    """La linea mas grande (entre preferred y low) que cabe en max_width, o None."""
    size = _fit_size(role, text, max_width, preferred, low)
    return None if size is None else _make_line(role, text, size)


# ---------------------------------------------------------------------------
# Formato de la etiqueta (el clasico del laboratorio)
# ---------------------------------------------------------------------------
# Composicion fija, la de las etiquetas que el laboratorio ya usa y prefiere:
#
#   logo | LABORATORIO DE INGENIERIA / tipo de activo / aviso
#   ----------------------------------------------------------
#   [ nombre en banda negra ]
#   RUTA · UBIC · CAT
#   |||||||||||||||| codigo de barras ||||||||||||||||
#                     2-1-01-00-000
#
# La geometria esta en puntos de impresora sobre el lienzo de referencia de
# 384 x 192 (50 x 25 mm a 203 dpi) y escala con el tamaño real configurado.
# Solo el CONTENIDO se ajusta para caber, sin cambiar el formato: primero baja
# la letra hasta su minimo, despues se quita el espacio extra entre letras, los
# datos pasan a una segunda linea y, como ultimo recurso, el texto se acorta
# con «…». Si aun asi falta alto para las barras, se omiten los datos.
_CLASSIC_CANVAS = (384, 192)
_C_MARGIN_X = 8
_C_MARGIN_Y = 4
_C_GAP = 2
_C_HEADER_HEIGHT = 46
_C_HEADER_LINE_GAP = 1
_C_DIVIDER = 1
_C_LOGO_BOX = (78, 42)
_C_HEADER_TEXT_GAP = 8
_C_TITLE_PADDING = (5, 1)
# (tamaño preferido, tamaño minimo con el espaciado original, piso sin espaciado)
_C_SIZES = {
    "lab": (14, 9, 9), "type": (12, 9, 9), "notice": (10, 9, 9),
    "name": (18, 15, 12), "meta": (11, 9, 9), "code": (26, 9, 9),
}
# Espacio extra entre letras y entre palabras, en puntos de impresora.
_C_SPACING = {"lab": (2, 2), "type": (1, 1), "notice": (1, 1), "name": (1, 1), "meta": (0, 0), "code": (0, 0)}
# Piso absoluto de las letras: 9 puntos a 203 dpi (~3,2 pt, el texto mas
# pequeño del formato clasico), aunque la etiqueta configurada sea mas chica.
_C_MIN_FONT = 9
_C_MIN_BAR_MM = 9.0
_C_MAX_BAR_MM = 10.0
_C_META_SEPARATOR = "  ·  "
_C_MAX_META_LINES = 2


def _classic_width(text: str, size: int, tracking: float = 0, word_spacing: float = 0) -> float:
    return (
        sum(_advance(char, size) for char in text)
        + max(0, len(text) - 1) * tracking
        + text.count(" ") * word_spacing
    )


def _classic_fit(role: str, text: str, max_width: float, scale: float, clip: bool = True,
                 legible: int = 1):
    """(linea, recortada): la letra mas grande que cabe con el espaciado
    original; si no cabe ni a su minimo, sin espaciado extra hasta el piso; y
    solo entonces acortada con «…». Ningun tamaño baja de `legible`. Con
    clip=False devuelve (None, True) en lugar de acortar."""
    preferred, minimum, floor = (max(legible, round(value * scale)) for value in _C_SIZES[role])
    tracking, word_spacing = (round(value * scale) for value in _C_SPACING[role])
    for size in range(preferred, minimum - 1, -1):
        if _classic_width(text, size, tracking, word_spacing) <= max_width:
            return LabelLine(role, text, size, tracking, word_spacing), False
    for size in range(minimum, min(floor, minimum) - 1, -1):
        if _classic_width(text, size) <= max_width:
            return LabelLine(role, text, size), False
    if not clip:
        return None, True
    low = min(floor, minimum)
    clipped = _ellipsize(text, _load_font(low), max_width)
    return LabelLine(role, clipped or text[:1], low), True


def _classic_meta_lines(segments: list, max_width: float, scale: float, legible: int) -> list:
    """Opciones para los datos [(clave, texto), ...], de la mas completa a la
    mas compacta: todos en una linea o en dos (sin cortar palabras); luego sin
    los ultimos datos (categoria, despues ubicacion), otra vez en una o dos
    lineas; y, como ultimo recurso, todo en una linea acortada con «…». Cada
    opcion: (lineas, recortada, claves mostradas)."""
    if not segments:
        return [([], False, ())]
    keys = tuple(key for key, _ in segments)
    texts = [text for _, text in segments]
    options = []
    for count in range(len(texts), 0, -1):
        shown = texts[:count]
        line, _ = _classic_fit(
            "meta", _C_META_SEPARATOR.join(shown), max_width, scale, clip=False, legible=legible,
        )
        if line is not None:
            options.append(([line], False, keys[:count]))
            continue
        best = None
        for cut in range(1, count) if _C_MAX_META_LINES > 1 else ():
            parts = [_C_META_SEPARATOR.join(shown[:cut]), _C_META_SEPARATOR.join(shown[cut:])]
            fitted = [_classic_fit("meta", part, max_width, scale, clip=False, legible=legible)[0]
                      for part in parts]
            if all(fitted):
                size = min(item.size for item in fitted)
                if best is None or size > best[0]:
                    best = (size, parts)
        if best:
            lines = [_classic_meta_line(text, best[0], max_width, scale) for text in best[1]]
            options.append((lines, False, keys[:count]))
    joined = _C_META_SEPARATOR.join(texts)
    options.append(([_classic_fit("meta", joined, max_width, scale, legible=legible)[0]], True, keys))
    return options


def _classic_meta_line(text: str, size: int, max_width: float, scale: float) -> LabelLine:
    """Linea de datos a un tamaño comun (las dos lineas se ven iguales)."""
    tracking, word_spacing = (round(value * scale) for value in _C_SPACING["meta"])
    if _classic_width(text, size, tracking, word_spacing) > max_width:
        tracking = word_spacing = 0
    if _classic_width(text, size, tracking, word_spacing) > max_width:
        text = _ellipsize(text, _load_font(size), max_width)
    return LabelLine("meta", text, size, tracking, word_spacing)


def _classic_fields(code: str, spec: LabelSpec, description, notice, category, location, item_type) -> dict:
    notice = _clean_text(notice)
    item_type = _clean_text(item_type)
    location, category = _clean_text(location), _clean_text(category)
    # notice=None (o "") conserva la variante minima, sin encabezado.
    header = []
    if notice:
        if spec.shows("brand"):
            header.append(("lab", LABEL_INSTITUTION_TEXT))
        if spec.shows("type"):
            header.append(("type", _type_text(item_type, code)))
        if spec.shows("notice"):
            header.append(("notice", notice))
    meta = []
    route = _location_guide(code)
    if route and spec.shows("route"):
        meta.append(("route", route))
    if location and spec.shows("location"):
        meta.append(("location", f"UBIC: {location}"))
    if category and spec.shows("category"):
        meta.append(("category", f"CAT: {category}"))
    return {
        "code": code,
        "name": _clean_text(description),
        "logo": bool(notice) and spec.shows("brand"),
        "header": header,
        "meta": meta,
    }


def _compose_classic(fields: dict, spec: LabelSpec, canvas: tuple) -> LabelLayout:
    width, height = canvas
    scale = min(width / _CLASSIC_CANVAS[0], height / _CLASSIC_CANVAS[1])

    def px(value: float) -> int:
        return max(1, round(value * scale))

    margin_x, margin_y, gap = px(_C_MARGIN_X), px(_C_MARGIN_Y), px(_C_GAP)
    content_width = width - 2 * margin_x
    legible = max(1, round(_C_MIN_FONT * spec.dpi / LABEL_DPI))
    shortened = []

    # --- Codigo de barras y codigo legible (siempre completos) ---
    modules = _code128_modules(fields["code"])
    module_dots, quiet = _barcode_geometry(len(modules), width, spec.dpi)
    if not module_dots:
        raise ValueError(
            f"El código '{fields['code']}' es demasiado largo: su código de barras no cabe "
            f"en una etiqueta de {spec.size_text} ({width} puntos de ancho)."
        )
    code_line, code_clipped = _classic_fit(
        "code", fields["code"], content_width, scale, clip=False, legible=legible,
    )
    if code_clipped:
        raise ValueError(
            f"El código '{fields['code']}' no cabe legible en una etiqueta de {spec.size_text}."
        )
    # Barras de 9 a 10 mm reales en 50 x 25, en proporcion en otros tamaños.
    physical = min(spec.width_mm / LABEL_WIDTH_MM, spec.height_mm / LABEL_HEIGHT_MM)
    min_bar = max(1, round(_mm_to_dots(_C_MIN_BAR_MM * physical, spec.dpi)))
    max_bar = max(min_bar, round(_mm_to_dots(_C_MAX_BAR_MM * physical, spec.dpi)))

    # --- Encabezado: logo + laboratorio / tipo / aviso ---
    header_height = px(_C_HEADER_HEIGHT)
    logo_box_size = (px(_C_LOGO_BOX[0]), min(header_height, px(_C_LOGO_BOX[1])))
    text_x = margin_x + (logo_box_size[0] + px(_C_HEADER_TEXT_GAP) if fields["logo"] else 0)
    header_lines = []
    for role, text in fields["header"]:
        header_width = width - margin_x - text_x
        line, clipped = _classic_fit(role, text, header_width, scale, legible=legible)
        if clipped and role == "notice" and text == LABEL_NOTICE_TEXT:
            line, clipped = _classic_fit(role, LABEL_NOTICE_SHORT_TEXT, header_width, scale, legible=legible)
        if clipped:
            shortened.append(role)
        line.x = text_x
        header_lines.append(line)
    branded = bool(header_lines or fields["logo"])
    # Con el piso de legibilidad, en etiquetas muy chicas el texto puede pedir
    # mas alto que el encabezado escalado: el encabezado crece para contenerlo.
    header_gap = px(_C_HEADER_LINE_GAP)
    header_ink = sum(line.ink_height for line in header_lines) + header_gap * max(0, len(header_lines) - 1)
    header_height = max(header_height, header_ink)

    # --- Nombre en banda negra ---
    pad_x, pad_y = px(_C_TITLE_PADDING[0]), px(_C_TITLE_PADDING[1])
    name_line = None
    if fields["name"]:
        name_line, clipped = _classic_fit(
            "name", fields["name"], content_width - 2 * pad_x, scale, legible=legible,
        )
        name_line.inverse = True
        if clipped:
            shortened.append("name")

    # --- Datos: la opcion mas completa que deja alto para las barras ---
    meta_options = _classic_meta_lines(fields["meta"], content_width, scale, legible)

    def rows_height(with_header: bool, with_name: bool, meta_lines: list) -> int:
        used = margin_y
        if with_header:
            used += header_height + px(_C_DIVIDER) + gap
        if with_name:
            used += name_line.ink_height + 2 * pad_y + gap
        used += sum(line.ink_height + gap for line in meta_lines)
        return used

    def bar_room(*state) -> int:
        return height - margin_y - code_line.ink_height - gap - rows_height(*state)

    # Si falta alto para las barras se prueba lo mas compacto: primero los
    # datos, luego sin datos, luego sin nombre y, al final, sin encabezado.
    plans = []
    for with_header in ((True, False) if branded else (False,)):
        for meta_lines, meta_clipped, meta_keys in meta_options:
            plans.append((with_header, bool(name_line), meta_lines, meta_clipped, meta_keys))
        if fields["meta"]:
            plans.append((with_header, bool(name_line), [], False, ()))
        if name_line:
            plans.append((with_header, False, [], False, ()))
    for with_header, with_name, meta_lines, meta_clipped, meta_keys in plans:
        if bar_room(with_header, with_name, meta_lines) >= min_bar:
            break
    else:
        with_header, with_name, meta_lines, meta_clipped, meta_keys = False, False, [], False, ()
        if bar_room(False, False, []) < 1:
            raise ValueError(
                f"La etiqueta de {spec.size_text} es demasiado baja: el código de barras no cabe."
            )
    if meta_clipped:
        shortened.append("meta")
    if meta_lines:
        common = min(line.size for line in meta_lines)
        meta_lines = [_classic_meta_line(line.text, common, content_width, scale) for line in meta_lines]

    room = bar_room(with_header, with_name, meta_lines)
    bar_height = max(1, min(max_bar, room))
    # El bloque completo se centra en el alto (en 50 x 25 casi no sobra espacio).
    y = margin_y + max(0, room - bar_height) // 2

    logo_box = divider_box = band_box = None
    lines = []
    if with_header:
        if fields["logo"]:
            logo_y = y + (header_height - logo_box_size[1]) // 2
            logo_box = (margin_x, logo_y, margin_x + logo_box_size[0], logo_y + logo_box_size[1])
        text_y = y + max(0, (header_height - header_ink) // 2)
        for line in header_lines:
            line.top = text_y
            text_y += line.ink_height + header_gap
        lines.extend(header_lines)
        divider_box = (margin_x, y + header_height, width - margin_x, y + header_height + px(_C_DIVIDER))
        y += header_height + px(_C_DIVIDER) + gap
    if with_name:
        band_height = name_line.ink_height + 2 * pad_y
        band_box = (margin_x, y, width - margin_x, y + band_height)
        name_line.x, name_line.top = margin_x + pad_x, y + pad_y
        lines.append(name_line)
        y += band_height + gap
    for line in meta_lines:
        line.x, line.top = margin_x, y
        lines.append(line)
        y += line.ink_height + gap
    bar_x = (width - len(modules) * module_dots) // 2
    bar_box = (bar_x, y, bar_x + len(modules) * module_dots, y + bar_height)
    y += bar_height + gap
    code_line.x, code_line.top = (width - round(code_line.width)) // 2, y
    lines.append(code_line)

    shown_meta = set(meta_keys)
    shown = []
    if with_header:
        shown += (["brand"] if fields["logo"] else []) + [role for role, _ in fields["header"] if role != "lab"]
    shown += [key for key, _ in fields["meta"] if key in shown_meta]
    requested = (["brand"] if fields["logo"] else []) + [r for r, _ in fields["header"] if r != "lab"]
    requested += [key for key, _ in fields["meta"]]
    omitted = (["name"] if fields["name"] and not with_name else []) + [k for k in requested if k not in shown]
    return LabelLayout(
        spec=spec, size=canvas, lines=lines,
        logo_box=logo_box, divider_box=divider_box, band_box=band_box, bar_box=bar_box,
        modules=modules, module_dots=module_dots, quiet_modules=quiet,
        shown=tuple(shown), omitted=tuple(omitted), shortened=tuple(dict.fromkeys(shortened)), scale=scale,
    )


def layout_label(code: str, spec: LabelSpec = None, *, canvas_size: tuple = None,
                 description: str = None, notice: str = LABEL_NOTICE_TEXT,
                 category: str = None, location: str = None,
                 item_type: str = None) -> LabelLayout:
    """Compone la etiqueta en el formato clasico ajustando solo el contenido
    (ver arriba) y reporta lo acortado u omitido. Lanza ValueError si el
    codigo no es valido o no cabe en la etiqueta."""
    code = (code or "").strip()
    _validate_printable(code)
    spec = spec or LabelSpec()
    canvas = tuple(canvas_size) if canvas_size else spec.canvas_size
    fields = _classic_fields(code, spec, description, notice, category, location, item_type)
    return _compose_classic(fields, spec, canvas)


def describe_omitted(layout: LabelLayout) -> list:
    """Nombres legibles del contenido que no cupo (para la interfaz)."""
    names = {"name": "Nombre", **LABEL_CONTENT_OPTIONS}
    return [names[key] for key in layout.omitted]


# ---------------------------------------------------------------------------
# Dibujo
# ---------------------------------------------------------------------------

def _draw_line(draw, line: LabelLine) -> None:
    font = line.font
    top, _ = _ink_extent(line.text, line.size)
    first_left = font.getbbox(line.text[:1], mode="1")[0] if line.text else 0
    pen = float(line.x - first_left)
    fill = 255 if line.inverse else 0
    for char in line.text:
        draw.text((round(pen), line.top - top), char, font=font, fill=fill)
        pen += font.getlength(char, mode="1") + line.tracking
        if char == " ":
            pen += line.word_spacing


def _paste_logo(canvas, box: tuple) -> None:
    x0, y0, x1, y1 = box
    logo = _load_brand_logo()
    if logo is None:
        draw = ImageDraw.Draw(canvas)
        draw.fontmode = "1"
        line = _fit("lab", "UNIMINUTO", x1 - x0, max(6, (y1 - y0) // 2), 6) or _make_line("lab", "UNIMINUTO", 6)
        line.x, line.top = x0, y0 + ((y1 - y0) - line.ink_height) // 2
        _draw_line(draw, line)
        return
    logo = logo.copy()
    logo.thumbnail((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    logo = logo.point(lambda value: 0 if value < 192 else 255)
    canvas.paste(logo, (x0 + ((x1 - x0) - logo.width) // 2, y0 + ((y1 - y0) - logo.height) // 2))


def render_label(layout: LabelLayout) -> Image.Image:
    width, height = layout.size
    canvas = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(canvas)
    # Letras suavizadas y luego umbralizadas a 1 bit, como el formato clasico:
    # trazos con el mismo grosor de las etiquetas que el laboratorio ya usa.
    if layout.logo_box:
        _paste_logo(canvas, layout.logo_box)
    if layout.divider_box:
        x0, y0, x1, y1 = layout.divider_box
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=0)
    if layout.band_box:
        x0, y0, x1, y1 = layout.band_box
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=0)
    for line in layout.lines:
        _draw_line(draw, line)
    x0, y0, _, y1 = layout.bar_box
    for run in re.finditer("1+", layout.modules):
        draw.rectangle(
            (x0 + run.start() * layout.module_dots, y0,
             x0 + run.end() * layout.module_dots - 1, y1 - 1),
            fill=0,
        )
    return canvas.convert("1", dither=Image.Dither.NONE)


# ---------------------------------------------------------------------------
# Salidas: imagen, PNG, PDF y hoja de prueba
# ---------------------------------------------------------------------------

def _resolve_spec(spec: LabelSpec, canvas_size) -> LabelSpec:
    """Compatibilidad: un canvas_size explicito (en puntos a 203 dpi) define el
    tamaño fisico si no se paso una especificacion."""
    if spec is not None or not canvas_size:
        return spec or LabelSpec()
    width, height = canvas_size
    return LabelSpec(
        width_mm=_clamp(width * 25.4 / LABEL_DPI, MIN_LABEL_WIDTH_MM, MAX_LABEL_WIDTH_MM),
        height_mm=_clamp(height * 25.4 / LABEL_DPI, MIN_LABEL_HEIGHT_MM, MAX_LABEL_HEIGHT_MM),
    )


def generate_label_image(code: str, canvas_size: tuple = None, *,
                         description: str = None, notice: str = LABEL_NOTICE_TEXT,
                         category: str = None, location: str = None,
                         item_type: str = None, spec: LabelSpec = None) -> Image.Image:
    """Etiqueta profesional a tamaño real: marca, producto, Code 128 y texto exacto.

    `description` conserva su nombre historico por compatibilidad y representa
    el nombre corto. `notice=None` mantiene la variante minima sin encabezado.
    """
    spec = _resolve_spec(spec, canvas_size)
    layout = layout_label(
        code, spec, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type,
    )
    return render_label(layout)


def generate_label_png_bytes(code: str, canvas_size: tuple = None, *,
                             description: str = None, notice: str = LABEL_NOTICE_TEXT,
                             category: str = None, location: str = None,
                             item_type: str = None, spec: LabelSpec = None) -> bytes:
    """PNG que declara la resolucion de la impresora (respaldo; el PDF es el
    formato recomendado porque los navegadores ignoran los dpi de un PNG)."""
    spec = _resolve_spec(spec, canvas_size)
    img = generate_label_image(
        code, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type, spec=spec,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(spec.dpi, spec.dpi))
    return buf.getvalue()


def _pdf_text(value: str) -> str:
    """Cadena PDF en UTF-16BE (admite tildes y cualquier caracter)."""
    return "<" + ("﻿" + value).encode("utf-16-be").hex().upper() + ">"


def _assemble_pdf(objects: list, info_number: int = None) -> bytes:
    """Construye un PDF minimo y valido sin dependencias adicionales."""
    document = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(payload)
        document.extend(b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    info = f" /Info {info_number} 0 R" if info_number else ""
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info} >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(document)


def _label_image_to_pdf(image: Image.Image, spec: LabelSpec = None, title: str = "Etiqueta") -> bytes:
    """Pagina del tamaño exacto de la etiqueta; cada pixel ocupa un punto de
    la impresora. El catalogo pide al visor imprimir sin escalar (PrintScaling
    /None) y elegir el papel por el tamaño del PDF (PickTrayByPDFSize).

    El raster se ancla a la esquina SUPERIOR IZQUIERDA (no se centra) para que,
    al rasterizar a la resolucion de la impresora, cada pixel caiga sobre un
    punto entero del cabezal; el sobrante (< 1 punto) queda a la derecha y
    abajo. Ademas los bordes quedan apenas hacia adentro: los visores redondean
    los bordes de una imagen hacia afuera (PDFium: Chrome y Edge) o suman un
    pixel al borde final (poppler), asi que un borde en 399,0001 o en -0,00001
    les hacia pintar 400 columnas y remuestrear, con barras de ancho desigual.
    El margen final es minimo porque Chrome, con la escala Predeterminado,
    recorta el papel a puntos PDF enteros y trunca a pixeles: la etiqueta se
    reduce hasta un 0,6 % y el borde no debe cruzar al pixel anterior. Asi el
    raster se copia 1:1 (verificado con los motores de Chrome/Edge y de poppler
    a 203 y 300 dpi, en tests/test_label_print.py)."""
    spec = spec or LabelSpec()
    page_width, page_height, image_object, content_object = _label_page_parts(image, spec)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/ViewerPreferences << /PrintScaling /None /PickTrayByPDFSize true >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
            f"/Resources << /XObject << /Label 4 0 R >> >> /Contents 5 0 R >>"
        ).encode("ascii"),
        image_object,
        content_object,
        (
            f"<< /Title {_pdf_text(f'{title} · {spec.describe()}')} "
            f"/Creator {_pdf_text('Inventario de Laboratorio UNIMINUTO')} >>"
        ).encode("ascii"),
    ]
    return _assemble_pdf(objects, info_number=6)


def _label_page_parts(image: Image.Image, spec: LabelSpec) -> tuple:
    """(ancho, alto, objeto imagen, objeto contenido) de la pagina de una
    etiqueta: el raster 1 bit anclado arriba a la izquierda con las holguras
    explicadas en `_label_image_to_pdf`."""
    image = image.convert("1", dither=Image.Dither.NONE)
    dot = 72 / spec.dpi  # un punto de la impresora, en puntos PDF
    page_width = round(spec.width_mm * 72 / 25.4, 4)
    page_height = round(spec.height_mm * 72 / 25.4, 4)
    start, end = _PDF_INSET_START_DOTS, _PDF_INSET_END_DOTS
    image_width = (image.width - start - end) * dot
    image_height = (image.height - start - end) * dot
    offset_x = start * dot
    offset_y = page_height - (image.height - end) * dot
    pixels = zlib.compress(image.tobytes())
    commands = (
        f"q\n{image_width:.6f} 0 0 {image_height:.6f} "
        f"{offset_x:.6f} {offset_y:.6f} cm\n/Label Do\nQ\n"
    ).encode("ascii")
    image_object = (
        f"<< /Type /XObject /Subtype /Image /Width {image.width} "
        f"/Height {image.height} /ColorSpace /DeviceGray /BitsPerComponent 1 "
        f"/Filter /FlateDecode /Length {len(pixels)} >>\nstream\n"
    ).encode("ascii") + pixels + b"\nendstream"
    content_object = (
        f"<< /Length {len(commands)} >>\nstream\n".encode("ascii")
        + commands + b"endstream"
    )
    return page_width, page_height, image_object, content_object


def generate_label_pdf_bytes(code: str, canvas_size: tuple = None, *,
                             description: str = None, notice: str = LABEL_NOTICE_TEXT,
                             category: str = None, location: str = None,
                             item_type: str = None, spec: LabelSpec = None) -> bytes:
    """PDF de una pagina del tamaño exacto de la etiqueta (formato recomendado)."""
    spec = _resolve_spec(spec, canvas_size)
    image = generate_label_image(
        code, canvas_size=canvas_size, description=description, notice=notice,
        category=category, location=location, item_type=item_type, spec=spec,
    )
    return _label_image_to_pdf(image, spec, title=f"Etiqueta {(code or '').strip()}")


# Rejillas de nitidez de la hoja de prueba: barras alternadas con el ancho de
# modulo del Code 128 (0,25-0,5 mm). Con la impresion punto por punto todas las
# barras de una rejilla salen iguales; si el visor o el driver remuestrea la
# imagen, unas salen mas gruesas que otras o grises.
_GRATING_MODULES_MM = (0.25, 0.35, 0.5)
_GRATING_BARS = 8
_GRATING_CAPTION = "Barras parejas = impresión exacta"


def generate_calibration_image(spec: LabelSpec = None) -> Image.Image:
    """Hoja de prueba del tamaño configurado: marco a 1 mm del borde, una regla
    milimetrada y tres rejillas de barras. Impresa y revisada revela si el
    driver reduce la pagina (regla mas corta), si el papel no coincide (marco
    cortado) o si la imagen se remuestrea (rejillas desiguales o grises)."""
    spec = spec or LabelSpec()
    width, height = spec.canvas_size
    dpi = spec.dpi
    scale = _type_scale(spec)

    def dots(mm: float) -> int:
        return round(_mm_to_dots(mm, dpi))

    canvas = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(canvas)
    draw.fontmode = "1"
    stroke = max(2, dots(0.25))
    inset = dots(1.0)
    draw.rectangle((inset, inset, width - 1 - inset, height - 1 - inset), outline=0, width=stroke)
    frame_w_mm = (width - 2 * inset) * 25.4 / dpi
    frame_h_mm = (height - 2 * inset) * 25.4 / dpi

    ruler_mm = max(10, int((spec.width_mm - 8) // 10) * 10)
    inner_width = width - 2 * (inset + stroke + dots(1.5))
    lines = []
    for role, text in (
        ("lab", "PRUEBA DE IMPRESIÓN"),
        ("meta", spec.describe()),
        ("meta", f"Regla {ruler_mm} mm · marco {_format_mm(round(frame_w_mm))} × "
                 f"{_format_mm(round(frame_h_mm))} mm"),
    ):
        preferred, low = _style_sizes(role, dpi, scale)
        line = _fit(role, text, inner_width, preferred, low)
        if line is not None:
            lines.append(line)
    ruler_height = dots(2.0) + stroke
    number_size = _style_sizes("meta", dpi, 1.0)[1]
    number_height = _ink_extent("0123456789", number_size)[1] - _ink_extent("0123456789", number_size)[0]
    ruler_block = ruler_height + dots(0.6) + number_height
    gap = dots(1.0)

    grating_modules = [max(1, dots(mm)) for mm in _GRATING_MODULES_MM]
    grating_widths = [(2 * _GRATING_BARS - 1) * module for module in grating_modules]
    grating_gap = dots(4.0)
    grating_width = sum(grating_widths) + grating_gap * (len(grating_widths) - 1)
    grating_height = dots(3.0)
    show_gratings = grating_width <= inner_width
    caption = None
    if show_gratings:
        preferred, low = _style_sizes("meta", dpi, scale)
        caption = _fit("meta", _GRATING_CAPTION, inner_width, preferred, low)

    available = height - 2 * (inset + stroke + dots(1.0))

    def total_height() -> int:
        total = ruler_block + sum(l.ink_height for l in lines) + gap * len(lines)
        if show_gratings:
            total += grating_height + gap
        if caption is not None:
            total += caption.ink_height + gap
        return total

    # Si no cabe todo, se sacrifica primero lo accesorio (leyenda, rejillas y
    # luego los textos), nunca la regla.
    while total_height() > available:
        if caption is not None:
            caption = None
        elif show_gratings:
            show_gratings = False
        elif lines:
            lines.pop()
        else:
            break
    y = (height - total_height()) // 2
    for line in lines[:2]:
        line.x = (width - round(line.width)) // 2
        line.top = y
        _draw_line(draw, line)
        y += line.ink_height + gap

    x_start = (width - dots(ruler_mm)) // 2
    baseline = y + ruler_height
    draw.rectangle((x_start, baseline - stroke, x_start + dots(ruler_mm), baseline - 1), fill=0)
    for mm in range(ruler_mm + 1):
        x = x_start + dots(mm)
        tick = dots(2.0) if mm % 10 == 0 else dots(1.3) if mm % 5 == 0 else dots(0.8)
        draw.rectangle((x, baseline - stroke - tick, x + max(1, stroke // 2), baseline - 1), fill=0)
        if mm % 10 == 0 or (ruler_mm < 20 and mm % 5 == 0):
            label = _make_line("meta", str(mm), number_size)
            label.x = x - round(label.width) // 2 + 1
            label.top = baseline + dots(0.6)
            _draw_line(draw, label)
    y = baseline + dots(0.6) + number_height + gap
    for line in lines[2:]:
        line.x = (width - round(line.width)) // 2
        line.top = y
        _draw_line(draw, line)
        y += line.ink_height + gap
    if show_gratings:
        x = (width - grating_width) // 2
        for module, grating in zip(grating_modules, grating_widths):
            for bar in range(_GRATING_BARS):
                left = x + 2 * bar * module
                draw.rectangle((left, y, left + module - 1, y + grating_height - 1), fill=0)
            x += grating + grating_gap
        y += grating_height + gap
    if caption is not None:
        caption.x = (width - round(caption.width)) // 2
        caption.top = y
        _draw_line(draw, caption)
    return canvas.convert("1", dither=Image.Dither.NONE)


def generate_calibration_pdf_bytes(spec: LabelSpec = None) -> bytes:
    spec = spec or LabelSpec()
    return _label_image_to_pdf(generate_calibration_image(spec), spec, title="Prueba de impresión")


# ---------------------------------------------------------------------------
# Etiquetas de items del inventario
# ---------------------------------------------------------------------------

def _item_label_fields(item: dict) -> dict:
    item = item or {}
    fields = {
        "code": str(item.get("id") or "").strip(),
        "description": _clean_text(item.get("name")),
        "category": _clean_text(item.get("category")),
        "location": _clean_text(item.get("location")),
        "item_type": _clean_text(item.get("item_type")),
    }
    if fields["item_type"] == "location":
        fields["notice"] = LOCATION_NOTICE_TEXT  # una ubicacion no se presta
    return fields


def generate_item_label_png_bytes(item: dict, spec: LabelSpec = None):
    """Etiqueta PNG de inventario; None para ids no imprimibles."""
    fields = _item_label_fields(item)
    if not _is_printable(fields["code"]):
        return None
    try:
        return generate_label_png_bytes(**fields, spec=spec)
    except ValueError:
        return None


def generate_item_label_pdf_bytes(item: dict, spec: LabelSpec = None):
    """Etiqueta PDF de inventario a tamaño real; None para ids no imprimibles."""
    fields = _item_label_fields(item)
    if not _is_printable(fields["code"]):
        return None
    try:
        return generate_label_pdf_bytes(**fields, spec=spec)
    except ValueError:
        return None


def layout_item_label(item: dict, spec: LabelSpec = None) -> LabelLayout:
    """Composicion de la etiqueta de un item (vista previa y diagnostico en la
    interfaz). Lanza ValueError si el codigo no es imprimible en ese tamaño."""
    fields = _item_label_fields(item)
    extra = {"notice": fields["notice"]} if "notice" in fields else {}
    return layout_label(
        fields["code"], spec or LabelSpec(), description=fields["description"],
        category=fields["category"], location=fields["location"], item_type=fields["item_type"], **extra,
    )


@functools.lru_cache(maxsize=256)  # ~5-80 KB por etiqueta: acota la memoria
def _cached_item_label_files(spec: LabelSpec, code: str, description: str, category: str,
                             location: str, item_type: str, notice: str = LABEL_NOTICE_TEXT) -> tuple:
    fields = dict(code=code, description=description, category=category,
                  location=location, item_type=item_type, notice=notice)
    image = generate_label_image(**fields, spec=spec)
    png = io.BytesIO()
    image.save(png, format="PNG", dpi=(spec.dpi, spec.dpi))
    return _label_image_to_pdf(image, spec, title=f"Etiqueta {code}"), png.getvalue()


def item_label_files(item: dict, spec: LabelSpec = None) -> tuple:
    """(pdf, png) de la etiqueta de un item, cacheados por contenido y tamaño:
    el catalogo no regenera todas las etiquetas en cada recarga. (None, None)
    si el codigo no es imprimible o no cabe en la etiqueta configurada."""
    fields = _item_label_fields(item)
    if not _is_printable(fields["code"]):
        return None, None
    try:
        return _cached_item_label_files(spec or LabelSpec(), **fields)
    except ValueError:
        return None, None


# ---------------------------------------------------------------------------
# Varias etiquetas en un solo PDF (p. ej. una estanteria y sus pisos)
# ---------------------------------------------------------------------------

def _labels_to_pdf(images: list, spec: LabelSpec, title: str) -> bytes:
    """PDF con una pagina por etiqueta, cada una igual a la pagina unica de
    `_label_image_to_pdf` (mismo tamaño, raster anclado arriba a la izquierda y
    las mismas holguras): se imprime punto por punto, en orden."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/ViewerPreferences << /PrintScaling /None /PickTrayByPDFSize true >> >>",
        None,  # arbol de paginas: se completa al final
    ]
    kids = []
    for image in images:
        page_width, page_height, image_object, content_object = _label_page_parts(image, spec)
        page = len(objects) + 1
        objects.append((
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.4f} {page_height:.4f}] "
            f"/Resources << /XObject << /Label {page + 1} 0 R >> >> /Contents {page + 2} 0 R >>"
        ).encode("ascii"))
        objects.extend([image_object, content_object])
        kids.append(f"{page} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode("ascii")
    objects.append((
        f"<< /Title {_pdf_text(f'{title} · {spec.describe()}')} "
        f"/Creator {_pdf_text('Inventario de Laboratorio UNIMINUTO')} >>"
    ).encode("ascii"))
    return _assemble_pdf(objects, info_number=len(objects))


def generate_labels_pdf_bytes(items: list, spec: LabelSpec = None, title: str = "Etiquetas"):
    """Un solo PDF con una pagina (del tamaño real de la etiqueta) por cada item
    imprimible de `items`, en el mismo orden: imprime de una vez las etiquetas de
    una estanteria y sus pisos. None si ninguno es imprimible."""
    spec = spec or LabelSpec()
    images = []
    for item in items or []:
        fields = _item_label_fields(item)
        if not _is_printable(fields["code"]):
            continue
        try:
            images.append(generate_label_image(**fields, spec=spec))
        except ValueError:
            continue
    if not images:
        return None
    return _labels_to_pdf(images, spec, title)


def printable_items(items: list) -> list:
    """Los items de ``items`` cuyo código se puede imprimir como etiqueta (mismo criterio del PDF)."""
    return [item for item in items or [] if _is_printable(_item_label_fields(item)["code"])]


def container_label_set(container: dict, children: list) -> list:
    """Etiquetas de un contenedor en el orden en que se pegan: la del contenedor y luego la de cada uno de sus
    productos, por código. Solo las imprimibles."""
    ordered = sorted(printable_items(children), key=lambda item: _natural_key(item.get("id")))
    return printable_items([container]) + ordered


def generate_container_labels_pdf_bytes(container: dict, children: list, spec: LabelSpec = None):
    """PDF con la etiqueta del contenedor y la de todos sus productos, una página cada una y en seguidilla, como el
    PDF de las ubicaciones de una estantería. ``None`` si no hay ninguna imprimible."""
    return generate_labels_pdf_bytes(
        container_label_set(container, children), spec,
        title=f"Etiquetas de {_clean_text((container or {}).get('name')) or 'contenedor'}")


def generate_catalog_labels_pdf_bytes(groups: list, spec: LabelSpec = None, title: str = "Etiquetas del inventario"):
    """PDF con las etiquetas de varios contenedores: ``groups`` es ``[(contenedor, [productos])]``; cada contenedor
    va seguido de sus productos."""
    items = [item for container, children in groups or [] for item in container_label_set(container, children)]
    return generate_labels_pdf_bytes(items, spec, title=title)


def _natural_key(code) -> tuple:
    """Orden natural de códigos (LAB-2 antes que LAB-10)."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part.lower())
                 for part in re.split(r"(\d+)", str(code or "")) if part)
