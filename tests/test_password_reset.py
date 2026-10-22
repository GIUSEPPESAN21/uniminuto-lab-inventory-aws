# -*- coding: utf-8 -*-
"""Restablecer contraseñas desde el perfil maestro (core/auth.py): permisos,
politica de claves, clave temporal, marca de cambio obligatorio, cierre de
sesiones abiertas y auditoria sin la contraseña."""

import json
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest

from core import auth, notifications

PASSWORD = "ClaveOriginal123"
NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
_REAL_GENSALT = bcrypt.gensalt


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch):
    """bcrypt real con el costo minimo: las pruebas hashean muchas claves."""
    monkeypatch.setattr(bcrypt, "gensalt", lambda *args, **kwargs: _REAL_GENSALT(rounds=4))


def _make(storage, email, role="estudiante", name="Usuario Prueba", status="active"):
    return storage.add_user(email, full_name=name, role=role, status=status,
                            password_hash=auth.hash_password(PASSWORD))


@pytest.fixture
def lab(storage):
    return {
        "master": _make(storage, "maestro@uniminuto.edu.co", "maestro", "Ana Maestra"),
        "other_master": _make(storage, "otro.maestro@uniminuto.edu.co", "maestro", "Otro Maestro"),
        "professor": _make(storage, "profe@uniminuto.edu.co", "profesor", "Carlos Profe"),
        "student": _make(storage, "laura@uniminuto.edu.co", "estudiante", "Laura Gomez"),
    }


def _hash(storage, user):
    return storage.get_user_by_id(user["id"])["password_hash"]


# --- clave temporal y politica ---------------------------------------------------

def test_temporary_password_is_readable_and_strong():
    seen = set()
    for _ in range(50):
        password = auth.generate_temporary_password()
        groups = password.split("-")
        assert len(groups) == auth.TEMP_PASSWORD_GROUPS
        assert all(len(group) == auth.TEMP_PASSWORD_GROUP_SIZE for group in groups)
        chars = "".join(groups)
        assert set(chars) <= set(auth.TEMP_PASSWORD_ALPHABET)
        assert not set(chars) & set("0Oo1lI")                 # nada ambiguo al dictarla
        assert any(c.isupper() for c in chars) and any(c.islower() for c in chars)
        assert any(c.isdigit() for c in chars)
        assert auth.validate_new_password(password) is None   # cumple la regla del registro
        seen.add(password)
    assert len(seen) == 50                                     # aleatoria


def test_ambiguous_characters_are_not_in_the_alphabet():
    assert not set(auth.TEMP_PASSWORD_ALPHABET) & set("0Oo1lI")


@pytest.mark.parametrize("password,confirmation,expected", [
    ("", None, "al menos 8"),
    ("corta", None, "al menos 8"),
    ("1234567", "1234567", "al menos 8"),
    ("ClaveLarga123", "ClaveLarga124", "no coinciden"),
])
def test_new_password_follows_the_registration_rule(password, confirmation, expected):
    assert expected in auth.validate_new_password(password, confirmation)


def test_valid_new_password_passes():
    assert auth.validate_new_password("ClaveLarga123") is None
    assert auth.validate_new_password("ClaveLarga123", "ClaveLarga123") is None


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("True", True), (True, True), ("si", True),
    ("", False), (None, False), ("0", False), (False, False), ("nan", False), (float("nan"), False),
])
def test_must_change_password_reads_excel_values(value, expected):
    assert auth.must_change_password({"must_change_password": value}) is expected


def test_must_change_password_is_false_for_old_accounts_without_the_column():
    assert auth.must_change_password({"id": "1", "role": "estudiante"}) is False
    assert auth.must_change_password(None) is False


# --- permisos --------------------------------------------------------------------

@pytest.mark.parametrize("actor_key", ["student", "professor"])
def test_only_a_master_can_reset(storage, lab, actor_key):
    before = _hash(storage, lab["student"])
    result, error = auth.admin_reset_password(storage, lab[actor_key], lab["student"]["id"])
    assert result is None and "administrador" in error.lower()
    assert _hash(storage, lab["student"]) == before
    assert storage.trace_events == []


