# -*- coding: utf-8 -*-
"""views/trazabilidad.py - Trazabilidad para el personal (supervisores y administrador).

El cliente no entra aquí: sigue su pedido en Solicitudes → Mis pedidos (core/tracker.py). Esta vista
ofrece el seguimiento de todos los pedidos con su línea de tiempo y una auditoría en frases claras,
la validación de comprobantes de ruta, el historial de un producto y el avance de los servicios."""

import streamlit as st

from core import live_data, location, permissions, service_requests, traceability
from core.ui import (
    badge_html, empty_state, esc, guard_cap, page_header, persistent_tabs, section_title, stat_cards, timeline,
)
from views.location_guide import render_route_record

_STATUS_FILTERS = {
    "Todas": None, "Pendientes": service_requests.STATUS_PENDING, "Aprobadas": service_requests.STATUS_APPROVED,
    "Rechazadas": service_requests.STATUS_REJECTED, "Canceladas": service_requests.STATUS_CANCELLED,
}
_TYPE_FILTERS = {"Todos": None, "Productos": service_requests.TYPE_PRODUCT, "Servicios": service_requests.TYPE_SERVICE}
_STAGE_FILTERS = {"Todas las etapas": None, **{
    label: key for key, (label, _tone) in traceability.STAGES.items()
}}
_FEEDBACK = {"success": st.success, "warning": st.warning, "info": st.info, "danger": st.error}
MAX_RESULTS = 20
MAX_CUSTODY_STEPS = 40


def _reviewer_names(storage, requests: list) -> dict:
    """Nombre de quien revisó cada solicitud (sin cargar todos los usuarios)."""
    names = {}
    for email in {row.get("reviewed_by") for row in requests if row.get("reviewed_by")}:
        found = storage.get_user_by_email(email)
        if found:
            names[email] = found.get("full_name") or email
    return names


def _loan_for(events: list, loans: dict):
    pickup = traceability.first_event(events, traceability.EVENT_PICKED_UP)
    return loans.get(pickup.get("loan_id")) if pickup else None


def _request_header(row: dict, events: list, loan: dict, show_requester: bool) -> None:
    stage, tone = traceability.request_stage(row, events, loan)
    product = row.get("request_type") == service_requests.TYPE_PRODUCT
    quantity = f" · {row.get('quantity')} u." if product else ""
    meta = traceability.short_id(row.get("id"))
    if show_requester:
        meta += f" · {row.get('requester_name') or 'Solicitante'}"
    if row.get("needed_at"):
        meta += f" · requerido {traceability.fmt_local(row.get('needed_at'))}"
    st.markdown(
        f"<div><b>{esc(traceability.request_title(row))}</b>{esc(quantity)}</div>"
        f"<div>{badge_html(stage, tone)} {badge_html('Producto' if product else 'Servicio')} "
        f"<span class=\"lab-section__caption\">{esc(meta)}</span></div>",
        unsafe_allow_html=True,
    )
    if show_requester and traceability.is_stuck(
            row, events, minutes=live_data.stuck_minutes()):
        st.warning(f"Atascada: más de {live_data.stuck_minutes()} min en la misma etapa.")


def _request_card(row: dict, events: list, loan: dict, names: dict, show_requester: bool) -> None:
    with st.container(border=True):
        _request_header(row, events, loan, show_requester)
        with st.expander("Ver línea de tiempo", expanded=False):
            timeline(traceability.request_timeline(row, events, loan, names))
            route = traceability.first_event(events, traceability.EVENT_ROUTE_VERIFIED)
            if route:
                st.markdown("**Ruta registrada**")
                render_route_record(route)
        if show_requester:
            with st.expander("Quién hizo qué", expanded=False):
                for event in events:
                    title = traceability.EVENT_LABELS.get(event.get("event_type"), event.get("event_type", "Evento"))
                    actor = event.get("actor_name") or permissions.role_short_label(event.get("actor_role"))
                    st.markdown(
                        f"**{esc(title)}** — {esc(actor)} · {esc(traceability.fmt_local(event.get('created_at')))}"
                    )
                    sentence = traceability.describe_event(event)
                    if sentence:
                        st.caption(sentence)


