# -*- coding: utf-8 -*-
"""Solicitudes: un solo lugar para pedir un producto, un servicio o una reserva y seguir su avance.

- **Nueva solicitud:** producto (se busca por nombre o código), servicio o reserva.
- **Mis pedidos:** cada pedido con su avance en lenguaje sencillo (enviada → aprobada → preparando →
  listo para reclamar → completado). El cliente no ve nada más que lo suyo.
- **Gestionar** (quien aprueba): solicitudes pendientes y, con permiso de reservas, las reservas por revisar.
"""

from datetime import datetime, time

import streamlit as st

from core import (
    dispatch, live_data, permissions, reservations, search, service_requests, tracker, traceability,
)
from core.ui import (
    badge_html, empty_state, esc, flash, guard_cap, more_button, page_header, paginate, persistent_tabs,
    section_title, show_flash, stepper,
)
from views import reservas
from views.location_guide import render_location_guide, render_verifiable_route

_TYPES = {"Producto": service_requests.TYPE_PRODUCT, "Servicio": service_requests.TYPE_SERVICE, "Reserva": "reservation"}
_FLASH = "solicitudes"
_PRODUCT_KINDS = {"standalone", "child"}      # lo que se puede pedir (no contenedores ni ubicaciones)
_TONE_BY_STATUS = {"pending": "warning", "approved": "success", "rejected": "danger", "cancelled": "neutral"}


def _request_title(row: dict) -> str:
    return row.get("item_name") if row.get("request_type") == "product" else row.get("service_name")


def _local(value) -> str:
    return reservations.as_bogota(value).strftime("%d/%m/%Y %I:%M %p") if value else "Sin fecha"


def _needed_at(day):
    """Fecha opcional «para cuándo»: si se elige, vence a las 6 p. m. de ese día (hora de Bogotá)."""
    return datetime.combine(day, time(18, 0), tzinfo=reservations.BOGOTA_TZ) if day else None


# ---------------------------------------------------------------------------
# Pedir un producto (también lo usa Buscar)
# ---------------------------------------------------------------------------