def test_a_disabled_or_unknown_master_cannot_reset(storage, lab):
    storage.update_user(lab["master"]["id"], {"status": "disabled"})
    assert auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])[0] is None
    assert auth.admin_reset_password(storage, {"id": "fantasma", "role": "maestro"},
                                     lab["student"]["id"])[0] is None
    assert auth.admin_reset_password(storage, None, lab["student"]["id"])[0] is None


def test_the_role_is_rechecked_against_the_database(storage, lab):
    # Una sesion vieja que aun dice "maestro" no basta: manda la base.
    stale_session = auth.public_user(lab["master"])
    storage.update_user(lab["master"]["id"], {"role": "profesor"})
    result, error = auth.admin_reset_password(storage, stale_session, lab["student"]["id"])
    assert result is None and "administrador" in error.lower()


def test_unknown_target_is_rejected(storage, lab):
    result, error = auth.admin_reset_password(storage, lab["master"], "no-existe")
    assert result is None and "no existe" in error.lower()


def test_master_cannot_reset_own_password_here(storage, lab):
    result, error = auth.admin_reset_password(storage, lab["master"], lab["master"]["id"])
    assert result is None and "mi perfil" in error.lower()


def test_master_cannot_reset_another_master(storage, lab):
    before = _hash(storage, lab["other_master"])
    result, error = auth.admin_reset_password(storage, lab["master"], lab["other_master"]["id"])
    assert result is None and "otro administrador" in error.lower()
    assert _hash(storage, lab["other_master"]) == before


def test_can_reset_password_matches_the_policy(lab):
    master = lab["master"]
    assert auth.can_reset_password(master, lab["student"])
    assert auth.can_reset_password(master, lab["professor"])
    assert not auth.can_reset_password(master, master)
    assert not auth.can_reset_password(master, lab["other_master"])
    assert not auth.can_reset_password(lab["professor"], lab["student"])
    assert not auth.can_reset_password(master, None)


# --- restablecer -----------------------------------------------------------------

@pytest.mark.parametrize("target_key", ["student", "professor"])
def test_generated_password_replaces_the_old_one(storage, lab, target_key):
    target = lab[target_key]
    result, error = auth.admin_reset_password(storage, lab["master"], target["id"], now=NOW)
    assert error is None
    temporary = result["temporary_password"]
    assert result["generated"] is True and temporary
    assert "password_hash" not in result["user"]

    fresh = storage.get_user_by_id(target["id"])
    assert auth.verify_password(temporary, fresh["password_hash"])
    assert not auth.verify_password(PASSWORD, fresh["password_hash"])
    assert auth.must_change_password(fresh)
    assert fresh["password_changed_at"] == NOW.isoformat()
    assert fresh["role"] == target["role"] and fresh["status"] == "active"   # nada mas cambia

    user, login_error = auth.login_user(storage, target["institutional_email"], temporary)
    assert login_error is None and auth.must_change_password(user)
    assert auth.login_user(storage, target["institutional_email"], PASSWORD)[0] is None


def test_manual_password_is_applied_and_not_returned(storage, lab):
    result, error = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"],
                                              new_password="ClaveManual2026")
    assert error is None
    assert result["generated"] is False and result["temporary_password"] is None
    fresh = storage.get_user_by_id(lab["student"]["id"])
    assert auth.verify_password("ClaveManual2026", fresh["password_hash"])
    assert auth.must_change_password(fresh)


def test_short_manual_password_changes_nothing(storage, lab):
    before = storage.get_user_by_id(lab["student"]["id"])
    result, error = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"],
                                              new_password="corta")
    assert result is None and "al menos 8" in error
    assert storage.get_user_by_id(lab["student"]["id"]) == before
    assert storage.trace_events == []


