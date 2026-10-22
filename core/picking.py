# -*- coding: utf-8 -*-
"""Estado de un picking en curso, en la memoria del PROCESO (no de la sesión).

Un toque de chip NFC abre la app en una pestaña nueva, y cada pestaña es una
sesión de Streamlit distinta: lo que vive en ``st.session_state`` se pierde. Por
eso las lecturas se guardan aquí, por ``(user_id, request_id)``, y cualquier
pestaña de la misma persona las ve y las suma.

- Las lecturas válidas esperan hasta «Completar picking» (una sola escritura).
- Antes de las unidades se lee la **ruta** (estantería → piso → contenedor → caja): cada punto
  confirmado queda en ``route`` con su código, método y hora.
- Las lecturas fallidas también se acumulan aquí y se guardan en UN solo evento
  al completar (antes cada error escribía en el Excel y en GitHub).
- Nada de esto se escribe en la base: si el proceso se reinicia, el picking
  vuelve a empezar (el pedido sigue «tomado» y se puede retomar).
- Las entradas vencen a las ``MAX_AGE_SECONDS`` de su última actividad y hay un
  tope de entradas, para no crecer sin límite.
"""

import threading
import time

MAX_AGE_SECONDS = 6 * 3600
MAX_ENTRIES = 500
MAX_FAILED = 200          # más fallos que esto no aportan información nueva

_lock = threading.RLock()
_entries: dict = {}       # (user_id, request_id) -> {"readings", "failed", "route", "touched"}
_active: dict = {}        # user_id -> request_id del picking abierto


def _now(now=None) -> float:
    return time.time() if now is None else float(now)


def _key(user_id, request_id) -> tuple:
    return str(user_id or ""), str(request_id or "")


def _purge(now: float) -> None:
    expired = [key for key, entry in _entries.items() if now - entry["touched"] > MAX_AGE_SECONDS]
    for key in expired:
        _drop(*key)
    if len(_entries) > MAX_ENTRIES:  # conserva las más recientes
        keep = sorted(_entries, key=lambda k: _entries[k]["touched"], reverse=True)[:MAX_ENTRIES]
        for key in [k for k in _entries if k not in keep]:
            _drop(*key)


def _drop(user_id: str, request_id: str) -> None:
    _entries.pop((user_id, request_id), None)
    if _active.get(user_id) == request_id:
        _active.pop(user_id, None)


def _entry(user_id, request_id, now=None, create: bool = True):
    key = _key(user_id, request_id)
    moment = _now(now)
    entry = _entries.get(key)
    if entry is None:
        if not create:
            return None
        entry = _entries[key] = {"readings": [], "failed": [], "route": [], "touched": moment}
        _purge(moment)  # tras insertar: el tope incluye la entrada nueva
    entry["touched"] = moment
    return entry


def reset() -> None:
    """Vacía todo (pruebas)."""
    with _lock:
        _entries.clear()
        _active.clear()


def begin(user_id, request_id, now=None) -> None:
    """Marca ``request_id`` como el picking abierto de ``user_id``."""
    with _lock:
        _entry(user_id, request_id, now)
        _active[str(user_id or "")] = str(request_id or "")


def active_request(user_id, now=None) -> str:
    """Pedido cuyo picking tiene abierto ``user_id`` ('' si ninguno o si venció)."""
    with _lock:
        user = str(user_id or "")
        request_id = _active.get(user, "")
        entry = _entries.get(_key(user, request_id)) if request_id else None
        if entry is None or _now(now) - entry["touched"] > MAX_AGE_SECONDS:
            _active.pop(user, None)
            return ""
        return request_id


def readings(user_id, request_id) -> list:
    with _lock:
        entry = _entries.get(_key(user_id, request_id))
        return [dict(row) for row in entry["readings"]] if entry else []


def failed(user_id, request_id) -> list:
    with _lock:
        entry = _entries.get(_key(user_id, request_id))
        return [dict(row) for row in entry["failed"]] if entry else []


def add_reading(user_id, request_id, code: str, method: str, at: str, now=None) -> int:
    """Agrega una lectura válida y devuelve cuántas hay."""
    with _lock:
        entry = _entry(user_id, request_id, now)
        entry["readings"].append({"code": code, "method": method, "at": at})
        return len(entry["readings"])


def add_reading_once(user_id, request_id, code: str, method: str, at: str, now=None) -> bool:
    """Guarda la lectura de la etiqueta de la caja o del producto SOLO si todavía no hay una (atómico entre
    pestañas). Devuelve si se guardó."""
    with _lock:
        entry = _entry(user_id, request_id, now)
        if entry["readings"]:
            return False
        entry["readings"].append({"code": code, "method": method, "at": at})
        return True


def route_steps(user_id, request_id) -> list:
    """Puntos de la ruta ya confirmados: ``[{"key", "code", "method", "at"}]`` en el orden en que se leyeron."""
    with _lock:
        entry = _entries.get(_key(user_id, request_id))
        return [dict(row) for row in entry.get("route", [])] if entry else []


def add_route_step(user_id, request_id, key: str, code: str, method: str, at: str, now=None) -> int:
    """Confirma el siguiente punto de la ruta y devuelve cuántos van."""
    with _lock:
        entry = _entry(user_id, request_id, now)
        entry.setdefault("route", []).append({"key": key, "code": code, "method": method, "at": at})
        return len(entry["route"])


def undo_last(user_id, request_id, now=None) -> int:
    """Quita la última lectura de unidad; si no hay, el último punto de la ruta."""
    with _lock:
        entry = _entry(user_id, request_id, now, create=False)
        if entry and entry["readings"]:
            entry["readings"].pop()
        elif entry and entry.get("route"):
            entry["route"].pop()
        if entry:
            entry["touched"] = _now(now)
        return len(entry["readings"]) if entry else 0


def add_failed(user_id, request_id, code: str, reason: str, method: str, at: str, now=None) -> int:
    """Acumula una lectura rechazada (se guarda al completar) y devuelve cuántas hay."""
    with _lock:
        entry = _entry(user_id, request_id, now)
        if len(entry["failed"]) < MAX_FAILED:
            entry["failed"].append({"code": code, "reason": reason, "method": method, "at": at})
        return len(entry["failed"])


def clear(user_id, request_id) -> None:
    """Borra lecturas y fallos de ese pedido (picking completado, liberado o cancelado)."""
    with _lock:
        _drop(*_key(user_id, request_id))


def size() -> int:
    with _lock:
        return len(_entries)
