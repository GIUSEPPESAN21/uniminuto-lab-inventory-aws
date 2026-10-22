# -*- coding: utf-8 -*-
"""views/correos.py - Correos automaticos (solo perfil maestro, dentro de Reportes).

Muestra si el servidor SMTP esta configurado (sin revelar ningun valor de los
Secrets), que avisos estan activos, a quien llegan, los ultimos envios con su
estado y permite mandar un correo de prueba o el resumen de vencidos."""

import pandas as pd
import streamlit as st

from core import email_events, notifications, permissions
from core import loans as loans_core
from core.ui import empty_state, kv_list, section_title, stat_cards

_FLASH_KEY = "email_settings_flash"
_LOG_LIMIT = 20

# Ejemplo completo para Gmail: los valores son de muestra, nunca los reales.
GMAIL_EXAMPLE = """# Streamlit Cloud -> tu app -> Settings -> Secrets (o .streamlit/secrets.toml)
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USE_TLS = true
SMTP_USE_SSL = false
SMTP_USERNAME = "laboratorio.uniminuto@gmail.com"
# Contraseña de APLICACIÓN de 16 letras (no la clave normal de la cuenta)
SMTP_PASSWORD = "abcd efgh ijkl mnop"
SMTP_FROM_EMAIL = "laboratorio.uniminuto@gmail.com"

# Opcional: enlace a la app dentro de cada correo
APP_URL = "https://tu-app.streamlit.app"
# Opcional: más destinatarios además de los perfiles maestro
ADMIN_NOTIFICATION_EMAILS = ["coordinacion.laboratorio@uniminuto.edu.co"]
"""

SECRET_HELP = {
    "SMTP_HOST": "Servidor de salida (Gmail: smtp.gmail.com · Outlook / Office 365: smtp.office365.com)",
    "SMTP_PORT": "587 con STARTTLS (recomendado) o 465 con SSL",
    "SMTP_USERNAME": "Cuenta con la que se inicia sesión en el servidor",
    "SMTP_PASSWORD": "En Gmail, una contraseña de aplicación (no la clave normal)",
    "SMTP_FROM_EMAIL": "Remitente que ven los destinatarios (si falta, se usa SMTP_USERNAME)",
    "SMTP_USE_TLS": "true con el puerto 587",
    "SMTP_USE_SSL": "true solo con el puerto 465",
    "ADMIN_NOTIFICATION_EMAILS": "Opcional: correos extra que reciben los avisos",
    "APP_URL": "Opcional: dirección de la app para enlazarla en los correos",
}

_STATUS_TEXT = {email_events.STATUS_SENT: "Enviado", email_events.STATUS_FAILED: "Falló"}


def _flash(kind: str, message: str) -> None:
    st.session_state[_FLASH_KEY] = (kind, message)


def _show_flash() -> None:
    flash = st.session_state.pop(_FLASH_KEY, None)
    if flash:
        kind, message = flash
        (st.success if kind == "success" else st.warning)(message)


def render_email_settings(storage, user: dict) -> None:
    if not permissions.has_cap(user, permissions.CAP_MANAGE_EMAIL):
        st.info("Solo un administrador configura los correos automáticos.")
        return

    status = notifications.smtp_status()
    prefs = email_events.load_preferences(storage)
    sources = email_events.recipient_sources(storage, prefs)
    log = email_events.recent_log(storage, limit=_LOG_LIMIT)
    active = sum(1 for event in email_events.EVENTS if prefs["events"].get(event, True))
    failed = sum(1 for row in log if row.get("status") == email_events.STATUS_FAILED)

    section_title(
        "Correos automáticos", icon=":material/mail:",
        caption="Cada solicitud, reserva, salida y devolución avisa por correo a los perfiles maestro; "
                "las aprobaciones y rechazos le llegan a quien lo pidió. El envío ocurre en segundo plano.",
    )
    _show_flash()
    stat_cards([
        {"label": "Servidor de correo", "value": "Configurado" if status["configured"] else "Sin configurar",
         "icon": ":material/dns:", "tone": "success" if status["configured"] else "warning"},
        {"label": "Avisos activos", "value": f"{active} de {len(email_events.EVENTS)}",
         "icon": ":material/notifications:", "tone": "info"},
        {"label": "Destinatarios", "value": len(sources), "icon": ":material/group:",
         "tone": "info" if sources else "warning"},
        {"label": "Fallidos recientes", "value": failed, "icon": ":material/error:",
         "tone": "danger" if failed else "neutral"},
    ])

    _render_smtp_status(status)
    _render_preferences(storage, user, prefs)
    _render_recipients(sources, prefs)
    _render_actions(storage, user, status, prefs, sources)
    _render_log(log)


