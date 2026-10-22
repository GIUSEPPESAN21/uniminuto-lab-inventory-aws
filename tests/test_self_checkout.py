# -*- coding: utf-8 -*-
"""v1.16 · modo de retiro por autoservicio (libre / solo con solicitud aprobada / apagado).

Por omisión nada cambia respecto a v1.15; las otras dos opciones limitan solo a quien no
aprueba solicitudes (los clientes), y la regla vive en el dominio, no solo en la pantalla."""

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import barcode, permissions, service_requests, traceability  # noqa: E402
from core import loans as loans_core  # noqa: E402
from core.storage import LabStorage  # noqa: E402

ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")
CODE = "LAB-MIC-01"


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role) for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 10, "location": "Gabinete"},
                      CODE, is_new=True, actor_email="seed")
    return storage, people


def _mode(storage, people, mode):
    ok, message = loans_core.set_self_checkout_mode(storage, people["maestro"], mode)
    assert ok, message


def _approved(storage, people, quantity=2, owner="estudiante"):
    row, _, _ = service_requests.submit_request(storage, people[owner], "product", CODE, quantity)
    ok, message = service_requests.review_request(storage, row["id"], "approved", people["encargado"])
    assert ok, message
    return row


def _open_loans(storage):
    return storage.get_all_loans(status="out")


# --- el ajuste ---------------------------------------------------------------------------------

def test_default_is_free_and_unknown_values_fall_back_to_free(lab):
    storage, people = lab
    assert loans_core.self_checkout_mode(storage) == loans_core.MODE_FREE
    storage.set_setting(loans_core.SETTING_SELF_CHECKOUT, "cualquier-cosa", "test")
    assert loans_core.self_checkout_mode(storage) == loans_core.MODE_FREE


@pytest.mark.parametrize("role", ["estudiante", "operario", "encargado", "profesor"])
def test_only_the_administrator_changes_the_mode(lab, role):
    storage, people = lab
    with pytest.raises(PermissionError):
        loans_core.set_self_checkout_mode(storage, people[role], loans_core.MODE_OFF)
    assert loans_core.self_checkout_mode(storage) == loans_core.MODE_FREE
    ok, _ = loans_core.set_self_checkout_mode(storage, people["maestro"], loans_core.MODE_OFF)
    assert ok and loans_core.self_checkout_mode(storage) == loans_core.MODE_OFF
    ok, message = loans_core.set_self_checkout_mode(storage, people["maestro"], "casi")
    assert ok is False and "Elige" in message and loans_core.self_checkout_mode(storage) == loans_core.MODE_OFF


# --- libre: como en v1.15 ---------------------------------------------------------------------------

def test_free_mode_keeps_the_current_behaviour(lab):
    storage, people = lab
    ok, message, loan = loans_core.checkout(storage, CODE, 3, people["estudiante"])
    assert ok, message
    assert loan["quantity"] == 3


# --- apagado -------------------------------------------------------------------------------------------

def test_off_mode_blocks_clients_but_not_the_staff_that_approves(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_OFF)
    ok, message, loan = loans_core.checkout(storage, CODE, 1, people["estudiante"])
    assert ok is False and loan is None and "apagado" in message
    assert _open_loans(storage) == []
    for role in ("encargado", "profesor", "maestro"):
        ok, message, _ = loans_core.checkout(storage, CODE, 1, people[role])
        assert ok, f"{role}: {message}"
    with pytest.raises(PermissionError):                       # el operario sigue sin poder dar salidas
        loans_core.checkout(storage, CODE, 1, people["operario"])


# --- solo con solicitud aprobada -----------------------------------------------------------------------

def test_approved_only_requires_an_approved_request_for_clients(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_APPROVED_ONLY)
    ok, message, _ = loans_core.checkout(storage, CODE, 1, people["estudiante"])
    assert ok is False and "solicitud aprobada" in message
    pending, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
    ok, message, _ = loans_core.checkout(storage, CODE, 1, people["estudiante"], request_id=pending["id"])
    assert ok is False and "solicitud aprobada" in message          # pendiente de aprobar no cuenta
    assert _open_loans(storage) == []


def test_approved_only_allows_the_approved_quantity_and_no_more(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_APPROVED_ONLY)
    row = _approved(storage, people, quantity=2)
    ok, message, _ = loans_core.checkout(storage, CODE, 3, people["estudiante"], request_id=row["id"])
    assert ok is False and "2 unidad" in message
    ok, message, loan = loans_core.checkout(storage, CODE, 2, people["estudiante"], request_id=row["id"])
    assert ok, message and loan["quantity"] == 2


