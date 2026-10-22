# -*- coding: utf-8 -*-
"""Flujo de despacho de solicitudes de producto.

La interfaz llama estas funciones; la autorización y las transiciones vuelven a
validarse atómicamente en :meth:`LabStorage.transition_request`.
"""

import csv
import hashlib
import io
import logging
import threading
from datetime import datetime, timezone

from core import permissions, pick_route, picking, traceability
from core.config import safe_secret

logger = logging.getLogger(__name__)


def allow_self_approval() -> bool:
    """ALLOW_SELF_APPROVAL (Secrets, apagado por defecto): permite que quien revisa
    apruebe una solicitud suya. Útil en una demostración con una sola persona."""
    return str(safe_secret("ALLOW_SELF_APPROVAL", "false") or "").strip().lower() in {
        "1", "true", "yes", "si", "sí",
    }


# Cómo llegó de verdad cada lectura del picking. NFC lo fija el propio flujo del
# toque (core/nfc + views/nfc_tap); lo escrito en el formulario lo fija el
# escenario elegido en Ajustes, no un selector libre de quien hace el picking.
METHOD_BARCODE_GUN = "barcode_gun"
METHOD_CAMERA = "camera"
METHOD_NFC = "nfc"
METHOD_MANUAL = "manual"
METHOD_COUNT = "count"        # unidades sacadas de la caja: no llevan código, el operario las cuenta y confirma
METHODS = (METHOD_BARCODE_GUN, METHOD_CAMERA, METHOD_NFC, METHOD_MANUAL, METHOD_COUNT)
METHOD_LABELS = {
    METHOD_BARCODE_GUN: "Pistola de barras", METHOD_CAMERA: "Cámara del teléfono",
    METHOD_NFC: "Chip NFC", METHOD_MANUAL: "Manual", METHOD_COUNT: "Conteo de unidades",
}
_LEGACY_METHODS = {"barcode": METHOD_BARCODE_GUN, "teclado": METHOD_MANUAL}

# Escenarios de la lúdica (A/B/C); se fijan en Ajustes (hoja `settings`).
SETTING_SCENARIO = "dispatch_scenario"
DEFAULT_SCENARIO = "B"
SCENARIOS = {
    "A": {"label": "Manual", "typed_method": METHOD_MANUAL,
          "help": "Las lecturas se escriben con el teclado."},
    "B": {"label": "Programa + pistola de barras", "typed_method": METHOD_BARCODE_GUN,
          "help": "Las lecturas llegan de la pistola (el campo recibe el código)."},
    "C": {"label": "Programa + chip NFC", "typed_method": METHOD_MANUAL,
          "help": "Se tocan los chips de estantería, piso y contenedor; el teclado queda como respaldo."},
}


def normalize_method(value) -> str:
    """Método válido (los nombres antiguos se convierten; lo desconocido es ``""``)."""
    text = str(value or "").strip().lower()
    text = _LEGACY_METHODS.get(text, text)
    return text if text in METHODS else ""


def scenario_key(storage) -> str:
    try:
        value = str(storage.get_setting(SETTING_SCENARIO, DEFAULT_SCENARIO) or "").strip().upper()
    except Exception:  # un ajuste ilegible nunca debe frenar el picking
        value = ""
    return value if value in SCENARIOS else DEFAULT_SCENARIO


def scenario_label(storage) -> str:
    key = scenario_key(storage)
    return f"Escenario {key} · {SCENARIOS[key]['label']}"


def typed_method(storage) -> str:
    """Método que se registra para lo escrito/leído en el formulario de picking."""
    return SCENARIOS[scenario_key(storage)]["typed_method"]


def set_scenario(storage, actor: dict, key: str):
    """Fija el escenario (solo administrador). Devuelve (ok, mensaje)."""
    permissions.require_cap(actor, permissions.CAP_DISPATCH_SETTINGS,
                            "Solo un administrador cambia el escenario de despacho.")
    key = str(key or "").strip().upper()
    if key not in SCENARIOS:
        return False, "Elige el escenario A, B o C."
    storage.set_setting(SETTING_SCENARIO, key, (actor or {}).get("institutional_email", ""))
    return True, f"Escenario fijado: {key} · {SCENARIOS[key]['label']}."


def _details(event: dict) -> dict:
    return traceability.event_details(event)


def _latest(events: list, kind: str):
    return next((row for row in reversed(events or []) if row.get("event_type") == kind), None)


def authorized_pickers(events: list) -> list:
    """IDs autorizados; lista vacía significa todos los operarios activos."""
    event = _latest(events, traceability.EVENT_ORDER_AUTHORIZED)
    values = _details(event or {}).get("authorized_pickers", [])
    return [str(value) for value in values] if isinstance(values, list) else []


def assigned_picker(events: list) -> str:
    assigned = ""
    for event in events or []:
        kind = event.get("event_type")
        if kind in {traceability.EVENT_ORDER_CLAIMED, traceability.EVENT_ORDER_REASSIGNED}:
            assigned = str(_details(event).get("picker_id") or event.get("actor_id") or "")
        elif kind == traceability.EVENT_ORDER_RELEASED:
            assigned = ""
    return assigned


