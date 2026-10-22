# -*- coding: utf-8 -*-
"""Hoja `trace_events` en el Excel real: escritura, filtros, persistencia,
migración automática de bases anteriores y UNA sola escritura (= un commit en
GitHub) por ruta completada, nunca una por paso."""

import json

import pandas as pd

from core import storage as storage_module
from core import traceability
from core.storage import LabStorage

ACTOR = "prof@uniminuto.edu.co"
STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}


def _restart():
    storage_module._cached_dfs = None


def test_events_are_appended_filtered_and_survive_a_restart():
    db = LabStorage()
    first = db.add_trace_event({"event_type": "route_verified", "request_id": "r1", "item_id": "I1",
                                "user_id": "s1", "receipt": "ABC123", "details": '{"a": 1}',
                                "created_at": "2026-10-09T10:00:00+00:00", "ignored": "x"})
    db.add_trace_event({"event_type": "picked_up", "request_id": "r1", "item_id": "I1", "user_id": "s1",
                        "loan_id": "L1", "created_at": "2026-10-09T11:00:00+00:00"})
    db.add_trace_event({"event_type": "picked_up", "request_id": "r2", "item_id": "I2", "user_id": "s2",
                        "created_at": "2026-10-09T12:00:00+00:00"})
    db.add_trace_event({"event_type": "service_started", "request_id": "r3", "user_id": "s2"})
    assert len(first["id"]) == 20 and "ignored" not in first
    _restart()
    assert [e["event_type"] for e in db.get_trace_events(request_id="r1")] == ["route_verified", "picked_up"]
    assert [e["request_id"] for e in db.get_trace_events(event_type="picked_up")] == ["r1", "r2"]
    assert db.get_trace_events(user_id="s1", event_type="picked_up")[0]["loan_id"] == "L1"
    assert db.get_trace_events(receipt="ABC123")[0]["details"] == '{"a": 1}'
    assert db.get_trace_events(request_id="r3")[0]["created_at"]       # se completa si falta
    assert db.get_trace_events(request_id="nada") == []
    assert len(db.get_trace_events()) == 4


def test_databases_from_before_this_version_get_the_trace_events_sheet():
    with pd.ExcelWriter(storage_module.EXCEL_PATH, engine="openpyxl") as writer:
        for sheet, columns in storage_module.SHEET_COLUMNS.items():
            if sheet != "trace_events":
                pd.DataFrame(columns=columns).to_excel(writer, sheet_name=sheet, index=False)
    _restart()
    db = LabStorage()
    assert "trace_events" in pd.ExcelFile(storage_module.EXCEL_PATH, engine="openpyxl").sheet_names
    assert db.get_trace_events() == []


def test_a_whole_route_costs_exactly_one_write(monkeypatch):
    db = LabStorage()
    db.save_item({"name": "Electrónica", "item_type": "master"}, "2-1-01-00-000", is_new=True, actor_email=ACTOR)
    db.save_item({"name": "Arduino", "item_type": "child", "parent_id": "2-1-01-00-000", "quantity": 2},
                 "2-1-01-01-001", is_new=True, actor_email=ACTOR)
    request = db.create_service_request({"request_type": "product", "item_id": "2-1-01-01-001",
                                         "item_name": "Arduino", "quantity": 1}, STUDENT)
    db.update_service_request_status(request["id"], "approved", ACTOR)
    request = db.get_service_request(request["id"])

    writes = []
    original = storage_module._write_excel
    monkeypatch.setattr(storage_module, "_write_excel", lambda dfs: (writes.append(1), original(dfs)))

    route = traceability.new_route(db.get_item("2-1-01-01-001"), db.get_item("2-1-01-00-000"), request)
    for code in ("2-1-01-00-000", "2-1-01-02-000", "2-1-01-01-000", "2-1-01-01-001"):
        traceability.apply_code(route, code, known_item=db.get_item(code))
    assert writes == []                                    # recorrer la ruta no escribe nada
    ok, _, event = traceability.save_route(db, request, route, STUDENT)
    assert ok and writes == [1]

    _restart()
    stored = db.get_trace_events(request_id=request["id"])[0]
    assert stored["receipt"] == event["receipt"]
    assert json.loads(stored["details"])["attempts"] == 1
    assert traceability.verify_receipt(db, event["receipt"])["valid"]
