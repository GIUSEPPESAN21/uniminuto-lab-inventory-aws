# -*- coding: utf-8 -*-
"""Escritor de libros .xlsx rápido para la base de datos.

``pandas.to_excel`` + openpyxl escribe celda por celda: con una base de un año de uso (≈800 mil celdas)
una sola escritura tardaba ~12 s, y **cada** cambio de la app reescribe el libro completo. Este módulo
arma el XML de las hojas directamente (texto «en línea», números y booleanos) y lo comprime: la misma base
se escribe en menos de 1 s. El archivo resultante lo leen igual openpyxl, pandas y Excel.

Por construcción un texto como ``=1+1`` es texto (nunca fórmula) y los caracteres que XML no admite se
quitan; los números y booleanos conservan su tipo. Si algo falla, quien llama puede volver a la ruta de
pandas (``core.excel_safe.write_frames``)."""

import io
import math
import numbers
import re
import threading
import zipfile
from xml.sax.saxutils import escape

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
_MAX_TEXT = 32767          # límite de Excel por celda
_FIXED_TIME = (2020, 1, 1, 0, 0, 0)   # fecha fija: el mismo contenido da los mismos bytes


def _column_name(index: int) -> str:
    name = ""
    while True:
        index, rest = divmod(index, 26)
        name = chr(65 + rest) + name
        if index == 0:
            return name
        index -= 1


_COLUMNS = [_column_name(i) for i in range(256)]


def _text_cell(ref: str, value: str) -> str:
    if _BAD_XML.search(value):
        value = _BAD_XML.sub("", value)
    if len(value) > _MAX_TEXT:
        value = value[:_MAX_TEXT]
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


def _cell(ref: str, value):
    """XML de una celda, o ``None`` si va vacía."""
    if value is None or (isinstance(value, str) and not value) or type(value).__name__ in ("NAType", "NaTType"):
        return None
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return f'<c r="{ref}" t="b"><v>{int(bool(value))}</v></c>'
    if isinstance(value, numbers.Integral):
        return f'<c r="{ref}"><v>{int(value)}</v></c>'
    if isinstance(value, numbers.Real):
        number = float(value)
        if math.isnan(number):
            return None
        if math.isinf(number):
            return _text_cell(ref, str(number))
        return f'<c r="{ref}"><v>{number!r}</v></c>'
    return _text_cell(ref, value if isinstance(value, str) else str(value))


def sheet_xml(columns, rows) -> str:
    """XML de una hoja: fila de encabezados y luego ``rows`` (iterable de tuplas)."""
    width = len(columns)
    if width > len(_COLUMNS):
        raise ValueError("Demasiadas columnas para el escritor rápido.")
    out = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_NS}"><sheetData>']
    out.append('<row r="1">' + "".join(_text_cell(f"{_COLUMNS[i]}1", str(name)) for i, name in enumerate(columns)) + "</row>")
    for number, row in enumerate(rows, start=2):
        cells = [xml for xml in (_cell(f"{_COLUMNS[i]}{number}", value) for i, value in enumerate(row)) if xml]
        out.append(f'<row r="{number}">{"".join(cells)}</row>' if cells else f'<row r="{number}"/>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


# Hoja -> (DataFrame, XML). Casi todas las escrituras cambian una sola hoja: las demás se reutilizan.
# Los DataFrame que llegan aquí no se modifican después (los escritores reemplazan, no mutan).
_SHEET_CACHE = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _SHEET_CACHE.clear()


def _sheet_xml_cached(name, df) -> str:
    with _cache_lock:
        cached = _SHEET_CACHE.get(name)
    if cached is not None and cached[0].equals(df):
        return cached[1]
    xml = sheet_xml(list(df.columns), df.itertuples(index=False, name=None))
    with _cache_lock:
        _SHEET_CACHE[name] = (df, xml)
    return xml


def _safe_sheet_name(name: str) -> str:
    return escape(str(name)[:31], {'"': "&quot;"})


def write_workbook(target, frames: dict) -> None:
    """Escribe ``{hoja: DataFrame}`` en ``target`` (``BytesIO`` o ruta) como libro .xlsx."""
    names = list(frames)
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(names) + 1))
            + "</Types>"),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{_NS}" xmlns:r="{_REL_NS}"><sheets>'
            + "".join(f'<sheet name="{_safe_sheet_name(name)}" sheetId="{i}" r:id="rId{i}"/>'
                      for i, name in enumerate(names, start=1))
            + "</sheets></workbook>"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1))
            + f'<Relationship Id="rId{len(names) + 1}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"),
        "xl/styles.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{_NS}">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'),
    }
    for i, name in enumerate(names, start=1):
        df = frames[name]
        parts[f"xl/worksheets/sheet{i}.xml"] = _sheet_xml_cached(name, df)
    buffer = io.BytesIO() if not hasattr(target, "write") else target
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for path, content in parts.items():
            info = zipfile.ZipInfo(path, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    if not hasattr(target, "write"):
        with open(target, "wb") as handle:
            handle.write(buffer.getvalue())
