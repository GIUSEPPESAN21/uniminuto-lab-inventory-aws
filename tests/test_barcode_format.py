# -*- coding: utf-8 -*-
"""Pruebas de validacion, interpretacion y construccion de codigos.

Cubre GLIOPS V3 (incluidos los ceros finales que significan "no aplica"),
mesas, Lego, codigos libres y los constructores usados por el formulario.
"""

import pytest

from core import barcode


@pytest.mark.parametrize(
    "code, expected",
    [
        ("1-2-05-12-001", {"format": "estandar_numerico", "estanteria": 1, "piso": 2, "contenedor": 5, "caja": 12, "item": 1}),
        ("3-6-1-1-1", {"format": "estandar_numerico", "estanteria": 3, "piso": 6, "contenedor": 1, "caja": 1, "item": 1}),
        # Niveles finales no aplicables: el caso de la captura y el nivel caja.
        ("2-1-01-00-000", {"format": "estandar_numerico", "estanteria": 2, "piso": 1, "contenedor": 1, "caja": 0, "item": 0}),
        ("2-1-01-01-000", {"format": "estandar_numerico", "estanteria": 2, "piso": 1, "contenedor": 1, "caja": 1, "item": 0}),
        ("M1-E2", {"format": "mesa_trabajo", "mesa": 1, "equipo": 2}),
        ("M2-E1", {"format": "mesa_trabajo", "mesa": 2, "equipo": 1}),
        ("E3-LM07", {"format": "exhibicion_lego", "estanteria": 3, "modelo": 7}),
        ("E3-LM1", {"format": "exhibicion_lego", "estanteria": 3, "modelo": 1}),
        ("0012345", {"format": "numerico_libre"}),
        ("7", {"format": "numerico_libre"}),
        ("12345678901234567890", {"format": "numerico_libre"}),
        ("LAB-MIC-01", {"format": "alfanumerico_libre"}),
        ("lab-mic-01", {"format": "alfanumerico_libre"}),
        ("CAJA-001", {"format": "alfanumerico_libre"}),
        ("SENSOR_T.02", {"format": "alfanumerico_libre"}),
        ("A", {"format": "alfanumerico_libre"}),
        ("M1", {"format": "alfanumerico_libre"}),
        ("MESA-01", {"format": "alfanumerico_libre"}),
        ("99-LAB", {"format": "alfanumerico_libre"}),
        ("ABCDEFGHIJKLM", {"format": "alfanumerico_libre"}),
    ],
)
def test_parse_code_valid_formats(code, expected):
    assert barcode.parse_code(code) == expected


def test_parse_code_ignores_surrounding_whitespace():
    assert barcode.parse_code("  LAB-MIC-01\t") == {"format": "alfanumerico_libre"}
    assert barcode.parse_code(" 0012345 ") == {"format": "numerico_libre"}


@pytest.mark.parametrize(
    "code",
    [
        "4-2-05-12-001",   # estanteria fuera de rango (1 a 3)
        "0-2-05-12-001",
        "1-7-05-12-001",   # piso fuera de rango (1 a 6)
        "1-0-05-12-001",
        "1-2-00-00-000",   # siempre debe existir un contenedor numerado
        "1-2-05-00-001",   # no puede existir item si caja no aplica
    ],
)
def test_parse_code_standard_rejects_invalid_range_or_hierarchy(code):
    with pytest.raises(ValueError):
        barcode.parse_code(code)


def test_standard_na_hierarchy_error_is_specific():
    with pytest.raises(ValueError, match="Item debe ser 000 cuando Caja es 00"):
        barcode.parse_code("2-1-01-00-001")