def test_manual_password_longer_than_72_bytes_works(storage, lab):
    long_password = "contraseña-muy-larga-" * 6           # > 72 bytes, como en el registro
    _, error = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"],
                                         new_password=long_password)
    assert error is None
    user, login_error = auth.login_user(storage, lab["student"]["institutional_email"], long_password)
    assert login_error is None and user


def test_disabled_account_can_be_reset_but_stays_disabled(storage, lab):
    storage.update_user(lab["student"]["id"], {"status": "disabled"})
    result, error = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])
    assert error is None
    _, login_error = auth.login_user(storage, lab["student"]["institutional_email"],
                                     result["temporary_password"])
    assert "deshabilitada" in login_error.lower()


def test_reset_lifts_a_login_lockout(storage, lab):
    email = lab["student"]["institutional_email"]
    for _ in range(auth.DEFAULT_LOGIN_MAX_ATTEMPTS):
        auth.login_user(storage, email, "clave-equivocada")
    assert "demasiados intentos" in auth.login_user(storage, email, PASSWORD)[1].lower()

    result, _ = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])
    user, error = auth.login_user(storage, email, result["temporary_password"])
    assert error is None and user


# --- sesiones abiertas -------------------------------------------------------------

def test_reset_closes_sessions_opened_before_it(storage, lab):
    session_user, _ = auth.login_user(storage, lab["student"]["institutional_email"], PASSWORD)
    login_at = NOW - timedelta(hours=1)
    assert auth.validate_session(storage, session_user, login_at, now=NOW)[0] is not None

    auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], now=NOW)
    user, problem = auth.validate_session(storage, session_user, login_at, now=NOW + timedelta(seconds=1))
    assert user is None and problem == auth.PASSWORD_CHANGED_SESSION_NOTICE


def test_sessions_opened_after_the_reset_stay_open(storage, lab):
    auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], now=NOW)
    session_user = auth.public_user(storage.get_user_by_id(lab["student"]["id"]))
    later = NOW + timedelta(minutes=1)
    user, problem = auth.validate_session(storage, session_user, later, now=later)
    assert problem is None and user["id"] == lab["student"]["id"]


def test_accounts_never_reset_keep_their_sessions(storage, lab):
    session_user = auth.public_user(lab["professor"])
    user, problem = auth.validate_session(storage, session_user, NOW, now=NOW + timedelta(minutes=5))
    assert problem is None and user


def test_naive_or_invalid_timestamps_do_not_break_the_session(storage, lab):
    storage.update_user(lab["student"]["id"], {"password_changed_at": "no-es-fecha"})
    session_user = auth.public_user(lab["student"])
    assert auth.validate_session(storage, session_user, NOW, now=NOW)[0] is not None
    storage.update_user(lab["student"]["id"], {"password_changed_at": "2026-10-10T16:00:00"})  # sin zona = UTC
    later = NOW + timedelta(hours=2)
    assert auth.validate_session(storage, session_user, NOW, now=later)[0] is None
    assert auth.validate_session(storage, session_user, later, now=later)[0] is not None


# --- auditoria y correo --------------------------------------------------------------

def test_reset_is_audited_without_the_password(storage, lab):
    result, _ = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], now=NOW)
    events = storage.get_trace_events(event_type=auth.PASSWORD_RESET_EVENT)
    assert len(events) == 1
    event = events[0]
    assert event["user_id"] == lab["student"]["id"]
    assert event["actor_id"] == lab["master"]["id"]
    assert event["actor_email"] == "maestro@uniminuto.edu.co"
    assert event["request_id"] == "" and event["item_id"] == ""   # no aparece en la trazabilidad
    details = json.loads(event["details"])
    assert details == {"mode": "generated", "target_role": "estudiante",
                       "email_requested": False, "email_sent": False}
    serialized = json.dumps(event)
    fresh_hash = _hash(storage, lab["student"])
    assert result["temporary_password"] not in serialized
    assert fresh_hash not in serialized and "$2b$" not in serialized


