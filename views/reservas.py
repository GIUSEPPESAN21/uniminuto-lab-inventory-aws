# -*- coding: utf-8 -*-
"""Reservas de actividades o del laboratorio completo."""

from datetime import datetime, timedelta

import streamlit as st

from core import permissions, reservations
from core.ui import card, card_header, empty_state, page_header, persistent_tabs, section_title, stat_cards

_SCOPE_LABELS = {
    "Actividad específica": reservations.SCOPE_ACTIVITY,
    "Laboratorio completo": reservations.SCOPE_FULL_LAB,
}
# Presentacion de cada estado: (texto, tono de la pastilla, icono).
_STATUS_BADGES = {
    "pending": ("Pendiente", "warning", "⏳"), "approved": ("Aprobada", "success", "✅"),
    "rejected": ("Rechazada", "danger", "⛔"), "cancelled": ("Cancelada", "neutral", "⚪"),
}
_STATUS_CARD_TONE = {"pending": "warning", "approved": "success", "rejected": "danger"}


def _local(value) -> str:
    if not value:
        return "Sin fecha"
    return reservations.as_bogota(value).strftime("%d/%m/%Y %I:%M %p")


def _status_badge(status) -> dict:
    text, tone, icon = _STATUS_BADGES.get(status, (status or "Sin estado", "neutral", None))
    return {"text": text, "tone": tone, "icon": icon}


def _reservation_card(storage, row: dict, user: dict, allow_cancel=True, key_prefix: str = "res") -> None:
    status = row.get("status")
    with card(f"{key_prefix}_{row['id']}", tone=_STATUS_CARD_TONE.get(status)):
        scope_icon = "🏫" if row.get("scope_type") == reservations.SCOPE_FULL_LAB else "🧪"
        card_header(
            row.get("activity"),
            subtitle=f"{_local(row.get('start_at'))} → {_local(row.get('end_at'))}",
            icon=scope_icon,
            badges=[_status_badge(status)],
        )
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.caption(f":material/group: {row.get('attendees')} asistente(s) · Solicita: {row.get('requester_name')}")
        c1.write(row.get("purpose") or "Sin propósito")
        if row.get("review_notes"):
            c1.caption(f"Respuesta: {row['review_notes']}")
        if allow_cancel and status in ("pending", "approved"):
            if c2.button("Cancelar", key=f"cancel_{key_prefix}_{row['id']}", use_container_width=True):
                ok, message = reservations.cancel_reservation(storage, row["id"], user)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()


def render_new(storage, user: dict) -> None:
    """Formulario de una reserva nueva (Solicitudes lo muestra como un tipo más)."""
    scope_label = st.radio("Alcance", list(_SCOPE_LABELS), horizontal=True, key="reservation_scope")
    scope_type = _SCOPE_LABELS[scope_label]
    now = datetime.now(reservations.BOGOTA_TZ)
    default_start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    default_end = default_start + timedelta(hours=2)

    with st.form("reservation_form", clear_on_submit=False):
        activity = ""
        if scope_type == reservations.SCOPE_ACTIVITY:
            activity = st.text_input("Actividad", placeholder="Ej: Práctica de robótica")
        purpose = st.text_area("Propósito", placeholder="Curso, grupo y objetivo de la actividad")
        attendees = st.number_input("Número de asistentes", min_value=1, max_value=500, value=10, step=1)
        c1, c2 = st.columns(2)
        start_date = c1.date_input("Fecha de inicio", value=default_start.date())
        start_time = c2.time_input("Hora de inicio", value=default_start.time().replace(tzinfo=None))
        c3, c4 = st.columns(2)
        end_date = c3.date_input("Fecha de finalización", value=default_end.date())
        end_time = c4.time_input("Hora de finalización", value=default_end.time().replace(tzinfo=None))
        submitted = st.form_submit_button("Enviar solicitud de reserva", type="primary", use_container_width=True)

        if submitted:
            start_at = datetime.combine(start_date, start_time, tzinfo=reservations.BOGOTA_TZ)
            end_at = datetime.combine(end_date, end_time, tzinfo=reservations.BOGOTA_TZ)
            try:
                row, notified, message = reservations.submit_reservation(
                    storage, user, scope_type, activity, purpose, attendees, start_at, end_at
                )
                st.success(f"Reserva #{row['id']} registrada y pendiente de aprobación.")
                if notified:
                    st.info(message)
                else:
                    st.warning(message)
            except ValueError as exc:
                st.error(str(exc))


def render_mine(storage, user: dict) -> None:
    mine = storage.get_reservations(user_id=user["id"])
    if not mine:
        empty_state("Todavía no tienes reservas.", "Solicita una en la pestaña Nueva reserva.", icon=":material/calendar_month:")
    for row in mine:
        _reservation_card(storage, row, user, key_prefix="mine")


def render_manage(storage, user: dict) -> None:
    """Reservas por revisar y próximas aprobadas (quien tiene ``manage_reservations``)."""
    pending = storage.get_reservations(status=reservations.STATUS_PENDING)
    approved = storage.get_reservations(status=reservations.STATUS_APPROVED)
    stat_cards([
        {"label": "Pendientes", "value": len(pending), "icon": "⏳",
         "tone": "warning" if pending else "success"},
        {"label": "Aprobadas", "value": len(approved), "icon": "✅", "tone": "info"},
    ])
    section_title("Por revisar", icon=":material/edit_note:")
    if not pending:
        empty_state("No hay reservas pendientes.", icon=":material/check_circle:")
    for row in pending:
        with st.expander(
            f":material/hourglass_top: {row.get('activity')} · {_local(row.get('start_at'))} · {row.get('requester_name')}",
            expanded=False,
        ):
            st.write(row.get("purpose"))
            st.caption(
                f"Alcance: {row.get('scope_type')} · {row.get('attendees')} asistentes · "
                f"Fin: {_local(row.get('end_at'))}"
            )
            with st.form(f"review_res_{row['id']}"):
                notes = st.text_input("Observación para el solicitante")
                c1, c2 = st.columns(2)
                approve = c1.form_submit_button(":material/check_circle: Aprobar", type="primary", use_container_width=True)
                reject = c2.form_submit_button(":material/block: Rechazar", use_container_width=True)
                if approve or reject:
                    decision = reservations.STATUS_APPROVED if approve else reservations.STATUS_REJECTED
                    ok, message = reservations.review_reservation(
                        storage, row["id"], decision, user, notes
                    )
                    (st.success if ok else st.error)(message)
                    if ok:
                        st.rerun()

    section_title("Próximas reservas aprobadas", icon=":material/event:")
    if not approved:
        st.caption("No hay reservas aprobadas.")
    for row in approved:
        _reservation_card(storage, row, user, allow_cancel=True, key_prefix="approved")


def render():
    """Página completa de Reservas (la app la muestra dentro de Solicitudes)."""
    storage = st.session_state.storage
    user = st.session_state.user
    reviewer = permissions.has_cap(user, permissions.CAP_MANAGE_RESERVATIONS)

    page_header(
        "Reservas", icon=":material/calendar_month:",
        subtitle="Solicita una actividad o el laboratorio completo y evita cruces de horario",
    )
    titles = [":material/add: Nueva reserva", ":material/event: Mis reservas"]
    if reviewer:
        titles.append(":material/fact_check: Gestionar")
    tabs = persistent_tabs(titles, "reservas")
    with tabs[0]:
        render_new(storage, user)
    with tabs[1]:
        render_mine(storage, user)
    if reviewer:
        with tabs[2]:
            render_manage(storage, user)
