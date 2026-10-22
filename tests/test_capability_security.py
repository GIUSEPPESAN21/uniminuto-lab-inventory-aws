# -*- coding: utf-8 -*-
"""Punto 1 de v1.15: seguridad por capacidad (no por nombre de rol).

Cubre el dominio (core.loans), la capa de datos (LabStorage con ``actor``), las
vistas reales (AppTest) y un recorrido del código que impide volver a escribir
``role == "estudiante"`` o llamar a un mutador sin pasar el actor.
"""

import ast
import re
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import barcode, permissions  # noqa: E402
from core import loans as loans_core  # noqa: E402
from core.storage import LabStorage  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")
SYSTEM = None  # actor=None: llamador interno de confianza (arranque, scripts, pruebas de datos)


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {
        role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role)
        for role in ROLES
    }
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 5}, "LAB-MIC-01",
                      is_new=True, actor_email="seed")
    return storage, people


def _loan_for(storage, borrower):
    ok, message, loan = loans_core.checkout(storage, "LAB-MIC-01", 1, borrower)
    assert ok, message
    return loan


# --- dominio y capa de datos: préstamos ------------------------------------------------

def test_roles_split_loan_capabilities_as_specified():
    for role in ("estudiante", "encargado", "profesor", "maestro"):
        assert permissions.has_cap({"role": role}, permissions.CAP_CHECKOUT)
    assert not permissions.has_cap({"role": "operario"}, permissions.CAP_CHECKOUT)
    for role in ("profesor", "maestro"):
        assert permissions.has_cap({"role": role}, permissions.CAP_VIEW_ALL_LOANS)
        assert permissions.has_cap({"role": role}, permissions.CAP_CHECKIN_ANY)
    for role in ("estudiante", "operario", "encargado"):
        assert not permissions.has_cap({"role": role}, permissions.CAP_VIEW_ALL_LOANS)
        assert not permissions.has_cap({"role": role}, permissions.CAP_CHECKIN_ANY)


def test_supervisor_encargado_cannot_return_a_loan_that_is_not_theirs(lab):
    storage, people = lab
    loan = _loan_for(storage, people["estudiante"])
    with pytest.raises(PermissionError):
        loans_core.checkin(storage, loan["id"], people["encargado"])
    with pytest.raises(PermissionError):                      # la capa de datos también la rechaza
        storage.return_loan(loan["id"], actor=people["encargado"])
    assert storage.get_open_loans_for_user(people["estudiante"]["id"])   # sigue abierto


def test_operator_cannot_checkout_or_checkin(lab):
    storage, people = lab
    loan = _loan_for(storage, people["estudiante"])
    with pytest.raises(PermissionError):
        loans_core.checkout(storage, "LAB-MIC-01", 1, people["operario"])
    with pytest.raises(PermissionError):
        loans_core.checkin(storage, loan["id"], people["operario"])


def test_owner_cannot_check_in_directly_but_professor_and_admin_can(lab):
    storage, people = lab
    own = _loan_for(storage, people["encargado"])
    with pytest.raises(PermissionError):                       # el dueño devuelve por core.returns
        loans_core.checkin(storage, own["id"], people["encargado"])
    with pytest.raises(PermissionError):
        storage.return_loan(own["id"], actor=people["encargado"])
    for role in ("profesor", "maestro"):
        loan = _loan_for(storage, people["estudiante"])
        assert loans_core.checkin(storage, loan["id"], people[role])[0]               # ajeno, con capacidad


def test_loans_visible_by_capability(lab):
    storage, people = lab
    _loan_for(storage, people["estudiante"])
    _loan_for(storage, people["encargado"])
    assert len(loans_core.get_loans_for_view(storage, people["encargado"])) == 1
    assert len(loans_core.get_loans_for_view(storage, people["estudiante"])) == 1
    assert len(loans_core.get_loans_for_view(storage, people["profesor"])) == 2
    assert loans_core.can_checkin_loan(people["profesor"], {"user_id": "x"})
    assert not loans_core.can_checkin_loan(people["encargado"], {"user_id": "x"})


# --- capa de datos: inventario y usuarios ------------------------------------------------

def test_storage_requires_edit_inventory_when_an_actor_is_given(lab):
    storage, people = lab
    data = {"name": "Nuevo", "item_type": "standalone", "quantity": 1}
    for role in ("estudiante", "operario", "encargado"):
        with pytest.raises(PermissionError):
            storage.save_item(data, "LAB-NEW-01", is_new=True, actor=people[role])
        with pytest.raises(PermissionError):
            storage.bulk_upsert_items([{"id": "LAB-NEW-02", "name": "x"}], actor=people[role])
        with pytest.raises(PermissionError):
            storage.delete_item("LAB-MIC-01", actor=people[role])
        with pytest.raises(PermissionError):
            storage.retire_item("LAB-MIC-01", actor=people[role])
    assert storage.get_item("LAB-NEW-01") is None
    storage.save_item(data, "LAB-NEW-01", is_new=True, actor=people["profesor"])
    assert storage.get_item("LAB-NEW-01")["name"] == "Nuevo"


def test_creating_locations_requires_manage_locations(lab):
    storage, people = lab
    payload = [{"id": "SALA-A", "name": "Sala A", "item_type": "location"}]
    with pytest.raises(PermissionError):
        storage.save_items_bulk(payload, actor=people["encargado"])
    storage.save_items_bulk(payload, actor=people["profesor"])
    assert storage.get_item("SALA-A")["item_type"] == "location"