# ---------------------------------------------------------------------------
# Personal
# ---------------------------------------------------------------------------

def _render_search(requests: list, events: list, index: dict, loans: dict, names: dict) -> None:
    query = st.text_input(
        "Buscar", key="trace_query",
        placeholder="Solicitud (#a1b2c3), código o nombre del producto, cliente o comprobante",
    )
    with st.expander(":material/filter_alt: Filtros", expanded=False):
        status_col, type_col, stage_col = st.columns(3)
        status = _STATUS_FILTERS[status_col.selectbox("Estado", list(_STATUS_FILTERS), key="trace_status")]
        kind = _TYPE_FILTERS[type_col.selectbox("Tipo", list(_TYPE_FILTERS), key="trace_type")]
        stage = _STAGE_FILTERS[stage_col.selectbox("Etapa", list(_STAGE_FILTERS), key="trace_stage")]
        dates = st.date_input("Rango de fechas", value=(), key="trace_dates")
    start = dates[0] if isinstance(dates, (list, tuple)) and dates else None
    end = dates[-1] if isinstance(dates, (list, tuple)) and len(dates) > 1 else start

    def included(row):
        if status is not None and row.get("status") != status:
            return False
        if kind is not None and row.get("request_type") != kind:
            return False
        if stage is not None and traceability.request_stage_key(row, index.get(row["id"], [])) != stage:
            return False
        created = traceability.to_datetime(row.get("created_at"))
        if start and (not created or created.date() < start):
            return False
        if end and (not created or created.date() > end):
            return False
        return True

    results = [row for row in traceability.search_requests(requests, query, events) if included(row)]
    if not results:
        empty_state("Sin resultados",
                    "Prueba con el código del producto, el nombre del cliente o el número de la solicitud.",
                    icon=":material/search:")
        return
    shown = results[:MAX_RESULTS]
    st.caption(f"{len(results)} solicitud(es)" + (f"; se muestran {len(shown)}." if len(results) > len(shown) else "."))
    for row in shown:
        events_row = index.get(row["id"], [])
        _request_card(row, events_row, _loan_for(events_row, loans), names, show_requester=True)


def _render_receipt_check(storage, user: dict, names: dict, loans: dict) -> None:
    st.caption("El cliente muestra el comprobante de 6 caracteres que obtuvo al terminar la ruta. "
               "Valídalo aquí antes de entregar el producto.")
    with st.form("trace_receipt_form"):
        code = st.text_input("Comprobante", placeholder="Ej: K7Q-2MX", max_chars=12)
        if st.form_submit_button(":material/verified: Validar comprobante", type="primary", use_container_width=True):
            st.session_state.trace_receipt = traceability.verify_receipt(storage, code)

    verification = st.session_state.get("trace_receipt")
    if not verification:
        return
    _FEEDBACK.get(verification.get("tone"), st.info)(verification["message"])
    for warning in verification.get("warnings") or []:
        st.caption(f":material/warning: {warning}")
    if not verification.get("valid"):
        return

    request = verification.get("request")
    if request:
        events_row = storage.get_trace_events(request_id=request["id"])
        _request_card(request, events_row, _loan_for(events_row, loans), names, show_requester=True)
    render_route_record(verification["event"])

    checks = verification.get("checks") or []
    if checks:
        st.caption("Validado por: " + ", ".join(
            f"{check.get('actor_name') or 'Supervisor'} ({traceability.fmt_local(check.get('created_at'))})"
            for check in checks
        ))
    if not any(check.get("actor_id") == user.get("id") for check in checks):
        if st.button(":material/check_circle: Registrar que validé este comprobante", key="trace_receipt_record", use_container_width=True):
            ok, message, _ = traceability.record_receipt_check(storage, verification, user)
            st.session_state.trace_receipt = traceability.verify_receipt(storage, verification["receipt"])
            st.session_state.trace_receipt_notice = (ok, message)
            st.rerun()
    notice = st.session_state.pop("trace_receipt_notice", None)
    if notice:
        (st.success if notice[0] else st.warning)(notice[1])


