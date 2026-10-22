# -*- coding: utf-8 -*-
"""Componentes móviles de ubicación.

- `render_location_guide`: guía estática hacia un producto (puntos de control y
  etiquetas que encontrará en el camino).
- `render_verifiable_route`: ruta de retiro de una solicitud aprobada en la que
  cada punto con etiqueta se confirma escaneándola (lector USB = teclado) o
  escribiendo su código. El avance vive en la sesión; al completarla se guarda
  UN evento de trazabilidad con un comprobante verificable.
- `render_route_record`: lo que quedó registrado de una ruta ya verificada.
"""

import streamlit as st

from core import location, nfc, service_requests, traceability
from core.ui import badge_html, material, section_title, stat_cards, timeline

_CRUMB_TONES = {"done": "success", "inferred": "warning", "current": "info", "pending": "neutral"}
_FEEDBACK = {"success": st.success, "warning": st.warning, "info": st.info, "danger": st.error}


def _label_names(storage, checkpoints: list) -> dict:
    """Nombres registrados de las etiquetas del camino (contenedor, caja…)."""
    names = {}
    if storage is None:
        return names
    for point in checkpoints:
        code = point.get("label_code")
        if not code or code in names:
            continue
        try:
            found = storage.get_item(code)
        except Exception:  # la guía nunca debe romper la página
            found = None
        if found and found.get("name"):
            names[code] = found["name"]
    return names


def _registered_places(storage, item: dict, parent: dict = None) -> dict:
    """{código: nombre} de las estanterías, pisos, mesas o zonas del camino que
    tienen etiqueta (ubicaciones registradas y activas)."""
    found = {}
    if storage is None:
        return found
    for code in location.place_codes(item, parent):
        try:
            place = storage.get_item(code)
        except Exception:  # la guía nunca debe romper la página
            place = None
        if place and place.get("item_type") == "location" and place.get("status") != "retired":
            found[code] = place.get("name") or ""
    return found


def _route_checkpoints(storage, item: dict, parent: dict = None) -> tuple:
    """(puntos de control, ubicaciones con etiqueta): los puntos de estantería,
    piso o mesa llevan etiqueta solo si esa ubicación está registrada."""
    places = _registered_places(storage, item, parent)
    checkpoints = location.build_route_checkpoints(item, parent, places, places)
    names = {**places, **_label_names(storage, checkpoints)}
    if any(names.get(p.get("label_code")) for p in checkpoints):
        checkpoints = location.build_route_checkpoints(item, parent, names, places)
    return checkpoints, places


def _checkpoints_for(storage, item: dict, parent: dict = None) -> list:
    return _route_checkpoints(storage, item, parent)[0]


def compact_route_for(storage, item: dict, parent: dict = None) -> str:
    """Ruta corta (E2 › P1 › C01 › CJ01 › I001) de un producto, con el vocabulario de la etiqueta."""
    return location.compact_route(_checkpoints_for(storage, item, parent))


def _breadcrumb(crumbs: list) -> None:
    """Ruta compacta (E2 › P1 › C01 › CJ01 › I001) como pastillas de estado."""
    html = " › ".join(badge_html(text, _CRUMB_TONES.get(state, "neutral")) for text, state in crumbs)
    st.markdown(f'<div class="lab-route">{html}</div>', unsafe_allow_html=True)


def _static_route(checkpoints: list, item: dict, parent: dict = None) -> None:
    _breadcrumb([(point["short"], "pending") for point in checkpoints])
    labels = len([point for point in checkpoints if point.get("label_code")])
    st.caption(
        f"{len(checkpoints)} puntos · {labels} con etiqueta :material/label:. Con una solicitud aprobada podrás "
        "confirmar cada etiqueta escaneándola y obtener un comprobante de tu recorrido."
    )
    timeline(traceability.checkpoint_steps(checkpoints))
    location_text = str((item or {}).get("location") or (parent or {}).get("location") or "").strip()
    if location_text:
        st.caption(f"Referencia registrada: {location_text}")


def render_location_guide(item: dict, parent: dict = None, key_prefix: str = "location") -> None:
    """Guía estática (sin registrar nada). `key_prefix` se conserva por compatibilidad."""
    storage = st.session_state.get("storage")
    checkpoints = _checkpoints_for(storage, item, parent)
    name = (item or {}).get("name") or "Producto"
    with st.expander(f":material/explore: Cómo llegar a {name}", expanded=False):
        _static_route(checkpoints, item, parent)


