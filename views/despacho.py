# -*- coding: utf-8 -*-
"""Cola operativa de despacho, diseñada para teléfono y lector de códigos.

El panel de picking (y el de decisión del supervisor) van ARRIBA: en el teléfono
la persona los ve sin recorrer la lista. Las lecturas del picking viven en
:mod:`core.picking` (memoria del proceso), no en ``st.session_state``: un toque
de chip NFC abre una pestaña nueva y esa pestaña suma la lectura al mismo
picking.
"""

import json

import streamlit as st
import streamlit.components.v1 as components

from core import dispatch, live_data, loans, ludica, permissions, pick_route, picking, returns, service_requests, traceability
from core.ui import badge_html, empty_state, esc, guard_cap, page_header, persistent_tabs, section_title
from views import location_guide, photo_panel

_DISPATCH_EVERY = live_data.run_every(live_data.dispatch_refresh_seconds())


def _notice(ok: bool, message: str) -> None:
    st.session_state.dispatch_notice = (ok, message)


def _order_context(storage, request: dict, key: str, photo: bool = True) -> None:
    """Dónde está el producto (ruta y referencia) y cómo se ve."""
    item = storage.get_item(request.get("item_id") or "")
    if not item:
        return
    parent = storage.get_item(item.get("parent_id") or "") if item.get("parent_id") else None
    try:
        route = location_guide.compact_route_for(storage, item, parent)
    except Exception:  # la guía nunca debe tumbar la cola
        route = ""
    reference = str(item.get("location") or (parent or {}).get("location") or "").strip()
    if route:
        st.markdown(f":material/pin_drop: **Ruta** {esc(route)}")
    if reference:
        st.markdown(f":material/location_on: {esc(reference)}")
    if photo:
        photo_panel.render_scan_photo(item, compact=True, key_suffix=key)
    location_guide.render_location_guide(item, parent, key_prefix=f"dispatch_guide_{key}")


def _focus_reading_input(label: str) -> None:
    """Enfoca el campo del código: la pistola «teclea» donde esté el foco.

    Streamlit no tiene ``autofocus`` en ``text_input`` (1.65), así que se pide el
    foco desde un iframe de altura cero (mismo origen que la app, como el puente
    de cookie de core.session)."""
    script = (
        "<script>const label = %s; const tries = [0, 150, 500];"
        "tries.forEach(ms => setTimeout(() => {"
        "const box = window.parent.document.querySelector('input[aria-label=' + JSON.stringify(label) + ']');"
        "if (box && window.parent.document.activeElement !== box) box.focus();}, ms));</script>"
    ) % json.dumps(label)
    components.html(script, height=0)


def _heading(request: dict, events: list) -> None:
    stage, tone = traceability.request_stage(request, events)
    st.markdown(
        f"<b>{esc(request.get('item_name') or request.get('item_id'))}</b> "
        f"{badge_html(stage, tone)}<br>"
        f"<span class='lab-section__caption'>{esc(traceability.short_id(request.get('id')))} · "
        f"{esc(request.get('requester_name'))} · {int(request.get('quantity') or 0)} u.</span>",
        unsafe_allow_html=True,
    )
    note = str(request.get("description") or "").strip()
    if note:      # la nota que escribió el cliente la ve quien prepara, recibe o entrega el pedido
        st.info(f"Nota del cliente: {note}", icon=":material/sticky_note_2:")


@st.fragment(run_every=_DISPATCH_EVERY)
def _queue_fragment(storage, user: dict) -> None:
    try:
        rows = dispatch.queue_for(storage, user)
    except PermissionError as exc:
        st.error(str(exc))
        return
    section_title(f"Por tomar ({len(rows)})", icon=":material/inbox:")
    if not rows:
        empty_state("Cola al día", "No hay pedidos autorizados disponibles.", icon=":material/inbox:")
        return
    for group in service_requests.group_by_order(rows):
        if len(group["rows"]) > 1:
            _order_header(group, f"Tomar todo el pedido ({len(group['rows'])})", f"claim_order_{group['order_id']}",
                          lambda order_id=group["order_id"]: _claim_whole_order(storage, user, order_id))
        for request in group["rows"]:
            _queue_line(storage, user, request)


