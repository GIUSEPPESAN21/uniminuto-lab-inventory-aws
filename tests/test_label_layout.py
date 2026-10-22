# -*- coding: utf-8 -*-
"""Etiquetas en el formato clasico del laboratorio a su tamaño real: la misma
composicion de siempre, escalada; solo el contenido se ajusta para caber (sin
cortes cuando hay alternativa). Tambien: PDF sin escalado, hoja de prueba con
regla milimetrada y configuracion guardada en la base."""

import io
import json
import re
import time

import pandas as pd
import pytest
from PIL import Image

from core import labels
from core import storage as storage_module
from test_labels import _scan_label

PRESETS = [(50, 25), (50, 30), (60, 40), (100, 50), (100, 75), (101.6, 76.2), (100, 100), (100, 150),
           (101.6, 152.4)]
ITEMS = [
    {"id": "2-1-01-00-000", "name": "Contenedor 1", "item_type": "master"},
    {"id": "2-1-01-01-000", "name": "Resistencias 220 ohm", "item_type": "child",
     "category": "Electrónica", "location": "Estante 2"},
    {"id": "2-1-01-01-001", "name": "Multímetro digital Fluke 115 True RMS", "item_type": "standalone",
     "category": "Instrumentación", "location": "Laboratorio 3"},
    {"id": "M1-E2", "name": "Impresora 3D Prusa MK4", "item_type": "standalone", "category": "Fabricación"},
    {"id": "E3-LM07", "name": "Brazo robótico Lego", "item_type": "standalone"},
    {"id": "LAB-MIC-01", "name": "Microscopio óptico binocular", "item_type": "standalone",
     "category": "Óptica", "location": "Estantería 2 · Piso 1"},
    {"id": "12345678901234567890", "name": "Osciloscopio Rigol", "item_type": "standalone"},
    {"id": "ABCDEFGHIJKLM", "name": "Supercalifragilisticoespialidoso", "item_type": "standalone"},
]
FULL_ITEM = ITEMS[2]


def _dots(mm, dpi=labels.LABEL_DPI):
    return mm * dpi / 25.4


def _blocks(layout):
    """Bloques verticales en orden: (tipo, y0, y1)."""
    divider_top = layout.divider_box[1] if layout.divider_box else None
    header = [
        line for line in layout.lines
        if divider_top is not None and line.top < divider_top and line.role != "name"
    ]
    blocks = []
    if header or layout.logo_box:
        tops = [line.top for line in header] + ([layout.logo_box[1]] if layout.logo_box else [])
        bottoms = [line.top + line.ink_height for line in header] + (
            [layout.logo_box[3]] if layout.logo_box else []
        )
        blocks.append(("header", min(tops), max(bottoms)))
    if layout.divider_box:
        blocks.append(("divider", layout.divider_box[1], layout.divider_box[3]))
    if layout.band_box:
        blocks.append(("band", layout.band_box[1], layout.band_box[3]))
    for line in layout.lines_for("meta"):
        if divider_top is None or line.top > divider_top:
            blocks.append(("meta", line.top, line.top + line.ink_height))
    blocks.append(("bars", layout.bar_box[1], layout.bar_box[3]))
    code = layout.lines_for("code")[0]
    blocks.append(("code", code.top, code.top + code.ink_height))
    return blocks


# --- Formato clasico: estructura y margenes ------------------------------------

@pytest.mark.parametrize("size", PRESETS)
@pytest.mark.parametrize("item", ITEMS, ids=[i["id"] for i in ITEMS])
def test_classic_blocks_keep_their_order_and_stay_inside_the_label(size, item):
    """El formato de siempre: encabezado, filete, banda del nombre, datos,
    barras y codigo, en ese orden, sin encimarse y dentro de la etiqueta."""
    spec = labels.LabelSpec(*size)
    layout = labels.layout_item_label(item, spec)
    width, height = spec.canvas_size
    blocks = _blocks(layout)
    kinds = [kind for kind, _, _ in blocks]
    order = ["header", "divider", "band", "meta", "bars", "code"]
    assert [k for k in order if k in kinds] == list(dict.fromkeys(kinds))
    for (kind_a, _, end_a), (kind_b, start_b, _) in zip(blocks, blocks[1:]):
        assert start_b >= end_a, f"{kind_a} encima de {kind_b} en {spec.size_text}"
    assert blocks[0][1] >= 1 and blocks[-1][2] <= height - 1
    for line in layout.lines:
        x0, _, x1, _ = line.box
        assert x0 >= 1 and x1 <= width - 1, line.text


