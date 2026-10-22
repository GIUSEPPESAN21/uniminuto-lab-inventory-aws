# -*- coding: utf-8 -*-
"""Actualización compartida de datos sin reiniciar autenticación ni formularios."""

from datetime import datetime, timezone

from core.config import safe_secret

DEFAULT_DATA_POLL_SECONDS = 60
DEFAULT_DISPATCH_REFRESH_SECONDS = 5
DEFAULT_LIST_REFRESH_SECONDS = 15


def seconds(name: str, default: int) -> int:
    try:
        value = int(safe_secret(name, default))
    except (TypeError, ValueError):
        return default
    return max(0, value)


def data_poll_seconds() -> int:
    return seconds("DATA_POLL_SECONDS", DEFAULT_DATA_POLL_SECONDS)


def dispatch_refresh_seconds() -> int:
    return seconds("DISPATCH_REFRESH_SECONDS", DEFAULT_DISPATCH_REFRESH_SECONDS)


def list_refresh_seconds() -> int:
    return seconds("LIST_REFRESH_SECONDS", DEFAULT_LIST_REFRESH_SECONDS)


def run_every(value: int):
    """Formato aceptado por st.fragment; None desactiva el temporizador."""
    return f"{value}s" if value and value > 0 else None


def poll(storage) -> bool:
    interval = data_poll_seconds()
    return storage.poll_remote(interval) if interval else False


def freshness(storage, now=None) -> str:
    state = storage.get_data_state()
    read_at = state.get("last_read_at")
    if not read_at:
        return "Datos aún no leídos"
    if isinstance(read_at, str):
        try:
            read_at = datetime.fromisoformat(read_at)
        except ValueError:
            return "Última lectura desconocida"
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if read_at.tzinfo is None:
        read_at = read_at.replace(tzinfo=timezone.utc)
    elapsed = max(0, int((current - read_at).total_seconds()))
    return f"Actualizado hace {elapsed} s"



def stuck_minutes() -> int:
    return max(1, seconds("STUCK_MINUTES", 30))
