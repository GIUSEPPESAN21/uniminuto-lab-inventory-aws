# -*- coding: utf-8 -*-
"""Flujos completos con la app real (AppTest): sesion revalidada en cada recarga,
registro de profesores con verificacion por correo, permisos por vista y escape
de HTML. Complementan las pruebas unitarias de core/auth.py."""

from datetime import datetime, timedelta, timezone

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth  # noqa: E402

# Ruta absoluta: desde Streamlit 1.65, AppTest.from_file resuelve las rutas
# relativas desde el archivo de prueba (tests/), no desde la raiz del repo.
APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

PASSWORD = "ClaveSegura123"
ADMIN = {"id": "admin-test", "role": "maestro", "institutional_email": "admin@uniminuto.edu.co"}


def _start():
    at = AppTest.from_file(APP_PATH)
    at.run()
    assert not at.exception
    return at, at.session_state["storage"]


def _make_user(storage, email, role="estudiante", name="Usuario Prueba"):
    return storage.create_user(
        full_name=name, email=email, password_hash=auth.hash_password(PASSWORD),
        role=role, program="Ingenieria", student_id="ID-1",
    )


def _login(at, email, password=PASSWORD):
    at.text_input[0].input(email)
    at.text_input[1].input(password)
    at.button[0].click().run()
    assert not at.exception


def _register(at, name, student_id, email, program="Ingenieria", password=PASSWORD):
    fields = at.text_input[2:8]
    for widget, value in zip(fields, [name, student_id, email, program, password, password]):
        widget.input(value)
    at.button[1].click().run()
    assert not at.exception


# --- sesion ----------------------------------------------------------------------

def test_login_stores_a_session_user_without_the_password_hash():
    at, storage = _start()
    _make_user(storage, "ana@uniminuto.edu.co")
    _login(at, "ana@uniminuto.edu.co")
    assert at.session_state["user"]["institutional_email"] == "ana@uniminuto.edu.co"
    assert "password_hash" not in at.session_state["user"]
    assert at.session_state["login_at"] is not None


def test_disabled_account_is_logged_out_on_the_next_interaction():
    at, storage = _start()
    user = _make_user(storage, "ana@uniminuto.edu.co")
    _login(at, "ana@uniminuto.edu.co")
    assert at.session_state["user"] is not None

    storage.update_user(user["id"], {"status": "disabled"}, actor=ADMIN)
    at.run()
    assert not at.exception
    assert at.session_state["user"] is None
    assert any("deshabilitada" in w.value.lower() for w in at.warning)


def test_role_change_by_an_admin_closes_the_session_and_applies_on_the_next_login():
    at, storage = _start()
    user = _make_user(storage, "ana@uniminuto.edu.co")
    _login(at, "ana@uniminuto.edu.co")
    assert "inventario" not in at.session_state["pages"]

    storage.update_user(user["id"], {"role": "profesor"}, actor=ADMIN)
    at.run()
    assert at.session_state["user"] is None
    assert any("revocada" in w.value.lower() for w in at.warning)
    _login(at, "ana@uniminuto.edu.co")
    assert at.session_state["user"]["role"] == "profesor"
    assert "inventario" in at.session_state["pages"]


def test_session_expires_after_the_configured_time():
    at, storage = _start()
    _make_user(storage, "ana@uniminuto.edu.co")
    _login(at, "ana@uniminuto.edu.co")

    at.session_state["login_at"] = datetime.now(timezone.utc) - timedelta(
        minutes=auth.DEFAULT_SESSION_TIMEOUT_MINUTES + 5
    )
    at.run()
    assert at.session_state["user"] is None
    assert any("expiro" in w.value.lower() for w in at.warning)


def test_repeated_wrong_passwords_lock_the_login_screen():
    at, storage = _start()
    _make_user(storage, "ana@uniminuto.edu.co")
    for _ in range(auth.DEFAULT_LOGIN_MAX_ATTEMPTS):
        _login(at, "ana@uniminuto.edu.co", "clave-equivocada")
        assert any(auth.GENERIC_LOGIN_ERROR in e.value for e in at.error)
    _login(at, "ana@uniminuto.edu.co")           # ni la clave correcta entra
    assert at.session_state["user"] is None
    assert any("demasiados intentos" in e.value.lower() for e in at.error)


