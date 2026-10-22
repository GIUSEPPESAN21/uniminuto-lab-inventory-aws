# -*- coding: utf-8 -*-
"""Restablecer contraseñas con la app real (AppTest): la opcion en la pagina
Usuarios (con confirmacion y clave temporal mostrada una sola vez), el cierre
de la sesion abierta del usuario y el cambio obligatorio al volver a ingresar."""

from pathlib import Path

import bcrypt
import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth  # noqa: E402
from core.storage import LabStorage  # noqa: E402

# Ruta absoluta: desde Streamlit 1.65, AppTest.from_file resuelve las rutas
# relativas desde el archivo de prueba (tests/), no desde la raiz del repo.
APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
PASSWORD = "ClaveSegura123"
_REAL_GENSALT = bcrypt.gensalt


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch):
    """bcrypt real con el costo minimo: las pruebas hashean muchas claves."""
    monkeypatch.setattr(bcrypt, "gensalt", lambda *args, **kwargs: _REAL_GENSALT(rounds=4))


@pytest.fixture
def lab():
    storage = LabStorage()
    users = {
        "maestro": storage.create_user("Ana Maestra", "ana@uniminuto.edu.co", auth.hash_password(PASSWORD),
                                       "maestro", "Coordinación", ""),
        "otro_maestro": storage.create_user("Otro Maestro", "otro@uniminuto.edu.co",
                                            auth.hash_password(PASSWORD), "maestro", "Coordinación", ""),
        "profesor": storage.create_user("Carlos Ruiz", "carlos@uniminuto.edu.co", auth.hash_password(PASSWORD),
                                        "profesor", "Ingeniería", "DOC-1"),
        "estudiante": storage.create_user("Laura Gómez", "laura@uniminuto.edu.co", auth.hash_password(PASSWORD),
                                          "estudiante", "Sistemas", "TI-1"),
    }
    return storage, users


def _users_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import usuarios

    st.session_state.storage = LabStorage()
    usuarios.render()


def _users_page(master):
    at = AppTest.from_function(_users_script, default_timeout=30)
    at.session_state["user"] = auth.public_user(master)
    at.run()
    assert not at.exception, at.exception
    return at


def _keys(at, prefix):
    return {b.key for b in at.button if (b.key or "").startswith(prefix)}


def _html(at):
    return " ".join(m.value for m in at.markdown)


# --- pagina Usuarios ----------------------------------------------------------------

def test_option_only_for_students_and_professors(lab):
    storage, users = lab
    at = _users_page(users["maestro"])
    starts = _keys(at, "pwreset_start_")
    assert starts == {f"pwreset_start_{users[k]['id']}_0" for k in ("profesor", "estudiante")}
    # Ni la propia tarjeta (se usa Mi perfil) ni la de otro maestro.
    assert not any(users["maestro"]["id"] in key or users["otro_maestro"]["id"] in key for key in starts)
    assert sum("Restablecer contraseña" in e.label for e in at.expander) == 2
    assert not [c for c in at.checkbox if str(c.key).startswith("pwreset_mail_")]   # sin SMTP no se ofrece el correo


def test_generated_password_needs_confirmation_and_is_shown_once(lab):
    storage, users = lab
    uid = users["estudiante"]["id"]
    at = _users_page(users["maestro"])

    at.button(key=f"pwreset_start_{uid}_0").click().run()
    assert not at.exception
    assert any("¿Restablecer la contraseña de Laura Gómez?" in w.value for w in at.warning)
    assert auth.verify_password(PASSWORD, storage.get_user_by_id(uid)["password_hash"])   # aun nada

    at.button(key=f"pwreset_yes_{uid}_0").click().run()
    assert not at.exception
    assert len(at.code) == 1
    temporary = at.code[0].value
    fresh = storage.get_user_by_id(uid)
    assert auth.verify_password(temporary, fresh["password_hash"])
    assert auth.must_change_password(fresh)
    assert any("restablecida" in s.value for s in at.success)
    assert any("Entrégala en persona" in c.value for c in at.caption)
    assert "Clave temporal" in _html(at)                        # pastilla en la tarjeta
    assert len(storage.get_trace_events(event_type=auth.PASSWORD_RESET_EVENT)) == 1

    at.run()                                                     # cualquier otra interaccion
    assert not at.exception
    assert not at.code                                           # ya no se vuelve a mostrar
    assert f"pwreset_result_{uid}" not in at.session_state      # ni queda guardada en la sesion


def test_cancel_leaves_the_password_untouched(lab):
    storage, users = lab
    uid = users["profesor"]["id"]
    at = _users_page(users["maestro"])
    at.button(key=f"pwreset_start_{uid}_0").click().run()
    at.button(key=f"pwreset_no_{uid}_0").click().run()
    assert not at.exception
    assert not any("¿Restablecer" in w.value for w in at.warning)
    fresh = storage.get_user_by_id(uid)
    assert auth.verify_password(PASSWORD, fresh["password_hash"])
    assert not auth.must_change_password(fresh)
    assert storage.get_trace_events(event_type=auth.PASSWORD_RESET_EVENT) == []