def _order_header(group: dict, label: str, key: str, action) -> None:
    """Encabezado de un pedido de varios productos con su acción para todas las líneas a la vez."""
    first = group["rows"][0]
    total = sum(int(row.get("quantity") or 0) for row in group["rows"])
    with st.container(border=True):
        st.markdown(f":material/shopping_cart: **Pedido {esc(group['order_id'].upper())}** · "
                    f"{len(group['rows'])} productos · {total} unidades · {esc(first.get('requester_name'))}")
        if st.button(label, key=key, type="primary", use_container_width=True):
            action()


def _claim_whole_order(storage, user: dict, order_id: str) -> None:
    ok, message, taken = dispatch.claim_order(storage, order_id, user)
    if ok and taken:
        started, text = dispatch.start_picking(storage, taken[0]["id"], user)
        message += " Lee la ruta y las unidades de cada producto." if started else f" {text}"
    _notice(ok, message)
    st.rerun()


def _queue_line(storage, user: dict, request: dict) -> None:
    events = storage.get_trace_events(request_id=request["id"])
    with st.container(border=True):
        _heading(request, events)
        _order_context(storage, request, f"queue_{request['id']}")
        if st.button("Tomar y empezar picking", key=f"claim_{request['id']}", type="primary",
                     use_container_width=True,
                     help="Lo tomas y se abre el panel de picking arriba, listo para leer el código."):
            ok, message, _ = dispatch.claim_request(storage, request["id"], user)
            if ok:
                ok, message = dispatch.start_picking(storage, request["id"], user)
                message = "Pedido tomado. Lee la ruta y el código de cada unidad, o confirma manualmente." if ok else message
            _notice(ok, message)
            st.rerun()


@st.fragment(run_every=_DISPATCH_EVERY)
def _mine_fragment(storage, user: dict) -> None:
    rows = dispatch.my_orders(storage, user)
    section_title(f"Mis pedidos ({len(rows)})", icon=":material/person_pin:")
    if not rows:
        empty_state("Sin pedidos asignados", "Toma uno desde la cola cuando estés listo.",
                    icon=":material/person_pin:")
        return
    active = picking.active_request(user["id"])
    for request in rows:
        events = storage.get_trace_events(request_id=request["id"])
        stage = traceability.request_stage_key(request, events)
        with st.container(border=True):
            _heading(request, events)
            if stage == traceability.STAGE_CLAIMED:
                _order_context(storage, request, f"mine_{request['id']}")
                c1, c2 = st.columns(2)
                if c1.button("Seguir picking" if active == request["id"] else "Iniciar picking",
                             key=f"start_pick_{request['id']}", type="primary", use_container_width=True):
                    ok, message = dispatch.start_picking(storage, request["id"], user)
                    if not ok:
                        _notice(False, message)
                    st.rerun()
                if c2.button("Liberar", key=f"release_{request['id']}", use_container_width=True):
                    ok, message, _ = dispatch.release_request(storage, request["id"], user)
                    _notice(ok, message)
                    st.rerun()
            elif stage == traceability.STAGE_PICKED:
                st.success("Picking completo. Esperando recepción del supervisor.")


@st.fragment(run_every=_DISPATCH_EVERY)
def _review_fragment(storage, user: dict) -> None:
    rows = dispatch.review_queue(storage, user)
    section_title(f"Por revisar y entregar ({len(rows)})", icon=":material/fact_check:")
    if not rows:
        empty_state("Sin pedidos por revisar", "Los picking completos aparecerán aquí.",
                    icon=":material/fact_check:")
        return
    for group in service_requests.group_by_order(rows):
        if len(group["rows"]) > 1:
            stages = {traceability.request_stage_key(r, storage.get_trace_events(request_id=r["id"]))
                      for r in group["rows"]}
            order_id = group["order_id"]
            if traceability.STAGE_PICKED in stages:
                _order_header(group, "Confirmar recepción de todo el pedido", f"receive_order_{order_id}",
                              lambda oid=order_id: _order_step(storage, user, dispatch.receive_order, oid))
            elif stages == {traceability.STAGE_RECEIVED}:
                _order_header(group, "Entregar todo el pedido al cliente", f"deliver_order_{order_id}",
                              lambda oid=order_id: _order_step(storage, user, dispatch.deliver_order, oid))
        for request in group["rows"]:
            _review_line(storage, user, request)


