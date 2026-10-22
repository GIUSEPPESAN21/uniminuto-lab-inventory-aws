# -*- coding: utf-8 -*-
"""Recarga del Excel desde disco (lo que ocurre al reiniciarse la app).

Bug detectado: al recargar, las celdas vacias llegaban como NaN y editar
cualquier item fallaba con "'float' object has no attribute 'strip'", tras tres
reintentos con espera. Estas pruebas fijan el comportamiento correcto."""

import pytest

from core import storage as storage_module
from core.storage import LabStorage

ACTOR = "profesor@uniminuto.edu.co"
ADMIN = {"id": "admin-test", "role": "maestro", "institutional_email": "admin@uniminuto.edu.co"}


@pytest.fixture
def db():
    return LabStorage()


def _restart():
    """Equivale a reiniciar la app: la cache se descarta y se relee el archivo."""
    storage_module._cached_dfs = None


def test_editing_a_standalone_item_after_a_restart_works(db):
    db.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": 2},
                 "LAB-001", is_new=True, actor_email=ACTOR)
    _restart()
    item = db.get_item("LAB-001")
    db.save_item({**item, "name": "Multimetro Fluke", "quantity": 5}, "LAB-001",
                 is_new=False, actor_email=ACTOR)
    _restart()
    assert db.get_item("LAB-001")["name"] == "Multimetro Fluke"
    assert db.get_item("LAB-001")["quantity"] == 5


def test_editing_a_master_after_a_restart_works(db):
    db.save_item({"name": "Caja", "item_type": "master"}, "2-1-02-00-000", is_new=True, actor_email=ACTOR)
    _restart()
    db.save_item({"name": "Caja editada", "item_type": "master"}, "2-1-02-00-000",
                 is_new=False, actor_email=ACTOR)
    _restart()
    assert db.get_item("2-1-02-00-000")["name"] == "Caja editada"


def test_child_keeps_its_parent_across_restarts(db):
    db.save_item({"name": "Caja", "item_type": "master"}, "2-1-02-00-000", is_new=True, actor_email=ACTOR)
    db.save_item({"name": "Resistencias", "item_type": "child", "parent_id": "2-1-02-00-000", "quantity": 4},
                 "2-1-02-01-000", is_new=True, actor_email=ACTOR)
    _restart()
    child = db.get_item("2-1-02-01-000")
    assert child["parent_id"] == "2-1-02-00-000"
    db.save_item({**child, "quantity": 9}, "2-1-02-01-000", is_new=False, actor_email=ACTOR)
    assert [c["id"] for c in db.get_children("2-1-02-00-000")] == ["2-1-02-01-000"]


def test_empty_cells_load_as_empty_strings_not_nan(db):
    db.save_item({"name": "Suelto", "item_type": "standalone"}, "LAB-009", is_new=True, actor_email=ACTOR)
    _restart()
    cached = storage_module._load_cache()["items"]
    row = cached[cached["id"] == "LAB-009"].iloc[0]
    assert row["parent_id"] == "" and row["location"] == ""


@pytest.mark.parametrize("name", ["NA", "None", "null", "N/A"])
def test_text_that_looks_like_a_null_marker_is_preserved(db, name):
    """Antes, pandas convertia estos textos legitimos en nulo al releer el Excel."""
    db.save_item({"name": "Item", "item_type": "standalone", "category": name},
                 "LAB-001", is_new=True, actor_email=ACTOR)
    _restart()
    cached = storage_module._load_cache()["items"]
    assert cached[cached["id"] == "LAB-001"].iloc[0]["category"] == name


def test_user_and_whitelist_survive_a_restart(db):
    user = db.create_user("Ana Prueba", "ana@uniminuto.edu.co", "hash", "estudiante", "Ing", "ID-1")
    db.add_to_whitelist("prof@uniminuto.edu.co")
    _restart()
    assert db.get_user_by_email("ana@uniminuto.edu.co")["id"] == user["id"]
    assert db.is_email_whitelisted_professor("prof@uniminuto.edu.co")
    updated = db.update_user(user["id"], {"role": "profesor"}, actor=ADMIN)
    assert updated["role"] == "profesor"