def assigned_picker_name(events: list) -> str:
    name = ""
    for event in events or []:
        kind = event.get("event_type")
        if kind in {traceability.EVENT_ORDER_CLAIMED, traceability.EVENT_ORDER_REASSIGNED}:
            name = str(_details(event).get("picker_name") or event.get("actor_name") or "")
        elif kind == traceability.EVENT_ORDER_RELEASED:
            name = ""
    return name


def is_authorized(events: list) -> bool:
    return _latest(events, traceability.EVENT_ORDER_AUTHORIZED) is not None


def _picker_list(values) -> list:
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({str(value).strip() for value in values if str(value or "").strip()})


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def plan_transition(request: dict, events: list, actor: dict, event_type: str,
                    details: dict = None, available=None, open_loan: dict = None) -> dict:
    """Valida una transición SIN efectos y describe lo que debe escribirse.

    El almacenamiento la invoca dentro de su sección crítica con datos leídos
    bajo el mismo bloqueo y aplica el plan en una sola escritura. Claves:
    ``ok``, ``message``, ``status`` (nuevo estado de la solicitud o None),
    ``loan_action`` (None, "create", "reuse" o "close"), ``loan_id``,
    ``details`` y ``extra_events``. Si falla: ``failure_kind`` y
    ``failure_details`` para el evento de auditoría.
    """
    request, actor = request or {}, actor or {}
    events = list(events or [])
    details = dict(details or {})
    stage = traceability.request_stage_key(request, events)

    def fail(message, kind=traceability.EVENT_ORDER_ACTION_FAILED, **extra):
        return {
            "ok": False, "message": message, "failure_kind": kind,
            "failure_details": {"attempted_event": event_type, "reason": message,
                                "stage": stage, **extra},
        }

    capability = traceability.EVENT_CAPABILITIES.get(event_type)
    if capability and not permissions.has_cap(actor, capability):
        return fail("No tienes permiso para realizar esta transición.")
    target = traceability.transition_target(stage, event_type)
    if target is None:
        if event_type == traceability.EVENT_ORDER_CLAIMED and stage == traceability.STAGE_CLAIMED:
            who = assigned_picker_name(events) or "otro operario"
            return fail(f"Este pedido ya lo tomó {who}.")
        label = traceability.EVENT_LABELS.get(event_type, event_type)
        return fail(f"«{label}» no es válido cuando la solicitud está en «{traceability.STAGES[stage][0]}».")

    is_product = request.get("request_type") == "product"
    quantity = int(request.get("quantity") or 0)
    owner = bool(actor.get("id")) and request.get("requester_id") == actor.get("id")
    assigned = assigned_picker(events)
    plan = {
        "ok": True, "message": traceability.EVENT_LABELS.get(event_type, "Transición registrada."),
        "status": None, "loan_action": None, "loan_id": "", "details": details,
        "extra_events": [], "stage": target,
    }

    if event_type == traceability.EVENT_ORDER_CANCELLED:
        if owner:
            if not permissions.has_cap(actor, permissions.CAP_CANCEL_OWN_REQUEST):
                return fail("No tienes permiso para cancelar esta solicitud.")
            if stage != traceability.STAGE_REQUESTED:
                return fail("Solo puedes cancelar tu solicitud mientras está en revisión.")
        elif not permissions.has_cap(actor, permissions.CAP_APPROVE):
            return fail("No tienes permiso para cancelar esta solicitud.")
        plan["status"] = "cancelled"
    elif event_type in {traceability.EVENT_ORDER_APPROVED, traceability.EVENT_ORDER_REJECTED}:
        approved = event_type == traceability.EVENT_ORDER_APPROVED
        if approved and owner and not allow_self_approval():
            return fail("No puedes aprobar tu propia solicitud: la revisa otra persona.")
        pickers = _picker_list(details.pop("authorized_pickers", None))
        if approved and is_product:
            if available is None or quantity > available:
                return fail(f"Ya no hay stock suficiente. Disponible: {available or 0}.")
            plan["extra_events"].append(
                (traceability.EVENT_ORDER_AUTHORIZED, {"authorized_pickers": pickers})
            )
        plan["status"] = "approved" if approved else "rejected"
    elif event_type == traceability.EVENT_ORDER_AUTHORIZED:
        if not is_product:
            return fail("Solo las solicitudes de producto pasan por despacho.")
        details["authorized_pickers"] = _picker_list(details.get("authorized_pickers"))
    elif event_type == traceability.EVENT_ORDER_CLAIMED:
        if not is_authorized(events):
            return fail("El pedido todavía no tiene autorización de despacho.")
        if traceability.first_event(events, traceability.EVENT_PICKED_UP):
            return fail("El solicitante ya retiró este pedido por autoservicio.")
        # Cualquier operario (o supervisor) puede tomar un pedido autorizado: ya no se asigna a quién.
        details["picker_id"] = actor.get("id", "")
        details["picker_name"] = actor.get("full_name", "")
    elif event_type == traceability.EVENT_ORDER_RELEASED:
        if assigned != actor.get("id") and not permissions.has_cap(actor, permissions.CAP_REASSIGN):
            return fail("Solo quien tomó el pedido o un supervisor puede liberarlo.")
        details["picker_id"] = assigned
        details["reason"] = _clean(details.get("reason"))
    elif event_type == traceability.EVENT_ORDER_REASSIGNED:
        picker_id = str(details.get("picker_id") or "")
        if not picker_id:
            return fail("Selecciona un operario activo.")
        if picker_id == assigned:
            return fail("El pedido ya está asignado a esa persona.")
        details["previous_picker_id"] = assigned
        details["reason"] = _clean(details.get("reason"))
    elif event_type == traceability.EVENT_ORDER_PICKED:
        if assigned != actor.get("id"):
            return fail("El pedido está asignado a otro operario.")
        readings = details.get("readings") or []
        if len(readings) != quantity:
            return fail(f"Faltan lecturas: se requieren {quantity} y hay {len(readings)}.")
        if any(not normalize_method(row.get("method")) for row in readings):
            return fail("Una lectura trae un método no válido.")
        wrong = next((row for row in readings if row.get("code") != request.get("item_id")), None)
        if wrong:
            return fail("Una lectura no corresponde al producto solicitado.",
                        kind=traceability.EVENT_ORDER_SCAN_FAILED,
                        scanned_code=str(wrong.get("code") or ""))
        if open_loan:
            # Re-picking tras «devuelto al picking»: las unidades ya salieron del
            # inventario con este préstamo; no se descuentan dos veces.
            plan["loan_action"], plan["loan_id"] = "reuse", str(open_loan.get("id") or "")
            details["reused_loan"] = True
        else:
            if available is None or quantity > available:
                return fail(f"Stock insuficiente. Disponible: {available or 0}.")
            plan["loan_action"] = "create"
        details["quantity"] = quantity
        counts = {}
        for row in readings:
            counts[normalize_method(row["method"])] = counts.get(normalize_method(row["method"]), 0) + 1
        details["methods"] = counts
        details["method"] = next(iter(counts)) if len(counts) == 1 else "mixed"
        failed_scans = list(details.pop("failed_scans", None) or [])
        if failed_scans:
            # Todas las lecturas rechazadas del picking, en UN solo evento y en la
            # misma escritura que el «picking completo».
            plan["extra_events"].append((traceability.EVENT_ORDER_SCAN_FAILED,
                                         {"scans": failed_scans, "count": len(failed_scans)}))
    elif event_type == traceability.EVENT_ORDER_DELIVERED:
        if not open_loan:
            return fail("No hay un préstamo abierto de este picking para entregar.")
        plan["loan_id"] = str(open_loan.get("id") or "")
        details["notes"] = _clean(details.get("notes"))
    elif event_type == traceability.EVENT_ORDER_RECEIVED:
        details["notes"] = _clean(details.get("notes"))
    elif event_type == traceability.EVENT_ORDER_REWORK:
        details["reason"] = _clean(details.get("reason"))
        if not details["reason"]:
            return fail("Indica qué debe corregirse en el picking.")
    elif event_type == traceability.EVENT_ORDER_RETURNED_TO_SHELF:
        details["reason"] = _clean(details.get("reason"))
        if not details["reason"]:
            return fail("Indica por qué se devuelve al estante.")
        if not open_loan:
            return fail("No existe un préstamo abierto para devolver al estante.")
        plan["loan_action"], plan["loan_id"] = "close", str(open_loan.get("id") or "")
    elif event_type == traceability.EVENT_ORDER_CLIENT_CONFIRMED:
        if not owner:
            return fail("Solo el solicitante puede confirmar la recepción.")
    return plan