def _render_custody(storage, requests: list, names: dict) -> None:
    with st.form("trace_custody_form"):
        code = st.text_input("Código del producto", placeholder="Escanea o escribe, p. ej. 2-1-01-01-001")
        if st.form_submit_button(":material/link: Ver cadena de custodia", use_container_width=True):
            st.session_state.trace_custody_code = (code or "").strip()
    code = st.session_state.get("trace_custody_code")
    if not code:
        st.caption("Muestra quién ha tenido el producto, cada movimiento de inventario, sus solicitudes, "
                   "rutas verificadas, retiros y validaciones.")
        return
    item = storage.get_item(code)
    if not item:
        st.warning("No hay ningún producto registrado con ese código.")
        return

    open_loans = storage.get_open_loans_for_item(item["id"])
    item_requests = [row for row in requests if row.get("item_id") == item["id"]]
    open_requests = [row for row in item_requests if row.get("status") in (
        service_requests.STATUS_PENDING, service_requests.STATUS_APPROVED)]
    parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
    stat_cards([
        {"label": "Unidades del laboratorio", "value": item.get("quantity", 0), "icon": "📦"},
        {"label": "Disponibles", "value": storage.get_available_quantity(item["id"]), "icon": "✅"},
        {"label": "En préstamo", "value": sum(int(loan.get("quantity") or 0) for loan in open_loans), "icon": "🤝"},
        {"label": "Solicitudes abiertas", "value": len(open_requests), "icon": "📝"},
    ])
    route = location.compact_route(location.build_route_checkpoints(item, parent))
    st.caption(f"{item.get('name')} · {item['id']} · ruta {route}")

    if open_loans:
        section_title("¿Quién lo tiene ahora?", icon=":material/handshake:")
        for loan in open_loans:
            due = loan.get("expected_return_at")
            st.caption(
                f"• {loan.get('user_name')} ({loan.get('user_role')}) · {loan.get('quantity')} u. · desde "
                f"{traceability.fmt_local(loan.get('checkout_at'))}"
                + (f" · devolver antes de {traceability.fmt_local(due)}" if due else "")
            )

    section_title("Historial del producto", icon=":material/link:",
                  caption="Lo más reciente primero: movimientos de inventario, solicitudes y trazabilidad.")
    steps = traceability.custody_timeline(
        storage.get_item_history(item["id"]), item_requests,
        storage.get_trace_events(item_id=item["id"]), names,
    )
    if not steps:
        empty_state("Sin movimientos", "Este producto todavía no tiene historial.", icon=":material/link:")
        return
    steps = list(reversed(steps))
    timeline(steps[:MAX_CUSTODY_STEPS])
    if len(steps) > MAX_CUSTODY_STEPS:
        st.caption(f"Se muestran los {MAX_CUSTODY_STEPS} registros más recientes de {len(steps)}.")


