# -*- coding: utf-8 -*-
"""Punto 8 de v1.15: actualización compartida (core/live_data.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from core import live_data


@pytest.fixture
def secrets(monkeypatch):
    values = {}
    monkeypatch.setattr(live_data, "safe_secret", lambda key, default=None: values.get(key, default))
    return values


def test_defaults_and_overrides(secrets):
    assert (live_data.data_poll_seconds(), live_data.dispatch_refresh_seconds(), live_data.list_refresh_seconds()) \
        == (60, 5, 15)
    secrets.update(DATA_POLL_SECONDS="30", DISPATCH_REFRESH_SECONDS=2, LIST_REFRESH_SECONDS="0")
    assert (live_data.data_poll_seconds(), live_data.dispatch_refresh_seconds(), live_data.list_refresh_seconds()) \
        == (30, 2, 0)


def test_invalid_or_negative_values_do_not_break_the_page(secrets):
    secrets["DATA_POLL_SECONDS"] = "rápido"
    assert live_data.data_poll_seconds() == 60                            # texto: vuelve al valor por omisión
    secrets["DATA_POLL_SECONDS"] = -5
    assert live_data.data_poll_seconds() == 0                             # negativo: se apaga, no lanza


def test_run_every_formats_for_st_fragment():
    assert live_data.run_every(5) == "5s" and live_data.run_every(0) is None and live_data.run_every(None) is None


def test_poll_delegates_to_the_storage_and_can_be_turned_off(secrets):
    calls = []

    class Storage:
        def poll_remote(self, interval):
            calls.append(interval)
            return True

    secrets["DATA_POLL_SECONDS"] = 45
    assert live_data.poll(Storage()) is True and calls == [45]
    secrets["DATA_POLL_SECONDS"] = 0
    assert live_data.poll(Storage()) is False and calls == [45]           # apagado: ni siquiera consulta


def test_freshness_messages():
    class Storage:
        def __init__(self, read_at):
            self.read_at = read_at

        def get_data_state(self):
            return {"last_read_at": self.read_at}

    now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert live_data.freshness(Storage(None), now) == "Datos aún no leídos"
    assert live_data.freshness(Storage(now - timedelta(seconds=7)), now) == "Actualizado hace 7 s"
    assert live_data.freshness(Storage((now - timedelta(seconds=9)).replace(tzinfo=None)), now) == "Actualizado hace 9 s"
    assert live_data.freshness(Storage((now - timedelta(seconds=11)).isoformat()), now) == "Actualizado hace 11 s"
    assert live_data.freshness(Storage("no-es-fecha"), now) == "Última lectura desconocida"
    assert live_data.freshness(Storage(now + timedelta(seconds=30)), now) == "Actualizado hace 0 s"   # reloj adelantado


def test_stuck_minutes_has_a_floor_of_one(secrets):
    assert live_data.stuck_minutes() == 30
    secrets["STUCK_MINUTES"] = 0
    assert live_data.stuck_minutes() == 1
    secrets["STUCK_MINUTES"] = "45"
    assert live_data.stuck_minutes() == 45