# --- Listas de Despacho: solo etapas abiertas, calculadas una vez por versión de datos ---------
#
# Cada operario y cada supervisor refresca sus listas cada pocos segundos. Recorrer
# todas las solicitudes aprobadas (con su historial de eventos) en cada refresco y
# para cada persona crecía con el historial de la lúdica; ahora se calcula UNA vez
# por versión de la base (data_state) y se comparte entre sesiones y listas. Los
# pedidos que ya salieron de las listas (entregados, confirmados, devueltos al
# estante) no vuelven a abrirse: se recuerdan por id y se excluyen sin leerlos.

OPEN_STAGES = frozenset({
    traceability.STAGE_APPROVED, traceability.STAGE_CLAIMED,
    traceability.STAGE_PICKED, traceability.STAGE_RECEIVED,
})
_CLOSED_FOR_LISTS = frozenset({
    traceability.STAGE_DELIVERED, traceability.STAGE_CONFIRMED, traceability.STAGE_RETURNED,
})

_orders_lock = threading.Lock()
_orders_cache = {"generation": None, "version": None, "rows": [], "closed": set()}


def reset_orders_cache() -> None:
    """Olvida lo calculado (pruebas)."""
    with _orders_lock:
        _orders_cache.update(generation=None, version=None, rows=[], closed=set())


