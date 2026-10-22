# -*- coding: utf-8 -*-
"""Punto 8 de v1.15: transiciones válidas e inválidas del despacho, rework sin doble
préstamo y «volver al estante» (con el almacenamiento en memoria de las pruebas)."""

import pytest

from core import dispatch, permissions, service_requests, traceability

ITEM = "2-1-01-01-001"
STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Olga", "institutional_email": "olga@uniminuto.edu.co", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Pablo", "institutional_email": "pablo@uniminuto.edu.co", "role": "encargado"}
ADMIN = {"id": "m1", "full_name": "Maria", "institutional_email": "maria@uniminuto.edu.co", "role": "maestro"}

# Cómo llegar a cada etapa desde una solicitud nueva (cada paso es una función del dominio).
STEPS = {
    traceability.STAGE_REQUESTED: [],
    traceability.STAGE_APPROVED: ["approve"],
    traceability.STAGE_CLAIMED: ["approve", "claim"],
    traceability.STAGE_PICKED: ["approve", "claim", "pick"],
    traceability.STAGE_RECEIVED: ["approve", "claim", "pick", "receive"],
    traceability.STAGE_DELIVERED: ["approve", "claim", "pick", "receive", "deliver"],
    traceability.STAGE_CONFIRMED: ["approve", "claim", "pick", "receive", "deliver", "confirm"],
    traceability.STAGE_REJECTED: ["reject"],
    traceability.STAGE_CANCELLED: ["cancel"],
    traceability.STAGE_RETURNED: ["approve", "claim", "pick", "receive", "shelf"],
}


