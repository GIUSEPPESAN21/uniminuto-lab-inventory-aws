# -*- coding: utf-8 -*-
"""views/perfil.py - Perfil del usuario logueado: datos propios y cambio de contrasena.

Incluye la pantalla obligatoria de cambio de clave tras un restablecimiento del
perfil maestro (`render_forced_password_change`), que app.py muestra en lugar
de la navegacion mientras la cuenta tenga una clave temporal."""

import streamlit as st

from core import auth, session
from core.ui import (
    ROLE_LABELS, badge_html, card, centered_columns, esc, footer, initials, kv_list, page_header,
    section_title,
)

_ROLE_TONES = {
    "estudiante": "success", "operario": "neutral", "encargado": "info",
    "profesor": "info", "maestro": "warning",
}


def render():
    storage = st.session_state.storage
    user = st.session_state.user

    page_header("Mi perfil", icon=":material/person:", subtitle="Tus datos y seguridad de la cuenta")

    col_info, col_security = st.columns([1.15, 1], gap="large")
    with col_info:
        section_title("Datos de la cuenta", icon=":material/badge:")
        with card("perfil_datos"):
            role = user["role"]
            st.markdown(
                f'<div class="lab-profile"><div class="lab-profile__avatar" aria-hidden="true">'
                f'{esc(initials(user["full_name"]))}</div><div style="min-width:0;">'
                f'<div class="lab-profile__name">{esc(user["full_name"])}</div>'
                f'{badge_html(ROLE_LABELS.get(role, role), _ROLE_TONES.get(role, "neutral"), "👤")}'
                f"</div></div>",
                unsafe_allow_html=True,
            )
            kv_list([
                ("ID Estudiante", user.get("student_id") or "N/A"),
                ("Correo institucional", user["institutional_email"]),
                ("Programa / departamento", user.get("program_or_department") or "N/A"),
            ])

    with col_security:
        section_title("Cambiar contraseña", icon=":material/lock:", caption="Usa al menos 8 caracteres.")
        with st.form("change_password_form", clear_on_submit=True):
            current_pw = st.text_input("Contraseña actual", type="password")
            new_pw = st.text_input("Nueva contraseña", type="password")
            new_pw2 = st.text_input("Confirmar nueva contraseña", type="password")
            submitted = st.form_submit_button("Actualizar contraseña", type="primary", use_container_width=True)

            if submitted:
                fresh_user = storage.get_user_by_email(user["institutional_email"])
                if not auth.verify_password(current_pw, fresh_user.get("password_hash", "")):
                    st.error("La contrasena actual no es correcta.")
                elif len(new_pw) < 8:
                    st.error("La nueva contrasena debe tener al menos 8 caracteres.")
                elif new_pw != new_pw2:
                    st.error("Las nuevas contrasenas no coinciden.")
                else:
                    updated = storage.update_user(
                        user["id"], {"password_hash": auth.hash_password(new_pw)}
                    )
                    public = auth.public_user(updated)
                    st.session_state.user = public
                    token, claims = session.issue_token(public)
                    st.session_state.session_token = token
                    st.session_state.session_claims = claims
                    if token:
                        session.queue_cookie(token)
                    st.success("Contrasena actualizada correctamente.")

        if st.button("Cerrar todas mis sesiones", icon=":material/lock:",
                     use_container_width=True, key="revoke_all_sessions"):
            storage.revoke_user_sessions(user["id"], user)
            session.revoke(st.session_state.get("session_token", ""))
            session.queue_cookie(clear=True)
            st.session_state.user = None
            st.session_state.session_token = ""
            st.session_state.session_notice = "Todas tus sesiones fueron cerradas."
            st.rerun()


def render_forced_password_change():
    """Pantalla obligatoria tras un restablecimiento del perfil maestro: el
    usuario elige una clave propia antes de usar la app. Al guardarla se borra
    la marca de clave temporal y esta sesion sigue abierta (las demas sesiones
    abiertas con la clave temporal se cierran, ver auth.validate_session)."""
    storage = st.session_state.storage
    user = st.session_state.user

    page_header(
        "Elige una contraseña nueva", icon=":material/lock_reset:",
        subtitle=f"{user.get('full_name') or 'Hola'}, un administrador restableció tu contraseña",
    )
    (column,) = centered_columns(1, 2)
    with column:
        with card("forced_password_change", tone="warning"):
            st.info(
                "Antes de continuar, elige una contraseña que solo tú conozcas. "
                "La contraseña temporal dejará de funcionar."
            )
            with st.form("forced_password_form", clear_on_submit=True):
                new_pw = st.text_input("Nueva contraseña", type="password")
                new_pw2 = st.text_input("Confirmar nueva contraseña", type="password")
                st.caption(f"Usa al menos {auth.MIN_PASSWORD_LENGTH} caracteres.")
                submitted = st.form_submit_button(
                    "Guardar y continuar", type="primary", use_container_width=True
                )
            if submitted:
                updated, changed_at, error = auth.complete_forced_password_change(
                    storage, user["id"], new_pw, new_pw2
                )
                if error:
                    st.error(error)
                else:
                    st.session_state.user = updated
                    st.session_state.login_at = changed_at
                    token, claims = session.issue_token(updated)
                    st.session_state.session_token = token
                    st.session_state.session_claims = claims
                    if token:
                        session.queue_cookie(token)
                    st.toast("Contraseña actualizada. ¡Bienvenido!", icon=":material/check_circle:")
                    st.rerun()
        if st.button("Cerrar sesión", icon=":material/logout:", key="forced_password_logout",
                     use_container_width=True):
            session.revoke(st.session_state.get("session_token", ""))
            session.queue_cookie(clear=True)
            st.session_state.user = None
            st.session_state.login_at = None
            st.session_state.session_token = ""
            st.rerun()
    footer()
