# -*- coding: utf-8 -*-
"""Pruebas de core/labels.py: etiqueta profesional (por defecto 50x25 mm a
203 dpi, la SAT TT 460) con logo, identificacion del producto, Code 128 y
codigo legible. El codigo de barras se lee desde los pixeles, como lo haria un
lector, para comprobar que devuelve EXACTAMENTE el texto registrado. El diseño
adaptable y los demas tamaños se prueban en test_label_layout.py.
"""

import io
import itertools
import random
import re
import string

import pytest
from barcode.charsets import code128 as c128
from PIL import Image

from core import barcode, labels

STOP_PATTERN = "1100011101011"  # parada completa segun ISO/IEC 15417 (anchos 2331112)
PATTERN_TO_VALUE = {pattern: value for value, pattern in enumerate(c128.CODES)}

LONG_DESCRIPTION = "Microscopio óptico binocular Olympus CX23 con cámara y 4 objetivos"
PRODUCT = {
    "id": "LAB-MIC-01",
    "name": "Microscopio óptico binocular",
    "category": "Óptica",
    "location": "Estantería 2 · Piso 1",
    "item_type": "standalone",
}

DECODE_CASES = [
    "1-2-05-12-001", "M1-E2", "E3-LM07", "3-6-999-999-9999",            # GLIOPS V3
    "0012345", "7", "42", "9912345", "12345678901234567890", "1234567890123456789",
    "LAB-MIC-01", "lab-mic-01", "CAJA-001", "SENSOR_T.02", "99-LAB",
    "X1234Y", "AB12345CD", "ABCDEFGHIJKLM", "A1B2C3D4E5F6G",
]


# --- Lector Code 128 minimo, independiente del codificador de la app ---------

def _values_to_text(symbols):
    start, *data = symbols
    charset = {103: "A", 104: "B", 105: "C"}[start]
    out = []
    for value in data:
        if charset == "C":
            if value < 100:
                out.append(f"{value:02d}")
            elif value == 100:
                charset = "B"
            elif value == 101:
                charset = "A"
            else:
                raise AssertionError(f"valor inesperado en el subconjunto C: {value}")
        elif charset == "B":
            if value < 95:
                out.append(chr(value + 32))
            elif value == 99:
                charset = "C"
            elif value == 101:
                charset = "A"
            else:
                raise AssertionError(f"valor inesperado en el subconjunto B: {value}")
        else:
            if value < 64:
                out.append(chr(value + 32))
            elif value < 96:
                out.append(chr(value - 64))
            elif value == 99:
                charset = "C"
            elif value == 100:
                charset = "B"
            else:
                raise AssertionError(f"valor inesperado en el subconjunto A: {value}")
    return "".join(out)


def _decode_modules(modules):
    """Texto de un patron de modulos Code 128 (de la barra de inicio a la de parada)."""
    assert modules.endswith(STOP_PATTERN), "falta el patron de parada"
    body = modules[: -len(STOP_PATTERN)]
    assert body and len(body) % 11 == 0, "simbolos incompletos"
    values = [PATTERN_TO_VALUE[body[i:i + 11]] for i in range(0, len(body), 11)]
    *symbols, checksum = values
    expected = (symbols[0] + sum(pos * v for pos, v in enumerate(symbols[1:], start=1))) % 103
    assert checksum == expected, "checksum invalido"
    return _values_to_text(symbols)


