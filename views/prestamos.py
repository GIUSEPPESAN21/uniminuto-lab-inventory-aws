# -*- coding: utf-8 -*-
"""views/prestamos.py - Panel de prestamos: 'Mis prestamos' para quien solo ve lo
suyo (cliente y encargado), 'Quien tiene que' completo para quien tiene la
capacidad `view_all_loans` (profesor y administrador)."""

import streamlit as st

from core import loans as loans_core, permissions, returns
from core.ui import (
    card, card_header, empty_state, guard_cap, more_button, page_header, paginate, persistent_tabs, section_title,
    stat_cards,
)


def _when(value, fmt: str = "%d/%m/%Y %H:%M") -> str:
    return value.strftime(fmt) if value else "N/A"


def _loan_row(storage, loan: dict, user: dict, key_prefix: str):
    overdue = loans_core.is_overdue(loan)
    with card(f"loan_{key_prefix}_{loan['id']}", tone="danger" if overdue else "info"):
        status = (
            {"text": "Vencido", "tone": "danger", "icon": "⏰"} if overdue
            else {"text": "Activo", "tone": "info", "icon": "📤"}
        )
        card_header(
            f"{loan['item_name']} ×{loan['quantity']}",
            subtitle=f"Prestado a: {loan['user_name']} ({loan['user_role']})",
            icon=":material/inventory_2:",
            badges=[status],
        )
        c1, c3 = st.columns([3, 1], vertical_alignment="center")
        details = [f"Salida: {_when(loan.get('checkout_at'))}"]
        expected = loan.get("expected_return_at")
        if expected:
            details.append(f"Devolver antes de: {_when(expected, '%d/%m/%Y')}")
        c1.caption(" · ".join(details))
        if loan.get("notes"):
            c1.caption(f"Notas: {loan['notes']}")
        if loan.get("operator_name"):
            c1.caption(f"Despachado por: {loan['operator_name']}")
        if loans_core.can_checkin_loan(user, loan):
            if c3.button("↩️ Reingresar", key=f"{key_prefix}_{loan['id']}", use_container_width=True,
                         type="primary" if overdue else "secondary"):
                try:
                    ok, msg = loans_core.checkin(storage, loan["id"], user)
                except PermissionError as exc:
                    ok, msg = False, str(exc)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
        elif returns.can_request_return(user, loan):
            _return_controls(storage, loan, user, c1, c3, key_prefix, overdue)


def _return_controls(storage, loan: dict, user: dict, info_col, button_col, key_prefix: str, overdue: bool):
    """Devolución del cliente: pide, un supervisor verifica y un operario guarda."""
    stage = returns.stage(storage, loan["id"])
    if stage == returns.STAGE_NONE:
        if button_col.button("↩️ Devolver", key=f"{key_prefix}_ret_{loan['id']}", use_container_width=True,
                             type="primary" if overdue else "secondary"):
            try:
                ok, msg = returns.request_return(storage, loan["id"], user)
            except PermissionError as exc:
                ok, msg = False, str(exc)
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()
        return
    info_col.info(returns.STAGE_LABELS[stage], icon=":material/hourglass_top:")
    if stage == returns.STAGE_VERIFIED:
        proof = returns.last_details(storage, loan["id"], "return_verified")
        condition = returns.CONDITIONS.get(str(proof.get("condition") or ""), "")
        info_col.caption(f"Recibido: {proof.get('received', '?')} de {loan['quantity']} · {condition}")


def render():
    storage = st.session_state.storage
    user = st.session_state.user

    if not guard_cap(user, permissions.CAP_CHECKOUT, "Préstamos"):
        return
    page_header("Préstamos", icon=":material/assignment:", subtitle="Salidas y reingresos del laboratorio")

    if not permissions.has_cap(user, permissions.CAP_VIEW_ALL_LOANS):
        loans = storage.get_open_loans_for_user(user["id"])
        overdue_count = len([l for l in loans if loans_core.is_overdue(l)])
        stat_cards([
            {"label": "Mis préstamos activos", "value": len(loans), "icon": "📋", "tone": "info"},
            {"label": "Vencidos", "value": overdue_count, "icon": "⏰",
             "tone": "danger" if overdue_count else "success"},
        ])
        section_title("Mis préstamos", icon=":material/assignment:")
        if not loans:
            empty_state("No tienes préstamos activos.", "Cuando registres una salida desde Buscar aparecerá aquí.",
                        icon=":material/inbox:")
        for loan in paginate(loans, "loans_mine"):
            _loan_row(storage, loan, user, "mine")
        more_button(len(loans), "loans_mine")
        return

    tab_activos, tab_historial = persistent_tabs(
        [":material/assignment: Préstamos activos", ":material/history: Historial completo"], "prestamos")

    with tab_activos:
        loans = storage.get_all_loans(status="out")
        overdue_count = len([l for l in loans if loans_core.is_overdue(l)])
        stat_cards([
            {"label": "Préstamos activos", "value": len(loans), "icon": "📋", "tone": "info"},
            {"label": "Vencidos", "value": overdue_count, "icon": "⏰",
             "tone": "danger" if overdue_count else "success"},
        ])
        if not loans:
            empty_state("No hay préstamos activos.", "Todo el material está en el laboratorio.", icon=":material/check_circle:")
        for loan in paginate(loans, "loans_active"):
            _loan_row(storage, loan, user, "active")
        more_button(len(loans), "loans_active")

    with tab_historial:
        all_loans = storage.get_all_loans()
        returned = [l for l in all_loans if l.get("status") == "returned"]
        if not returned:
            empty_state("Aún no hay préstamos devueltos en el historial.", icon=":material/folder_open:")
        for loan in paginate(returned, "loans_hist"):
            with card(f"loan_hist_{loan['id']}"):
                card_header(
                    f"{loan['item_name']} ×{loan['quantity']}",
                    subtitle=f"{loan['user_name']} ({loan['user_role']})",
                    icon=":material/inventory_2:",
                    badges=[{"text": "Devuelto", "tone": "success", "icon": "✅"}],
                )
                st.caption(
                    f"Salida: {_when(loan.get('checkout_at'))} · "
                    f"Reingreso: {_when(loan.get('return_at'))}"
                )
        more_button(len(returned), "loans_hist")