def _data_state(storage):
    reader = getattr(storage, "get_data_state", None)
    try:
        return reader() if reader else None
    except Exception:
        return None


def _compute_open_orders(storage, closed: set) -> list:
    rows = []
    for request in storage.get_service_requests(status="approved", exclude_ids=closed):
        if request.get("request_type") != "product":
            continue
        events = storage.get_trace_events(request_id=request["id"])
        stage = traceability.request_stage_key(request, events)
        if stage in OPEN_STAGES:
            rows.append({"request": request, "events": events, "stage": stage})
        elif stage in _CLOSED_FOR_LISTS:
            closed.add(request["id"])
    return rows


def open_orders(storage) -> list:
    """Pedidos de producto aprobados que siguen abiertos: ``[{request, events, stage}]``.

    Sin ``get_data_state`` (almacenamientos de prueba) se calcula en cada llamada."""
    state = _data_state(storage)
    if state is None:
        return _compute_open_orders(storage, set())
    with _orders_lock:
        if _orders_cache["generation"] != state.get("generation"):
            _orders_cache.update(generation=state.get("generation"), version=None, rows=[], closed=set())
        if _orders_cache["version"] != state.get("version"):
            _orders_cache["rows"] = _compute_open_orders(storage, _orders_cache["closed"])
            _orders_cache["version"] = state.get("version")
        rows = _orders_cache["rows"]
    return [{"request": dict(row["request"]), "events": row["events"], "stage": row["stage"]} for row in rows]


def queue_for(storage, actor: dict) -> list:
    """Pedidos aprobados y autorizados que ``actor`` puede tomar (todos: cualquiera elige el que despacha)."""
    permissions.require_cap(actor, permissions.CAP_VIEW_DISPATCH_QUEUE)
    rows = []
    for order in open_orders(storage):
        if order["stage"] != traceability.STAGE_APPROVED or not is_authorized(order["events"]):
            continue
        rows.append(order["request"])
    return rows


def pending_authorization(storage, actor: dict) -> list:
    """Aprobadas sin autorización (p. ej. anteriores a v1.15) para el supervisor."""
    permissions.require_cap(actor, permissions.CAP_AUTHORIZE_DISPATCH)
    rows = []
    for order in open_orders(storage):
        if order["stage"] != traceability.STAGE_APPROVED:
            continue
        legacy = {event.get("event_type") for event in order["events"]} & {
            traceability.EVENT_PICKED_UP, traceability.EVENT_ROUTE_VERIFIED,
        }
        if not is_authorized(order["events"]) and not legacy:
            rows.append(order["request"])
    return rows


def my_history(storage, actor: dict, limit: int = 20) -> list:
    """Resumen de lo que ``actor`` tomó, sin datos de otros operarios."""
    permissions.require_cap(actor, permissions.CAP_VIEW_DISPATCH_QUEUE)
    rows = []
    for request in storage.get_service_requests():
        events = storage.get_trace_events(request_id=request["id"])
        mine = [event for event in events if event.get("actor_id") == actor.get("id")
                and event.get("event_type") in {
                    traceability.EVENT_ORDER_CLAIMED, traceability.EVENT_ORDER_RELEASED,
                    traceability.EVENT_ORDER_PICKED,
                }]
        if not mine:
            continue
        rows.append({
            "request_id": request["id"], "short_id": traceability.short_id(request["id"]),
            "item_name": request.get("item_name", ""), "quantity": request.get("quantity", 0),
            "last_action": traceability.EVENT_LABELS.get(mine[-1].get("event_type"), ""),
            "at": mine[-1].get("created_at", ""),
            "stage": traceability.STAGES[traceability.request_stage_key(request, events)][0],
        })
    rows.sort(key=lambda row: str(row["at"]), reverse=True)
    return rows[:max(1, int(limit))]


def my_orders(storage, actor: dict) -> list:
    permissions.require_cap(actor, permissions.CAP_VIEW_DISPATCH_QUEUE)
    return [
        order["request"] for order in open_orders(storage)
        if order["stage"] in {traceability.STAGE_CLAIMED, traceability.STAGE_PICKED}
        and assigned_picker(order["events"]) == actor.get("id")
    ]


def review_queue(storage, actor: dict) -> list:
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    return [
        order["request"] for order in open_orders(storage)
        if order["stage"] in {traceability.STAGE_PICKED, traceability.STAGE_RECEIVED}
    ]


def claimed_orders(storage, actor: dict) -> list:
    """Pedidos en picking (para reasignar o liberar): ``[(solicitud, eventos)]``."""
    permissions.require_cap(actor, permissions.CAP_REASSIGN)
    return [(order["request"], order["events"]) for order in open_orders(storage)
            if order["stage"] == traceability.STAGE_CLAIMED]


def authorize_request(storage, request_id: str, actor: dict, picker_ids=None):
    permissions.require_cap(actor, permissions.CAP_AUTHORIZE_DISPATCH)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_AUTHORIZED, actor,
        {"authorized_pickers": sorted(set(str(v) for v in (picker_ids or []) if v))},
    )


