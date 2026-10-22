# -*- coding: utf-8 -*-
"""Punto 8 de v1.15: sesión persistente (core/session.py) con la base real (LabStorage):
token manipulado o vencido, época, cuenta deshabilitada, cierre persistente y sin SESSION_SECRET."""

import base64
import json

import pytest

from core import session
from core.storage import LabStorage

SECRET = "una-frase-larga-y-distinta-de-nfc"
NOW = 1_800_000_000


@pytest.fixture
def secrets(monkeypatch):
    values = {"SESSION_SECRET": SECRET}
    monkeypatch.setattr(session, "safe_secret", lambda key, default=None: values.get(key, default))
    return values


@pytest.fixture
def lab():
    storage = LabStorage()
    user = storage.create_user("Ana Prueba", "ana@uniminuto.edu.co", "hash-secreto", "estudiante")
    return storage, user


def _issue(user, now=NOW, claims=None):
    token, payload = session.issue_token(user, now=_dt(now), claims=claims)
    assert token and payload
    return token, payload


def _dt(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _forge(token, **changes):
    """Mismo token con el cuerpo alterado pero la firma original (lo que haría un atacante)."""
    body, signature = token.split(".")
    padded = body + "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    payload.update(changes)
    new_body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{new_body}.{signature}"


def test_without_session_secret_nothing_is_issued_or_accepted(lab, monkeypatch):
    storage, user = lab
    monkeypatch.setattr(session, "safe_secret", lambda key, default=None: default)
    assert session.enabled() is False
    assert session.issue_token(user) == ("", {})
    restored, claims, reason = session.verify_token("cualquier.cosa", storage)
    assert restored is None and claims is None and "no está configurada" in reason
    with pytest.raises(ValueError):
        session.decode_token("a.b")


def test_the_secret_must_differ_from_the_nfc_secret(secrets):
    secrets["NFC_SECRET"] = SECRET
    assert session.session_secret() == "" and session.enabled() is False
    secrets["NFC_SECRET"] = "otra-frase"
    assert session.enabled() is True


def test_a_valid_token_restores_the_user_without_the_password_hash(secrets, lab):
    storage, user = lab
    token, claims = _issue(user)
    restored, restored_claims, reason = session.verify_token(token, storage, now=_dt(NOW + 5))
    assert reason is None and restored["id"] == user["id"] and restored_claims["sid"] == claims["sid"]
    assert "password_hash" not in restored


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),        # firma alterada
    lambda t: ("A" if t[0] != "A" else "B") + t[1:],                    # cuerpo alterado
    lambda t: t.replace(".", ""),                                       # sin separador
    lambda t: t + ".extra",                                             # separador de más
    lambda t: "",
])
def test_tampered_tokens_are_rejected(secrets, lab, mutate):
    storage, user = lab
    token, _ = _issue(user)
    restored, claims, reason = session.verify_token(mutate(token), storage, now=_dt(NOW + 5))
    assert restored is None and claims is None and reason


def test_a_forged_payload_with_the_original_signature_is_rejected(secrets, lab):
    storage, user = lab
    admin = storage.create_user("Admin", "admin@uniminuto.edu.co", "x", "maestro")
    token, _ = _issue(user)
    forged = _forge(token, uid=admin["id"])                              # intenta entrar como otra persona
    assert session.verify_token(forged, storage, now=_dt(NOW + 5))[0] is None
    longer = _forge(token, exp=NOW + 10 ** 9)                            # o alargar su vencimiento
    assert session.verify_token(longer, storage, now=_dt(NOW + 5))[0] is None


def test_a_token_signed_with_another_secret_is_rejected(secrets, lab):
    storage, user = lab
    secrets["SESSION_SECRET"] = "otro-secreto-distinto"
    token, _ = _issue(user)
    secrets["SESSION_SECRET"] = SECRET
    assert session.verify_token(token, storage, now=_dt(NOW + 5))[0] is None


def test_expiry_and_inactivity(secrets, lab):
    storage, user = lab
    token, claims = _issue(user)
    maximum = session.max_hours() * 3600
    assert "duración máxima" in session.verify_token(token, storage, now=_dt(claims["iat"] + maximum + 1))[2]
    idle = session.idle_minutes() * 60
    assert "inactividad" in session.verify_token(token, storage, now=_dt(NOW + idle + 1))[2]
    assert session.verify_token(token, storage, now=_dt(NOW + idle - 1))[2] is None


