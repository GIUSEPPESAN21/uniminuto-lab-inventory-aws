# -*- coding: utf-8 -*-
"""Punto 4 de v1.15: «despachado por X» en el préstamo, en item_history y en el correo."""

import pytest

from core import auth, dispatch, email_events, service_requests, traceability
from core import storage as storage_module
from core.storage import LabStorage

CODE = "LAB-MIC-01"
ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    hashed = auth.hash_password("ClaveSegura123")
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", hashed, role)
              for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 5}, CODE, is_new=True)
    return storage, people


def _picked(storage, people, operator=None, quantity=1):
    operator = operator or people["operario"]
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, quantity)
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    assert dispatch.claim_request(storage, row["id"], operator)[0]
    assert dispatch.start_picking(storage, row["id"], operator)[0]
    for _ in range(quantity):
        assert dispatch.register_reading(storage, row["id"], operator, CODE, "barcode_gun")[0]
    ok, message, _ = dispatch.complete_picking(storage, row["id"], operator)
    assert ok, message
    return row["id"]


def _loan(storage, request_id):
    return next(row for row in storage.get_all_loans() if row.get("request_id") == request_id)


def test_the_picking_loan_records_who_dispatched_it(lab):
    storage, people = lab
    loan = _loan(storage, _picked(storage, people))
    assert loan["operator_id"] == people["operario"]["id"]
    assert loan["operator_name"] == "Persona operario"
    assert loan["user_id"] == people["estudiante"]["id"]                  # el préstamo sigue siendo del cliente


def test_item_history_says_dispatched_by(lab):
    storage, people = lab
    _picked(storage, people)
    out = [row for row in storage.get_item_history(CODE) if row["type"] == "Salida"]
    assert len(out) == 1 and "despachado por Persona operario" in out[0]["details"]
    assert out[0]["quantity_change"] == -1                                # disponible = total − préstamos abiertos
    assert storage.get_available_quantity(CODE) == 4


def test_direct_checkouts_have_no_operator(lab):
    from core import loans as loans_core

    storage, people = lab
    _, _, loan = loans_core.checkout(storage, CODE, 1, people["estudiante"])
    stored = next(row for row in storage.get_all_loans() if row["id"] == loan["id"])
    assert stored["operator_id"] == "" and stored["operator_name"] == ""
    assert "despachado" not in storage.get_item_history(CODE)[0]["details"]


def test_rework_keeps_one_loan_and_moves_it_to_the_new_operator(lab):
    storage, people = lab
    other = storage.create_user("Otro operario", "otro@uniminuto.edu.co", "x", "operario")
    request_id = _picked(storage, people)
    assert dispatch.receive_request(storage, request_id, people["encargado"])[0]
    assert dispatch.rework_request(storage, request_id, people["encargado"], "falta una unidad")[0]
    assert dispatch.reassign_request(storage, request_id, people["encargado"], other, "turno")[0]
    assert dispatch.start_picking(storage, request_id, other)[0]
    assert dispatch.register_reading(storage, request_id, other, CODE, "manual")[0]
    assert dispatch.complete_picking(storage, request_id, other)[0]
    loans = [row for row in storage.get_all_loans() if row.get("request_id") == request_id]
    assert len(loans) == 1 and loans[0]["operator_name"] == "Otro operario"   # sin doble préstamo
    assert storage.get_available_quantity(CODE) == 4


def test_the_email_says_dispatched_by_in_checkout_and_checkin():
    loan = {"id": "abcdef123456", "item_id": CODE, "item_name": "Microscopio", "quantity": 1,
            "user_id": "u1", "operator_name": "Olga Operaria", "checkout_at": "2026-10-10T13:00:00+00:00",
            "return_at": "2026-10-11T13:00:00+00:00"}
    borrower = {"id": "u1", "full_name": "Ana Prueba", "role": "estudiante"}
    out = email_events.loan_checkout_content(loan, {"id": CODE}, borrower)
    back = email_events.loan_checkin_content(loan, borrower)
    assert "Despachado por" in out.text and "Olga Operaria" in out.text
    assert "Despachado por" in back.text and "Olga Operaria" in back.text
    plain = email_events.loan_checkout_content({**loan, "operator_name": ""}, {"id": CODE}, borrower)
    assert "Despachado por" not in plain.text


def test_completing_a_picking_notifies_the_checkout_with_the_operator(lab, monkeypatch):
    storage, people = lab
    sent = []
    monkeypatch.setattr(email_events, "notify_loan_checkout",
                        lambda st, loan, item, borrower: sent.append((loan, item, borrower)))
    _picked(storage, people)
    assert len(sent) == 1
    loan, item, borrower = sent[0]
    assert loan["operator_name"] == "Persona operario" and item["id"] == CODE
    assert borrower["id"] == people["estudiante"]["id"]


def test_a_failing_email_never_undoes_the_picking(lab, monkeypatch):
    storage, people = lab

    def boom(*args, **kwargs):
        raise RuntimeError("SMTP caído")

    monkeypatch.setattr(email_events, "notify_loan_checkout", boom)
    request_id = _picked(storage, people)
    stage = traceability.request_stage_key(storage.get_service_request(request_id),
                                           storage.get_trace_events(request_id=request_id))
    assert stage == traceability.STAGE_PICKED


def test_old_databases_load_without_the_operator_columns(tmp_path, monkeypatch):
    """Una base anterior (hoja loans sin operator_*) se carga y las columnas se completan."""
    import pandas as pd

    legacy_columns = [c for c in storage_module.SHEET_COLUMNS["loans"] if not c.startswith("operator_")]
    path = tmp_path / "legacy.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([{**{c: "" for c in legacy_columns}, "id": "L1", "item_id": CODE, "status": "returned",
                       "quantity": 1}]).to_excel(writer, sheet_name="loans", index=False)
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(path))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    loans = LabStorage().get_all_loans()
    assert loans[0]["id"] == "L1" and loans[0]["operator_id"] == "" and loans[0]["operator_name"] == ""
