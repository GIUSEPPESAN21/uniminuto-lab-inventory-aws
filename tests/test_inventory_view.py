# -*- coding: utf-8 -*-
"""Interfaz de eliminacion definitiva (views/inventario.py) con la app real:
confirmacion obligatoria, bloqueo por prestamos abiertos y recreacion del codigo."""

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import loans as loans_core  # noqa: E402
from core.storage import LabStorage  # noqa: E402

CODE = "2-1-01-00-000"
ACTOR = "prof@uniminuto.edu.co"


def _edit_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inventario

    storage = LabStorage()
    st.session_state.storage = storage
    st.session_state.user = {
        "id": "p1", "role": "profesor", "full_name": "Prof Uno",
        "institutional_email": "prof@uniminuto.edu.co",
    }
    inventario.render()


@pytest.fixture
def db():
    storage = LabStorage()
    storage.save_item({"name": "Contenedor 1", "item_type": "master", "quantity": 0},
                      CODE, is_new=True, actor_email=ACTOR)
    return storage


def _open_editor():
    at = AppTest.from_function(_edit_script)
    at.session_state["editing_item_id"] = CODE
    at.run()
    assert not at.exception
    return at


def _delete_button(at):
    return next(b for b in at.button if "Eliminar definitivamente" in b.label)


def test_editor_warns_that_deleting_is_permanent(db):
    at = _open_editor()
    captions = " ".join(c.value for c in at.caption)
    assert "permanente" in captions and "queda libre" in captions
    assert not any("Dar de baja" in b.label for b in at.button)   # ya no hay baja logica en la UI


def test_delete_requires_the_confirmation_checkbox(db):
    at = _open_editor()
    _delete_button(at).click().run()
    assert any("casilla de confirmación" in w.value for w in at.warning)
    assert db.get_item(CODE) is not None


def test_confirmed_delete_removes_the_item_for_good_and_frees_the_code(db):
    at = _open_editor()
    [box for box in at.checkbox if "eliminará" in box.label][0].check()
    _delete_button(at).click().run()
    assert not at.exception

    assert db.get_item(CODE) is None
    assert at.session_state["editing_item_id"] is None
    # y el codigo se puede volver a registrar (el bug original):
    db.save_item({"name": "Contenedor 1 nuevo", "item_type": "master", "quantity": 0},
                 CODE, is_new=True, actor_email=ACTOR)
    assert db.get_item(CODE)["name"] == "Contenedor 1 nuevo"


def test_delete_is_refused_while_the_item_has_open_loans(db):
    db.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": 3},
                 "LAB-001", is_new=True, actor_email=ACTOR)
    student = db.create_user("Ana Prueba", "ana@uniminuto.edu.co", "hash", "estudiante", "Ing", "ID-1")
    assert loans_core.checkout(db, "LAB-001", 1, student)[0]

    at = AppTest.from_function(_edit_script)
    at.session_state["editing_item_id"] = "LAB-001"
    at.run()
    assert any("préstamo(s) abierto(s)" in c.value for c in at.caption)
    [box for box in at.checkbox if "eliminará" in box.label][0].check()
    _delete_button(at).click().run()
    assert any("prestamos abiertos" in e.value for e in at.error)
    assert db.get_item("LAB-001") is not None


def test_catalog_lists_items_with_batch_availability(db):
    db.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": 4},
                 "LAB-001", is_new=True, actor_email=ACTOR)
    at = AppTest.from_function(_edit_script)
    at.run()
    assert not at.exception
    assert any("2 item(s) encontrados" in c.value for c in at.caption)
