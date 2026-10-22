# -*- coding: utf-8 -*-
"""views/escanear.py - Flujo central: escanear codigo de Contenedor Principal,
Contenedor de Caracteristica o Item Individual; dar salida (checkout) y
registrar reingreso (checkin)."""

import hashlib
from datetime import datetime, timedelta

import streamlit as st

from core import barcode, labels, loans as loans_core, notifications, permissions, returns, search, traceability
from core.labels import ITEM_TYPE_BY_CHOICE, ITEM_TYPE_CHOICES, ITEM_TYPE_HELP, ITEM_TYPE_NAMES
from core.ui import empty_state, esc, guard_cap, load_error, page_header, section_title, show_flash
from views import photo_panel, piece_panel, solicitudes
from views.code_input import render_code_input
from views import ubicaciones
from views.location_guide import render_location_guide


def _pickup_label(request: dict, routes: dict) -> str:
    route = routes.get(request["id"])
    proof = (f"ruta verificada · comprobante {traceability.format_receipt(route.get('receipt'))}"
             if route else "sin ruta verificada")
    return f"Solicitud {traceability.short_id(request['id'])} · {request.get('quantity') or 1} u. · {proof}"


def _render_pickup_notice(pickups: list, routes: dict) -> None:
    """Aviso de que quien escanea tiene una solicitud aprobada de este producto."""
    verified = [request for request in pickups if routes.get(request["id"])]
    if verified:
        st.success(
            f":material/explore: Tienes {len(pickups)} solicitud(es) aprobada(s) de este producto; "
            f"{len(verified)} con ruta verificada. Al confirmar la salida quedará enlazada."
        )
    else:
        st.info(
            f":material/explore: Tienes {len(pickups)} solicitud(es) aprobada(s) de este producto. Al confirmar la salida "
            "quedará enlazada; si quieres, primero recorre la ruta verificable en Trazabilidad."
        )


def _render_label_download(item: dict):
    spec = labels.load_label_spec(st.session_state.storage)
    label_pdf, label_png = labels.item_label_files(item, spec)
    if not label_png:
        return
    pdf_col, png_col = st.columns(2)
    pdf_col.download_button(
        f":material/label: PDF {spec.size_text}", data=label_pdf,
        file_name=f"etiqueta_{item['id']}.pdf", mime="application/pdf",
        help="Formato recomendado: mide exactamente lo mismo que la etiqueta",
        key=f"label_scan_pdf_{item['id']}", type="primary", use_container_width=True,
    )
    png_col.download_button(
        "PNG · solo archivo", data=label_png,
        file_name=f"etiqueta_{item['id']}.png", mime="image/png",
        help="Para guardar o integrar. No lo imprimas desde la app Fotos: la recorta y la "
             "agranda. Para imprimir usa el PDF.",
        key=f"label_scan_png_{item['id']}", use_container_width=True,
    )
    st.caption(
        f"Etiqueta de {spec.describe()}: abre el PDF con Edge o Chrome (Ctrl + P), papel USER de "
        f"{spec.size_text}, escala Predeterminado, sin márgenes."
    )

