# -*- coding: utf-8 -*-
"""Gestión de usuarios y lista blanca, exclusiva del rol maestro."""

import streamlit as st

from core import auth, permissions
from core.ui import (
    ROLE_LABELS, badge_html, card, card_header_html, empty_state, guard_cap, initials, page_header,
    flash, more_button, paginate, persistent_tabs, section_title, show_flash, stat_cards,
)

ROLES = list(permissions.ROLES)
_ROLE_TONES = {
    "estudiante": "success", "operario": "neutral", "encargado": "info",
    "profesor": "info", "maestro": "warning",
}

# Restablecer contraseña (core/auth.admin_reset_password): modos del selector.
_RESET_GENERATE = "generate"
_RESET_MANUAL = "manual"
_RESET_MODES = {
    _RESET_GENERATE: "Generar una contraseña temporal",
    _RESET_MANUAL: "Escribirla yo",
}


def _show_reset_result(user: dict, result: dict) -> None:
    """Resultado del restablecimiento. La clave generada se muestra UNA sola vez:
    la vista la saca de la sesion al pintarla y no vuelve a aparecer."""
    name = user.get("full_name") or "el usuario"
    st.success(f"Contraseña de {name} restablecida. Deberá elegir una nueva al ingresar.")
    if result.get("temporary_password"):
        st.code(result["temporary_password"], language=None)
        st.caption(
            ":material/content_copy: Cópiala con el botón del recuadro: solo se muestra esta vez. "
            "Entrégala en persona (no por chat ni en público); al ingresar, el usuario deberá cambiarla."
        )
    if result.get("email_requested"):
        if result.get("email_sent"):
            st.info("También se envió al correo institucional del usuario.")
        else:
            st.warning("No se pudo enviar el correo: entrega la contraseña en persona.")


def _password_reset_panel(storage, user: dict, current_user: dict) -> None:
    """Expander "Restablecer contraseña" de la tarjeta de un estudiante o profesor.

    Dos pasos: elegir como se define la clave y luego confirmar. Las keys llevan
    un contador que cambia tras cada restablecimiento para dejar el formulario limpio."""
    uid = user["id"]
    result = st.session_state.pop(f"pwreset_result_{uid}", None)
    if result:
        _show_reset_result(user, result)

    nonce = st.session_state.get(f"pwreset_nonce_{uid}", 0)
    confirm_key = f"pwreset_confirm_{uid}"
    with st.expander(":material/key: Restablecer contraseña", expanded=False):
        st.caption(
            "Para quien olvidó su contraseña. Sus sesiones abiertas se cerrarán y, al ingresar "
            "con la nueva, la app le pedirá elegir una propia."
        )
        mode = st.radio(
            "Nueva contraseña", list(_RESET_MODES), format_func=_RESET_MODES.get, horizontal=True,
            key=f"pwreset_mode_{uid}_{nonce}",
        )
        new_pw = confirm_pw = None
        if mode == _RESET_MANUAL:
            new_pw = st.text_input("Contraseña nueva", type="password", key=f"pwreset_pw_{uid}_{nonce}")
            confirm_pw = st.text_input(
                "Confirmar contraseña nueva", type="password", key=f"pwreset_pw2_{uid}_{nonce}"
            )
            st.caption(f"Al menos {auth.MIN_PASSWORD_LENGTH} caracteres.")
        send_email = False
        if auth.email_delivery_configured():
            send_email = st.checkbox(
                "Enviar la contraseña temporal al correo del usuario", value=False,
                key=f"pwreset_mail_{uid}_{nonce}",
            )

        if st.button("Restablecer contraseña", icon=":material/lock_reset:", key=f"pwreset_start_{uid}_{nonce}"):
            error = auth.validate_new_password(new_pw, confirm_pw) if mode == _RESET_MANUAL else None
            if error:
                st.session_state.pop(confirm_key, None)
                st.error(error)
            else:
                st.session_state[confirm_key] = True

        if st.session_state.get(confirm_key):
            st.warning(
                f"¿Restablecer la contraseña de {user.get('full_name') or 'este usuario'}? "
                "La actual dejará de funcionar y sus sesiones abiertas se cerrarán."
            )
            c_yes, c_no = st.columns(2)
            if c_yes.button("Sí, restablecer", type="primary", key=f"pwreset_yes_{uid}_{nonce}",
                            use_container_width=True):
                error = auth.validate_new_password(new_pw, confirm_pw) if mode == _RESET_MANUAL else None
                outcome = None
                if not error:
                    outcome, error = auth.admin_reset_password(
                        storage, current_user, uid,
                        new_password=new_pw if mode == _RESET_MANUAL else None, send_email=send_email,
                    )
                if error:
                    st.error(error)
                else:
                    st.session_state.pop(confirm_key, None)
                    st.session_state[f"pwreset_nonce_{uid}"] = nonce + 1
                    st.session_state[f"pwreset_result_{uid}"] = {
                        "temporary_password": outcome.get("temporary_password"),
                        "email_requested": outcome.get("email_requested"),
                        "email_sent": outcome.get("email_sent"),
                    }
                    st.rerun()
            if c_no.button("Cancelar", key=f"pwreset_no_{uid}_{nonce}", use_container_width=True):
                st.session_state.pop(confirm_key, None)
                st.rerun()