def _scan_label(img):
    """Lee la etiqueta como un lector: toma la franja de barras (la racha mas
    alta de filas de pixeles identicas y con tinta), la recorre de izquierda a
    derecha y decodifica. Devuelve el texto y la geometria medida."""
    gray = img.convert("L")
    width, height = gray.size
    pix = gray.load()
    rows = [tuple(pix[x, y] < 128 for x in range(width)) for y in range(height)]

    band_top, band_height, y = 0, 0, 0
    while y < height:
        if not any(rows[y]):
            y += 1
            continue
        start = y
        while y < height and rows[y] == rows[start]:
            y += 1
        if y - start > band_height:
            band_top, band_height = start, y - start

    row = rows[band_top]
    first = row.index(True)
    last = width - 1 - row[::-1].index(True)
    runs = [len(list(group)) for _, group in itertools.groupby(row[first:last + 1])]
    dots = min(runs)
    assert all(run % dots == 0 for run in runs), "barras con un ancho que no es un numero entero de modulos"
    modules = "".join(("1" if i % 2 == 0 else "0") * (run // dots) for i, run in enumerate(runs))
    return {
        "text": _decode_modules(modules), "dots": dots,
        "quiet_left": first, "quiet_right": width - 1 - last,
        "band_top": band_top, "band_height": band_height, "rows": rows,
    }


def _text_lines(rows, y0, y1):
    """Cantidad de lineas de texto (grupos de filas con tinta) entre y0 y y1."""
    lines, inside = 0, False
    for y in range(y0, y1):
        has_ink = any(rows[y])
        if has_ink and not inside:
            lines += 1
        inside = has_ink
    return lines


def _ink_x_bounds(rows, y0, y1):
    xs = [x for y in range(y0, y1) for x, black in enumerate(rows[y]) if black]
    return min(xs), max(xs)


def _region_ink(rows, x0, y0, x1, y1):
    return sum(rows[y][x] for y in range(y0, y1) for x in range(x0, x1))

# --- Pruebas -------------------------------------------------------------------

def test_code128_table_matches_iso_standard():
    """El lector usa la tabla de patrones de python-barcode; se ancla aqui a
    valores del estandar para no depender de ella a ciegas."""
    assert c128.CODES[0] == "11011001100"      # 212222
    assert c128.CODES[103] == "11010000100"    # START A, 211412
    assert c128.CODES[104] == "11010010000"    # START B, 211214
    assert c128.CODES[105] == "11010011100"    # START C, 211232
    assert c128.STOP + "11" == STOP_PATTERN


@pytest.mark.parametrize(
    "code", ["1-2-05-12-001", "M1-E2", "E3-LM07", "3-6-999-999-9999", "0012345", "LAB-MIC-01", "CAJA-001"]
)
def test_generate_label_image_has_fixed_canvas_size(code):
    img = labels.generate_label_image(code)
    assert img.size == labels.LABEL_CANVAS_SIZE
    assert img.mode == "1"  # blanco y negro puro, ideal para termica


def test_generate_label_image_custom_canvas_size():
    img = labels.generate_label_image("M1-E2", canvas_size=(300, 150))
    assert img.size == (300, 150)
    small = labels.generate_label_image("LAB-MIC-01", canvas_size=(300, 150), description=LONG_DESCRIPTION)
    assert _scan_label(small)["text"] == "LAB-MIC-01"


@pytest.mark.parametrize("code", ["LAB MIC 01", "LAB-MÍC-01", "M3-E1", "ABCDEFGHIJKLMN", ""])
def test_generate_label_image_rejects_invalid_code(code):
    with pytest.raises(ValueError):
        labels.generate_label_image(code)


@pytest.mark.parametrize("code", DECODE_CASES)
def test_label_barcode_scans_back_to_the_exact_code(code):
    scan = _scan_label(labels.generate_label_image(code, description=LONG_DESCRIPTION))
    assert scan["text"] == code
    assert scan["quiet_left"] >= 6 * scan["dots"]
    assert scan["quiet_right"] >= 6 * scan["dots"]


@pytest.mark.parametrize(
    "code",
    ["1-2-05-12-001", "0012345", "LAB-MIC-01", "ABCDEFGHIJKLM", "A1B2C3D4E5F6G",
     "12345678901234567890", "1234567890123456789"],
)
def test_label_bars_are_at_least_025mm_wide_and_7mm_tall(code):
    """7 mm supera la recomendacion general para Code 128 (>= 6,35 mm o el 15 %
    del ancho del simbolo). Antes se exigian 9 mm en una etiqueta de 25 mm y el
    texto quedaba apretado en los 16 mm restantes."""
    scan = _scan_label(labels.generate_label_image(code, description=LONG_DESCRIPTION))
    assert scan["dots"] >= 2          # 2 puntos a 203 dpi = 0,25 mm
    assert scan["band_height"] >= 56  # 7 mm a 203 dpi


def _random_free_codes(count=400, seed=20260928):
    rng = random.Random(seed)
    codes = set()
    while len(codes) < count:
        if rng.random() < 0.3:
            code = "".join(rng.choice(string.digits) for _ in range(rng.randint(1, barcode.NUMERIC_MAX_LEN)))
        else:
            parts = []
            for _ in range(rng.randint(1, 4)):
                pool = string.digits if rng.random() < 0.5 else string.ascii_letters
                parts.append("".join(rng.choice(pool) for _ in range(rng.randint(1, 7))))
                parts.append(rng.choice(["", "", "-", "_", "."]))
            code = "".join(parts)[: barcode.ALNUM_MAX_LEN].strip("-_.")
        if barcode.detect_format(code) in (barcode.FORMAT_NUMERIC, barcode.FORMAT_ALNUM):
            codes.add(code)
    return sorted(codes)


def test_random_free_codes_encode_exactly_and_fit_with_wide_bars():
    width = labels.LABEL_CANVAS_SIZE[0]
    for code in _random_free_codes():
        modules = labels._code128_modules(code)
        assert _decode_modules(modules) == code, code
        assert labels._module_dots(len(modules), width) >= 2, code


def test_professional_item_label_has_logo_header_product_metadata_and_divider():
    layout = labels.layout_item_label(PRODUCT)
    img = labels.render_label(layout)
    scan = _scan_label(img)
    rows = scan["rows"]

    assert scan["text"] == PRODUCT["id"]
    assert scan["band_height"] >= 56  # 7 mm a 203 dpi aun con encabezado y datos
    assert _text_lines(rows, scan["band_top"] + scan["band_height"], img.size[1]) == 1
    assert labels._load_brand_logo() is not None
    assert _region_ink(rows, *layout.logo_box) > 50
    lab = layout.lines_for("lab")[0]
    assert lab.text == labels.LABEL_INSTITUTION_TEXT  # completo, nunca recortado
    x0, y0, x1, y1 = lab.box
    assert _region_ink(rows, x0, y0, min(x1, img.width), y1) > 50
    dx0, dy0, dx1, _ = layout.divider_box
    assert sum(rows[dy0]) >= (dx1 - dx0) - 2  # linea divisoria completa
    bx0, by0, bx1, _ = layout.band_box
    assert sum(rows[by0]) >= (bx1 - bx0) - 2  # banda negra del nombre
    assert layout.lines_for("name")[0].text == PRODUCT["name"]
    assert "UBIC: Estantería 2 · Piso 1" in " ".join(line.text for line in layout.lines_for("meta"))


def test_brand_logo_asset_is_versioned_and_converts_to_monochrome():
    assert labels.LABEL_LOGO_PATH.is_file()
    logo = labels._load_brand_logo()
    assert logo is not None
    assert logo.mode == "L"
    assert set(logo.getdata()) <= {0, 255}
    assert 0 in logo.getdata()


def test_thermal_typography_keeps_the_classic_sizes_and_spacing():
    """Formato clasico: titulo del laboratorio espaciado, nombre grande en la
    banda y codigo legible mas grande; ningun texto baja de 9 puntos (~3,2 pt)."""
    layout = labels.layout_item_label(PRODUCT)
    dpi = labels.LABEL_DPI
    assert layout.min_text_pt >= 9 * 72 / dpi - 0.01
    lab = layout.lines_for("lab")[0]
    assert round(14 * layout.scale) - 2 <= lab.size <= round(14 * layout.scale)
    assert lab.tracking == round(2 * layout.scale)  # el titulo conserva su espaciado
    assert layout.lines_for("name")[0].pt(dpi) >= 5.0
    assert layout.lines_for("code")[0].pt(dpi) >= 9.0
    assert all(line.tracking == 0 for line in layout.lines_for("meta") + layout.lines_for("code"))


@pytest.mark.parametrize(
    "code, expected",
    [
        ("2-1-01-00-000", "RUTA: E2 › P1 › C01"),
        ("2-1-01-02-000", "RUTA: E2 › P1 › C01 › CJ02"),
        ("2-1-01-02-003", "RUTA: E2 › P1 › C01 › CJ02 › I003"),
        ("M1-E2", "RUTA: MESA 1 › EQ2"),
        ("E3-LM07", "RUTA: E3 › LEGO 07"),
        ("LAB-MIC-01", ""),
    ],
)
def test_location_guide_is_compact_and_omits_non_applicable_levels(code, expected):
    assert labels._location_guide(code) == expected


def test_title_spacing_accounts_for_letters_and_words():
    font = labels._load_font(14, bold=True)
    base = labels._text_width("LAB TEST", font)
    spaced = labels._text_width("LAB TEST", font, tracking=2, word_spacing=3)
    assert spaced == pytest.approx(base + (len("LAB TEST") - 1) * 2 + 3)


def test_product_title_band_and_route_line_preserve_full_barcode():
    item = {
        **PRODUCT,
        "id": "2-1-01-02-003",
        "name": "Microscopio binocular",
    }
    layout = labels.layout_item_label(item)
    img = labels.render_label(layout)
    scan = _scan_label(img)

    assert layout.band_box  # banda negra del nombre
    assert layout.lines_for("name")[0].text == "Microscopio binocular"
    route = [
        chunk for line in layout.lines_for("meta")
        for chunk in line.text.split(labels._C_META_SEPARATOR) if chunk.startswith("RUTA:")
    ]
    assert route == ["RUTA: E2 › P1 › C01 › CJ02 › I003"]  # completa, nunca recortada
    assert scan["band_height"] >= 56
    assert scan["text"] == item["id"]


def test_label_without_notice_or_description_only_has_bars_and_code():
    img = labels.generate_label_image("LAB-MIC-01", notice=None)
    scan = _scan_label(img)
    assert scan["text"] == "LAB-MIC-01"
    assert _text_lines(scan["rows"], 0, scan["band_top"]) == 0
    assert _text_lines(scan["rows"], scan["band_top"] + scan["band_height"], img.size[1]) == 1


def test_generate_label_png_bytes_returns_valid_png():
    data = labels.generate_label_png_bytes("1-2-05-12-001")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

    img = Image.open(io.BytesIO(data))
    assert img.size == labels.LABEL_CANVAS_SIZE


def test_label_png_for_lab_mic_01_declares_printer_dpi_and_scans_back():
    data = labels.generate_label_png_bytes("LAB-MIC-01", description="Microscopio óptico")
    img = Image.open(io.BytesIO(data))
    assert img.info["dpi"] == pytest.approx((203, 203), abs=0.5)
    assert _scan_label(img)["text"] == "LAB-MIC-01"


@pytest.mark.parametrize(
    "width_mm, height_mm, dpi", [(50, 25, 203), (60, 40, 203), (100, 50, 203), (50, 25, 300)],
)
def test_print_ready_pdf_matches_the_label_size_and_asks_viewers_not_to_scale(width_mm, height_mm, dpi):
    spec = labels.LabelSpec(width_mm, height_mm, dpi)
    data = labels.generate_label_pdf_bytes(
        PRODUCT["id"], description=PRODUCT["name"], category=PRODUCT["category"],
        location=PRODUCT["location"], item_type=PRODUCT["item_type"], spec=spec,
    )
    width_px, height_px = spec.canvas_size
    assert data.startswith(b"%PDF-1.7")
    assert data.endswith(b"%%EOF\n")
    assert f"/Width {width_px} /Height {height_px}".encode() in data
    assert b"/BitsPerComponent 1" in data
    # El visor no debe escalar al imprimir y debe elegir el papel por el tamaño del PDF.
    assert b"/ViewerPreferences << /PrintScaling /None /PickTrayByPDFSize true >>" in data
    assert b"/Info 6 0 R" in data and b"/Title <FEFF" in data

    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", data)
    assert media_box
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(width_mm, abs=0.01)
    assert float(media_box.group(2)) * 25.4 / 72 == pytest.approx(height_mm, abs=0.01)

    matrix = re.search(rb"q\n([0-9.]+) 0 0 ([0-9.]+) ([0-9.]+) ([0-9.]+) cm", data)
    assert matrix
    # Cada pixel ocupa un punto de impresora y el raster parte de la esquina
    # superior izquierda (la alineacion fina se prueba en test_label_print.py).
    assert float(matrix.group(1)) * dpi / 72 == pytest.approx(width_px, abs=0.02)
    assert float(matrix.group(2)) * dpi / 72 == pytest.approx(height_px, abs=0.02)
    assert float(matrix.group(3)) * dpi / 72 == pytest.approx(0, abs=0.02)
    top = float(media_box.group(2)) - float(matrix.group(4)) - float(matrix.group(2))
    assert top * dpi / 72 == pytest.approx(0, abs=0.02)

    startxref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[startxref:].startswith(b"xref\n")


def test_generate_item_label_pdf_uses_item_and_skips_unprintable_ids():
    assert labels.generate_item_label_pdf_bytes(PRODUCT).startswith(b"%PDF-1.7")
    assert labels.generate_item_label_pdf_bytes({"id": "CAJA 001"}) is None
    assert labels.generate_item_label_pdf_bytes({"id": ""}) is None

def test_generate_item_label_png_bytes_uses_the_item_and_skips_unprintable_ids():
    data = labels.generate_item_label_png_bytes({"id": "0012345", "name": "Osciloscopio"})
    assert _scan_label(Image.open(io.BytesIO(data)))["text"] == "0012345"
    assert labels.generate_item_label_png_bytes({"id": "CAJA 001", "name": "Caja vieja"}) is None
    assert labels.generate_item_label_png_bytes({"id": "", "name": "Sin codigo"}) is None


def test_generate_item_label_forwards_professional_metadata(monkeypatch):
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return b"png"

    monkeypatch.setattr(labels, "generate_label_png_bytes", fake_generate)
    assert labels.generate_item_label_png_bytes(PRODUCT) == b"png"
    assert captured == {
        "code": "LAB-MIC-01",
        "description": "Microscopio óptico binocular",
        "category": "Óptica",
        "location": "Estantería 2 · Piso 1",
        "item_type": "standalone",
        "spec": None,
    }

def test_container_level_code_with_na_zeros_generates_and_scans_exactly():
    """El codigo que originaba el error tambien debe producir una etiqueta
    Code 128 cuyo contenido recuperado sea exactamente el mismo."""
    code = "2-1-01-00-000"
    img = labels.generate_label_image(code, description="Contenedor 1")
    assert _scan_label(img)["text"] == code


def test_default_label_matches_sat_tt460_50x25_at_203dpi():
    assert labels.LABEL_DPI == 203
    assert labels.LABEL_WIDTH_MM == 50
    assert labels.LABEL_HEIGHT_MM == 25
    # Toda el area fisica: 50 mm x 203 / 25,4 = 399,6 -> 399 puntos (sin pasarse
    # del borde). Antes se usaban 384 x 192 y quedaba ~1 mm sin aprovechar.
    assert labels.LABEL_CANVAS_SIZE == (399, 199)
