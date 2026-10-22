# -*- coding: utf-8 -*-
"""El PDF de la etiqueta se imprime punto por punto: al rasterizarlo a la
resolucion de la impresora, cada pixel del raster cae sobre un punto del
cabezal, sin remuestreo, y las barras conservan su ancho exacto.

Se comprueba de tres formas:
- geometria: los bordes del raster quedan apenas dentro de puntos enteros y
  sobreviven al redondeo de poppler, Acrobat y Chrome/Edge en todo el rango
  de tamaños permitido;
- PDFium (el motor de Chrome y Edge) siguiendo la ruta con la que Chrome
  imprime un PDF en Windows hacia una impresora raster como la SAT TT460;
- poppler (pdftoppm), si esta instalado.
"""

import ctypes
import io
import math
import re
import shutil
import subprocess

import pytest
from PIL import Image, ImageChops

from core import labels
from test_labels import _scan_label

ITEM = {
    "id": "2-1-01-01-001", "name": "Multímetro digital", "item_type": "standalone",
    "category": "Instrumentación", "location": "Laboratorio 3",
}
# Presets y, a 203 dpi, las medidas en las que el redondeo de Chrome es mas
# exigente (el papel en puntos PDF enteros queda a menos de 1/72 de un pixel).
PRINT_SPECS = [
    (50, 25, 203), (50, 30, 203), (60, 40, 203), (100, 50, 203), (100, 75, 203),
    (101.6, 76.2, 203), (100, 100, 203), (100, 150, 203), (47, 47, 203), (72.5, 72.5, 203),
    (98, 98, 203), (104, 21.7, 203), (101.6, 152.4, 203),
    (50, 25, 300), (60, 40, 300), (100, 50, 300), (101.6, 150, 300),
]


def _spec_id(spec):
    return f"{spec[0]:g}x{spec[1]:g}@{spec[2]}"


def _placement(data: bytes) -> tuple:
    """(ancho, alto) de la pagina y (a, d, e, f) de la matriz del raster, en pt."""
    media = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", data)
    matrix = re.search(rb"q\n([0-9.]+) 0 0 ([0-9.]+) ([0-9.]+) ([0-9.]+) cm", data)
    assert media and matrix
    return tuple(float(value) for value in media.groups() + matrix.groups())


def _raster_edges(data: bytes, dpi: int) -> tuple:
    """Bordes del raster en puntos de impresora, medidos desde la esquina
    superior izquierda de la pagina: (izquierda, arriba, derecha, abajo)."""
    _, page_height, a, d, e, f = _placement(data)
    k = dpi / 72
    return e * k, (page_height - f - d) * k, (e + a) * k, (page_height - f) * k


def _chrome_scale(page_pt: float, dpi: int, floor_paper: bool) -> float:
    """Escala con la que Chrome dibuja la pagina en el bitmap que envia a una
    impresora raster en Windows: CalculatePosition() TRUNCA el tamaño en
    pixeles (pdf/pdfium/pdfium_api_wrappers.cc). Con la escala Predeterminado
    o Tamaño real (kNone) antes recorta el papel a puntos PDF enteros
    (ToFlooredSize en pdf/pdfium/pdfium_print.cc)."""
    page_px = (math.floor(page_pt) if floor_paper else page_pt) * dpi / 72
    return int(page_px) / page_px


def _blank_label_pdf(spec: labels.LabelSpec) -> bytes:
    return labels._label_image_to_pdf(Image.new("1", spec.canvas_size, 1), spec)


def _assert_printed_exactly(printed: Image.Image, raster: Image.Image, dpi: int) -> None:
    """El bitmap impreso coincide pixel a pixel con el raster. Chrome puede
    recortar el borde derecho e inferior (menos de 1 pt del papel y 1 pixel
    del truncado), pero nunca debe remuestrear."""
    width = min(printed.width, raster.width)
    height = min(printed.height, raster.height)
    max_clip = math.ceil(dpi / 72) + 1
    assert width >= raster.width - max_clip and height >= raster.height - max_clip
    box = (0, 0, width, height)
    difference = ImageChops.difference(printed.convert("L").crop(box), raster.convert("L").crop(box))
    assert difference.getbbox() is None, f"pixeles remuestreados en {difference.getbbox()}"


def _label_and_raster(spec: labels.LabelSpec) -> tuple:
    pdf, _ = labels.item_label_files(ITEM, spec)
    raster = labels.render_label(labels.layout_item_label(ITEM, spec))
    return pdf, raster


# --- Geometria (sin dependencias) ----------------------------------------------

