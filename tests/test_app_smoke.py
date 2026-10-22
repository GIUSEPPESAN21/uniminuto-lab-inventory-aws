# -*- coding: utf-8 -*-
"""Prueba de humo end-to-end usando streamlit.testing.v1.AppTest: simula un
login/registro reales (sin navegador) contra la app completa. No depende de
Secrets de Streamlit (MASTER_EMAIL, etc.): la cuenta usada para el login se
siembra directamente en el storage en memoria de la propia sesion de prueba,
para que la suite sea 100% determinista tanto en local como en CI."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest

from core import auth

# Ruta absoluta: desde Streamlit 1.65, AppTest.from_file resuelve las rutas
# relativas desde el archivo de prueba (tests/), no desde la raiz del repo.
APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def test_login_screen_renders_without_exceptions():
    at = AppTest.from_file(APP_PATH)
    at.run()
    assert not at.exception
    assert any("Inventario de Laboratorio" in md.value for md in at.markdown)


def test_master_login_succeeds_and_shows_navigation():
    at = AppTest.from_file(APP_PATH)
    at.run()
    assert not at.exception

    # Sembramos una cuenta maestra directamente en el storage de la sesion
    # de prueba (independiente de cualquier secrets.toml).
    storage = at.session_state["storage"]
    storage.create_user(
        full_name="Maestro de Prueba", email="maestro.prueba@uniminuto.edu.co",
        password_hash=auth.hash_password("ClaveMaestra123"), role="maestro",
    )

    # Los widgets dentro de un st.form no exponen una key legible facil de
    # adivinar; se localizan por orden dentro del primer formulario (login).
    email_input = at.text_input[0]
    password_input = at.text_input[1]
    email_input.input("maestro.prueba@uniminuto.edu.co")
    password_input.input("ClaveMaestra123")
    at.button[0].click().run()

    assert not at.exception
    assert at.session_state["user"] is not None
    assert at.session_state["user"]["role"] == "maestro"


def test_student_self_registration_end_to_end():
    at = AppTest.from_file(APP_PATH)
    at.run()
    assert not at.exception

    # Widgets del formulario de registro (2da pestana) aparecen despues de
    # los del login (1era pestana) en el orden de ejecucion del script.
    full_name_input, student_id_input, email_input, program_input, pw_input, pw2_input = at.text_input[2:8]
    full_name_input.input("Estudiante de Prueba")
    student_id_input.input("TI2024001")
    email_input.input("estudiante.prueba@uniminuto.edu.co")
    program_input.input("Ingenieria Industrial")
    pw_input.input("ClaveSegura123")
    pw2_input.input("ClaveSegura123")
    at.button[1].click().run()

    assert not at.exception
    assert any("estudiante" in s.value.lower() for s in at.success)


def _login(role: str, email: str):
    at = AppTest.from_file(APP_PATH)
    at.run()
    at.session_state["storage"].create_user(
        full_name="Usuario de Prueba", email=email,
        password_hash=auth.hash_password("ClaveSegura123"), role=role,
    )
    at.text_input[0].input(email)
    at.text_input[1].input("ClaveSegura123")
    at.button[0].click().run()
    assert not at.exception
    return at


def test_ubicaciones_in_menu_for_managers_only():
    manager = _login("maestro", "maestro.ubic@uniminuto.edu.co")
    assert "ubicaciones" in manager.session_state["pages"]
    student = _login("estudiante", "estudiante.ubic@uniminuto.edu.co")
    assert "ubicaciones" not in student.session_state["pages"]


def _render_ubicaciones_page():
    import streamlit as st

    from core.storage import LabStorage
    from views import ubicaciones

    if "storage" not in st.session_state:
        st.session_state.storage = LabStorage()
    st.session_state.user = {"id": "u1", "role": st.session_state.get("_role", "maestro"), "full_name": "T"}
    ubicaciones.render_page()


def test_ubicaciones_page_has_shelf_and_table_creation():
    at = AppTest.from_function(_render_ubicaciones_page)
    at.run()
    assert not at.exception
    labels = [e.label for e in at.expander]
    assert any("Crear estantería" in label for label in labels)
    assert any("mesa de trabajo" in label.lower() for label in labels)


def test_ubicaciones_page_blocked_for_students():
    at = AppTest.from_function(_render_ubicaciones_page)
    at.session_state["_role"] = "estudiante"
    at.run()
    assert not at.exception
    assert not any("Crear estantería" in e.label for e in at.expander)
