# -*- coding: utf-8 -*-
"""Seguimiento sencillo de un pedido, en el lenguaje de quien lo pidió.

La trazabilidad completa (eventos, actores, comprobantes) es para el personal. El cliente
solo necesita saber **dónde va su pedido y qué le toca hacer**: ``client_tracker`` lo
resume en un titular, una pista y una línea de pasos."""

from core import reservations, service_requests, traceability

DONE, CURRENT, PENDING, BLOCKED = "done", "current", "pending", "blocked"

_PRODUCT_STEPS = ("Enviada", "Aprobada", "Preparando tu pedido", "Listo para reclamar", "Completado")
_SERVICE_STEPS = ("Enviada", "Aprobada", "En curso", "Entregado")
_RESERVATION_STEPS = ("Enviada", "Aprobada")

# etapa de despacho -> (paso actual, titular, pista, tono)
_PRODUCT_STAGES = {
    traceability.STAGE_REQUESTED: (0, "Enviada · esperando aprobación",
                                   "Un supervisor la revisará pronto.", "warning"),
    traceability.STAGE_APPROVED: (1, "Aprobada · en la cola del laboratorio",
                                  "Un operario la tomará para prepararla.", "info"),
    traceability.STAGE_CLAIMED: (2, "Preparando tu pedido",
                                 "Un operario está reuniendo tus productos.", "info"),
    traceability.STAGE_PICKED: (2, "Preparando tu pedido",
                                "Ya están reunidos; el supervisor los revisa.", "info"),
    traceability.STAGE_RECEIVED: (3, "Listo para reclamar",
                                  "Pasa por el laboratorio: el supervisor te lo entrega.", "success"),
    traceability.STAGE_DELIVERED: (3, "Entregado · confirma que lo recibiste",
                                   "Pulsa «Recibí mi pedido» para cerrar el pedido.", "success"),
    traceability.STAGE_CONFIRMED: (4, "Completado", "Gracias: el pedido quedó cerrado.", "success"),
}
_TERMINAL = {
    traceability.STAGE_REJECTED: ("Rechazada", "Mira la respuesta del supervisor.", "danger"),
    traceability.STAGE_CANCELLED: ("Cancelada", "El pedido se canceló.", "neutral"),
    traceability.STAGE_RETURNED: ("Devuelto al estante", "No se entregó; las unidades volvieron al inventario.",
                                  "neutral"),
}


def _steps(labels: tuple, current: int, terminal: bool = False) -> list:
    steps = []
    for index, label in enumerate(labels):
        if terminal and index == current:
            state = BLOCKED
        elif index < current or (index == current and current == len(labels) - 1 and not terminal):
            state = DONE
        elif index == current:
            state = CURRENT
        else:
            state = PENDING
        steps.append({"title": label, "state": state})
    return steps


def client_tracker(request: dict, events: list = None, loan: dict = None) -> dict:
    """``{"headline", "hint", "tone", "steps", "can_confirm"}`` de una solicitud."""
    request, events = request or {}, events or []
    status = request.get("status")
    product = request.get("request_type") != service_requests.TYPE_SERVICE
    labels = _PRODUCT_STEPS if product else _SERVICE_STEPS

    if status in (service_requests.STATUS_REJECTED, service_requests.STATUS_CANCELLED) or (
            product and traceability.request_stage_key(request, events) in _TERMINAL):
        key = status if status in (service_requests.STATUS_REJECTED, service_requests.STATUS_CANCELLED) \
            else traceability.request_stage_key(request, events)
        headline, hint, tone = _TERMINAL[key]
        reached = 1 if key != traceability.STAGE_CANCELLED else 0
        return {"headline": headline, "hint": hint, "tone": tone, "can_confirm": False,
                "steps": _steps(labels, min(reached, len(labels) - 1), terminal=True)}

    if not product:
        stage = traceability.service_stage(events) if status == service_requests.STATUS_APPROVED else None
        current, headline, hint, tone = {
            None: (0, "Enviada · esperando aprobación", "Un supervisor la revisará pronto.", "warning"),
            "approved": (1, "Aprobada", "El laboratorio la atenderá.", "info"),
            "started": (2, "En curso", "El laboratorio ya está trabajando en ella.", "info"),
            "delivered": (3, "Entregado", "El servicio quedó cerrado.", "success"),
        }[stage]
        return {"headline": headline, "hint": hint, "tone": tone, "can_confirm": False,
                "steps": _steps(labels, current)}

    picked_up = any(event.get("event_type") == traceability.EVENT_PICKED_UP for event in events)
    authorized = any(event.get("event_type") == traceability.EVENT_ORDER_AUTHORIZED for event in events)
    if picked_up and not traceability.dispatch_started(events):
        stage_text, _ = traceability.request_stage(request, events, loan)
        return {"headline": stage_text, "hint": "Ya lo retiraste con tu cuenta.", "tone": "success",
                "can_confirm": False, "steps": _steps(labels, 4)}
    if traceability.dispatch_started(events) or (status == service_requests.STATUS_APPROVED and authorized):
        key = traceability.request_stage_key(request, events)
        current, headline, hint, tone = _PRODUCT_STAGES.get(key, _PRODUCT_STAGES[traceability.STAGE_APPROVED])
        return {"headline": headline, "hint": hint, "tone": tone,
                "can_confirm": key == traceability.STAGE_DELIVERED, "steps": _steps(labels, current)}

    # Autoservicio (solicitud aprobada que se retira por Buscar, sin despacho).
    if status == service_requests.STATUS_PENDING:
        current, headline, hint, tone = _PRODUCT_STAGES[traceability.STAGE_REQUESTED]
        return {"headline": headline, "hint": hint, "tone": tone, "can_confirm": False,
                "steps": _steps(labels, current)}
    return {"headline": "Aprobada · por retirar", "hint": "Búscalo en el laboratorio y retíralo con tu cuenta.",
            "tone": "info", "can_confirm": False, "steps": _steps(labels, 1)}


def reservation_tracker(reservation: dict) -> dict:
    status = (reservation or {}).get("status")
    if status == reservations.STATUS_APPROVED:
        return {"headline": "Aprobada", "hint": "Tu horario quedó reservado.", "tone": "success",
                "can_confirm": False, "steps": _steps(_RESERVATION_STEPS, 1)}
    if status in (reservations.STATUS_REJECTED, reservations.STATUS_CANCELLED):
        rejected = status == reservations.STATUS_REJECTED
        return {"headline": "Rechazada" if rejected else "Cancelada",
                "hint": "Mira la respuesta del supervisor." if rejected else "La reserva se canceló.",
                "tone": "danger" if rejected else "neutral", "can_confirm": False,
                "steps": _steps(_RESERVATION_STEPS, 1, terminal=True)}
    return {"headline": "Enviada · esperando aprobación", "hint": "Un supervisor la revisará pronto.",
            "tone": "warning", "can_confirm": False, "steps": _steps(_RESERVATION_STEPS, 0)}