def _order_step(storage, user: dict, step, order_id: str) -> None:
    ok, message, _ = step(storage, order_id, user)
    _notice(ok, message)
    st.rerun()


def _review_line(storage, user: dict, request: dict) -> None:
    events = storage.get_trace_events(request_id=request["id"])
    stage = traceability.request_stage_key(request, events)
    with st.container(border=True):
        _heading(request, events)
        _order_context(storage, request, f"review_{request['id']}")
        if stage == traceability.STAGE_PICKED:
            if st.button("Confirmar recepción", key=f"receive_{request['id']}",
                         type="primary", use_container_width=True):
                ok, message, _ = dispatch.receive_request(storage, request["id"], user)
                _notice(ok, message)
                st.rerun()
        elif stage == traceability.STAGE_RECEIVED:
            c1, c2 = st.columns(2)
            if c1.button("Entregar al cliente", key=f"deliver_{request['id']}",
                         type="primary", use_container_width=True):
                ok, message, _ = dispatch.deliver_request(storage, request["id"], user)
                _notice(ok, message)
                st.rerun()
            if c2.button("Corregir / devolver", key=f"review_fix_{request['id']}",
                         use_container_width=True):
                st.session_state.dispatch_review_focus = request["id"]
                st.rerun()


def _return_heading(row: dict) -> None:
    loan = row["loan"]
    st.markdown(f"**{esc(loan.get('item_name'))}** ×{esc(loan.get('quantity'))} · devuelve "
                f"{esc(loan.get('user_name'))} ({esc(loan.get('user_role'))})")
    note = str(row["requested"].get("notes") or "").strip()
    if note:
        st.info(f"Nota del cliente: {note}", icon=":material/sticky_note_2:")


@st.fragment(run_every=_DISPATCH_EVERY)
def _returns_fragment(storage, user: dict) -> None:
    """Devoluciones: el supervisor verifica lo que llega y un operario lo guarda (cierra el préstamo)."""
    if permissions.has_cap(user, permissions.CAP_VERIFY_RETURN):
        rows = returns.pending(storage, returns.STAGE_REQUESTED)
        section_title(f"Por verificar ({len(rows)})", icon=":material/fact_check:",
                      caption="Revisa el estado y cuenta lo que el cliente trae antes de que se guarde.")
        if not rows:
            empty_state("Sin devoluciones por verificar", "Aquí aparecen cuando un cliente pulsa «Devolver».",
                        icon=":material/assignment_return:")
        for row in rows:
            loan = row["loan"]
            with st.container(border=True):
                _return_heading(row)
                with st.form(f"verify_return_{loan['id']}"):
                    c1, c2 = st.columns(2)
                    condition = c1.selectbox("Estado", list(returns.CONDITIONS), format_func=returns.CONDITIONS.get,
                                             key=f"ret_cond_{loan['id']}")
                    received = c2.number_input("Unidades recibidas", min_value=1, max_value=int(loan["quantity"]),
                                               value=int(loan["quantity"]), step=1, key=f"ret_qty_{loan['id']}")
                    notes = st.text_input("Observación (opcional)", key=f"ret_note_{loan['id']}")
                    send = st.form_submit_button("Verificar devolución", type="primary", use_container_width=True)
                if send:
                    try:
                        ok, message = returns.verify_return(storage, loan["id"], user, condition, received, notes)
                    except PermissionError as exc:
                        ok, message = False, str(exc)
                    _notice(ok, message)
                    st.rerun()
    if permissions.has_cap(user, permissions.CAP_STORE_RETURN):
        rows = returns.pending(storage, returns.STAGE_VERIFIED)
        section_title(f"Por guardar ({len(rows)})", icon=":material/move_to_inbox:",
                      caption="Ya verificadas por un supervisor: llévalas a su lugar y confirma.")
        if not rows:
            empty_state("Nada por guardar", "Las devoluciones verificadas aparecerán aquí.",
                        icon=":material/inventory_2:")
        for row in rows:
            loan, proof = row["loan"], row["verified"]
            with st.container(border=True):
                _return_heading(row)
                condition = returns.CONDITIONS.get(str(proof.get("condition") or ""), "")
                st.success(f"Verificado: {proof.get('received', '?')} de {loan['quantity']} unidad(es) · {condition}"
                           + (f" · «{proof['notes']}»" if proof.get("notes") else ""))
                try:
                    item = storage.get_item(loan["item_id"]) or {}
                except Exception:       # la ubicación es solo una ayuda
                    item = {}
                if item.get("location"):
                    st.caption(f"Se guarda en: {item['location']}")
                if st.button("Guardar en el estante", key=f"store_return_{loan['id']}", type="primary",
                             use_container_width=True):
                    try:
                        ok, message = returns.store_return(storage, loan["id"], user)
                    except PermissionError as exc:
                        ok, message = False, str(exc)
                    _notice(ok, message)
                    st.rerun()