def _sweep_sizes() -> list:
    """Todo el rango cada 0,5 mm y, cada 0,1 mm, donde el truncado de Chrome es
    mas exigente a 203 dpi: papeles de 61, 133, 205, 277, 349 o 421 pt enteros
    (61 * 203 / 72 = 171,99: al truncar se pierden 71/72 de pixel)."""
    widths = {half / 2 for half in range(40, 209)}
    heights = {half / 2 for half in range(30, 321)}
    for points in (61, 133, 205, 277, 349, 421):
        tight = {round(points * 25.4 / 72 + tenth / 10, 1) for tenth in range(4)}
        widths |= {mm for mm in tight if 20 <= mm <= 104}
        heights |= {mm for mm in tight if 15 <= mm <= 160}
    return [(width, 25) for width in sorted(widths)] + [(50, height) for height in sorted(heights)]


@pytest.mark.parametrize("dpi", labels.SUPPORTED_DPI)
def test_raster_edges_survive_every_viewer_rounding(dpi):
    """Todos los anchos y altos permitidos (ver _sweep_sizes).

    - poppler y Acrobat dibujan a escala exacta: el inicio debe caer en el
      pixel 0 y el final antes del borde del ultimo pixel (poppler suma un
      pixel a un borde final que cae justo en un entero);
    - PDFium (Chrome/Edge) cubre el rectangulo exterior (floor/ceil) de la
      imagen ya escalada por el truncado de Chrome: debe seguir cubriendo
      exactamente n pixeles para copiar el raster 1:1.
    """
    for width_mm, height_mm in _sweep_sizes():
        spec = labels.LabelSpec(width_mm, height_mm, dpi)
        data = _blank_label_pdf(spec)
        page_width, page_height = _placement(data)[:2]
        left, top, right, bottom = _raster_edges(data, dpi)
        columns, rows = spec.canvas_size
        for start, end, pixels, page_pt in ((left, right, columns, page_width),
                                            (top, bottom, rows, page_height)):
            assert 0 < start < 0.05 and pixels - 0.05 < end < pixels, spec.describe()
            for floor_paper in (True, False):
                scale = _chrome_scale(page_pt, dpi, floor_paper)
                assert math.floor(start * scale) == 0, spec.describe()
                assert math.ceil(end * scale) == pixels, spec.describe()


def test_raster_is_anchored_top_left_not_centered():
    """Chrome alinea el contenido arriba a la izquierda al imprimir y el
    sobrante de la pagina (< 1 punto) debe quedar a la derecha y abajo."""
    spec = labels.LabelSpec(50, 25, 203)
    data = _blank_label_pdf(spec)
    left, top, right, bottom = _raster_edges(data, 203)
    page_width, page_height = (value * 203 / 72 for value in _placement(data)[:2])
    assert left < 0.02 and top < 0.02
    assert page_width - right == pytest.approx(0.6, abs=0.01)    # 399,6 - 399
    assert page_height - bottom == pytest.approx(0.8, abs=0.01)  # 199,8 - 199


# --- PDFium: el motor de Chrome y Edge -----------------------------------------

@pytest.fixture(scope="module")
def pdfium():
    return pytest.importorskip("pypdfium2")


def _chrome_print(pdfium, data: bytes, dpi: int, scaling: str) -> Image.Image:
    """Imprime `data` como Chrome/Edge en Windows hacia una impresora raster y
    devuelve el bitmap que recibe el driver:
    1. PDFiumPrint::CreatePrintPdf importa la pagina en un PDF nuevo. Con la
       escala Predeterminado/Tamaño real (kNone), TransformPDFPageForPrinting
       recorta el papel (= la etiqueta) a puntos enteros y alinea el contenido
       arriba a la izquierda; con kSourceSize deja la pagina intacta.
    2. RenderPageToDC pinta la pagina con FPDF_RenderPageBitmap en un bitmap
       del tamaño truncado (FPDF_ANNOT | FPDF_PRINTING) y lo copia 1:1.
    """
    raw = pdfium.raw
    source = pdfium.PdfDocument(data)
    document = pdfium.PdfDocument.new()
    document.import_pages(source)
    if scaling == "predeterminado":
        page = document[0]
        width, height = raw.FPDF_GetPageWidthF(page.raw), raw.FPDF_GetPageHeightF(page.raw)
        paper_width, paper_height = math.floor(width), math.floor(height)
        for set_box in (raw.FPDFPage_SetMediaBox, raw.FPDFPage_SetCropBox, raw.FPDFPage_SetTrimBox):
            set_box(page.raw, 0, 0, paper_width, paper_height)
        shift = paper_height - height  # CalculateNonScaledClipBoxOffset, rotacion 0
        assert raw.FPDFPage_TransFormWithClip(
            page.raw, raw.FS_MATRIX(1, 0, 0, 1, 0, shift),
            raw.FS_RECTF(0, height + shift, width, shift),
        )
        page.close()
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    source.close()

    printed = pdfium.PdfDocument(buffer.getvalue())
    page = printed[0]
    width = int(raw.FPDF_GetPageWidthF(page.raw) * dpi / 72)
    height = int(raw.FPDF_GetPageHeightF(page.raw) * dpi / 72)
    bitmap = raw.FPDFBitmap_Create(width, height, 0)
    try:
        raw.FPDFBitmap_FillRect(bitmap, 0, 0, width, height, 0xFFFFFFFF)
        raw.FPDF_RenderPageBitmap(bitmap, page.raw, 0, 0, width, height, 0,
                                  raw.FPDF_ANNOT | raw.FPDF_PRINTING)
        stride = raw.FPDFBitmap_GetStride(bitmap)
        pointer = ctypes.cast(raw.FPDFBitmap_GetBuffer(bitmap),
                              ctypes.POINTER(ctypes.c_ubyte * (stride * height)))
        image = Image.frombuffer("RGBX", (width, height), bytes(pointer.contents),
                                 "raw", "BGRX", stride, 1).convert("L")
    finally:
        raw.FPDFBitmap_Destroy(bitmap)
        page.close()
        printed.close()
    return image


