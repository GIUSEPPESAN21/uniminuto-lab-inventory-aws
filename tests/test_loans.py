# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone

from core import loans as loans_core


def _make_user(storage, role="estudiante"):
    return storage.add_user(f"{role}@uniminuto.edu.co", role=role, full_name=role.capitalize())


def test_checkout_reduces_availability(storage):
    storage.add_item("MULT-01", name="Multimetro", quantity=3)
    user = _make_user(storage)

    ok, msg, loan = loans_core.checkout(storage, "MULT-01", 2, user)

    assert ok is True
    assert loan is not None
    assert storage.get_available_quantity("MULT-01") == 1


def test_checkout_fails_when_insufficient_stock(storage):
    storage.add_item("MULT-01", name="Multimetro", quantity=1)
    user = _make_user(storage)

    ok, msg, loan = loans_core.checkout(storage, "MULT-01", 5, user)

    assert ok is False
    assert loan is None
    assert "insuficiente" in msg.lower()


def test_checkout_fails_on_master_container(storage):
    storage.add_item("CAJA-001", name="Caja", item_type="master", quantity=0)
    user = _make_user(storage)

    ok, msg, loan = loans_core.checkout(storage, "CAJA-001", 1, user)

    assert ok is False
    assert "contenedor principal" in msg.lower()


def test_checkout_rejects_non_positive_quantity(storage):
    storage.add_item("MULT-01", name="Multimetro", quantity=3)
    user = _make_user(storage)

    ok, msg, loan = loans_core.checkout(storage, "MULT-01", 0, user)

    assert ok is False


def test_checkin_restores_availability(storage):
    storage.add_item("MULT-01", name="Multimetro", quantity=3)
    user = _make_user(storage)
    _, _, loan = loans_core.checkout(storage, "MULT-01", 2, user)

    ok, msg = loans_core.checkin(storage, loan["id"], _make_user(storage, role="profesor"))

    assert ok is True
    assert storage.get_available_quantity("MULT-01") == 3


def test_checkin_fails_if_already_returned(storage):
    storage.add_item("MULT-01", name="Multimetro", quantity=3)
    user = _make_user(storage)
    _, _, loan = loans_core.checkout(storage, "MULT-01", 1, user)
    professor = _make_user(storage, role="profesor")
    loans_core.checkin(storage, loan["id"], professor)

    ok, msg = loans_core.checkin(storage, loan["id"], professor)

    assert ok is False


def test_is_overdue_true_when_expected_date_passed():
    loan = {"status": "out", "expected_return_at": datetime.now(timezone.utc) - timedelta(days=1)}
    assert loans_core.is_overdue(loan) is True


def test_is_overdue_false_without_expected_date():
    loan = {"status": "out", "expected_return_at": None}
    assert loans_core.is_overdue(loan) is False


def test_is_overdue_false_when_returned():
    loan = {"status": "returned", "expected_return_at": datetime.now(timezone.utc) - timedelta(days=1)}
    assert loans_core.is_overdue(loan) is False


def test_get_overdue_loans_filters_correctly(storage):
    storage.add_item("MULT-01", name="Multimetro", quantity=2)
    user = _make_user(storage)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    future = datetime.now(timezone.utc) + timedelta(days=2)

    loans_core.checkout(storage, "MULT-01", 1, user, expected_return_at=past)
    loans_core.checkout(storage, "MULT-01", 1, user, expected_return_at=future)

    overdue = loans_core.get_overdue_loans(storage)
    assert len(overdue) == 1