def _user_card(storage, user: dict, current_user: dict) -> None:
    is_active = user.get("status") == "active"
    with card(f"user_{user['id']}", tone=None if is_active else "danger"):
        role = user.get("role", "estudiante")
        badges = [{"text": ROLE_LABELS.get(role, role), "tone": _ROLE_TONES.get(role, "neutral")}]
        badges.append({"text": "Activo", "tone": "success", "icon": "●"} if is_active
                      else {"text": "Deshabilitado", "tone": "danger", "icon": "●"})
        if auth.must_change_password(user):
            badges.append({"text": "Clave temporal", "tone": "warning", "icon": ":material/key:"})
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2], vertical_alignment="center")
        c1.markdown(
            card_header_html(
                user.get("full_name"),
                subtitle=f"{user.get('institutional_email')} · ID: {user.get('student_id') or 'N/A'}",
                icon=initials(user.get("full_name")),
                badges=badges,
            ),
            unsafe_allow_html=True,
        )
        c2.caption(f"Programa: {user.get('program_or_department') or 'N/A'}")

        new_role = c3.selectbox(
            "Rol", ROLES, index=ROLES.index(user.get("role", permissions.ROLE_STUDENT)),
            key=f"role_{user['id']}", label_visibility="collapsed",
            format_func=permissions.role_detail_label,
        )
        if new_role != user.get("role"):
            if user["id"] == current_user["id"]:
                c3.error("No puedes cambiar tu propio rol.")
            elif c3.button("Guardar rol", key=f"save_role_{user['id']}"):
                storage.update_user(user["id"], {"role": new_role}, actor=current_user)
                st.success(
                    f"Rol de {user['full_name']} actualizado a {permissions.role_detail_label(new_role)}."
                )
                st.rerun()

        toggle_label = ":material/block: Deshabilitar" if is_active else ":material/check_circle: Habilitar"
        if user["id"] == current_user["id"]:
            # Nadie se deshabilita a si mismo: en su lugar, una pastilla que
            # conserva la simetria de la fila.
            c4.markdown(f'<div style="text-align:center">{badge_html("Tu cuenta", "info", "👤")}</div>',
                        unsafe_allow_html=True)
        elif c4.button(toggle_label, key=f"toggle_{user['id']}", use_container_width=True):
            storage.update_user(
                user["id"], {"status": "disabled" if is_active else "active"},
                actor=current_user,
            )
            st.rerun()

        with st.expander(":material/edit: Corregir datos del usuario", expanded=False):
            with st.form(f"edit_user_{user['id']}"):
                full_name = st.text_input("Nombre completo", value=user.get("full_name") or "")
                student_id = st.text_input("ID de estudiante", value=user.get("student_id") or "")
                email = st.text_input(
                    "Correo institucional", value=user.get("institutional_email") or ""
                )
                program = st.text_input(
                    "Programa o departamento", value=user.get("program_or_department") or ""
                )
                submitted = st.form_submit_button(
                    "Guardar correcciones", type="primary", use_container_width=True
                )
                if submitted:
                    changes, error = auth.validate_profile_update(
                        storage, user["id"], full_name, email, program, student_id
                    )
                    if error:
                        st.error(error)
                    else:
                        try:
                            updated = storage.update_user(user["id"], changes, actor=current_user)
                            if user["id"] == current_user["id"] and updated:
                                st.session_state.user = auth.public_user({**current_user, **updated})
                            st.success("Datos del usuario actualizados.")
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))

        # Solo estudiantes y profesores ajenos: la propia clave se cambia en Mi
        # perfil y la de otro maestro no se restablece desde aqui.
        if auth.can_reset_password(current_user, user):
            _password_reset_panel(storage, user, current_user)


