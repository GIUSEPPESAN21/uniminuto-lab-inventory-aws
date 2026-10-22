# -*- coding: utf-8 -*-
"""Escritura segura de libros Excel y CSV.

Dos problemas reales de escribir texto de personas en un Excel:

* Un texto que empieza por ``=`` (``=1+1``, ``=HYPERLINK(...)``) lo guarda openpyxl
  como FÓRMULA: al recargar la celda queda vacía (se pierde el dato) y quien abra el
  archivo descargado ejecuta la fórmula. Aquí esas celdas se escriben como texto.
* Un carácter de control (por ejemplo el separador ``\\x1d`` que añaden algunas
  pistolas de códigos de barras) hace que openpyxl lance ``IllegalCharacterError``
  y el guardado falle. Aquí se quitan, sin perder el resto del texto.

Los CSV tienen su propio riesgo: Excel evalúa una celda que empieza por ``=``, ``+``,
``-`` o ``@``. ``csv_safe`` antepone un apóstrofo solo a los textos que lo necesitan.
"""

import logging
import re

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils.exceptions import IllegalCharacterError

logger = logging.getLogger(__name__)

_NUMBER = re.compile(r"^[+-]?\d+([.,]\d+)?$")
_CSV_TRIGGERS = ("=", "@", "+", "-", "\t", "\r")


def _is_text(series: pd.Series) -> bool:
    """Columna de texto: ``object`` (pandas 2 y las hojas del libro) o ``str`` (pandas 3)."""
    return pd.api.types.is_object_dtype(series) or isinstance(series.dtype, pd.StringDtype)


def strip_illegal(value):
    """Quita los caracteres que un libro Excel no admite (solo en texto)."""
    return ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value


def clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Copia de ``df`` sin caracteres de control en sus columnas de texto."""
    clean = df.copy()
    for column in clean.columns:
        if _is_text(clean[column]):
            clean[column] = clean[column].map(strip_illegal)
    return clean


def _protect_formulas(worksheet, df: pd.DataFrame) -> None:
    """Marca como texto las celdas que openpyxl habría guardado como fórmula."""
    for column_number, column in enumerate(df.columns, start=1):
        if not _is_text(df[column]):
            continue
        for row_number, value in enumerate(df[column].tolist(), start=2):
            if isinstance(value, str) and len(value) > 1 and value[0] == "=":
                worksheet.cell(row=row_number, column=column_number).data_type = "s"


def _write_once(target, frames: dict) -> None:
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        for sheet, df in frames.items():
            df.to_excel(writer, sheet_name=sheet, index=False)
            _protect_formulas(writer.sheets[sheet], df)


def write_frames(target, frames: dict) -> None:
    """Escribe ``{hoja: DataFrame}`` en ``target`` (un ``BytesIO`` o una ruta).

    Camino normal sin costo extra de limpieza: solo si openpyxl rechaza un carácter de
    control se limpian las hojas y se reescribe todo (queda un aviso en el registro)."""
    try:
        _write_once(target, frames)
    except IllegalCharacterError:
        logger.warning("Se quitaron caracteres de control del texto antes de guardar el libro.")
        if hasattr(target, "seek"):
            target.seek(0)
            target.truncate()
        _write_once(target, {sheet: clean_frame(df) for sheet, df in frames.items()})


def csv_safe(value):
    """Texto seguro para un CSV que se abrirá en Excel (evita la inyección de fórmulas).

    Solo toca textos que empiezan por ``=``, ``@``, tabulación, retorno de carro o
    ``+``/``-`` seguidos de algo que no es un número; el resto queda igual."""
    if not isinstance(value, str) or not value or value[0] not in _CSV_TRIGGERS:
        return value
    if value[0] in "+-" and _NUMBER.match(value):
        return value
    return "'" + value


def csv_safe_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Copia de ``df`` con ``csv_safe`` aplicado a sus columnas de texto."""
    safe = df.copy()
    for column in safe.columns:
        if _is_text(safe[column]):
            safe[column] = safe[column].map(csv_safe)
    return safe