@pytest.mark.parametrize("scaling", ["predeterminado", "origen"])
@pytest.mark.parametrize("size", PRINT_SPECS, ids=_spec_id)
def test_chrome_and_edge_print_the_label_dot_for_dot(pdfium, size, scaling):
    spec = labels.LabelSpec(*size)
    pdf, raster = _label_and_raster(spec)
    printed = _chrome_print(pdfium, pdf, spec.dpi, scaling)
    _assert_printed_exactly(printed, raster, spec.dpi)
    assert _scan_label(printed)["text"] == ITEM["id"]


@pytest.mark.parametrize(
    "size", [(50, 25, 203), (100, 50, 203), (101.6, 76.2, 203), (50, 25, 300)], ids=_spec_id,
)
def test_chrome_prints_the_calibration_page_dot_for_dot(pdfium, size):
    spec = labels.LabelSpec(*size)
    printed = _chrome_print(pdfium, labels.generate_calibration_pdf_bytes(spec), spec.dpi,
                            "predeterminado")
    _assert_printed_exactly(printed, labels.generate_calibration_image(spec), spec.dpi)


def test_chrome_emulation_detects_the_old_centered_raster(pdfium):
    """Control: el PDF de v1.10 centraba el raster con 4 decimales. En la
    etiqueta de 50 x 25 mm Chrome lo remuestreaba (perdia una fila de puntos
    en el texto); la emulacion debe detectarlo."""
    spec = labels.LabelSpec(50, 25, 203)
    _, raster = _label_and_raster(spec)
    current = labels._label_image_to_pdf(raster, spec)
    page_width, page_height = spec.width_mm * 72 / 25.4, spec.height_mm * 72 / 25.4
    image_width, image_height = raster.width * 72 / spec.dpi, raster.height * 72 / spec.dpi
    centered = current.replace(
        re.search(rb"q\n[^\n]+ cm", current).group(0),
        (f"q\n{image_width:.4f} 0 0 {image_height:.4f} {(page_width - image_width) / 2:.4f} "
         f"{(page_height - image_height) / 2:.4f} cm").encode("ascii"),
    )
    printed = _chrome_print(pdfium, centered, spec.dpi, "predeterminado")
    with pytest.raises(AssertionError, match="remuestreados"):
        _assert_printed_exactly(printed, raster, spec.dpi)


# --- poppler (Evince, Okular, CUPS en Linux) -----------------------------------

@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler (pdftoppm) no esta instalado")
@pytest.mark.parametrize("size", PRINT_SPECS, ids=_spec_id)
def test_poppler_prints_the_label_dot_for_dot(tmp_path, size):
    spec = labels.LabelSpec(*size)
    pdf, raster = _label_and_raster(spec)
    path = tmp_path / "etiqueta.pdf"
    path.write_bytes(pdf)
    subprocess.run(
        ["pdftoppm", "-r", str(spec.dpi), "-gray", "-singlefile", str(path), str(tmp_path / "impresa")],
        check=True, capture_output=True,
    )
    printed = Image.open(tmp_path / "impresa.pgm")
    _assert_printed_exactly(printed, raster, spec.dpi)
    assert _scan_label(printed)["text"] == ITEM["id"]