def _render_item_actions(item: dict, parent: dict = None):
    storage = st.session_state.storage
    user = st.session_state.user

    render_location_guide(item, parent, key_prefix=f"scan_guide_{item['id']}")

    if parent:
        st.caption(f":material/shelves: Pertenece al {ITEM_TYPE_NAMES['master']}: **{parent.get('name')}** (`{parent.get('id')}`)")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Disponibles", item["available"])
    c2.metric("Total propiedad del lab", item.get("quantity", 0))
    c3.metric("Ubicacion", item.get("location") or "N/A")
    c4.metric("Categoria", item.get("category") or "N/A")

    if item.get("description"):
        st.caption(item["description"])

    piece_panel.render(storage, user, item, key_prefix=f"scan_{item['id']}")

    if permissions.has_cap(user, permissions.CAP_REQUEST) and _requestable(item):
        st.markdown("##### :material/send: Pedir este producto")
        st.caption("El laboratorio lo prepara y te avisa cuando esté listo; sigue su avance en Solicitudes → Mis pedidos.")
        solicitudes.request_product_form(storage, user, item, key_prefix=f"scan_{item['id']}", flash_key=_FLASH)

    st.markdown("##### :material/outbox: Retirar yo mismo (salida directa)")
    pickups = traceability.pending_pickups(storage, user, item["id"])
    routes = {request["id"]: traceability.route_event_for(storage, request["id"]) for request in pickups}
    if pickups:
        _render_pickup_notice(pickups, routes)
    mode = loans_core.self_checkout_mode(storage)
    restricted = mode != loans_core.MODE_FREE and loans_core.is_self_checkout_restricted(user)
    must_link = restricted and mode == loans_core.MODE_APPROVED_ONLY
    if restricted and mode == loans_core.MODE_OFF:
        st.info("El retiro por autoservicio está apagado. Pide el producto desde **Solicitudes** y el "
                "laboratorio te lo entrega.", icon=":material/info:")
    elif must_link and not pickups:
        st.info("Para retirar este producto necesitas una solicitud aprobada a tu nombre. "
                "Pídelo desde **Solicitudes**.", icon=":material/info:")
    elif item["available"] <= 0:
        st.warning("No hay unidades disponibles para dar salida en este momento.")
    else:
        with st.form(f"checkout_form_{item['id']}"):
            linked_request = None
            default_qty = 1
            if pickups:
                options = {request["id"]: request for request in pickups}
                choice = st.selectbox(
                    "Solicitud aprobada que retiras", [*options] if must_link else [*options, ""],
                    format_func=lambda rid: _pickup_label(options[rid], routes) if rid else "No vincular con una solicitud",
                    help="La salida queda enlazada a la solicitud en Trazabilidad (evento «retirado»).",
                )
                linked_request = options.get(choice)
                default_qty = int(pickups[0].get("quantity") or 1)
            default_qty = max(1, min(default_qty, int(item["available"])))
            qty = st.number_input("Cantidad", min_value=1, max_value=int(item["available"]), value=default_qty, step=1)
            with_date = st.checkbox("Definir fecha esperada de devolucion")
            expected_date = None
            if with_date:
                expected_date = st.date_input("Devolver antes de", value=datetime.now(loans_core.BOGOTA_TZ).date() + timedelta(days=7))
            notes = st.text_input("Notas (opcional)", placeholder="Motivo de uso, practica, proyecto...")
            submitted = st.form_submit_button(":material/check_circle: Confirmar salida", type="primary", use_container_width=True)

            if submitted:
                expected_dt = None
                if with_date:
                    expected_dt = loans_core.deadline_from_date(expected_date)
                try:
                    ok, msg, loan = loans_core.checkout(
                        storage, item["id"], int(qty), user, expected_dt, notes,
                        request_id=(linked_request or {}).get("id", ""),
                    )
                except PermissionError as exc:
                    ok, msg, loan = False, str(exc), None
                if ok:
                    st.success(msg)
                    if linked_request:
                        # Se muestra tras el rerun (ver render): la salida ya quedó registrada.
                        linked, link_msg, _ = traceability.record_pickup(storage, linked_request, loan, user)
                        st.session_state.scan_trace_notice = (linked, f"{msg} {link_msg}")
                    notifications.send_whatsapp_alert_async(f"📤 Salida: {msg}")
                    st.session_state.scan_result = barcode.scan(storage, item["id"])
                    st.rerun()
                else:
                    st.error(msg)

    st.markdown("##### :material/move_to_inbox: Reingresar (checkin)")
    open_loans = storage.get_open_loans_for_item(item["id"])
    if not permissions.has_cap(user, permissions.CAP_VIEW_ALL_LOANS):
        open_loans = [l for l in open_loans if l["user_id"] == user["id"]]

    if not open_loans:
        st.info("No hay prestamos abiertos de este item para reingresar.")
    else:
        for loan in open_loans:
            overdue_tag = " · :red[:material/warning: **VENCIDO**]" if loans_core.is_overdue(loan) else ""
            with st.container(border=True):
                cc1, cc2 = st.columns([3, 1])
                cc1.write(f"**{loan['user_name']}** ({loan['user_role']}) · {loan['quantity']} u.{overdue_tag}")
                if returns.can_request_return(user, loan):
                    stage = returns.stage(storage, loan["id"])
                    if stage != returns.STAGE_NONE:
                        cc1.caption(returns.STAGE_LABELS[stage])
                    elif cc2.button("Devolver", key=f"return_{loan['id']}", use_container_width=True):
                        ok, msg = returns.request_return(storage, loan["id"], user)
                        (st.success if ok else st.error)(msg)
                        if ok:
                            st.rerun()
                if loans_core.can_checkin_loan(user, loan) and cc2.button(
                        "Reingresar", key=f"checkin_{loan['id']}", use_container_width=True):
                    try:
                        ok, msg = loans_core.checkin(storage, loan["id"], user)
                    except PermissionError as exc:
                        ok, msg = False, str(exc)
                    if ok:
                        st.success(msg)
                        st.session_state.scan_result = barcode.scan(storage, item["id"])
                        st.rerun()
                    else:
                        st.error(msg)


