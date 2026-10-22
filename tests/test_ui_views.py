# -*- coding: utf-8 -*-
"""Vistas rediseñadas (inicio, perfil, acerca de, préstamos, reservas, usuarios,
login y barra lateral) con la app real: se pintan sin errores para cada rol,
escapan los textos escritos por usuarios y conservan el orden de los campos
del acceso (del que dependen las pruebas de seguridad)."""

from datetime import datetime, timedelta, timezone

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth, loans as loans_core  # noqa: E402
from core.storage import LabStorage  # noqa: E402

# Ruta absoluta: desde Streamlit 1.65, AppTest.from_file resuelve las rutas
# relativas desde el archivo de prueba (tests/), no desde la raiz del repo.
APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

ACTOR = "admin@uniminuto.edu.co"
EVIL = "<img src=x onerror=alert(1)>"
PASSWORD = "ClaveSegura123"


@pytest.fixture
def lab():
    storage = LabStorage()
    users = {
        "estudiante": storage.create_user("Laura Gómez", "laura@uniminuto.edu.co", "hash", "estudiante",
                                          "Ingeniería de Sistemas", "TI-1"),
        "profesor": storage.create_user("Carlos Ruiz", "carlos@uniminuto.edu.co", "hash", "profesor",
                                        "Ingeniería", "DOC-1"),
        "maestro": storage.create_user(f"Ana {EVIL}", "ana@uniminuto.edu.co", "hash", "maestro", "Coordinación", ""),
    }
    storage.save_item({"name": EVIL, "item_type": "standalone", "quantity": 3, "min_stock_alert": 2},
                      "LAB-001", is_new=True, actor_email=ACTOR)
    storage.save_item({"name": "Arduino UNO", "item_type": "standalone", "quantity": 5},
                      "LAB-002", is_new=True, actor_email=ACTOR)
    now = datetime.now(timezone.utc)
    ok, _, _ = loans_core.checkout(storage, "LAB-001", 2, users["estudiante"], expected_return_at=now - timedelta(days=1))
    assert ok
    ok, _, returned = loans_core.checkout(storage, "LAB-002", 1, users["profesor"], expected_return_at=now + timedelta(days=2))
    assert ok
    storage.return_loan(returned["id"], ACTOR)
    start = now + timedelta(days=3)
    own = storage.create_reservation({
        "scope_type": "activity", "activity": EVIL, "purpose": "Robótica", "attendees": 12,
        "start_at": start, "end_at": start + timedelta(hours=2),
    }, users["profesor"])
    storage.update_reservation_status(own["id"], "approved", ACTOR, "OK")
    storage.create_reservation({
        "scope_type": "full_lab", "activity": "Laboratorio completo", "purpose": "Feria", "attendees": 40,
        "start_at": start + timedelta(days=2), "end_at": start + timedelta(days=2, hours=3),
    }, users["estudiante"])
    storage.add_to_whitelist("nuevo.profe@uniminuto.edu.co")
    return {role: auth.public_user(user) for role, user in users.items()}


def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    importlib.import_module(f"views.{st.session_state.view}").render()


def _render(view, user):
    at = AppTest.from_function(_view_script, default_timeout=30)
    at.session_state["view"] = view
    at.session_state["user"] = user
    at.run()
    assert not at.exception, at.exception
    return at


def _html(at):
    return " ".join(m.value for m in at.markdown)


@pytest.mark.parametrize("role", ["estudiante", "profesor", "maestro"])
@pytest.mark.parametrize("view", ["inicio", "perfil", "acerca_de", "prestamos", "reservas"])
def test_views_render_for_every_role(lab, view, role):
    html = _html(_render(view, lab[role]))
    assert "<img src=x" not in html


@pytest.mark.parametrize("role", ["estudiante", "profesor", "maestro"])
def test_dashboard_shows_hero_stats_and_escaped_alerts(lab, role):
    html = _html(_render("inicio", lab[role]))
    assert "lab-hero" in html and "lab-stat-grid" in html
    assert "lab-alert-list" in html
    assert "&lt;img src=x" in html                          # item con nombre malicioso, escapado
    if role == "estudiante":
        assert "está vencido" in html                       # su propio préstamo vencido
    else:
        assert "Vencido:" in html and "Laura Gómez" in html


def test_dashboard_without_items_explains_what_to_do(lab):
    storage = LabStorage()
    for item_id in ("LAB-001", "LAB-002"):
        for loan in storage.get_open_loans_for_item(item_id):
            storage.return_loan(loan["id"], ACTOR)
        storage.delete_item(item_id, ACTOR)
    at = _render("inicio", lab["profesor"])
    assert any("inventario todavía está vacío" in i.value for i in at.info)
    assert "Sin alertas por el momento." in _html(at)


def test_profile_card_escapes_the_name(lab):
    html = _html(_render("perfil", lab["maestro"]))
    assert "lab-profile" in html and "lab-kv" in html
    assert "&lt;img src=x" in html


def test_reviewer_with_own_approved_reservation_renders_both_lists(lab):
    # La misma reserva aparece en "Mis reservas" y en "Gestionar": antes chocaban
    # las keys de sus botones "Cancelar".
    at = _render("reservas", lab["profesor"])
    cancel_keys = [b.key for b in at.button if b.label == "Cancelar"]
    assert len(cancel_keys) == len(set(cancel_keys)) >= 2
    assert "lab-badge--success" in _html(at)


def test_loans_view_marks_overdue_loans(lab):
    html = _html(_render("prestamos", lab["profesor"]))
    assert "Vencido" in html and "lab-badge--danger" in html
    assert "Devuelto" in html                               # pestaña de historial


def test_users_view_renders_cards_for_master(lab):
    at = _render("usuarios", lab["maestro"])
    html = _html(at)
    assert "lab-card-head" in html and "&lt;img src=x" in html
    assert any(b.label == "Quitar" for b in at.button)


# --- login y barra lateral (app completa) ---------------------------------------------

def test_login_keeps_field_order_and_local_branding():
    at = AppTest.from_file(APP_PATH)
    at.run()
    assert not at.exception
    assert [w.label for w in at.text_input[:2]] == ["Correo institucional", "Contraseña"]
    assert at.button[0].label == "Ingresar"
    html = _html(at)
    assert "lab-auth-brand" in html and "data:image/png;base64," in html
    assert "upload.wikimedia.org" not in html


def test_sidebar_shows_user_card_and_logout_still_works():
    at = AppTest.from_file(APP_PATH)
    at.run()
    storage = at.session_state["storage"]
    storage.create_user("Laura Gómez", "laura@uniminuto.edu.co", auth.hash_password(PASSWORD), "estudiante",
                        "Sistemas", "TI-1")
    at.text_input[0].input("laura@uniminuto.edu.co")
    at.text_input[1].input(PASSWORD)
    at.button[0].click().run()
    assert not at.exception
    assert "solicitudes" in at.session_state["pages"] and "trazabilidad" not in at.session_state["pages"]
    sidebar = " ".join(m.value for m in at.sidebar.markdown)
    assert "user-chip" in sidebar and "role-estudiante" in sidebar
    logout = next(b for b in at.sidebar.button if "Cerrar sesión" in b.label)
    logout.click().run()
    assert at.session_state["user"] is None