def request_product_form(storage, user: dict, item: dict, key_prefix: str, flash_key: str = _FLASH) -> None:
    """Formulario corto para pedir ``item``: cantidad, nota opcional y, si hace falta, para cuándo."""
    if not permissions.has_cap(user, permissions.CAP_REQUEST):
        return
    available = storage.get_available_quantity(item["id"])
    if available <= 0:
        st.warning(f"«{item.get('name')}» no tiene unidades disponibles ahora.")
        return
    with st.form(f"request_form_{key_prefix}"):
        quantity = st.number_input("Cantidad", min_value=1, max_value=int(available), value=1, step=1,
                                   help=f"Hay {available} disponible(s).")
        note = st.text_input("Nota (opcional)", placeholder="Para qué lo necesitas")
        day = st.date_input("¿Para qué día? (opcional)", value=None, min_value=datetime.now(reservations.BOGOTA_TZ).date())
        send_col, add_col = st.columns(2)
        send = send_col.form_submit_button(":material/send: Pedir este producto", type="primary", use_container_width=True)
        add = add_col.form_submit_button(":material/add_shopping_cart: Agregar al pedido", use_container_width=True,
                                         help="Suma este producto a un pedido con varios productos y envíalo todo junto.")
        if add:
            basket_add(item, int(quantity), available)
            flash(f"«{item.get('name')}» × {int(quantity)} agregado al pedido. Sigue agregando o envíalo desde "
                  "Solicitudes → Nueva solicitud.", key=flash_key)
            st.rerun()
        if send:
            try:
                row, notified, message = service_requests.submit_request(
                    storage, user, service_requests.TYPE_PRODUCT, item["id"], quantity,
                    description=note, needed_at=_needed_at(day),
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                flash(f"Pedido {traceability.short_id(row['id'])} enviado: «{item.get('name')}» × {int(quantity)}. "
                      "Sigue su avance en Solicitudes → Mis pedidos." + ("" if notified else f" ({message})"),
                      key=flash_key)
                st.rerun()


BASKET_KEY = "order_basket"      # carrito del pedido de varios productos (por sesión)


def basket_lines() -> list:
    return [dict(line) for line in st.session_state.get(BASKET_KEY, [])]


def basket_add(item: dict, quantity: int, available: int = None) -> None:
    """Suma ``quantity`` de ``item`` al carrito (tope: lo disponible)."""
    lines = basket_lines()
    st.session_state.pop(f"basket_qty_{item['id']}", None)       # que la cantidad nueva reemplace a la del campo
    for line in lines:
        if line["item_id"] == item["id"]:
            line["quantity"] += quantity
            break
    else:
        lines.append({"item_id": item["id"], "name": item.get("name") or item["id"], "quantity": quantity})
    if available is not None:
        for line in lines:
            if line["item_id"] == item["id"]:
                line["quantity"] = max(1, min(line["quantity"], int(available)))
    st.session_state[BASKET_KEY] = lines


def basket_remove(item_id: str) -> None:
    st.session_state.pop(f"basket_qty_{item_id}", None)
    st.session_state[BASKET_KEY] = [line for line in basket_lines() if line["item_id"] != item_id]


def _render_basket(storage, user: dict) -> None:
    """El pedido de varios productos: lo agregado, una nota para todos y el envío en una sola solicitud."""
    lines = basket_lines()
    if not lines:
        return
    st.divider()
    section_title(f"Tu pedido ({len(lines)} producto{'s' if len(lines) != 1 else ''})", icon=":material/shopping_cart:",
                  caption="Se envía como UNA solicitud: el supervisor la aprueba completa y el laboratorio la prepara junta.")
    for line in lines:
        c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="center")
        c1.markdown(f"**{esc(line['name'])}** · `{esc(line['item_id'])}`")
        available = storage.get_available_quantity(line["item_id"])
        quantity = c2.number_input("Cantidad", min_value=1, max_value=max(1, int(available)),
                                   value=min(int(line["quantity"]), max(1, int(available))), step=1,
                                   key=f"basket_qty_{line['item_id']}", label_visibility="collapsed",
                                   help=f"Hay {available} disponible(s).")
        if int(quantity) != int(line["quantity"]):
            line["quantity"] = int(quantity)
            st.session_state[BASKET_KEY] = [dict(line) if row["item_id"] == line["item_id"] else row for row in lines]
        if c3.button(":material/delete:", key=f"basket_del_{line['item_id']}", help="Quitar del pedido",
                     use_container_width=True):
            basket_remove(line["item_id"])
            st.rerun()
    with st.form("basket_send_form"):
        note = st.text_input("Nota para el laboratorio (opcional)", placeholder="Para qué lo necesitas")
        day = st.date_input("¿Para qué día? (opcional)", value=None, min_value=datetime.now(reservations.BOGOTA_TZ).date())
        c1, c2 = st.columns([3, 1])
        send = c1.form_submit_button(f":material/send: Enviar pedido de {len(lines)} producto(s)", type="primary",
                                     use_container_width=True)
        clear = c2.form_submit_button("Vaciar", use_container_width=True)
    if clear:
        st.session_state[BASKET_KEY] = []
        st.rerun()
    if send:
        try:
            rows, notified, message = service_requests.submit_order(
                storage, user, [{"item_id": line["item_id"], "quantity": line["quantity"]} for line in lines],
                description=note, needed_at=_needed_at(day))
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state[BASKET_KEY] = []
            flash(f"Pedido enviado: {len(rows)} producto(s) en una sola solicitud. Sigue su avance en Mis pedidos."
                  + ("" if notified else f" ({message})"), key=_FLASH)
            st.rerun()