@pytest.fixture
def lab(storage):
    storage.add_item("2-1-01-00-000", name="Electrónica", item_type="master")
    storage.add_item(ITEM, name="Arduino UNO", item_type="child", parent_id="2-1-01-00-000", quantity=500)
    for person in (STUDENT, OPERATOR, SUPERVISOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    storage.set_setting("picking_route_required", "0")      # aquí se prueban las transiciones, no la ruta (test_pick_route)
    return storage


def _advance(storage, request_id, steps):
    actions = {
        "approve": lambda: service_requests.review_request(storage, request_id, "approved", SUPERVISOR)[0],
        "reject": lambda: service_requests.review_request(storage, request_id, "rejected", SUPERVISOR)[0],
        "cancel": lambda: service_requests.cancel_request(storage, request_id, STUDENT)[0],
        "claim": lambda: dispatch.claim_request(storage, request_id, OPERATOR)[0],
        "pick": lambda: (dispatch.start_picking(storage, request_id, OPERATOR)[0]
                         and dispatch.confirm_units(storage, request_id, OPERATOR,
                                                    storage.get_service_request(request_id)["quantity"],
                                                    box_confirmed=True)[0]),
        "receive": lambda: dispatch.receive_request(storage, request_id, SUPERVISOR)[0],
        "deliver": lambda: dispatch.deliver_request(storage, request_id, SUPERVISOR)[0],
        "confirm": lambda: dispatch.confirm_client_receipt(storage, request_id, STUDENT)[0],
        "shelf": lambda: dispatch.return_to_shelf(storage, request_id, SUPERVISOR, "no era lo pedido")[0],
    }
    for step in steps:
        assert actions[step](), step


def _order(storage, stage):
    row, _, _ = service_requests.submit_request(storage, STUDENT, "product", ITEM, 1)
    _advance(storage, row["id"], STEPS[stage])
    assert traceability.request_stage_key(storage.get_service_request(row["id"]),
                                          storage.get_trace_events(request_id=row["id"])) == stage
    return row["id"]


# Evento de aplicar -> quién lo hace (con permiso) y cómo se llama en el dominio.
ATTEMPTS = {
    traceability.EVENT_ORDER_APPROVED: (SUPERVISOR, {}),
    traceability.EVENT_ORDER_REJECTED: (SUPERVISOR, {}),
    traceability.EVENT_ORDER_CANCELLED: (SUPERVISOR, {}),   # quien revisa puede cancelar en cualquier etapa abierta
    traceability.EVENT_ORDER_AUTHORIZED: (SUPERVISOR, {"authorized_pickers": []}),
    traceability.EVENT_ORDER_CLAIMED: (OPERATOR, {}),
    traceability.EVENT_ORDER_RELEASED: (OPERATOR, {"reason": "x"}),
    traceability.EVENT_ORDER_PICKED: (OPERATOR, {"readings": [{"code": ITEM, "method": "manual"}]}),
    traceability.EVENT_ORDER_RECEIVED: (SUPERVISOR, {}),
    traceability.EVENT_ORDER_DELIVERED: (SUPERVISOR, {}),
    traceability.EVENT_ORDER_REWORK: (SUPERVISOR, {"reason": "falta una unidad"}),
    traceability.EVENT_ORDER_RETURNED_TO_SHELF: (SUPERVISOR, {"reason": "no era lo pedido"}),
    traceability.EVENT_ORDER_CLIENT_CONFIRMED: (STUDENT, {}),
}


def _snapshot(storage, request_id):
    request = storage.get_service_request(request_id)
    loans = sorted((l["id"], l["status"], l["quantity"]) for l in storage.loans.values())
    return (traceability.request_stage_key(request, storage.get_trace_events(request_id=request_id)),
            request["status"], loans, storage.get_available_quantity(ITEM))


@pytest.mark.parametrize("stage", list(STEPS))
def test_every_event_is_valid_only_where_the_table_allows_it(lab, stage):
    """Matriz completa: 10 etapas × 12 eventos. Lo no permitido no cambia nada y deja auditoría."""
    allowed = traceability.ALLOWED_TRANSITIONS.get(stage, {})
    for event, (actor, details) in ATTEMPTS.items():
        request_id = _order(lab, stage)
        before = _snapshot(lab, request_id)
        count_before = len(lab.get_trace_events(request_id=request_id))
        ok, message, row = lab.transition_request(request_id, event, actor, dict(details))
        if event in allowed:
            # el evento está en la tabla: pasa, salvo que una regla de dominio lo frene (aquí no hay)
            assert ok, (stage, event, message)
        else:
            assert not ok and message, (stage, event)
            assert _snapshot(lab, request_id) == before, (stage, event)   # nada cambió
            events = lab.get_trace_events(request_id=request_id)
            assert len(events) == count_before + 1                         # solo el intento rechazado, auditado
            assert events[-1]["event_type"] in {traceability.EVENT_ORDER_ACTION_FAILED,
                                                traceability.EVENT_ORDER_SCAN_FAILED}


def test_permissions_are_checked_before_the_stage(lab):
    request_id = _order(lab, traceability.STAGE_REQUESTED)
    ok, message, _ = lab.transition_request(request_id, traceability.EVENT_ORDER_APPROVED, STUDENT, {})
    assert not ok and "permiso" in message
    ok, message, _ = lab.transition_request(request_id, traceability.EVENT_ORDER_APPROVED, OPERATOR, {})
    assert not ok and "permiso" in message
    assert traceability.request_stage_key(lab.get_service_request(request_id), []) == traceability.STAGE_REQUESTED


def test_only_the_owner_confirms_and_cancels_only_while_the_request_is_in_review(lab):
    delivered = _order(lab, traceability.STAGE_DELIVERED)
    other = {"id": "s2", "full_name": "Otra", "institutional_email": "o@x.edu.co", "role": "estudiante"}
    ok, message, _ = lab.transition_request(delivered, traceability.EVENT_ORDER_CLIENT_CONFIRMED, other, {})
    assert not ok and "solicitante" in message
    requested = _order(lab, traceability.STAGE_REQUESTED)
    assert lab.transition_request(requested, traceability.EVENT_ORDER_CANCELLED, other, {})[0] is False   # ajena: no
    assert lab.transition_request(requested, traceability.EVENT_ORDER_CANCELLED, STUDENT, {})[0] is True   # propia en revisión: sí
    approved = _order(lab, traceability.STAGE_APPROVED)
    ok, message, _ = lab.transition_request(approved, traceability.EVENT_ORDER_CANCELLED, STUDENT, {})
    assert not ok and "mientras está en revisión" in message             # ya aprobada: solo quien revisa la cancela
    assert lab.transition_request(approved, traceability.EVENT_ORDER_CANCELLED, SUPERVISOR, {})[0] is True


def test_picking_needs_the_exact_quantity_the_right_product_and_available_stock(lab):
    row, _, _ = service_requests.submit_request(lab, STUDENT, "product", ITEM, 2)
    _advance(lab, row["id"], ["approve", "claim"])
    ok, message, _ = dispatch.complete_picking(lab, row["id"], OPERATOR, [{"code": ITEM, "method": "manual"}])
    assert not ok and "Faltan lecturas" in message
    ok, message, event = dispatch.complete_picking(
        lab, row["id"], OPERATOR, [{"code": ITEM, "method": "manual"}, {"code": "OTRO", "method": "manual"}])
    assert not ok and "no corresponde" in message and event["event_type"] == traceability.EVENT_ORDER_SCAN_FAILED
    assert lab.get_available_quantity(ITEM) == 500 and lab.loans == {}


def test_rework_reuses_the_open_loan_and_never_loans_twice(lab):
    request_id = _order(lab, traceability.STAGE_RECEIVED)
    assert len(lab.loans) == 1 and lab.get_available_quantity(ITEM) == 499
    assert dispatch.rework_request(lab, request_id, SUPERVISOR, "falta rotular")[0]
    assert traceability.request_stage_key(lab.get_service_request(request_id),
                                          lab.get_trace_events(request_id=request_id)) == traceability.STAGE_CLAIMED
    assert dispatch.start_picking(lab, request_id, OPERATOR)[0]
    assert dispatch.confirm_units(lab, request_id, OPERATOR, lab.get_service_request(request_id)["quantity"],
                                  box_confirmed=True)[0]
    assert len(lab.loans) == 1 and lab.get_available_quantity(ITEM) == 499   # el mismo préstamo; no se descuenta dos veces
    picked = lab.get_trace_events(request_id=request_id, event_type=traceability.EVENT_ORDER_PICKED)
    assert traceability.event_details(picked[-1]).get("reused_loan") is True
    assert dispatch.rework_request(lab, request_id, SUPERVISOR, "")[0] is False   # el motivo es obligatorio


def test_returning_to_the_shelf_closes_the_loan_and_restores_availability(lab):
    request_id = _order(lab, traceability.STAGE_RECEIVED)
    assert lab.get_available_quantity(ITEM) == 499
    assert dispatch.return_to_shelf(lab, request_id, SUPERVISOR, "")[0] is False      # motivo obligatorio
    assert dispatch.return_to_shelf(lab, request_id, SUPERVISOR, "no era lo pedido")[0]
    assert lab.get_available_quantity(ITEM) == 500                                     # disponible = total − préstamos abiertos
    assert all(loan["status"] == "returned" for loan in lab.loans.values())
    assert traceability.request_stage_key(lab.get_service_request(request_id),
                                          lab.get_trace_events(request_id=request_id)) == traceability.STAGE_RETURNED
    ok, message, _ = dispatch.rework_request(lab, request_id, SUPERVISOR, "otra vez")
    assert not ok                                                                      # etapa final: no se reabre


def test_release_and_reassign_rules(lab):
    request_id = _order(lab, traceability.STAGE_CLAIMED)
    other = {"id": "o2", "full_name": "Omar", "institutional_email": "omar@x.edu.co", "role": "operario",
             "status": "active"}
    lab.users["o2"] = other
    assert dispatch.release_request(lab, request_id, other, "no es mío")[0] is False   # solo quien lo tomó o un supervisor
    assert dispatch.reassign_request(lab, request_id, SUPERVISOR, {**OPERATOR, "status": "active"})[0] is False  # ya es suyo
    assert dispatch.reassign_request(lab, request_id, SUPERVISOR, other, "turno")[0]
    assert dispatch.assigned_picker(lab.get_trace_events(request_id=request_id)) == "o2"
    assert dispatch.reassign_request(lab, request_id, SUPERVISOR, {**other, "status": "disabled"})[0] is False
    assert dispatch.release_request(lab, request_id, SUPERVISOR, "se acabó el turno")[0]
    assert traceability.request_stage_key(lab.get_service_request(request_id),
                                          lab.get_trace_events(request_id=request_id)) == traceability.STAGE_APPROVED


def test_any_operator_or_supervisor_can_pick_any_authorized_order(lab):
    """v1.19: ya no se asigna a quién; el pedido lo elige quien lo despacha (aunque una aprobación antigua traiga una lista)."""
    lab.users["o2"] = {"id": "o2", "full_name": "Omar", "institutional_email": "omar@x.edu.co", "role": "operario",
                       "status": "active"}
    row, _, _ = service_requests.submit_request(lab, STUDENT, "product", ITEM, 1)
    assert service_requests.review_request(lab, row["id"], "approved", SUPERVISOR, authorized_pickers=["o2"])[0]
    assert len(dispatch.queue_for(lab, OPERATOR)) == 1 and len(dispatch.queue_for(lab, lab.users["o2"])) == 1
    assert len(dispatch.queue_for(lab, SUPERVISOR)) == 1                    # el supervisor también ve la cola
    ok, message, _ = dispatch.claim_request(lab, row["id"], OPERATOR)
    assert ok, message


def test_only_product_requests_go_through_dispatch(lab):
    row, _, _ = service_requests.submit_request(lab, STUDENT, "service", service_name="Impresión 3D",
                                                description="Pieza de práctica")
    ok, message, _ = lab.transition_request(row["id"], traceability.EVENT_ORDER_AUTHORIZED, SUPERVISOR, {})
    assert not ok
    assert permissions.has_cap(SUPERVISOR, permissions.CAP_AUTHORIZE_DISPATCH)