def _route_steps(route: dict, box_checked: bool = False) -> None:
    """Ruta obligatoria: estantería, piso y contenedor se leen en orden; la caja es una casilla al final."""
    lines = []
    for number, point in enumerate(route["points"]):
        if number < route["done"]:
            lines.append(f":green[:material/check_circle:] ~~{point['title']}~~")
        elif number == route["done"]:
            lines.append(f":material/arrow_forward: **{point['title']}** · lee `{point['label_code']}`")
        else:
            lines.append(f":gray[:material/radio_button_unchecked: {point['title']}]")
    if route.get("box"):
        lines.append(f":gray[:material/check_box_outline_blank: {route['box']} · no se lee: marca la casilla al sacar "
                     "las unidades]")
    st.markdown("  \n".join(lines))
    if not route["complete"]:
        st.caption("Lee en orden: estantería y piso (chip NFC o código de barras) y contenedor (chip NFC o barras). "
                   "La caja no se lee: se confirma con una casilla.")


@st.fragment(run_every=_DISPATCH_EVERY)
def _picking_progress(storage, user: dict, request_id: str, needed: int) -> None:
    """Avance del picking leído del almacén del proceso: se actualiza solo, así
    una lectura NFC hecha en otra pestaña aparece aquí sin tocar nada.

    Orden: la ruta (estantería → piso → contenedor, se leen), luego la caja —una casilla obligatoria que deja
    registrado quién retiró— y el conteo de las unidades, que no llevan código de barras. Un producto que no está
    en una caja pide, en lugar de la casilla, leer una vez su etiqueta."""
    readings = dispatch.current_readings(user, request_id)
    route = dispatch.route_progress(storage, user, request_id)
    if route["required"] or route.get("box"):
        _route_steps(route)
    request = storage.get_service_request(request_id) or {}
    label_read = bool(readings)
    needs_label = route["label_required"]
    if needs_label and label_read:
        st.markdown(f":green[:material/check_circle:] Etiqueta del producto leída "
                    f"({dispatch.METHOD_LABELS.get(readings[0].get('method'), readings[0].get('method'))})")
    elif needs_label and route["complete"]:
        st.markdown(f":material/arrow_forward: **Etiqueta del producto** · lee `{request.get('item_id')}`")
    elif route["complete"] and route.get("box"):
        st.success("Ruta leída. Abre la caja, saca las unidades y marca la casilla.", icon=":material/route:")
    ready = route["complete"] and (label_read or not needs_label)
    with st.form(f"count_units_{request_id}", clear_on_submit=False):
        box_ok = True
        if route.get("box"):
            box_ok = st.checkbox(
                f"Confirmo que abrí la {route['box']} y saqué las unidades de ahí ({user.get('full_name') or 'yo'})",
                key=f"box_ok_{request_id}", disabled=not ready,
                help="Es obligatorio: queda registrado a tu nombre quién retiró el producto de la caja.")
        counted = st.number_input(f"Unidades que sacaste (pedido: {needed})", min_value=0,
                                  max_value=max(needed * 5, 1), value=needed, step=1, disabled=not ready,
                                  help="Las unidades no llevan código de barras: cuéntalas y confirma.")
        done = st.form_submit_button("Confirmar unidades y completar picking", type="primary",
                                     disabled=not ready, use_container_width=True)
    if done:
        ok, message, _ = dispatch.confirm_units(storage, request_id, user, counted, box_confirmed=bool(box_ok))
        _notice(ok, message)
        st.rerun()
    c1, c2 = st.columns(2)
    if c1.button("Deshacer última", disabled=not (readings or route["done"]), key=f"undo_pick_{request_id}",
                 use_container_width=True):
        dispatch.undo_reading(user, request_id)
        st.rerun()
    if c2.button("Cerrar panel", key=f"close_pick_{request_id}", use_container_width=True,
                 help="Descarta las lecturas de este panel; el pedido sigue tomado."):
        dispatch.abandon_picking(user, request_id)
        st.rerun()