def _render_services(storage, user: dict, requests: list, index: dict, names: dict) -> None:
    services = [row for row in requests if row.get("request_type") == service_requests.TYPE_SERVICE
                and row.get("status") == service_requests.STATUS_APPROVED]
    active = [row for row in services if traceability.service_stage(index.get(row["id"])) != "delivered"]
    delivered = [row for row in services if row not in active]
    notice = st.session_state.pop("trace_service_notice", None)
    if notice:
        (st.success if notice[0] else st.error)(notice[1])
    if not active:
        empty_state("Sin servicios por atender", "Los servicios aprobados aparecen aquí hasta su entrega.", icon=":material/build:")
    for row in active:
        events_row = index.get(row["id"], [])
        stage = traceability.service_stage(events_row)
        with st.container(border=True):
            _request_header(row, events_row, None, show_requester=True)
            if row.get("description"):
                st.caption(row["description"])
            with st.form(f"trace_service_{row['id']}"):
                notes = st.text_input("Nota para el cliente (opcional)", key=f"trace_service_notes_{row['id']}")
                cols = st.columns(2 if stage == "approved" else 1)
                start = cols[0].form_submit_button(
                    "▶️ Marcar en curso", use_container_width=True) if stage == "approved" else False
                deliver = cols[-1].form_submit_button(":material/inventory_2: Marcar entregado", type="primary", use_container_width=True)
                if start or deliver:
                    target = traceability.EVENT_SERVICE_STARTED if start else traceability.EVENT_SERVICE_DELIVERED
                    ok, message, _ = traceability.mark_service_stage(storage, row["id"], target, user, notes)
                    st.session_state.trace_service_notice = (ok, message)
                    st.rerun()
    if delivered:
        with st.expander(f"Entregados ({len(delivered)})"):
            for row in delivered:
                events_row = index.get(row["id"], [])
                _request_card(row, events_row, None, names, show_requester=True)


def _render_manager(storage, user: dict) -> None:
    requests = traceability.visible_requests(storage, user)
    events = traceability.visible_events(storage, user)
    index = traceability.events_by_request(events)
    loans = {loan["id"]: loan for loan in storage.get_all_loans()}
    names = {row["institutional_email"]: row.get("full_name") or row["institutional_email"]
             for row in storage.get_all_users() if row.get("institutional_email")}
    stat_cards(traceability.manager_summary(requests, events))

    tab_titles = [
        ":material/search: Pedidos", ":material/verified: Comprobantes",
        ":material/link: Historial de un producto", ":material/build: Servicios",
    ]
    if permissions.has_cap(user, permissions.CAP_REPORTS):
        tab_titles.append(":material/shield: Integridad")
    tabs = persistent_tabs(tab_titles, "trazabilidad")
    tab_search, tab_receipt, tab_custody, tab_services = tabs[:4]
    with tab_search:
        _render_search(requests, events, index, loans, names)
    with tab_receipt:
        _render_receipt_check(storage, user, names, loans)
    with tab_custody:
        _render_custody(storage, requests, names)
    with tab_services:
        _render_services(storage, user, requests, index, names)
    if len(tabs) > 4:
        with tabs[4]:
            issues = traceability.reconcile(storage)
            st.metric("Inconsistencias", len(issues))
            if issues:
                st.dataframe(issues, use_container_width=True, hide_index=True)
            else:
                st.success("No se detectaron inconsistencias en solicitudes y préstamos.")
            st.download_button(
                "Exportar trazas seudonimizadas",
                traceability.export_trace_csv(events, "uniminuto-trace-v1"),
                "trazabilidad-sin-pii.csv", "text/csv", use_container_width=True,
            )


@st.fragment(run_every=live_data.run_every(live_data.list_refresh_seconds()))
def _live_overview(storage, user: dict) -> None:
    requests = traceability.visible_requests(storage, user)
    stuck = 0
    if permissions.has_cap(user, permissions.CAP_VIEW_ALL_TRACES):
        index = storage.get_events_index()       # una lectura compartida, no una por pedido
        for request in requests:
            stuck += int(traceability.is_stuck(request, index.get(request["id"], []), live_data.stuck_minutes()))
    st.caption(f"{len(requests)} solicitud(es) visibles" + (f" · {stuck} atascada(s)" if stuck else ""))


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    if not guard_cap(user, permissions.CAP_VIEW_ALL_TRACES, "Trazabilidad"):
        return
    page_header("Trazabilidad", icon=":material/explore:", subtitle="Seguimiento de todos los pedidos y de cada producto")
    _live_overview(storage, user)
    _render_manager(storage, user)