def test_manual_reset_audit_has_no_password(storage, lab):
    auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], new_password="ClaveManual2026")
    serialized = json.dumps(storage.trace_events)
    assert "ClaveManual2026" not in serialized
    assert json.loads(storage.trace_events[0]["details"])["mode"] == "manual"


def test_audit_failure_does_not_undo_the_reset(storage, lab, monkeypatch):
    def broken(event):
        raise OSError("disco lleno")

    monkeypatch.setattr(storage, "add_trace_event", broken)
    result, error = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])
    assert error is None
    assert auth.verify_password(result["temporary_password"], _hash(storage, lab["student"]))


@pytest.fixture
def mailbox(monkeypatch):
    sent = []

    def fake_send(subject, body, recipients):
        sent.append({"subject": subject, "body": body, "recipients": recipients})
        return True, "ok"

    monkeypatch.setattr(notifications, "send_email_notification", fake_send)
    return sent


def test_email_is_only_sent_when_requested(storage, lab, mailbox):
    auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])
    assert mailbox == []

    result, _ = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], send_email=True)
    assert result["email_requested"] and result["email_sent"]
    assert mailbox[0]["recipients"] == ["laura@uniminuto.edu.co"]
    assert result["temporary_password"] in mailbox[0]["body"]
    details = json.loads(storage.trace_events[-1]["details"])
    assert details["email_requested"] is True and details["email_sent"] is True
    assert result["temporary_password"] not in json.dumps(storage.trace_events)


def test_email_failure_keeps_the_reset(storage, lab, monkeypatch):
    monkeypatch.setattr(notifications, "send_email_notification", lambda *a: (False, "SMTP caido"))
    result, error = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], send_email=True)
    assert error is None and result["email_sent"] is False
    assert auth.verify_password(result["temporary_password"], _hash(storage, lab["student"]))


def test_email_delivery_needs_smtp_host_and_sender(monkeypatch):
    assert auth.email_delivery_configured() is False            # las pruebas no tienen Secrets
    secrets_map = {"SMTP_HOST": "smtp.uniminuto.edu.co", "SMTP_USERNAME": "lab@uniminuto.edu.co"}
    monkeypatch.setattr(auth, "safe_secret", lambda key, default=None: secrets_map.get(key, default))
    assert auth.email_delivery_configured() is True
    secrets_map.pop("SMTP_HOST")
    assert auth.email_delivery_configured() is False


# --- cambio obligatorio al ingresar ---------------------------------------------------

def test_forced_change_clears_the_flag_and_sets_the_new_password(storage, lab):
    result, _ = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], now=NOW)
    later = NOW + timedelta(minutes=2)
    user, changed_at, error = auth.complete_forced_password_change(
        storage, lab["student"]["id"], "MiClavePropia1", "MiClavePropia1", now=later
    )
    assert error is None and changed_at == later
    assert "password_hash" not in user and not auth.must_change_password(user)
    fresh = storage.get_user_by_id(lab["student"]["id"])
    assert auth.verify_password("MiClavePropia1", fresh["password_hash"])
    assert not auth.verify_password(result["temporary_password"], fresh["password_hash"])
    assert not auth.must_change_password(fresh)


def test_forced_change_keeps_this_session_and_closes_older_ones(storage, lab):
    auth.admin_reset_password(storage, lab["master"], lab["student"]["id"], now=NOW)
    temp_session_start = NOW + timedelta(minutes=1)        # alguien mas entro con la temporal
    changed = NOW + timedelta(minutes=2)
    user, changed_at, _ = auth.complete_forced_password_change(
        storage, lab["student"]["id"], "MiClavePropia1", "MiClavePropia1", now=changed
    )
    after = changed + timedelta(seconds=5)
    assert auth.validate_session(storage, user, changed_at, now=after)[0] is not None
    assert auth.validate_session(storage, user, temp_session_start, now=after)[0] is None