def _pick_product(storage, key: str):
    """Buscador de productos: nombre, código o lectura de pistola. Devuelve el ítem elegido o ``None``."""
    items = [item for item in storage.get_all_items() if item.get("item_type") in _PRODUCT_KINDS]
    if not items:
        st.warning("No hay productos activos disponibles para solicitar.")
        return None
    query = st.text_input(":material/search: Buscar producto", key=f"{key}_query",
                          placeholder="Escribe el nombre o escanea el código")
    matches = search.search_items(items, query, kinds=_PRODUCT_KINDS) if query.strip() else items[:search.MAX_RESULTS]
    if query.strip() and not matches:
        empty_state("Sin resultados", "Prueba con otra palabra o con el código del producto.", icon=":material/search:")
        return None
    if not query.strip():
        st.caption("Escribe arriba para filtrar; mientras tanto se muestran los primeros productos.")
    options = {item["id"]: item for item in matches}
    choice = st.selectbox(
        "Producto", list(options), key=f"{key}_choice",
        format_func=lambda code: f"{options[code]['name']} · {code}",
    )
    return options.get(choice)


def _render_new(storage, user: dict) -> None:
    kind = st.radio("¿Qué necesitas?", list(_TYPES), horizontal=True, key="request_kind")
    request_type = _TYPES[kind]
    if request_type == "reservation":
        reservas.render_new(storage, user)
        return
    if request_type == service_requests.TYPE_PRODUCT:
        item = _pick_product(storage, "request_product")
        if item is None:
            _render_basket(storage, user)
            return
        parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
        c1, c2 = st.columns([1, 2], vertical_alignment="center")
        c1.metric("Disponibles ahora", storage.get_available_quantity(item["id"]))
        c2.caption(f"Ubicación: {item.get('location') or (parent or {}).get('location') or 'sin dato'}")
        with st.expander(":material/pin_drop: ¿Dónde está?", expanded=False):
            render_location_guide(item, parent, key_prefix=f"request_guide_{item['id']}")
        request_product_form(storage, user, item, key_prefix=f"new_{item['id']}")
        _render_basket(storage, user)
        return

    with st.form("service_request_form"):
        service_name = st.text_input("Servicio requerido", placeholder="Ej: impresión 3D, corte láser o asesoría")
        description = st.text_area("¿Qué necesitas?", placeholder="Explica el uso, curso, materiales o resultado esperado")
        day = st.date_input("¿Para qué día? (opcional)", value=None, min_value=datetime.now(reservations.BOGOTA_TZ).date())
        if st.form_submit_button(":material/send: Pedir servicio", type="primary", use_container_width=True):
            try:
                row, notified, message = service_requests.submit_request(
                    storage, user, service_requests.TYPE_SERVICE, service_name=service_name,
                    description=description, needed_at=_needed_at(day),
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                flash(f"Solicitud {traceability.short_id(row['id'])} enviada. Sigue su avance en Mis pedidos."
                      + ("" if notified else f" ({message})"), key=_FLASH)
                st.rerun()


# ---------------------------------------------------------------------------
# Mis pedidos
# ---------------------------------------------------------------------------

def _is_closed(progress: dict) -> bool:
    return all(step["state"] in ("done", "blocked") for step in progress["steps"])


def _status_header(title: str, meta: str, progress: dict) -> None:
    st.markdown(
        f"<div><b>{esc(title)}</b> {badge_html(progress['headline'], progress['tone'])}</div>"
        f"<div class=\"lab-section__caption\">{esc(meta)}</div>",
        unsafe_allow_html=True,
    )
    stepper(progress["steps"])
    if progress.get("hint"):
        st.caption(progress["hint"])


def render_request_status(storage, user: dict, row: dict, events: list = None, loan: dict = None,
                          key_prefix: str = "mine") -> None:
    """Tarjeta de seguimiento de un pedido de producto o servicio (la ve su dueño y el personal)."""
    events = events if events is not None else storage.get_trace_events(request_id=row["id"])
    progress = tracker.client_tracker(row, events, loan)
    product = row.get("request_type") == service_requests.TYPE_PRODUCT
    quantity = f" × {row.get('quantity')}" if product else ""
    own = row.get("requester_id") == user.get("id")
    with st.container(border=True):
        _status_header(f"{_request_title(row) or 'Solicitud'}{quantity}",
                       f"{traceability.short_id(row.get('id'))} · pedido {_local(row.get('created_at'))}"
                       + ("" if own else f" · {row.get('requester_name')}"), progress)
        if row.get("description"):
            st.caption(f"Nota: {row['description']}")
        if row.get("review_notes"):
            st.caption(f"Respuesta del supervisor: {row['review_notes']}")
        c1, c2 = st.columns(2)
        if own and progress["can_confirm"] and permissions.has_cap(user, permissions.CAP_CONFIRM_RECEIPT):
            if c1.button("Recibí mi pedido", key=f"{key_prefix}_confirm_{row['id']}", type="primary",
                         use_container_width=True):
                ok, message, _ = dispatch.confirm_client_receipt(storage, row["id"], user)
                flash(message, "success" if ok else "error", key=_FLASH)
                st.rerun()
        picked_up = bool(traceability.first_event(events, traceability.EVENT_PICKED_UP))
        started = traceability.dispatch_started(events)
        if (own and row.get("status") in ("pending", "approved") and not picked_up and not started
                and permissions.has_cap(user, permissions.CAP_CANCEL_OWN_REQUEST)):
            if c2.button("Cancelar pedido", key=f"{key_prefix}_cancel_{row['id']}", use_container_width=True):
                ok, message = service_requests.cancel_request(storage, row["id"], user)
                flash(message, "success" if ok else "error", key=_FLASH)
                st.rerun()
        # Retiro por cuenta propia (sin despacho): la ruta verificable sigue disponible.
        if (own and product and row.get("status") == service_requests.STATUS_APPROVED and not picked_up
                and not started and permissions.has_cap(user, permissions.CAP_CHECKOUT)):
            focused = st.session_state.get("trace_focus_request") == row["id"]    # lo abre un toque de chip NFC
            with st.expander(":material/explore: Retirarlo yo mismo: ruta hasta el producto", expanded=focused):
                st.caption("Escanea (o escribe) el código de cada etiqueta en el camino; al llegar obtienes un "
                           "comprobante que el supervisor puede validar. Si prefieres, espera a que un operario lo prepare.")
                render_verifiable_route(storage, row, user, key_prefix="trace_route")


def _order_status(storage, user: dict, order: dict) -> None:
    """Pedido de varios productos: encabezado con el resumen y cada producto con su propio avance."""
    lines = order["lines"]
    first = lines[0][0]
    total = sum(int(row.get("quantity") or 0) for row, *_ in lines)
    open_lines = [row for row, events, _, closed in lines
                  if not closed and row.get("status") in ("pending", "approved")
                  and not traceability.dispatch_started(events)
                  and not traceability.first_event(events, traceability.EVENT_PICKED_UP)]
    with st.container(border=True):
        st.markdown(f"**Pedido {esc(order['order_id'].upper())}** · {len(lines)} productos · {total} unidades")
        st.caption(f"Enviado {_local(first.get('created_at'))}. El supervisor lo aprueba completo y el laboratorio lo "
                   "prepara junto; cada producto muestra su avance.")
        for row, events, loan, _ in lines:
            render_request_status(storage, user, row, events, loan, key_prefix=f"order_{order['order_id']}")
        if open_lines and permissions.has_cap(user, permissions.CAP_CANCEL_OWN_REQUEST):
            if st.button("Cancelar todo el pedido", key=f"order_cancel_{order['order_id']}", use_container_width=True):
                ok, message = service_requests.cancel_order(storage, order["order_id"], user)
                flash(message, "success" if ok else "error", key=_FLASH)
                st.rerun()


def _reservation_status(storage, row: dict, user: dict) -> None:
    progress = tracker.reservation_tracker(row)
    with st.container(border=True):
        _status_header(f"Reserva · {row.get('activity') or 'Laboratorio completo'}",
                       f"{_local(row.get('start_at'))} → {_local(row.get('end_at'))} · "
                       f"{row.get('attendees')} asistente(s)", progress)
        if row.get("review_notes"):
            st.caption(f"Respuesta del supervisor: {row['review_notes']}")
        if row.get("status") in ("pending", "approved") and row.get("requester_id") == user.get("id"):
            if st.button("Cancelar reserva", key=f"mine_cancel_res_{row['id']}", use_container_width=True):
                ok, message = reservations.cancel_reservation(storage, row["id"], user)
                flash(message, "success" if ok else "error", key=_FLASH)
                st.rerun()


@st.fragment(run_every=live_data.run_every(live_data.list_refresh_seconds()))
def _render_mine(storage, user: dict) -> None:
    requests = storage.get_service_requests(user_id=user["id"])
    mine_reservations = storage.get_reservations(user_id=user["id"])
    if not requests and not mine_reservations:
        empty_state("Todavía no tienes pedidos", "Cuando pidas un producto, un servicio o una reserva, aquí verás en qué paso va.",
                    icon=":material/history:")
        return
    index = traceability.events_by_request(storage.get_trace_events(user_id=user["id"]))
    loans = {loan["id"]: loan for loan in storage.get_all_loans() if loan.get("user_id") == user["id"]}

    entries = []
    for group in service_requests.group_by_order(requests):
        lines = []
        for row in group["rows"]:
            events = index.get(row["id"], [])
            pickup = traceability.first_event(events, traceability.EVENT_PICKED_UP)
            loan = loans.get(pickup.get("loan_id")) if pickup else None
            lines.append((row, events, loan, _is_closed(tracker.client_tracker(row, events, loan))))
        if len(lines) == 1:
            row, events, loan, closed = lines[0]
            entries.append((row.get("created_at") or "", "request", row, events, loan, closed))
        else:        # pedido de varios productos: una sola tarjeta con sus líneas
            first = group["rows"][0]
            entries.append((first.get("created_at") or "", "order", {"order_id": group["order_id"], "lines": lines},
                            [], None, all(line[3] for line in lines)))
    for row in mine_reservations:
        entries.append((row.get("created_at") or "", "reservation", row, [], None,
                        _is_closed(tracker.reservation_tracker(row))))
    entries.sort(key=lambda entry: str(entry[0]), reverse=True)

    def draw(entry):
        _, kind, row, events, loan, _ = entry
        if kind == "request":
            render_request_status(storage, user, row, events, loan)
        elif kind == "order":
            _order_status(storage, user, row)
        else:
            _reservation_status(storage, row, user)

    active = [entry for entry in entries if not entry[5]]
    closed = [entry for entry in entries if entry[5]]
    section_title(f"En curso ({len(active)})", icon=":material/pending_actions:")
    if not active:
        st.caption("No tienes pedidos en curso.")
    for entry in active:
        draw(entry)
    if closed:
        with st.expander(f"Historial ({len(closed)})"):
            for entry in closed:
                draw(entry)


# ---------------------------------------------------------------------------
# Gestionar (quien aprueba)
# ---------------------------------------------------------------------------

def _review_order(storage, user: dict, group: dict) -> None:
    """Pedido de varios productos pendiente: se aprueba o rechaza completo."""
    rows = group["rows"]
    first = rows[0]
    with st.expander(f"Pedido de {len(rows)} productos · {first.get('requester_name')} · "
                     f"{_local(first.get('needed_at'))}", expanded=False):
        st.markdown(f"**Pedido {esc(group['order_id'].upper())}** · {esc(first.get('requester_name'))}")
        for row in rows:
            st.markdown(f"- {esc(_request_title(row))} × {esc(row.get('quantity'))} "
                        f"(disponibles ahora: {storage.get_available_quantity(row.get('item_id'))})")
        if first.get("description"):
            st.info(f"Nota del cliente: {first['description']}", icon=":material/sticky_note_2:")
        with st.form(f"review_order_{group['order_id']}"):
            notes = st.text_input("Respuesta para el solicitante (opcional)")
            own = first.get("requester_id") == user["id"] and not dispatch.allow_self_approval()
            if own:
                st.caption("Es tu propio pedido: lo aprueba otra persona.")
            c1, c2 = st.columns(2)
            approve = c1.form_submit_button("Aprobar pedido completo", type="primary",
                                            use_container_width=True, disabled=own)
            reject = c2.form_submit_button("Rechazar pedido completo", use_container_width=True)
            if approve or reject:
                decision = service_requests.STATUS_APPROVED if approve else service_requests.STATUS_REJECTED
                ok, message = service_requests.review_order(storage, group["order_id"], decision, user, notes)
                flash(message, "success" if ok else "error", key=_FLASH)
                st.rerun()


def _render_manage(storage, user: dict) -> None:
    pending = storage.get_service_requests(status=service_requests.STATUS_PENDING)
    section_title(f"Solicitudes por revisar ({len(pending)})", icon=":material/fact_check:")
    if not pending:
        st.success("No hay solicitudes pendientes.")
    for group in paginate(service_requests.group_by_order(pending), "requests_pending"):
        if len(group["rows"]) > 1:
            _review_order(storage, user, group)
            continue
        row = group["rows"][0]
        with st.expander(
            f"{_request_title(row)} · {row.get('requester_name')} · {_local(row.get('needed_at'))}", expanded=False,
        ):
            quantity = f" × {row.get('quantity')}" if row.get("request_type") == "product" else ""
            st.markdown(f"**{esc(_request_title(row) or 'Solicitud')}**{esc(quantity)} · "
                        f"{esc(row.get('requester_name'))} · {esc(traceability.short_id(row.get('id')))}")
            if row.get("description"):
                st.info(f"Nota del cliente: {row['description']}", icon=":material/sticky_note_2:")
            with st.form(f"review_request_{row['id']}"):
                notes = st.text_input("Respuesta para el solicitante (opcional)")
                own = row.get("requester_id") == user["id"] and not dispatch.allow_self_approval()
                if own:
                    st.caption("Es tu propia solicitud: la aprueba otra persona.")
                c1, c2 = st.columns(2)
                approve = c1.form_submit_button("Aprobar", type="primary", use_container_width=True, disabled=own)
                reject = c2.form_submit_button("Rechazar", use_container_width=True)
                if approve or reject:
                    decision = service_requests.STATUS_APPROVED if approve else service_requests.STATUS_REJECTED
                    ok, message = service_requests.review_request(
                        storage, row["id"], decision, user, notes,
                    )
                    flash(message, "success" if ok else "error", key=_FLASH)
                    st.rerun()
    more_button(len(pending), "requests_pending")
    st.caption("Al aprobar un producto entra a la cola de Despacho: cualquier operario o supervisor lo puede tomar. "
               "El préstamo y el descuento de disponibilidad se crean al completar el picking.")

    if permissions.has_cap(user, permissions.CAP_MANAGE_RESERVATIONS):
        st.divider()
        section_title("Reservas", icon=":material/calendar_month:")
        reservas.render_manage(storage, user)


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    if not guard_cap(user, permissions.CAP_REQUEST, "Solicitudes"):
        return
    reviewer = permissions.has_cap(user, permissions.CAP_APPROVE) or permissions.has_cap(
        user, permissions.CAP_MANAGE_RESERVATIONS)

    page_header("Solicitudes", icon=":material/edit_note:",
                subtitle="Pide un producto, un servicio o una reserva y sigue su avance")
    show_flash(_FLASH)
    titles = [":material/add: Nueva solicitud", ":material/timeline: Mis pedidos"]
    if reviewer:
        titles.append(":material/fact_check: Gestionar")
    tabs = persistent_tabs(titles, "solicitudes")
    with tabs[0]:
        _render_new(storage, user)
    with tabs[1]:
        _render_mine(storage, user)
    if reviewer:
        with tabs[2]:
            _render_manage(storage, user)

