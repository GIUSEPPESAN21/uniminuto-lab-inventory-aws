# -*- coding: utf-8 -*-
"""Eliminacion DEFINITIVA de items y reutilizacion de codigos.

Bug original: "eliminar" un item solo lo marcaba `retired` pero dejaba su fila en
el Excel, asi que volver a registrar el mismo codigo fallaba con "Ya existe un
item con ese codigo". Ahora `delete_item` borra el item, su historial y sus
prestamos devueltos, y un codigo de un item dado de baja queda libre."""

import pytest

from core import loans as loans_core
from core import storage as storage_module
from core.storage import LabStorage

ACTOR = "profesor@uniminuto.edu.co"
PROFESSOR = {"id": "prof-1", "role": "profesor", "full_name": "Profe Prueba", "institutional_email": "profe@uniminuto.edu.co"}          # quien reingresa directo (el cliente devuelve por core.returns)
MASTER_CODE = "2-1-01-00-000"
CHILD_CODE = "2-1-01-01-000"


@pytest.fixture
def db():
    return LabStorage()


@pytest.fixture
def student(db):
    return db.create_user("Ana Prueba", "ana@uniminuto.edu.co", "hash", "estudiante", "Ing", "ID-1")


def _add(db, code, item_type="standalone", parent_id="", quantity=3, name="Item"):
    db.save_item(
        {"name": name, "item_type": item_type, "parent_id": parent_id, "quantity": quantity},
        code, is_new=True, actor_email=ACTOR,
    )


def _reload_from_disk():
    """Fuerza a leer el Excel real (descarta la cache en memoria)."""
    storage_module._cached_dfs = None


def test_deleted_code_can_be_registered_again(db):
    _add(db, MASTER_CODE, "master", name="Contenedor 1")
    ok, message = db.delete_item(MASTER_CODE, ACTOR)
    assert ok and "definitivamente" in message

    _add(db, MASTER_CODE, "master", name="Contenedor 1 nuevo")  # antes: "ya existe"
    assert db.get_item(MASTER_CODE)["name"] == "Contenedor 1 nuevo"


def test_delete_is_permanent_on_disk(db):
    _add(db, "LAB-001")
    assert db.delete_item("LAB-001", ACTOR)[0]
    _reload_from_disk()
    assert db.get_item("LAB-001") is None
    assert db.get_all_items(include_retired=True) == []


def test_delete_removes_history_and_returned_loans(db, student):
    _add(db, "LAB-001", quantity=5)
    ok, _, loan = loans_core.checkout(db, "LAB-001", 2, student)
    assert ok
    assert loans_core.checkin(db, loan["id"], PROFESSOR)[0]
    assert db.get_item_history("LAB-001")

    assert db.delete_item("LAB-001", ACTOR)[0]
    assert db.get_item_history("LAB-001") == []
    assert db.get_all_loans() == []


def test_recreated_code_does_not_inherit_old_history_or_loans(db, student):
    _add(db, "LAB-001", quantity=5)
    _, _, loan = loans_core.checkout(db, "LAB-001", 1, student)
    loans_core.checkin(db, loan["id"], PROFESSOR)
    db.delete_item("LAB-001", ACTOR)

    _add(db, "LAB-001", quantity=7)
    history = db.get_item_history("LAB-001")
    assert [row["type"] for row in history] == ["Alta"]
    assert db.get_all_loans() == []
    assert db.get_available_quantity("LAB-001") == 7


def test_delete_master_cascades_to_children(db):
    _add(db, MASTER_CODE, "master", quantity=0, name="Caja")
    _add(db, CHILD_CODE, "child", parent_id=MASTER_CODE, name="Resistencias")
    _add(db, "LAB-001", name="Suelto")

    ok, message = db.delete_item(MASTER_CODE, ACTOR)
    assert ok and "1 item(s) que contenia" in message
    assert db.get_item(MASTER_CODE) is None
    assert db.get_item(CHILD_CODE) is None
    assert db.get_item("LAB-001") is not None  # lo demas no se toca


def test_delete_child_keeps_parent(db):
    _add(db, MASTER_CODE, "master", quantity=0)
    _add(db, CHILD_CODE, "child", parent_id=MASTER_CODE)
    assert db.delete_item(CHILD_CODE, ACTOR)[0]
    assert db.get_item(MASTER_CODE) is not None
    assert db.get_children(MASTER_CODE) == []


def test_delete_is_blocked_by_open_loans_and_changes_nothing(db, student):
    _add(db, "LAB-001", quantity=5)
    ok, _, _ = loans_core.checkout(db, "LAB-001", 2, student)
    assert ok

    deleted, message = db.delete_item("LAB-001", ACTOR)
    assert not deleted and "prestamos abiertos" in message
    assert db.get_item("LAB-001") is not None
    assert db.get_available_quantity("LAB-001") == 3


def test_delete_blocked_when_a_child_has_open_loans(db, student):
    _add(db, MASTER_CODE, "master", quantity=0)
    _add(db, CHILD_CODE, "child", parent_id=MASTER_CODE, quantity=4)
    loans_core.checkout(db, CHILD_CODE, 1, student)
    assert not db.delete_item(MASTER_CODE, ACTOR)[0]
    assert db.get_item(CHILD_CODE) is not None