def claim_request(storage, request_id: str, actor: dict):
    permissions.require_cap(actor, permissions.CAP_CLAIM)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_CLAIMED, actor,
        {"picker_id": actor.get("id", ""), "picker_name": actor.get("full_name", "")},
    )


def _order_batch(rows: list, action, verb: str):
    """Aplica ``action(request_id) -> (ok, mensaje, …)`` a cada línea y resume. Devuelve ``(ok, mensaje, hechas)``."""
    done, problems = [], []
    for row in rows:
        result = action(row["id"])
        (done if result[0] else problems).append((row, result[1]))
    if not rows:
        return False, "No hay líneas de este pedido en ese paso.", []
    text = f"{verb} {len(done)} de {len(rows)} producto(s) del pedido."
    if problems:
        text += f" {problems[0][1]}"
    return bool(done), text, [row for row, _ in done]


def claim_order(storage, order_id: str, actor: dict):
    """Toma de una vez todas las líneas disponibles de un pedido de varios productos."""
    permissions.require_cap(actor, permissions.CAP_CLAIM)
    lines = [row for row in queue_for(storage, actor) if order_id and row.get("order_id") == order_id]
    return _order_batch(lines, lambda rid: claim_request(storage, rid, actor), "Tomaste")


def receive_order(storage, order_id: str, actor: dict):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    lines = [order["request"] for order in open_orders(storage)
             if order_id and order["request"].get("order_id") == order_id
             and order["stage"] == traceability.STAGE_PICKED]
    return _order_batch(lines, lambda rid: receive_request(storage, rid, actor), "Recibiste")


def deliver_order(storage, order_id: str, actor: dict):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    lines = [order["request"] for order in open_orders(storage)
             if order_id and order["request"].get("order_id") == order_id
             and order["stage"] == traceability.STAGE_RECEIVED]
    return _order_batch(lines, lambda rid: deliver_request(storage, rid, actor), "Entregaste")


def release_request(storage, request_id: str, actor: dict, reason: str = ""):
    permissions.require_cap(actor, permissions.CAP_RELEASE)
    previous = assigned_picker(storage.get_trace_events(request_id=request_id))
    result = storage.transition_request(
        request_id, traceability.EVENT_ORDER_RELEASED, actor,
        {"picker_id": actor.get("id", ""), "reason": " ".join((reason or "").split())},
    )
    if result[0] and previous:
        picking.clear(previous, request_id)  # sus lecturas pendientes ya no valen
    return result


def reassign_request(storage, request_id: str, actor: dict, picker: dict, reason: str = ""):
    permissions.require_cap(actor, permissions.CAP_REASSIGN)
    if not picker or picker.get("status") != "active" or picker.get("role") != permissions.ROLE_OPERATOR:
        return False, "Selecciona un operario activo.", None
    previous = assigned_picker(storage.get_trace_events(request_id=request_id))
    result = storage.transition_request(
        request_id, traceability.EVENT_ORDER_REASSIGNED, actor,
        {"picker_id": picker["id"], "picker_name": picker.get("full_name", ""),
         "reason": " ".join((reason or "").split())},
    )
    if result[0] and previous:
        picking.clear(previous, request_id)
    return result


def validate_reading(request: dict, code: str) -> tuple:
    code = (code or "").strip()
    if not code:
        return False, "La lectura está vacía."
    if code != (request or {}).get("item_id"):
        return False, f"Se leyó {code}; se esperaba {(request or {}).get('item_id') or 'el producto solicitado'}."
    return True, "Lectura correcta."