def _render_new_item_wizard(scanned_code: str):
    storage = st.session_state.storage
    user = st.session_state.user

    st.warning(f"El codigo `{scanned_code}` no existe todavia en el inventario.")
    if not barcode.is_valid_code(scanned_code):
        st.caption(f":material/warning: Este codigo no cumple ningun formato valido. {barcode.FORMAT_HELP}")

    if not permissions.has_cap(user, permissions.CAP_EDIT_INVENTORY):
        st.info("Pide a un supervisor o al administrador del laboratorio que registre este item.")
        return

    item_kind = st.radio("¿Qué quieres registrar?", ITEM_TYPE_CHOICES, horizontal=True)
    item_type = ITEM_TYPE_BY_CHOICE[item_kind]
    st.caption(ITEM_TYPE_HELP[item_type])

    # El hash solo forma una clave de widget estable: evita conservar valores de
    # un codigo escaneado anterior cuando cambia el resultado de busqueda.
    code_token = hashlib.sha1(scanned_code.encode("utf-8")).hexdigest()[:10]
    code_to_save = render_code_input(
        storage, item_type, key_prefix=f"scan_new_code_{code_token}", initial_code=scanned_code
    )

    with st.form("new_item_form"):
        name_label = "Nombre / Característica" if item_kind == ITEM_TYPE_NAMES["child"] else "Nombre"
        name_placeholder = "Ej: Resistencias 220 Ω" if item_kind == ITEM_TYPE_NAMES["child"] else None
        name = st.text_input(name_label, placeholder=name_placeholder)
        category = st.text_input("Categoria", placeholder="Electronica, Mecanica, EPP, Herramientas...")
        description = st.text_area("Descripcion", height=80)
        location = st.text_input("Ubicacion fisica", placeholder="Estante 3, Gabinete B...")

        parent_id = ""
        if item_kind == ITEM_TYPE_NAMES["child"]:
            masters = storage.get_all_masters()
            options = {f"{m['name']} ({m['id']})": m["id"] for m in masters}
            if not options:
                st.warning(f"Todavia no hay {ITEM_TYPE_NAMES['master'].lower()}es creados.")
            else:
                choice = st.selectbox(ITEM_TYPE_NAMES["master"], list(options.keys()))
                parent_id = options.get(choice, "")

        quantity, min_alert, loose_pieces = 0, 0, False
        if item_kind != ITEM_TYPE_NAMES["master"]:
            quantity = st.number_input("Cantidad inicial", min_value=0, step=1, value=1)
            min_alert = st.number_input("Umbral de alerta de disponibilidad", min_value=0, step=1, value=0)
        loose_pieces = st.checkbox("Se retiran por piezas (Lego, bloques, kits…)", key="scan_new_pieces",
                                   help="Las personas dicen cuántas piezas se llevan y cuántas devuelven, sin dar de alta cada pieza ni tocar el stock.")

        submitted = st.form_submit_button(":material/save: Registrar item", type="primary", use_container_width=True)

        if submitted:
            code_to_save = (code_to_save or "").strip()
            code_error = None
            if code_to_save:
                try:
                    barcode.validate_code_format(code_to_save)
                except ValueError as exc:
                    code_error = str(exc)

            if not code_to_save:
                st.error("El código es obligatorio.")
            elif code_error:
                st.error(code_error)
            elif storage.item_code_in_use(code_to_save):
                st.error("Ya existe un item con ese código.")
            elif not name:
                st.error("El nombre es obligatorio.")
            elif item_kind == ITEM_TYPE_NAMES["child"] and not parent_id:
                st.error(f"Debes seleccionar un {ITEM_TYPE_NAMES['master']}.")
            else:
                data = {
                    "name": name, "category": category, "description": description,
                    "item_type": item_type, "parent_id": parent_id, "unit": "unidad",
                    "quantity": int(quantity), "location": location,
                    "min_stock_alert": int(min_alert), "status": "active",
                    "created_by": user["institutional_email"], "loose_pieces": "1" if loose_pieces else "",
                }
                try:
                    storage.save_item(data, code_to_save, is_new=True,
                                      actor_email=user["institutional_email"], actor=user)
                    st.success(f"¡'{name}' registrado correctamente con el código {code_to_save}!")
                    st.session_state.scan_result = barcode.scan(storage, code_to_save)
                    st.rerun()
                except Exception as e:
                    load_error("No se pudo registrar el producto", e)