# ---------------------------------------------------------------------------
# Ruta verificable
# ---------------------------------------------------------------------------

def _state_key(request_id: str) -> str:
    return f"trace_route_{request_id}"


def _feedback_key(request_id: str) -> str:
    return f"trace_route_msg_{request_id}"


def _load_route(storage, request: dict) -> dict:
    key = _state_key(request["id"])
    route = st.session_state.get(key)
    if not route or route.get("item_id") != request.get("item_id"):
        item = storage.get_item(request.get("item_id")) or {
            "id": request.get("item_id"), "name": request.get("item_name"),
        }
        parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
        checkpoints, places = _route_checkpoints(storage, item, parent)
        names = {**places, **_label_names(storage, checkpoints)}
        route = traceability.new_route(item, parent, request, names, places)
        st.session_state[key] = route
    return route


def _save(storage, request: dict, user: dict, route: dict, result: dict) -> None:
    """Guarda la ruta (una sola escritura) y anexa el resultado al mensaje."""
    try:
        ok, message, _ = traceability.save_route(storage, request, route, user)
    except Exception as exc:  # E/S o red: el avance sigue en la sesión
        result.update(tone="danger", message=f"{result['message']} No se pudo guardar ({exc}); "
                                             "tu avance sigue aquí, inténtalo de nuevo.")
        return
    result.update(tone="success" if ok else "danger", message=f"{result['message']} {message}")
    if ok:
        st.session_state.pop(_state_key(request["id"]), None)


def _on_scan(storage, request: dict, user: dict, input_key: str) -> None:
    route = st.session_state.get(_state_key(request["id"]))
    if not route:
        return
    code = str(st.session_state.get(input_key) or "").strip()
    known = None
    if code:
        try:
            known = storage.get_item(code)
        except Exception:
            known = None
    result = traceability.apply_code(route, code, known_item=known)
    if result["ok"] and result["completed"] and traceability.route_stats(route)["full"]:
        _save(storage, request, user, route, result)
    st.session_state[_feedback_key(request["id"])] = result


def _on_arrival(storage, request: dict, user: dict, input_key: str) -> None:
    if str(st.session_state.get(input_key) or "").strip():
        _on_scan(storage, request, user, input_key)  # escribió un código: se trata como lectura
        return
    route = st.session_state.get(_state_key(request["id"]))
    if route:
        st.session_state[_feedback_key(request["id"])] = traceability.confirm_arrival(route)


def _show_feedback(result: dict) -> None:
    if result:
        _FEEDBACK.get(result.get("tone"), st.info)(result.get("message", ""))


def _page_link(page_key: str, label: str, icon: str) -> None:
    page = (st.session_state.get("pages") or {}).get(page_key)
    if page is not None:
        st.page_link(page, label=label, icon=material(icon), use_container_width=True)


def render_route_record(event: dict, show_next_step: bool = False) -> None:
    """Resumen chequeable de una ruta guardada: comprobante, etiquetas, duración
    y cada punto con su hora."""
    info = traceability.event_details(event)
    labels, scanned = int(info.get("labels") or 0), int(info.get("scanned") or 0)
    stat_cards([
        {"label": "Comprobante de ruta", "value": traceability.format_receipt(event.get("receipt")),
         "icon": "🔏", "tone": "success", "help": "Muéstralo al retirar el producto"},
        {"label": "Etiquetas escaneadas", "value": f"{scanned}/{labels}", "icon": "🏷️",
         "tone": "success" if scanned == labels else "warning",
         "help": "Completa" if scanned == labels else "Algunas quedaron inferidas"},
        {"label": "Duración del recorrido", "value": traceability.fmt_duration(info.get("duration_s")) or "—",
         "icon": "⏱️"},
    ])
    caption = (f"Ruta {info.get('route') or ''} · verificada el "
               f"{traceability.fmt_local(event.get('created_at'))} por {event.get('actor_name') or 'el solicitante'}")
    if info.get("attempts"):
        caption += f" · {info['attempts']} lectura(s) equivocada(s) en el camino"
    st.caption(caption)
    timeline(traceability.checkpoint_steps(info.get("checkpoints") or []))
    if show_next_step:
        st.info("Siguiente paso: en **Buscar**, escanea el producto y confirma la salida con tu usuario; "
                "quedará enlazada a esta solicitud.")
        _page_link("escanear", "Ir a Buscar", "🔎")


