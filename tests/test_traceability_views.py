# -*- coding: utf-8 -*-
"""Flujos de la ruta verificable y la trazabilidad con las vistas reales
(AppTest + Excel temporal): el estudiante recorre la ruta escaneando cada
etiqueta, el profesor valida el comprobante, la salida en Escanear se enlaza
con la solicitud y nadie ve solicitudes ajenas."""

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import traceability  # noqa: E402
from core.storage import LabStorage  # noqa: E402

PROF_EMAIL = "prof@uniminuto.edu.co"


def _session_user(row: dict) -> dict:
    return {key: row[key] for key in ("id", "full_name", "role", "institutional_email")}


@pytest.fixture
def lab():
    db = LabStorage()
    db.save_item({"name": "Electrónica básica", "item_type": "master"}, "2-1-01-00-000", is_new=True,
                  actor_email=PROF_EMAIL)
    db.save_item({"name": "Arduino UNO", "item_type": "child", "parent_id": "2-1-01-00-000", "quantity": 3},
                 "2-1-01-01-001", is_new=True, actor_email=PROF_EMAIL)
    student = db.create_user("Ana Estudiante", "ana@uniminuto.edu.co", "x", "estudiante")
    other = db.create_user("Luis Otro", "luis@uniminuto.edu.co", "x", "estudiante")
    prof = db.create_user("Profe Gómez", PROF_EMAIL, "x", "profesor")
    request = db.create_service_request({"request_type": "product", "item_id": "2-1-01-01-001",
                                         "item_name": "Arduino UNO", "quantity": 1}, student)
    db.update_service_request_status(request["id"], "approved", PROF_EMAIL, "Pasa por él")
    return {"db": db, "student": student, "other": other, "prof": prof,
            "request": db.get_service_request(request["id"])}


def _page_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import escanear, solicitudes, trazabilidad

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    {"trazabilidad": trazabilidad, "solicitudes": solicitudes, "escanear": escanear}[st.session_state.page].render()


def _open(page: str, user: dict, **state) -> AppTest:
    at = AppTest.from_function(_page_script, default_timeout=30)
    at.session_state["page"] = page
    at.session_state["test_user"] = _session_user(user)
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    assert not at.exception
    return at


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    for kind in ("success", "warning", "info", "error"):
        parts += [element.value for element in getattr(at, kind)]
    return " ".join(str(part) for part in parts)


def _button(at: AppTest, prefix: str, form: str = None):
    return next(button for button in at.button
                if button.label.startswith(prefix) and (form is None or button.proto.form_id == form))


def _scan(at: AppTest, request_id: str, code: str, button: str = ":material/check_circle: Confirmar etiqueta", prefix="trace_route"):
    at.text_input(key=f"{prefix}_code_{request_id}").input(code)
    _button(at, button, form=f"{prefix}_form_{request_id}").click().run()
    assert not at.exception


def test_student_walks_the_route_scanning_each_label_and_gets_a_receipt(lab):
    rid = lab["request"]["id"]
    at = _open("solicitudes", lab["student"])                  # la ruta vive en Solicitudes → Mis pedidos
    assert "Paso 1 de 5: Estantería 2" in _text(at)

    _scan(at, rid, "2-1-02-00-000", ":material/label: Confirmar etiqueta")          # contenedor equivocado
    assert "Estás en el Contenedor 02; el tuyo es el 01" in _text(at)
    _scan(at, rid, "2-1-01-00-000", ":material/label: Confirmar etiqueta")
    assert "Contenedor 01 verificado" in _text(at) and "Paso 4 de 5: Caja 01" in _text(at)
    assert lab["db"].get_trace_events() == []                            # nada escrito todavía
    _scan(at, rid, "2-1-01-01-000")
    _scan(at, rid, "2-1-01-01-001")

    events = lab["db"].get_trace_events(request_id=rid)
    assert [e["event_type"] for e in events] == [traceability.EVENT_ROUTE_VERIFIED]
    receipt = traceability.format_receipt(events[0]["receipt"])
    text = _text(at)
    assert f"Tu comprobante es {receipt}" in text and "Comprobante de ruta" in text
    assert "3/3" in text and "1 lectura(s) equivocada(s)" in text


def test_shelf_step_can_be_confirmed_on_arrival(lab):
    rid = lab["request"]["id"]
    at = _open("solicitudes", lab["student"])
    _button(at, ":material/location_on: Ya estoy aquí").click().run()
    assert "Llegaste a Estantería 2" in _text(at) and "Paso 2 de 5: Piso 1" in _text(at)
    _scan(at, rid, "2-1-01-01-001", ":material/label: Confirmar etiqueta")          # salta directo al producto
    assert "Saltaste 2 etiquetas" in _text(at)
    assert lab["db"].get_trace_events() == []                            # parcial: no se guarda solo
    _button(at, ":material/save: Guardar ruta con verificación parcial").click().run()
    events = lab["db"].get_trace_events(request_id=rid)
    assert len(events) == 1 and '"scanned": 1' in events[0]["details"]


