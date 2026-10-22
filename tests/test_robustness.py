# -*- coding: utf-8 -*-
"""v1.18 · fallos latentes: carreras al cargar la caché, cuentas y reservas duplicadas, préstamos inválidos,
firmas NFC no ASCII, sesiones revocadas y mensajes de error sin detalles técnicos."""

import threading
from datetime import datetime, timedelta

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth, nfc, reservations, session  # noqa: E402
from core import storage as sm  # noqa: E402
from core.storage import LabStorage  # noqa: E402

ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role) for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 5}, "LAB-MIC-01", is_new=True)
    return storage, people


# --- cuentas ---------------------------------------------------------------------------------------

def test_the_same_email_cannot_create_two_accounts_even_concurrently(lab):
    storage, _ = lab
    outcomes = []

    def register():
        try:
            storage.create_user("Dup", "dup@uniminuto.edu.co", "x", "estudiante")
            outcomes.append("ok")
        except ValueError:
            outcomes.append("duplicado")

    threads = [threading.Thread(target=register) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1 and outcomes.count("duplicado") == 7
    assert len([u for u in storage.get_all_users() if u["institutional_email"] == "dup@uniminuto.edu.co"]) == 1


def test_a_registration_that_loses_the_race_gets_a_clear_message(lab, monkeypatch):
    storage, _ = lab
    prepared, error = auth.validate_registration(storage, "Nueva Persona", "nueva@uniminuto.edu.co", "ClaveSegura123",
                                                 "Ing", "123")
    assert not error
    storage.create_user("Otra", "nueva@uniminuto.edu.co", "x", "estudiante")        # ganó la carrera
    monkeypatch.setattr(storage, "get_user_by_email", lambda email: None)           # la comprobación previa pasó
    user, message = auth.create_account(storage, prepared)
    assert user is None and "Ya existe una cuenta" in message


# --- préstamos -------------------------------------------------------------------------------------

def test_loans_reject_non_positive_quantities_and_retired_items(lab):
    storage, people = lab
    item = storage.get_item("LAB-MIC-01")
    for quantity in (0, -3):
        with pytest.raises(ValueError):
            storage.create_loan(item, quantity, people["estudiante"])
    storage.retire_item("LAB-MIC-01", actor=people["profesor"])
    with pytest.raises(ValueError, match="dado de baja"):
        storage.create_loan(item, 1, people["estudiante"])
    assert storage.get_all_loans() == []


# --- reservas --------------------------------------------------------------------------------------

def _reserve(storage, user, hours=48):
    start = datetime.now(reservations.BOGOTA_TZ) + timedelta(hours=hours)
    row, _, _ = reservations.submit_reservation(storage, user, reservations.SCOPE_ACTIVITY, "Práctica", "Robótica",
                                                10, start, start + timedelta(hours=2))
    return row


def test_only_roles_with_the_reserve_capability_can_book(lab):
    storage, people = lab
    with pytest.raises(PermissionError):
        reservations.submit_reservation(storage, people["operario"], reservations.SCOPE_ACTIVITY, "x", "y", 5,
                                        datetime.now(reservations.BOGOTA_TZ) + timedelta(days=1),
                                        datetime.now(reservations.BOGOTA_TZ) + timedelta(days=1, hours=2))
    assert _reserve(storage, people["estudiante"])["status"] == "pending"


def test_nobody_approves_their_own_reservation_unless_the_demo_switch_is_on(lab, monkeypatch):
    storage, people = lab
    mine = _reserve(storage, people["profesor"])
    ok, message = reservations.review_reservation(storage, mine["id"], "approved", people["profesor"])
    assert not ok and "tu propia reserva" in message
    assert reservations.review_reservation(storage, mine["id"], "rejected", people["profesor"])[0]    # rechazar sí
    other = _reserve(storage, people["profesor"], hours=100)
    assert reservations.review_reservation(storage, other["id"], "approved", people["maestro"])[0]
    third = _reserve(storage, people["profesor"], hours=200)
    monkeypatch.setattr(sm, "safe_secret", lambda key, default=None: "true" if key == "ALLOW_SELF_APPROVAL" else default)
    from core import dispatch
    monkeypatch.setattr(dispatch, "safe_secret", lambda key, default=None: "true" if key == "ALLOW_SELF_APPROVAL" else default)
    assert reservations.review_reservation(storage, third["id"], "approved", people["profesor"])[0]


def test_two_simultaneous_reviews_of_one_reservation_cannot_both_win(lab):
    storage, people = lab
    row = _reserve(storage, people["estudiante"])
    assert storage.update_reservation_status(row["id"], "approved", "a@uniminuto.edu.co") is True
    with pytest.raises(ValueError, match="ya fue revisada"):
        storage.update_reservation_status(row["id"], "rejected", "b@uniminuto.edu.co")
    assert storage.get_reservation(row["id"])["status"] == "approved"
    assert reservations.cancel_reservation(storage, row["id"], people["estudiante"])[0]      # cancelar sigue siendo posible


# --- caché ------------------------------------------------------------------------------------------

def test_a_slow_cache_load_never_overwrites_a_newer_published_version(lab, monkeypatch):
    storage, _ = lab
    newer = {name: frame.copy() for name, frame in sm._read_excel().items()}
    sm._cached_dfs = None
    real = sm.pd.ExcelFile

    def slow_excel_file(*args, **kwargs):
        book = real(*args, **kwargs)
        sm._cached_dfs = newer              # una escritura publica su versión mientras este hilo parseaba
        return book

    monkeypatch.setattr(sm.pd, "ExcelFile", slow_excel_file)
    assert sm._load_cache() is newer and sm._cached_dfs is newer


def test_concurrent_readers_and_writers_lose_nothing(lab):
    storage, _ = lab
    errors = []

    def write(n):
        try:
            storage.save_item({"name": f"Item {n}", "item_type": "standalone", "quantity": 1}, f"LAB-R-{n:03d}", is_new=True)
        except Exception as exc:       # pragma: no cover - solo si falla la prueba
            errors.append(exc)

    def read():
        for _ in range(20):
            storage.get_all_items()
            storage.get_available_quantity("LAB-MIC-01")

    threads = [threading.Thread(target=write, args=(n,)) for n in range(10)] + [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and len(storage.get_all_items()) == 11


# --- NFC y sesiones ---------------------------------------------------------------------------------

def test_a_non_ascii_signature_is_invalid_not_a_crash():
    assert nfc.check_signature("2-1-01-01-001", "FIRMA-ñandú-€", "secreto") == nfc.SIG_INVALID


def test_revoked_sessions_drop_the_oldest_instead_of_clearing_everything(monkeypatch):
    session._revoked.clear()
    monkeypatch.setattr(session, "_REVOKED_LIMIT", 50)
    for number in range(50):
        session._revoked[f"old-{number}"] = None
    session.revoke({"sid": "recent", "exp": 9999999999})
    assert "recent" in session._revoked and "old-49" in session._revoked          # las recientes siguen cerradas
    assert "old-0" not in session._revoked and len(session._revoked) <= 50
    session._revoked.clear()


# --- mensajes de error --------------------------------------------------------------------------------

def _error_script():
    from core.ui import load_error

    load_error("No se pudo cargar lo pedido", RuntimeError("SECRETO: ruta interna /var/x y token ghp_123"))


def test_load_errors_show_a_friendly_message_without_technical_details():
    at = AppTest.from_function(_error_script, default_timeout=30).run()
    text = " ".join(e.value for e in at.error)
    assert "No se pudo cargar lo pedido" in text and "Actualizar" in text
    assert "SECRETO" not in text and "ghp_" not in text


# --- estado de la base ----------------------------------------------------------------------------------

def test_health_reports_size_rows_and_write_speed(lab):
    storage, _ = lab
    storage.save_item({"name": "Otro", "item_type": "standalone", "quantity": 1}, "LAB-H-01", is_new=True)
    health = storage.get_health()
    assert health["workbook_mb"] > 0 and health["rows"]["items"] == 2 and health["rows"]["users"] == 5
    assert health["cells"] > 0 and health["write"]["count"] >= 1 and health["write"]["last_ms"] >= 0
    assert health["sync"]["mode"] in ("sync", "deferred")


def test_data_tab_shows_the_health_panel_and_warns_when_the_base_is_slow(lab, monkeypatch):
    storage, people = lab

    def script():
        import streamlit as st

        from core.storage import LabStorage
        from views import usuarios

        st.session_state.storage = LabStorage()
        st.session_state.user = st.session_state.test_user
        usuarios.render()

    def open_page():
        at = AppTest.from_function(script, default_timeout=60)
        at.session_state["test_user"] = {k: people["maestro"][k] for k in ("id", "full_name", "role", "institutional_email")}
        return at.run()

    text = " ".join(m.value for m in open_page().markdown)
    assert "Estado de la base" in text and "Tamaño del Excel" in text
    assert not [w for w in open_page().warning if "lenta" in w.value]
    monkeypatch.setattr(sm, "_write_times_ms", [5000] * 10)
    assert [w for w in open_page().warning if "lenta" in w.value]


def test_the_storage_benchmark_script_runs(capsys, monkeypatch):
    import importlib.util
    from pathlib import Path

    # El script cambia la ruta del libro y apaga GitHub: se restauran al terminar la prueba.
    monkeypatch.setattr(sm, "EXCEL_PATH", sm.EXCEL_PATH)
    monkeypatch.setattr(sm, "_is_github_configured", sm._is_github_configured)

    path = Path(__file__).resolve().parent.parent / "scripts" / "bench_storage.py"
    spec = importlib.util.spec_from_file_location("bench_storage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--items", "30"]) == 0
    out = capsys.readouterr().out
    assert "guardar un producto" in out and "archivo:" in out