@pytest.mark.parametrize(
    "code",
    [
        "M3-E1", "M0-E1", "m1-e2", "M1-EXTRA",
        "E3-LM0", "E3LM07", "e3-lm07", "E4-LM01",
        "1-2-3", "1.2.05.12.001", "12_34",
        "LAB MIC 01", "LAB--MIC", "-LAB01", "LAB01-", "LAB/MIC/01",
        "LAB-MÍC-01", "ÑANDU-01", "１２３４",
        "123456789012345678901", "ABCDEFGHIJKLMN", "", "   ",
    ],
)
def test_parse_code_rejects_invalid(code):
    with pytest.raises(ValueError):
        barcode.parse_code(code)


def test_parse_code_error_names_forbidden_characters():
    with pytest.raises(ValueError, match="espacio"):
        barcode.parse_code("LAB MIC 01")
    with pytest.raises(ValueError, match="Í"):
        barcode.parse_code("LAB-MÍC-01")


def test_parse_code_error_explains_reserved_prefixes_and_length_limits():
    with pytest.raises(ValueError, match="mesas de trabajo"):
        barcode.parse_code("m1-e2")
    with pytest.raises(ValueError, match="Lego"):
        barcode.parse_code("E3LM07")
    with pytest.raises(ValueError, match=str(barcode.ALNUM_MAX_LEN)):
        barcode.parse_code("ABCDEFGHIJKLMN")
    with pytest.raises(ValueError, match=str(barcode.NUMERIC_MAX_LEN)):
        barcode.parse_code("1" * 21)


def test_standard_code_level_detects_each_hierarchy_level():
    assert barcode.standard_code_level("2-1-01-00-000") == barcode.LEVEL_CONTAINER
    assert barcode.standard_code_level("2-1-01-01-000") == barcode.LEVEL_BOX
    assert barcode.standard_code_level("2-1-01-01-001") == barcode.LEVEL_ITEM
    assert barcode.standard_code_level("LAB-MIC-01") is None


def test_describe_standard_code_shows_na_instead_of_zero_placeholders():
    description = barcode.describe_parsed(barcode.parse_code("2-1-01-00-000"))
    assert "Contenedor 01" in description
    assert "Caja N/A" in description
    assert "Ítem N/A" in description


@pytest.mark.parametrize(
    "args, expected",
    [
        ((2, 1, 1, 0, 0), "2-1-01-00-000"),
        ((2, 1, 1, 1, 0), "2-1-01-01-000"),
        ((2, 1, 1, 1, 1), "2-1-01-01-001"),
        ((3, 6, 12, 25, 314), "3-6-12-25-314"),
    ],
)
def test_build_standard_code_is_canonical(args, expected):
    assert barcode.build_standard_code(*args) == expected


def test_build_standard_code_validates_inputs_and_hierarchy():
    with pytest.raises(ValueError, match="Estanteria"):
        barcode.build_standard_code(4, 1, 1, 0, 0)
    with pytest.raises(ValueError, match="Contenedor"):
        barcode.build_standard_code(1, 1, 0, 0, 0)
    with pytest.raises(ValueError, match="Item debe ser 000"):
        barcode.build_standard_code(1, 1, 1, 0, 1)
    with pytest.raises(ValueError, match="numero entero"):
        barcode.build_standard_code(1, 1, "uno", 0, 0)


def test_build_special_and_free_codes():
    assert barcode.build_mesa_code(1, 2) == "M1-E2"
    assert barcode.build_lego_code(7, 2) == "E3-LM07"
    assert barcode.build_numeric_code(12345, 7) == "0012345"
    assert barcode.build_prefixed_code("cont-lego", 1, 2) == "CONT-LEGO-01"
    assert barcode.build_prefixed_code("lab.mic", 4, 3, "_") == "LAB.MIC_004"


def test_builders_reject_values_that_do_not_fit_or_validate():
    with pytest.raises(ValueError, match="no cabe"):
        barcode.build_numeric_code(12345678, 7)
    with pytest.raises(ValueError, match="prefijo"):
        barcode.build_prefixed_code("LAB MIC", 1, 2)
    with pytest.raises(ValueError, match=str(barcode.ALNUM_MAX_LEN)):
        barcode.build_prefixed_code("ABCDEFGHIJK", 1, 2)
    with pytest.raises(ValueError, match="Mesa"):
        barcode.build_mesa_code(3, 1)


