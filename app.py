# -*- coding: utf-8 -*-
"""
Inventario de Laboratorio UNIMINUTO
Gestion de inventario y prestamos (checkout/checkin) para el laboratorio de
ingenieria de UNIMINUTO, con codigos de barras jerarquicos (contenedor
maestro + items hijos) y login por roles (estudiante / profesor / maestro)
via correo institucional.
"""

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from core.storage import LabStorage, RemoteUnavailableError
from core import auth, live_data, permissions, session
from core.ui import LOGO_PATH, SYMBOL_PATH, footer, sync_status_banner, user_chip_html

st.set_page_config(
    page_title="Inventario de Laboratorio UNIMINUTO",
    page_icon=str(SYMBOL_PATH) if SYMBOL_PATH.exists() else "🎓",
    layout="wide",
)


@st.cache_data
def load_css():
    # Ruta relativa a este archivo (no al directorio de trabajo) y UTF-8 explicito:
    # la hoja de estilos tiene tildes y simbolos.
    try:
        return (Path(__file__).resolve().parent / "style.css").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


css = load_css()
if css:
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


@st.cache_resource
def get_storage():
    return LabStorage()


try:
    storage = get_storage()
except RemoteUnavailableError as e:
    # GitHub no respondió y no hay copia local: no se arranca con una base vacía.
    st.error(f"**No se pudo cargar la base de datos.** {e}", icon=":material/cloud_off:")
    st.button(":material/refresh: Reintentar", key="retry_storage_boot")   # el rerun vuelve a intentarlo
    st.stop()
except Exception as e:
    st.error(f"**Error critico de inicializacion de la base de datos:** {e}")
    st.stop()

st.session_state.storage = storage
auth.ensure_master_seed(storage)

if "user" not in st.session_state:
    st.session_state.user = None

# Aplica la escritura/borrado programado en el render anterior.
session.render_cookie_command()
if not st.session_state.user and session.enabled():
    saved_token = session.cookie_from_context()
    if saved_token:
        restored_user, restored_claims, restore_problem = session.verify_token(saved_token, storage)
        if restored_user and session.tab_closed(restored_claims):
            session.revoke(restored_claims, storage)
            restored_user, restore_problem = None, "Tu sesión se cerró al cerrar la pestaña."
        if restored_user:
            st.session_state.user = restored_user
            st.session_state.session_token = saved_token
            st.session_state.session_claims = restored_claims
            st.session_state.login_at = datetime.fromtimestamp(
                restored_claims["iat"], tz=timezone.utc
            )
        else:
            session.queue_cookie(clear=True)
            session.render_cookie_command()
            st.session_state.session_notice = restore_problem

# Chip NFC: un toque abre la app con ?nfc=<codigo>. Se guarda ANTES del login
# para registrarlo en cuanto la persona inicie sesion (ver views/nfc_tap.py).
from views import nfc_tap
nfc_tap.capture_tap()

if not st.session_state.user:
    from views import login
    nfc_tap.render_login_hint()
    login.render()
    st.stop()

# La sesion se revalida contra la base en CADA recarga: un cambio de rol o una
# cuenta deshabilitada surten efecto de inmediato, y la sesion expira sola.
token = st.session_state.get("session_token", "")
if session.enabled() and token:
    session_user, session_claims, session_problem = session.verify_token(token, storage)
else:
    session_user, session_problem = auth.validate_session(
        storage, st.session_state.user, st.session_state.get("login_at")
    )
    session_claims = st.session_state.get("session_claims") or {}
if session_user is None:
    if st.session_state.get("user") and session_problem:
        session.audit(storage, "session_expired", st.session_state.user,
                      {"reason": session_problem})
    session.revoke(token)   # ya es inválida por caducidad, época o estado: no hace falta persistirla
    session.queue_cookie(clear=True)
    st.session_state.user = None
    st.session_state.login_at = None
    st.session_state.session_token = ""
    st.session_state.session_claims = {}
    st.session_state.session_notice = session_problem
    st.rerun()
st.session_state.user = session_user
if st.session_state.get("login_at") is None:
    st.session_state.login_at = datetime.now(timezone.utc)
if session.enabled():
    # Renovación deslizante (S2): solo en ejecuciones completas iniciadas por la
    # persona; los fragmentos con `run_every` no pasan por aquí, así que la
    # auto-actualización nunca cuenta como actividad. Se reescribe la cookie a
    # lo sumo una vez por minuto y nunca se escribe en el Excel.
    if session.needs_rotation(session_claims):
        rotated, claims = session.issue_token(session_user, claims=session_claims)
        st.session_state.session_token = rotated
        st.session_state.session_claims = claims
        session.queue_cookie(rotated)
    else:
        st.session_state.session_claims = session_claims
user = session_user
live_data.poll(storage)

if session.enabled() and session.heartbeat_every():
    @st.fragment(run_every=session.heartbeat_every())
    def _session_heartbeat():
        # Señal de pestaña abierta: no renueva la inactividad ni escribe datos.
        session.beat(st.session_state.get("session_claims") or {})

    with st.sidebar:
        _session_heartbeat()

# Clave restablecida por el perfil maestro: antes de usar la app el usuario elige
# una propia (views/perfil.py). Las demas cuentas siguen el flujo de siempre.
if auth.must_change_password(user):
    from views import perfil
    perfil.render_forced_password_change()
    st.stop()