def _render_smtp_status(status: dict) -> None:
    section_title("Servidor de correo (SMTP)", icon=":material/dns:")
    if status["configured"]:
        st.success(f"El servidor de correo está configurado (conexión {status['security']}). "
                   "Por seguridad, aquí nunca se muestran los valores de los Secrets.")
    else:
        st.warning(
            "Los correos automáticos están apagados porque falta configurar el servidor de correo. "
            f"Agrega en los Secrets de la app: {', '.join(status['missing'])} "
            "(y, si tu servidor pide inicio de sesión, SMTP_USERNAME y SMTP_PASSWORD)."
        )
    for warning in status["warnings"]:
        st.warning(warning)

    present = status["present"]
    with st.expander(":material/key: Secrets del correo (solo si existen, sin valores)",
                     expanded=not status["configured"]):
        kv_list([
            (key, f"{'Definido' if present.get(key) else 'Falta'} · {help_text}")
            for key, help_text in SECRET_HELP.items()
        ])
    with st.expander(":material/help: Cómo configurarlo (ejemplo con Gmail)", expanded=not status["configured"]):
        st.markdown(
            "1. En la cuenta de Gmail del laboratorio activa la **verificación en 2 pasos** "
            "(Cuenta de Google → Seguridad).\n"
            "2. En **Contraseñas de aplicaciones** crea una para «Correo»: Google muestra 16 letras.\n"
            "3. Pega este bloque en **Streamlit Cloud → tu app → Settings → Secrets**, con tus datos, "
            "y guarda (la app se reinicia sola).\n"
            "4. Vuelve aquí y pulsa **Enviar correo de prueba**."
        )
        st.code(GMAIL_EXAMPLE, language="toml")
        st.caption("Con Outlook / Office 365 usa SMTP_HOST = \"smtp.office365.com\" y el puerto 587. "
                   "Nunca subas estos valores al repositorio.")


def _render_preferences(storage, user: dict, prefs: dict) -> None:
    section_title("Qué avisos se envían", icon=":material/notifications:",
                  caption="Todos vienen activos. Apaga los que no necesites.")
    with st.form("email_preferences_form"):
        values = {
            event: st.toggle(email_events.EVENT_LABELS[event], value=prefs["events"].get(event, True),
                             help=email_events.EVENT_HELP[event], key=f"email_event_{event}")
            for event in email_events.EVENTS
        }
        include_professors = st.toggle(
            "Enviar también a los supervisores activos", value=prefs["include_professors"],
            help="Además de los administradores y de ADMIN_NOTIFICATION_EMAILS.", key="email_include_professors",
        )
        if st.form_submit_button(":material/save: Guardar preferencias", type="primary", width="stretch"):
            try:
                email_events.save_preferences(storage, values, include_professors,
                                              actor_email=user.get("institutional_email", ""))
                _flash("success", "Preferencias de correo guardadas.")
            except Exception as exc:
                _flash("warning", f"No se pudieron guardar las preferencias: {exc}")
            st.rerun()


def _render_recipients(sources: list, prefs: dict) -> None:
    section_title("Quién recibe los avisos", icon=":material/group:",
                  caption="Administradores activos + ADMIN_NOTIFICATION_EMAILS"
                          + (" + supervisores activos" if prefs["include_professors"] else "")
                          + ", sin repetidos ni correos inválidos.")
    if not sources:
        empty_state("Nadie recibiría los avisos",
                    "Ningún administrador activo tiene un correo válido y ADMIN_NOTIFICATION_EMAILS está vacío.",
                    icon=":material/group_off:")
        return
    kv_list(sources)


def _render_actions(storage, user: dict, status: dict, prefs: dict, sources: list) -> None:
    section_title("Probar el envío", icon=":material/send:")
    configured = status["configured"]
    own = notifications.deliverable_email(user.get("institutional_email"))
    col_test, col_overdue = st.columns(2)
    with col_test:
        options = {f"Solo a mí ({own or 'sin correo válido'})": False,
                   f"A todos los destinatarios ({len(sources)})": True}
        choice = st.radio("Enviar la prueba a", list(options), key="email_test_target")
        if st.button(":material/send: Enviar correo de prueba", type="primary", width="stretch",
                     disabled=not configured, key="email_send_test",
                     help=None if configured else "Primero configura el servidor de correo."):
            with st.spinner("Enviando correo de prueba..."):
                ok, message = email_events.send_test_email(storage, user, to_all=options[choice])
            _flash("success" if ok else "warning", message)
            st.rerun()
    with col_overdue:
        try:
            overdue = len(loans_core.get_overdue_loans(storage))
        except Exception:
            overdue = 0
        enabled = prefs["events"].get(email_events.EVENT_LOANS_OVERDUE, True)
        st.caption(f"Préstamos vencidos ahora: **{overdue}**. El resumen llega a los {len(sources)} "
                   "destinatario(s) de los avisos.")
        if st.button(f":material/alarm: Enviar resumen de vencidos ({overdue})", width="stretch",
                     disabled=not (configured and enabled and overdue), key="email_send_overdue"):
            ok, message = email_events.notify_overdue_loans(storage, user)
            _flash("success" if ok else "warning", message)
            st.rerun()


def _render_log(log: list) -> None:
    section_title("Últimos envíos", icon=":material/history:",
                  caption=f"Los {_LOG_LIMIT} más recientes, con su resultado.")
    pending = email_events.pending_count()
    if pending:
        st.info(f"{pending} correo(s) enviándose en segundo plano.")
    if st.button(":material/refresh: Actualizar", key="email_log_refresh"):
        st.rerun()
    if not log:
        empty_state("Todavía no se ha enviado ningún correo",
                    "Aquí verás cada envío con su estado cuando el servidor de correo esté configurado.",
                    icon=":material/mail:")
        return
    rows = [{
        "Fecha": email_events.format_local(row.get("created_at")),
        "Evento": email_events.EVENT_LABELS.get(row.get("event_type"), row.get("event_type") or ""),
        "Asunto": row.get("subject") or "",
        "Destinatarios": row.get("recipient_count") or "0",
        "Estado": _STATUS_TEXT.get(row.get("status"), row.get("status") or ""),
        "Detalle": row.get("error") or "",
    } for row in log]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
