# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

import pytest

from core import notifications, reservations


def _user(role="estudiante", user_id="u1"):
    return {
        "id": user_id, "full_name": "Ana Prueba", "institutional_email": f"{user_id}@uniminuto.edu.co",
        "role": role,
    }


def _future(hours=2):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def test_validate_reservation_normalizes_and_uses_utc():
    data = reservations.validate_reservation(
        reservations.SCOPE_ACTIVITY, "  Robótica   básica ", " Práctica  semanal ", 12,
        _future(2), _future(4),
    )
    assert data["activity"] == "Robótica básica"
    assert data["purpose"] == "Práctica semanal"
    assert data["start_at"].tzinfo == timezone.utc


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"scope_type": "bad"}, "Selecciona"),
        ({"activity": ""}, "actividad"),
        ({"purpose": ""}, "propósito"),
        ({"attendees": 0}, "asistentes"),
    ],
)
def test_validate_reservation_rejects_bad_fields(kwargs, message):
    values = {
        "scope_type": reservations.SCOPE_ACTIVITY, "activity": "Robótica", "purpose": "Clase",
        "attendees": 10, "start_at": _future(2), "end_at": _future(4),
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        reservations.validate_reservation(**values)


def test_validate_reservation_rejects_past_and_reversed_ranges():
    with pytest.raises(ValueError, match="futuro"):
        reservations.validate_reservation(
            reservations.SCOPE_FULL_LAB, "", "Evento", 10, _future(-2), _future(2)
        )
    with pytest.raises(ValueError, match="posterior"):
        reservations.validate_reservation(
            reservations.SCOPE_FULL_LAB, "", "Evento", 10, _future(4), _future(2)
        )


def test_conflict_rules_full_lab_and_same_activity_only():
    start, end = _future(2), _future(4)
    approved = {
        "id": "r1", "status": "approved", "scope_type": "activity",
        "activity": "Robótica", "start_at": start, "end_at": end,
    }
    same = {"scope_type": "activity", "activity": " robótica ", "start_at": _future(3), "end_at": _future(5)}
    other = {"scope_type": "activity", "activity": "Impresión 3D", "start_at": _future(3), "end_at": _future(5)}
    full = {"scope_type": "full_lab", "activity": "Laboratorio completo", "start_at": _future(3), "end_at": _future(5)}
    assert reservations.reservations_conflict(approved, same) is True
    assert reservations.reservations_conflict(approved, other) is False
    assert reservations.reservations_conflict(approved, full) is True
    assert reservations.reservations_conflict({**approved, "status": "pending"}, full) is False


def test_submit_reservation_saves_even_when_email_is_unconfigured(storage, monkeypatch):
    monkeypatch.setattr(notifications, "get_admin_notification_emails", lambda storage: [])
    monkeypatch.setattr(
        notifications, "send_email_notification", lambda subject, body, recipients, **_: (False, "SMTP no configurado")
    )
    row, notified, message = reservations.submit_reservation(
        storage, _user(), reservations.SCOPE_FULL_LAB, "", "Feria", 25, _future(2), _future(5)
    )
    assert row["status"] == "pending"
    assert notified is False and "SMTP" in message
    assert storage.get_reservation(row["id"])["email_error"] == "SMTP no configurado"


def test_review_reservation_enforces_role_and_conflicts(storage):
    candidate = storage.create_reservation(
        reservations.validate_reservation(
            reservations.SCOPE_FULL_LAB, "", "Evento", 20, _future(3), _future(5)
        ),
        _user(),
    )
    ok, _ = reservations.review_reservation(storage, candidate["id"], "approved", _user())
    assert ok is False

    existing = storage.create_reservation(
        reservations.validate_reservation(
            reservations.SCOPE_ACTIVITY, "Robótica", "Clase", 10, _future(2), _future(4)
        ),
        _user(user_id="u2"),
    )
    storage.update_reservation_status(existing["id"], "approved", "admin@uniminuto.edu.co")
    ok, message = reservations.review_reservation(
        storage, candidate["id"], "approved", _user("maestro", "admin")
    )
    assert ok is False and "Conflicto" in message


def test_owner_can_cancel_reservation(storage):
    user = _user()
    row = storage.create_reservation(
        reservations.validate_reservation(
            reservations.SCOPE_ACTIVITY, "Robótica", "Clase", 10, _future(2), _future(4)
        ), user,
    )
    ok, _ = reservations.cancel_reservation(storage, row["id"], user)
    assert ok is True
    assert storage.get_reservation(row["id"])["status"] == "cancelled"