def _picking_panel(storage, user: dict) -> None:
    if not permissions.has_cap(user, permissions.CAP_PICK):
        return
    request_id = picking.active_request(user["id"])
    if not request_id:
        return
    request = storage.get_service_request(request_id)
    events = storage.get_trace_events(request_id=request_id) if request else []
    if (not request or traceability.request_stage_key(request, events) != traceability.STAGE_CLAIMED
            or dispatch.assigned_picker(events) != user["id"]):
        dispatch.abandon_picking(user, request_id)
        return
    needed = int(request.get("quantity") or 0)
    with st.container(border=True, key="dispatch_picking_panel"):
        section_title(f"Picking · {request.get('item_name')}", icon=":material/barcode_scanner:",
                      caption=f"Lee estantería, piso y contenedor; la caja se marca con una casilla y las unidades se cuentan ({needed} en total). "
                          f"{dispatch.scenario_label(storage)}.")
        if str(request.get("description") or "").strip():
            st.info(f"Nota del cliente: {str(request['description']).strip()}", icon=":material/sticky_note_2:")
        _order_context(storage, request, f"pick_{request_id}")
        _picking_progress(storage, user, request_id, needed)
        scenario = dispatch.SCENARIOS[dispatch.scenario_key(storage)]
        st.caption(f"{scenario['help']} Las lecturas por NFC se suman solas, también desde otra pestaña.")
        with st.form(f"pick_scan_{request_id}", clear_on_submit=True):
            code = st.text_input("Código leído", placeholder=request.get("item_id"))
            add = st.form_submit_button("Registrar lectura", type="primary", use_container_width=True)
        _focus_reading_input("Código leído")
        if add:
            ok, message, _ = dispatch.register_reading(
                storage, request_id, user, code, dispatch.typed_method(storage))
            _notice(ok, message)
            st.rerun()
        _manual_confirmation(storage, user, request)


def _manual_confirmation(storage, user: dict, request: dict) -> None:
    """Alternativa a leer cada unidad: confirmar por escrito, con constancia del nombre y la autorización."""
    request_id = request["id"]
    manual_first = dispatch.scenario_key(storage) == "A"
    with st.expander(":material/edit_note: ¿Sin lector? Confirmar manualmente", expanded=manual_first):
        st.caption(dispatch.manual_confirmation_message(user, request))
        authorized = st.checkbox("Autorizo esta confirmación manual", key=f"manual_auth_{request_id}",
                                 help="Queda registrada con tu nombre y se distingue de una lectura real.")
        note = st.text_input("Nota (opcional)", key=f"manual_note_{request_id}",
                             placeholder="Por qué no se leyó el código")
        if st.button("Confirmar y completar picking", key=f"manual_done_{request_id}", type="primary",
                     disabled=not authorized, use_container_width=True):
            ok, message, _ = dispatch.confirm_manual_picking(storage, request_id, user, authorized, note)
            _notice(ok, message)
            st.rerun()


