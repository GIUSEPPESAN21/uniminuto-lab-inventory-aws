# -*- coding: utf-8 -*-
"""Reglas que deben cumplirse aunque dos personas actuen a la vez (stock y cruces
de reservas se re-verifican DENTRO del lock de la base) y fechas limite de
prestamos con la zona horaria de Colombia."""

import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core import loans as loans_core
from core import reservations as reservations_core
from core.storage import LabStorage

BOGOTA = ZoneInfo("America/Bogota")
REVIEWER = {"id": "rev-1", "role": "profesor", "institutional_email": "prof@uniminuto.edu.co"}


@pytest.fixture
def db():
    return LabStorage()


def _user(db, n):
    return db.create_user(f"Usuario Numero{n}", f"u{n}@uniminuto.edu.co", "hash", "estudiante", "Ing", f"ID-{n}")


def _item(db, code="LAB-001", quantity=3):
    db.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": quantity},
                 code, is_new=True, actor_email="prof@uniminuto.edu.co")


# --- stock ------------------------------------------------------------------------

def test_storage_refuses_to_lend_more_than_is_available(db):
    _item(db, quantity=2)
    item = db.get_item("LAB-001")
    db.create_loan(item, 2, _user(db, 1))
    with pytest.raises(ValueError, match="Stock insuficiente. Disponible: 0"):
        db.create_loan(item, 1, _user(db, 2))
    assert len(db.get_open_loans_for_item("LAB-001")) == 1


def test_checkout_reports_a_clean_error_when_the_precheck_was_stale(db, monkeypatch):
    _item(db, quantity=1)
    first, second = _user(db, 1), _user(db, 2)
    assert loans_core.checkout(db, "LAB-001", 1, first)[0]

    # La comprobacion previa de `checkout` ve datos viejos (otra persona acaba de
    # sacar la unidad): la capa de datos debe frenarlo igual.
    monkeypatch.setattr(db, "get_available_quantity", lambda item_id: 5)
    ok, message, loan = loans_core.checkout(db, "LAB-001", 1, second)
    assert not ok and loan is None and "Stock insuficiente" in message
    assert len(db.get_open_loans_for_item("LAB-001")) == 1


def test_concurrent_checkouts_never_oversell(db):
    _item(db, quantity=3)
    users = [_user(db, n) for n in range(1, 9)]
    results = []

    def take(user):
        results.append(loans_core.checkout(db, "LAB-001", 1, user)[0])

    threads = [threading.Thread(target=take, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 3
    assert len(db.get_open_loans_for_item("LAB-001")) == 3
    assert db.get_available_quantity("LAB-001") == 0


# --- reservas ---------------------------------------------------------------------

def _reservation(db, user, start, scope="full_lab", activity="Laboratorio completo", hours=2):
    return db.create_reservation(
        {"scope_type": scope, "activity": activity, "purpose": "Practica", "attendees": 10,
         "start_at": start, "end_at": start + timedelta(hours=hours)}, user,
    )


def test_storage_refuses_a_reservation_that_overlaps_an_approved_one(db):
    user = _user(db, 1)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    first = _reservation(db, user, start)
    assert db.update_reservation_status(first["id"], "approved", REVIEWER["institutional_email"])

    with pytest.raises(ValueError, match="se cruza"):
        _reservation(db, user, start + timedelta(hours=1))
    _reservation(db, user, start + timedelta(hours=3))     # fuera del cruce: permitido


def test_storage_refuses_the_second_overlapping_approval(db):
    user = _user(db, 1)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    first = _reservation(db, user, start)
    second = _reservation(db, user, start + timedelta(hours=1))   # ambas pendientes
    assert db.update_reservation_status(first["id"], "approved", "prof@uniminuto.edu.co")

    with pytest.raises(ValueError, match="Conflicto"):
        db.update_reservation_status(second["id"], "approved", "prof@uniminuto.edu.co")
    assert db.get_reservation(second["id"])["status"] == "pending"   # no quedo aprobada
    assert db.update_reservation_status(second["id"], "rejected", "prof@uniminuto.edu.co")


def test_review_reports_a_clean_error_when_the_precheck_was_stale(db, monkeypatch):
    user = _user(db, 1)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    first = _reservation(db, user, start)
    second = _reservation(db, user, start + timedelta(hours=1))
    assert reservations_core.review_reservation(db, first["id"], "approved", REVIEWER)[0]

    real = reservations_core.find_conflict
    calls = {"n": 0}

    def stale_first_call(existing, candidate, exclude_id=""):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(existing, candidate, exclude_id)

    monkeypatch.setattr(reservations_core, "find_conflict", stale_first_call)
    ok, message = reservations_core.review_reservation(db, second["id"], "approved", REVIEWER)
    assert not ok and "Conflicto" in message
    assert db.get_reservation(second["id"])["status"] == "pending"


def test_a_reservation_can_still_be_approved_when_there_is_no_overlap(db):
    user = _user(db, 1)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    only = _reservation(db, user, start)
    ok, message = reservations_core.review_reservation(db, only["id"], "approved", REVIEWER)
    assert ok and message == "Reserva aprobada."


# --- vencimiento de prestamos --------------------------------------------------------

def _loan_due(day):
    return {"status": "out", "expected_return_at": loans_core.deadline_from_date(day)}


def test_deadline_from_date_is_midnight_utc():
    assert loans_core.deadline_from_date(date(2026, 10, 10)) == datetime(2026, 10, 10, tzinfo=timezone.utc)


def test_date_deadline_is_not_overdue_during_the_whole_due_day_in_colombia():
    loan = _loan_due(date(2026, 10, 10))
    # Antes del arreglo vencia a las 7 p. m. del dia ANTERIOR (medianoche UTC).
    assert loans_core.is_overdue(loan, now=datetime(2026, 10, 9, 20, 0, tzinfo=BOGOTA)) is False
    assert loans_core.is_overdue(loan, now=datetime(2026, 10, 10, 23, 30, tzinfo=BOGOTA)) is False


def test_date_deadline_becomes_overdue_when_the_due_day_ends_in_colombia():
    loan = _loan_due(date(2026, 10, 10))
    assert loans_core.is_overdue(loan, now=datetime(2026, 10, 11, 0, 1, tzinfo=BOGOTA)) is True


def test_deadline_with_an_explicit_time_is_respected():
    due = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    loan = {"status": "out", "expected_return_at": due}
    assert loans_core.is_overdue(loan, now=due - timedelta(minutes=1)) is False
    assert loans_core.is_overdue(loan, now=due + timedelta(minutes=1)) is True


def test_naive_deadline_is_read_as_utc():
    naive = datetime(2026, 10, 10, 15, 0)
    loan = {"status": "out", "expected_return_at": naive}
    assert loans_core.is_overdue(loan, now=datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)) is True