def test_manual_password_is_validated_then_applied_without_showing_it(lab):
    storage, users = lab
    uid = users["profesor"]["id"]
    at = _users_page(users["maestro"])
    at.radio(key=f"pwreset_mode_{uid}_0").set_value("manual").run()
    at.text_input(key=f"pwreset_pw_{uid}_0").input("ClaveNueva2026")
    at.text_input(key=f"pwreset_pw2_{uid}_0").input("OtraClave2026")
    at.button(key=f"pwreset_start_{uid}_0").click().run()
    assert any("no coinciden" in e.value for e in at.error)
    assert not any("¿Restablecer" in w.value for w in at.warning)

    at.text_input(key=f"pwreset_pw_{uid}_0").input("corta")
    at.text_input(key=f"pwreset_pw2_{uid}_0").input("corta")
    at.button(key=f"pwreset_start_{uid}_0").click().run()
    assert any("al menos 8" in e.value for e in at.error)

    at.text_input(key=f"pwreset_pw_{uid}_0").input("ClaveNueva2026")
    at.text_input(key=f"pwreset_pw2_{uid}_0").input("ClaveNueva2026")
    at.button(key=f"pwreset_start_{uid}_0").click().run()
    at.button(key=f"pwreset_yes_{uid}_0").click().run()
    assert not at.exception
    assert not at.code                                           # el maestro ya la conoce
    fresh = storage.get_user_by_id(uid)
    assert auth.verify_password("ClaveNueva2026", fresh["password_hash"])
    assert auth.must_change_password(fresh)
    # El formulario queda limpio (nuevas keys) para un proximo restablecimiento.
    assert at.radio(key=f"pwreset_mode_{uid}_1").value == "generate"


def test_email_option_appears_only_with_smtp_and_is_off_by_default(lab, monkeypatch):
    storage, users = lab
    uid = users["estudiante"]["id"]
    sent = []
    monkeypatch.setattr(auth, "email_delivery_configured", lambda: True)
    monkeypatch.setattr(auth, "send_temporary_password_email",
                        lambda email, password, name="": sent.append((email, password)) or (True, "ok"))
    at = _users_page(users["maestro"])
    box = at.checkbox(key=f"pwreset_mail_{uid}_0")
    assert box.label == "Enviar la contraseña temporal al correo del usuario"
    assert box.value is False

    box.check().run()
    at.button(key=f"pwreset_start_{uid}_0").click().run()
    at.button(key=f"pwreset_yes_{uid}_0").click().run()
    assert not at.exception
    assert sent and sent[0][0] == "laura@uniminuto.edu.co"
    assert sent[0][1] == at.code[0].value
    assert any("correo institucional" in i.value for i in at.info)


# --- app completa: sesion abierta y cambio obligatorio ---------------------------------

def _login(at, email, password):
    at.text_input[0].input(email)
    at.text_input[1].input(password)
    at.button[0].click().run()
    assert not at.exception


def _start_app():
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception
    return at


def test_reset_logs_out_the_open_session(lab):
    storage, users = lab
    at = _start_app()
    _login(at, "laura@uniminuto.edu.co", PASSWORD)
    assert at.session_state["user"] is not None

    auth.admin_reset_password(storage, users["maestro"], users["estudiante"]["id"])
    at.run()
    assert not at.exception
    assert at.session_state["user"] is None
    assert any(auth.PASSWORD_CHANGED_SESSION_NOTICE in w.value for w in at.warning)


def test_login_with_temporary_password_forces_a_new_one(lab):
    storage, users = lab
    uid = users["estudiante"]["id"]
    result, _ = auth.admin_reset_password(storage, users["maestro"], uid)
    at = _start_app()
    _login(at, "laura@uniminuto.edu.co", result["temporary_password"])

    # Pantalla obligatoria: sin navegacion ni barra lateral hasta cambiarla.
    assert at.session_state["user"]["id"] == uid
    assert "Elige una contraseña nueva" in _html(at)
    assert "pages" not in at.session_state
    assert not at.sidebar.markdown
    assert [w.label for w in at.text_input] == ["Nueva contraseña", "Confirmar nueva contraseña"]

    submit = next(b for b in at.button if b.label == "Guardar y continuar")
    at.text_input[0].input("MiClavePropia1")
    at.text_input[1].input("MiClavePropia2")
    submit.click().run()
    assert any("no coinciden" in e.value for e in at.error)
    assert "pages" not in at.session_state

    at.text_input[0].input("MiClavePropia1")
    at.text_input[1].input("MiClavePropia1")
    next(b for b in at.button if b.label == "Guardar y continuar").click().run()
    assert not at.exception
    assert at.session_state["user"] is not None                 # sigue dentro, ya con su clave
    assert "pages" in at.session_state and "inicio" in at.session_state["pages"]
    fresh = storage.get_user_by_id(uid)
    assert not auth.must_change_password(fresh)
    assert auth.verify_password("MiClavePropia1", fresh["password_hash"])

    at.run()                                                     # la sesion sobrevive a recargas
    assert at.session_state["user"] is not None and "pages" in at.session_state


def test_forced_screen_allows_logging_out(lab):
    storage, users = lab
    result, _ = auth.admin_reset_password(storage, users["maestro"], users["profesor"]["id"])
    at = _start_app()
    _login(at, "carlos@uniminuto.edu.co", result["temporary_password"])
    next(b for b in at.button if "Cerrar sesión" in b.label).click().run()
    assert not at.exception
    assert at.session_state["user"] is None
    assert at.button[0].label == "Ingresar"


def test_accounts_without_reset_go_straight_to_the_app(lab):
    _, users = lab
    at = _start_app()
    _login(at, "carlos@uniminuto.edu.co", PASSWORD)
    assert "Elige una contraseña nueva" not in _html(at)
    assert "pages" in at.session_state
