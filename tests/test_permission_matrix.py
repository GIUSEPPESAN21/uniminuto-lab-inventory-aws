# -*- coding: utf-8 -*-
"""Punto 8 de v1.15: matriz rol × página × acción.

1. Páginas del menú por rol (la app real, con login).
2. Cada vista llamada «a mano» (como si se escribiera su URL) se niega a quien no tiene la
   capacidad, y muestra su contenido a quien sí.
3. Acciones de dominio por capacidad y por rol (sin pasar por ninguna vista)."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth, dispatch, loans, permissions, service_requests  # noqa: E402
from core.storage import LabStorage  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
PASSWORD = "ClaveSegura123"
ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")

COMMON = {"inicio", "perfil", "acerca_de"}
CLIENT = COMMON | {"escanear", "solicitudes", "prestamos"}          # «escanear» es la página Buscar
STAFF = CLIENT | {"despacho", "trazabilidad"}                       # el cliente sigue su pedido en Solicitudes
PAGES = {
    "estudiante": CLIENT,
    "operario": COMMON | {"despacho"},
    "encargado": STAFF,
    "profesor": STAFF | {"inventario", "ubicaciones", "reportes", "nfc"},
    "maestro": STAFF | {"inventario", "ubicaciones", "reportes", "nfc", "usuarios"},
}
ALL_PAGES = set().union(*PAGES.values())


@pytest.fixture
def people():
    storage = LabStorage()
    hashed = auth.hash_password(PASSWORD)
    return {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", hashed, role)
            for role in ROLES}


@pytest.mark.parametrize("role", ROLES)
def test_the_menu_offers_exactly_the_pages_of_the_role(people, role):
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    at.text_input[0].input(f"{role}@uniminuto.edu.co")
    at.text_input[1].input(PASSWORD)
    at.button[0].click().run()
    assert not at.exception
    assert set(at.session_state["pages"]) == PAGES[role]


# vista -> (módulo, páginas donde se muestra con permiso). Las vistas «a mano» ignoran el menú.
def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    module = importlib.import_module(f"views.{st.session_state.test_view}")
    getattr(module, st.session_state.get("test_entry", "render"))()


VIEW_ENTRY = {"ubicaciones": "render_page"}
# «reservas» queda fuera a propósito: la v1.15 no toca Reservas; el menú no se la ofrece al operario.
GUARDED = ("escanear", "prestamos", "solicitudes", "trazabilidad", "despacho", "inventario",
           "ubicaciones", "reportes", "nfc", "usuarios")


def _open(view, person):
    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_view"] = view
    at.session_state["test_entry"] = VIEW_ENTRY.get(view, "render")
    at.run()
    assert not at.exception
    return at


@pytest.mark.parametrize("view", GUARDED)
def test_every_view_refuses_a_hand_typed_visit_without_the_capability(people, view):
    for role in ROLES:
        at = _open(view, people[role])
        denied = any("No tienes permiso" in str(e.value) for e in at.error)
        assert denied == (view not in PAGES[role]), (view, role)


def test_the_page_table_covers_every_registered_page_and_nothing_else():
    assert ALL_PAGES == {"inicio", "escanear", "solicitudes", "prestamos", "trazabilidad", "despacho",
                         "inventario", "ubicaciones", "reportes", "nfc", "usuarios", "perfil", "acerca_de"}


# --- acciones de dominio por rol -------------------------------------------------------

_CLIENT_AND_ABOVE = {"estudiante", "encargado", "profesor", "maestro"}
_SUPERVISORS = {"encargado", "profesor", "maestro"}
_SUPERVISOR_PROFESSOR = {"profesor", "maestro"}

# Escrita a mano y completa: una capacidad nueva sin su fila hace fallar
# test_the_table_lists_every_capability, así que cada una se decide a propósito.
CAPABILITY_TABLE = {
    # cliente
    permissions.CAP_REQUEST: _CLIENT_AND_ABOVE,
    permissions.CAP_VIEW_OWN_REQUESTS: _CLIENT_AND_ABOVE,
    permissions.CAP_CANCEL_OWN_REQUEST: _CLIENT_AND_ABOVE,
    permissions.CAP_CONFIRM_RECEIPT: _CLIENT_AND_ABOVE,
    permissions.CAP_RESERVE: _CLIENT_AND_ABOVE,
    permissions.CAP_CHECKOUT: _CLIENT_AND_ABOVE,
    # operario (despacho)
    permissions.CAP_VIEW_DISPATCH_QUEUE: {"operario"} | _SUPERVISORS,      # los supervisores también hacen picking
    permissions.CAP_CLAIM: {"operario"} | _SUPERVISORS,
    permissions.CAP_PICK: {"operario"} | _SUPERVISORS,
    permissions.CAP_RELEASE: {"operario"} | _SUPERVISORS,
    permissions.CAP_STORE_RETURN: {"operario"} | _SUPERVISORS,             # guardan lo que el cliente devolvió
    # supervisor
    permissions.CAP_APPROVE: _SUPERVISORS,
    permissions.CAP_AUTHORIZE_DISPATCH: _SUPERVISORS,
    permissions.CAP_RECEIVE_AND_DELIVER: _SUPERVISORS,
    permissions.CAP_REASSIGN: _SUPERVISORS,
    permissions.CAP_VERIFY_RETURN: _SUPERVISORS,                           # verifican estado y cantidad devueltos
    permissions.CAP_VIEW_ALL_TRACES: _SUPERVISORS,
    permissions.CAP_DISPATCH_REPORTS: _SUPERVISORS,
    # supervisor con inventario (profesor)
    permissions.CAP_EDIT_INVENTORY: _SUPERVISOR_PROFESSOR,
    permissions.CAP_MANAGE_NFC: _SUPERVISOR_PROFESSOR,
    permissions.CAP_MANAGE_LOCATIONS: _SUPERVISOR_PROFESSOR,
    permissions.CAP_REPORTS: _SUPERVISOR_PROFESSOR,
    permissions.CAP_MANAGE_RESERVATIONS: _SUPERVISOR_PROFESSOR,
    permissions.CAP_VIEW_ALL_LOANS: _SUPERVISOR_PROFESSOR,
    permissions.CAP_CHECKIN_ANY: _SUPERVISOR_PROFESSOR,
    # administrador
    permissions.CAP_MANAGE_USERS: {"maestro"},
    permissions.CAP_MANAGE_EMAIL: {"maestro"},
    permissions.CAP_ADJUST_COUNTS: {"maestro"},
    permissions.CAP_DELETE_PHOTOS: {"maestro"},
    permissions.CAP_DISPATCH_SETTINGS: {"maestro"},
    permissions.CAP_RESOLVE_SYNC: {"maestro"},
    permissions.CAP_PURGE_DATA: {"maestro"},
}


def test_the_table_lists_every_capability():
    defined = {value for name, value in vars(permissions).items() if name.startswith("CAP_")}
    assert defined == set(CAPABILITY_TABLE), (
        f"sin fila: {sorted(defined - set(CAPABILITY_TABLE))}; sobrantes: {sorted(set(CAPABILITY_TABLE) - defined)}"
    )


def test_no_role_holds_a_capability_the_table_does_not_grant_it():
    for role in ROLES:
        granted = {cap for cap, holders in CAPABILITY_TABLE.items() if role in holders}
        assert set(permissions.ROLE_CAPS[role]) == granted, role


@pytest.mark.parametrize("capability", sorted(CAPABILITY_TABLE))
def test_capability_table(capability):
    holders = {role for role in ROLES if permissions.has_cap({"role": role}, capability)}
    assert holders == CAPABILITY_TABLE[capability], capability


def test_unknown_roles_and_missing_users_have_no_capability():
    assert not permissions.has_cap(None, permissions.CAP_REQUEST)
    assert not permissions.has_cap({"role": "intruso"}, permissions.CAP_REQUEST)
    with pytest.raises(PermissionError):
        permissions.require_cap({"role": "intruso"}, permissions.CAP_REQUEST)


def test_domain_actions_enforce_the_capability_even_without_a_view(people):
    storage = LabStorage()
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 5}, "LAB-MIC-01", is_new=True)
    with pytest.raises(PermissionError):                                  # el operario no solicita
        service_requests.submit_request(storage, people["operario"], "product", "LAB-MIC-01", 1)
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", "LAB-MIC-01", 1)
    for role in ("estudiante", "operario"):                               # ni el cliente ni el operario aprueban
        ok, message = service_requests.review_request(storage, row["id"], "approved", people[role])
        assert not ok and "permiso" in message
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    with pytest.raises(PermissionError):                                  # el cliente no toma pedidos
        dispatch.claim_request(storage, row["id"], people["estudiante"])
    with pytest.raises(PermissionError):                                  # el cliente no ve la cola
        dispatch.queue_for(storage, people["estudiante"])
    with pytest.raises(PermissionError):                                  # el operario no recibe ni entrega
        dispatch.receive_request(storage, row["id"], people["operario"])
    with pytest.raises(PermissionError):                                  # ni escanea salidas
        loans.checkout(storage, "LAB-MIC-01", 1, people["operario"])
    with pytest.raises(PermissionError):                                  # el supervisor no fija el escenario
        dispatch.set_scenario(storage, people["encargado"], "A")
