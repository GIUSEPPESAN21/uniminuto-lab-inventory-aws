# -*- coding: utf-8 -*-
"""Pruebas de core/storage: los items NUEVOS deben cumplir un formato de
core/barcode.py; las ediciones heredadas no se revalidan. Incluye guardado y
recarga reales en un Excel temporal para comprobar que los codigos se
conservan exactamente, incluidos ceros de relleno y niveles no aplicables."""

import pandas as pd
import pytest

from core import storage as storage_module
from core.storage import SHEET_COLUMNS, _validate_item_data

EMPTY_ITEMS = pd.DataFrame(columns=SHEET_COLUMNS["items"])
STANDALONE = {"item_type": "standalone", "parent_id": ""}


@pytest.mark.parametrize(
    "code",
    ["1-2-05-12-001", "2-1-01-00-000", "2-1-01-01-000", "M1-E2"],
)
def test_new_item_with_valid_structured_code_is_accepted(code):
    _validate_item_data(STANDALONE, EMPTY_ITEMS, code, is_new=True)


@pytest.mark.parametrize("code", ["0012345", "LAB-MIC-01", "CAJA-001"])
def test_new_item_with_free_code_is_accepted(code):
    _validate_item_data(STANDALONE, EMPTY_ITEMS, code, is_new=True)


@pytest.mark.parametrize(
    "code", ["LAB MIC 01", "LAB-MÍC-01", "M3-E1", "2-1-00-00-000", "2-1-01-00-001"]
)
def test_new_item_with_invalid_code_is_rejected(code):
    with pytest.raises(ValueError):
        _validate_item_data(STANDALONE, EMPTY_ITEMS, code, is_new=True)


def test_editing_existing_item_does_not_revalidate_legacy_code():
    _validate_item_data(STANDALONE, EMPTY_ITEMS, "CAJA 001", is_new=False)


@pytest.fixture
def excel_storage(tmp_path, monkeypatch):
    """LabStorage real sobre un Excel temporal y sin GitHub."""
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(tmp_path / "lab_db_test.xlsx"))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    monkeypatch.setattr(storage_module, "_is_github_configured", lambda: False)
    return storage_module.LabStorage()


@pytest.mark.parametrize(
    "code,item_type",
    [
        ("0012345", "standalone"),
        ("LAB-MIC-01", "standalone"),
        ("2-1-01-00-000", "master"),
        ("2-1-01-01-000", "child"),
    ],
)
def test_codes_are_saved_and_reloaded_exactly_from_excel(excel_storage, code, item_type):
    # Los child reales exigen parent_id; para esta prueba de serializacion se
    # usa standalone, pues la validacion de la relacion padre tiene sus propias pruebas.
    stored_type = "standalone" if item_type == "child" else item_type
    excel_storage.save_item(
        {"name": "Elemento", "item_type": stored_type, "quantity": 2},
        code, is_new=True, actor_email="profesor@uniminuto.edu.co",
    )
    storage_module._cached_dfs = None

    item = excel_storage.get_item(code)
    assert item is not None
    assert item["id"] == code
    assert item["name"] == "Elemento"


def test_numeric_code_keeps_leading_zeros_after_reload(excel_storage):
    excel_storage.save_item(
        {"name": "Osciloscopio", "item_type": "standalone", "quantity": 1},
        "0012345", is_new=True, actor_email="profesor@uniminuto.edu.co",
    )
    storage_module._cached_dfs = None

    assert excel_storage.get_item("12345") is None
    assert excel_storage.get_item("0012345")["id"] == "0012345"