def test_pixels_confirm_blank_rows_between_text_blocks():
    """Comprobacion independiente del motor: en la imagen impresa hay filas
    totalmente blancas entre los bloques de texto y las barras."""
    layout = labels.layout_item_label(ITEMS[1])
    img = labels.render_label(layout).convert("L")
    width = img.width
    blank = [all(img.getpixel((x, y)) > 127 for x in range(width)) for y in range(img.height)]
    blocks = [b for b in _blocks(layout) if b[0] in ("meta", "bars", "code")]
    for (_, _, end_a), (_, start_b, _) in zip(blocks, blocks[1:]):
        assert any(blank[end_a:start_b]), "no hay una sola fila en blanco entre dos bloques"


def test_reference_label_keeps_the_classic_format():
    """50 x 25 mm: logo + «LABORATORIO DE INGENIERÍA» / tipo / aviso, filete,
    nombre en banda negra, ruta y datos, barras de 9-10 mm y codigo grande."""
    layout = labels.layout_item_label(
        {"id": "2-1-01-00-000", "name": "Contenedor 1", "item_type": "master",
         "category": "Piezas Lego", "location": "Estantería- 2; Piso-1; Contenedor-1."},
    )
    assert layout.logo_box and layout.divider_box and layout.band_box
    assert [line.text for line in layout.lines_for("lab")] == [labels.LABEL_INSTITUTION_TEXT]
    assert [line.text for line in layout.lines_for("type")] == ["Contenedor Principal"]
    assert [line.text for line in layout.lines_for("notice")] == [labels.LABEL_NOTICE_TEXT]
    name = layout.lines_for("name")[0]
    assert name.text == "Contenedor 1" and name.inverse
    assert layout.lines_for("code")[0].text == "2-1-01-00-000"
    assert 9.0 <= layout.bar_height_mm <= 10.5
    # El encabezado va junto al logo y el titulo del laboratorio es el mas grande.
    lab, kind, notice = layout.lines_for("lab")[0], layout.lines_for("type")[0], layout.lines_for("notice")[0]
    assert lab.x > layout.logo_box[2] and lab.size > kind.size > notice.size
    assert layout.lines_for("code")[0].size > name.size > lab.size


# --- Solo se ajusta el contenido ---------------------------------------------------

def test_content_is_adjusted_instead_of_cut():
    """Antes el aviso y los datos salian cortados («NO RETIRAR SIN P…»,
    «CAT…»). Ahora el aviso cabe sin el espacio extra entre letras y los datos
    pasan a una segunda linea: nada termina en «…»."""
    layout = labels.layout_item_label(
        {"id": "2-1-01-00-000", "name": "Contenedor 1", "item_type": "master",
         "category": "Piezas Lego", "location": "Estantería- 2; Piso-1; Contenedor-1."},
    )
    assert not any(line.text.endswith("…") for line in layout.lines)
    assert layout.shortened == () and layout.omitted == ()
    meta = [line.text for line in layout.lines_for("meta")]
    assert len(meta) == 2
    joined = labels._C_META_SEPARATOR.join(meta)
    assert "RUTA: E2 › P1 › C01" in joined and "UBIC: Estantería- 2; Piso-1; Contenedor-1." in joined
    assert "CAT: Piezas Lego" in joined
    assert len({line.size for line in layout.lines_for("meta")}) == 1  # las dos lineas se ven iguales
    # El titulo conserva su espaciado; solo el aviso, que no cabia, lo pierde.
    assert layout.lines_for("lab")[0].tracking > 0
    assert layout.lines_for("notice")[0].tracking == 0


def test_data_drop_whole_items_before_cutting_a_word():
    item = {"id": "2-1-01-02-003", "name": "Microscopio binocular", "item_type": "standalone",
            "category": "Óptica", "location": "Estantería 2 · Piso 1"}
    layout = labels.layout_item_label(item)
    meta = [line.text for line in layout.lines_for("meta")]
    assert meta == ["RUTA: E2 › P1 › C01 › CJ02 › I003  ·  UBIC: Estantería 2 · Piso 1"]
    assert layout.omitted == ("category",) and layout.shortened == ()
    assert "Categoría" in labels.describe_omitted(layout)
    # Con mas alto, la categoria vuelve en una segunda linea.
    taller = labels.layout_item_label(item, labels.LabelSpec(50, 30))
    assert taller.omitted == () and len(taller.lines_for("meta")) == 2


