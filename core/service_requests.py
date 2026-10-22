# -*- coding: utf-8 -*-
"""Flujo de solicitudes de productos y servicios del laboratorio."""

import uuid
from datetime import datetime, timezone

from core import email_events, permissions
from core.config import safe_secret
from core.reservations import as_utc

TYPE_PRODUCT = "product"
TYPE_SERVICE = "service"
VALID_TYPES = (TYPE_PRODUCT, TYPE_SERVICE)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
REVIEWER_ROLES = permissions.MANAGER_ROLES
MAX_ORDER_LINES = 30          # productos distintos en un solo pedido


def auto_approve_products() -> bool:
    return str(safe_secret("AUTO_APPROVE_PRODUCT_REQUESTS", "false") or "").strip().lower() in {
        "1", "true", "yes", "si", "sí",
    }


def validate_request(storage, request_type: str, item_id: str = "", quantity=1,
                     service_name: str = "", description: str = "", needed_at=None) -> dict:
    request_type = (request_type or "").strip()
    item_id = (item_id or "").strip()
    service_name = " ".join((service_name or "").strip().split())
    description = " ".join((description or "").strip().split())
    if request_type not in VALID_TYPES:
        raise ValueError("Selecciona una solicitud de producto o de servicio.")

    item = None
    if request_type == TYPE_PRODUCT:
        item = storage.get_item(item_id)
        if not item or item.get("status") == "retired":
            raise ValueError("Selecciona un producto activo del inventario.")
        if item.get("item_type") == "master":
            raise ValueError("Solicita un producto, no un Contenedor Principal.")
        if item.get("item_type") == "location":
            raise ValueError("Solicita un producto, no una ubicación (estantería, piso, mesa o zona).")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            raise ValueError("La cantidad debe ser un número entero.") from None
        if quantity < 1:
            raise ValueError("La cantidad debe ser mayor a cero.")
        available = storage.get_available_quantity(item_id)
        if quantity > available:
            raise ValueError(f"Stock insuficiente. Disponible: {available}.")
        service_name = ""
    else:
        if not service_name:
            raise ValueError("Indica el servicio requerido.")
        if not description:
            raise ValueError("Describe la necesidad o el resultado esperado del servicio.")
        quantity = 0
        item_id = ""

    needed = ""
    if needed_at:
        needed_dt = as_utc(needed_at, "Fecha requerida")
        if needed_dt <= datetime.now(timezone.utc):
            raise ValueError("La fecha requerida debe estar en el futuro.")
        needed = needed_dt.isoformat()

    return {
        "request_type": request_type,
        "item_id": item_id,
        "item_name": item.get("name", "") if item else "",
        "quantity": quantity,
        "service_name": service_name,
        "description": description,
        "needed_at": needed,
    }


def submit_request(storage, user: dict, request_type: str, item_id: str = "", quantity=1,
                   service_name: str = "", description: str = "", needed_at=None):
    permissions.require_cap(user, permissions.CAP_REQUEST, "No tienes permiso para crear solicitudes.")
    data = validate_request(
        storage, request_type, item_id, quantity, service_name, description, needed_at
    )
    row = storage.create_service_request(data, user)
    if request_type == TYPE_PRODUCT and auto_approve_products():
        row = _auto_approve(storage, row)
    notified, message = email_events.notify_request_created(storage, row, user)
    row["email_notified"], row["email_error"] = notified, "" if notified else message
    return row, notified, message


def _auto_approve(storage, row: dict) -> dict:
    system_actor = {
        "id": "system:auto-approve", "full_name": "Aprobación automática",
        "institutional_email": "", "role": permissions.ROLE_MASTER,
    }
    storage.transition_request(
        row["id"], "order_approved", system_actor,
        {"notes": "Aprobación automática", "authorized_pickers": []},
    )
    return storage.get_service_request(row["id"])


def merge_lines(lines) -> list:
    """Une las líneas del carrito que repiten producto (suma sus cantidades) y valida su forma.
    ``lines``: ``[{"item_id", "quantity"}]``. Lanza ``ValueError`` con un mensaje claro."""
    merged = {}
    for line in lines or []:
        item_id = str((line or {}).get("item_id") or "").strip()
        if not item_id:
            raise ValueError("Una línea del pedido no tiene producto.")
        try:
            quantity = int(line.get("quantity", 1))
        except (TypeError, ValueError):
            raise ValueError("La cantidad debe ser un número entero.") from None
        if quantity < 1:
            raise ValueError("La cantidad debe ser mayor a cero.")
        merged[item_id] = merged.get(item_id, 0) + quantity
    if not merged:
        raise ValueError("Agrega al menos un producto al pedido.")
    if len(merged) > MAX_ORDER_LINES:
        raise ValueError(f"Un pedido admite hasta {MAX_ORDER_LINES} productos distintos.")
    return [{"item_id": item_id, "quantity": quantity} for item_id, quantity in merged.items()]