def _review_action_panel(storage, user: dict) -> None:
    request_id = st.session_state.get("dispatch_review_focus")
    if not request_id:
        return
    request = storage.get_service_request(request_id)
    if not request:
        st.session_state.pop("dispatch_review_focus", None)
        return
    with st.container(border=True, key="dispatch_review_panel"):
        section_title(f"Decisión · {request.get('item_name')}", icon=":material/rule:")
        with st.form(f"review_action_{request_id}"):
            reason = st.text_area("Motivo obligatorio")
            action = st.radio("Acción", ["rework", "shelf"],
                              format_func={"rework": "Volver al picking", "shelf": "Devolver al estante"}.get)
            submit = st.form_submit_button("Aplicar decisión", type="primary", use_container_width=True)
        if submit:
            result = (dispatch.rework_request(storage, request_id, user, reason)
                      if action == "rework" else dispatch.return_to_shelf(storage, request_id, user, reason))
            _notice(result[0], result[1])
            if result[0]:
                st.session_state.pop("dispatch_review_focus", None)
            st.rerun()


def _oversight(storage, user: dict) -> None:
    if not permissions.has_cap(user, permissions.CAP_REASSIGN):
        return
    claimed = dispatch.claimed_orders(storage, user)
    if not claimed:
        return
    with st.expander(f"Reasignar o liberar ({len(claimed)})"):
        choices = {request["id"]: request for request, _ in claimed}
        request_id = st.selectbox("Pedido", list(choices),
                                  format_func=lambda value: f"{choices[value].get('item_name')} · {traceability.short_id(value)}")
        operators = [row for row in storage.get_all_users()
                     if row.get("status") == "active" and row.get("role") in permissions.roles_with(permissions.CAP_PICK)]
        selected = st.selectbox("Nuevo responsable del picking", operators,
                                format_func=lambda row: row.get("full_name"), index=None,
                                placeholder="Selecciona a quién")
        reason = st.text_input("Motivo de reasignación")
        c1, c2 = st.columns(2)
        if c1.button("Reasignar", disabled=selected is None, use_container_width=True):
            ok, message, _ = dispatch.reassign_request(storage, request_id, user, selected, reason)
            _notice(ok, message)
            st.rerun()
        if c2.button("Liberar a la cola", use_container_width=True):
            ok, message, _ = dispatch.release_request(storage, request_id, user, reason)
            _notice(ok, message)
            st.rerun()


def _reports(storage, user: dict) -> None:
    if not permissions.has_cap(user, permissions.CAP_DISPATCH_REPORTS):
        return
    rows = dispatch.dispatch_metrics(storage, user)
    if len(rows) > 200:
        st.caption(f"Se muestran los 200 pedidos más recientes de {len(rows)}; el CSV trae todos.")
    st.dataframe(rows[:200], use_container_width=True, hide_index=True)
    # Los CSV se generan al pulsar el botón (con miles de eventos tardaban segundos en cada recarga).
    st.download_button("Descargar tiempos CSV", lambda: dispatch.dispatch_csv(storage, user),
                       "tiempos-despacho.csv", "text/csv", use_container_width=True)
    st.download_button(
        "Descargar eventos de la lúdica (CSV sin datos personales)", lambda: ludica.events_csv(storage, user),
        f"eventos-{ludica.session_id(storage)}.csv", "text/csv", use_container_width=True,
        help="Columnas: sesion_id, timestamp, estacion, tipo_evento, item_codigo, transaccion_id, "
             "usuario_sim, resultado_sistema. Los identificadores van seudonimizados.",
    )


