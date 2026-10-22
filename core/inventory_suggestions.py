# -*- coding: utf-8 -*-
"""
core/inventory_suggestions.py - Propone productos a partir de la descripcion
libre de un Contenedor Principal.

Ejemplo real: un contenedor con la descripcion

    "Contiene Piezas Lego de Pines 4×2 - 2×2 - 2×1
     Además, Contiene un Caja con Separadores de Fichas y Pieza lego Lisas de 2×2"

produce cinco Contenedores de Caracteristica ("child") listos para revisar:
"Pieza Lego con pines 4x2", "... 2x2", "... 2x1", "Caja con separadores de
fichas" y "Pieza Lego lisa 2x2", con codigo de nivel caja (2-1-01-NN-000),
la categoria del contenedor, una ubicacion coherente y cantidad 0 pendiente de
conteo (la descripcion no trae cantidades, salvo que el texto las diga).

Todo el modulo son funciones PURAS y deterministas (sin Streamlit, sin storage,
sin red ni IA): la vista (views/inventario.py) muestra las propuestas para que
el usuario las edite y confirme, y core/storage.py vuelve a validar al guardar.
Nada se inventa: lo que el texto no deja claro (una medida "1x0", un termino
generico como "fichas", una pieza unida con "y" a una caja) se devuelve como
NOTA para que el usuario lo confirme, y cada propuesta lleva una CONFIANZA
("alta", "media" o "baja"): las de confianza baja no se marcan para crear.

Como se interpreta el texto (pensado para cualquier contenedor, no solo Lego):

1. Normalizacion: ×, X y * entre numeros son "x"; "2 por 2" y "2 by 2" son 2x2;
   la coma decimal (4,7) es punto; se quitan viñetas y numeracion de listas,
   "etc." y los puntos de las abreviaturas (uds., pzs.); "(15)" es la cantidad;
   las aclaraciones entre parentesis se anotan; y una lista con unidad
   compartida ("220, 330 y 470 ohm") se reparte ("220 ohm, 330 ohm y 470 ohm").
2. El texto se divide en frases por saltos de linea, punto y coma y puntos (no
   los decimales), y cada frase en partes por "-", ",", ":", "/", "+", "y", "o".
   Una parte unida con "y" o "," a la anterior solo se separa si empieza un
   producto nuevo (trae una medida, una cantidad, una caja o un sustantivo
   conocido): asi "Caja con puertas y ventanas" queda como un solo producto
   y "... Fichas y Pieza lego Lisas de 2x2" como dos.
3. Se descartan muletillas iniciales ("Además", "Contiene", "Hay", "un",
   "Contendor 2 contiene", ...), tolerando errores de digitacion.
4. Cada parte es una CAJA ("Caja con ..."; si dice "que contiene ..." lo que
   sigue son productos dentro de la caja), una PIEZA con una o varias medidas
   ("Pines 4x2", "Bases de 1 pin", "220 ohm", "10uF", "20 cm") o un PRODUCTO sin
   medida. Una medida suelta ("2x2") hereda el sujeto de la parte anterior.
5. Las cantidades ("20 piezas de 2x2", "x10", "50 uds", "(15)") llenan Cantidad.
6. El nombre se normaliza: sustantivo en singular + marca + caracteristica +
   medida ("Pieza Lego con pines 4x2", "Base Lego de 1 pin",
   "Resistencia de 220 Ω", "Condensador de 10 µF"); los adjetivos (colores,
   "ultrasónico") concuerdan en genero con el sustantivo.
7. Se validan las medidas: una dimension 0 o mayor que 48 no existe en una
   pieza; se avisa y la propuesta queda sin marcar. Lo repetido se propone una
   sola vez y lo que no se pudo leer se informa.
"""

import difflib
import math
import re
import unicodedata
from functools import lru_cache

from core import barcode

DEFAULT_UNIT = "unidad"
PENDING_COUNT_NOTE = "Cantidad pendiente de conteo."
HISTORY_DETAILS = "Creado desde la descripción del contenedor {parent_id} (generador de productos)."

KIND_PIECE = "pieza"
KIND_BOX = "caja"
KIND_PRODUCT = "producto"

CONFIDENCE_HIGH = "alta"
CONFIDENCE_MEDIUM = "media"
CONFIDENCE_LOW = "baja"

MAX_DIMENSION = 48          # una pieza (Lego) no mide mas de 48 en un lado: 0 o mas de 48 se revisa
MAX_TEXT_LENGTH = 20000     # una descripcion mas larga se corta (y se avisa)
MAX_ENTRIES = 120           # tope de productos propuestos por contenedor
MAX_PART_LENGTH = 300       # una parte mas larga no se sigue uniendo (evita lo cuadratico en textos absurdos)
MAX_UNPARSED_SHOWN = 12     # fragmentos ilegibles que se listan en los avisos
MAX_QUANTITY = 1_000_000    # una cantidad mayor en el texto casi seguro es un error de digitacion

OHM = "\u03a9"
MICRO = "\u00b5"

# Columnas de la vista previa editable (views/inventario.py).
COL_CREATE = "Crear"
COL_CODE = "Código"
COL_NAME = "Nombre"
COL_FEATURE = "Característica"
COL_QUANTITY = "Cantidad"
COL_UNIT = "Unidad"
COL_NOTES = "Notas"
EDITOR_COLUMNS = (COL_CREATE, COL_CODE, COL_NAME, COL_FEATURE, COL_QUANTITY, COL_UNIT, COL_NOTES)


# ---------------------------------------------------------------------------
# Vocabulario (claves sin tildes y en minuscula: ver fold())
# ---------------------------------------------------------------------------

# Sustantivo singular -> (como se muestra, genero gramatical).
_NOUNS = {
    "pieza": ("Pieza", "f"), "base": ("Base", "f"), "bloque": ("Bloque", "m"),
    "ladrillo": ("Ladrillo", "m"), "placa": ("Placa", "f"), "plancha": ("Plancha", "f"),
    "lamina": ("Lámina", "f"), "viga": ("Viga", "f"), "rueda": ("Rueda", "f"),
    "llanta": ("Llanta", "f"), "eje": ("Eje", "m"), "engranaje": ("Engranaje", "m"),
    "conector": ("Conector", "m"), "teja": ("Teja", "f"), "baldosa": ("Baldosa", "f"),
    "puerta": ("Puerta", "f"), "ventana": ("Ventana", "f"), "figura": ("Figura", "f"),
    "minifigura": ("Minifigura", "f"), "ficha": ("Ficha", "f"), "polea": ("Polea", "f"),
    "bisagra": ("Bisagra", "f"), "tornillo": ("Tornillo", "m"), "tuerca": ("Tuerca", "f"),
    "arandela": ("Arandela", "f"), "cable": ("Cable", "m"), "sensor": ("Sensor", "m"),
    "motor": ("Motor", "m"), "boton": ("Botón", "m"), "resistencia": ("Resistencia", "f"),
    "condensador": ("Condensador", "m"), "led": ("LED", "m"),
    # Electronica y laboratorio.
    "kit": ("Kit", "m"), "diodo": ("Diodo", "m"), "transistor": ("Transistor", "m"),
    "protoboard": ("Protoboard", "f"), "servo": ("Servo", "m"), "servomotor": ("Servomotor", "m"),
    "rele": ("Relé", "m"), "pulsador": ("Pulsador", "m"), "interruptor": ("Interruptor", "m"),
    "potenciometro": ("Potenciómetro", "m"), "buzzer": ("Buzzer", "m"), "display": ("Display", "m"),
    "pantalla": ("Pantalla", "f"), "bateria": ("Batería", "f"), "pila": ("Pila", "f"),
    "fuente": ("Fuente", "f"), "modulo": ("Módulo", "m"), "shield": ("Shield", "m"),
    "zocalo": ("Zócalo", "m"), "bobina": ("Bobina", "f"), "inductor": ("Inductor", "m"),
    "fusible": ("Fusible", "m"), "regulador": ("Regulador", "m"), "cargador": ("Cargador", "m"),
    "adaptador": ("Adaptador", "m"), "switch": ("Switch", "m"), "header": ("Header", "m"),
    "jack": ("Jack", "m"), "jumper": ("Jumper", "m"), "soporte": ("Soporte", "m"), "tapa": ("Tapa", "f"),
    "perno": ("Perno", "m"), "remache": ("Remache", "m"), "clavo": ("Clavo", "m"),
    "resorte": ("Resorte", "m"), "iman": ("Imán", "m"), "cinta": ("Cinta", "f"),
    "varilla": ("Varilla", "f"), "tubo": ("Tubo", "m"), "tarjeta": ("Tarjeta", "f"),
    "microcontrolador": ("Microcontrolador", "m"), "multimetro": ("Multímetro", "m"),
    "osciloscopio": ("Osciloscopio", "m"), "soldador": ("Soldador", "m"), "pinza": ("Pinza", "f"),
    "alicate": ("Alicate", "m"), "destornillador": ("Destornillador", "m"), "martillo": ("Martillo", "m"),
    "llave": ("Llave", "f"), "separador": ("Separador", "m"), "espaciador": ("Espaciador", "m"),
    "lupa": ("Lupa", "f"), "pegante": ("Pegante", "m"), "lampara": ("Lámpara", "f"),
}
# Sinonimos que se nombran con un mismo sustantivo (asi se detectan repetidos).
_NOUN_ALIASES = {"capacitor": "condensador", "resistor": "resistencia", "breadboard": "protoboard",
                 "pulsadores": "pulsador", "battery": "bateria"}

# Piezas "de juego de construccion": reciben la marca del contenedor (Lego) si la medida es 4x2 o "2 pines".
_PIECE_NOUNS = {
    "pieza", "base", "bloque", "ladrillo", "placa", "plancha", "lamina", "viga", "rueda", "llanta", "eje",
    "engranaje", "conector", "teja", "baldosa", "puerta", "ventana", "figura", "minifigura", "polea", "bisagra",
}

# Contenedores fisicos que pueden venir DENTRO de un contenedor.
_BOX_NOUNS = {
    "caja", "cajita", "bolsa", "estuche", "bandeja", "organizador", "recipiente",
    "tarro", "frasco", "compartimento", "compartimiento", "gaveta", "cajon",
}

# Caracteristica (singular) -> (clave canonica, texto femenino, texto masculino).
_CHARACTERISTICS = {
    "pin": ("pin", "con pines", "con pines"),
    "teton": ("pin", "con pines", "con pines"),
    "stud": ("pin", "con pines", "con pines"),
    "lisa": ("lisa", "lisa", "liso"),
    "liso": ("lisa", "lisa", "liso"),
    "bisel": ("bisel", "con bisel", "con bisel"),
    "biselada": ("bisel", "con bisel", "con bisel"),
    "biselado": ("bisel", "con bisel", "con bisel"),
    "inclinada": ("bisel", "con bisel", "con bisel"),
    "inclinado": ("bisel", "con bisel", "con bisel"),
    "plana": ("plana", "plana", "plano"),
    "plano": ("plana", "plana", "plano"),
    "curva": ("curva", "curva", "curvo"),
    "curvo": ("curva", "curva", "curvo"),
    "redonda": ("redonda", "redonda", "redondo"),
    "redondo": ("redonda", "redonda", "redondo"),
}

_BRANDS = {"lego": "Lego", "duplo": "Duplo", "technic": "Technic", "mindstorms": "Mindstorms"}
_BRAND_ALIASES = {"legos": "lego", "leggo": "lego", "legoo": "lego", "duplos": "duplo"}
# Marcas que, nombradas en cualquier parte del contenedor, aplican a todas sus piezas
# (una linea como "Technic" solo se usa si la pieza la menciona).
_CONTEXT_BRANDS = {"lego", "duplo"}

# Nombres propios y siglas que se escriben siempre igual.
_PROPER = {
    "arduino": "Arduino", "raspberry": "Raspberry", "pi": "Pi", "uno": "UNO", "esp32": "ESP32",
    "esp8266": "ESP8266", "nodemcu": "NodeMCU", "usb": "USB", "led": "LED", "leds": "LEDs",
    "lcd": "LCD", "oled": "OLED", "pwm": "PWM", "gps": "GPS", "rfid": "RFID", "rgb": "RGB",
    "hdmi": "HDMI", "wifi": "WiFi", "bluetooth": "Bluetooth", "ev3": "EV3", "nxt": "NXT",
    "pcb": "PCB", "dc": "DC", "ac": "AC", "ttl": "TTL", "sd": "SD", "i2c": "I2C", "spi": "SPI",
    "uart": "UART", "ir": "IR", "pic": "PIC", "avr": "AVR", "arm": "ARM", "tft": "TFT", "pla": "PLA",
    "abs": "ABS", "ptfe": "PTFE", "microbit": "micro:bit", "lipo": "LiPo", "xbee": "XBee",
}

# Adjetivos con genero: masculino sin tilde -> masculino como se escribe.
_ADJECTIVES = {
    "rojo": "rojo", "amarillo": "amarillo", "blanco": "blanco", "negro": "negro", "morado": "morado",
    "rosado": "rosado", "dorado": "dorado", "plateado": "plateado", "anaranjado": "anaranjado",
    "pequeno": "pequeño", "mediano": "mediano", "largo": "largo", "corto": "corto", "ancho": "ancho",
    "delgado": "delgado", "grueso": "grueso", "ultrasonico": "ultrasónico", "analogico": "analógico",
    "metalico": "metálico", "plastico": "plástico", "magnetico": "magnético", "optico": "óptico",
    "infrarrojo": "infrarrojo", "inalambrico": "inalámbrico", "electrico": "eléctrico",
    "electronico": "electrónico", "mecanico": "mecánico", "electrolitico": "electrolítico",
    "ceramico": "cerámico", "fijo": "fijo", "nuevo": "nuevo", "usado": "usado", "daniado": "dañado",
    "danado": "dañado", "roto": "roto", "transparente": "transparente",
}
# Adjetivos sin genero (no cambian al concordar).
_ADJECTIVES_FIXED = {
    "azul", "verde", "gris", "naranja", "beige", "rosa", "celeste", "lila", "violeta", "cafe", "marron",
    "transparente", "grande", "digital", "flexible", "resistente", "universal", "estandar", "mini",
    "micro", "macho", "hembra", "doble", "simple", "cuadrado", "redondo",
}

_PREPOSITIONS = {"de", "del", "con", "para", "sin", "en", "por", "a"}
_CONJUNCTIONS = {"y", "e", "o", "u"}
_STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "un", "una", "unos", "unas", "con", "y", "e",
    "o", "u", "para", "en", "tipo", "al", "a", "su", "sus", "que", "se", "por", "sin",
}