def test_sliding_renewal_keeps_the_session_and_never_extends_the_absolute_limit(secrets, lab):
    storage, user = lab
    token, claims = _issue(user)
    later = NOW + 600
    assert session.needs_rotation(claims, _dt(later)) and not session.needs_rotation(claims, _dt(NOW + 10))
    renewed, new_claims = session.issue_token(user, now=_dt(later), claims=claims)
    assert new_claims["sid"] == claims["sid"] and new_claims["iat"] == claims["iat"]
    assert new_claims["exp"] == claims["exp"] and new_claims["last"] == later
    assert session.verify_token(renewed, storage, now=_dt(later))[2] is None
    assert session.needs_rotation({"last": "no-es-numero"}) is True


def test_a_new_epoch_revokes_open_tokens(secrets, lab):
    storage, user = lab
    token, _ = _issue(user)
    storage.revoke_user_sessions(user["id"], {"id": user["id"], "role": "estudiante"})
    assert "revocada" in session.verify_token(token, storage, now=_dt(NOW + 5))[2]


def test_disabled_or_deleted_accounts_cannot_restore(secrets, lab):
    storage, user = lab
    admin = storage.create_user("Admin", "admin@uniminuto.edu.co", "x", "maestro")
    token, _ = _issue(user)
    storage.update_user(user["id"], {"status": "disabled"}, actor=admin)
    assert "no está disponible" in session.verify_token(token, storage, now=_dt(NOW + 5))[2]
    ghost = {"id": "no-existe", "session_epoch": 0}
    token, _ = _issue(ghost)
    assert "no está disponible" in session.verify_token(token, storage, now=_dt(NOW + 5))[2]


def test_logout_survives_a_restart_until_the_token_would_have_expired(secrets, lab):
    storage, user = lab
    token, claims = _issue(user, now=NOW)
    session.revoke(token, storage)
    assert "cerrada" in session.verify_token(token, storage, now=_dt(NOW + 5))[2]
    session._revoked.clear()                                              # la app se reinició: memoria vacía
    assert "cerrada" in session.verify_token(token, storage, now=_dt(NOW + 5))[2]
    assert claims["sid"] in storage.get_revoked_sessions(now=NOW)
    assert claims["sid"] not in storage.get_revoked_sessions(now=claims["exp"] + 1)   # ya no hace falta recordarla


def test_revoking_without_storage_only_remembers_in_memory(secrets, lab):
    storage, user = lab
    token, claims = _issue(user)
    session.revoke(token)
    assert storage.get_revoked_sessions(now=NOW) == {}
    session._revoked.clear()
    assert session.verify_token(token, storage, now=_dt(NOW + 5))[2] is None   # límite documentado: sin base no sobrevive


def test_revoked_registry_is_bounded_and_pruned(lab, monkeypatch):
    storage, user = lab
    monkeypatch.setattr(LabStorage, "REVOKED_SESSIONS_LIMIT", 5)
    for number in range(8):
        storage.add_revoked_session(f"sid-{number}", NOW + 1000 + number, now=NOW)
    storage.add_revoked_session("vencida", NOW - 1, now=NOW)
    kept = storage.get_revoked_sessions(now=NOW)
    assert len(kept) <= 5 and "vencida" not in kept and "sid-7" in kept and "sid-0" not in kept
    storage.add_revoked_session("", NOW + 5)                              # sin id no se guarda nada
    assert "" not in storage.get_revoked_sessions(now=NOW)


def test_tab_close_heartbeat(secrets):
    secrets["SESSION_TAB_CLOSE_GRACE_SECONDS"] = 30
    claims = {"sid": "abc"}
    assert session.tab_closed(claims, _dt(NOW)) is False                  # sin latido registrado no se cierra por error
    session.beat(claims, _dt(NOW))
    assert session.tab_closed(claims, _dt(NOW + 10)) is False
    assert session.tab_closed(claims, _dt(NOW + 31)) is True
    assert session.heartbeat_every() == "10s"
    secrets["SESSION_TAB_CLOSE_GRACE_SECONDS"] = 0
    assert session.tab_closed(claims, _dt(NOW + 999)) is False and session.heartbeat_every() is None
