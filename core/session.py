# -*- coding: utf-8 -*-
"""Persistencia segura de sesión entre recargas de Streamlit.

El token solo contiene identificadores y tiempos. La cookie escrita desde
JavaScript no puede ser HttpOnly; se mitiga con expiración corta, SameSite=Lax,
Secure bajo HTTPS y revocación por ``session_epoch``.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timezone

from core.config import safe_secret

COOKIE_NAME = "uniminuto_lab_session"
DEFAULT_IDLE_MINUTES = 30
DEFAULT_MAX_HOURS = 12
ROTATE_EVERY_SECONDS = 60
_REVOKED_LIMIT = 10000
_revoked_lock = threading.Lock()
_revoked: dict = {}          # sid -> None; conserva el orden de inserción (los más antiguos salen primero)


def _now(now=None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def needs_rotation(claims: dict, now=None) -> bool:
    """True si conviene renovar la marca de actividad del token (máx. 1/min)."""
    try:
        last = int((claims or {}).get("last") or 0)
    except (TypeError, ValueError):
        return True
    return int(_now(now).timestamp()) - last >= ROTATE_EVERY_SECONDS


def _integer(name: str, default: int, allow_zero: bool = False) -> int:
    try:
        value = int(safe_secret(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 or (allow_zero and value == 0) else default


def idle_minutes() -> int:
    return _integer("SESSION_IDLE_MINUTES", DEFAULT_IDLE_MINUTES)


def max_hours() -> int:
    return _integer("SESSION_MAX_HOURS", DEFAULT_MAX_HOURS)


def tab_close_grace_seconds() -> int:
    return _integer("SESSION_TAB_CLOSE_GRACE_SECONDS", 0, allow_zero=True)


# Latido opcional (S3-c). Vive en memoria del proceso: no escribe en el Excel
# ni en GitHub. Solo se consulta al RESTAURAR desde la cookie (pestaña nueva o
# F5); si la última señal es más vieja que la gracia, se asume pestaña cerrada.
_heartbeat_lock = threading.Lock()
_heartbeats: dict = {}


def beat(claims: dict, now=None) -> None:
    sid = str((claims or {}).get("sid") or "")
    if not sid:
        return
    with _heartbeat_lock:
        if len(_heartbeats) >= _REVOKED_LIMIT:
            _heartbeats.clear()
        _heartbeats[sid] = int(_now(now).timestamp())


def tab_closed(claims: dict, now=None) -> bool:
    grace = tab_close_grace_seconds()
    sid = str((claims or {}).get("sid") or "")
    if not grace or not sid:
        return False
    with _heartbeat_lock:
        last = _heartbeats.get(sid)
    # Sin registro (p. ej. el servidor se reinició) no se cierra por error.
    return last is not None and int(_now(now).timestamp()) - last > grace


def heartbeat_every():
    """Intervalo para `st.fragment(run_every=...)`; None si está apagado."""
    grace = tab_close_grace_seconds()
    return f"{max(2, grace // 3)}s" if grace else None


def session_secret() -> str:
    value = str(safe_secret("SESSION_SECRET", "") or "").strip()
    nfc_value = str(safe_secret("NFC_SECRET", "") or "").strip()
    return "" if not value or (nfc_value and hmac.compare_digest(value, nfc_value)) else value


def enabled() -> bool:
    return bool(session_secret())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def issue_token(user: dict, now=None, claims: dict = None) -> tuple:
    """Devuelve (token, claims). Sin SESSION_SECRET devuelve ("", {})."""
    secret = session_secret()
    if not secret or not user:
        return "", {}
    moment = _now(now)
    previous = claims or {}
    issued = int(previous.get("iat") or moment.timestamp())
    payload = {
        "uid": str(user.get("id") or ""),
        "epoch": int(user.get("session_epoch") or 0),
        "iat": issued,
        "last": int(moment.timestamp()),
        "exp": issued + max_hours() * 3600,
        "sid": str(previous.get("sid") or secrets.token_urlsafe(18)),
    }
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(body, secret)}", payload


def decode_token(token: str) -> dict:
    secret = session_secret()
    if not secret or not token or token.count(".") != 1:
        raise ValueError("Sesión no disponible.")
    body, supplied = token.split(".", 1)
    expected = _sign(body, secret)
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("Sesión inválida.")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Sesión inválida.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Sesión inválida.")
    return payload


def revoke(token_or_claims, storage=None) -> None:
    """Cierra una sesión: la recuerda en memoria y, con ``storage``, también en la base
    hasta el vencimiento del token, así un reinicio de la app no la revive.

    Límite que se mantiene (documentado en el README): la cookie la escribe JavaScript,
    no puede ser HttpOnly; quien la haya copiado antes del cierre queda bloqueado por
    este registro, pero no por el navegador."""
    try:
        claims = decode_token(token_or_claims) if isinstance(token_or_claims, str) else token_or_claims
        sid = str((claims or {}).get("sid") or "")
    except ValueError:
        sid = ""
    if not sid:
        return
    with _revoked_lock:
        if len(_revoked) >= _REVOKED_LIMIT:
            # Antes se vaciaba todo y las sesiones cerradas volvían a ser válidas hasta su vencimiento;
            # ahora solo salen las más antiguas (la base conserva el resto, ver add_revoked_session).
            for old in list(_revoked)[: _REVOKED_LIMIT // 10]:
                del _revoked[old]
        _revoked[sid] = None
    if storage is not None:
        try:
            storage.add_revoked_session(sid, int((claims or {}).get("exp") or 0))
        except Exception:  # un fallo de E/S no debe impedir cerrar la sesión en memoria
            pass


def verify_token(token: str, storage, now=None) -> tuple:
    """Devuelve (usuario, claims, motivo). Siempre revalida estado y epoch."""
    if not session_secret():
        return None, None, "La persistencia de sesión no está configurada."
    try:
        claims = decode_token(token)
    except ValueError as exc:
        return None, None, str(exc)
    moment = int(_now(now).timestamp())
    with _revoked_lock:
        closed = claims.get("sid") in _revoked
    if not closed:
        try:
            closed = str(claims.get("sid") or "") in storage.get_revoked_sessions(moment)
        except Exception:  # sin registro legible, valen las demás comprobaciones
            closed = False
    if closed:
        return None, None, "La sesión fue cerrada."
    if moment > int(claims.get("exp") or 0):
        return None, None, "Tu sesión alcanzó su duración máxima."
    if moment - int(claims.get("last") or 0) > idle_minutes() * 60:
        return None, None, "Tu sesión se cerró por inactividad."
    user = storage.get_user_by_id(str(claims.get("uid") or ""))
    if not user or user.get("status") != "active":
        return None, None, "Tu cuenta no está disponible."
    if int(user.get("session_epoch") or 0) != int(claims.get("epoch") or 0):
        return None, None, "La sesión fue revocada. Inicia sesión de nuevo."
    from core.auth import public_user
    return public_user(user), claims, None


def cookie_from_context() -> str:
    try:
        import streamlit as st
        return str(st.context.cookies.get(COOKIE_NAME, "") or "")
    except Exception:
        return ""


def queue_cookie(token: str = "", clear: bool = False) -> None:
    """Programa escritura/borrado para el siguiente render de la misma sesión."""
    try:
        import streamlit as st
        st.session_state.session_cookie_command = {"token": token, "clear": bool(clear)}
    except Exception:
        pass


def render_cookie_command() -> None:
    """Ejecuta el puente mínimo de cookie. No transmite el token a terceros."""
    try:
        import streamlit as st
        import streamlit.components.v1 as components
        command = st.session_state.pop("session_cookie_command", None)
    except Exception:
        return
    if not command:
        return
    token = str(command.get("token") or "")
    clear = bool(command.get("clear"))
    name_js = json.dumps(COOKIE_NAME)
    value_js = json.dumps("" if clear else token)
    max_age = "; Max-Age=0" if clear else ""
    html = f"""<script>
const name={name_js}; const value={value_js};
const secure = window.location.protocol === 'https:' ? '; Secure' : '';
window.parent.document.cookie = `${{name}}=${{encodeURIComponent(value)}}; Path=/; SameSite=Lax${{secure}}{max_age}`;
</script>"""
    components.html(html, width=0, height=0)


def audit(storage, event_type: str, user: dict, details: dict = None) -> None:
    """Login/logout/expiración; nunca registra el token ni cada renovación."""
    try:
        storage.add_trace_event({
            "event_type": event_type, "user_id": user.get("id", ""),
            "owner_id": user.get("id", ""), "actor_id": user.get("id", ""),
            "actor_name": user.get("full_name", ""),
            "actor_email": user.get("institutional_email", ""),
            "actor_role": user.get("role", ""),
            "details": json.dumps(details or {}, ensure_ascii=False),
        })
    except Exception:
        # La autenticación nunca debe quedar bloqueada por la auditoría remota.
        pass
