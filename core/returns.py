# -*- coding: utf-8 -*-
"""core/returns.py - Devolución de lo prestado en tres pasos, cada uno por quien corresponde.

1. **Solicitud** · el cliente avisa que devuelve («Devolver» en Préstamos).
2. **Verificación** · un supervisor revisa lo que llega: estado y cantidad recibida.
3. **Almacenamiento** · un operario (o supervisor) lo guarda en el estante. Solo aquí se cierra el
   préstamo y la disponibilidad vuelve a subir.

El estado no tiene hoja propia: se lee de los eventos de trazabilidad del préstamo (``loan_id``), así
queda toda la cadena de custodia. Quien tiene `checkin_any_loan` (profesor, administrador) sigue
pudiendo reingresar directo con ``core.loans.checkin``. Una devolución parcial cierra solo lo
recibido y deja el préstamo abierto por lo que falta."""

import json
from datetime import datetime, timezone

from core import email_events, permissions, traceability

SYSTEM = None      # llamador interno de confianza: aquí ya se verificó `store_return` y la etapa

STAGE_NONE, STAGE_REQUESTED, STAGE_VERIFIED = "none", "requested", "verified"
STAGE_LABELS = {
    STAGE_NONE: "En poder del cliente",
    STAGE_REQUESTED: "Devolución pedida · espera la verificación del supervisor",
    STAGE_VERIFIED: "Verificada · espera que un operario la guarde",
}
CONDITIONS = {"ok": "En buen estado", "damaged": "Con daños", "incomplete": "Incompleto"}
_NEXT_STAGE = {
    traceability.EVENT_RETURN_REQUESTED: STAGE_REQUESTED,
    traceability.EVENT_RETURN_VERIFIED: STAGE_VERIFIED,
    traceability.EVENT_RETURN_STORED: STAGE_NONE,     # guardado: el ciclo queda libre (hay otro si fue parcial)
}


def _find_loan(storage, loan_id: str):
    return next((loan for loan in storage.get_all_loans() if loan.get("id") == loan_id), None)


def _own_events(storage, loan_id: str) -> list:
    return [event for event in storage.get_trace_events(loan_id=loan_id)
            if event.get("event_type") in traceability.RETURN_EVENTS]


def stage_of(events: list) -> str:
    """Etapa de la devolución según los eventos del préstamo (el último manda)."""
    stage = STAGE_NONE
    for event in events:
        stage = _NEXT_STAGE.get(event.get("event_type"), stage)
    return stage


def stage(storage, loan_id: str) -> str:
    return stage_of(_own_events(storage, loan_id))


def last_details(storage, loan_id: str, kind: str) -> dict:
    """Detalles del último evento ``kind`` del préstamo (para mostrar estado y cantidad verificados)."""
    for event in reversed(_own_events(storage, loan_id)):
        if event.get("event_type") == kind:
            return traceability.event_details(event)
    return {}