def _stamp(now=None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")


def start_picking(storage, request_id: str, actor: dict):
    """Abre el picking de un pedido que ``actor`` tomó. Devuelve (ok, mensaje)."""
    permissions.require_cap(actor, permissions.CAP_PICK)
    request = storage.get_service_request(request_id)
    events = storage.get_trace_events(request_id=request_id) if request else []
    if not request or traceability.request_stage_key(request, events) != traceability.STAGE_CLAIMED:
        return False, "El pedido ya no está en picking."
    if assigned_picker(events) != actor.get("id"):
        return False, "El pedido está asignado a otro operario."
    picking.begin(actor.get("id"), request_id)
    return True, "Picking abierto."


def register_reading(storage, request_id: str, actor: dict, code: str, method: str, now=None):
    """Suma una lectura al picking abierto de ``actor`` (sin escribir en la base).

    Devuelve ``(ok, mensaje, cuántas)``. Una lectura que no corresponde al
    producto se acumula como fallida y se guarda con el picking completo."""
    permissions.require_cap(actor, permissions.CAP_PICK)
    method = normalize_method(method)
    if not method:
        return False, "Método de lectura no válido.", len(picking.readings(actor.get("id"), request_id))
    request = storage.get_service_request(request_id)
    events = storage.get_trace_events(request_id=request_id) if request else []
    if (not request or traceability.request_stage_key(request, events) != traceability.STAGE_CLAIMED
            or assigned_picker(events) != actor.get("id")):
        return False, "Este pedido ya no está en tu picking.", 0
    user_id = actor.get("id")
    needed = int(request.get("quantity") or 0)
    current = len(picking.readings(user_id, request_id))      # 0 o 1: la etiqueta de la caja o del producto
    code = str(code or "").strip()
    if pick_route.required(storage):
        route = pick_route.read(storage, request, picking.route_steps(user_id, request_id), code)
        if route is not None:
            ok, message, point = route
            if point is not None:
                picking.add_route_step(user_id, request_id, point["key"], code, method, _stamp(now))
            elif not ok:
                picking.add_failed(user_id, request_id, code, message, method, _stamp(now))
            return ok, message, current
    if method == METHOD_COUNT:
        return False, "El conteo es solo para confirmar las unidades que se sacan de la caja.", current
    ok, message = validate_reading(request, code)
    if not ok:
        picking.add_failed(user_id, request_id, code, message, method, _stamp(now))
        return False, message, current
    follow = f"Ahora cuenta las {needed} unidad(es) que sacas y confírmalas." + (
        " (La caja no necesita lectura: basta su casilla.)" if pick_route.box_step(storage, request.get("item_id")) else "")
    if not picking.add_reading_once(user_id, request_id, code, method, _stamp(now)):
        return True, f"La etiqueta del producto ya estaba leída. {follow}", 1
    return True, f"Etiqueta del producto leída ({METHOD_LABELS[method]}). {follow}", 1


def register_active_reading(storage, actor: dict, code: str, method: str, now=None,
                            accepted: bool = True, reason: str = ""):
    """`register_reading` para el picking que ``actor`` tiene abierto en cualquier
    pestaña (lo usa el toque NFC, que llega en una sesión nueva).

    ``accepted=False`` es un toque que el flujo NFC ya rechazó (firma inválida,
    chip no registrado…): nunca suma, solo queda como lectura fallida con su
    ``reason``. Devuelve ``(ok, mensaje)`` o ``None`` si no hay picking abierto."""
    permissions.require_cap(actor, permissions.CAP_PICK)
    request_id = picking.active_request(actor.get("id"))
    if not request_id:
        return None
    if not accepted:
        text = reason or "El toque no fue aceptado."
        picking.add_failed(actor.get("id"), request_id, str(code or "").strip(), text,
                           normalize_method(method) or METHOD_NFC, _stamp(now))
        return False, text
    ok, message, _ = register_reading(storage, request_id, actor, code, method, now)
    return ok, message


def undo_reading(actor: dict, request_id: str) -> int:
    permissions.require_cap(actor, permissions.CAP_PICK)
    return picking.undo_last(actor.get("id"), request_id)


def current_readings(actor: dict, request_id: str) -> list:
    return picking.readings(actor.get("id"), request_id)


def abandon_picking(actor: dict, request_id: str) -> None:
    """Cierra el panel de picking y descarta lecturas y fallos pendientes."""
    picking.clear(actor.get("id"), request_id)


def complete_picking(storage, request_id: str, actor: dict, readings: list = None, method: str = None,
                     extra_details: dict = None):
    """Completa el picking con una sola escritura.

    Sin ``readings`` usa las acumuladas en :mod:`core.picking`; las lecturas
    fallidas viajan en la misma escritura (un único evento de rechazos)."""
    permissions.require_cap(actor, permissions.CAP_PICK)
    pending_route = route_pending(storage, request_id, actor)
    if pending_route:
        return False, pending_route, None
    from_store = readings is None
    now = _stamp()
    label_read = None
    if from_store:
        # Las unidades que se sacan de la caja no llevan código. Si el producto está en una caja, la caja no se lee:
        # se marca la casilla de confirmación (quién retiró queda registrado). Si no hay caja, se lee una vez la
        # etiqueta del producto. Cada unidad queda registrada con el método «conteo».
        stored = picking.readings(actor.get("id"), request_id)
        request = storage.get_service_request(request_id)
        box = pick_route.box_step(storage, (request or {}).get("item_id"))
        if box is not None and not (extra_details or {}).get("box_confirmation"):
            return False, (f"Marca la casilla de la {box['title']}: confirma que abriste la caja y sacaste "
                           "las unidades de ahí."), None
        if box is None and not stored:
            return False, "Lee la etiqueta del producto antes de confirmar las unidades.", None
        label_read = stored[0] if stored else None
        source = [{"code": (request or {}).get("item_id"), "method": METHOD_COUNT, "at": now}
                  for _ in range(int((request or {}).get("quantity") or 0))]
        method = METHOD_COUNT
    else:
        source = readings
    default = normalize_method(method) or typed_method(storage)
    clean = []
    for value in source or []:
        if isinstance(value, dict):
            clean.append({"code": str(value.get("code") or "").strip(),
                          "method": normalize_method(value.get("method") or default) or "",
                          "at": str(value.get("at") or now)})
        else:
            clean.append({"code": str(value or "").strip(), "method": default, "at": now})
    details = {"picker_id": actor.get("id", ""), "readings": clean}
    steps = picking.route_steps(actor.get("id"), request_id)
    if steps:
        details["route"] = steps
    if label_read:
        details["label_read"] = label_read
    details.update(extra_details or {})
    failed = picking.failed(actor.get("id"), request_id) if from_store else []
    if failed:
        details["failed_scans"] = failed
    result = storage.transition_request(request_id, traceability.EVENT_ORDER_PICKED, actor, details)
    if result[0]:
        picking.clear(actor.get("id"), request_id)
        if not traceability.event_details(result[2]).get("reused_loan"):
            _notify_dispatched_loan(storage, request_id)
    return result


def box_confirmation(storage, request: dict, actor: dict, via: str = "checkbox") -> dict:
    """Constancia de quién retiró las unidades de la caja (o ``{}`` si el producto no está en una caja)."""
    box = pick_route.box_step(storage, (request or {}).get("item_id"))
    if not box:
        return {}
    return {"box": box["title"], "box_code": box.get("label_code", ""), "via": via,
            "confirmed_by_id": actor.get("id", ""), "confirmed_by_name": actor.get("full_name", ""),
            "confirmed_by_email": actor.get("institutional_email", ""), "units": int((request or {}).get("quantity") or 0),
            "at": _stamp()}


def confirm_units(storage, request_id: str, actor: dict, counted, note: str = "", box_confirmed: bool = False):
    """El operario confirma cuántas unidades sacó (no llevan código de barras) y completa el picking.

    Exige la ruta leída y que la cuenta coincida con lo pedido. Si el producto está en una caja, además exige la
    **casilla de la caja** (``box_confirmed``): queda registrado quién retiró. Si no hay caja, exige la etiqueta del
    producto ya leída. Devuelve lo mismo que ``complete_picking``."""
    permissions.require_cap(actor, permissions.CAP_PICK)
    request = storage.get_service_request(request_id)
    if not request:
        return False, "El pedido no existe.", None
    needed = int(request.get("quantity") or 0)
    try:
        counted = int(counted)
    except (TypeError, ValueError):
        return False, "Escribe cuántas unidades sacaste, con un número entero.", None
    if counted != needed:
        return False, (f"El pedido es de {needed} unidad(es) y contaste {counted}. Saca la cantidad pedida; si no "
                       "alcanzan, libera el pedido y avisa al supervisor."), None
    extra = {"counted_units": counted, "count_note": _clean(note)}
    if box_confirmed:
        proof = box_confirmation(storage, request, actor)
        if proof:
            extra["box_confirmation"] = proof
    return complete_picking(storage, request_id, actor, extra_details=extra)


def route_pending(storage, request_id: str, actor: dict) -> str:
    """Mensaje si todavía falta leer puntos de la ruta (estantería → piso → contenedor → caja); '' si no."""
    if not pick_route.required(storage):
        return ""
    request = storage.get_service_request(request_id)
    if not request:
        return ""
    state = pick_route.progress(storage, request.get("item_id"), picking.route_steps(actor.get("id"), request_id))
    if state["complete"]:
        return ""
    return f"Antes de completar el picking lee la ruta: falta {pick_route.hint(state['next'])}."


def route_progress(storage, actor: dict, request_id: str) -> dict:
    """Avance de la ruta del picking de ``actor`` (para el panel): ``{"required", "points", "done", "next", "complete"}``."""
    request = storage.get_service_request(request_id)
    box = pick_route.box_step(storage, (request or {}).get("item_id")) if request else None
    extra = {"box": box["title"] if box else "", "label_required": request is not None and box is None}
    if not request or not pick_route.required(storage):
        return {"required": False, "points": [], "done": 0, "next": None, "complete": True, **extra}
    state = pick_route.progress(storage, request.get("item_id"), picking.route_steps(actor.get("id"), request_id))
    return {"required": bool(state["points"]), **state, **extra}


def manual_confirmation_message(actor: dict, request: dict) -> str:
    """Texto que queda como constancia de una confirmación sin lectura de código."""
    return (f"{(actor or {}).get('full_name') or 'El operario'} confirma que tomó "
            f"{int((request or {}).get('quantity') or 0)} unidad(es) de «{(request or {}).get('item_name') or (request or {}).get('item_id')}» "
            "sin leer su código y autoriza este registro.")


def confirm_manual_picking(storage, request_id: str, actor: dict, authorized: bool, note: str = ""):
    """Completa el picking **sin leer cada unidad**: quien lo hace confirma por escrito y se
    registra su nombre y el mensaje de autorización. Cada unidad queda con el método ``manual``.

    Es la alternativa cuando no hay pistola ni chip a mano; el CSV y la trazabilidad la distinguen
    de una lectura real. Devuelve lo mismo que ``complete_picking``."""
    permissions.require_cap(actor, permissions.CAP_PICK)
    if not authorized:
        return False, "Marca la casilla de autorización para confirmar sin leer el código.", None
    request = storage.get_service_request(request_id)
    events = storage.get_trace_events(request_id=request_id) if request else []
    if (not request or traceability.request_stage_key(request, events) != traceability.STAGE_CLAIMED
            or assigned_picker(events) != actor.get("id")):
        return False, "Este pedido ya no está en tu picking.", None
    pending_route = route_pending(storage, request_id, actor)
    if pending_route:
        return False, pending_route, None
    needed = int(request.get("quantity") or 0)
    now = _stamp()
    readings = [{"code": request.get("item_id"), "method": METHOD_MANUAL, "at": now} for _ in range(needed)]
    proof = {"manual_confirmation": {
        "operator_id": actor.get("id", ""), "operator_name": actor.get("full_name", ""),
        "message": manual_confirmation_message(actor, request), "note": _clean(note), "at": now,
    }}
    box_proof = box_confirmation(storage, request, actor, via="manual")
    if box_proof:
        proof["box_confirmation"] = box_proof
    result = complete_picking(storage, request_id, actor, readings=readings, method=METHOD_MANUAL,
                              extra_details=proof)
    if result[0]:
        picking.clear(actor.get("id"), request_id)
    return result


def _notify_dispatched_loan(storage, request_id: str) -> None:
    """La salida del picking avisa a los responsables igual que una salida directa,
    con «Despachado por». Es un aviso: si falla, el picking ya quedó guardado."""
    try:
        from core import email_events

        loan = next((row for row in storage.get_all_loans(status="out")
                     if row.get("request_id") == request_id), None)
        if not loan:
            return
        borrower = storage.get_user_by_id(loan.get("user_id") or "") or {
            "full_name": loan.get("user_name"), "role": loan.get("user_role"),
        }
        email_events.notify_loan_checkout(storage, loan, storage.get_item(loan.get("item_id") or ""), borrower)
    except Exception as exc:  # noqa: BLE001 - un correo nunca deshace un picking
        logger.warning(f"No se pudo avisar la salida del picking {request_id}: {exc}")


def receive_request(storage, request_id: str, actor: dict, notes: str = ""):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_RECEIVED, actor,
        {"notes": " ".join((notes or "").split())},
    )


