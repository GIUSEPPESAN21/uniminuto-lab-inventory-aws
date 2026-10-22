# -*- coding: utf-8 -*-
"""v1.17 · Solicitudes unificadas (producto, servicio y reserva), Mis pedidos, Buscar y Trazabilidad legible."""

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import dispatch, service_requests, traceability  # noqa: E402
from core.storage import LabStorage  # noqa: E402

ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")
CODE = "LAB-MIC-01"


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role) for role in ROLES}
    storage.save_item({"name": "Microscopio óptico", "item_type": "standalone", "quantity": 5, "location": "Gabinete azul",
                       "category": "Óptica"}, CODE, is_new=True, actor_email="seed")
    storage.save_item({"name": "Resistencia 220", "item_type": "standalone", "quantity": 50}, "LAB-RES-220", is_new=True)
    storage.save_item({"name": "Resistencia 330", "item_type": "standalone", "quantity": 50}, "LAB-RES-330", is_new=True)
    return storage, people


def _script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    importlib.import_module(f"views.{st.session_state.test_view}").render()


def _open(view, person, **state):
    at = AppTest.from_function(_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_view"] = view
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    assert not at.exception, at.exception
    return at


def _text(at) -> str:
    parts = [e.value for e in at.markdown] + [e.value for e in at.caption]
    for kind in ("success", "warning", "info", "error"):
        parts += [e.value for e in getattr(at, kind)]
    return " ".join(str(p) for p in parts)


def _button(at, text):
    return next(b for b in at.button if text in b.label)


def _tab_labels(at):
    return [tab.label for tab in at.tabs]


# --- Solicitudes: un solo lugar --------------------------------------------------------------

def test_tabs_follow_the_role(lab):
    storage, people = lab
    client = _tab_labels(_open("solicitudes", people["estudiante"]))
    assert any("Nueva solicitud" in label for label in client) and any("Mis pedidos" in label for label in client)
    assert not any("Gestionar" in label for label in client)
    for role in ("encargado", "profesor", "maestro"):
        assert any("Gestionar" in label for label in _tab_labels(_open("solicitudes", people[role])))


def test_client_orders_a_product_by_searching_and_sees_the_message_after_the_reload(lab):
    storage, people = lab
    at = _open("solicitudes", people["estudiante"])
    at.text_input(key="request_product_query").input("microscopio").run()
    assert [o for o in at.selectbox(key="request_product_choice").options] == [f"Microscopio óptico · {CODE}"]
    at.number_input[0].set_value(2)
    _button(at, "Pedir este producto").click().run()
    assert not at.exception
    [row] = storage.get_service_requests(user_id=people["estudiante"]["id"])
    assert row["item_id"] == CODE and int(row["quantity"]) == 2 and row["status"] == "pending"
    assert "enviado" in _text(at) and "Mis pedidos" in _text(at)             # el aviso sobrevive al rerun


def test_client_orders_a_service_and_a_reservation_from_the_same_page(lab):
    storage, people = lab
    at = _open("solicitudes", people["estudiante"])
    at.radio(key="request_kind").set_value("Servicio").run()
    at.text_input[0].input("Impresión 3D")
    at.text_area[0].input("Una pieza para el robot")
    _button(at, "Pedir servicio").click().run()
    [service] = storage.get_service_requests(user_id=people["estudiante"]["id"])
    assert service["request_type"] == "service" and service["service_name"] == "Impresión 3D"

    at = _open("solicitudes", people["estudiante"])
    at.radio(key="request_kind").set_value("Reserva").run()
    assert any("Enviar solicitud de reserva" in b.label for b in at.button)


def test_my_orders_shows_the_progress_in_plain_words_and_lets_the_client_confirm(lab):
    storage, people = lab
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
    assert "Enviada · esperando aprobación" in _text(_open("solicitudes", people["estudiante"]))
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    assert dispatch.claim_request(storage, row["id"], people["operario"])[0]
    assert dispatch.confirm_manual_picking(storage, row["id"], people["operario"], True)[0]
    assert "Preparando tu pedido" in _text(_open("solicitudes", people["estudiante"]))
    assert dispatch.receive_request(storage, row["id"], people["encargado"])[0]
    text = _text(_open("solicitudes", people["estudiante"]))
    assert "Listo para reclamar" in text and "lab-stepper" in text
    assert dispatch.deliver_request(storage, row["id"], people["encargado"])[0]
    at = _open("solicitudes", people["estudiante"])
    _button(at, "Recibí mi pedido").click().run()
    assert not at.exception
    assert traceability.request_stage_key(storage.get_service_request(row["id"]),
                                          storage.get_trace_events(request_id=row["id"])) == traceability.STAGE_CONFIRMED
    assert "Completado" in _text(_open("solicitudes", people["estudiante"]))


def test_client_sees_only_their_own_orders_and_can_cancel_a_pending_one(lab):
    storage, people = lab
    mine, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
    service_requests.submit_request(storage, people["profesor"], "product", "LAB-RES-220", 1)
    at = _open("solicitudes", people["estudiante"])
    text = _text(at)
    assert "Microscopio óptico" in text and "Resistencia 220" not in text and "Persona profesor" not in text
    _button(at, "Cancelar pedido").click().run()
    assert storage.get_service_request(mine["id"])["status"] == "cancelled"


def test_reviewer_manages_requests_and_reservations_in_one_place(lab):
    storage, people = lab
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
    at = _open("solicitudes", people["profesor"])
    assert any("Microscopio óptico" in e.label for e in at.expander)
    approve = [b for b in at.button if b.label == "Aprobar"][0]
    approve.click().run()
    assert not at.exception
    assert storage.get_service_request(row["id"])["status"] == "approved"
    assert "Reservas" in _text(at)                           # la gestión de reservas vive en la misma pestaña


# --- Buscar: un solo cuadro ----------------------------------------------------------------------------

def _search(person, text, storage):
    at = _open("escanear", person)
    at.text_input[0].input(text)
    _button(at, "Buscar").click().run()
    assert not at.exception
    return at


def test_search_by_name_lists_matches_and_opens_one(lab):
    storage, people = lab
    at = _search(people["estudiante"], "resistencia", storage)
    text = _text(at)
    assert "Resistencia 220" in text and "Resistencia 330" in text and "Microscopio" not in text
    [button for button in at.button if button.key == "open_LAB-RES-330"][0].click().run()
    assert "Resistencia 330" in _text(at) and "Pedir este producto" in _text(at)


def test_a_single_match_and_an_exact_code_open_the_product_directly(lab):
    storage, people = lab
    for query in ("microscopio", CODE):
        at = _search(people["estudiante"], query, storage)
        assert "Microscopio óptico" in _text(at) and any("Pedir este producto" in b.label for b in at.button)


def test_search_finds_an_order_by_its_number_but_never_somebody_elses(lab):
    storage, people = lab
    mine, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
    other, _, _ = service_requests.submit_request(storage, people["profesor"], "product", "LAB-RES-220", 1)
    at = _search(people["estudiante"], f"#{mine['id'][:8]}", storage)
    assert "Enviada · esperando aprobación" in _text(at)
    at = _search(people["estudiante"], f"#{other['id'][:8]}", storage)
    assert "Sin resultados" in _text(at) or "No encontré" in _text(at)


def test_nothing_found_is_explained_and_staff_can_register_a_new_code(lab):
    storage, people = lab
    assert "No encontré" in _text(_search(people["estudiante"], "zzzxxx", storage))
    staff = _search(people["profesor"], "9-9-99-99-999", storage)
    assert "Registrar" in _text(staff) or any("Registrar" in b.label for b in staff.button) or any(
        "Registrar" in e.label for e in staff.expander)


def test_nfc_scan_result_still_opens_the_page(lab):
    from core import barcode

    storage, people = lab
    at = _open("escanear", people["estudiante"], scan_result=barcode.scan(storage, CODE))
    assert "Microscopio óptico" in _text(at)


# --- Trazabilidad del personal ------------------------------------------------------------------------

def test_audit_is_written_in_sentences_not_json(lab):
    storage, people = lab
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 2)
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"], "ok")[0]
    assert dispatch.claim_request(storage, row["id"], people["operario"])[0]
    assert dispatch.confirm_manual_picking(storage, row["id"], people["operario"], True, "sin pila")[0]
    text = _text(_open("trazabilidad", people["encargado"]))
    assert "Autorizó el despacho" in text and "Tomó el pedido" in text
    assert "Terminó el picking de 2 unidad(es) con confirmación manual" in text and "sin pila" in text
    assert '"authorized_pickers"' not in text and '"picker_id"' not in text


def test_describe_event_covers_the_admin_events():
    cases = {
        "role_changed": ({"from_role": "estudiante", "to_role": "maestro"}, "de Cliente a Administrador"),
        "whitelist_changed": ({"action": "added", "email": "a@b.edu"}, "Agregó a a@b.edu"),
        "sync_conflict_resolved": ({"kept": "remote"}, "de GitHub"),
        "order_released": ({"reason": "me equivoqué"}, "«me equivoqué»"),
    }
    import json

    for kind, (details, expected) in cases.items():
        assert expected in traceability.describe_event({"event_type": kind, "details": json.dumps(details)})
    assert traceability.describe_event({"event_type": "otro", "details": '{"a": 1}'}) == "a: 1"