def test_approved_only_rejects_somebody_elses_or_another_products_request(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_APPROVED_ONLY)
    other = _approved(storage, people, quantity=1, owner="profesor")        # solicitud de otra persona
    ok, _, _ = loans_core.checkout(storage, CODE, 1, people["estudiante"], request_id=other["id"])
    assert ok is False
    storage.save_item({"name": "Lupa", "item_type": "standalone", "quantity": 5}, "LAB-LUPA-01", is_new=True)
    mine = _approved(storage, people, quantity=1)
    ok, _, _ = loans_core.checkout(storage, "LAB-LUPA-01", 1, people["estudiante"], request_id=mine["id"])
    assert ok is False                                                         # la solicitud es del microscopio


def test_a_request_cannot_be_picked_up_twice_or_after_dispatch_started(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_APPROVED_ONLY)
    row = _approved(storage, people, quantity=2)
    ok, message, loan = loans_core.checkout(storage, CODE, 2, people["estudiante"], request_id=row["id"])
    assert ok, message
    linked, link_message, _ = traceability.record_pickup(
        storage, storage.get_service_request(row["id"]), loan, people["estudiante"])
    assert linked, link_message
    ok, _, _ = loans_core.checkout(storage, CODE, 1, people["estudiante"], request_id=row["id"])
    assert ok is False                                                         # ya se retiró

    from core import dispatch

    second = _approved(storage, people, quantity=1)
    assert dispatch.claim_request(storage, second["id"], people["operario"])[0]
    ok, _, _ = loans_core.checkout(storage, CODE, 1, people["estudiante"], request_id=second["id"])
    assert ok is False                                                         # un operario ya lo despacha


def test_approved_only_does_not_limit_the_staff(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_APPROVED_ONLY)
    for role in ("encargado", "profesor", "maestro"):
        ok, message, _ = loans_core.checkout(storage, CODE, 1, people[role])
        assert ok, f"{role}: {message}"


def test_the_capability_that_decides_who_is_restricted_is_approving_requests():
    for role in ("estudiante", "operario"):
        assert loans_core.is_self_checkout_restricted({"role": role})
    for role in ("encargado", "profesor", "maestro"):
        assert permissions.has_cap({"role": role}, permissions.CAP_APPROVE)
        assert not loans_core.is_self_checkout_restricted({"role": role})


# --- la pantalla de Escanear ---------------------------------------------------------------------------------

def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    importlib.import_module("views.escanear").render()


def _scan_screen(storage, person):
    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["scan_result"] = barcode.scan(storage, CODE)
    at.run()
    assert not at.exception
    return at


def _text(elements) -> str:
    return " ".join(str(e.value) for e in elements)


def test_scan_screen_explains_why_there_is_no_checkout_form(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_OFF)
    at = _scan_screen(storage, people["estudiante"])
    assert "apagado" in _text(at.info) and not [b for b in at.button if "Confirmar salida" in b.label]
    staff = _scan_screen(storage, people["profesor"])
    assert [b for b in staff.button if "Confirmar salida" in b.label]

    _mode(storage, people, loans_core.MODE_APPROVED_ONLY)
    at = _scan_screen(storage, people["estudiante"])
    assert "solicitud aprobada" in _text(at.info) and not [b for b in at.button if "Confirmar salida" in b.label]


def test_scan_screen_forces_the_link_and_registers_the_pickup(lab):
    storage, people = lab
    _mode(storage, people, loans_core.MODE_APPROVED_ONLY)
    row = _approved(storage, people, quantity=2)
    at = _scan_screen(storage, people["estudiante"])
    select = [s for s in at.selectbox if "Solicitud aprobada" in s.label][0]
    assert len(select.options) == 1 and "No vincular" not in select.options[0]   # sin «No vincular»
    [b for b in at.button if "Confirmar salida" in b.label][0].click().run()
    assert not at.exception
    loans = _open_loans(storage)
    assert len(loans) == 1 and loans[0]["quantity"] == 2
    events = storage.get_trace_events(request_id=row["id"], event_type=traceability.EVENT_PICKED_UP)
    assert len(events) == 1


def test_scan_screen_in_free_mode_still_offers_not_linking(lab):
    storage, people = lab
    _approved(storage, people, quantity=2)
    at = _scan_screen(storage, people["estudiante"])
    select = [s for s in at.selectbox if "Solicitud aprobada" in s.label][0]
    assert any("No vincular" in option for option in select.options)
