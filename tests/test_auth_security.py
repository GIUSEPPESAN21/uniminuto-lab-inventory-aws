# -*- coding: utf-8 -*-
"""Seguridad de autenticacion: errores genericos, bloqueo por intentos, sesion
revalidada, y verificacion de correo antes de otorgar el rol profesor."""

from datetime import datetime, timedelta, timezone

import pytest

from core import auth

EMAIL = "ana@uniminuto.edu.co"
PASSWORD = "password123"


@pytest.fixture
def account(storage):
    user, error = auth.register_user(storage, "Ana Perez", EMAIL, PASSWORD, "Ing.", "TI001")
    assert error is None
    return user


@pytest.fixture
def clock(monkeypatch):
    """Reloj controlable para el bloqueo por intentos."""
    state = {"now": 1_000_000.0}
    monkeypatch.setattr(auth, "_now", lambda: state["now"])
    return state


# --- inicio de sesion ---------------------------------------------------------

def test_unknown_account_and_wrong_password_give_the_same_error(storage, account):
    _, unknown = auth.login_user(storage, "nadie@uniminuto.edu.co", "lo-que-sea")
    _, wrong = auth.login_user(storage, EMAIL, "clave-equivocada")
    assert unknown == wrong == auth.GENERIC_LOGIN_ERROR  # no revela que correos existen


def test_login_result_never_carries_the_password_hash(storage, account):
    user, error = auth.login_user(storage, EMAIL, PASSWORD)
    assert error is None
    assert "password_hash" not in user
    assert user["institutional_email"] == EMAIL


def test_disabled_account_is_only_revealed_after_the_correct_password(storage, account):
    storage.update_user(account["id"], {"status": "disabled"})
    _, with_wrong_password = auth.login_user(storage, EMAIL, "clave-equivocada")
    assert with_wrong_password == auth.GENERIC_LOGIN_ERROR
    _, with_right_password = auth.login_user(storage, EMAIL, PASSWORD)
    assert "deshabilitada" in with_right_password.lower()


def test_account_is_locked_after_too_many_failures(storage, account, clock):
    for _ in range(auth.DEFAULT_LOGIN_MAX_ATTEMPTS):
        assert auth.login_user(storage, EMAIL, "mal")[1] == auth.GENERIC_LOGIN_ERROR

    user, error = auth.login_user(storage, EMAIL, PASSWORD)   # ni la clave correcta entra
    assert user is None and "demasiados intentos" in error.lower()


def test_lock_expires_and_success_clears_the_counter(storage, account, clock):
    for _ in range(auth.DEFAULT_LOGIN_MAX_ATTEMPTS):
        auth.login_user(storage, EMAIL, "mal")
    clock["now"] += auth.DEFAULT_LOGIN_LOCKOUT_MINUTES * 60 + 1

    user, error = auth.login_user(storage, EMAIL, PASSWORD)
    assert error is None and user is not None
    # contador a cero: se vuelven a tolerar fallos sueltos sin bloquear
    for _ in range(auth.DEFAULT_LOGIN_MAX_ATTEMPTS - 1):
        auth.login_user(storage, EMAIL, "mal")
    assert auth.login_user(storage, EMAIL, PASSWORD)[1] is None


def test_unknown_emails_are_throttled_too(storage, clock):
    for _ in range(auth.DEFAULT_LOGIN_MAX_ATTEMPTS):
        auth.login_user(storage, "fantasma@uniminuto.edu.co", "x")
    _, error = auth.login_user(storage, "fantasma@uniminuto.edu.co", "x")
    assert "demasiados intentos" in error.lower()


def test_old_failures_do_not_accumulate_forever(storage, account, clock):
    for _ in range(auth.DEFAULT_LOGIN_MAX_ATTEMPTS - 1):
        auth.login_user(storage, EMAIL, "mal")
    clock["now"] += auth.DEFAULT_LOGIN_LOCKOUT_MINUTES * 60 + 1   # pasa la ventana
    auth.login_user(storage, EMAIL, "mal")
    assert auth.login_user(storage, EMAIL, PASSWORD)[1] is None


def test_limits_are_configurable_from_secrets(storage, account, clock, monkeypatch):
    values = {"LOGIN_MAX_ATTEMPTS": 2, "LOGIN_LOCKOUT_MINUTES": 1}
    monkeypatch.setattr(auth, "safe_secret", lambda key, default=None: values.get(key, default))
    auth.login_user(storage, EMAIL, "mal")
    auth.login_user(storage, EMAIL, "mal")
    assert "demasiados intentos" in auth.login_user(storage, EMAIL, PASSWORD)[1].lower()


# --- sesion ---------------------------------------------------------------------

def test_session_picks_up_role_changes_immediately(storage, account):
    session_user, _ = auth.login_user(storage, EMAIL, PASSWORD)
    storage.update_user(account["id"], {"role": "profesor"})
    refreshed, problem = auth.validate_session(storage, session_user)
    assert problem is None and refreshed["role"] == "profesor"
    assert "password_hash" not in refreshed


