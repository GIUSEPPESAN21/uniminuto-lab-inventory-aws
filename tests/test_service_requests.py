# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

import pytest

from core import notifications, service_requests


def _user(role="estudiante", user_id="u1"):
    return {
        "id": user_id, "full_name": "Ana Prueba", "institutional_email": f"{user_id}@uniminuto.edu.co",
        "role": role,
    }


def _future():
    return datetime.now(timezone.utc) + timedelta(days=1)


def test_submit_product_request_validates_stock_and_records_email(storage, monkeypatch):
    storage.add_item("LAB-MIC-01", name="Microscopio", quantity=2)
    monkeypatch.setattr(notifications, "get_admin_notification_emails", lambda storage: ["admin@uniminuto.edu.co"])
    monkeypatch.setattr(
        notifications, "send_email_notification", lambda subject, body, recipients, **_: (True, "Enviado")
    )
    row, notified, _ = service_requests.submit_request(
        storage, _user(), service_requests.TYPE_PRODUCT, "LAB-MIC-01", 2,
        description="Práctica de óptica", needed_at=_future(),
    )
    assert row["item_name"] == "Microscopio"
    assert row["quantity"] == 2
    assert notified is True
    assert storage.get_service_request(row["id"])["email_notified"] is True


def test_product_request_rejects_missing_master_retired_and_excess_stock(storage):
    storage.add_item("MASTER-01", item_type="master", quantity=1)
    storage.add_item("OLD-01", status="retired", quantity=1)
    storage.add_item("ITEM-01", quantity=1)
    with pytest.raises(ValueError, match="Contenedor Principal"):
        service_requests.validate_request(storage, "product", "MASTER-01", 1)
    with pytest.raises(ValueError, match="activo"):
        service_requests.validate_request(storage, "product", "OLD-01", 1)
    with pytest.raises(ValueError, match="Stock insuficiente"):
        service_requests.validate_request(storage, "product", "ITEM-01", 2)


def test_service_request_requires_name_and_description(storage):
    with pytest.raises(ValueError, match="servicio"):
        service_requests.validate_request(storage, "service", service_name="", description="Detalle")
    with pytest.raises(ValueError, match="Describe"):
        service_requests.validate_request(storage, "service", service_name="Impresión 3D", description="")


def test_submit_service_request_is_saved_if_email_fails(storage, monkeypatch):
    monkeypatch.setattr(notifications, "get_admin_notification_emails", lambda storage: [])
    monkeypatch.setattr(
        notifications, "send_email_notification", lambda subject, body, recipients, **_: (False, "Sin SMTP")
    )
    row, notified, message = service_requests.submit_request(
        storage, _user(), "service", service_name="Corte láser",
        description="Cortar una plantilla", needed_at=_future(),
    )
    assert row["status"] == "pending"
    assert notified is False and message == "Sin SMTP"
    assert storage.get_service_request(row["id"])["email_error"] == "Sin SMTP"


def test_review_request_checks_role_and_stock_again(storage):
    item = storage.add_item("ITEM-01", name="Arduino", quantity=1)
    row = storage.create_service_request(
        {
            "request_type": "product", "item_id": item["id"], "item_name": item["name"],
            "quantity": 1, "service_name": "", "description": "", "needed_at": "",
        }, _user(),
    )
    ok, _ = service_requests.review_request(storage, row["id"], "approved", _user())
    assert ok is False
    storage.create_loan(item, 1, _user())
    ok, message = service_requests.review_request(
        storage, row["id"], "approved", _user("maestro", "admin")
    )
    assert ok is False and "stock" in message.lower()


def test_owner_can_cancel_request(storage):
    user = _user()
    row = storage.create_service_request(
        {
            "request_type": "service", "item_id": "", "item_name": "", "quantity": 0,
            "service_name": "Impresión", "description": "Pieza", "needed_at": "",
        }, user,
    )
    ok, _ = service_requests.cancel_request(storage, row["id"], user)
    assert ok is True
    assert storage.get_service_request(row["id"])["status"] == "cancelled"