def test_a_long_name_shrinks_then_loses_spacing_and_only_then_is_shortened():
    spec = labels.LabelSpec()
    fits = labels.layout_label("LAB-MIC-01", spec, description="Microscopio óptico binocular")
    assert fits.lines_for("name")[0].text == "Microscopio óptico binocular" and fits.shortened == ()
    long_name = "Multímetro digital Fluke 115 True RMS con puntas de prueba y estuche rígido"
    cut = labels.layout_label("LAB-MIC-01", spec, description=long_name)
    name = cut.lines_for("name")[0]
    assert name.text.endswith("…") and long_name.startswith(name.text[:-1].rstrip())
    assert name.tracking == 0 and "name" in cut.shortened
    assert name.size == max(round(12 * cut.scale), 9)


@pytest.mark.parametrize("dpi", labels.SUPPORTED_DPI)
@pytest.mark.parametrize("size", PRESETS)
def test_no_text_is_printed_below_the_classic_minimum(size, dpi):
    """El formato clasico nunca imprime letras de menos de 9 puntos a 203 dpi
    (~3,2 pt): en etiquetas mas grandes todo crece en proporcion."""
    spec = labels.LabelSpec(*size, dpi)
    floor_pt = 9 * 72 / labels.LABEL_DPI
    for item in ITEMS:
        layout = labels.layout_item_label(item, spec)
        assert layout.min_text_pt >= floor_pt - 0.1, (item["id"], spec.describe())
        assert [line.text for line in layout.lines_for("code")] == [item["id"]]


@pytest.mark.parametrize("size", PRESETS)
def test_institutional_texts_and_routes_are_never_truncated(size):
    spec = labels.LabelSpec(*size)
    for item in ITEMS:
        layout = labels.layout_item_label(item, spec)
        for line in layout.lines_for("lab"):
            assert line.text == labels.LABEL_INSTITUTION_TEXT
        for line in layout.lines_for("notice"):
            assert line.text in (labels.LABEL_NOTICE_TEXT, labels.LABEL_NOTICE_SHORT_TEXT)
        routes = [
            chunk for line in layout.lines_for("meta")
            for chunk in line.text.split(labels._C_META_SEPARATOR) if chunk.startswith("RUTA:")
        ]
        for route in routes:
            assert route == labels._location_guide(item["id"])


def test_bigger_labels_scale_the_same_format():
    small = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(50, 25))
    big = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(100, 50))
    for role in ("lab", "name", "code"):
        assert big.lines_for(role)[0].size == pytest.approx(2 * small.lines_for(role)[0].size, abs=2)
    assert big.bar_height_mm == pytest.approx(2 * small.bar_height_mm, rel=0.15)
    def roles(layout):
        return [line.role for line in layout.lines if line.role != "meta"]
    assert roles(big) == roles(small)
    assert big.lines_for("meta") and small.lines_for("meta")


def test_tall_labels_center_the_format_vertically():
    layout = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(100, 150))
    height = layout.size[1]
    blocks = _blocks(layout)
    top, bottom = blocks[0][1], height - blocks[-1][2]
    assert abs(top - bottom) <= 0.1 * height


@pytest.mark.parametrize("size", PRESETS)
def test_brand_is_kept_on_every_preset(size):
    for item in ITEMS:
        assert "brand" in labels.layout_item_label(item, labels.LabelSpec(*size)).shown


def test_content_can_be_switched_off():
    bare = labels.LabelSpec(content=())
    layout = labels.layout_item_label(FULL_ITEM, bare)
    assert layout.logo_box is None and layout.divider_box is None
    assert {line.role for line in layout.lines} == {"name", "code"}
    assert layout.omitted == ()

    notice_only = labels.layout_item_label(FULL_ITEM, labels.LabelSpec(content=("notice",)))
    assert notice_only.logo_box is None
    assert [line.role for line in notice_only.lines][:1] == ["notice"]