def test_session_ends_when_the_account_is_disabled_or_deleted(storage, account):
    session_user, _ = auth.login_user(storage, EMAIL, PASSWORD)
    storage.update_user(account["id"], {"status": "disabled"})
    user, problem = auth.validate_session(storage, session_user)
    assert user is None and "deshabilitada" in problem.lower()

    user, problem = auth.validate_session(storage, {"id": "no-existe"})
    assert user is None and "no existe" in problem.lower()


def test_session_expires_after_the_configured_time(storage, account):
    session_user, _ = auth.login_user(storage, EMAIL, PASSWORD)
    login_at = datetime.now(timezone.utc)
    later = login_at + timedelta(minutes=auth.DEFAULT_SESSION_TIMEOUT_MINUTES + 1)
    user, problem = auth.validate_session(storage, session_user, login_at, now=later)
    assert user is None and "expiro" in problem.lower()

    soon = login_at + timedelta(minutes=5)
    assert auth.validate_session(storage, session_user, login_at, now=soon)[0] is not None


def test_public_user_strips_only_the_hash():
    cleaned = auth.public_user({"id": "1", "role": "estudiante", "password_hash": "secreto"})
    assert cleaned == {"id": "1", "role": "estudiante"}


# --- rol profesor: la lista blanca exige verificar el correo --------------------

def test_verified_flag_does_not_promote_a_non_whitelisted_email(storage):
    user, _ = auth.register_user(
        storage, "Ana Perez", EMAIL, PASSWORD, "Ing.", "TI001", email_verified=True
    )
    assert user["role"] == "estudiante"


def test_needs_verification_only_for_whitelisted_emails(storage):
    storage.add_to_whitelist("prof@uniminuto.edu.co")
    assert auth.needs_professor_verification(storage, "Prof@Uniminuto.edu.co")
    assert not auth.needs_professor_verification(storage, EMAIL)


def test_create_account_rejects_an_email_registered_in_the_meantime(storage):
    prepared, error = auth.validate_registration(storage, "Ana Perez", EMAIL, PASSWORD, "Ing.", "TI001")
    assert error is None
    auth.create_account(storage, prepared)
    user, error = auth.create_account(storage, prepared)
    assert user is None and "ya existe" in error.lower()


def test_prepared_registration_holds_a_hash_not_the_password(storage):
    prepared, _ = auth.validate_registration(storage, "Ana Perez", EMAIL, PASSWORD, "Ing.", "TI001")
    assert PASSWORD not in prepared.values()
    assert auth.verify_password(PASSWORD, prepared["password_hash"])


# --- codigo de verificacion ------------------------------------------------------

def test_correct_code_verifies_and_consumes_the_challenge():
    code, challenge = auth.new_verification_challenge("prof@uniminuto.edu.co", now=100.0)
    assert len(code) == 6 and code.isdigit()
    ok, _, remaining = auth.check_verification_code(challenge, code, now=101.0)
    assert ok and remaining is None


def test_the_code_is_never_stored_in_clear():
    code, challenge = auth.new_verification_challenge("prof@uniminuto.edu.co")
    assert code not in str(challenge)


def test_wrong_code_decrements_attempts_then_burns_the_challenge():
    code, challenge = auth.new_verification_challenge("prof@uniminuto.edu.co", now=100.0)
    wrong = "000000" if code != "000000" else "111111"
    for expected_left in range(auth.VERIFICATION_MAX_ATTEMPTS - 1, 0, -1):
        ok, message, challenge = auth.check_verification_code(challenge, wrong, now=101.0)
        assert not ok and challenge["attempts_left"] == expected_left
        assert str(expected_left) in message
    ok, message, challenge = auth.check_verification_code(challenge, wrong, now=101.0)
    assert not ok and challenge is None and "nuevo" in message.lower()


def test_expired_code_is_rejected_even_if_correct():
    code, challenge = auth.new_verification_challenge("prof@uniminuto.edu.co", now=100.0)
    late = 100.0 + auth.VERIFICATION_TTL_SECONDS + 1
    ok, message, remaining = auth.check_verification_code(challenge, code, now=late)
    assert not ok and remaining is None and "vencio" in message.lower()


def test_check_without_a_challenge_fails_safely():
    ok, _, remaining = auth.check_verification_code(None, "123456")
    assert not ok and remaining is None


def test_verification_email_goes_only_to_the_requested_address(monkeypatch):
    sent = {}

    def fake_send(subject, body, recipients):
        sent.update(subject=subject, body=body, recipients=recipients)
        return True, "ok"

    monkeypatch.setattr(auth.notifications, "send_email_notification", fake_send)
    ok, _ = auth.send_verification_email("prof@uniminuto.edu.co", "123456")
    assert ok and sent["recipients"] == ["prof@uniminuto.edu.co"]
    assert "123456" in sent["body"]
