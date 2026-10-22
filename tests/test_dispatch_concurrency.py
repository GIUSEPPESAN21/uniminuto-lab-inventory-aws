# -*- coding: utf-8 -*-
"""Punto 8 de v1.15: concurrencia con hilos sobre la base real (LabStorage): una sola
persona gana cada transición, nunca se vende más de lo que hay y no hay préstamos dobles."""

import threading

import pytest

from core import auth, dispatch, service_requests, traceability
from core.storage import LabStorage

CODE = "LAB-MIC-01"


@pytest.fixture
def lab():
    storage = LabStorage()
    hashed = auth.hash_password("ClaveSegura123")
    people = {
        "client": storage.create_user("Cliente", "cliente@uniminuto.edu.co", hashed, "estudiante"),
        "supervisors": [storage.create_user(f"Supervisor {i}", f"sup{i}@uniminuto.edu.co", hashed, "encargado")
                        for i in range(3)],
        "operators": [storage.create_user(f"Operario {i}", f"op{i}@uniminuto.edu.co", hashed, "operario")
                      for i in range(6)],
    }
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 3}, CODE, is_new=True)
    return storage, people


def _run(threads):
    errors = []

    def guard(target):
        def wrapper():
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - la prueba informa, no se traga el error
                errors.append(repr(exc))
        return wrapper

    workers = [threading.Thread(target=guard(t)) for t in threads]
    barrier = threading.Barrier(len(workers))
    for worker, target in zip(workers, threads):
        pass
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert not errors, errors
    assert not any(worker.is_alive() for worker in workers)
    del barrier


def _approved(storage, people, quantity=1):
    row, _, _ = service_requests.submit_request(storage, people["client"], "product", CODE, quantity)
    assert service_requests.review_request(storage, row["id"], "approved", people["supervisors"][0])[0]
    return row["id"]


def test_only_one_operator_claims_an_order(lab):
    storage, people = lab
    request_id = _approved(storage, people)
    results = []
    _run([lambda op=op: results.append(dispatch.claim_request(storage, request_id, op)[0])
          for op in people["operators"]])
    assert results.count(True) == 1 and results.count(False) == 5
    events = storage.get_trace_events(request_id=request_id)
    assert len([e for e in events if e["event_type"] == traceability.EVENT_ORDER_CLAIMED]) == 1
    assert len([e for e in events if e["event_type"] == traceability.EVENT_ORDER_ACTION_FAILED]) == 5   # intentos auditados


def test_two_supervisors_cannot_both_approve_or_both_receive(lab):
    storage, people = lab
    row, _, _ = service_requests.submit_request(storage, people["client"], "product", CODE, 1)
    results = []
    _run([lambda s=s: results.append(service_requests.review_request(storage, row["id"], "approved", s)[0])
          for s in people["supervisors"]])
    assert results.count(True) == 1
    assert len(storage.get_trace_events(request_id=row["id"], event_type=traceability.EVENT_ORDER_APPROVED)) == 1
    operator = people["operators"][0]
    assert dispatch.claim_request(storage, row["id"], operator)[0]
    dispatch.start_picking(storage, row["id"], operator)
    dispatch.register_reading(storage, row["id"], operator, CODE, "manual")
    assert dispatch.complete_picking(storage, row["id"], operator)[0]
    received = []
    _run([lambda s=s: received.append(dispatch.receive_request(storage, row["id"], s)[0])
          for s in people["supervisors"]])
    assert received.count(True) == 1


def test_stock_is_never_oversold_when_many_pickings_complete_at_once(lab):
    storage, people = lab                                                  # hay 3 unidades
    request_ids = [_approved(storage, people) for _ in range(5)]
    for request_id, operator in zip(request_ids, people["operators"]):
        assert dispatch.claim_request(storage, request_id, operator)[0]
        dispatch.start_picking(storage, request_id, operator)
        assert dispatch.register_reading(storage, request_id, operator, CODE, "barcode_gun")[0]
    results = []
    _run([lambda r=r, o=o: results.append(dispatch.complete_picking(storage, r, o)[0])
          for r, o in zip(request_ids, people["operators"])])
    assert results.count(True) == 3 and results.count(False) == 2        # no más de lo que hay
    assert storage.get_available_quantity(CODE) == 0
    open_loans = [l for l in storage.get_all_loans(status="out") if l["item_id"] == CODE]
    assert len(open_loans) == 3 and sum(l["quantity"] for l in open_loans) == 3


def test_a_double_click_on_complete_creates_a_single_loan(lab):
    storage, people = lab
    request_id = _approved(storage, people)
    operator = people["operators"][0]
    dispatch.claim_request(storage, request_id, operator)
    dispatch.start_picking(storage, request_id, operator)
    dispatch.register_reading(storage, request_id, operator, CODE, "manual")
    readings = dispatch.current_readings(operator, request_id)
    results = []
    _run([lambda: results.append(dispatch.complete_picking(storage, request_id, operator, readings)[0])
          for _ in range(4)])
    assert results.count(True) == 1
    assert len([l for l in storage.get_all_loans() if l.get("request_id") == request_id]) == 1
    assert storage.get_available_quantity(CODE) == 2


def test_parallel_picking_reads_from_two_tabs_never_exceed_the_quantity(lab):
    storage, people = lab
    request_id = _approved(storage, people, quantity=2)
    operator = people["operators"][0]
    dispatch.claim_request(storage, request_id, operator)
    dispatch.start_picking(storage, request_id, operator)
    outcomes = []
    _run([lambda m=m: outcomes.append(dispatch.register_reading(storage, request_id, operator, CODE, m)[0])
          for m in ("barcode_gun", "nfc", "nfc", "manual", "barcode_gun")])
    # Cinco lecturas de la misma etiqueta desde dos pestañas: todas son válidas, pero la etiqueta se guarda UNA vez.
    assert outcomes.count(True) == 5 and len(dispatch.current_readings(operator, request_id)) == 1


def test_list_refreshes_during_writes_never_raise(lab):
    storage, people = lab
    for _ in range(3):
        _approved(storage, people)
    stop = threading.Event()
    failures = []

    def reader():
        try:
            while not stop.is_set():
                dispatch.queue_for(storage, people["operators"][0])
                dispatch.review_queue(storage, people["supervisors"][0])
        except Exception as exc:  # noqa: BLE001
            failures.append(repr(exc))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for request in storage.get_service_requests(status="approved"):
            dispatch.claim_request(storage, request["id"], people["operators"][1])
    finally:
        stop.set()
        thread.join(timeout=30)
    assert failures == []