def render():
    storage = st.session_state.storage
    current_user = st.session_state.user

    if not guard_cap(current_user, permissions.CAP_MANAGE_USERS, "la gestión de usuarios"):
        return

    page_header(
        "Usuarios", icon=":material/group:",
        subtitle="Corrección de datos, roles, estados y lista blanca de supervisores",
    )
    show_flash("usuarios")
    tab_usuarios, tab_whitelist, tab_data = persistent_tabs(
        [":material/group: Usuarios", ":material/check_circle: Lista blanca de supervisores",
         ":material/database: Datos"], "usuarios")

    with tab_usuarios:
        st.caption(
            "Puedes corregir nombre, ID, correo y programa, además de activar cuentas o cambiar roles. "
            "Si un cliente o supervisor olvidó su contraseña, restablécela desde su tarjeta. "
            "El registro solo otorga el rol de supervisor (profesor) si el correo ya está en la lista blanca."
        )
        users = storage.get_all_users()
        stat_cards([
            {"label": "Usuarios", "value": len(users), "icon": "👥", "tone": "info"},
            {"label": "Profesores", "value": len([u for u in users if u.get("role") == "profesor"]),
             "icon": "👨‍🏫", "tone": "neutral"},
            {"label": "Deshabilitados", "value": len([u for u in users if u.get("status") != "active"]),
             "icon": "🚫", "tone": "neutral"},
        ])
        search = st.text_input(":material/search: Buscar por nombre, correo o ID")
        if search:
            value = search.lower()
            users = [
                user for user in users
                if value in (user.get("full_name") or "").lower()
                or value in (user.get("institutional_email") or "").lower()
                or value in (user.get("student_id") or "").lower()
            ]
        section_title(f"{len(users)} usuario(s)", icon=":material/contacts:")
        for user in paginate(users, "users_list", 30):
            _user_card(storage, user, current_user)
        more_button(len(users), "users_list", 30)

    with tab_whitelist:
        st.caption(
            "Los correos en esta lista obtendrán automáticamente el rol de supervisor (profesor) al registrarse. "
            "Cualquier otro correo institucional se registra como cliente."
        )
        with st.form("add_whitelist_form", clear_on_submit=True):
            email = st.text_input("Correo institucional del supervisor")
            if st.form_submit_button(":material/add: Agregar a la lista blanca", type="primary"):
                if not auth.is_institutional_email(email):
                    st.error("Ingresa un correo institucional válido (.edu o .edu.co).")
                else:
                    storage.add_to_whitelist(email, actor=current_user)
                    st.success(f"'{email}' agregado a la lista blanca.")
                    st.rerun()

        emails = storage.get_whitelist()
        section_title("Correos autorizados", icon=":material/check_circle:", caption=f"{len(emails)} correo(s) en la lista")
        if not emails:
            empty_state("La lista blanca está vacía.", icon=":material/inbox:")
        for email in emails:
            with card(f"wl_{email}"):
                c1, c2 = st.columns([4, 1], vertical_alignment="center")
                c1.markdown(card_header_html(email, icon=":material/mail:"), unsafe_allow_html=True)
                if c2.button("Quitar", key=f"remove_wl_{email}", use_container_width=True):
                    storage.remove_from_whitelist(email, actor=current_user)
                    st.rerun()

    with tab_data:
        _render_data_tab(storage, current_user)


_CONFIRM_WORD = "BORRAR"
_SLOW_WRITE_MS = 2000
_BIG_WORKBOOK_MB = 15