@pytest.mark.parametrize("size", PRESETS)
@pytest.mark.parametrize("dpi", labels.SUPPORTED_DPI)
def test_every_size_and_resolution_scans_back_exactly(size, dpi):
    spec = labels.LabelSpec(*size, dpi)
    for item in ITEMS:
        img = labels.render_label(labels.layout_item_label(item, spec))
        assert img.size == spec.canvas_size and img.mode == "1"
        scan = _scan_label(img)
        assert scan["text"] == item["id"]
        assert scan["dots"] * 25.4 / dpi >= 0.249           # modulo >= 0,25 mm
        assert scan["band_height"] * 25.4 / dpi >= 6.99     # barras >= 7 mm
        assert scan["quiet_left"] >= 6 * scan["dots"] and scan["quiet_right"] >= 6 * scan["dots"]


def test_300dpi_png_declares_its_resolution():
    spec = labels.LabelSpec(50, 25, 300)
    data = labels.generate_item_label_png_bytes(FULL_ITEM, spec)
    img = Image.open(io.BytesIO(data))
    assert img.size == (590, 295)
    assert img.info["dpi"] == pytest.approx((300, 300), abs=0.5)


def test_code_too_long_for_a_narrow_label_is_reported_clearly():
    with pytest.raises(ValueError, match="no cabe"):
        labels.layout_label("2-1-01-01-001", labels.LabelSpec(20, 15), description="X")
    thin = labels.layout_label("12345678901234567890", labels.LabelSpec(20, 15), description="X")
    assert any("muy finas" in note for note in thin.warnings)


# --- Hoja de prueba ----------------------------------------------------------------

