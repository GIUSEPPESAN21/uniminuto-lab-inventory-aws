# -*- coding: utf-8 -*-
"""Pruebas de core/storage.firestore_retry.

Bug detectado al integrar la validacion de codigos de core/barcode.py: el
decorador reintentaba errores de validacion (ValueError) como si fueran
fallas transitorias de red, y su `raise` final (fuera de cualquier bloque
`except`) producia un RuntimeError('No active exception to reraise') que
ocultaba el error real. Estas pruebas fijan el comportamiento correcto."""

import time

import pytest

from core import storage
from core.storage import firestore_retry


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(storage.time, "sleep", lambda _seconds: None)


def test_value_error_propagates_immediately_without_retry():
    calls = []

    @firestore_retry
    def always_invalid():
        calls.append(1)
        raise ValueError("codigo invalido")

    start = time.monotonic()
    with pytest.raises(ValueError, match="codigo invalido"):
        always_invalid()
    elapsed = time.monotonic() - start

    assert len(calls) == 1  # nunca deberia reintentar un error de validacion
    assert elapsed < 0.5  # sin los sleep(1)/sleep(2) del retry


def test_transient_error_is_retried_and_original_exception_reraised():
    calls = []

    @firestore_retry
    def always_flaky():
        calls.append(1)
        raise ConnectionError("timeout de red")

    with pytest.raises(ConnectionError, match="timeout de red"):
        always_flaky()

    assert len(calls) == 3  # se reintenta hasta agotar los 3 intentos


def test_succeeds_after_a_transient_failure():
    calls = []

    @firestore_retry
    def fails_once_then_ok():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("timeout de red")
        return "ok"

    assert fails_once_then_ok() == "ok"
    assert len(calls) == 2