def _record(storage, loan: dict, kind: str, actor: dict, details: dict) -> dict:
    return storage.add_trace_event({
        "event_type": kind, "request_id": "", "item_id": loan.get("item_id", ""), "loan_id": loan["id"],
        "user_id": loan.get("user_id", ""), "owner_id": loan.get("user_id", ""),
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
        "receipt": "", "details": json.dumps(details, ensure_ascii=False, default=str),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def _open_loan(storage, loan_id: str):
    loan = _find_loan(storage, loan_id)
    if not loan:
        return None, "El préstamo no existe."
    if loan.get("status") != "out":
        return None, "Este préstamo ya fue devuelto."
    return loan, ""


def can_request_return(user: dict, loan: dict) -> bool:
    """El dueño del préstamo (con `checkout`) lo devuelve por este flujo; el personal con
    `checkin_any_loan` reingresa directo, sin pedir nada."""
    return (permissions.has_cap(user, permissions.CAP_CHECKOUT) and bool(user.get("id"))
            and loan.get("user_id") == user.get("id")
            and not permissions.has_cap(user, permissions.CAP_CHECKIN_ANY))


def request_return(storage, loan_id: str, actor: dict, notes: str = ""):
    """El cliente avisa que devuelve. Devuelve ``(ok, mensaje)``."""
    permissions.require_cap(actor, permissions.CAP_CHECKOUT, "No tienes permiso para devolver préstamos.")
    loan, problem = _open_loan(storage, loan_id)
    if not loan:
        return False, problem
    if loan.get("user_id") != actor.get("id"):
        raise PermissionError("Solo puedes pedir la devolución de tus propios préstamos.")
    current = stage(storage, loan_id)
    if current != STAGE_NONE:
        return False, "Ya pediste esta devolución: " + STAGE_LABELS[current].split(" · ")[1] + "."
    _record(storage, loan, traceability.EVENT_RETURN_REQUESTED, actor, {"notes": str(notes or "").strip()})
    return True, "Listo: lleva el producto al laboratorio. Un supervisor lo revisará y un operario lo guardará."


def verify_return(storage, loan_id: str, actor: dict, condition: str, received: int, notes: str = ""):
    """El supervisor registra lo que realmente llegó. Devuelve ``(ok, mensaje)``."""
    permissions.require_cap(actor, permissions.CAP_VERIFY_RETURN, "Solo un supervisor verifica devoluciones.")
    if condition not in CONDITIONS:
        return False, "Elige el estado en que llegó el producto."
    loan, problem = _open_loan(storage, loan_id)
    if not loan:
        return False, problem
    if stage(storage, loan_id) != STAGE_REQUESTED:
        return False, "Esta devolución no está esperando verificación."
    borrowed = int(float(loan.get("quantity") or 0))
    try:
        received = int(received)
    except (TypeError, ValueError):
        return False, "La cantidad recibida debe ser un número."
    if not 1 <= received <= borrowed:
        return False, f"La cantidad recibida debe estar entre 1 y {borrowed}."
    if condition == "incomplete" and received == borrowed:
        return False, "Si llegó todo completo, elige «En buen estado» o «Con daños»."
    _record(storage, loan, traceability.EVENT_RETURN_VERIFIED, actor, {
        "condition": condition, "received": received, "borrowed": borrowed, "notes": str(notes or "").strip(),
    })
    return True, f"Devolución verificada: {received} de {borrowed} unidad(es). Ya la puede guardar un operario."


def store_return(storage, loan_id: str, actor: dict, notes: str = ""):
    """Un operario (o supervisor) guarda lo verificado y cierra el préstamo. Devuelve ``(ok, mensaje)``."""
    permissions.require_cap(actor, permissions.CAP_STORE_RETURN, "No tienes permiso para guardar devoluciones.")
    loan, problem = _open_loan(storage, loan_id)
    if not loan:
        return False, problem
    if stage(storage, loan_id) != STAGE_VERIFIED:
        return False, "Un supervisor debe verificar la devolución antes de guardarla."
    proof = last_details(storage, loan_id, traceability.EVENT_RETURN_VERIFIED)
    received = int(proof.get("received") or 0)
    condition = CONDITIONS.get(str(proof.get("condition") or ""), "")
    ok, message = storage.return_loan(
        loan_id, actor_email=actor.get("institutional_email", ""), actor=SYSTEM, quantity=received,
        note=" · ".join(part for part in (condition, str(notes or "").strip()) if part),
    )
    if not ok:
        return False, message
    _record(storage, loan, traceability.EVENT_RETURN_STORED, actor, {
        "received": received, "condition": proof.get("condition", ""), "notes": str(notes or "").strip(),
    })
    if received >= int(float(loan.get("quantity") or 0)):
        email_events.notify_loan_checkin(storage, loan_id, actor)
    return True, message


def pending(storage, wanted: str) -> list:
    """Préstamos abiertos cuya devolución está en la etapa ``wanted``, del más antiguo al más reciente.
    Cada fila es ``{"loan": …, "requested": {…}, "verified": {…}}`` con los detalles registrados."""
    by_loan = {}
    for event in storage.get_trace_events():
        if event.get("event_type") in traceability.RETURN_EVENTS and event.get("loan_id"):
            by_loan.setdefault(event["loan_id"], []).append(event)
    rows = []
    for loan in storage.get_all_loans(status="out"):
        events = by_loan.get(loan["id"], [])
        if not events or stage_of(events) != wanted:
            continue
        info = {kind: {} for kind in traceability.RETURN_EVENTS}
        for event in events:
            info[event["event_type"]] = traceability.event_details(event)
        rows.append({"loan": loan, "requested": info[traceability.EVENT_RETURN_REQUESTED],
                     "verified": info[traceability.EVENT_RETURN_VERIFIED]})
    return sorted(rows, key=lambda row: str(row["loan"].get("checkout_at") or ""))


def counts(storage) -> dict:
    """Cuántas devoluciones esperan verificación y cuántas esperan ser guardadas."""
    return {STAGE_REQUESTED: len(pending(storage, STAGE_REQUESTED)),
            STAGE_VERIFIED: len(pending(storage, STAGE_VERIFIED))}