@pytest.mark.parametrize("new,confirmation,expected", [
    ("corta", "corta", "al menos 8"),
    ("MiClavePropia1", "MiClavePropia2", "no coinciden"),
])
def test_forced_change_validates_the_new_password(storage, lab, new, confirmation, expected):
    auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])
    user, _, error = auth.complete_forced_password_change(storage, lab["student"]["id"], new, confirmation)
    assert user is None and expected in error
    assert auth.must_change_password(storage.get_user_by_id(lab["student"]["id"]))


def test_forced_change_rejects_reusing_the_temporary_password(storage, lab):
    result, _ = auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])
    temporary = result["temporary_password"]
    user, _, error = auth.complete_forced_password_change(storage, lab["student"]["id"], temporary, temporary)
    assert user is None and "distinta" in error


def test_forced_change_needs_a_pending_reset(storage, lab):
    user, _, error = auth.complete_forced_password_change(
        storage, lab["student"]["id"], "MiClavePropia1", "MiClavePropia1"
    )
    assert user is None and "pendiente" in error
    assert auth.verify_password(PASSWORD, _hash(storage, lab["student"]))


def test_forced_change_refuses_disabled_or_missing_accounts(storage, lab):
    auth.admin_reset_password(storage, lab["master"], lab["student"]["id"])
    storage.update_user(lab["student"]["id"], {"status": "disabled"})
    assert auth.complete_forced_password_change(storage, lab["student"]["id"], "MiClavePropia1",
                                                "MiClavePropia1")[0] is None
    assert auth.complete_forced_password_change(storage, "no-existe", "MiClavePropia1",
                                                "MiClavePropia1")[0] is None


# --- base real (Excel) ----------------------------------------------------------------

def test_reset_persists_in_the_excel_database():
    from core import storage as storage_module
    from core.storage import LabStorage

    db = LabStorage()
    master = db.create_user("Ana Maestra", "maestro@uniminuto.edu.co", auth.hash_password(PASSWORD), "maestro")
    student = db.create_user("Laura Gomez", "laura@uniminuto.edu.co", auth.hash_password(PASSWORD), "estudiante")
    assert not auth.must_change_password(db.get_user_by_id(student["id"]))

    result, error = auth.admin_reset_password(db, master, student["id"], now=NOW)
    assert error is None
    storage_module._cached_dfs = None                         # releer el Excel del disco
    fresh = db.get_user_by_id(student["id"])
    assert auth.must_change_password(fresh)
    assert fresh["password_changed_at"] == NOW.isoformat()
    assert auth.verify_password(result["temporary_password"], fresh["password_hash"])
    events = db.get_trace_events(event_type=auth.PASSWORD_RESET_EVENT)
    assert len(events) == 1 and events[0]["user_id"] == student["id"]
    assert result["temporary_password"] not in json.dumps(events)


def test_old_database_without_the_new_columns_still_loads(tmp_path):
    import pandas as pd

    from core import storage as storage_module
    from core.storage import SHEET_COLUMNS, LabStorage

    new_columns = ("must_change_password", "password_changed_at")
    with pd.ExcelWriter(storage_module.EXCEL_PATH, engine="openpyxl") as writer:
        for sheet, columns in SHEET_COLUMNS.items():
            rows = []
            if sheet == "users":
                columns = [c for c in columns if c not in new_columns]
                rows = [{"id": "u1", "full_name": "Laura Gomez", "student_id": "TI-1",
                         "institutional_email": "laura@uniminuto.edu.co",
                         "password_hash": auth.hash_password(PASSWORD), "role": "estudiante",
                         "program_or_department": "Sistemas", "status": "active", "created_at": ""}]
            pd.DataFrame(rows, columns=columns).to_excel(writer, sheet_name=sheet, index=False)
    storage_module._cached_dfs = None

    db = LabStorage()
    user = db.get_user_by_id("u1")
    assert user["must_change_password"] == "" and not auth.must_change_password(user)
    session_user, error = auth.login_user(db, "laura@uniminuto.edu.co", PASSWORD)
    assert error is None
    assert auth.validate_session(db, session_user, NOW, now=NOW)[0] is not None
    assert db.update_user("u1", {"must_change_password": "1"})["must_change_password"] == "1"