@pytest.mark.parametrize("size, dpi", [((50, 25), 203), ((100, 50), 203), ((20, 15), 203), ((50, 25), 300)])
def test_calibration_page_has_a_full_frame_and_a_ruler_of_exact_length(size, dpi):
    spec = labels.LabelSpec(*size, dpi)
    img = labels.generate_calibration_image(spec).convert("L")
    width, height = img.size
    assert (width, height) == spec.canvas_size
    pix = img.load()

    def longest_run(y):
        best = run = 0
        for x in range(width):
            run = run + 1 if pix[x, y] < 128 else 0
            best = max(best, run)
        return best

    runs = [longest_run(y) for y in range(height)]
    _assert_clear_margins(img, dpi)
    inset = round(_dots(1.0, dpi))
    frame = width - 2 * inset
    assert runs[inset] >= frame - 2 and runs[height - 1 - inset] >= frame - 2  # marco completo
    ruler = max(run for run in runs if run < frame - 2)
    expected_mm = max(10, int((size[0] - 8) // 10) * 10)
    assert abs(ruler - _dots(expected_mm, dpi)) <= 3  # la regla mide lo que dice (+-0,4 mm)


def _assert_clear_margins(img, dpi):
    """Nada se imprime fuera del marco ni a menos de 1 mm de su lado interior."""
    width, height = img.size
    pix = img.load()
    inset = round(_dots(1.0, dpi))
    stroke = max(2, round(_dots(0.25, dpi)))
    inner = (inset + stroke, inset + stroke, width - 1 - inset - stroke, height - 1 - inset - stroke)
    margin = inset + stroke + round(_dots(1.0, dpi))
    for y in range(height):
        for x in range(width):
            if pix[x, y] >= 128:
                continue
            assert inset <= x <= width - 1 - inset and inset <= y <= height - 1 - inset, (
                f"tinta fuera del marco en ({x}, {y})")
            if inner[0] <= x <= inner[2] and inner[1] <= y <= inner[3]:
                assert margin <= x <= width - 1 - margin and margin <= y <= height - 1 - margin, (
                    f"tinta a menos de 1 mm del marco en ({x}, {y})")


def _black_runs(pix, y, width):
    """Tramos negros (inicio, largo) de una fila."""
    runs, start = [], None
    for x in range(width):
        black = pix[x, y] < 128
        if black and start is None:
            start = x
        elif not black and start is not None:
            runs.append((start, x - start))
            start = None
    if start is not None:
        runs.append((start, width - start))
    return runs


@pytest.mark.parametrize(
    "size, dpi, modules",
    [((60, 40), 203, (2, 3, 4)), ((100, 75), 203, (2, 3, 4)), ((60, 40), 300, (3, 4, 6)),
     ((50, 25), 203, (2, 3, 4))],
)
def test_calibration_gratings_use_the_barcode_module_widths(size, dpi, modules):
    """Tres rejillas de 8 barras con el ancho de modulo del Code 128 (0,25, 0,35
    y 0,5 mm), con espacios iguales a las barras: si el visor remuestrea la
    imagen, esos anchos dejan de ser exactos."""
    spec = labels.LabelSpec(*size, dpi)
    img = labels.generate_calibration_image(spec).convert("L")
    width, height = img.size
    pix = img.load()
    inset = round(_dots(1.0, dpi))
    expected_widths = [module for module in modules for _ in range(8)]

    rows = []
    for y in range(height):
        runs = [(x, w) for x, w in _black_runs(pix, y, width) if inset + 8 < x < width - inset - 8]
        if [w for _, w in runs] == expected_widths:
            rows.append((y, runs))
    assert len(rows) >= round(_dots(3.0, dpi)) - 1  # la rejilla mide 3 mm de alto
    ys = [y for y, _ in rows]
    assert ys == list(range(ys[0], ys[0] + len(ys)))  # filas contiguas: una sola franja
    runs = rows[0][1]
    for index, module in enumerate(modules):
        bars = runs[index * 8:(index + 1) * 8]
        assert [bars[i + 1][0] - bars[i][0] for i in range(7)] == [2 * module] * 7  # espacio = barra
    group_center = (runs[0][0] + runs[-1][0] + runs[-1][1]) / 2
    assert abs(group_center - width / 2) <= 2  # el conjunto queda centrado en la etiqueta


def test_calibration_page_drops_gratings_before_the_ruler_when_space_is_short(monkeypatch):
    drawn = []
    original = labels._draw_line
    monkeypatch.setattr(labels, "_draw_line", lambda draw, line: (drawn.append(line.text), original(draw, line))[1])

    labels.generate_calibration_image(labels.LabelSpec(60, 40))
    assert "Barras parejas = impresión exacta" in drawn and "PRUEBA DE IMPRESIÓN" in drawn

    drawn.clear()  # 50 x 25: no cabe la leyenda, pero si las rejillas y los textos
    img = labels.generate_calibration_image(labels.LabelSpec(50, 25)).convert("L")
    assert "Barras parejas = impresión exacta" not in drawn and "PRUEBA DE IMPRESIÓN" in drawn
    pix = img.load()
    assert any(len(_black_runs(pix, y, img.width)) >= 26 for y in range(img.height))  # rejillas

    for size in ((20, 15), (25, 60)):  # sin espacio de alto (20 x 15) o de ancho (25 x 60)
        img = labels.generate_calibration_image(labels.LabelSpec(*size)).convert("L")
        _assert_clear_margins(img, 203)
        pix = img.load()
        assert all(len(_black_runs(pix, y, img.width)) < 26 for y in range(img.height)), size
        assert any(0.4 * img.width < w < img.width for y in range(img.height)
                   for _, w in _black_runs(pix, y, img.width)), size  # la regla se conserva


def test_calibration_pdf_is_print_ready():
    spec = labels.LabelSpec(100, 50)
    data = labels.generate_calibration_pdf_bytes(spec)
    assert data.startswith(b"%PDF-1.7") and b"/PrintScaling /None" in data
    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", data)
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(100, abs=0.01)
    assert float(media_box.group(2)) * 25.4 / 72 == pytest.approx(50, abs=0.01)


# --- Especificacion y configuracion guardada ----------------------------------------------

def test_spec_shows_the_size_in_inches_for_the_driver_paper():
    assert labels.LabelSpec(50, 25).inches_text == "1,97 × 0,98 in"
    assert labels.LabelSpec(100, 75).inches_text == "3,94 × 2,95 in"
    assert labels.LabelSpec(101.6, 76.2).inches_text == "4 × 3 in"
    assert labels.LabelSpec(101.6, 152.4).inches_text == "4 × 6 in"
    assert labels.LabelSpec(100, 75).preset_key == "100x75"


def test_spec_validates_ranges_with_helpful_messages():
    with pytest.raises(ValueError, match="ancho"):
        labels.LabelSpec(10, 25)
    with pytest.raises(ValueError, match="104 mm"):
        labels.LabelSpec(120, 25)
    with pytest.raises(ValueError, match="alto"):
        labels.LabelSpec(50, 10)
    with pytest.raises(ValueError, match="203 dpi"):
        labels.LabelSpec(50, 25, 600)
    with pytest.raises(ValueError, match="números"):
        labels.LabelSpec("ancho", 25)


def test_spec_normalizes_content_and_round_trips():
    spec = labels.LabelSpec(50, 30, 203, ("category", "brand", "desconocido"))
    assert spec.content == ("brand", "category")
    assert labels.LabelSpec.from_dict(spec.to_dict()) == spec
    assert spec.preset_key == "50x30"
    assert labels.LabelSpec(55, 35).preset_key == "custom"
    assert spec.describe() == "50 × 30 mm · 203 dpi"
    assert labels.LabelSpec(50.5, 25).size_text == "50,5 × 25 mm"
    with pytest.raises(ValueError):
        labels.LabelSpec.from_dict({"content": "brand"})


def test_load_label_spec_falls_back_to_the_factory_default(storage):
    key = labels.LABEL_SPEC_SETTING_KEY
    assert labels.load_label_spec(storage) == labels.LabelSpec()
    storage.set_setting(key, "{no es json")
    assert labels.load_label_spec(storage) == labels.LabelSpec()
    storage.set_setting(key, json.dumps({"width_mm": 500}))
    assert labels.load_label_spec(storage) == labels.LabelSpec()
    assert labels.load_label_spec(object()) == labels.LabelSpec()  # backend sin configuracion


def test_saved_spec_is_loaded_back(storage):
    spec = labels.LabelSpec(100, 50, 203, ("brand", "route"))
    labels.save_label_spec(storage, spec, actor_email="prof@uniminuto.edu.co")
    assert labels.load_label_spec(storage) == spec


def test_settings_persist_in_the_database_and_survive_a_restart():
    db = storage_module.LabStorage()
    assert db.get_setting("label_spec") is None
    assert db.get_setting("label_spec", "defecto") == "defecto"
    db.set_setting("label_spec", '{"width_mm": 100}', "prof@uniminuto.edu.co")
    db.set_setting("label_spec", '{"width_mm": 60}', "prof@uniminuto.edu.co")  # reemplaza
    storage_module._cached_dfs = None  # = reiniciar la app
    assert db.get_setting("label_spec") == '{"width_mm": 60}'
    settings = storage_module._load_cache()["settings"]
    assert len(settings) == 1 and settings.iloc[0]["updated_by"] == "prof@uniminuto.edu.co"


def test_databases_from_before_this_version_get_the_settings_sheet():
    with pd.ExcelWriter(storage_module.EXCEL_PATH, engine="openpyxl") as writer:
        for sheet, columns in storage_module.SHEET_COLUMNS.items():
            if sheet != "settings":
                pd.DataFrame(columns=columns).to_excel(writer, sheet_name=sheet, index=False)
    storage_module._cached_dfs = None
    db = storage_module.LabStorage()
    assert "settings" in pd.ExcelFile(storage_module.EXCEL_PATH, engine="openpyxl").sheet_names
    assert db.get_setting("label_spec", "defecto") == "defecto"


# --- Rendimiento y cache ------------------------------------------------------------------

def test_item_label_files_are_cached_per_item_and_size():
    first = labels.item_label_files(ITEMS[1], labels.LabelSpec())
    again = labels.item_label_files(dict(ITEMS[1]), labels.LabelSpec())
    assert first is again
    bigger = labels.item_label_files(ITEMS[1], labels.LabelSpec(100, 50))
    assert bigger != first
    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", bigger[0])
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(100, abs=0.01)
    assert labels.item_label_files({"id": "CAJA 001"}) == (None, None)


def test_labels_are_fast_enough_for_a_large_catalog():
    items = [
        {"id": f"2-1-0{1 + i % 9}-0{1 + i % 5}-00{1 + i % 9}", "name": f"Equipo de prueba número {i}",
         "category": "Electrónica", "location": "Estante 2", "item_type": "standalone"}
        for i in range(30)
    ]
    labels.layout_item_label(items[0])  # calentamiento de fuentes
    start = time.perf_counter()
    for item in items:
        labels.render_label(labels.layout_item_label(item))
    # ~15 ms por etiqueta en un equipo normal; el margen amplio evita falsos fallos en CI.
    assert (time.perf_counter() - start) / len(items) < 0.25