def deliver_request(storage, request_id: str, actor: dict, notes: str = ""):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_DELIVERED, actor,
        {"notes": " ".join((notes or "").split())},
    )


def rework_request(storage, request_id: str, actor: dict, reason: str):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    reason = " ".join((reason or "").split())
    if not reason:
        return False, "Indica qué debe corregirse en el picking.", None
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_REWORK, actor, {"reason": reason},
    )


def return_to_shelf(storage, request_id: str, actor: dict, reason: str):
    permissions.require_cap(actor, permissions.CAP_RECEIVE_AND_DELIVER)
    reason = " ".join((reason or "").split())
    if not reason:
        return False, "Indica por qué se devuelve al estante.", None
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_RETURNED_TO_SHELF, actor, {"reason": reason},
    )


def confirm_client_receipt(storage, request_id: str, actor: dict):
    permissions.require_cap(actor, permissions.CAP_CONFIRM_RECEIPT)
    return storage.transition_request(
        request_id, traceability.EVENT_ORDER_CLIENT_CONFIRMED, actor, {},
    )


def dispatch_metrics(storage, actor: dict) -> list:
    permissions.require_cap(actor, permissions.CAP_DISPATCH_REPORTS)
    rows = []
    index = storage.get_events_index()          # una sola lectura de eventos, no una por pedido
    for request in storage.get_service_requests():
        events = index.get(request["id"], [])
        if request.get("request_type") != "product" or not events:
            continue
        by_type = {event.get("event_type"): event for event in events}
        requested = traceability.to_datetime(request.get("created_at"))
        delivered = traceability.to_datetime((by_type.get(traceability.EVENT_ORDER_DELIVERED) or {}).get("created_at"))
        claimed = traceability.to_datetime((by_type.get(traceability.EVENT_ORDER_CLAIMED) or {}).get("created_at"))
        picked = traceability.to_datetime((by_type.get(traceability.EVENT_ORDER_PICKED) or {}).get("created_at"))
        rows.append({
            "request_id": request["id"], "stage": traceability.request_stage_key(request, events),
            "requested_at": request.get("created_at", ""),
            "claim_seconds": int((claimed - requested).total_seconds()) if requested and claimed else "",
            "picking_seconds": int((picked - claimed).total_seconds()) if claimed and picked else "",
            "delivery_seconds": int((delivered - requested).total_seconds()) if requested and delivered else "",
            "picker_role": (by_type.get(traceability.EVENT_ORDER_PICKED) or {}).get("actor_role", ""),
        })
    return rows


def dispatch_csv(storage, actor: dict, salt: str = "dispatch-report") -> bytes:
    rows = dispatch_metrics(storage, actor)
    output = io.StringIO(newline="")
    fields = ["request_id", "stage", "requested_at", "claim_seconds", "picking_seconds",
              "delivery_seconds", "picker_role"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        row = dict(row)
        row["request_id"] = hashlib.sha256(f"{salt}:{row['request_id']}".encode()).hexdigest()[:12]
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")