_FLASH = "buscar"
_REQUESTABLE = {"standalone", "child"}
_RESULTS_KEY = "search_results"


def _requestable(item: dict) -> bool:
    return item.get("item_type") in _REQUESTABLE and item.get("status") != "retired"


def _run_search(storage, user: dict, query: str) -> None:
    """Un solo cuadro: un código exacto abre el producto o la ubicación; si no, se busca por nombre y por pedido."""
    query = query.strip()
    st.session_state.pop(_RESULTS_KEY, None)
    result = barcode.scan(storage, query)
    if result["status"] in ("found_item", "found_master", "found_location") or result.get("retired"):
        st.session_state.scan_result = result
        return
    staff = permissions.has_cap(user, permissions.CAP_EDIT_INVENTORY)
    kinds = None if staff else _REQUESTABLE
    items = search.search_items(storage.get_all_items(), query, kinds=kinds)
    requests = []
    if search.looks_like_request_id(query) or not items:
        requests = traceability.search_requests(traceability.visible_requests(storage, user), query)[:search.MAX_RESULTS]
    if len(items) == 1 and not requests:
        st.session_state.scan_result = barcode.scan(storage, items[0]["id"])
        return
    if not items and not requests and staff and result["status"] == "not_found":
        st.session_state.scan_result = result          # código nuevo: se ofrece registrarlo
        return
    st.session_state.scan_result = None
    st.session_state[_RESULTS_KEY] = {"query": query, "items": [item["id"] for item in items],
                                      "requests": [row["id"] for row in requests]}


