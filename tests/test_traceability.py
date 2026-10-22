# -*- coding: utf-8 -*-
"""Trazabilidad chequeable: un solo evento por ruta, comprobante verificable,
enlace del retiro con la solicitud, avance de servicios, líneas de tiempo,
indicadores, cadena de custodia y permisos (un estudiante solo ve lo suyo)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from core import loans as loans_core, traceability

T0 = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
STUDENT = {"id": "s1", "full_name": "Ana Estudiante", "institutional_email": "ana@uniminuto.edu.co",
           "role": "estudiante"}
OTHER = {"id": "s2", "full_name": "Luis Otro", "institutional_email": "luis@uniminuto.edu.co", "role": "estudiante"}
PROF = {"id": "p1", "full_name": "Profe Gómez", "institutional_email": "prof@uniminuto.edu.co", "role": "profesor"}


@pytest.fixture
def lab(storage):
    storage.add_item("2-1-01-00-000", name="Electrónica", item_type="master")
    storage.add_item("2-1-01-01-001", name="Arduino UNO", item_type="child", parent_id="2-1-01-00-000", quantity=3)
    return storage


def _request(storage, user=STUDENT, status="approved", request_type="product", item_id="2-1-01-01-001", **extra):
    row = storage.create_service_request({
        "request_type": request_type, "item_id": item_id if request_type == "product" else "",
        "item_name": "Arduino UNO" if request_type == "product" else "", "quantity": 1 if request_type == "product" else 0,
        "service_name": "Impresión 3D" if request_type == "service" else "", "description": "Práctica",
        "needed_at": "",
    }, user)
    storage.service_requests[row["id"]].update(status=status, created_at=T0.isoformat(), **extra)
    return storage.get_service_request(row["id"])


def _walk(storage, request, now=T0):
    item = storage.get_item(request["item_id"])
    parent = storage.get_item(item["parent_id"])
    route = traceability.new_route(item, parent, request)
    for offset, code in enumerate(("2-1-01-00-000", "2-1-01-01-000", "2-1-01-01-001")):
        traceability.apply_code(route, code, now + timedelta(seconds=30 * offset))
    return route


# --- guardar la ruta: un solo evento ------------------------------------------------------

def test_completed_route_is_saved_as_a_single_event_with_every_checkpoint(lab):
    request = _request(lab)
    ok, message, event = traceability.save_route(lab, request, _walk(lab, request), STUDENT)
    assert ok and "comprobante" in message
    assert len(lab.trace_events) == 1
    assert event["event_type"] == traceability.EVENT_ROUTE_VERIFIED
    assert event["user_id"] == "s1" and event["actor_id"] == "s1"
    details = json.loads(event["details"])
    assert [p["code"] for p in details["checkpoints"] if p["method"] == "scan"] == [
        "2-1-01-00-000", "2-1-01-01-000", "2-1-01-01-001"]
    assert all(p["at"] for p in details["checkpoints"])
    assert details["scanned"] == details["labels"] == 3 and details["duration_s"] == 60
    assert details["route"] == "E2 › P1 › C01 › CJ01 › I001"


def test_saving_twice_is_idempotent(lab):
    request = _request(lab)
    route = _walk(lab, request)
    traceability.save_route(lab, request, route, STUDENT)
    ok, message, _ = traceability.save_route(lab, request, route, STUDENT)
    assert ok and "ya estaba registrada" in message and len(lab.trace_events) == 1


@pytest.mark.parametrize("change, error", [
    ({"status": "pending"}, "aprobada"),
    ({"request_type": "service"}, "productos"),
])
def test_route_is_only_saved_for_approved_product_requests(lab, change, error):
    request = _request(lab)
    route = _walk(lab, request)
    ok, message, _ = traceability.save_route(lab, {**request, **change}, route, STUDENT)
    assert not ok and error in message and not lab.trace_events


def test_only_the_requester_can_save_and_the_route_must_be_complete_and_match(lab):
    request = _request(lab)
    route = _walk(lab, request)
    assert not traceability.save_route(lab, request, route, OTHER)[0]
    assert not traceability.save_route(lab, request, route, PROF)[0]
    incomplete = traceability.new_route(lab.get_item("2-1-01-01-001"), None, request)
    assert "Completa la ruta" in traceability.save_route(lab, request, incomplete, STUDENT)[1]
    foreign = {**route, "request_id": "otra"}
    assert "no corresponde" in traceability.save_route(lab, request, foreign, STUDENT)[1]
    assert not lab.trace_events


# --- comprobante -----------------------------------------------------------------------

def test_receipt_is_short_deterministic_and_unambiguous():
    code = traceability.make_receipt("req1", "2-1-01-01-001", "s1", T0)
    assert len(code) == 6 and set(code) <= set(traceability.RECEIPT_ALPHABET)
    assert not set(code) & set("ILOU")
    assert code == traceability.make_receipt("req1", "2-1-01-01-001", "s1", T0.isoformat())
    assert code != traceability.make_receipt("req1", "2-1-01-01-001", "s1", T0 + timedelta(seconds=1))
    assert code != traceability.make_receipt("req2", "2-1-01-01-001", "s1", T0)


def test_receipt_normalization_accepts_how_people_type_it():
    assert traceability.normalize_receipt(" k7q-2mx ") == "K7Q2MX"
    assert traceability.normalize_receipt("OIL") == "011"
    assert traceability.format_receipt("k7q2mx") == "K7Q-2MX"
    assert traceability.is_receipt_like("K7Q-2MX") and not traceability.is_receipt_like("K7Q")


def test_professor_validates_the_receipt_and_records_the_check(lab):
    request = _request(lab)
    _, _, event = traceability.save_route(lab, request, _walk(lab, request), STUDENT)
    typed = traceability.format_receipt(event["receipt"]).lower()
    result = traceability.verify_receipt(lab, typed)
    assert result["valid"] and result["tone"] == "success"
    assert "Ana Estudiante verificó la ruta a «Arduino UNO»" in result["message"]
    assert result["message"].endswith(("a. m.", "p. m.")) and ".." not in result["message"]
    assert result["request"]["id"] == request["id"] and not result["pickup"]

    assert not traceability.record_receipt_check(lab, result, STUDENT)[0]
    ok, _, check = traceability.record_receipt_check(lab, result, PROF)
    assert ok and check["event_type"] == traceability.EVENT_RECEIPT_CHECKED
    again = traceability.verify_receipt(lab, event["receipt"])
    assert len(again["checks"]) == 1
    assert not traceability.record_receipt_check(lab, again, PROF)[0]    # una vez por profesor


def test_unknown_malformed_and_tampered_receipts_are_rejected(lab):
    assert traceability.verify_receipt(lab, "abc")["tone"] == "warning"
    assert "No existe" in traceability.verify_receipt(lab, "K7Q-2MX")["message"]
    request = _request(lab)
    _, _, event = traceability.save_route(lab, request, _walk(lab, request), STUDENT)
    lab.trace_events[0]["user_id"] = "s2"          # alguien edita el registro
    tampered = traceability.verify_receipt(lab, event["receipt"])
    assert not tampered["valid"] and tampered.get("tampered")


def test_receipt_of_a_cancelled_or_picked_up_request_warns(lab):
    request = _request(lab)
    _, _, event = traceability.save_route(lab, request, _walk(lab, request), STUDENT)
    ok, _, loan = loans_core.checkout(lab, "2-1-01-01-001", 1, STUDENT)
    traceability.record_pickup(lab, request, loan, STUDENT)
    lab.service_requests[request["id"]]["status"] = "cancelled"
    result = traceability.verify_receipt(lab, event["receipt"])
    assert result["valid"] and result["tone"] == "warning"
    assert any("cancelada" in w for w in result["warnings"])
    assert any("ya se retiró" in w for w in result["warnings"])


# --- retiro enlazado ---------------------------------------------------------------------

def test_checkout_of_an_approved_request_is_linked_without_changing_states(lab):
    request = _request(lab)
    traceability.save_route(lab, request, _walk(lab, request), STUDENT)
    assert [r["id"] for r in traceability.pending_pickups(lab, STUDENT, "2-1-01-01-001")] == [request["id"]]
    _, _, loan = loans_core.checkout(lab, "2-1-01-01-001", 1, STUDENT)
    ok, message, event = traceability.record_pickup(lab, request, loan, STUDENT)
    assert ok and request["id"][:6] in message
    assert event["loan_id"] == loan["id"] and json.loads(event["details"])["route_verified"] is True
    assert lab.get_service_request(request["id"])["status"] == "approved"   # el estado no cambia
    assert lab.loans[loan["id"]]["status"] == "out"
    assert traceability.pending_pickups(lab, STUDENT, "2-1-01-01-001") == []
    assert not traceability.record_pickup(lab, request, loan, STUDENT)[0]   # no se duplica


def test_pickup_requires_same_student_same_product_and_approved_request(lab):
    lab.add_item("LAB-OTRO", name="Otro", quantity=2)
    request = _request(lab)
    _, _, other_loan = loans_core.checkout(lab, "2-1-01-01-001", 1, OTHER)
    _, _, wrong_item = loans_core.checkout(lab, "LAB-OTRO", 1, STUDENT)
    assert "otro producto" in traceability.record_pickup(lab, request, wrong_item, STUDENT)[1]
    assert "quien hizo la solicitud" in traceability.record_pickup(lab, request, other_loan, OTHER)[1]
    pending = {**request, "status": "pending"}
    assert not traceability.record_pickup(lab, pending, other_loan, STUDENT)[0]
    assert traceability.pending_pickups(lab, OTHER, "2-1-01-01-001") == []
    assert traceability.pending_pickups(lab, None, "2-1-01-01-001") == []


# --- servicios ---------------------------------------------------------------------------

def test_service_progress_is_recorded_as_events_by_managers_only(lab):
    request = _request(lab, request_type="service")
    assert not traceability.mark_service_stage(lab, request["id"], traceability.EVENT_SERVICE_STARTED, STUDENT)[0]
    ok, _, _ = traceability.mark_service_stage(lab, request["id"], traceability.EVENT_SERVICE_STARTED, PROF, "Mañana")
    assert ok
    assert "ya está en curso" in traceability.mark_service_stage(
        lab, request["id"], traceability.EVENT_SERVICE_STARTED, PROF)[1]
    assert traceability.mark_service_stage(lab, request["id"], traceability.EVENT_SERVICE_DELIVERED, PROF)[0]
    assert "ya fue entregado" in traceability.mark_service_stage(
        lab, request["id"], traceability.EVENT_SERVICE_DELIVERED, PROF)[1]
    assert lab.get_service_request(request["id"])["status"] == "approved"
    steps = traceability.request_timeline(request, lab.get_trace_events(request_id=request["id"]))
    assert [s["title"] for s in steps] == ["Solicitud creada", "Aprobada", "En curso", "Entregado"]
    assert [s["state"] for s in steps] == ["done"] * 4
    assert "Mañana" in steps[2]["detail"]


def test_service_stage_rules(lab):
    product = _request(lab)
    pending = _request(lab, request_type="service", status="pending")
    assert "Solo las solicitudes de servicio" in traceability.mark_service_stage(
        lab, product["id"], traceability.EVENT_SERVICE_STARTED, PROF)[1]
    assert "aprobado" in traceability.mark_service_stage(
        lab, pending["id"], traceability.EVENT_SERVICE_STARTED, PROF)[1]
    assert not traceability.mark_service_stage(lab, "nada", traceability.EVENT_SERVICE_STARTED, PROF)[0]
    assert not traceability.mark_service_stage(lab, pending["id"], "otro", PROF)[0]


def test_service_can_be_delivered_directly(lab):
    request = _request(lab, request_type="service")
    traceability.mark_service_stage(lab, request["id"], traceability.EVENT_SERVICE_DELIVERED, PROF)
    steps = traceability.request_timeline(request, lab.get_trace_events(request_id=request["id"]))
    assert steps[2]["state"] == "done" and "sin registro de inicio" in steps[2]["detail"]
    assert traceability.request_stage(request, lab.get_trace_events(request_id=request["id"])) == ("Entregado", "success")


# --- líneas de tiempo --------------------------------------------------------------------

def test_product_timeline_walks_from_created_to_returned(lab):
    request = _request(lab, reviewed_by="prof@uniminuto.edu.co", reviewed_at=T0.isoformat(), review_notes="OK")
    names = {"prof@uniminuto.edu.co": "Profe Gómez"}
    # Reloj fijo: «vencido» depende de la hora actual y T0 queda atrás con el tiempo.
    clock = T0 + timedelta(days=1)
    states = lambda events, loan=None: [  # noqa: E731
        s["state"] for s in traceability.request_timeline(request, events, loan, names, now=clock)
    ]
    assert states([]) == ["done", "done", "current", "pending", "pending"]
    assert "Por Profe Gómez · OK" in traceability.request_timeline(request, [], None, names)[1]["detail"]

    traceability.save_route(lab, request, _walk(lab, request), STUDENT)
    assert states(lab.get_trace_events(request_id=request["id"])) == ["done", "done", "done", "current", "pending"]

    _, _, loan = loans_core.checkout(lab, "2-1-01-01-001", 1, STUDENT, T0 + timedelta(days=3))
    traceability.record_pickup(lab, request, loan, STUDENT)
    events = lab.get_trace_events(request_id=request["id"])
    assert states(events, lab.loans[loan["id"]]) == ["done", "done", "done", "done", "current"]
    assert traceability.request_stage(request, events, lab.loans[loan["id"]])[0] == "Retirada · en préstamo"

    lab.return_loan(loan["id"])
    assert states(events, lab.loans[loan["id"]]) == ["done"] * 5
    assert traceability.request_stage(request, events, lab.loans[loan["id"]]) == ("Devuelta", "success")


def test_overdue_loan_is_flagged_and_pickup_without_route_is_explained(lab):
    request = _request(lab)
    _, _, loan = loans_core.checkout(lab, "2-1-01-01-001", 1, STUDENT, T0 - timedelta(days=2))
    traceability.record_pickup(lab, request, loan, STUDENT)
    steps = traceability.request_timeline(request, lab.get_trace_events(request_id=request["id"]), lab.loans[loan["id"]])
    assert steps[2]["state"] == "pending" and "sin recorrer la ruta" in steps[2]["detail"]
    assert steps[4]["state"] == "blocked" and "vencido" in steps[4]["detail"]


@pytest.mark.parametrize("status, title", [("rejected", "Rechazada"), ("cancelled", "Cancelada")])
def test_closed_requests_block_the_timeline(lab, status, title):
    request = _request(lab, status=status, review_notes="Sin stock")
    steps = traceability.request_timeline(request, [])
    assert steps[1]["title"] == title and steps[1]["state"] == "blocked" and "Sin stock" in steps[1]["detail"]
    assert all("No aplica" in s["detail"] for s in steps[2:])
    pending = traceability.request_timeline(_request(lab, status="pending"), [])
    assert pending[1]["state"] == "current" and pending[2]["state"] == "pending"


# --- búsqueda, indicadores y permisos -----------------------------------------------------

def test_search_by_id_code_name_email_and_receipt(lab):
    mine = _request(lab)
    other = _request(lab, user=OTHER, request_type="service")
    _, _, event = traceability.save_route(lab, mine, _walk(lab, mine), STUDENT)
    rows = lab.get_service_requests()
    events = lab.get_trace_events()
    find = lambda q: [r["id"] for r in traceability.search_requests(rows, q, events)]  # noqa: E731
    assert find(f"#{mine['id'][:4]}") == [mine["id"]]
    assert find("2-1-01-01-001") == [mine["id"]]
    assert find("luis") == [other["id"]]
    assert find("impresión") == [other["id"]]
    assert find(traceability.format_receipt(event["receipt"])) == [mine["id"]]
    assert len(find("")) == 2


def test_students_only_see_their_own_requests_and_events(lab):
    mine = _request(lab)
    _request(lab, user=OTHER)
    traceability.save_route(lab, mine, _walk(lab, mine), STUDENT)
    lab.add_trace_event({"event_type": "picked_up", "request_id": "x", "user_id": "s2"})
    assert [r["requester_id"] for r in traceability.visible_requests(lab, STUDENT)] == ["s1"]
    assert {e["user_id"] for e in traceability.visible_events(lab, STUDENT)} == {"s1"}
    assert len(traceability.visible_requests(lab, PROF)) == 2
    assert len(traceability.visible_events(lab, PROF)) == 2
    # Un filtro manipulado no salta la restricción del estudiante.
    assert traceability.visible_events(lab, STUDENT, user_id="s2") == traceability.visible_events(lab, STUDENT)
    assert traceability.visible_requests(lab, None) == [] and traceability.visible_events(lab, None) == []
    assert traceability.can_view_request(PROF, mine) and not traceability.can_view_request(OTHER, mine)


def test_summaries_count_ready_routes_and_pickup_rate(lab):
    ready = _request(lab)
    picked = _request(lab)
    _request(lab, status="pending")
    service = _request(lab, request_type="service")
    traceability.save_route(lab, picked, _walk(lab, picked), STUDENT)
    _, _, loan = loans_core.checkout(lab, "2-1-01-01-001", 1, STUDENT)
    traceability.record_pickup(lab, picked, loan, STUDENT)
    traceability.mark_service_stage(lab, service["id"], traceability.EVENT_SERVICE_STARTED, PROF)
    requests, events = lab.get_service_requests(), lab.get_trace_events()

    student = {s["label"]: s["value"] for s in traceability.student_summary(requests, events, [loan])}
    assert student == {"Solicitudes activas": 3, "Listas para retirar": 1, "Rutas verificadas": 1, "En préstamo": 1}
    manager = {s["label"]: s["value"] for s in traceability.manager_summary(requests, events)}
    assert manager["Por revisar"] == 1 and manager["Aprobadas por retirar"] == 1
    assert manager["Retiros con ruta verificada"] == "100 %" and manager["Servicios en curso"] == 1
    assert ready["id"] not in {e["request_id"] for e in events if e["event_type"] == "picked_up"}


def test_custody_timeline_merges_history_requests_and_events_in_order(lab):
    request = _request(lab, reviewed_by="prof@uniminuto.edu.co", reviewed_at=(T0 + timedelta(minutes=5)).isoformat())
    lab.history.append({"item_id": "2-1-01-01-001", "timestamp": (T0 - timedelta(days=1)).isoformat(),
                        "type": "Alta", "quantity_change": "3", "actor_user_id": "prof@uniminuto.edu.co",
                        "details": "Registro inicial"})
    traceability.save_route(lab, request, _walk(lab, request, T0 + timedelta(minutes=10)), STUDENT)
    steps = traceability.custody_timeline(
        lab.get_item_history("2-1-01-01-001"), [request], lab.get_trace_events(item_id="2-1-01-01-001"),
        {"prof@uniminuto.edu.co": "Profe Gómez"},
    )
    assert [s["title"] for s in steps] == [
        "Alta", f"Solicitud {traceability.short_id(request['id'])} creada",
        f"Solicitud {traceability.short_id(request['id'])} aprobada", "Ruta verificada",
    ]
    assert "por Profe Gómez" in steps[0]["detail"] and "3/3 etiquetas" in steps[3]["detail"]
    assert all(s["state"] == "done" and s["time"] for s in steps)


def test_helpers_degrade_gracefully():
    assert traceability.event_details({"details": "no es json"}) == {}
    assert traceability.event_details({"details": '["lista"]'}) == {}
    assert traceability.fmt_local(None) == "" and traceability.fmt_local("basura") == ""
    assert traceability.fmt_duration(None) == "" and traceability.fmt_duration(3725) == "1 h 2 min"
    assert traceability.fmt_duration(75) == "1 min 15 s"
    assert traceability.short_id("") == "#—"