from views import despacho, inicio, escanear, inventario, prestamos, solicitudes, trazabilidad, usuarios, reportes, acerca_de, perfil
from views import nfc as nfc_page
from views import ubicaciones as ubicaciones_page

pages = {
    "inicio": st.Page(inicio.render, title="Inicio", icon=":material/home:", default=True, url_path="inicio"),
}
if permissions.has_cap(user, permissions.CAP_REQUEST):
    # «Buscar» conserva la clave y la ruta «escanear»: los chips NFC ya grabados apuntan ahí.
    pages["escanear"] = st.Page(escanear.render, title="Buscar", icon=":material/search:", url_path="escanear")
    pages["solicitudes"] = st.Page(solicitudes.render, title="Solicitudes", icon=":material/edit_note:", url_path="solicitudes")
    pages["prestamos"] = st.Page(prestamos.render, title="Préstamos", icon=":material/assignment:", url_path="prestamos")
if permissions.has_cap(user, permissions.CAP_VIEW_ALL_TRACES):
    # El cliente sigue su pedido en Solicitudes → Mis pedidos; la trazabilidad completa es del personal.
    pages["trazabilidad"] = st.Page(trazabilidad.render, title="Trazabilidad", icon=":material/explore:", url_path="trazabilidad")
if permissions.has_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE) or permissions.has_cap(user, permissions.CAP_RECEIVE_AND_DELIVER):
    pages["despacho"] = st.Page(despacho.render, title="Despacho", icon=":material/local_shipping:", url_path="despacho")

if permissions.has_cap(user, permissions.CAP_EDIT_INVENTORY):
    pages["inventario"] = st.Page(inventario.render, title="Inventario", icon=":material/inventory_2:", url_path="inventario")
if permissions.has_cap(user, permissions.CAP_MANAGE_LOCATIONS):
    pages["ubicaciones"] = st.Page(ubicaciones_page.render_page, title="Ubicaciones", icon=":material/map:", url_path="ubicaciones")
if permissions.has_cap(user, permissions.CAP_REPORTS):
    pages["reportes"] = st.Page(reportes.render, title="Reportes", icon=":material/bar_chart:", url_path="reportes")
if permissions.has_cap(user, permissions.CAP_MANAGE_NFC):
    pages["nfc"] = st.Page(nfc_page.render, title="Chips NFC", icon=":material/nfc:", url_path="nfc")
if permissions.has_cap(user, permissions.CAP_MANAGE_USERS):
    pages["usuarios"] = st.Page(usuarios.render, title="Usuarios", icon=":material/group:", url_path="usuarios")

pages["perfil"] = st.Page(perfil.render, title="Mi perfil", icon=":material/person:", url_path="perfil")
pages["acerca_de"] = st.Page(acerca_de.render, title="Acerca de", icon=":material/info:", url_path="acerca-de")

st.session_state.pages = pages

# Navegación agrupada por capacidades.
principal_keys = ["inicio", "escanear", "solicitudes", "prestamos", "trazabilidad"]
nav_sections = {"Principal": [pages[key] for key in principal_keys if key in pages]}
if "despacho" in pages:
    nav_sections["Operación"] = [pages["despacho"]]
gestion_keys = ["inventario", "ubicaciones", "reportes", "nfc"]
management = [pages[key] for key in gestion_keys if key in pages]
if management:
    nav_sections["Gestión"] = management
if "usuarios" in pages:
    nav_sections["Administración"] = [pages["usuarios"]]
nav_sections["Mi cuenta"] = [pages["perfil"], pages["acerca_de"]]

# Marca institucional: el logotipo completo, centrado, encabeza la barra
# lateral; con la barra cerrada (celular) la cabecera muestra solo el simbolo,
# que sigue siendo legible a ese tamaño. Se sirven desde assets/, sin Internet.
if LOGO_PATH.exists():
    st.logo(str(LOGO_PATH), size="large",
            icon_image=str(SYMBOL_PATH) if SYMBOL_PATH.exists() else str(LOGO_PATH))

with st.sidebar:
    st.markdown(user_chip_html(user), unsafe_allow_html=True)
    if st.button("Cerrar sesión", icon=":material/logout:", use_container_width=True):
        session.audit(storage, "session_logout", user)
        session.revoke(st.session_state.get("session_token", ""), storage)
        session.queue_cookie(clear=True)
        st.session_state.user = None
        st.session_state.login_at = None
        st.session_state.session_token = ""
        st.session_state.session_claims = {}
        st.rerun()
    if st.button("Actualizar", icon=":material/refresh:", use_container_width=True):
        storage.refresh_data(force_remote=True)
        st.rerun()
    st.caption(live_data.freshness(storage))
    if permissions.has_cap(user, permissions.CAP_MANAGE_USERS) and not session.enabled():
        st.warning("Configura SESSION_SECRET para conservar la sesión al recargar la página (F5).")

sync_status_banner(storage, user)

nav = st.navigation(nav_sections)
# Toque NFC pendiente: se registra una sola vez y lleva a su pagina.
nfc_tap.handle_pending(storage, user, pages, nav)
nfc_tap.render_tap_banner()
nav.run()
# Escribe en esta misma ejecución la cookie renovada (si la hay).
session.render_cookie_command()
footer()
