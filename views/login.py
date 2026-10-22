# -*- coding: utf-8 -*-
"""views/login.py - Pantalla de inicio de sesion y registro."""

from datetime import datetime, timezone

import streamlit as st

from core import auth, session
from core.ui import esc, footer, icon_html, logo_data_uri

PENDING_KEY = "pending_registration"


def _show_created_account(user: dict) -> None:
    st.success(f"¡Cuenta creada como **{user['role']}**! Ya puedes iniciar sesion en la pestana anterior.")


def _start_professor_verification(storage, prepared: dict) -> None:
    """El correo esta en la lista blanca de profesores: antes de darle ese rol hay
    que comprobar que el solicitante es dueño del correo (codigo por email)."""
    code, challenge = auth.new_verification_challenge(prepared["email"])
    sent, _message = auth.send_verification_email(prepared["email"], code)
    if sent:
        st.session_state[PENDING_KEY] = {"prepared": prepared, "challenge": challenge}
        st.rerun()
        return

    # Sin correo configurado no hay forma de verificar: nunca se otorga el rol
    # sin verificar; la cuenta nace estudiante y el maestro puede ascenderla.
    user, error = auth.create_account(storage, prepared, email_verified=False)
    if error:
        st.error(error)
        return
    _show_created_account(user)
    st.info(
        "Tu correo esta en la lista de supervisores, pero no pudimos enviarte el codigo de "
        "verificacion. Tu cuenta se creo como **cliente**: pide a un administrador que "
        "active tu rol de supervisor."
    )


def _render_verification_step(storage) -> None:
    pending = st.session_state[PENDING_KEY]
    prepared = pending["prepared"]
    st.info(
        f"Enviamos un codigo de 6 digitos a **{prepared['email']}**. Escribelo para verificar "
        "tu correo y activar el rol de supervisor. Vence en 15 minutos."
    )
    with st.form("verify_registration_form"):
        code = st.text_input("Código de verificación", max_chars=6, placeholder="123456")
        c1, c2, c3 = st.columns(3)
        verify = c1.form_submit_button("Verificar y crear cuenta", type="primary", use_container_width=True)
        resend = c2.form_submit_button("Reenviar código", use_container_width=True)
        cancel = c3.form_submit_button("Cancelar", use_container_width=True)

    if cancel:
        st.session_state.pop(PENDING_KEY, None)
        st.rerun()
    elif resend:
        new_code, challenge = auth.new_verification_challenge(prepared["email"])
        sent, message = auth.send_verification_email(prepared["email"], new_code)
        if sent:
            pending["challenge"] = challenge
            st.success("Enviamos un codigo nuevo.")
        else:
            st.error(message)
    elif verify:
        ok, message, challenge = auth.check_verification_code(pending["challenge"], code)
        if ok:
            user, error = auth.create_account(storage, prepared, email_verified=True)
            st.session_state.pop(PENDING_KEY, None)
            if error:
                st.error(error)
            else:
                _show_created_account(user)
        elif challenge is None:
            st.session_state.pop(PENDING_KEY, None)
            st.error(message)
        else:
            pending["challenge"] = challenge
            st.error(message)


def _render_registration_form(storage) -> None:
    st.caption("Solo se aceptan correos institucionales (terminados en **.edu** o **.edu.co**).")
    st.caption("Todos los campos son obligatorios.")
    with st.form("register_form"):
        full_name = st.text_input("Nombre completo *", placeholder="Nombre y apellido")
        student_id = st.text_input("ID Estudiante *", placeholder="Ej: TI2024001")
        email = st.text_input(
            "Correo institucional *", key="reg_email", placeholder="nombre@tuinstitucion.edu.co"
        )
        program = st.text_input("Programa académico o departamento *")
        password = st.text_input("Contraseña *", type="password", key="reg_pw")
        password2 = st.text_input("Confirmar contraseña *", type="password", key="reg_pw2")
        submitted = st.form_submit_button("Crear cuenta", type="primary", use_container_width=True)

        if submitted:
            if password != password2:
                st.error("Las contrasenas no coinciden.")
                return
            prepared, error = auth.validate_registration(
                storage, full_name, email, password, program, student_id
            )
            if error:
                st.error(error)
            elif auth.needs_professor_verification(storage, prepared["email"]):
                _start_professor_verification(storage, prepared)
            else:
                user, error = auth.create_account(storage, prepared)
                if error:
                    st.error(error)
                else:
                    _show_created_account(user)


def _brand_panel() -> None:
    """Panel de marca (izquierda en escritorio, arriba en celular)."""
    logo = logo_data_uri()
    logo_html = (
        f'<div class="lab-auth-brand__logo"><img src="{logo}" alt="Logotipo de UNIMINUTO"></div>' if logo else ""
    )
    features = "".join(
        f'<li><span aria-hidden="true">{icon_html(icon)}</span>{esc(text)}</li>'
        for icon, text in (
            ("🛰️", "Escanea códigos y registra salidas en segundos"),
            ("🗓️", "Reserva actividades o el laboratorio completo"),
            ("🧭", "Sigue cada solicitud y préstamo de principio a fin"),
        )
    )
    st.markdown(
        f'<section class="lab-auth-brand">{logo_html}'
        '<div class="lab-auth-brand__eyebrow">UNIMINUTO · Laboratorio de Ingeniería</div>'
        '<div class="lab-auth-brand__title" role="heading" aria-level="1">Inventario de Laboratorio</div>'
        '<div class="lab-auth-brand__text">Gestión de inventario y préstamos para laboratorios de ingeniería.</div>'
        f'<ul class="lab-auth-brand__list">{features}</ul></section>',
        unsafe_allow_html=True,
    )


def render():
    storage = st.session_state.storage

    with st.container(key="lab_login"):
        col_brand, col_form = st.columns([1.05, 1], gap="large", vertical_alignment="center")
        with col_brand:
            _brand_panel()

        with col_form:
            with st.container(key="lab_auth_card"):
                st.markdown(
                    '<div class="lab-auth-head"><div class="lab-auth-head__title" role="heading" aria-level="2">'
                    "Accede a tu cuenta</div>"
                    '<div class="lab-auth-head__text">Usa tu correo institucional para ingresar o registrarte.</div>'
                    "</div>",
                    unsafe_allow_html=True,
                )

                notice = st.session_state.pop("session_notice", None)
                if notice:
                    st.warning(notice)

                tab_login, tab_register = st.tabs([":material/login: Iniciar sesión", ":material/person_add: Registrarme"])

                with tab_login:
                    with st.form("login_form"):
                        email = st.text_input("Correo institucional", placeholder="nombre@uniminuto.edu.co")
                        password = st.text_input("Contraseña", type="password")
                        submitted = st.form_submit_button("Ingresar", type="primary", use_container_width=True)

                        if submitted:
                            user, error = auth.login_user(storage, email, password)
                            if error:
                                st.error(error)
                            else:
                                st.session_state.user = user
                                st.session_state.login_at = datetime.now(timezone.utc)
                                token, claims = session.issue_token(user)
                                st.session_state.session_token = token
                                st.session_state.session_claims = claims
                                if token:
                                    session.queue_cookie(token)
                                session.audit(storage, "session_login", user)
                                st.rerun()

                with tab_register:
                    if st.session_state.get(PENDING_KEY):
                        _render_verification_step(storage)
                    else:
                        _render_registration_form(storage)

    footer()