def render_verifiable_route(storage, request: dict, user: dict, key_prefix: str = "route") -> None:
    """Ruta de retiro verificable de `request` para `user` (móvil primero)."""
    request_id = request["id"]
    saved = traceability.route_event_for(storage, request_id)
    feedback = st.session_state.pop(_feedback_key(request_id), None)
    if saved:
        _show_feedback(feedback)
        picked = traceability.first_event(
            storage.get_trace_events(request_id=request_id, event_type=traceability.EVENT_PICKED_UP),
            traceability.EVENT_PICKED_UP,
        )
        render_route_record(saved, show_next_step=not picked and request.get("requester_id") == user.get("id"))
        return
    if request.get("request_type") != service_requests.TYPE_PRODUCT:
        return

    if request.get("status") != service_requests.STATUS_APPROVED or request.get("requester_id") != user.get("id"):
        item = storage.get_item(request.get("item_id")) or {
            "id": request.get("item_id"), "name": request.get("item_name"),
        }
        parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
        _static_route(_checkpoints_for(storage, item, parent), item, parent)
        if request.get("status") == service_requests.STATUS_PENDING:
            st.caption("Podrás verificar la ruta cuando tu solicitud sea aprobada.")
        return

    route = _load_route(storage, request)
    # Toques de chips NFC (en esta u otra pestaña) confirman sus puntos de control.
    tapped = nfc.merge_user_taps(storage, user, request, route)
    if tapped:
        if tapped["completed"] and traceability.route_stats(route)["full"]:
            _save(storage, request, user, route, tapped)
            st.session_state[_feedback_key(request_id)] = tapped
            st.rerun()
        feedback = tapped
    stats = traceability.route_stats(route)
    index = traceability.current_index(route)
    _breadcrumb(traceability.breadcrumb(route))
    st.progress(
        stats["progress"],
        text=f"{stats['done']} de {stats['total']} puntos · {stats['scanned']} de {stats['labels']} etiquetas",
    )

    if index is not None:
        point = route["checkpoints"][index]
        title = f"{point['title']} · {point['name']}" if point.get("name") and point["name"] != point["title"] else point["title"]
        section_title(f"Paso {index + 1} de {stats['total']}: {title}", icon=":material/location_on:", caption=point["hint"])
    elif not stats["full"]:
        st.warning(
            f"Llegaste al producto, pero {stats['inferred']} etiqueta(s) quedaron sin escanear. "
            "Escanéalas ahora para una verificación completa, o guarda la ruta así."
        )
    _show_feedback(feedback)

    if index is not None or not stats["full"]:
        input_key = f"{key_prefix}_code_{request_id}"
        with st.form(f"{key_prefix}_form_{request_id}", clear_on_submit=True):
            st.text_input(
                "Código de la etiqueta", key=input_key,
                placeholder="Escanea con el lector o escribe el código",
                help="El lector USB escribe el código y presiona Enter por ti.",
            )
            args = (storage, request, user, input_key)
            needs_label = index is None or bool(route["checkpoints"][index].get("label_code"))
            if needs_label:
                st.form_submit_button(":material/check_circle: Confirmar etiqueta", type="primary", use_container_width=True,
                                      on_click=_on_scan, args=args)
            else:
                # El primer botón es el que dispara Enter (lector USB): confirmar etiqueta.
                scan_col, arrive_col = st.columns(2)
                scan_col.form_submit_button(":material/label: Confirmar etiqueta", use_container_width=True,
                                            on_click=_on_scan, args=args)
                arrive_col.form_submit_button(":material/location_on: Ya estoy aquí", type="primary", use_container_width=True,
                                              on_click=_on_arrival, args=args)

    if index is None and not stats["full"]:
        if st.button(":material/save: Guardar ruta con verificación parcial", key=f"{key_prefix}_partial_{request_id}",
                     use_container_width=True):
            result = {"ok": True, "tone": "success", "message": "", "completed": True}
            _save(storage, request, user, route, result)
            st.session_state[_feedback_key(request_id)] = result
            st.rerun()

    timeline(traceability.route_timeline(route))
    if stats["done"] and st.button("↺ Reiniciar ruta", key=f"{key_prefix}_reset_{request_id}"):
        st.session_state.pop(_state_key(request_id), None)
        st.rerun()