def _render_results(storage, user: dict) -> bool:
    """Lista de coincidencias de la última búsqueda. Devuelve si había algo que mostrar."""
    found = st.session_state.get(_RESULTS_KEY)
    if not found:
        return False
    items = [item for item in (storage.get_item(code) for code in found["items"]) if item]
    requests = [row for row in (storage.get_service_request(rid) for rid in found["requests"]) if row]
    if not items and not requests:
        empty_state("Sin resultados", f"No encontré nada para «{found['query']}». Prueba con otra palabra, "
                    "con el código completo o con #pedido.", icon=":material/search:")
        return True
    if items:
        section_title(f"Productos y ubicaciones ({len(items)})", icon=":material/inventory_2:")
    for item in items:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1], vertical_alignment="center")
            kind = ITEM_TYPE_NAMES.get(item.get("item_type"), "")
            available = storage.get_available_quantity(item["id"]) if _requestable(item) else None
            stock = "" if available is None else f" · {available} disponible(s)"
            c1.markdown(f"<b>{esc(item.get('name'))}</b><br><span class=\"lab-section__caption\">"
                        f"{esc(item['id'])} · {esc(kind)}{esc(stock)} · {esc(item.get('location') or 'sin ubicación')}</span>",
                        unsafe_allow_html=True)
            if c2.button("Abrir", key=f"open_{item['id']}", type="primary", use_container_width=True):
                st.session_state.scan_result = barcode.scan(storage, item["id"])
                st.rerun()
    if requests:
        section_title(f"Pedidos ({len(requests)})", icon=":material/timeline:")
        for row in requests:
            solicitudes.render_request_status(storage, user, row, key_prefix="found")
    return True


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    if not guard_cap(user, permissions.CAP_REQUEST, "Buscar"):
        return

    page_header("Buscar", icon=":material/search:",
                subtitle="Escanea un código o escribe el nombre de un producto, una ubicación o un pedido")
    show_flash(_FLASH)

    with st.form("scan_form", clear_on_submit=True):
        code = st.text_input("Buscar", placeholder="Escanea el código o escribe un nombre (ej. «microscopio»)")
        submitted = st.form_submit_button(":material/search: Buscar", type="primary", use_container_width=True)
        if submitted and code.strip():
            _run_search(storage, user, code)
        elif submitted:
            st.warning("Escribe o escanea algo para buscar.")
    with st.expander("Formatos de código aceptados"):
        st.caption(barcode.FORMAT_HELP)

    result = st.session_state.get("scan_result")
    st.markdown("---")

    notice = st.session_state.pop("scan_trace_notice", None)
    if notice:
        linked, message = notice
        (st.success if linked else st.warning)(f":material/explore: {message}")

    if not result:
        if not _render_results(storage, user):
            st.caption("Esperando búsqueda… Escanea con tu lector USB o escribe lo que buscas.")
        return
    if st.session_state.get(_RESULTS_KEY) and st.button(":material/arrow_back: Volver a los resultados", key="back_results"):
        st.session_state.scan_result = None
        st.rerun()

    if result["status"] == "error":
        st.error(result["message"])
        if result.get("retired"):
            st.caption("Su código quedó libre: puedes registrarlo de nuevo como un item nuevo.")
            _render_new_item_wizard(result["barcode"])
    elif result["status"] == "found_location" or (result["status"] == "not_found" and result.get("place")):
        ubicaciones.render_scan_result(
            storage, st.session_state.user, result, labels.load_label_spec(storage), render_label=_render_label_download,
        )
    elif result["status"] == "not_found":
        _render_new_item_wizard(result["barcode"])
    elif result["status"] == "found_master":
        item = result["item"]
        st.success(f":material/shelves: {ITEM_TYPE_NAMES['master']}: **{item['name']}** (`{item['id']}`)")
        if result.get("parsed"):
            st.caption(f":material/menu_book: {barcode.describe_parsed(result['parsed'])}")
        if item.get("description"):
            st.caption(item["description"])
        st.caption(f"Ubicacion: {item.get('location') or 'N/A'}")
        photo_panel.render_scan_photo(item)
        _render_label_download(item)
        render_location_guide(item, key_prefix=f"scan_master_guide_{item['id']}")
        children = result["children"]
        if not children:
            st.info(f"Este {ITEM_TYPE_NAMES['master']} todavía no tiene {ITEM_TYPE_NAMES['child']}s registrados dentro.")
        else:
            for child in children:
                with st.expander(f"{child['name']} — {child['available']} disponibles"):
                    photo_panel.render_scan_photo(child, compact=True)
                    _render_item_actions(child, parent=item)
    elif result["status"] == "found_item":
        item = result["item"]
        st.success(f":material/check: Item encontrado: **{item['name']}** (`{item['id']}`)")
        if result.get("parsed"):
            st.caption(f":material/menu_book: {barcode.describe_parsed(result['parsed'])}")
        photo_panel.render_scan_photo(item)
        _render_label_download(item)
        _render_item_actions(item, parent=result.get("parent"))
