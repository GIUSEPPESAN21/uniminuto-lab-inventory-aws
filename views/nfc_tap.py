# -*- coding: utf-8 -*-
"""views/nfc_tap.py - Lo que pasa cuando un chip NFC abre la app.

El chip guarda una URL como <APP_URL>/escanear?nfc=<codigo>&s=<firma> (ver
core/nfc.py). Al tocarlo, el telefono abre esa URL en el navegador:

1. `capture_tap` (app.py, ANTES del login) guarda el toque en la sesion: si hay
   que iniciar sesion, el toque espera y se procesa al entrar.
2. `handle_pending` (app.py, ya con usuario y navegacion) lo registra una sola
   vez, prepara la pagina de destino (Escanear, la ruta en Trazabilidad o el
   conteo en Chips NFC), quita ?nfc= de la URL (una recarga no cuenta doble) y
   navega.
3. `render_tap_banner` muestra el resultado arriba de la pagina."""

import time

import streamlit as st

from core import barcode, dispatch, nfc, permissions

PENDING_KEY = "nfc_pending"          # toque esperando el login o su proceso
HANDLED_KEY = "nfc_handled"          # ultimo toque procesado en esta sesion (codigo|firma y hora)
RESULT_KEY = "nfc_tap_result"        # aviso a mostrar tras procesarlo
COUNT_FOCUS_KEY = "nfc_count_focus"  # producto tocado durante un conteo (lo muestra Chips NFC)

_FEEDBACK = {"success": st.success, "warning": st.warning, "info": st.info, "danger": st.error}


def _clear_query() -> None:
    for key in (nfc.QUERY_CODE, nfc.QUERY_SIGNATURE):
        try:
            st.query_params.pop(key, None)
        except Exception:  # la URL nunca debe romper la pagina
            pass


def _fingerprint(tap: dict) -> str:
    return f"{tap.get('code', '')}|{tap.get('signature', '')}"


def capture_tap() -> None:
    """Guarda en la sesion el toque que trae la URL (?nfc=...), si lo hay."""
    try:
        tap = nfc.parse_tap(st.query_params)
    except Exception:
        tap = None
    if not tap:
        return
    handled = st.session_state.get(HANDLED_KEY) or {}
    if (handled.get("fingerprint") == _fingerprint(tap)
            and time.time() - float(handled.get("at") or 0) < nfc.DEDUP_SECONDS):
        _clear_query()  # el mismo toque que ya se proceso en esta sesion
        return
    st.session_state[PENDING_KEY] = tap


def render_login_hint() -> None:
    """Aviso en la pantalla de acceso: el toque se registra al iniciar sesion."""
    tap = st.session_state.get(PENDING_KEY)
    if not tap:
        return
    if tap.get("error"):
        st.warning(f":material/nfc: {tap['error']}")
    else:
        st.info(f":material/nfc: Tocaste el chip NFC de **{tap['code']}**. Inicia sesión y el toque quedará "
                "registrado.")


def apply_outcome(storage, outcome: dict) -> None:
    """Deja lista la pagina de destino de un toque ya procesado."""
    user = st.session_state.get("user") or {}
    # El picking abierto vive en la memoria del proceso (core.picking), no en esta
    # sesión: el toque llega en una pestaña nueva y aun así suma a ese picking.
    if user.get("id") and permissions.has_cap(user, permissions.CAP_PICK):
        reading = dispatch.register_active_reading(
            storage, user, outcome.get("code", ""), dispatch.METHOD_NFC,
            accepted=bool(outcome.get("ok")), reason=outcome.get("message", ""),
        )
        if reading is not None:
            ok, message = reading
            st.session_state[RESULT_KEY] = {"tone": "success" if ok else "warning", "message": message}
            outcome["target"] = "despacho"
            return
    target = outcome.get("target")
    if target == nfc.TARGET_COUNT:
        st.session_state[COUNT_FOCUS_KEY] = {"code": outcome["code"], "method": "nfc",
                                             "message": outcome.get("message", ""),
                                             "tone": outcome.get("tone", "success")}
        return
    if target == nfc.TARGET_ROUTE:
        st.session_state.trace_focus_request = outcome.get("request_id")
    elif outcome.get("code"):
        st.session_state.scan_result = barcode.scan(storage, outcome["code"])
    st.session_state[RESULT_KEY] = {"tone": outcome.get("tone", "info"), "message": outcome.get("message", "")}


def handle_pending(storage, user: dict, pages: dict, current_page=None) -> None:
    """Procesa el toque pendiente (una sola vez) y navega a su pagina."""
    tap = st.session_state.pop(PENDING_KEY, None)
    if not tap:
        return
    st.session_state[HANDLED_KEY] = {"fingerprint": _fingerprint(tap), "at": time.time()}
    if tap.get("error"):
        outcome = {"tone": "warning", "message": tap["error"], "target": nfc.TARGET_SCAN, "code": ""}
    else:
        try:
            outcome = nfc.handle_tap(storage, user, tap["code"], tap.get("signature", ""))
        except Exception as exc:  # E/S o red: el toque no se pierde en silencio
            outcome = {"tone": "danger", "target": nfc.TARGET_SCAN, "code": tap["code"],
                       "message": f"No se pudo registrar el toque del chip NFC ({exc}). Inténtalo de nuevo."}
    apply_outcome(storage, outcome)
    _clear_query()
    target = pages.get(outcome.get("target"))
    if target is None and outcome.get("target") == nfc.TARGET_ROUTE:
        target = pages.get("solicitudes")        # el cliente recorre su ruta desde Solicitudes → Mis pedidos
    target = target or pages.get(nfc.TARGET_SCAN)
    current_path = getattr(current_page, "url_path", None)
    if target is not None and current_path != target.url_path:
        st.switch_page(target)


def render_tap_banner() -> None:
    """Resultado del ultimo toque, arriba de la pagina (se muestra una vez)."""
    result = st.session_state.pop(RESULT_KEY, None)
    if result and result.get("message"):
        _FEEDBACK.get(result.get("tone"), st.info)(f":material/nfc: {result['message']}")