def test_changing_role_or_status_needs_an_authorized_actor(lab):
    storage, people = lab
    target = people["estudiante"]["id"]
    with pytest.raises(PermissionError):                                  # sin actor
        storage.update_user(target, {"role": "profesor"})
    with pytest.raises(PermissionError):
        storage.update_user(target, {"status": "disabled"})
    for role in ("estudiante", "operario", "encargado", "profesor"):      # actor sin manage_users
        with pytest.raises(PermissionError):
            storage.update_user(target, {"role": "maestro"}, actor=people[role])
    with pytest.raises(PermissionError):                                  # nadie se asciende solo
        storage.update_user(people["maestro"]["id"], {"role": "estudiante"}, actor=people["maestro"])
    assert storage.get_user_by_id(target)["role"] == "estudiante"
    storage.update_user(target, {"role": "operario"}, actor=people["maestro"])
    assert storage.get_user_by_id(target)["role"] == "operario"


def test_editing_another_person_needs_manage_users_but_self_edit_is_allowed(lab):
    storage, people = lab
    with pytest.raises(PermissionError):
        storage.update_user(people["estudiante"]["id"], {"full_name": "Otro"}, actor=people["profesor"])
    storage.update_user(people["profesor"]["id"], {"full_name": "Profe Nuevo"}, actor=people["profesor"])
    storage.update_user(people["estudiante"]["id"], {"full_name": "Corregido"}, actor=people["maestro"])
    assert storage.get_user_by_id(people["estudiante"]["id"])["full_name"] == "Corregido"


def test_permission_errors_are_not_retried(lab, monkeypatch):
    """PermissionError es OSError: sin excluirlo, with_retry dormiría 1 s + 2 s por intento."""
    import time

    storage, people = lab
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    with pytest.raises(PermissionError):
        storage.save_item({"name": "x", "item_type": "standalone"}, "LAB-X-01", is_new=True,
                          actor=people["encargado"])
    assert sleeps == []


# --- vistas reales -----------------------------------------------------------------------

def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    importlib.import_module(f"views.{st.session_state.test_view}").render()


def _open(view, person, **state):
    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_view"] = view
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    assert not at.exception
    return at


def _text(elements) -> str:
    return " ".join(str(e.value) for e in elements)


def test_scan_view_hides_other_peoples_loans_and_returns_from_the_encargado(lab):
    storage, people = lab
    _loan_for(storage, people["estudiante"])
    scan = barcode.scan(storage, "LAB-MIC-01")
    at = _open("escanear", people["encargado"], scan_result=scan)
    assert not [b for b in at.button if b.label == "Reingresar"]
    assert "No hay prestamos abiertos" in _text(at.info)
    prof = _open("escanear", people["profesor"], scan_result=scan)
    assert [b for b in prof.button if b.label == "Reingresar"]


def test_scan_view_does_not_offer_creating_items_to_the_encargado(lab):
    storage, people = lab
    scan = barcode.scan(storage, "LAB-NOPE-1")
    assert scan["status"] == "not_found"
    for role in ("estudiante", "encargado"):
        at = _open("escanear", people[role], scan_result=scan)
        assert "Pide a un supervisor" in _text(at.info)
        assert not [b for b in at.button if "Registrar item" in b.label]
    prof = _open("escanear", people["profesor"], scan_result=scan)
    assert [b for b in prof.button if "Registrar item" in b.label]


def test_loans_page_is_own_loans_only_for_the_encargado(lab):
    storage, people = lab
    _loan_for(storage, people["estudiante"])
    mine = _loan_for(storage, people["encargado"])
    at = _open("prestamos", people["encargado"])
    text = _text(at.markdown)
    assert "Mis préstamos" in text and "Persona estudiante" not in text
    assert [b for b in at.button if "Devolver" in b.label]                 # el suyo, por el flujo de devolución
    assert len([b for b in at.button if "Devolver" in b.label]) == 1
    assert not [b for b in at.button if "Reingresar" in b.label]           # sin reingreso directo
    assert mine["id"]
    prof = _open("prestamos", people["profesor"])
    assert "Persona estudiante" in _text(prof.markdown) and prof.tabs


def test_operator_is_blocked_from_scan_and_loans_pages(lab):
    storage, people = lab
    for view in ("escanear", "prestamos"):
        at = _open(view, people["operario"])
        assert "No tienes permiso" in _text(at.error)


def test_operator_home_shows_orders_not_inventory(lab):
    storage, people = lab
    at = _open("inicio", people["operario"])
    html = _text(at.markdown)
    assert "Pedidos por tomar" in html and "Mis pedidos" in html
    assert "Items en catálogo" not in html and "Usuarios registrados" not in html


# --- recorridos del código ---------------------------------------------------------------

def _python_files():
    for folder in ("core", "views"):
        yield from (ROOT / folder).glob("*.py")
    yield ROOT / "app.py"


def test_no_code_decides_by_the_literal_student_role():
    pattern = re.compile(r"""role["']?\]?\)?\s*[!=]=\s*["']estudiante["']""")
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _python_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


_MUTATORS = {
    "save_item", "save_items_bulk", "bulk_upsert_items", "delete_item", "retire_item", "return_loan",
    "add_to_whitelist", "remove_from_whitelist", "resolve_sync_conflict",
}


def test_every_application_call_to_a_mutator_passes_the_actor():
    missing = []
    for path in _python_files():
        if path.name == "storage.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _MUTATORS
                    and "actor" not in {kw.arg for kw in node.keywords}):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.func.attr}")
    assert missing == []


def test_role_changes_in_the_application_always_pass_the_actor():
    missing = []
    for path in _python_files():
        if path.name == "storage.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "update_user" and len(node.args) >= 2):
                continue
            changes = node.args[1]
            keys = {k.value for k in changes.keys if isinstance(k, ast.Constant)} if isinstance(changes, ast.Dict) else set()
            if {"role", "status"} & keys and "actor" not in {kw.arg for kw in node.keywords}:
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert missing == []