def _settings(storage, user: dict) -> None:
    """Ajustes del despacho (administrador): el escenario fija cómo se registra lo escrito."""
    if not permissions.has_cap(user, permissions.CAP_DISPATCH_SETTINGS):
        return
    current = dispatch.scenario_key(storage)
    section_title("Escenario de la lúdica", icon=":material/tune:",
                  caption="Lo fija el administrador; quien hace el picking no lo cambia.")
    keys = list(dispatch.SCENARIOS)
    choice = st.selectbox(
        "Escenario", keys, index=keys.index(current), key="dispatch_scenario_choice",
        format_func=lambda key: f"{key} · {dispatch.SCENARIOS[key]['label']}",
    )
    st.caption(f"{dispatch.SCENARIOS[choice]['help']} Método registrado para lo escrito: "
               f"**{dispatch.METHOD_LABELS[dispatch.SCENARIOS[choice]['typed_method']]}**; "
               "las lecturas por chip NFC siempre se registran como NFC.")
    if st.button("Guardar escenario", key="dispatch_scenario_save", type="primary",
                 disabled=choice == current):
        ok, message = dispatch.set_scenario(storage, user, choice)
        _notice(ok, message)
        st.rerun()

    section_title("Ruta obligatoria del picking", icon=":material/route:",
                  caption="El operario debe leer en orden estantería, piso y contenedor antes de sacar las unidades. "
                          "Estantería y piso se exigen solo si están registrados en Inventario → Ubicaciones. "
                          "La caja no se lee: su casilla de confirmación es obligatoria siempre y deja "
                          "registrado quién retiró.")
    route_on = pick_route.required(storage)
    wanted = st.toggle("Exigir la lectura de la ruta en el picking", value=route_on, key="pick_route_toggle")
    if st.button("Guardar ruta obligatoria", key="pick_route_save", type="primary", disabled=wanted == route_on):
        ok, message = pick_route.set_required(storage, user, wanted)
        _notice(ok, message)
        st.rerun()

    section_title("Retiro por autoservicio", icon=":material/outbox:",
                  caption="Qué pueden retirar los clientes desde Buscar sin pasar por Despacho. "
                          "El personal que aprueba solicitudes no se ve limitado.")
    modes = list(loans.SELF_CHECKOUT_MODES)
    saved = loans.self_checkout_mode(storage)
    mode = st.selectbox(
        "Modo", modes, index=modes.index(saved), key="self_checkout_choice",
        format_func=lambda key: loans.SELF_CHECKOUT_MODES[key][0],
    )
    st.caption(loans.SELF_CHECKOUT_MODES[mode][1])
    if st.button("Guardar modo", key="self_checkout_save", type="primary", disabled=mode == saved):
        ok, message = loans.set_self_checkout_mode(storage, user, mode)
        _notice(ok, message)
        st.rerun()


def render() -> None:
    storage, user = st.session_state.storage, st.session_state.user
    if not (permissions.has_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE)
            or permissions.has_cap(user, permissions.CAP_RECEIVE_AND_DELIVER)):
        guard_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE, "Despacho")
        return
    page_header("Despacho", icon=":material/local_shipping:",
                subtitle="Tomar pedido → picking por unidad → recepción → entrega · devoluciones")
    notice = st.session_state.pop("dispatch_notice", None)
    if notice:
        (st.success if notice[0] else st.error)(notice[1])
    # Lo que se está haciendo ahora, arriba: en el teléfono no hay que recorrer la lista.
    _picking_panel(storage, user)
    _review_action_panel(storage, user)
    titles, renderers = [], []
    if permissions.has_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE):
        titles += ["Por tomar", "Mis pedidos"]
        renderers += [_queue_fragment, _mine_fragment]
    if permissions.has_cap(user, permissions.CAP_RECEIVE_AND_DELIVER):
        titles.append("Por revisar")
        renderers.append(_review_fragment)
    if (permissions.has_cap(user, permissions.CAP_VERIFY_RETURN)
            or permissions.has_cap(user, permissions.CAP_STORE_RETURN)):
        titles.append("Devoluciones")
        renderers.append(_returns_fragment)
    if permissions.has_cap(user, permissions.CAP_DISPATCH_REPORTS):
        titles.append("Tiempos")
        renderers.append(_reports)
    if permissions.has_cap(user, permissions.CAP_DISPATCH_SETTINGS):
        titles.append("Ajustes")
        renderers.append(_settings)
    tabs = persistent_tabs(titles, "despacho")
    for tab, renderer in zip(tabs, renderers):
        with tab:
            renderer(storage, user)
    _oversight(storage, user)