def test_students_never_see_requests_of_others(lab):
    db = lab["db"]
    db.save_item({"name": "Equipo Secreto", "item_type": "standalone", "quantity": 1}, "LAB-SEC-01",
                 is_new=True, actor_email=PROF_EMAIL)
    foreign = db.create_service_request({"request_type": "product", "item_id": "LAB-SEC-01",
                                         "item_name": "Equipo Secreto", "quantity": 1}, lab["other"])
    db.add_trace_event({"event_type": "route_verified", "request_id": foreign["id"], "user_id": lab["other"]["id"],
                        "receipt": "ZZZZZZ", "details": "{}"})
    text = _text(_open("solicitudes", lab["student"]))
    assert "Equipo Secreto" not in text and "ZZZ" not in text and "Luis" not in text
    assert "Arduino UNO" in text


def test_professor_validates_the_receipt_and_leaves_a_record(lab):
    db, request = lab["db"], lab["request"]
    route = traceability.new_route(db.get_item("2-1-01-01-001"), db.get_item("2-1-01-00-000"), request)
    traceability.apply_code(route, "2-1-01-01-001")
    _, _, event = traceability.save_route(db, request, route, _session_user(lab["student"]))

    at = _open("trazabilidad", lab["prof"])
    assert "Retiros con ruta verificada" in _text(at)
    receipt_input = next(t for t in at.text_input if t.label == "Comprobante")
    receipt_input.input(traceability.format_receipt(event["receipt"]).lower())
    _button(at, ":material/verified: Validar comprobante").click().run()
    assert "Comprobante válido: Ana Estudiante verificó la ruta a «Arduino UNO»" in _text(at)
    _button(at, ":material/check_circle: Registrar que validé").click().run()
    assert not at.exception
    checks = db.get_trace_events(request_id=request["id"], event_type=traceability.EVENT_RECEIPT_CHECKED)
    assert len(checks) == 1 and checks[0]["actor_name"] == "Profe Gómez"
    assert "Validación registrada" in _text(at)


def test_professor_search_and_custody_show_the_whole_chain(lab):
    db, request = lab["db"], lab["request"]
    route = traceability.new_route(db.get_item("2-1-01-01-001"), db.get_item("2-1-01-00-000"), request)
    traceability.apply_code(route, "2-1-01-01-001")
    traceability.save_route(db, request, route, _session_user(lab["student"]))

    at = _open("trazabilidad", lab["prof"])
    at.text_input(key="trace_query").input("ana").run()
    text = _text(at)
    assert "Ruta verificada · por retirar" in text and "Ana Estudiante" in text

    custody = next(t for t in at.text_input if t.label == "Código del producto")
    custody.input("2-1-01-01-001")
    _button(at, ":material/link: Ver cadena de custodia").click().run()
    text = _text(at)
    assert "ruta E2 › P1 › C01 › CJ01 › I001" in text
    assert "Ruta verificada" in text and "Alta" in text and "aprobada" in text


def test_professor_moves_a_service_forward(lab):
    db = lab["db"]
    service = db.create_service_request({"request_type": "service", "service_name": "Impresión 3D",
                                         "description": "Pieza", "quantity": 0}, lab["student"])
    db.update_service_request_status(service["id"], "approved", PROF_EMAIL)
    at = _open("trazabilidad", lab["prof"])
    _button(at, "▶️ Marcar en curso").click().run()
    assert "marcado como en curso" in _text(at)
    _button(at, ":material/inventory_2: Marcar entregado").click().run()
    stages = [e["event_type"] for e in db.get_trace_events(request_id=service["id"])]
    assert stages == [traceability.EVENT_SERVICE_STARTED, traceability.EVENT_SERVICE_DELIVERED]
    assert db.get_service_request(service["id"])["status"] == "approved"

    text = _text(_open("solicitudes", lab["student"]))
    assert "Entregado" in text


def test_checkout_in_escanear_is_linked_to_the_approved_request(lab):
    from core import barcode

    db, request = lab["db"], lab["request"]
    at = _open("escanear", lab["student"], scan_result=barcode.scan(db, "2-1-01-01-001"))
    assert "Tienes 1 solicitud(es) aprobada(s) de este producto" in _text(at)
    assert at.selectbox[0].value == request["id"]
    _button(at, ":material/check_circle: Confirmar salida").click().run()
    assert not at.exception

    loans = db.get_open_loans_for_user(lab["student"]["id"])
    pickups = db.get_trace_events(request_id=request["id"], event_type=traceability.EVENT_PICKED_UP)
    assert len(loans) == 1 and len(pickups) == 1 and pickups[0]["loan_id"] == loans[0]["id"]
    assert db.get_service_request(request["id"])["status"] == "approved"
    assert "Salida vinculada a la solicitud" in _text(at)


def test_my_requests_tab_offers_the_route_and_escapes_user_text(lab):
    db = lab["db"]
    db.save_item({"name": "<img src=x onerror=alert(1)>", "item_type": "standalone", "quantity": 2},
                 "LAB-XSS-01", is_new=True, actor_email=PROF_EMAIL)
    hostile = db.create_service_request({"request_type": "product", "item_id": "LAB-XSS-01",
                                         "item_name": "<img src=x onerror=alert(1)>", "quantity": 1}, lab["student"])
    db.update_service_request_status(hostile["id"], "approved", PROF_EMAIL)
    at = _open("solicitudes", lab["student"])
    assert any("ruta hasta el producto" in e.label for e in at.expander)
    html = " ".join(m.value for m in at.markdown)
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "Aprobada · por retirar" in html

    _scan(at, lab["request"]["id"], "2-1-01-00-000", ":material/label: Confirmar etiqueta", prefix="trace_route")
    assert "Contenedor 01 verificado" in _text(at)