def test_delete_missing_item_reports_error(db):
    ok, message = db.delete_item("NO-EXISTE", ACTOR)
    assert not ok and "no existe" in message.lower()


def test_delete_cancels_open_product_requests_but_not_closed_ones(db, student):
    _add(db, "LAB-001", quantity=5, name="Multimetro")

    def request(status):
        row = db.create_service_request(
            {"request_type": "product", "item_id": "LAB-001", "item_name": "Multimetro",
             "quantity": 1, "service_name": "", "description": "", "needed_at": ""},
            student,
        )
        if status != "pending":
            db.update_service_request_status(row["id"], status, ACTOR, "")
        return row["id"]

    pending, approved, rejected = request("pending"), request("approved"), request("rejected")
    assert db.delete_item("LAB-001", ACTOR)[0]

    assert db.get_service_request(pending)["status"] == "cancelled"
    assert db.get_service_request(approved)["status"] == "cancelled"
    assert db.get_service_request(rejected)["status"] == "rejected"
    assert "eliminado" in db.get_service_request(pending)["review_notes"].lower()


def test_delete_impact_summarizes_what_will_be_lost(db, student):
    _add(db, MASTER_CODE, "master", quantity=0)
    _add(db, CHILD_CODE, "child", parent_id=MASTER_CODE, quantity=4)
    _, _, loan = loans_core.checkout(db, CHILD_CODE, 1, student)
    loans_core.checkin(db, loan["id"], PROFESSOR)
    loans_core.checkout(db, CHILD_CODE, 1, student)

    impact = db.get_delete_impact(MASTER_CODE)
    assert impact == {
        "exists": True, "contained_items": 1, "open_loans": 1,
        "closed_loans": 1, "open_requests": 0,
    }
    assert db.get_delete_impact("NO-EXISTE")["exists"] is False


# --- Items dados de baja (retire_item) antes de esta version -----------------

def test_retired_code_can_be_registered_again(db):
    """Caso real reportado: el item quedo `retired` y su codigo bloqueado."""
    _add(db, MASTER_CODE, "master", quantity=0, name="Contenedor 1")
    assert db.retire_item(MASTER_CODE, ACTOR)[0]
    assert db.item_code_in_use(MASTER_CODE) is False

    _add(db, MASTER_CODE, "master", quantity=0, name="Contenedor 1 nuevo")
    item = db.get_item(MASTER_CODE)
    assert item["name"] == "Contenedor 1 nuevo" and item["status"] == "active"
    assert [row["type"] for row in db.get_item_history(MASTER_CODE)] == ["Alta"]


def test_active_code_cannot_be_registered_twice(db):
    _add(db, "LAB-001")
    assert db.item_code_in_use("LAB-001") is True
    with pytest.raises(ValueError, match="Ya existe"):
        _add(db, "LAB-001")


def test_bulk_import_over_a_retired_code_creates_a_fresh_item(db):
    _add(db, "LAB-001", name="Viejo")
    db.retire_item("LAB-001", ACTOR)
    result = db.bulk_upsert_items(
        [{"id": "LAB-001", "name": "Nuevo", "item_type": "standalone", "quantity": "2"}], ACTOR
    )
    assert result["created"] == ["LAB-001"] and result["updated"] == []
    assert db.get_item("LAB-001")["name"] == "Nuevo"
    assert [row["type"] for row in db.get_item_history("LAB-001")] == ["Alta"]


def test_retired_master_with_retired_children_is_fully_purged_on_reuse(db):
    _add(db, MASTER_CODE, "master", quantity=0)
    _add(db, CHILD_CODE, "child", parent_id=MASTER_CODE)
    db.retire_item(MASTER_CODE, ACTOR)
    _add(db, MASTER_CODE, "master", quantity=0, name="Nuevo master")
    assert db.get_item(CHILD_CODE) is None  # el hijo retirado no reaparece enlazado


def test_availability_map_matches_per_item_quantity(db, student):
    _add(db, "LAB-001", quantity=5)
    _add(db, "LAB-002", quantity=2)
    loans_core.checkout(db, "LAB-001", 3, student)
    availability = db.get_availability_map()
    assert availability == {
        "LAB-001": db.get_available_quantity("LAB-001"),
        "LAB-002": db.get_available_quantity("LAB-002"),
    }
    assert availability["LAB-001"] == 2


def test_every_write_is_on_disk_immediately(db):
    """'Los cambios se guardan al momento': tras cada operacion el archivo ya
    refleja el estado, incluso si la cache en memoria se pierde."""
    _add(db, "LAB-001", quantity=1)
    _reload_from_disk()
    assert db.get_item("LAB-001")["quantity"] == 1

    db.save_item({"name": "Item", "item_type": "standalone", "quantity": 9}, "LAB-001",
                 is_new=False, actor_email=ACTOR)
    _reload_from_disk()
    assert db.get_item("LAB-001")["quantity"] == 9

    db.delete_item("LAB-001", ACTOR)
    _reload_from_disk()
    assert db.get_item("LAB-001") is None


def test_isolated_environment_never_touches_the_repo_root():
    assert "isolated_db" in storage_module.EXCEL_PATH
    assert not storage_module.EXCEL_PATH.startswith("UNIMINUTO")

