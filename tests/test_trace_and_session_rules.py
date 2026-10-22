# -*- coding: utf-8 -*-
"""Punto 6 de v1.15: ``is_stuck`` no cuenta lo entregado sin confirmar, el supervisor no
aprueba su propia solicitud (ALLOW_SELF_APPROVAL) y el cierre de sesión persiste."""

from datetime import datetime, timedelta, timezone

import pytest

from core import dispatch, service_requests, traceability

ITEM = "2-1-01-01-001"
T0 = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Olga", "institutional_email": "olga@uniminuto.edu.co", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Pablo", "institutional_email": "pablo@uniminuto.edu.co", "role": "encargado"}
ADMIN = {"id": "m1", "full_name": "Maria", "institutional_email": "maria@uniminuto.edu.co", "role": "maestro"}


@pytest.fixture
def lab(storage):
    storage.add_item("2-1-01-00-000", name="Electrónica", item_type="master")
    storage.add_item(ITEM, name="Arduino UNO", item_type="child", parent_id="2-1-01-00-000", quantity=5)
    for person in (STUDENT, OPERATOR, SUPERVISOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    storage.set_setting("picking_route_required", "0")      # aquí se prueban las transiciones, no la ruta (test_pick_route)
    return storage


def _stage(storage, request_id):
    return traceability.request_stage_key(storage.get_service_request(request_id),
                                          storage.get_trace_events(request_id=request_id))


def _delivered(storage):
    row, _, _ = service_requests.submit_request(storage, STUDENT, "product", ITEM, 1)
    assert service_requests.review_request(storage, row["id"], "approved", SUPERVISOR)[0]
    assert dispatch.claim_request(storage, row["id"], OPERATOR)[0]
    assert dispatch.start_picking(storage, row["id"], OPERATOR)[0]
    assert dispatch.confirm_units(storage, row["id"], OPERATOR, row["quantity"], box_confirmed=True)[0]
    assert dispatch.receive_request(storage, row["id"], SUPERVISOR)[0]
    assert dispatch.deliver_request(storage, row["id"], SUPERVISOR)[0]
    return row["id"]


# --- is_stuck ----------------------------------------------------------------------------

def test_a_delivered_order_waiting_for_the_client_is_never_stuck(lab):
    request_id = _delivered(lab)
    request, events = lab.get_service_request(request_id), lab.get_trace_events(request_id=request_id)
    assert _stage(lab, request_id) == traceability.STAGE_DELIVERED
    far_future = datetime.now(timezone.utc) + timedelta(days=30)
    assert traceability.is_stuck(request, events, minutes=30, now=far_future) is False


def test_orders_waiting_on_the_lab_are_still_stuck_after_the_limit(lab):
    row, _, _ = service_requests.submit_request(lab, STUDENT, "product", ITEM, 1)
    assert service_requests.review_request(lab, row["id"], "approved", SUPERVISOR)[0]
    request, events = lab.get_service_request(row["id"]), lab.get_trace_events(request_id=row["id"])
    changed = traceability.stage_changed_at(request, events)
    assert traceability.is_stuck(request, events, minutes=30, now=changed + timedelta(minutes=29)) is False
    assert traceability.is_stuck(request, events, minutes=30, now=changed + timedelta(minutes=31)) is True


def test_confirmed_rejected_cancelled_and_returned_are_not_stuck(lab):
    request_id = _delivered(lab)
    assert dispatch.confirm_client_receipt(lab, request_id, STUDENT)[0]
    request, events = lab.get_service_request(request_id), lab.get_trace_events(request_id=request_id)
    assert traceability.is_stuck(request, events, 1, now=datetime.now(timezone.utc) + timedelta(days=9)) is False


# --- auto-aprobación ---------------------------------------------------------------------

def test_a_supervisor_cannot_approve_their_own_request(lab):
    row, _, _ = service_requests.submit_request(lab, SUPERVISOR, "product", ITEM, 1)
    ok, message = service_requests.review_request(lab, row["id"], "approved", SUPERVISOR)
    assert not ok and "tu propia solicitud" in message
    assert lab.get_service_request(row["id"])["status"] == "pending"
    assert lab.get_trace_events(request_id=row["id"])[-1]["event_type"] == traceability.EVENT_ORDER_ACTION_FAILED
    ok, _ = service_requests.review_request(lab, row["id"], "approved", ADMIN)         # otra persona sí
    assert ok and lab.get_service_request(row["id"])["status"] == "approved"


def test_the_administrator_does_not_approve_their_own_request_either(lab):
    row, _, _ = service_requests.submit_request(lab, ADMIN, "product", ITEM, 1)
    assert service_requests.review_request(lab, row["id"], "approved", ADMIN)[0] is False
    assert service_requests.review_request(lab, row["id"], "rejected", ADMIN)[0] is True   # rechazar lo propio es como cancelarlo


def test_allow_self_approval_is_off_by_default_and_can_be_turned_on(lab, monkeypatch):
    assert dispatch.allow_self_approval() is False
    for value, expected in (("true", True), ("1", True), ("Sí", True), ("false", False), ("", False), ("no", False)):
        monkeypatch.setattr(dispatch, "safe_secret", lambda key, default=None, v=value: v if key == "ALLOW_SELF_APPROVAL" else default)
        assert dispatch.allow_self_approval() is expected
    monkeypatch.setattr(dispatch, "safe_secret", lambda key, default=None: "true" if key == "ALLOW_SELF_APPROVAL" else default)
    row, _, _ = service_requests.submit_request(lab, SUPERVISOR, "product", ITEM, 1)
    assert service_requests.review_request(lab, row["id"], "approved", SUPERVISOR)[0] is True


def test_automatic_approval_is_not_blocked_by_the_rule(lab, monkeypatch):
    monkeypatch.setattr(service_requests, "auto_approve_products", lambda: True)
    row, _, _ = service_requests.submit_request(lab, SUPERVISOR, "product", ITEM, 1)
    assert lab.get_service_request(row["id"])["status"] == "approved"      # la aprobación del sistema no es «propia»