def _render_health(storage) -> None:
    """Tamaño y velocidad de la base, con aviso cuando conviene limpiar o archivar."""
    health = storage.get_health()
    write = health["write"]
    sync = health["sync"]
    section_title("Estado de la base", icon=":material/monitor_heart:")
    stat_cards([
        {"label": "Tamaño del Excel", "value": f"{health['workbook_mb']} MB", "icon": "📦",
         "tone": "warning" if health["workbook_mb"] > _BIG_WORKBOOK_MB else "success"},
        {"label": "Celdas", "value": f"{health['cells']:,}".replace(",", "."), "icon": "🧮", "tone": "neutral"},
        {"label": "Última escritura", "value": "—" if write["last_ms"] is None else f"{write['last_ms']} ms",
         "icon": "⏱️", "tone": "warning" if (write["p95_ms"] or 0) > _SLOW_WRITE_MS else "success"},
        {"label": "Sincronización", "value": "Pendiente" if sync.get("pending") else (
            "Al día" if sync.get("ok") is not False else "Con error"), "icon": "☁️",
         "tone": "success" if sync.get("ok") is not False and not sync.get("pending") else "warning"},
    ])
    if health["workbook_mb"] > _BIG_WORKBOOK_MB or (write["p95_ms"] or 0) > _SLOW_WRITE_MS:
        st.warning("La base ya es grande o lenta de guardar. Borra lo que no uses (abajo) o pasa a `SYNC_MODE = \"deferred\"` "
                   "en los Secrets; si sigue creciendo, conviene migrar a una base de datos.")


def _render_data_tab(storage, user: dict) -> None:
    """Dejar la base limpia para empezar (solicitudes, reservas, correos…) sin tocar personas ni productos."""
    if not permissions.has_cap(user, permissions.CAP_PURGE_DATA):
        st.info("Solo un administrador puede borrar datos operativos.")
        return
    section_title("Empezar de cero", icon=":material/cleaning_services:",
                  caption="Borra lo operativo de prueba. No toca personas, productos, ubicaciones, fotos ni ajustes, "
                          "y los préstamos abiertos se conservan porque de ellos depende la disponibilidad.")
    _render_health(storage)
    counts = storage.operational_data_counts()
    stat_cards([
        {"label": "Solicitudes", "value": counts["solicitudes"], "icon": "📝", "tone": "neutral"},
        {"label": "Reservas", "value": counts["reservas"], "icon": "🗓️", "tone": "neutral"},
        {"label": "Eventos de pedidos", "value": counts["trazabilidad"], "icon": "🧭", "tone": "neutral"},
        {"label": "Préstamos abiertos", "value": counts["prestamos_abiertos"], "icon": "🤝", "tone": "info"},
    ])
    with st.form("purge_form"):
        requests = st.checkbox(f"Solicitudes y su trazabilidad ({counts['solicitudes']} solicitudes, "
                               f"{counts['trazabilidad']} eventos)", value=True)
        reservations = st.checkbox(f"Reservas ({counts['reservas']})", value=True)
        mail_log = st.checkbox(f"Registro de correos enviados ({counts['correos']})", value=True)
        noise = st.checkbox(f"Inicios de sesión y toques de chip NFC antiguos ({counts['sesiones_nfc']})", value=True,
                            help="Son los eventos que más hacen crecer el Excel.")
        closed = st.checkbox(f"Préstamos ya devueltos ({counts['prestamos_cerrados']})", value=False,
                             help="Pierdes el historial de quién devolvió qué. Los préstamos abiertos nunca se borran.")
        word = st.text_input(f"Para confirmar escribe {_CONFIRM_WORD}", placeholder=_CONFIRM_WORD)
        st.caption("Antes de borrar se guarda una copia local (.bak) y GitHub conserva la versión anterior en su historial.")
        if st.form_submit_button(":material/delete_sweep: Borrar datos seleccionados", type="primary",
                                 use_container_width=True):
            if word.strip() != _CONFIRM_WORD:
                st.error(f"Escribe {_CONFIRM_WORD} para confirmar.")
            elif not any((requests, reservations, mail_log, noise, closed)):
                st.warning("Elige qué borrar.")
            else:
                removed = storage.purge_operational_data(
                    user, requests=requests, reservations=reservations, mail_log=mail_log,
                    session_noise=noise, closed_loans=closed)
                summary = ", ".join(f"{label}: {count}" for label, count in removed.items()) or "nada que borrar"
                flash(f"Listo. Filas borradas · {summary}.", key="usuarios")
                st.rerun()