# Terminos que no dicen QUE es el producto: se piden confirmar.
_PRODUCT_HEADS = {"arduino", "raspberry", "esp32", "esp8266", "nodemcu"}   # nombres propios que abren un producto
_RELATIVE_VERBS = {"tiene", "tienen", "trae", "traen", "lleva", "llevan", "incluye", "incluyen", "contiene",
                   "contienen", "posee", "poseen"}
_VAGUE_HEADS = {"cosa", "elemento", "accesorio", "objeto", "material", "repuesto", "articulo"}
_VAGUE = {
    "ficha", "cosa", "elemento", "accesorio", "material", "surtido", "surtida",
    "vario", "varia", "otro", "otra", "miscelaneo", "miscelanea", "repuesto", "objeto",
    "articulo", "producto", "pieza_",
}

# Errores de digitacion frecuentes (palabra mal escrita sin tildes -> la correcta).
_TYPOS = {
    "piesa": "pieza", "piesas": "piezas", "peiza": "pieza", "peizas": "piezas", "pizas": "piezas",
    "piza": "pieza", "lizas": "lisas", "liza": "lisa", "lizo": "liso", "lizos": "lisos",
    "bicel": "bisel", "biceles": "biseles", "vicel": "bisel", "viseles": "biseles", "pinez": "pines",
    "pinnes": "pines", "pinen": "pines", "tetone": "tetones", "resistensia": "resistencia",
    "resistensias": "resistencias", "resitencia": "resistencia", "resitencias": "resistencias",
    "capasitor": "capacitor", "capasitores": "capacitores", "condesador": "condensador",
    "condesadores": "condensadores", "tornilo": "tornillo", "tornilos": "tornillos",
    "yumper": "jumper", "yumpers": "jumpers", "senzor": "sensor", "senzores": "sensores",
    "cavle": "cable", "cavles": "cables", "protobord": "protoboard", "protoboar": "protoboard",
    "ardiuno": "arduino",
}
_IRREGULAR_SINGULAR = {
    "luces": "luz", "lapices": "lápiz", "peces": "pez", "veces": "vez", "cruces": "cruz",
    "reles": "relé", "switches": "switch", "jacks": "jack", "imanes": "imán", "ejes": "eje",
}
# Palabras que terminan en "s" sin ser plurales (y las que el singular
# aproximado no debe tocar).
_NOT_PLURAL = {
    "gris", "dos", "tres", "seis", "mas", "menos", "lunes", "chasis", "tesis", "crisis",
    "bus", "plus", "gas", "atlas", "lapiz", "x", "cms", "mms", "uds", "pzs", "kgs", "grs",
}
_VOWELS = set("aeiou")

# Palabras con las que empieza una parte y que no son parte del nombre.
_STARTER_WORDS = {
    "ademas", "tambien", "igualmente", "asimismo", "contiene", "contienen", "incluye", "incluyen",
    "tiene", "tienen", "hay", "trae", "traen", "lleva", "llevan", "guarda", "guardan", "almacena",
    "almacenan", "alberga", "albergan", "posee", "poseen", "encontramos", "encuentra", "encuentran",
    "encuentras", "aqui", "ahi", "dentro", "adentro",
}
_FUZZY_STARTERS = ("contiene", "contienen", "incluye", "incluyen", "almacena", "ademas", "tambien")
_ARTICLE_WORDS = {
    "y", "e", "un", "una", "unos", "unas", "el", "la", "los", "las", "alguno", "alguna", "algunos", "algunas",
    "vario", "varia", "varios", "varias", "otro", "otra", "otros", "otras", "en",
}
_APPROX_WORDS = {"aprox", "aproximadamente", "unos", "unas", "como", "casi"}