# --- registro de profesores ----------------------------------------------------------

def test_whitelisted_email_without_smtp_is_created_as_student_with_guidance():
    at, storage = _start()
    storage.add_to_whitelist("prof@uniminuto.edu.co")      # SMTP no esta configurado
    _register(at, "Profesor Gomez", "DOC-1", "prof@uniminuto.edu.co")

    assert storage.get_user_by_email("prof@uniminuto.edu.co")["role"] == "estudiante"
    assert any("lista de supervisores" in i.value for i in at.info)


@pytest.fixture
def captured_code(monkeypatch):
    sent = {}

    def fake_send(email, code):
        sent.update(email=email, code=code)
        return True, "ok"

    monkeypatch.setattr(auth, "send_verification_email", fake_send)
    return sent


def test_whitelisted_email_gets_professor_role_only_after_the_code(captured_code):
    at, storage = _start()
    storage.add_to_whitelist("prof@uniminuto.edu.co")
    _register(at, "Profesor Gomez", "DOC-1", "prof@uniminuto.edu.co")

    assert storage.get_user_by_email("prof@uniminuto.edu.co") is None   # aun no hay cuenta
    assert captured_code["email"] == "prof@uniminuto.edu.co"
    assert any("codigo de 6 digitos" in i.value for i in at.info)

    code_input = at.text_input[2]                            # tras email y clave del login
    wrong = "000000" if captured_code["code"] != "000000" else "111111"
    code_input.input(wrong)
    at.button[1].click().run()
    assert any("incorrecto" in e.value.lower() for e in at.error)
    assert storage.get_user_by_email("prof@uniminuto.edu.co") is None

    at.text_input[2].input(captured_code["code"])
    at.button[1].click().run()
    assert not at.exception
    assert storage.get_user_by_email("prof@uniminuto.edu.co")["role"] == "profesor"
    assert any("profesor" in s.value for s in at.success)


def test_cancelling_the_verification_creates_no_account(captured_code):
    at, storage = _start()
    storage.add_to_whitelist("prof@uniminuto.edu.co")
    _register(at, "Profesor Gomez", "DOC-1", "prof@uniminuto.edu.co")
    at.button[3].click().run()                                # Cancelar
    assert storage.get_user_by_email("prof@uniminuto.edu.co") is None
    assert "pending_registration" not in at.session_state


def test_regular_student_registration_does_not_ask_for_a_code(captured_code):
    at, storage = _start()
    _register(at, "Estudiante Prueba", "TI-1", "est@uniminuto.edu.co")
    assert captured_code == {}                                # no se envio ningun codigo
    assert storage.get_user_by_email("est@uniminuto.edu.co")["role"] == "estudiante"


# --- permisos por vista y escape de HTML ------------------------------------------------

def _guard_script():
    import streamlit as st

    from views import inventario, reportes, usuarios

    st.session_state.storage = object()
    st.session_state.user = {"role": "estudiante", "institutional_email": "x@uniminuto.edu.co"}
    {"inventario": inventario, "reportes": reportes, "usuarios": usuarios}[st.session_state.target].render()


@pytest.mark.parametrize("view", ["inventario", "reportes", "usuarios"])
def test_sensitive_views_refuse_a_student_even_if_reached_directly(view):
    at = AppTest.from_function(_guard_script)
    at.session_state["target"] = view
    at.run()
    assert not at.exception
    assert any("No tienes permiso" in e.value for e in at.error)


def _header_script():
    from core.ui import page_header

    page_header("<img src=x onerror=alert(1)>", subtitle="<b>hola</b>")


def test_page_header_escapes_user_supplied_text():
    at = AppTest.from_function(_header_script)
    at.run()
    rendered = " ".join(m.value for m in at.markdown)
    assert "&lt;img src=x" in rendered and "<img src=x" not in rendered
    assert "&lt;b&gt;hola" in rendered


def test_sidebar_escapes_the_user_name():
    at, storage = _start()
    _make_user(storage, "eve@uniminuto.edu.co", name="<script>alert(1)</script> Perez")
    _login(at, "eve@uniminuto.edu.co")
    sidebar_html = " ".join(m.value for m in at.sidebar.markdown)
    assert "&lt;script&gt;" in sidebar_html
    assert "<script>alert" not in sidebar_html
