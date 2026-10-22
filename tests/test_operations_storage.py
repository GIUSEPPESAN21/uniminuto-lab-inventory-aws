# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core import storage as storage_module


@pytest.fixture
def operations_storage(tmp_path, monkeypatch):
    db = tmp_path / "operations.xlsx"
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(db))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    monkeypatch.setattr(storage_module, "_is_github_configured", lambda: False)
    storage = storage_module.LabStorage()
    yield storage, db
    storage_module._cached_dfs = None


def _user(storage, email="ana@uniminuto.edu.co"):
    return storage.create_user(
        "Ana Prueba", email, "hash", "estudiante", "Ingeniería", "ID-1"
    )


def test_new_sheets_are_created_automatically(operations_storage):
    _, db = operations_storage
    sheets = set(pd.ExcelFile(db, engine="openpyxl").sheet_names)
    assert {"reservations", "service_requests"}.issubset(sheets)


def test_reservation_round_trip_status_and_notification(operations_storage):
    storage, _ = operations_storage
    user = _user(storage)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    row = storage.create_reservation(
        {
            "scope_type": "full_lab", "activity": "Laboratorio completo",
            "purpose": "Feria", "attendees": 20,
            "start_at": start, "end_at": start + timedelta(hours=2),
        }, user,
    )
    loaded = storage.get_reservation(row["id"])
    assert loaded["requester_id"] == user["id"]
    assert loaded["attendees"] == 20
    assert loaded["start_at"].tzinfo is not None
    assert storage.update_reservation_status(row["id"], "approved", "admin@uniminuto.edu.co", "OK")
    assert storage.update_reservation_notification(row["id"], True)
    loaded = storage.get_reservation(row["id"])
    assert loaded["status"] == "approved"
    assert loaded["email_notified"] is True


def test_service_request_round_trip(operations_storage):
    storage, _ = operations_storage
    user = _user(storage)
    row = storage.create_service_request(
        {
            "request_type": "service", "item_id": "", "item_name": "", "quantity": 0,
            "service_name": "Impresión 3D", "description": "Prototipo", "needed_at": "",
        }, user,
    )
    assert storage.get_service_request(row["id"])["service_name"] == "Impresión 3D"
    assert storage.get_service_requests(user_id=user["id"])[0]["id"] == row["id"]
    assert storage.update_service_request_status(
        row["id"], "rejected", "admin@uniminuto.edu.co", "Sin material"
    )
    assert storage.update_service_request_notification(row["id"], False, "Sin SMTP")
    loaded = storage.get_service_request(row["id"])
    assert loaded["status"] == "rejected"
    assert loaded["email_error"] == "Sin SMTP"


def test_update_user_returns_data_rejects_unknown_and_duplicate_email(operations_storage):
    storage, _ = operations_storage
    first = _user(storage, "one@uniminuto.edu.co")
    second = _user(storage, "two@uniminuto.edu.co")
    updated = storage.update_user(first["id"], {"full_name": "Ana Corregida"})
    assert updated["full_name"] == "Ana Corregida"
    with pytest.raises(ValueError, match="no permitidos"):
        storage.update_user(first["id"], {"is_admin": True})
    with pytest.raises(ValueError, match="otra cuenta"):
        storage.update_user(first["id"], {"institutional_email": second["institutional_email"]})