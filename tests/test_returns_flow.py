# -*- coding: utf-8 -*-
"""v1.19 · devolución en tres pasos: el cliente la pide, el supervisor la verifica, un operario la guarda."""

import pytest

from core import loans, permissions, returns, traceability
from core.storage import LabStorage

ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role) for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 5, "location": "Estante 2"},
                      "LAB-MIC-01", is_new=True, actor_email="seed")
    return storage, people


def _borrow(storage, who, quantity=3):
    ok, message, loan = loans.checkout(storage, "LAB-MIC-01", quantity, who)
    assert ok, message
    return loan


def _verified(storage, people, quantity=3, condition="ok", received=None):
    loan = _borrow(storage, people["estudiante"], quantity)
    assert returns.request_return(storage, loan["id"], people["estudiante"])[0]
    ok, message = returns.verify_return(storage, loan["id"], people["encargado"], condition,
                                        quantity if received is None else received)
    assert ok, message
    return loan


def test_availability_only_comes_back_when_an_operator_stores_it(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    assert storage.get_available_quantity("LAB-MIC-01") == 2
    assert returns.request_return(storage, loan["id"], people["estudiante"])[0]
    assert storage.get_available_quantity("LAB-MIC-01") == 2                  # solo avisó
    assert returns.verify_return(storage, loan["id"], people["encargado"], "ok", 3)[0]
    assert storage.get_available_quantity("LAB-MIC-01") == 2                  # verificada, aún sin guardar
    ok, _ = returns.store_return(storage, loan["id"], people["operario"])
    assert ok and storage.get_available_quantity("LAB-MIC-01") == 5
    assert storage.get_open_loans_for_user(people["estudiante"]["id"]) == []


def test_each_step_belongs_to_its_role(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    with pytest.raises(PermissionError):                      # el operario no ve préstamos ni los devuelve
        returns.request_return(storage, loan["id"], people["operario"])
    with pytest.raises(PermissionError):                      # tampoco otro cliente
        returns.request_return(storage, loan["id"], people["encargado"])
    returns.request_return(storage, loan["id"], people["estudiante"])
    for role in ("estudiante", "operario"):
        with pytest.raises(PermissionError):
            returns.verify_return(storage, loan["id"], people[role], "ok", 3)
    returns.verify_return(storage, loan["id"], people["profesor"], "ok", 3)
    with pytest.raises(PermissionError):
        returns.store_return(storage, loan["id"], people["estudiante"])
    assert returns.store_return(storage, loan["id"], people["encargado"])[0]       # el supervisor también guarda


def test_steps_cannot_be_skipped_or_repeated(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    assert not returns.verify_return(storage, loan["id"], people["encargado"], "ok", 3)[0]    # sin solicitud
    assert not returns.store_return(storage, loan["id"], people["operario"])[0]               # sin verificar
    assert returns.request_return(storage, loan["id"], people["estudiante"])[0]
    assert not returns.request_return(storage, loan["id"], people["estudiante"])[0]           # ya pedida
    assert not returns.store_return(storage, loan["id"], people["operario"])[0]               # falta la verificación
    assert returns.verify_return(storage, loan["id"], people["encargado"], "ok", 3)[0]
    assert not returns.verify_return(storage, loan["id"], people["encargado"], "ok", 3)[0]    # ya verificada
    assert returns.store_return(storage, loan["id"], people["operario"])[0]
    assert not returns.store_return(storage, loan["id"], people["operario"])[0]               # ya cerrada


def test_quantity_and_condition_are_validated(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    returns.request_return(storage, loan["id"], people["estudiante"])
    sup = people["encargado"]
    assert not returns.verify_return(storage, loan["id"], sup, "roto", 3)[0]
    for bad in (0, 4, -1, "x", None):
        assert not returns.verify_return(storage, loan["id"], sup, "ok", bad)[0]
    assert not returns.verify_return(storage, loan["id"], sup, "incomplete", 3)[0]            # completo ≠ incompleto
    assert returns.stage(storage, loan["id"]) == returns.STAGE_REQUESTED


def test_partial_return_closes_only_what_arrived_and_keeps_the_rest_open(lab):
    storage, people = lab
    loan = _verified(storage, people, quantity=3, condition="incomplete", received=2)
    ok, message = returns.store_return(storage, loan["id"], people["operario"])
    assert ok and "parcial" in message.lower()
    assert storage.get_available_quantity("LAB-MIC-01") == 4                  # 5 − 1 que aún falta
    open_loans = storage.get_open_loans_for_user(people["estudiante"]["id"])
    assert [row["quantity"] for row in open_loans] == [1]
    assert returns.stage(storage, loan["id"]) == returns.STAGE_NONE           # puede pedir devolver lo que falta
    assert returns.request_return(storage, loan["id"], people["estudiante"])[0]
    assert returns.verify_return(storage, loan["id"], people["encargado"], "ok", 1)[0]
    assert returns.store_return(storage, loan["id"], people["operario"])[0]
    assert storage.get_available_quantity("LAB-MIC-01") == 5


def test_the_condition_is_kept_in_the_item_history_and_the_chain_of_custody(lab):
    storage, people = lab
    loan = _verified(storage, people, condition="damaged")
    returns.store_return(storage, loan["id"], people["operario"], notes="Lente rayado")
    history = [row for row in storage.get_item_history("LAB-MIC-01") if row["type"] == "Reingreso"]
    assert history and "Con daños" in history[0]["details"] and "Lente rayado" in history[0]["details"]
    events = storage.get_trace_events(loan_id=loan["id"])
    kinds = [event["event_type"] for event in events if event["event_type"] in traceability.RETURN_EVENTS]
    assert kinds == [traceability.EVENT_RETURN_REQUESTED, traceability.EVENT_RETURN_VERIFIED,
                     traceability.EVENT_RETURN_STORED]
    by_kind = {event["event_type"]: event for event in events}
    assert by_kind[traceability.EVENT_RETURN_VERIFIED]["actor_role"] == "encargado"
    assert by_kind[traceability.EVENT_RETURN_STORED]["actor_role"] == "operario"
    assert all(event["owner_id"] == people["estudiante"]["id"] for event in by_kind.values())
    assert "Verificó la devolución: 3 unidad(es), con daños" in traceability.describe_event(
        by_kind[traceability.EVENT_RETURN_VERIFIED])


def test_queues_show_each_loan_only_in_its_stage(lab):
    storage, people = lab
    first = _borrow(storage, people["estudiante"], 1)
    second = _borrow(storage, people["estudiante"], 1)
    returns.request_return(storage, first["id"], people["estudiante"])
    returns.request_return(storage, second["id"], people["estudiante"])
    returns.verify_return(storage, second["id"], people["encargado"], "ok", 1)
    assert [row["loan"]["id"] for row in returns.pending(storage, returns.STAGE_REQUESTED)] == [first["id"]]
    assert [row["loan"]["id"] for row in returns.pending(storage, returns.STAGE_VERIFIED)] == [second["id"]]
    assert returns.counts(storage) == {returns.STAGE_REQUESTED: 1, returns.STAGE_VERIFIED: 1}


def test_direct_check_in_is_only_for_those_with_the_capability(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    with pytest.raises(PermissionError):
        loans.checkin(storage, loan["id"], people["estudiante"])
    assert not loans.can_checkin_loan(people["estudiante"], loan)
    assert loans.can_checkin_loan(people["profesor"], loan) and loans.checkin(storage, loan["id"], people["profesor"])[0]


def test_nobody_but_the_flow_can_close_a_loan_for_a_client(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    for role in ("estudiante", "encargado", "operario"):
        with pytest.raises(PermissionError):
            storage.return_loan(loan["id"], actor=people[role])
    assert storage.get_open_loans_for_user(people["estudiante"]["id"])


def test_can_request_return_rules(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    assert returns.can_request_return(people["estudiante"], loan)
    assert not returns.can_request_return(people["encargado"], loan)              # de otra persona
    assert not returns.can_request_return(people["profesor"], dict(loan, user_id=people["profesor"]["id"]))
    assert permissions.has_cap(people["operario"], permissions.CAP_STORE_RETURN)
    assert not permissions.has_cap(people["operario"], permissions.CAP_VERIFY_RETURN)


# --- vistas reales ------------------------------------------------------------------------

def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    importlib.import_module(f"views.{st.session_state.test_view}").render()


def _open(view, person):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_view"] = view
    at.run()
    assert not at.exception
    return at


def _labels(at):
    return [button.label for button in at.button]


def test_the_client_asks_for_the_return_from_loans_and_then_sees_the_stage(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    at = _open("prestamos", people["estudiante"])
    assert any("Devolver" in label for label in _labels(at)) and not any("Reingresar" in l for l in _labels(at))
    [button for button in at.button if "Devolver" in button.label][0].click().run()
    assert returns.stage(storage, loan["id"]) == returns.STAGE_REQUESTED
    again = _open("prestamos", people["estudiante"])
    assert not any("Devolver" in label for label in _labels(again))
    assert "verificación del supervisor" in " ".join(str(e.value) for e in again.info)


def test_despacho_returns_tab_is_split_between_supervisor_and_operator(lab):
    storage, people = lab
    loan = _borrow(storage, people["estudiante"])
    returns.request_return(storage, loan["id"], people["estudiante"], notes="Trae el estuche")

    sup = _open("despacho", people["encargado"])
    assert any(button.label == "Verificar devolución" for button in sup.button)
    assert "Trae el estuche" in " ".join(str(e.value) for e in sup.info)
    [button for button in sup.button if button.label == "Verificar devolución"][0].click().run()
    assert returns.stage(storage, loan["id"]) == returns.STAGE_VERIFIED

    operator = _open("despacho", people["operario"])
    assert not any(button.label == "Verificar devolución" for button in operator.button)
    store = [button for button in operator.button if button.label == "Guardar en el estante"]
    assert store
    store[0].click().run()
    assert storage.get_available_quantity("LAB-MIC-01") == 5