def fold(text) -> str:
    """Minusculas y sin tildes (la ñ queda como n): para comparar palabras sin
    depender de como se escribieron."""
    decomposed = unicodedata.normalize("NFKD", "" if text is None else str(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def singular(word: str) -> str:
    """Singular aproximado (en minuscula) de un sustantivo o adjetivo en español.
    Suficiente para nombres de inventario: piezas->pieza, pines->pin,
    biseles->bisel, separadores->separador, bases->base, botones->botón."""
    low = (word or "").lower()
    folded = fold(low)
    if folded in _IRREGULAR_SINGULAR:
        return _IRREGULAR_SINGULAR[folded]
    if (len(folded) <= 3 or folded in _NOT_PLURAL or not folded.endswith("s")
            or folded.endswith(("is", "us")) or not folded[:-1].isalpha()):
        return low
    if folded.endswith("ones") and len(folded) > 5:
        return low[:-4] + "ón"
    if folded.endswith("es") and len(folded) > 4:
        stem = folded[:-2]
        if stem[-1] in "lnrd" and stem[-2] in _VOWELS:
            return low[:-2]
    return low[:-1]


def _word_key(word: str) -> str:
    return fold(singular(word))


def _masculine(key: str) -> str:
    """Clave masculina de un adjetivo con genero (roja -> rojo); el resto igual."""
    if key.endswith("a") and key[:-1] + "o" in _ADJECTIVES:
        return key[:-1] + "o"
    return key


# ---------------------------------------------------------------------------
# Expresiones
# ---------------------------------------------------------------------------

_X = "[x×X*✕✖]"
# Dimensiones: 4x2, 4 x 2, 2x2x1. Una tercera cifra solo si se escribe igual que la
# primera pareja ("4x2 x10" son 4x2 con cantidad 10).
_DIM_RE = re.compile(
    rf"(?<![\w.,])(?:(\d{{1,4}}){_X}(\d{{1,4}}){_X}(\d{{1,4}})"
    rf"|(\d{{1,4}})\s+{_X}\s+(\d{{1,4}})\s+{_X}\s+(\d{{1,4}})"
    rf"|(\d{{1,4}})\s*{_X}\s*(\d{{1,4}}))(?!\w)"
)
_DIM_UNIT_RE = re.compile(r"\s*(mm|cm|mts?|m|cms|mms|pulgadas?|pulg)(?![^\W_])", re.IGNORECASE)
_PIN_RE = re.compile(r"(?:\bde\s+)?(?<![\w.,])(\d{1,3})\s*(?:pin(?:es)?|tet[oó]n(?:es)?|studs?)\b", re.IGNORECASE)

_COUNT_RE = re.compile(r"(?:\bde\s+)?(?<![\w.,])(\d{1,4})\s*(dientes|diente)\b", re.IGNORECASE)
_NUM = r"\d+(?:[.,]\d+)?"
# Unidades con valor: (nombre, expresion). El nombre dice como se escribe.
_UNIT_PATTERNS = (
    ("ohm", r"(?:(?:kilo|mega|meg)\s?o?h(?:mios?|ms?)|(?:[kKmM]\s?)?(?:ohmios?|ohms?|[\u03a9\u03c9\u2126]))"),
    ("farad", r"(?:(?:nano|micro|pico|mili)faradios?|[pnu\u00b5\u03bcm]f\b|faradios?)"),
    ("henry", r"(?:[pnu\u00b5\u03bcm]h\b|henrios?)"),
    ("volt", r"(?:vdc|vac|voltios?|volts?|v)"),
    ("amp", r"(?:miliamperios?|amperios?|amperes?|amps?|[mMu\u00b5\u03bc]?ah|[mMu\u00b5\u03bc]?a)"),
    ("watt", r"(?:[km]?w|watts?|vatios?)"),
    ("hertz", r"(?:[kmg]?hz)"),
    ("awg", r"awg"),
    ("rpm", r"rpm"),
    ("length", r"(?:mm|cm|mts?|metros?|cms|mms|m|pulgadas?|pulg)"),
    ("mass", r"(?:kg|kgs|mg|gr|grs|gramos?|kilos?|g)"),
    ("volume", r"(?:ml|cl|lts?|litros?|l)"),
    ("percent", r"%"),
)
_VALUE_RE = re.compile(
    rf"(?<![\w.,])(?P<num>\d+/\d+|{_NUM}(?:(?:[-–]|\s+a\s+|\s+hasta\s+){_NUM})?)\s*(?:"
    + "|".join(f"(?P<{name}>{pattern})" for name, pattern in _UNIT_PATTERNS)
    + r")(?![^\W_])",
    re.IGNORECASE,
)
_SHARED_UNIT = "(?:" + "|".join(pattern for _, pattern in _UNIT_PATTERNS) + r"|pin(?:es)?|dientes?)"
_LIST_SEP = r"(?:\s*[,;/]\s*|\s+[yYeE]\s+|\s+[-–—]\s+)"
_SHARED_LIST_RE = re.compile(
    rf"(?<![\w.,xX×*])(?P<nums>{_NUM}(?:{_LIST_SEP}{_NUM})+)\s*(?P<unit>{_SHARED_UNIT})(?![^\W_])", re.IGNORECASE
)
_BARE_K_RE = re.compile(
    r"(?<![\w.,])(?:(?P<a>\d+)k(?P<b>\d+)|(?P<num>\d+(?:[.,]\d+)?)\s?(?P<k>[kKM]))(?![^\W_])"
)

_UNIT_WORDS = r"unidades|unidad|unids?|unds?|und|uds?|u"
_PIECE_WORDS = r"piezas|pieza|pzas|pza|pzs|pz"
_QTY_LEAD_UNIT_RE = re.compile(rf"^(\d{{1,6}})\s*(?:{_UNIT_WORDS})\b\.?\s*(?:de\s+)?", re.IGNORECASE)
_QTY_LEAD_PIECE_RE = re.compile(rf"^(\d{{1,6}})\s*(?:{_PIECE_WORDS})\b\.?", re.IGNORECASE)
_QTY_LEAD_WORD_RE = re.compile(r"^(\d{1,12})\s+(?=[^\W\d_])")
_QTY_X_RE = re.compile(r"(?<![\w.,])[xX×]\s*(\d{1,12})(?![\w.,])")
_QTY_TRAIL_RE = re.compile(rf"(?<![\w.,])(\d{{1,6}})\s*(?:{_UNIT_WORDS}|{_PIECE_WORDS})\b\.?", re.IGNORECASE)
_QTY_WORD_RE = re.compile(r"(?<![\w])(?:cantidad|cant)\s*[:=]?\s*(\d{1,12})(?![\w.,])", re.IGNORECASE)

# Frases: salto de linea, punto y coma, viñetas y punto (no el decimal 2.5).
_STRONG_SPLIT = re.compile(r"\n+|;|[•·▪●○◦]|\.(?!\d)")
# Partes: coma, dos puntos, y/e/o/u, "+", " / ", guiones con espacio y guion entre medidas (4x2-2x2).
_WEAK_SPLIT = re.compile(
    r"(\s*,\s*|\s*:\s*|\s+[yeo]\s+|\s+\+\s+|\s+/\s+|(?<=\d)\s*/\s*(?=\d+\s*[x×X*]\s*\d)"
    r"|\s+[-–—]+\s*|\s*[-–—]+\s+|(?<=\d)[-–—](?=\d+\s*[x×X*]\s*\d))",
    re.IGNORECASE,
)
_EDGE_JUNK = re.compile(r"^[\s\-–—*•·,:;()\"'«»/]+|[\s\-–—*•·,:;.()\"'«»/]+$")
_WORD_RE = re.compile(r"[^\W_]+")
_TOKEN_RE = re.compile(r"[^\W_]+(?:[-'’.+][^\W_]+)*")
_LETTER_RE = re.compile(r"[^\W\d_]")
_REMARK_RE = re.compile("[-]")
_CONTENTS_RE = re.compile(
    r"\b(?:que\s+(?:adem[aá]s\s+)?(?:contiene|contienen|incluye|incluyen|tiene|tienen|trae|traen|lleva|llevan"
    r"|guarda|guardan|almacena|almacenan|alberga|albergan)"
    r"|donde\s+(?:hay|est[aá]n?)"
    r"|(?:dentro|adentro)\s+(?:de\s+(?:ella|esta|la\s+caja)\s+)?(?:hay|est[aá]n?|se\s+encuentran?)"
    r"|en\s+cuyo\s+interior\s+(?:hay|est[aá]n?))\b\s*:?\s*",
    re.IGNORECASE,
)
# Para algo que no es una caja ("Kit que incluye ...") solo cuentan los verbos claros de contenido.
_STRONG_CONTENTS_RE = re.compile(
    r"\bque\s+(?:adem[aá]s\s+)?(?:contiene|contienen|incluye|incluyen|guarda|guardan|almacena|almacenan"
    r"|alberga|albergan)\b\s*:?\s*",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"[,\s]*\b(?:etc[eé]tera|etc|entre\s+otr[oa]s(?:\s+cosas)?|y\s+dem[aá]s|y\s+otr[oa]s(?:\s+cosas)?)\b\.?",
    re.IGNORECASE,
)
_ABBREVIATION_RE = re.compile(
    r"\b(aprox|cant|uds?|unds?|und|pzs?|pzas?|nro|ref|max|min|mts?|cms?|mms?|kgs?|grs?|lts?|pulg)\.(?=\s|$|,|;)",
    re.IGNORECASE,
)
_LIST_MARKER_RE = re.compile(r"(?m)^[ \t]*(?:\d{1,2}[.)]|[a-zA-Z][.)]|[-*•·▪●○◦])[ \t]+")
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d{1,2}(?![\dxX×*]))")
_TIMES_WORD_RE = re.compile(r"(?<=\d)\s+(?:por|by)\s+(?=\d)", re.IGNORECASE)
_QTY_PAREN_RE = re.compile(
    rf"\(\s*[xX×]?\s*(\d{{1,6}})\s*(?:{_UNIT_WORDS}|{_PIECE_WORDS})?\s*\)", re.IGNORECASE
)
_PAREN_RE = re.compile(r"\(([^()]*)\)")


def _collapse(text) -> str:
    return re.sub(r"\s+", " ", "" if text is None else str(text)).strip()


def _shorten(text, limit: int = 80) -> str:
    text = _collapse(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _first_word(text: str) -> str:
    match = _LETTER_RE.search(text or "")
    if not match:
        return ""
    word = _WORD_RE.match(text, match.start())
    return word.group(0) if word else ""


# ---------------------------------------------------------------------------
# Reconocimiento de palabras (con errores de digitacion)
# ---------------------------------------------------------------------------

_FUZZY_VOCABULARY = sorted(
    key for key in (set(_NOUNS) | set(_CHARACTERISTICS) | set(_BRANDS) | _BOX_NOUNS) if len(key) >= 5
)


def _lookup(key: str):
    """(tipo, clave) de una palabra ya en singular y sin tildes."""
    key = _NOUN_ALIASES.get(key, key)
    if key in _BOX_NOUNS:
        return "box", key
    if key in _NOUNS:
        return "noun", key
    if key in _CHARACTERISTICS:
        return "char", key
    return None


@lru_cache(maxsize=8192)
def _resolve_cached(word: str):
    """Reconoce una palabra del vocabulario, corrigiendo errores de digitacion.
    Devuelve None o (tipo, clave, palabra_corregida, difusa): `tipo` es "noun",
    "box", "char" o "brand"; `palabra_corregida` es None si no hubo correccion
    y `difusa` indica que la correccion fue por parecido (menos segura)."""
    if not word or any(ch.isdigit() for ch in word):
        return None
    folded = fold(word)
    if folded in _BRANDS:
        return "brand", folded, None, False
    if folded in _BRAND_ALIASES:
        return "brand", _BRAND_ALIASES[folded], word, False
    single = fold(singular(word))
    hit = _lookup(single)
    if hit:
        return hit[0], hit[1], None, False
    typo = _TYPOS.get(folded) or _TYPOS.get(single)
    if typo and typo != folded:
        fixed = _resolve_cached(typo)
        if fixed:
            return fixed[0], fixed[1], typo, False
    if len(single) >= 6 and single.isalpha():
        close = difflib.get_close_matches(single, _FUZZY_VOCABULARY, n=1, cutoff=0.88)
        if close:
            fixed = _lookup(close[0])
            if fixed:
                return fixed[0], fixed[1], close[0], True
            if close[0] in _BRANDS:
                return "brand", close[0], close[0], True
    return None


def _resolve(word: str):
    return _resolve_cached(word or "")


def _first_kind(text: str) -> str:
    """Tipo de la primera palabra de una parte ("box", "noun", ...) o ""."""
    word = _first_word(text)
    hit = _resolve(word) if word else None
    return hit[0] if hit else ""


def _is_box(text: str) -> bool:
    return _first_kind(text) == "box"


def _has_contents_verb(text: str) -> bool:
    return bool(_CONTENTS_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Medidas
# ---------------------------------------------------------------------------

def _dims_of(match) -> list:
    return [int(g) for g in match.groups() if g is not None]


def _canonical_dims(match) -> str:
    return "x".join(str(d) for d in sorted(_dims_of(match)))


def _number(text: str) -> str:
    return re.sub(r"\s+(?:a|hasta)\s+", "-", text, flags=re.IGNORECASE).replace(",", ".")


def _unit_display(group: str, raw: str, number: str):
    """Texto canonico de un valor con unidad ("220 Ω", "10 µF", "20 cm") o None
    si la combinacion no es una medida (p. ej. "2 a" es la preposicion)."""
    low = fold(raw).replace(" ", "")
    micro = {"u": MICRO, "\u03bc": MICRO}
    if "/" in number and group != "watt":
        return None
    if group == "ohm":
        prefix = "k" if low[:1] == "k" else ("M" if low[:1] == "m" else "")
        return f"{number} {prefix}{OHM}"
    if group in ("farad", "henry"):
        letter = "F" if group == "farad" else "H"
        if low.startswith(("nano", "micro", "pico", "mili")):
            prefix = {"n": "n", "m": MICRO if low.startswith("micro") else "m", "p": "p"}[low[0]]
        elif low.startswith(("faradio", "henrio")):
            prefix = ""
        else:
            prefix = {"p": "p", "n": "n", "m": "m", **micro}[low[0]]
        return f"{number} {prefix}{letter}"
    if group == "volt":
        return f"{number} V"
    if group == "amp":
        if raw == "a":
            return None
        if low.startswith("miliamp"):
            return f"{number} mA"
        if low.startswith("amp"):
            return f"{number} A"
        hours = "h" if low.endswith("h") else ""
        prefix = {"m": "m", **micro}.get(low[:1], "") if low not in ("a", "ah") else ""
        return f"{number} {prefix}A{hours}"
    if group == "watt":
        prefix = {"k": "k", "m": "m"}.get(low[:1], "") if low not in ("w", "watt", "watts", "vatio", "vatios") else ""
        return f"{number} {prefix}W"
    if group == "hertz":
        prefix = {"k": "k", "m": "M", "g": "G"}.get(low[:1], "") if low != "hz" else ""
        return f"{number} {prefix}Hz"
    if group == "awg":
        return f"{number} AWG"
    if group == "rpm":
        return f"{number} rpm"
    if group == "length":
        return f"{number} {_length_unit(low)}"
    if group == "mass":
        unit = "kg" if low.startswith(("kg", "kilo")) else ("mg" if low == "mg" else "g")
        return f"{number} {unit}"
    if group == "volume":
        unit = "ml" if low == "ml" else ("cl" if low == "cl" else "L")
        return f"{number} {unit}"
    if group == "percent":
        return f"{number}%"
    return None


def _length_unit(low: str) -> str:
    if low.startswith(("mm", "milim")):
        return "mm"
    if low.startswith(("cm", "centim")):
        return "cm"
    if low.startswith(("pulg",)):
        return "in"
    return "m"


def _measure(family, text, feature, start, end, raw, **extra):
    base = {"family": family, "text": text, "feature": feature, "start": start, "end": end, "raw": raw,
            "zero": False, "oversize": False, "guess": "", "unitless": True}
    base.update(extra)
    return base


def _find_measures(text: str, bare_k: bool = False) -> list:
    """Medidas de la parte, en orden: dimensiones (4x2, 2 x 2 x 1, 20x30 cm),
    numero de pines ("de 1 pin") y valores con unidad (220 ohm, 10uF, 5 V, 20 cm).
    Cada una: {"family", "text" (para el nombre), "feature", "start", "end", "raw",
    "zero", "oversize", "guess", "unitless"}."""
    found = []
    for match in _DIM_RE.finditer(text):
        dims = _dims_of(match)
        normalized = "x".join(str(d) for d in dims)
        end, unitless = match.end(), True
        unit = _DIM_UNIT_RE.match(text, end)
        if unit:
            normalized += f" {_length_unit(fold(unit.group(1)))}"
            end, unitless = unit.end(), False
        zero = any(d == 0 for d in dims)
        found.append(_measure(
            "dim", normalized, normalized, match.start(), end, text[match.start():end],
            zero=zero, guess="x".join(str(d or 1) for d in dims) if zero else "",
            oversize=unitless and any(d > MAX_DIMENSION for d in dims), unitless=unitless,
        ))
    for match in _VALUE_RE.finditer(text):
        group = match.lastgroup
        display = _unit_display(group, match.group(group), _number(match.group("num")))
        if display:
            found.append(_measure("value:" + group, f"de {display}", display, match.start(), match.end(),
                                  match.group(0), unitless=False))
    for match in _PIN_RE.finditer(text):
        count = int(match.group(1))
        shown = f"{count} pin" if count == 1 else f"{count} pines"
        found.append(_measure("pins", f"de {shown}", shown, match.start(), match.end(), match.group(0),
                              zero=count == 0, guess="de 1 pin" if count == 0 else ""))
    for match in _COUNT_RE.finditer(text):
        shown = f"{int(match.group(1))} " + ("diente" if int(match.group(1)) == 1 else "dientes")
        found.append(_measure("count", f"de {shown}", shown, match.start(), match.end(), match.group(0)))
    if bare_k:
        for match in _BARE_K_RE.finditer(text):
            if match.group("a"):
                number, prefix = f"{match.group('a')}.{match.group('b')}", "k"
            else:
                number, prefix = _number(match.group("num")), "M" if match.group("k") == "M" else "k"
            display = f"{number} {prefix}{OHM}"
            found.append(_measure("value:ohm", f"de {display}", display, match.start(), match.end(),
                                  match.group(0), unitless=False))
    # Sin solapes: gana la que empieza antes y, a igual inicio, la mas larga.
    result, last_end = [], -1
    for measure in sorted(found, key=lambda m: (m["start"], -(m["end"] - m["start"]))):
        if measure["start"] >= last_end:
            result.append(measure)
            last_end = measure["end"]
    return result


def _normalize_values(text: str) -> str:
    """Reescribe los valores con unidad en su forma canonica ("220 ohm" ->
    "220 Ω", "10uF" -> "10 µF") y deja el resto igual."""
    out, last = [], 0
    for measure in _find_measures(text, bare_k=False):
        if measure["family"].startswith("value:"):
            out.append(text[last:measure["start"]])
            out.append(measure["feature"])
            last = measure["end"]
    out.append(text[last:])
    return "".join(out)


def product_key(name) -> frozenset:
    """Clave para detectar productos repetidos sin importar mayusculas, tildes,
    plurales, genero, palabras vacias, marca, unidades escritas distinto ni el
    orden de la medida (4x2 == 2x4, como en las piezas Lego).
    "Pieza Lego con pines 4x2" == "Piezas de pines 2x4"; "Resistencia de 220 Ω"
    == "resistencias 220 ohm"."""
    text = _normalize_values(str(name or ""))
    text = _DIM_RE.sub(lambda m: f" {_canonical_dims(m)} ", fold(text))
    keys = set()
    for word in _WORD_RE.findall(text):
        if word in _STOPWORDS or word in _BRANDS or word in _BRAND_ALIASES:
            continue
        key = word if _DIM_RE.fullmatch(word) else _word_key(word)
        key = _NOUN_ALIASES.get(key, key)
        key = _CHARACTERISTICS.get(key, (key,))[0]
        key = _masculine(key)
        if key in ("pieza", "", "con"):
            continue
        keys.add(key)
    return frozenset(keys)


def zero_measures(name) -> list:
    """Medidas con una dimension 0 dentro de un nombre ("1x0"): no existen."""
    return [m for m in _find_measures(str(name or "")) if m["zero"]]


def oversize_measures(name) -> list:
    """Dimensiones sin unidad mayores que MAX_DIMENSION dentro de un nombre."""
    return [m for m in _find_measures(str(name or "")) if m["oversize"]]


# ---------------------------------------------------------------------------
# Cantidades
# ---------------------------------------------------------------------------

def _find_quantity(text: str, measures: list):
    """Cantidad escrita en la parte ("20 piezas de 2x2", "x10", "50 uds", "cant. 5").
    Devuelve None o {"value": int, "spans": [(inicio, fin, reemplazo)], "others": [int]}:
    los tramos a quitar del texto (el reemplazo conserva "piezas" cuando es el
    sustantivo) y las demas cantidades distintas que trae la parte (dudosas)."""
    taken = [(m["start"], m["end"]) for m in measures]

    def free(start, end):
        return not any(start < t_end and t_start < end for t_start, t_end in taken)

    found = []  # (inicio, valor, fin, reemplazo)
    for regex, replacement in ((_QTY_LEAD_UNIT_RE, " "), (_QTY_LEAD_PIECE_RE, "piezas"), (_QTY_LEAD_WORD_RE, " ")):
        match = regex.match(text)
        if match and free(0, match.end(1)):
            found.append((0, int(match.group(1)), match.end(), replacement))
            break
    for regex in (_QTY_X_RE, _QTY_TRAIL_RE, _QTY_WORD_RE):
        for match in regex.finditer(text):
            if not free(match.start(), match.end()):
                continue
            if any(match.start() < end and start < match.end() for start, _, end, _ in found):
                continue
            found.append((match.start(), int(match.group(1)), match.end(), " "))
    if not found:
        return None
    found.sort()
    first = found[0][1]
    return {"value": first, "spans": [(start, end, repl) for start, _, end, repl in found],
            "others": sorted({value for _, value, _, _ in found if value != first})}


def _remove_spans(text: str, spans) -> str:
    """Texto sin los tramos (inicio, fin) o (inicio, fin, reemplazo)."""
    out, last = [], 0
    for span in sorted(spans, key=lambda s: s[0]):
        start, end = span[0], span[1]
        if start < last:
            continue
        out.append(text[last:start])
        out.append(span[2] if len(span) > 2 else " ")
        last = end
    out.append(text[last:])
    return _collapse("".join(out))


# ---------------------------------------------------------------------------
# Preparacion del texto
# ---------------------------------------------------------------------------

def _expand_shared_units(text: str) -> str:
    """"220, 330 y 470 ohm" -> "220 ohm, 330 ohm y 470 ohm" (la unidad compartida
    se reparte); las fracciones de vatio ("1/4 W") se dejan."""
    def spread(match):
        unit = match.group("unit")
        nums = match.group("nums")
        probe = _VALUE_RE.fullmatch(f"1 {unit}")
        if probe and probe.lastgroup == "watt" and "/" in nums:
            return match.group(0)
        pieces = re.split(f"({_LIST_SEP})", nums)
        return "".join(p if i % 2 else f"{p} {unit}" for i, p in enumerate(pieces))
    return _SHARED_LIST_RE.sub(spread, text)


def _prepare(text):
    """Texto listo para dividir en partes. Devuelve (texto, aclaraciones, avisos):
    `aclaraciones` son los parentesis que no son medida ni cantidad (se sustituyen
    por un caracter marcador) y `avisos` cuenta si habia «etc.» o si se corto."""
    notices = {"filler": False, "truncated": False}
    text = "" if _is_missing(text) else str(text)
    if len(text) > MAX_TEXT_LENGTH:
        text, notices["truncated"] = text[:MAX_TEXT_LENGTH], True
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[​‌‍﻿]", "", text)
    text = re.sub(r"[   \t]", " ", text)
    text = re.sub(r"[-]", " ", text)
    text = _LIST_MARKER_RE.sub("", text)
    if _FILLER_RE.search(text):
        notices["filler"] = True
        text = _FILLER_RE.sub("", text)
    text = re.sub(r"\b(?:cantidad|cant)\s*:\s*(?=\d)", "x", text, flags=re.IGNORECASE)
    text = _ABBREVIATION_RE.sub(r"\1", text)
    text = _DECIMAL_COMMA_RE.sub(".", text)
    text = _TIMES_WORD_RE.sub("x", text)
    text = _QTY_PAREN_RE.sub(lambda m: f" x{m.group(1)} ", text)

    remarks = []

    def parenthetical(match):
        inner = match.group(1).strip()
        if not inner:
            return " "
        if _find_measures(inner, bare_k=True):
            return f" , {inner} , "
        remarks.append(_collapse(inner))
        return chr(0xE100 + len(remarks) - 1)

    for _ in range(3):
        text = _PAREN_RE.sub(parenthetical, text)
    text = text.replace("(", " ").replace(")", " ")
    return _expand_shared_units(text), remarks, notices


# ---------------------------------------------------------------------------
# Partes de la descripcion
# ---------------------------------------------------------------------------

def _is_starter(word: str) -> bool:
    key = fold(word)
    if key in _STARTER_WORDS:
        return True
    return len(key) >= 7 and bool(difflib.get_close_matches(key, _FUZZY_STARTERS, n=1, cutoff=0.84))


_CONTAINER_WORD = "contenedor"
_MULTIWORD_STARTER_RE = re.compile(r"^(?:se\s+encuentran?|cuentan?\s+con|dispone[n]?\s+de)\b", re.IGNORECASE)


def _clean(raw: str):
    """Quita bordes y muletillas iniciales ("Además, Contiene un", "Contendor 2
    contiene", "aprox."). Devuelve (texto, empezaba_con_verbo, aproximado)."""
    text = _EDGE_JUNK.sub("", raw or "")
    starter = approx = False
    while True:
        match = re.match(r"[^\W\d_]+", text)
        if not match:
            break
        word, key = match.group(0), fold(match.group(0))
        rest = _EDGE_JUNK.sub("", text[match.end():])
        if len(key) >= 8 and difflib.SequenceMatcher(None, key, _CONTAINER_WORD).ratio() >= 0.85:
            # "Contendor 2 contiene ..." / "Contenedor con ...": el contenedor mismo no es un producto.
            after = re.sub(r"^\d+\s*", "", rest)
            following = _first_word(after)
            if after != rest or _is_starter(following) or fold(following) in ("con", "de", "que", "tiene"):
                text, starter = re.sub(r"^(?:con|de|que)\b\s*", "", after, flags=re.IGNORECASE), True
                continue
            break
        if _MULTIWORD_STARTER_RE.match(text):
            text = _EDGE_JUNK.sub("", _MULTIWORD_STARTER_RE.sub("", text, count=1))
            starter = True
            continue
        if _is_starter(word):
            starter = True
        elif key in _APPROX_WORDS and re.match(r"\d", rest):
            approx = True
        elif key in ("alrededor", "cerca") and re.match(r"de\s+\d", rest, re.IGNORECASE):
            approx = True
            rest = re.sub(r"^de\s+", "", rest, flags=re.IGNORECASE)
        elif key in _ARTICLE_WORDS:
            pass
        else:
            break
        text = rest
    return _collapse(text), starter, approx


def _sep_kind(separator: str) -> str:
    token = (separator or "").strip().lower()
    if token == ",":
        return "comma"
    if token == ":":
        return "colon"
    if token in ("y", "e", "+", "/"):
        return "and"
    if token in ("o", "u"):
        return "or"
    return "dash"


def _measure_only(clean: str) -> bool:
    """La parte es solo una medida (y quiza una cantidad): "4x2", "x5 de 2x2"."""
    measures = _find_measures(clean, bare_k=True)
    if not measures:
        return False
    quantity = _find_quantity(clean, measures)
    spans = [(m["start"], m["end"]) for m in measures] + (quantity["spans"] if quantity else [])
    return not _strip_stopword_edges(_remove_spans(clean, spans))


def _starts_new(clean: str, starter: bool, sep: str, prev_clean: str) -> bool:
    if starter or not clean or sep in ("strong", "dash", "colon"):
        return True
    if _has_contents_verb(prev_clean):
        return True  # lo que sigue a "que contiene ..." son productos aparte (dentro de la caja)
    if sep in ("and", "comma", "or") and _is_box(prev_clean) and _measure_only(clean):
        return False  # "Caja con piezas 2x2 y 4x2": la segunda medida sigue la lista de la caja
    measures = _find_measures(clean, bare_k=True)
    if measures or _find_quantity(clean, measures):
        return True
    kind = _first_kind(clean)
    if kind == "box":
        return True
    if _word_key(_first_word(clean)) in _VAGUE_HEADS or fold(_first_word(clean)) in _PRODUCT_HEADS:
        return not _is_box(prev_clean)
    if kind == "noun":
        # "Caja con puertas y ventanas": lo que sigue a una caja es su contenido.
        return not _is_box(prev_clean)
    return False


def _split_parts(text: str, remarks=()) -> list:
    """Partes interpretables, ya unidas cuando un "y"/"," no empieza un
    producto nuevo. Cada parte: {"raw", "clean", "sep", "clause", "starter",
    "approx", "remarks", "after_box_and", "prev_clean"}."""
    merged = []
    clause_number = 0
    for clause in _STRONG_SPLIT.split(text or ""):
        clause_number += 1
        pieces = _WEAK_SPLIT.split(clause)
        for index in range(0, len(pieces), 2):
            raw = pieces[index]
            separator = pieces[index - 1] if index else ""
            sep = _sep_kind(separator) if index else "strong"
            found = [remarks[ord(ch) - 0xE100] for ch in _REMARK_RE.findall(raw) if ord(ch) - 0xE100 < len(remarks)]
            raw = _REMARK_RE.sub(" ", raw)
            clean, starter, approx = _clean(raw)
            previous = merged[-1] if merged else None
            if not clean:
                if previous:
                    previous["remarks"].extend(found)  # solo muletillas o una aclaracion: es de la parte anterior
                continue
            if previous and len(previous["raw"]) < MAX_PART_LENGTH \
                    and not _starts_new(clean, starter, sep, previous["clean"]):
                previous["raw"] = f"{previous['raw']}{separator}{raw}"
                previous["clean"] = _clean(previous["raw"])[0]
                previous["remarks"].extend(found)
                continue
            merged.append({
                "raw": raw, "clean": clean, "sep": sep, "clause": clause_number, "starter": starter,
                "approx": approx, "remarks": list(found),
                "after_box_and": bool(sep in ("and", "or") and previous and _is_box(previous["clean"])),
                "prev_clean": previous["clean"] if previous else "",
            })
    return merged


# ---------------------------------------------------------------------------
# Nombres
# ---------------------------------------------------------------------------

def _keep_as_written(word: str) -> bool:
    """Siglas cortas (USB, LED, EV3), medidas y mezclas (NodeMCU) se respetan tal cual."""
    return (any(ch.isdigit() for ch in word) or (1 < len(word) <= 4 and word.isupper())
            or (len(word) == 1 and word.isupper() and word not in "YOEU")      # «tipo C», «clase B»
            or any(ch.isupper() for ch in word[1:]) and not word.isupper())


def _shouting(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 4 and not any(ch.islower() for ch in letters)


def _display_word(word: str, shouty: bool = False) -> str:
    """Una palabra como se muestra dentro de un nombre: marcas y siglas conocidas,
    medidas y mezclas tal cual, el resto en minuscula."""
    key = fold(word)
    if key in _BRANDS:
        return _BRANDS[key]
    if key in _PROPER:
        return _PROPER[key]
    if shouty:
        return word if any(ch.isdigit() for ch in word) else word.lower()
    if _keep_as_written(word):
        return word
    return word.lower()


_PUNCT_RE = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)


def _sentence_case(text: str, singular_first: bool = False) -> str:
    """"Caja con Puertas y Ventanas" -> "Caja con puertas y ventanas". Respeta
    marcas, siglas (EV3, USB, UNO) y medidas; un texto TODO EN MAYUSCULAS se pasa a
    minusculas. Un nombre de modelo en mayuscula inicial tras un nombre propio
    (Arduino Nano) conserva la mayuscula."""
    shouty = _shouting(text)
    words, after_proper = [], False
    for index, word in enumerate(text.split()):
        lead, core, trail = _PUNCT_RE.match(word).groups()
        shown = _display_word(core, shouty) if core else core
        if index == 0:
            if singular_first and core and _lookup(fold(singular(core))):
                shown = singular(core)
            shown = shown[:1].upper() + shown[1:]
        elif after_proper and core[:1].isupper() and not shouty and shown == core.lower():
            shown = shown[:1].upper() + shown[1:]
        after_proper = fold(core) in _PROPER and fold(core) not in ("led", "leds", "usb")
        words.append(lead + shown + trail)
    return " ".join(words)


def _strip_stopword_edges(text: str) -> str:
    words = _collapse(_EDGE_JUNK.sub("", text or "")).split()
    while words and fold(words[0]) in _STOPWORDS:
        words.pop(0)
    while words and fold(words[-1]) in _STOPWORDS:
        words.pop()
    return " ".join(words)


def _looks_like_adjective(word: str) -> bool:
    key = fold(singular(word))
    return key in _ADJECTIVES_FIXED or _masculine(key) in _ADJECTIVES


def _guess_gender(word: str) -> str:
    key = fold(word)
    if key.endswith(("cion", "sion", "dad", "tad", "tud", "umbre")) or key.endswith("a") and key not in (
            "problema", "mapa", "dia", "sistema", "programa", "tema", "idioma"):
        return "f"
    return "m"


def _agree(word: str, gender: str) -> str:
    """Adjetivo en singular y con el genero del sustantivo (roja -> rojo con "Bloque")."""
    plain = fold(word)
    base = _ADJECTIVES.get(_masculine(plain))
    if base is None:
        return word
    return base if gender == "m" or not base.endswith("o") else base[:-1] + "a"


def _analyze_subject(subject: str) -> dict:
    """Sustantivo, marca, caracteristicas y adjetivos libres de un sujeto como
    "Piezas Lego de Pines", "piezas con Biseles" o "resistencias de carbón".

    Lo que va tras una preposicion que no es una caracteristica ("con 3 sensores
    ultrasónicos", "de carbón") se conserva tal cual lo escribio el usuario.
    `groups` son esos adjetivos/complementos: {"preposition", "words", "position"}."""
    info = {"noun": None, "key": None, "gender": "f", "brand": None, "characteristics": [],
            "groups": [], "head_free": False, "fixes": [], "fuzzy": False, "text": subject or ""}
    shouty = _shouting(subject or "")
    preposition, group, conjunction, after_proper = None, None, None, False
    for position, token in enumerate(_TOKEN_RE.findall(subject or "")):
        key = fold(token)
        if key in _PREPOSITIONS:
            preposition, group, conjunction, after_proper = key, None, None, False
            continue
        if key in _CONJUNCTIONS:
            conjunction = key if group is not None else None
            continue
        if key in _STOPWORDS or key in _ARTICLE_WORDS or key in _RELATIVE_VERBS:
            continue
        hit = _resolve(token)
        kind = hit[0] if hit else None
        verbatim = group is not None and group["preposition"] is not None
        if hit and hit[2]:
            info["fixes"].append((token, hit[2]))
            info["fuzzy"] = info["fuzzy"] or hit[3]
        complement = preposition is not None and bool(info["groups"])    # «Paquete con placas»: placas no es el nucleo
        if kind in ("char", "brand") or (kind in ("noun", "box") and not verbatim and info["noun"] is None
                                         and not complement):
            preposition, group, conjunction = None, None, None
            if kind == "brand":
                shown = _BRANDS[hit[1]]
                if not info["brand"]:
                    info["brand"] = shown
                elif shown not in info["brand"].split():
                    info["brand"] += " " + shown  # "Lego Technic"
            elif kind == "char":
                if _CHARACTERISTICS[hit[1]] not in info["characteristics"]:
                    info["characteristics"].append(_CHARACTERISTICS[hit[1]])
            else:
                info["noun"], info["gender"] = _NOUNS.get(hit[1], (hit[1].capitalize(), "f"))
                info["key"] = hit[1]
            continue
        # Palabra libre: adjetivo, nombre propio o complemento.
        word = _display_word(token, shouty)
        if after_proper and token[:1].isupper() and not shouty and word == token.lower():
            word = word.capitalize()  # modelo de un nombre propio: "Arduino Nano"
        after_proper = key in _PROPER and key not in ("led", "leds", "usb")
        if preposition is not None:
            group = {"preposition": preposition, "words": [word], "position": position}
            info["groups"].append(group)
        elif group is not None:
            group["words"].extend(([conjunction] if conjunction else []) + [word])
        else:
            group = {"preposition": None, "words": [word], "position": position}
            info["groups"].append(group)
        preposition = conjunction = None
    # Sin sustantivo conocido: la primera palabra libre (si no es un adjetivo) es el sustantivo.
    first = info["groups"][0] if info["groups"] else None
    if info["noun"] is None and first and first["preposition"] is None and first["position"] == 0 \
            and not _looks_like_adjective(first["words"][0]):
        head = first["words"].pop(0)
        info["noun"] = head[:1].upper() + head[1:]
        info["gender"], info["head_free"] = _guess_gender(head), True
        if not first["words"]:
            info["groups"].pop(0)
    return info


def _render_groups(info: dict, gender: str, with_prepositions: bool = True) -> list:
    """Adjetivos y complementos del sujeto, listos para el nombre."""
    rendered = []
    for group in info["groups"]:
        if group["preposition"] is not None:
            if with_prepositions:
                rendered.append(" ".join([group["preposition"], *group["words"]]))
            continue
        words = []
        counted = any(ch.isdigit() for word in group["words"] for ch in word)  # "3 colores": tal cual
        for word in group["words"]:
            if counted or word in _CONJUNCTIONS or word[:1].isupper() or _keep_as_written(word) or fold(word) in _PROPER:
                words.append(word)
            else:
                words.append(_agree(singular(word), gender))
        rendered.append(" ".join(words))
    return rendered


def _build_name(info: dict, measure_text: str, brand: str, noun_fallback=None):
    """(nombre, caracteristica) canonicos: sustantivo, marca, caracteristicas,
    adjetivos, medida. La caracteristica es lo que distingue al producto."""
    noun = info["noun"] or (noun_fallback[0] if noun_fallback else "Pieza")
    if info["head_free"] and measure_text:
        noun = singular(noun)[:1].upper() + singular(noun)[1:]  # "Cristales de 16 MHz" -> "Cristal de 16 MHz"
    gender = info["gender"] if info["noun"] else (noun_fallback[1] if noun_fallback else "f")
    chars = [entry[1 if gender == "f" else 2] for entry in info["characteristics"]]
    complements = _render_groups(info, gender)
    words = [noun] + ([brand] if brand else []) + chars + complements + ([measure_text] if measure_text else [])
    return _collapse(" ".join(words))


def _feature(info: dict, measure_feature: str, noun_fallback=None, is_product=False) -> str:
    gender = info["gender"] if info["noun"] else (noun_fallback[1] if noun_fallback else "f")
    chars = [entry[1 if gender == "f" else 2] for entry in info["characteristics"]]
    parts = list(chars)
    for group in info["groups"]:
        rendered = _render_groups({"groups": [group]}, gender)
        text = rendered[0] if rendered else ""
        short = len(group["words"]) <= 2
        if text and (group["preposition"] is None or (short and not is_product)):
            parts.append(text)
    if measure_feature:
        parts.append(measure_feature)
    text = _collapse(" ".join(parts))
    return text[:1].upper() + text[1:]


def _vague_words(text: str) -> list:
    """Terminos genericos que cierran el nombre ("... separadores de fichas")."""
    words = _WORD_RE.findall(text or "")
    if not words:
        return []
    last = words[-1]
    return [last.lower()] if _word_key(last) in _VAGUE else []


def _context_brand(*texts) -> str:
    for text in texts:
        for word in _WORD_RE.findall(fold(text)):
            word = _BRAND_ALIASES.get(word, word)
            if word in _CONTEXT_BRANDS:
                return _BRANDS[word]
    return ""


def _entry(kind, name, feature, source, quantity=None, notes=None, selected=True, confidence=CONFIDENCE_HIGH,
           inside=""):
    return {
        "kind": kind, "name": name, "feature": feature, "source": source,
        "quantity": quantity, "notes": list(notes or []), "selected": selected,
        "confidence": confidence, "inside": inside,
    }


def _level(flags) -> str:
    levels = {level for level, _ in flags}
    if CONFIDENCE_LOW in levels:
        return CONFIDENCE_LOW
    return CONFIDENCE_MEDIUM if CONFIDENCE_MEDIUM in levels else CONFIDENCE_HIGH


# ---------------------------------------------------------------------------
# Interpretacion de la descripcion
# ---------------------------------------------------------------------------

_RESISTOR_KEYS = {"resistencia", "potenciometro"}
_SPEC_ORDER = {name: index for index, name in enumerate(
    ("value:ohm", "value:farad", "value:henry", "value:volt", "value:amp", "value:watt", "value:hertz"))}
_NOUN_FOR_UNIT = {"value:ohm": "resistencia", "value:farad": "condensador", "value:henry": "bobina"}


class _Parser:
    """Recorre las partes de la descripcion y arma las entradas. Guarda el
    sujeto vigente (para medidas sueltas como "2x2") y la caja vigente (para
    saber que va dentro de ella)."""

    def __init__(self, default_brand: str):
        self.default_brand = default_brand
        self.entries, self.unparsed = [], []
        self.current = None      # (info, texto) del sujeto vigente
        self.last_free = None    # producto sin medida de la parte anterior (posible encabezado)
        self.inside = None       # {"name", "clause"} de la caja que "contiene" lo que sigue
        self.queue = []
        self.position = 0

    # -- recorrido ---------------------------------------------------------

    def run(self, parts):
        self.queue, self.position = list(parts), 0
        while self.position < len(self.queue):
            part = self.queue[self.position]
            self.position += 1
            try:
                self.handle(part)
            except Exception:  # noqa: BLE001 - una parte rara nunca debe tumbar la vista
                self.unparsed.append(part.get("clean", ""))
                self.last_free = None

    def inside_for(self, part):
        if self.inside and self.inside["clause"] == part.get("clause"):
            return self.inside
        return None

    def add(self, entry, part, flags=(), tail=()):
        """Agrega la entrada. Notas: cantidad, motivos de duda, aclaraciones y,
        si va dentro de una caja, donde esta. La confianza sale de los motivos."""
        notes = list(entry["notes"])
        flags = list(flags)
        if _too_long(entry["name"]):
            # Un nombre nunca debe ser una oracion: se acorta y se deja el texto completo en la nota.
            flags.append((CONFIDENCE_LOW, f"El nombre era una frase larga (parece una oración, no un producto) y "
                                          f"se acortó; el texto era «{entry['name']}»."))
            entry["name"] = _short_name(entry["name"])
        for _, note in flags + [(None, t) for t in tail]:
            if note not in notes:
                notes.append(note)
        inside = self.inside_for(part)
        if inside and entry["kind"] != KIND_BOX:
            entry["inside"] = inside["name"]
            notes.append(f"Según la descripción está dentro de «{inside['name']}»; "
                         "se propone como producto propio del contenedor.")
        entry["notes"] = notes
        entry["confidence"] = _level(flags)
        entry["selected"] = entry["confidence"] != CONFIDENCE_LOW
        self.entries.append(entry)

    # -- una parte ---------------------------------------------------------

    def handle(self, part):
        clean = part["clean"]
        measures = _find_measures(clean, bare_k=self.bare_k(clean))
        if not measures and _starts_prose(clean):
            self.last_free = None
            return                       # «Este estuche...», «De esta manera...»: prosa, no un producto
        quantity = _find_quantity(clean, measures)
        remainder = _remove_spans(clean, quantity["spans"] if quantity else [])
        previous = self.entries[-1] if self.entries else None
        can_attach = (quantity is not None and quantity["value"] <= MAX_QUANTITY and part["sep"] != "and"
                      and previous is not None and previous["quantity"] is None)
        if can_attach and not measures and fold(remainder) in ("", "pieza", "piezas"):
            # "Pines 4x2 - 20 uds": la cantidad es de la propuesta anterior.
            previous["quantity"] = quantity["value"]
            previous["notes"].insert(0, f"Cantidad tomada de la descripción ({quantity['value']}).")
            self.last_free = None
            return
        if not _LETTER_RE.search(remainder) and not measures:
            if clean.isdigit() and part["sep"] in ("dash", "comma", "colon") and previous is not None \
                    and previous["quantity"] is None and int(clean) <= MAX_QUANTITY and not previous["inside"]:
                previous["quantity"] = int(clean)
                previous["notes"].insert(0, f"Se tomó «{clean}» como la cantidad de «{previous['name']}»: "
                                            "confírmalo.")
                if previous["confidence"] == CONFIDENCE_HIGH:
                    previous["confidence"] = CONFIDENCE_MEDIUM
            else:
                self.unparsed.append(clean)
            self.last_free = None
            return

        flags, notes, tail = [], [], []
        if quantity and quantity["value"] > MAX_QUANTITY:
            flags.append((CONFIDENCE_LOW, f"La cantidad escrita ({quantity['value']}) es demasiado grande: "
                                          "revisa la descripción."))
            quantity = dict(quantity, value=None)
        if quantity and quantity["value"] is not None:
            notes.append(f"Cantidad tomada de la descripción ({quantity['value']})."
                         + (" Es aproximada." if part.get("approx") else ""))
        if quantity and quantity["value"] is not None and quantity["others"]:
            flags.append((CONFIDENCE_MEDIUM,
                          f"La parte trae más de una cantidad ({quantity['value']} y "
                          f"{', '.join(str(v) for v in quantity['others'])}): se tomó {quantity['value']}."))
        if part["after_box_and"] and not self.inside_for(part) and not _is_box(remainder):
            joiner = "o" if part["sep"] == "or" else "y"
            flags.append((CONFIDENCE_MEDIUM,
                          f"Venía unido con «{joiner}» a «{self.last_box_name(part)}»: confirma si está dentro "
                          "de esa caja o suelto en el contenedor."))
        elif part["sep"] == "or":
            flags.append((CONFIDENCE_MEDIUM, "Venía unido con «o»: confirma si son productos distintos."))
        if part["remarks"]:
            tail.append("La descripción aclara: " + "; ".join(f"«{r}»" for r in part["remarks"]) + ".")

        # "Caja con X que contiene A y B" / "Kit que incluye A": lo que sigue va dentro.
        verb = (_CONTENTS_RE if _is_box(remainder) else _STRONG_CONTENTS_RE).search(remainder)
        head, rest = (_EDGE_JUNK.sub("", remainder[:verb.start()]), remainder[verb.end():]) if verb \
            else (remainder, "")
        if verb and not _LETTER_RE.search(head):
            head, verb = "", None
            self.insert(part, rest, head)
            return
        last = self.entries[-1] if self.entries else None
        if _is_box(head):
            self.box(part, head, quantity, notes, flags, tail)
        elif verb:
            head_measures = _find_measures(head, bare_k=self.bare_k(head))
            head_quantity = dict(quantity, spans=[]) if quantity else None
            if head_measures:
                self.pieces(part, head, head_measures, head_quantity, notes, flags, tail)
            else:
                self.product(part, head, quantity, notes, flags, tail)
        elif not measures:
            self.product(part, head, quantity, notes, flags, tail)
        else:
            self.pieces(part, clean, measures, quantity, notes, flags, tail)
        added = self.entries[-1] if self.entries and self.entries[-1] is not last else None
        following = self.queue[self.position] if self.position < len(self.queue) else None
        colon = bool(following and following["sep"] == "colon" and following["clause"] == part["clause"]
                     and added is not None and added["kind"] == KIND_BOX)
        if added is not None and (verb or colon):
            self.inside = {"name": added["name"], "clause": part["clause"]}
            self.current = self.last_free = None
        elif added is not None and added["kind"] == KIND_BOX:
            self.inside = None
        if verb:
            self.insert(part, rest, head)

    def insert(self, part, rest: str, head: str):
        """Lo que sigue a «que contiene ...» se procesa a continuacion como partes propias."""
        remainder = _clean(rest)[0] if rest else ""
        if remainder:
            self.queue.insert(self.position, {
                "raw": rest, "clean": remainder, "sep": "strong", "clause": part["clause"], "starter": True,
                "approx": False, "remarks": [], "after_box_and": False, "prev_clean": head,
            })
            self.resplit()

    def resplit(self):
        """Divide la parte recien insertada en sus productos ("fichas y piezas 2x2")."""
        part = self.queue[self.position]
        pieces = _split_parts(part["clean"])
        for piece in pieces:
            piece["clause"], piece["starter"] = part["clause"], True
        self.queue[self.position:self.position + 1] = pieces

    def bare_k(self, text: str) -> bool:
        """"10k" solo es una resistencia si el contexto habla de resistencias."""
        if self.current and self.current[0]["key"] in _RESISTOR_KEYS:
            return True
        for word in _WORD_RE.findall(text):
            hit = _resolve(word)
            if hit and hit[1] in _RESISTOR_KEYS:
                return True
        return False

    def last_box_name(self, part) -> str:
        for entry in reversed(self.entries):
            if entry["kind"] == KIND_BOX:
                return entry["name"]
        return _sentence_case(_clean(part["prev_clean"])[0], singular_first=True)

    # -- cajas -------------------------------------------------------------

    def box(self, part, text, quantity, notes, flags, tail):
        name = _sentence_case(text, singular_first=True)
        vague = _vague_words(text)
        if vague:
            flags.append((CONFIDENCE_MEDIUM,
                          f"«{', '.join(vague)}» es un término genérico: confirma qué contiene la caja "
                          "(p. ej. qué tipo de piezas) y ajusta el nombre."))
        tail.insert(0, "Caja física: en Cantidad registra las piezas que contiene (o 1 si cuentas la caja).")
        self.add(_entry(KIND_BOX, name, "Caja", _collapse(text), quantity["value"] if quantity else None, notes),
                 part, flags, tail)
        self.current = self.last_free = None

    # -- productos sin medida ---------------------------------------------

    def product(self, part, text, quantity, notes, flags, tail):
        text = _collapse(text)
        info = _analyze_subject(text)
        if not (info["noun"] or info["characteristics"] or info["groups"]) \
                or len(re.sub(r"[\W\d_]", "", text)) < 2:
            self.unparsed.append(part["clean"])
            self.last_free = None
            return
        if not re.search(r"[^\W\d_]{3}", text) or (info["head_free"] and len(info["noun"]) < 2):
            flags.append((CONFIDENCE_LOW, "No parece un nombre de producto: revisa la descripción."))
        several = bool(re.search(r"\s[ye]\s|,", text, re.IGNORECASE))
        if several:
            name, feature = _sentence_case(text), ""  # varios productos juntos: se deja como lo escribio
        else:
            name = _build_name(info, "", info["brand"] or "")
            feature = _feature(info, "", is_product=True)
        self.note_fixes(info, flags)
        vague = _vague_words(text)
        if vague:
            flags.append((CONFIDENCE_MEDIUM, f"«{', '.join(vague)}» es un término genérico: precisa qué producto es."))
        elif info["key"] == "pieza" and not info["characteristics"] and not info["groups"] and not info["brand"]:
            flags.append((CONFIDENCE_MEDIUM, "Solo dice «pieza»: precisa qué tipo de pieza es."))
        if several:
            flags.append((CONFIDENCE_MEDIUM,
                          "Parece nombrar varios productos: si es así, divídelo o ajusta el nombre."))
        elif len(text.split()) > 8:
            flags.append((CONFIDENCE_MEDIUM, "Es una frase larga: revisa que sea un solo producto."))
        entry = _entry(KIND_PRODUCT, name, feature, text, quantity["value"] if quantity else None, notes)
        self.add(entry, part, flags, tail)
        self.last_free = {"entry": entry, "info": info, "text": text}

    # -- piezas con medida -------------------------------------------------

    def pieces(self, part, clean, measures, quantity, notes, flags, tail):
        spans = [(m["start"], m["end"]) for m in measures] + (quantity["spans"] if quantity else [])
        phrase = _strip_stopword_edges(_remove_spans(clean, spans))
        fresh = _analyze_subject(phrase) if phrase else None
        inherited = False
        if fresh and (fresh["noun"] or fresh["characteristics"] or fresh["groups"] or fresh["brand"]):
            info = fresh
            if fresh["noun"] is None and self.current:
                before = self.current[0]
                info = dict(fresh, noun=before["noun"], key=before["key"], gender=before["gender"],
                            brand=fresh["brand"] or before["brand"])
            self.current = (info, phrase)
        elif self.last_free is not None and self.entries and self.entries[-1] is self.last_free["entry"]:
            header = self.last_free
            self.entries.pop()
            self.current = (header["info"], header["text"])
            inherited = True
        elif self.current:
            inherited = True
        self.last_free = None
        known = self.current is not None
        info, subject_text = self.current if known else (_analyze_subject(""), "")
        self.note_fixes(info, flags)
        for first, second in zip(measures, measures[1:]):
            if fold(clean[first["end"]:second["start"]]).strip() in ("a", "hasta"):
                first["range"] = second["range"] = True  # "de 2x2 a 2x6"
        for measure in self.group_measures(measures):
            if inherited:
                source = f"{subject_text} … {measure['raw']}"
            elif len(measures) > 1 and not measure.get("merged"):
                source = f"{phrase} … {measure['raw']}" if phrase else measure["raw"]
            else:
                source = clean
            self.piece_entry(part, source, measure, info, known, inherited, quantity, notes, list(flags), tail)

    @staticmethod
    def group_measures(measures):
        """Una entrada por medida, salvo valores de distinta magnitud en una misma
        parte ("100 µF 25 V"), que son las caracteristicas de UN producto."""
        families = [m["family"] for m in measures]
        if len(measures) > 1 and all(f.startswith("value:") for f in families) and len(set(families)) == len(families):
            measures = sorted(measures, key=lambda m: _SPEC_ORDER.get(m["family"], 99))
            families = [m["family"] for m in measures]
            joined = " ".join(m["feature"] for m in measures)
            return [dict(measures[0], text=f"de {joined}", feature=joined, family="+".join(families),
                         raw=" ".join(m["raw"] for m in measures), end=measures[-1]["end"], merged=True)]
        return measures

    def piece_entry(self, part, source, measure, info, known, inherited, quantity, notes, flags, tail):
        brand = info["brand"] or ""
        if brand and self.default_brand and self.default_brand not in brand.split():
            brand = f"{self.default_brand} {brand}"  # "Technic" en un contenedor Lego: "Lego Technic"
        family = measure["family"]
        noun_fallback = None
        if not brand and self.default_brand and family in ("dim", "pins") and measure["unitless"] \
                and (info["noun"] is None or info["key"] in _PIECE_NOUNS):
            brand = self.default_brand
        if info["noun"] is None and family.startswith("value:") and not info["characteristics"] \
                and not info["groups"]:
            inferred = _NOUN_FOR_UNIT.get(family.split("+")[0])
            if inferred:
                noun_fallback = _NOUNS[inferred]
                flags.append((CONFIDENCE_MEDIUM,
                              f"La descripción no dice qué es «{measure['raw']}»; por la unidad se asumió "
                              f"«{noun_fallback[0]}»: revisa el nombre."))
        if not known and noun_fallback is None:
            flags.append((CONFIDENCE_LOW,
                          f"La descripción no dice qué tipo de pieza es «{measure['raw']}»: revisa el nombre."))
        elif info["key"] == "pieza" and not info["characteristics"] and not info["groups"] and not brand \
                and known and not inherited:
            flags.append((CONFIDENCE_MEDIUM, "Solo dice «pieza»: precisa qué tipo de pieza es."))
        if measure.get("range"):
            flags.append((CONFIDENCE_MEDIUM,
                          "Las medidas venían como un rango («a»): se proponen solo las que se nombran; "
                          "agrega las intermedias si existen."))
        if measure["zero"]:
            flags.append((CONFIDENCE_LOW,
                          f"Revisar medida: «{measure['text']}» tiene una dimensión 0, que no existe; "
                          f"probablemente es «{measure['guess']}». Corrige el nombre y marca «{COL_CREATE}» "
                          "para incluirla."))
        if measure["oversize"]:
            flags.append((CONFIDENCE_LOW,
                          f"Revisar medida: «{measure['text']}» supera {MAX_DIMENSION} en un lado, algo que no "
                          f"existe en una pieza; confirma que está bien escrita. Corrige el nombre y marca "
                          f"«{COL_CREATE}» para incluirla."))
        if info["head_free"] and len(info["noun"]) < 2:
            flags.append((CONFIDENCE_LOW, "El nombre es demasiado corto o confuso: revisa la descripción."))
        vague = _vague_words(info["text"])
        if vague:
            flags.append((CONFIDENCE_MEDIUM, f"«{', '.join(vague)}» es un término genérico: precisa qué pieza es."))
        if info["fuzzy"]:
            flags.append((CONFIDENCE_MEDIUM, "Se corrigió una palabra por parecido: revisa el nombre."))
        name = _build_name(info, measure["text"], brand, noun_fallback)
        feature = _feature(info, measure["feature"], noun_fallback)
        entry = _entry(KIND_PIECE, name, feature, source, quantity["value"] if quantity else None, list(notes))
        self.add(entry, part, flags, tail)

    @staticmethod
    def note_fixes(info, flags):
        for original, fixed in info["fixes"]:
            flags.append((CONFIDENCE_MEDIUM if info["fuzzy"] else CONFIDENCE_HIGH,
                          f"Se interpretó «{original}» como «{fixed}»."))


# ---------------------------------------------------------------------------
# Prosa y «Titulo: explicacion»
# ---------------------------------------------------------------------------

# Una oracion de prosa no es una lista de productos: se descarta o solo se
# aprovechan sus colores y su medida global («todas las piezas son 2x1»).
_PROSE_FIRST = {"este", "esta", "estos", "estas", "ese", "esa", "esos", "esas", "esto", "eso", "asi", "cada"}
_CONTENT_VERBS = {"contiene", "contienen", "incluye", "incluyen", "almacena", "almacenan", "guarda", "guardan"}
_PROSE_FINITE = {
    "esta", "estan", "es", "son", "fue", "fueron", "muestra", "muestran", "consolida", "consolidan", "permite",
    "permiten", "facilita", "facilitan", "sirve", "sirven", "agrupa", "agrupan", "ofrece", "ofrecen", "presenta",
    "presentan", "compone", "componen", "destina", "destinan", "utiliza", "utilizan", "corresponde",
    "corresponden", "reune", "reunen", "representa", "representan", "equivale", "conforma", "conforman",
}
_COLOR_WORDS = {
    "azul", "verde", "rojo", "roja", "amarillo", "amarilla", "naranja", "morado", "morada", "violeta", "lila",
    "rosa", "rosado", "rosada", "magenta", "cafe", "marron", "gris", "negro", "negra", "blanco", "blanca", "beige",
    "turquesa", "cian", "celeste", "dorado", "dorada", "plateado", "plateada", "transparente", "translucido",
    "neon", "fucsia", "vinotinto", "crema", "salmon", "lima", "oliva", "mostaza", "guinda", "granate", "purpura",
    "anaranjado", "caqui", "arena", "multicolor", "aqua", "coral", "lavanda", "durazno", "terracota",
}
_COLOR_MODIFIERS = {"claro", "clara", "oscuro", "oscura", "brillante", "intenso", "pastel", "metalico", "metalica",
                    "fluorescente", "fosforescente", "profundo", "suave", "palido"}
_NUMBER_WORDS = {"un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
                 "nueve": 9, "diez": 10, "par": 2, "pareja": 2}
_COLOR_MARKER_RE = re.compile(r"\b(?:tonos|tonalidades|colores)\b(?:\s+como|\s+de|\s*:)?\s*:?\s*", re.IGNORECASE)
_EXTRA_COLOR_RE = re.compile(
    r"(?:junto\s+a|adem[aá]s\s+de|m[aá]s)\s+(?P<q>un\s+par|una\s+pareja|dos|tres|cuatro|cinco|seis|siete|ocho"
    r"|nueve|diez|\d+|una|un)\s+(?:de\s+)?(?:(?:piezas|bloques|unidades|fichas)\s+)?(?:(?:aisladas?|sueltas?|extras?)\s+)?"
    r"(?:en|de)\s+(?:color\s+|tono\s+)?(?P<color>[^\W\d_]+(?:\s+[^\W\d_]+){0,2})",
    re.IGNORECASE,
)
_GLOBAL_DIM_RE = re.compile(
    r"\b(?:todas|todos|cada)\b[^.;\n]{0,60}?\b(?:son|miden|mide|tienen|tiene|es|de|medida(?:\s+de)?)\s+(?:de\s+)?(?=\d)",
    re.IGNORECASE,
)
_GLOBAL_NOUN_RE = re.compile(r"\b(?:todas|todos)\s+(?:las|los)\s+(\w+)|\bcada\s+(\w+)", re.IGNORECASE)

# «Titulo: explicacion»: un producto por titulo; la explicacion solo aporta la caracteristica.
_RECORD_SPLIT = re.compile(
    r"\n+|(?<=[.!?])\s{2,}|(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡][^:.\n]{2,160}:\s+[A-ZÁÉÍÓÚÑ¿¡])")
_TITLED_RE = re.compile(r"^(?P<title>[^:.\n]{3,160}?)\s*:\s+(?P<expl>[A-ZÁÉÍÓÚÑ¿¡][^\n]{14,})$")
_QTY_PAREN_WORDS = {
    "multiples", "multiple", "varias", "varios", "muchas", "muchos", "algunas", "algunos", "diversas", "diversos",
    "unidades", "unidad", "und", "uds", "ud", "piezas", "pieza", "pzs", "cantidad", "aprox", "aproximadamente",
    "x", "total", "stock", "disponibles", "disponible", "de",
}
_QUANTIFIERS = {"varias", "varios", "unas", "unos", "algunas", "algunos", "diversas", "diversos", "distintas",
                "distintos", "multiples", "muchas", "muchos"}
_FEATURE_LINK_RE = re.compile(
    r"^(?P<head>[^\W\d_]+)\s+(?P<link>que\s+(?:contiene|incluye|guarda|almacena)|enfocado\s+en|exclusivo\s+para"
    r"|dedicado\s+a|destinado\s+a|especial\s+para|con|de)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
MAX_FEATURE_LENGTH = 70
MAX_NAME_WORDS = 8
MAX_NAME_CHARS = 60
MAX_TITLE_CHARS = 80


def _words(text: str) -> list:
    return fold(text).split()


def _starts_prose(text: str) -> bool:
    """La parte abre como prosa («Este estuche...», «De esta manera...»): no es un producto."""
    words = [w.strip(",;:()") for w in _words(_collapse(text))[:3]]
    if not words:
        return False
    first, second = words[0], words[1] if len(words) > 1 else ""
    return (first in _PROSE_FIRST
            or (first == "de" and second in ("este", "esta", "ese", "esa", "esto"))
            or (first == "por" and second in ("esto", "ello", "lo"))
            or (first in ("todas", "todos") and second in ("las", "los"))
            or (first == "en" and second in ("resumen", "general", "total", "conclusion"))
            or (first in ("los", "las", "lo") and second in ("cuales", "cual")))


def _has_finite_verb(text: str) -> bool:
    return any(w.strip(",;:().") in _PROSE_FINITE for w in _words(text))


def _color_phrase(tokens: list):
    """Color de 1 a 3 palabras («azul claro», «verde lima») o None."""
    if not 1 <= len(tokens) <= 3:
        return None
    keys = [fold(t) for t in tokens]
    if all(k in _COLOR_WORDS or k in _COLOR_MODIFIERS for k in keys) and any(k in _COLOR_WORDS for k in keys):
        return " ".join(t.lower() for t in tokens)
    return None


def _extract_colors(sentence: str) -> list:
    """Colores de una lista del tipo «en tonos como azul claro, magenta y lila»."""
    marker = _COLOR_MARKER_RE.search(sentence)
    if not marker:
        return []
    tail = re.split(r"[(.]", sentence[marker.end():], maxsplit=1)[0]
    colors = []
    for item in re.split(r",|;|\s+y\s+|\s+e\s+|\s+o\s+", tail):
        tokens = re.findall(r"[^\W\d_]+", item)
        color = _color_phrase(tokens)
        if color is None:
            break
        if color not in colors:
            colors.append(color)
    return colors if len(colors) >= 2 else []


def _extract_extra_colors(sentence: str) -> list:
    """«junto a un par de piezas aisladas en verde lima» -> [("verde lima", 2, texto)]."""
    found = []
    for match in _EXTRA_COLOR_RE.finditer(sentence):
        tokens = re.findall(r"[^\W\d_]+", match.group("color"))
        while tokens and _color_phrase(tokens) is None:
            tokens.pop()
        color = _color_phrase(tokens) if tokens else None
        if color is None:
            continue
        q = fold(match.group("q"))
        quantity = int(q) if q.isdigit() else _NUMBER_WORDS.get(q.split()[-1], 1)
        found.append((color, quantity, _collapse(match.group(0))))
    return found


def _extract_global_dimension(sentence: str):
    """(medida, sustantivo) de «todas las piezas son 2x1» o (None, None)."""
    match = _GLOBAL_DIM_RE.search(sentence)
    if not match:
        return None, None
    for measure in _find_measures(sentence[match.end():match.end() + 24]):
        if measure["family"] == "dim" and measure["start"] == 0:
            noun_match = _GLOBAL_NOUN_RE.search(sentence)
            word = (noun_match.group(1) or noun_match.group(2)) if noun_match else ""
            hit = _resolve(word) if word else None
            return measure, (hit[1] if hit and hit[0] == "noun" else None)
    return None, None


def _sentence_noun(sentence: str, before: int):
    """Ultimo sustantivo conocido de la oracion antes de la posicion `before`."""
    found = None
    for word in _WORD_RE.findall(sentence[:before]):
        hit = _resolve(word)
        if hit and hit[0] == "noun":
            found = hit[1]
    return found


def _split_sentences(text: str) -> list:
    sentences = []
    for line in re.split(r"\n+", text):
        sentences.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])", line) if s.strip())
    return sentences


def _is_container_subject(sentence: str) -> bool:
    """«El Contenedor 5 esta compuesto por ...»: habla del contenedor, no de un producto."""
    words = re.findall(r"[^\W\d_]+", sentence)[:3]
    for word in words:
        key = fold(word)
        if key in ("el", "este", "nuestro", "la"):
            continue
        return len(key) >= 8 and difflib.SequenceMatcher(None, key, _CONTAINER_WORD).ratio() >= 0.85
    return False


def _subject_then_verb(sentence: str) -> bool:
    """«El inventario muestra ...», «Esta caja es ...»: sujeto y verbo de prosa al inicio."""
    tokens = [fold(w) for w in re.findall(r"[^\W\d_]+", sentence)
              if fold(w) not in ("el", "la", "los", "las", "un", "una", "unos", "unas")][:3]
    return "que" not in tokens and any(t in _PROSE_FINITE for t in tokens)


def _after_prose_verb(sentence: str) -> str:
    """«El inventario muestra tornillos y tuercas» -> «tornillos y tuercas»;
    «El Contenedor 3 tiene una caja con puertas» -> «una caja con puertas»."""
    words = list(re.finditer(r"[^\W\d_]+", sentence))[:5]
    for match in words:
        key = fold(match.group(0))
        if key in _PROSE_FINITE or key in _STARTER_WORDS:
            return sentence[match.end():].strip(" ,:")
    return sentence


def _container_describes_a_box(sentence: str) -> bool:
    """«El contenedor tiene una gran caja con divisiones»: la caja es el propio
    contenedor, no un producto (si es un producto, el usuario la marca)."""
    if not _is_container_subject(sentence) or _find_measures(sentence):
        return False
    rest = _after_prose_verb(sentence)
    verb = next((fold(m.group(0)) for m in re.finditer(r"[^\W\d_]+", sentence)
                 if fold(m.group(0)) in _PROSE_FINITE or fold(m.group(0)) in _STARTER_WORDS), "")
    if rest == sentence or verb in _CONTENT_VERBS:
        return False
    return any((_resolve(w) or ("",))[0] == "box" for w in re.findall(r"[^\W\d_]+", rest)[:3])


def _analyze_sentence(sentence: str) -> dict:
    colors = _extract_colors(sentence)
    own_measures = [m for m in _find_measures(sentence) if m["family"] == "dim"]
    starts = _starts_prose(sentence)
    finite = _has_finite_verb(sentence)
    container = _is_container_subject(sentence) and len(sentence.split()) >= 10 and not own_measures
    marker = _COLOR_MARKER_RE.search(sentence)
    box_container = _container_describes_a_box(sentence)
    dim, dim_noun = _extract_global_dimension(sentence)
    return {
        "text": sentence, "colors": colors, "extra_colors": _extract_extra_colors(sentence) if colors else [],
        "own_dim": own_measures[0] if len(own_measures) == 1 else None, "global_dim": dim, "dim_noun": dim_noun,
        "noun": _sentence_noun(sentence, marker.start()) if marker else None,
        "marker": sentence[marker.start():].split("(")[0].strip(" .") if marker else "",
        "starts": starts, "prose": bool(colors) or starts or finite or container or box_container,
        "discard": starts or container,
    }


def _color_entries(info: dict, dim, noun_key, default_brand: str) -> list:
    """Un producto por color: «Pieza Lego 2x1 azul claro»."""
    noun_display, _ = _NOUNS.get(noun_key or "pieza", _NOUNS["pieza"])
    brand = default_brand if (noun_key or "pieza") in _PIECE_NOUNS and (dim is None or dim["unitless"]) else ""
    base = [noun_display] + ([brand] if brand else []) + ([dim["text"]] if dim else [])
    entries = []
    excerpt = _shorten(info["marker"], 100)
    todo = [(c, None, "") for c in info["colors"]] + list(info["extra_colors"])
    for color, quantity, mention in todo:
        flags, notes = [], []
        if dim is None:
            flags.append((CONFIDENCE_MEDIUM, "La descripción no dice la medida de estas piezas: complétala en el nombre."))
        if dim is not None and dim["zero"]:
            flags.append((CONFIDENCE_LOW, f"Revisar medida: «{dim['text']}» tiene una dimensión 0, que no existe; "
                                          f"probablemente es «{dim['guess']}»."))
        if dim is not None and dim["oversize"]:
            flags.append((CONFIDENCE_LOW, f"Revisar medida: «{dim['text']}» supera {MAX_DIMENSION} en un lado."))
        if mention:
            flags.append((CONFIDENCE_MEDIUM, f"Aparece como «{mention}»: son pocas piezas aparte; "
                                             "confirma la cantidad y el color."))
            notes.append(f"Cantidad tomada de la descripción ({quantity}).")
        name = _collapse(" ".join(base + [color]))
        entry = _entry(KIND_PIECE, name, (dim["text"] + " " if dim else "") + color, f"{color} — {excerpt}",
                       quantity, notes)
        entry["notes"] += [n for _, n in flags]
        entry["confidence"] = _level(flags)
        entry["selected"] = entry["confidence"] != CONFIDENCE_LOW
        entry["feature"] = entry["feature"][:1].upper() + entry["feature"][1:]
        entries.append(entry)
    return entries


def _cap_phrase(text: str, limit: int) -> str:
    """Acorta en un limite natural (coma, «y», espacio) sin partir palabras ni dejar conectores colgando."""
    text = _collapse(text)
    if len(text) > limit:
        cut = text[:limit]
        best = max(cut.rfind(", "), cut.rfind(" y "), cut.rfind(" o "))
        text = cut[:best] if best >= 20 else cut[:cut.rfind(" ")] if " " in cut else cut
    words = text.rstrip(" ,;:.").split()
    while words and fold(words[-1]) in _STOPWORDS:
        words.pop()
    return " ".join(words)


def _too_long(name: str) -> bool:
    words = len(name.split())
    return (words > MAX_NAME_WORDS and len(name) > MAX_NAME_CHARS) or len(name) > 80 or words > 14


def _short_name(name: str) -> str:
    words = name.split()[:MAX_NAME_WORDS]
    return _cap_phrase(" ".join(words), MAX_NAME_CHARS)


def _feature_from_explanation(expl: str) -> str:
    """Caracteristica corta a partir de la explicacion: «Paquete que contiene
    varias plataformas base de cimentacion para ensamblajes» -> «Paquete de
    plataformas base de cimentacion»."""
    text = _collapse(re.sub(r"\([^()]*\)", " ", expl)).rstrip(" .")
    text = re.sub(r"\s+([,;])", r"\1", text)
    match = _FEATURE_LINK_RE.match(text)
    if match:
        rest = match.group("rest")
        words = rest.split()
        while words and fold(words[0]) in _QUANTIFIERS:
            words.pop(0)
        rest = " ".join(words)
        if not fold(match.group("link")).endswith(("para", " a")):
            rest = re.split(r"\s+para\s+", rest, maxsplit=1)[0]
        text = f"{match.group('head')} de {rest}"
    else:
        text = " ".join(text.split()[:10])
    return _cap_phrase(text, MAX_FEATURE_LENGTH)


def _strip_quantity_parens(title: str):
    """(titulo sin parentesis de cantidad, cantidad o None, texto del parentesis o "")."""
    quantity, mention = None, ""

    def drop(match):
        nonlocal quantity, mention
        tokens = re.findall(r"[a-z]+|\d+", fold(match.group(1)))
        digits = [t for t in tokens if t.isdigit()]
        if tokens and all(t.isdigit() or t in _QTY_PAREN_WORDS for t in tokens):
            mention = _collapse(match.group(1))
            if digits:
                quantity = int(digits[0])
            return " "
        return match.group(0)

    cleaned = re.sub(r"\(([^()]*)\)", drop, title)
    return _collapse(cleaned), quantity, mention


def _balance_parentheses(text: str) -> str:
    """Quita los parentesis sueltos para que el nombre quede balanceado."""
    stack, loose = [], set()
    for index, ch in enumerate(text):
        if ch == "(":
            stack.append(index)
        elif ch == ")":
            if stack:
                stack.pop()
            else:
                loose.add(index)
    loose.update(stack)
    return _collapse("".join(" " if i in loose else ch for i, ch in enumerate(text)))


def _titled_entry(title: str, expl: str) -> dict:
    cleaned, quantity, mention = _strip_quantity_parens(title)
    cleaned = _balance_parentheses(cleaned).strip(" ,;:-")
    name = _sentence_case(cleaned)
    notes, flags = [], []
    if quantity is not None:
        notes.append(f"Cantidad tomada de la descripción ({quantity}).")
    elif mention:
        notes.append(f"La descripción dice «{mention}» sin una cantidad exacta: cuéntalas y escribe la cantidad.")
    if len(name) > MAX_TITLE_CHARS:
        flags.append((CONFIDENCE_LOW, f"El título es muy largo: se acortó; el original era «{name}»."))
        name = _cap_phrase(name, MAX_TITLE_CHARS)
        name = _balance_parentheses(name)
    kind = KIND_BOX if _is_box(cleaned) else KIND_PRODUCT
    if kind == KIND_BOX:
        notes.append("Caja física: en Cantidad registra las piezas que contiene (o 1 si cuentas la caja).")
    notes += [n for _, n in flags]
    entry = _entry(kind, name, _feature_from_explanation(expl), _shorten(f"{title.strip()}: {expl.strip()}", 240),
                   quantity, notes)
    entry["confidence"] = _level(flags)
    entry["selected"] = entry["confidence"] != CONFIDENCE_LOW
    entry["origins"] = [title, expl, f"{title}: {expl}"]
    return entry


def _titled_records(text: str):
    """Lista [("titulo", titulo, explicacion) | ("otro", texto)] si el texto esta en
    formato «Titulo: explicacion», o None."""
    records = [r.strip() for r in _RECORD_SPLIT.split(text) if r and r.strip()]
    result, titled = [], 0
    for record in records:
        match = _TITLED_RE.match(record)
        if match:
            title, expl = match.group("title").strip(), match.group("expl").strip()
            first = fold(_first_word(title))
            if (len(title.split()) <= 14 and len(expl.split()) >= 6 and first not in _STARTER_WORDS
                    and not _starts_prose(title) and not _has_finite_verb(title)
                    and not _measure_only(title)):
                result.append(("titulo", title, expl))
                titled += 1
                continue
        result.append(("otro", record))
    known = titled == 1 and len(records) <= 3 and any(
        r[0] == "titulo" and _first_kind(r[1]) in ("noun", "box") and not _find_measures(r[2]) for r in result)
    return result if titled >= 2 or known else None


def _prose_like(text: str) -> bool:
    return (len(text.split()) >= 8 or text.rstrip().endswith(":") or _starts_prose(text) or _has_finite_verb(text))


def _run_plain(text: str, default_brand: str):
    parser = _Parser(default_brand)
    prepared, remarks, notices = _prepare(text)
    parser.run(_split_parts(prepared, remarks))
    return parser.entries, parser.unparsed, notices


def _interpret_plain(text: str, default_brand: str):
    """Listas de productos y prosa: lo que es prosa se descarta o solo aporta colores y la medida global."""
    sentences = _split_sentences(text)
    analyzed = [_analyze_sentence(s) for s in sentences]
    if not any(a["prose"] for a in analyzed):
        return _run_plain(text, default_brand)
    global_dim = next((a["global_dim"] for a in analyzed if a["global_dim"]), None)
    global_noun = next((a["dim_noun"] for a in analyzed if a["dim_noun"]), None)
    entries, unparsed, notices, buffer = [], [], {"filler": False, "truncated": False}, []

    def flush():
        if buffer:
            found, bad, seen = _run_plain("\n".join(buffer), default_brand)
            entries.extend(found)
            unparsed.extend(bad)
            notices["filler"] = notices["filler"] or seen["filler"]
            buffer.clear()

    for info in analyzed:
        if not info["prose"]:
            buffer.append(info["text"])
            continue
        flush()
        if info["colors"]:
            dim = info["own_dim"] or global_dim
            noun = info["noun"] or global_noun
            entries.extend(_color_entries(info, dim, noun, default_brand))
        elif info["discard"] or info["global_dim"]:
            continue                       # describe el contenedor o solo da la medida global
        else:
            found, bad, _ = _run_plain(_after_prose_verb(info["text"]), default_brand)
            for entry in found:            # una oracion ambigua nunca se marca sola
                entry["notes"].append("Viene de una oración que parece descripción, no de una lista de productos: "
                                      "revisa si es un producto.")
                entry["confidence"], entry["selected"] = CONFIDENCE_LOW, False
            entries.extend(found)
            unparsed.extend(bad)
    flush()
    return entries, unparsed, notices


def _interpret(raw: str, default_brand: str):
    text = unicodedata.normalize("NFKC", raw).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[   \t]", " ", text)
    records = _titled_records(text)
    if records is None:
        return _interpret_plain(text, default_brand)
    entries, unparsed, notices, buffer = [], [], {"filler": False, "truncated": False}, []

    def flush():
        if buffer:
            found, bad, seen = _interpret_plain("\n".join(buffer), default_brand)
            entries.extend(found)
            unparsed.extend(bad)
            notices["filler"] = notices["filler"] or seen["filler"]
            buffer.clear()

    for record in records:
        if record[0] == "titulo":
            flush()
            entries.append(_titled_entry(record[1], record[2]))
        elif not _prose_like(record[1]):
            buffer.append(record[1])        # lo demas (introduccion, prosa) no es un producto
    flush()
    return entries, unparsed, notices


def parse_description(text, context: str = "") -> dict:
    """Interpreta la descripcion de un contenedor.

    Devuelve {"entries": [...], "warnings": [...], "unparsed": [...]} donde cada
    entrada es {"kind", "name", "feature", "source", "quantity", "notes",
    "selected", "confidence", "inside"}. `quantity` es None salvo que el texto la
    diga ("20 piezas ..."); `confidence` es "alta", "media" o "baja" (las de
    confianza baja llegan con selected=False); `inside` es el nombre de la caja
    en la que el texto dice que esta. `context` (categoria y nombre del
    contenedor) solo aporta la marca. Nunca lanza excepciones."""
    try:
        raw = "" if _is_missing(text) else str(text)
        default_brand = _context_brand(raw, context)
        truncated = len(raw) > MAX_TEXT_LENGTH
        entries, unparsed, notices = _interpret(raw[:MAX_TEXT_LENGTH], default_brand)
        notices["truncated"] = truncated
    except Exception:  # noqa: BLE001
        fragment = _shorten(text)
        return {"entries": [], "warnings": ["No se pudo interpretar la descripción: revísala."],
                "unparsed": [fragment] if fragment else []}
    warnings = []
    if notices["truncated"]:
        warnings.append(f"La descripción es muy larga: solo se analizaron los primeros {MAX_TEXT_LENGTH} caracteres.")
    if notices["filler"]:
        warnings.append("La descripción termina con «etc.» o similar: puede haber productos sin nombrar; "
                        "revisa si falta alguno.")

    # Un mismo producto descrito dos veces se propone una sola vez.
    unique, seen = [], {}
    for entry in entries:
        key = product_key(entry["name"])
        if key and key in seen:
            first = seen[key]
            same = "aparece más de una vez" if first["name"] == entry["name"] \
                else f"es la misma pieza que «{first['name']}»"
            message = f"«{entry['name']}» {same} en la descripción: se propone una sola vez."
            if entry["quantity"] is not None:
                first["quantity"] = (first["quantity"] or 0) + entry["quantity"]
                message = message[:-1] + f", sumando las cantidades ({first['quantity']})."
            warnings.append(message)
            continue
        seen[key] = entry
        unique.append(entry)
    unparsed = [_shorten(fragment) for fragment in unparsed]
    for fragment in unparsed[:MAX_UNPARSED_SHOWN]:
        warnings.append(f"No se pudo interpretar «{fragment}»: revisa la descripción.")
    if len(unparsed) > MAX_UNPARSED_SHOWN:
        warnings.append(f"Hay {len(unparsed) - MAX_UNPARSED_SHOWN} fragmentos más que no se pudieron interpretar.")
    if len(unique) > MAX_ENTRIES:
        warnings.append(f"La descripción nombra más de {MAX_ENTRIES} productos: se proponen los primeros {MAX_ENTRIES}.")
        unique = unique[:MAX_ENTRIES]
    return {"entries": unique, "warnings": warnings, "unparsed": unparsed}


# ---------------------------------------------------------------------------
# Codigos y ubicacion
# ---------------------------------------------------------------------------

def _safe_parse(code):
    try:
        return barcode.parse_code(str(code or "").strip())
    except ValueError:
        return None


def _standard(code):
    parsed = _safe_parse(code)
    return parsed if parsed and parsed["format"] == barcode.FORMAT_STANDARD else None


def next_child_codes(parent_code: str, used_ids, count: int):
    """`count` codigos libres y consecutivos para productos dentro del contenedor
    `parent_code`. Devuelve (codigos, nota); un codigo vacio significa que no se
    puede proponer uno y la nota explica por que.

    - Contenedor GLIOPS (2-1-01-00-000): nivel caja 2-1-01-NN-000, con NN
      despues de la caja mas alta ya usada en ese contenedor.
    - Caja GLIOPS (2-1-01-03-000): nivel item 2-1-01-03-NNN.
    - Codigo alfanumerico libre (CONT-01): CONT-01-NN si cabe en la etiqueta.
    """
    parent_code = str(parent_code or "").strip()
    used = {str(code or "").strip() for code in used_ids or ()}
    used.add(parent_code)
    if count <= 0:
        return [], ""
    parsed = _safe_parse(parent_code)
    manual = "Escribe el código de este producto: el contenedor no tiene un código del que derivarlo."

    if parsed and parsed["format"] == barcode.FORMAT_STANDARD:
        level = barcode.standard_code_level(parsed)
        if level == barcode.LEVEL_ITEM:
            return [""] * count, (
                "Escribe el código de este producto: el contenedor tiene un código de nivel ítem."
            )
        same_place = []
        for code in used:
            other = _standard(code)
            if other and all(other[k] == parsed[k] for k in ("estanteria", "piso", "contenedor")):
                same_place.append(other)
        place = (parsed["estanteria"], parsed["piso"], parsed["contenedor"])
        if level == barcode.LEVEL_CONTAINER:
            number = max((o["caja"] for o in same_place), default=0) + 1
        else:
            number = max((o["item"] for o in same_place if o["caja"] == parsed["caja"]), default=0) + 1
        codes = []
        while len(codes) < count:
            if level == barcode.LEVEL_CONTAINER:
                code = barcode.build_standard_code(*place, number, 0)
            else:
                code = barcode.build_standard_code(*place, parsed["caja"], number)
            if code not in used:
                codes.append(code)
                used.add(code)
            number += 1
        return codes, ""

    if parsed and parsed["format"] == barcode.FORMAT_ALNUM:
        codes, number = [], 1
        while len(codes) < count:
            code = f"{parent_code}-{number:02d}"
            if barcode.detect_format(code) != barcode.FORMAT_ALNUM:
                break
            if code not in used:
                codes.append(code)
                used.add(code)
            number += 1
        if len(codes) == count:
            return codes, ""
        return codes + [""] * (count - len(codes)), (
            "Escribe el código de este producto: el del contenedor es demasiado largo para derivarlo."
        )
    return [""] * count, manual


def child_location(container: dict, code: str) -> str:
    """Ubicacion de un producto dentro del contenedor: la del contenedor mas la
    caja (o item) que indica su codigo, con el mismo estilo de separadores.
    "Estantería- 2; Piso-1; Contenedor-1." + 2-1-01-04-000 ->
    "Estantería- 2; Piso-1; Contenedor-1; Caja 04."."""
    container = container or {}
    base = _collapse(container.get("location"))
    parent = _standard(container.get("id"))
    child = _standard(code)
    if not base and parent:
        base = f"Estantería {parent['estanteria']}; Piso {parent['piso']}; Contenedor {parent['contenedor']:02d}"
        if parent["caja"]:
            base += f"; Caja {parent['caja']:02d}"
    if not child:
        return base
    extra = []
    if child["caja"] and (not parent or parent["caja"] != child["caja"]):
        extra.append(f"Caja {child['caja']:02d}")
    if child["item"]:
        extra.append(f"Ítem {child['item']:03d}")
    if not extra:
        return base
    if not base:
        return "; ".join(extra)
    trailing_dot = base.endswith(".")
    core = base.rstrip(" .;,")
    separator = "; " if ";" in core else (", " if "," in core else " · ")
    return core + separator + separator.join(extra) + ("." if trailing_dot else "")


# ---------------------------------------------------------------------------
# Propuestas para un contenedor
# ---------------------------------------------------------------------------

def _active(item: dict) -> bool:
    return (item or {}).get("status") != "retired"


def container_children(container_id: str, items) -> list:
    container_id = str(container_id or "").strip()
    return [i for i in items or () if _active(i) and str(i.get("parent_id") or "").strip() == container_id]


# ---------------------------------------------------------------------------
# Productos que el contenedor ya tiene
# ---------------------------------------------------------------------------

_FRAGMENT_RE = re.compile(r"Creado desde la descripción de «.*?» \([^)]*\):\s*«(.*?)»\.(?:\s|$)", re.DOTALL)
SIMILARITY_THRESHOLD = 0.94


def _plain_text(text) -> str:
    """Texto comparable: sin tildes, mayusculas ni signos."""
    return _collapse(re.sub(r"[\W_]+", " ", fold(text)))


def _figures(text: str) -> list:
    """Palabras con cifras (2x2, 220, M3): dos nombres con cifras distintas son productos distintos."""
    return sorted(t for t in text.split() if any(ch.isdigit() for ch in t))


def child_source_fragment(child: dict) -> str:
    """Fragmento de la descripcion en que se creo un producto (lo guarda
    build_payload en su descripcion) o ""."""
    match = _FRAGMENT_RE.search(str((child or {}).get("description") or ""))
    return match.group(1) if match else ""


def names_match(first, second) -> bool:
    """Dos nombres son el mismo producto: igual clave (plural, tildes, orden de la
    medida), igual sin el parentesis de cantidad («(multiples unidades)»), o casi
    iguales ignorando puntuacion, siempre con las mismas cifras (2x2 no es 2x4)."""
    forms_a = {str(first or ""), _strip_quantity_parens(str(first or ""))[0]}
    forms_b = {str(second or ""), _strip_quantity_parens(str(second or ""))[0]}
    for a in forms_a:
        for b in forms_b:
            key = product_key(a)
            if key and key == product_key(b):
                return True
    a = _plain_text(_strip_quantity_parens(str(first or ""))[0])
    b = _plain_text(_strip_quantity_parens(str(second or ""))[0])
    return (bool(a) and bool(b) and _figures(a) == _figures(b)
            and difflib.SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD)


def find_existing_child(entry: dict, children) -> dict:
    """El hijo del contenedor que ya es esta propuesta (por nombre o por el
    fragmento de la descripcion en que se creo), o None."""
    origins = [_plain_text(o) for o in (entry.get("origins") or [entry.get("source")]) if o]
    for child in children:
        if names_match(entry.get("name"), child.get("name")):
            return child
        fragment = _plain_text(child_source_fragment(child))
        if len(fragment) < 8:
            continue
        for origin in origins:
            if fragment == origin or (min(len(fragment), len(origin)) >= 15 and (fragment in origin or origin in fragment)):
                return child
    return None


def suggest_products(container: dict, items=()) -> dict:
    """Propuestas de Contenedores de Caracteristica para `container` a partir de
    su descripcion, sin repetir los productos que ya tiene.

    `items` debe incluir TODOS los items (tambien los dados de baja) para no
    proponer un codigo ya registrado. Devuelve:
    {"container_id", "container_name", "proposals": [...], "existing": [...],
     "warnings": [...], "unparsed": [...]}. Cada propuesta trae: kind, name,
    feature, source, notes, selected (False si la confianza es baja), confidence
    ("alta", "media" o "baja"), inside (caja en la que el texto dice que esta),
    code, parent_id, item_type ("child"), category, location, unit,
    quantity (0 si el texto no la dice), min_stock_alert y pending_count."""
    container = container or {}
    items = list(items or ())
    container_id = str(container.get("id") or "").strip()
    parsed = parse_description(
        container.get("description"), context=f"{container.get('category') or ''} {container.get('name') or ''}"
    )
    children = container_children(container_id, items)

    proposals, existing = [], []
    for entry in parsed["entries"]:
        match = find_existing_child(entry, children)
        if match is not None:
            existing.append({"name": entry["name"], "id": match.get("id"),
                             "existing_name": match.get("name"), "source": entry["source"]})
            continue
        proposals.append(entry)

    used_ids = {str(i.get("id") or "").strip() for i in items}
    codes, code_note = next_child_codes(container_id, used_ids, len(proposals))
    for proposal, code in zip(proposals, codes):
        quantity = proposal["quantity"] if proposal["quantity"] is not None else 0
        proposal.update(
            code=code, parent_id=container_id, item_type="child",
            category=container.get("category") or "", location=child_location(container, code),
            unit=DEFAULT_UNIT, quantity=quantity, min_stock_alert=0, pending_count=quantity == 0,
        )
        if not code and code_note:
            proposal["notes"].append(code_note)
    return {
        "container_id": container_id,
        "container_name": container.get("name") or "",
        "proposals": proposals,
        "existing": existing,
        "warnings": parsed["warnings"],
        "unparsed": parsed["unparsed"],
    }


def editor_records(proposals) -> list:
    """Filas de la vista previa editable (st.data_editor), en el orden de las
    propuestas."""
    return [
        {
            COL_CREATE: bool(p.get("selected", True)),
            COL_CODE: p.get("code") or "",
            COL_NAME: p.get("name") or "",
            COL_FEATURE: p.get("feature") or "",
            COL_QUANTITY: int(p.get("quantity") or 0),
            COL_UNIT: p.get("unit") or DEFAULT_UNIT,
            COL_NOTES: " ".join(p.get("notes") or []),
        }
        for p in proposals or ()
    ]


def rows_from_records(records, proposals) -> list:
    """Une lo editado por el usuario con los datos de origen de cada propuesta
    (fragmento de la descripcion, notas). Las filas se emparejan por posicion:
    la vista previa no permite agregar ni borrar filas."""
    rows = []
    proposals = list(proposals or ())
    for index, record in enumerate(records or ()):
        proposal = proposals[index] if index < len(proposals) else {}
        rows.append({
            "selected": record.get(COL_CREATE, False),
            "code": record.get(COL_CODE, ""),
            "name": record.get(COL_NAME, ""),
            "quantity": record.get(COL_QUANTITY, 0),
            "unit": record.get(COL_UNIT, DEFAULT_UNIT),
            "source": proposal.get("source", ""),
            "feature": proposal.get("feature", ""),
        })
    return rows


def _is_missing(value) -> bool:
    """None, NaN, pd.NA/NaT o texto vacio (celda borrada en la vista previa)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value) in ("<NA>", "NaT", "nan")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "si", "sí", "x")
    if _is_missing(value):
        return False
    try:
        return bool(value)
    except (TypeError, ValueError):
        return False


def _as_quantity(value):
    """(cantidad, error). Vacio cuenta como 0 (pendiente de conteo)."""
    if _is_missing(value):
        return 0, None
    if isinstance(value, bool):
        return None, "la cantidad debe ser un número entero"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "la cantidad debe ser un número entero"
    if math.isnan(number):
        return 0, None
    if not number.is_integer():
        return None, "la cantidad debe ser un número entero"
    if number < 0:
        return None, "la cantidad no puede ser negativa"
    return int(number), None


def _hierarchy_error(code: str, container_id: str):
    """El codigo GLIOPS de un producto debe quedar DENTRO del contenedor."""
    parent = _standard(container_id)
    child = _standard(code)
    if not parent:
        return None
    place = f"{parent['estanteria']}-{parent['piso']}-{parent['contenedor']:02d}"
    if parent["caja"] == 0:
        expected = f"{place}-NN-000"
        ok = child and child["caja"] > 0 and child["item"] == 0
    elif parent["item"] == 0:
        expected = f"{place}-{parent['caja']:02d}-NNN"
        ok = child and child["caja"] == parent["caja"] and child["item"] > 0
    else:
        return None
    same_place = child and all(child[k] == parent[k] for k in ("estanteria", "piso", "contenedor"))
    if ok and same_place:
        return None
    return f"el código {code} no queda dentro del contenedor {container_id} (usa {expected})"


def build_payload(row: dict, container: dict) -> dict:
    """Datos para LabStorage.save_items_bulk de una fila ya validada."""
    container = container or {}
    container_id = str(container.get("id") or "").strip()
    code = str(row.get("code") or "").strip()
    quantity = int(row.get("quantity") or 0)
    source = _collapse(row.get("source"))
    description = f"Creado desde la descripción de «{container.get('name') or container_id}» ({container_id})"
    description += f": «{source}»." if source else "."
    if quantity == 0:
        description += f" {PENDING_COUNT_NOTE}"
    return {
        "id": code,
        "name": _collapse(row.get("name")),
        "category": container.get("category") or "",
        "description": description,
        "item_type": "child",
        "parent_id": container_id,
        "unit": _collapse(row.get("unit")) or DEFAULT_UNIT,
        "quantity": quantity,
        "location": child_location(container, code),
        "min_stock_alert": 0,
        "status": "active",
    }


def validate_rows(rows, container: dict, items=()) -> dict:
    """Valida las filas marcadas para crear con las reglas del inventario
    (formato GLIOPS, codigo libre, jerarquia dentro del contenedor, nombre y
    cantidad) y sin repetir productos del contenedor ni de la propia tanda.

    Devuelve {"payloads": [...], "errors": [...], "warnings": [...],
    "selected": n}. Solo hay payloads utiles si `errors` esta vacio."""
    container = container or {}
    container_id = str(container.get("id") or "").strip()
    items = list(items or ())
    active_by_id = {str(i.get("id") or "").strip(): i for i in items if _active(i)}
    child_by_key = {}
    for child in container_children(container_id, items):
        child_by_key.setdefault(product_key(child.get("name")), child)

    payloads, errors, warnings = [], [], []
    seen_codes, seen_keys = {}, {}
    selected = 0
    for position, row in enumerate(rows or (), start=1):
        if not _truthy(row.get("selected")):
            continue
        selected += 1
        name = _collapse(row.get("name"))
        code = str(row.get("code") or "").strip()
        label = f"Fila {position}" + (f" ({name})" if name else "")
        problems = []

        if not name:
            problems.append("el nombre es obligatorio")
        if not code:
            problems.append("el código es obligatorio")
        else:
            try:
                barcode.validate_code_format(code)
            except ValueError as exc:
                problems.append(str(exc))
            else:
                hierarchy = _hierarchy_error(code, container_id)
                if hierarchy:
                    problems.append(hierarchy)
            if code == container_id:
                problems.append("no puede usar el mismo código del contenedor")
            elif code in seen_codes:
                problems.append(f"el código {code} está repetido (fila {seen_codes[code]})")
            elif code in active_by_id:
                problems.append(f"el código {code} ya pertenece a «{active_by_id[code].get('name')}»")
            seen_codes.setdefault(code, position)

        quantity, quantity_error = _as_quantity(row.get("quantity"))
        if quantity_error:
            problems.append(quantity_error)

        if name:
            key = product_key(name)
            if key and key in child_by_key:
                existing = child_by_key[key]
                problems.append(f"ya existe «{existing.get('name')}» en este contenedor ({existing.get('id')})")
            elif key and key in seen_keys:
                problems.append(f"repite el producto de la fila {seen_keys[key]}")
            seen_keys.setdefault(key, position)
            for measure in zero_measures(name):
                warnings.append(
                    f"{label}: la medida «{measure['raw']}» tiene una dimensión 0; "
                    f"¿quisiste decir «{measure['guess']}»?"
                )
            for measure in oversize_measures(name):
                warnings.append(
                    f"{label}: la medida «{measure['raw']}» supera {MAX_DIMENSION} en un lado; "
                    "¿está bien escrita?"
                )

        if problems:
            errors.append(f"{label}: " + "; ".join(problem.rstrip(".") for problem in problems) + ".")
            continue
        payloads.append(build_payload({**row, "name": name, "code": code, "quantity": quantity}, container))
    return {"payloads": payloads, "errors": errors, "warnings": warnings, "selected": selected}


def is_pending_count(item: dict) -> bool:
    """Producto creado sin cantidad (pendiente de conteo) que aun no se ha contado."""
    item = item or {}
    try:
        quantity = int(float(item.get("quantity") or 0))
    except (TypeError, ValueError):
        quantity = 0
    return (item.get("item_type") != "master" and quantity == 0
            and PENDING_COUNT_NOTE in str(item.get("description") or ""))