def test_help_explains_na_zero_convention():
    assert "2-1-01-00-000" in barcode.FORMAT_HELP
    assert "CAJA=00" in barcode.FORMAT_HELP
    assert "ITEM=000" in barcode.FORMAT_HELP


def test_detect_format_and_is_valid_code():
    assert barcode.detect_format("2-1-01-00-000") == barcode.FORMAT_STANDARD
    assert barcode.detect_format("M1-E2") == barcode.FORMAT_MESA
    assert barcode.detect_format("E3-LM07") == barcode.FORMAT_LEGO
    assert barcode.detect_format("0012345") == barcode.FORMAT_NUMERIC
    assert barcode.detect_format("LAB-MIC-01") == barcode.FORMAT_ALNUM
    assert barcode.detect_format("no valido") is None
    assert barcode.is_valid_code("LAB-MIC-01") is True
    assert barcode.is_valid_code("no valido") is False


def test_validate_code_format_returns_format_name():
    assert barcode.validate_code_format("2-1-01-00-000") == barcode.FORMAT_STANDARD
    assert barcode.validate_code_format("E3-LM07") == barcode.FORMAT_LEGO
    assert barcode.validate_code_format("LAB-MIC-01") == barcode.FORMAT_ALNUM
    assert barcode.validate_code_format("0012345") == barcode.FORMAT_NUMERIC
    with pytest.raises(ValueError):
        barcode.validate_code_format("no valido")


def test_describe_parsed_for_each_format():
    std = barcode.parse_code("1-2-05-12-001")
    assert "Estantería 1" in barcode.describe_parsed(std)
    assert "Ítem 001" in barcode.describe_parsed(std)
    assert "Mesa de trabajo 1" in barcode.describe_parsed(barcode.parse_code("M1-E2"))
    assert "Modelo 7" in barcode.describe_parsed(barcode.parse_code("E3-LM07"))
    assert "numérico libre" in barcode.describe_parsed(barcode.parse_code("0012345"))
    assert "alfanumérico libre" in barcode.describe_parsed(barcode.parse_code("LAB-MIC-01"))


def test_scan_exposes_parsed_components_for_valid_format(storage):
    storage.add_item("M1-E1", name="Impresora 3D", item_type="standalone", quantity=1)
    result = barcode.scan(storage, "M1-E1")
    assert result["status"] == "found_item"
    assert result["parsed"] == {"format": "mesa_trabajo", "mesa": 1, "equipo": 1}


@pytest.mark.parametrize(
    "code, fmt",
    [
        ("LAB-MIC-01", "alfanumerico_libre"),
        ("0012345", "numerico_libre"),
        ("2-1-01-00-000", "estandar_numerico"),
    ],
)
def test_scan_finds_codes_by_their_exact_text(storage, code, fmt):
    storage.add_item(code, name="Microscopio", item_type="standalone", quantity=2)
    result = barcode.scan(storage, code)
    assert result["status"] == "found_item"
    assert result["item"]["id"] == code
    assert result["parsed"]["format"] == fmt


def test_scan_does_not_drop_leading_zeros(storage):
    storage.add_item("0012345", name="Osciloscopio", quantity=1)
    assert barcode.scan(storage, "12345")["status"] == "not_found"


def test_scan_still_works_for_legacy_ids_without_parsed_info(storage):
    storage.add_item("CAJA 001", name="Caja vieja", item_type="master")
    result = barcode.scan(storage, "CAJA 001")
    assert result["status"] == "found_master"
    assert result["parsed"] is None


def test_scan_legacy_hyphenated_ids_now_parse_as_free_alphanumeric(storage):
    storage.add_item("CAJA-001", name="Caja vieja", item_type="master")
    result = barcode.scan(storage, "CAJA-001")
    assert result["status"] == "found_master"
    assert result["parsed"] == {"format": "alfanumerico_libre"}