def submit_order(storage, user: dict, lines, description: str = "", needed_at=None):
    """Pedido de VARIOS productos en una sola solicitud: se validan todas las líneas antes de crear nada y
    se guardan juntas (todo o nada) con el mismo ``order_id``. Con una sola línea es un pedido normal.

    Devuelve ``(filas, notificado, mensaje)``; ``filas`` son las solicitudes creadas, una por producto."""
    permissions.require_cap(user, permissions.CAP_REQUEST, "No tienes permiso para crear solicitudes.")
    merged = merge_lines(lines)
    if len(merged) == 1:
        row, notified, message = submit_request(
            storage, user, TYPE_PRODUCT, merged[0]["item_id"], merged[0]["quantity"],
            description=description, needed_at=needed_at,
        )
        return [row], notified, message
    data = []
    for line in merged:
        try:
            data.append(validate_request(storage, TYPE_PRODUCT, line["item_id"], line["quantity"],
                                         description=description, needed_at=needed_at))
        except ValueError as exc:
            name = (storage.get_item(line["item_id"]) or {}).get("name") or line["item_id"]
            raise ValueError(f"«{name}»: {exc}") from None
    order_id = uuid.uuid4().hex[:8]
    rows = storage.create_service_requests(data, user, order_id)
    if auto_approve_products():
        rows = [_auto_approve(storage, row) or row for row in rows]
    summary = dict(rows[0], item_name=f"Pedido de {len(rows)} productos: " + ", ".join(
        f"{row.get('item_name')} ×{row.get('quantity')}" for row in rows),
        quantity=sum(int(row.get("quantity") or 0) for row in rows))
    notified, message = email_events.notify_request_created(storage, summary, user)
    for row in rows:
        row["email_notified"], row["email_error"] = notified, "" if notified else message
    return rows, notified, message


def order_rows(storage, order_id: str) -> list:
    """Líneas (solicitudes) de un pedido de varios productos, en el orden en que se crearon."""
    if not order_id:
        return []
    rows = [row for row in storage.get_service_requests() if row.get("order_id") == order_id]
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""))


def group_by_order(rows: list) -> list:
    """Agrupa filas para mostrarlas: ``[{"order_id", "rows"}]``. Las de un mismo pedido quedan juntas, en la
    posición de su primera línea; una solicitud suelta es un grupo de una fila con ``order_id`` vacío."""
    groups, index = [], {}
    for row in rows or []:
        order_id = str(row.get("order_id") or "")
        if order_id and order_id in index:
            index[order_id]["rows"].append(row)
            continue
        group = {"order_id": order_id, "rows": [row]}
        groups.append(group)
        if order_id:
            index[order_id] = group
    return groups


def review_order(storage, order_id: str, decision: str, reviewer: dict, notes: str = ""):
    """Aprueba o rechaza todas las líneas pendientes de un pedido. Devuelve ``(ok, mensaje)``."""
    pending = [row for row in order_rows(storage, order_id) if row.get("status") == STATUS_PENDING]
    if not pending:
        return False, "Este pedido no tiene líneas pendientes."
    done, problems = 0, []
    for row in pending:
        ok, message = review_request(storage, row["id"], decision, reviewer, notes)
        if ok:
            done += 1
        else:
            problems.append(message)
    verb = "aprobadas" if decision == STATUS_APPROVED else "rechazadas"
    if problems:
        return done > 0, f"{done} de {len(pending)} línea(s) {verb}. " + problems[0]
    return True, f"Pedido de {done} producto(s): todas las líneas {verb}."


def cancel_order(storage, order_id: str, actor: dict):
    """Cancela las líneas de un pedido que todavía se pueden cancelar. Devuelve ``(ok, mensaje)``."""
    rows = [row for row in order_rows(storage, order_id) if row.get("status") in (STATUS_PENDING, STATUS_APPROVED)]
    if not rows:
        return False, "Este pedido ya no tiene líneas para cancelar."
    done, problems = 0, []
    for row in rows:
        ok, message = cancel_request(storage, row["id"], actor)
        if ok:
            done += 1
        else:
            problems.append(message)
    if problems:
        return done > 0, f"Se cancelaron {done} de {len(rows)} línea(s). " + problems[0]
    return True, f"Pedido cancelado ({done} producto(s))."


def review_request(storage, request_id: str, decision: str, reviewer: dict, notes: str = "",
                   authorized_pickers=None):
    if not permissions.has_cap(reviewer, permissions.CAP_APPROVE):
        return False, "No tienes permiso para revisar solicitudes."
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        return False, "La decisión debe ser approved o rejected."
    request = storage.get_service_request(request_id)
    if not request:
        return False, "La solicitud no existe."
    event_type = "order_approved" if decision == STATUS_APPROVED else "order_rejected"
    ok, message, _ = storage.transition_request(
        request_id, event_type, reviewer,
        {"notes": " ".join((notes or "").split()),
         "authorized_pickers": list(authorized_pickers or [])},
    )
    if not ok:
        return False, message
    request = storage.get_service_request(request_id) or request
    email_events.notify_request_reviewed(storage, request, decision, reviewer, notes)
    return True, "Solicitud aprobada." if decision == STATUS_APPROVED else "Solicitud rechazada."


def cancel_request(storage, request_id: str, actor: dict):
    request = storage.get_service_request(request_id)
    if not request:
        return False, "La solicitud no existe."
    owner = request.get("requester_id") == actor.get("id")
    if owner and not permissions.has_cap(actor, permissions.CAP_CANCEL_OWN_REQUEST):
        return False, "No tienes permiso para cancelar esta solicitud."
    ok, message, _ = storage.transition_request(
        request_id, "order_cancelled", actor, {"reason": "Cancelada"}
    )
    return ok, "Solicitud cancelada." if ok else message