# -*- coding: utf-8 -*-
"""Reglas de negocio para reservar actividades o el laboratorio completo."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core import email_events, permissions

BOGOTA_TZ = ZoneInfo("America/Bogota")
SCOPE_ACTIVITY = "activity"
SCOPE_FULL_LAB = "full_lab"
VALID_SCOPES = (SCOPE_ACTIVITY, SCOPE_FULL_LAB)
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
FINAL_STATUSES = (STATUS_REJECTED, STATUS_CANCELLED)
REVIEWER_ROLES = tuple(
    role for role, caps in permissions.ROLE_CAPS.items()
    if permissions.CAP_MANAGE_RESERVATIONS in caps
)


def as_utc(value, field: str = "Fecha") -> datetime:
    """Convierte datetime/ISO a UTC; valores sin zona se interpretan Bogotá."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{field} no tiene un formato válido.") from None
    if not isinstance(value, datetime):
        raise ValueError(f"{field} es obligatoria.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=BOGOTA_TZ)
    return value.astimezone(timezone.utc)


def as_bogota(value) -> datetime:
    return as_utc(value).astimezone(BOGOTA_TZ)


def normalize_activity(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def validate_reservation(scope_type: str, activity: str, purpose: str, attendees,
                         start_at, end_at, now: datetime = None) -> dict:
    scope_type = (scope_type or "").strip()
    activity = " ".join((activity or "").strip().split())
    purpose = " ".join((purpose or "").strip().split())
    if scope_type not in VALID_SCOPES:
        raise ValueError("Selecciona si reservas una actividad o el laboratorio completo.")
    if scope_type == SCOPE_ACTIVITY and not activity:
        raise ValueError("Describe la actividad que deseas reservar.")
    if not purpose:
        raise ValueError("Indica el propósito de la reserva.")
    try:
        attendees = int(attendees)
    except (TypeError, ValueError):
        raise ValueError("El número de asistentes debe ser entero.") from None
    if not 1 <= attendees <= 500:
        raise ValueError("El número de asistentes debe estar entre 1 y 500.")

    start = as_utc(start_at, "Fecha de inicio")
    end = as_utc(end_at, "Fecha de finalización")
    now = as_utc(now or datetime.now(timezone.utc), "Fecha actual")
    if start <= now:
        raise ValueError("La reserva debe comenzar en el futuro.")
    if end <= start:
        raise ValueError("La finalización debe ser posterior al inicio.")
    if end - start > timedelta(days=7):
        raise ValueError("Una reserva no puede durar más de 7 días.")

    return {
        "scope_type": scope_type,
        "activity": activity if scope_type == SCOPE_ACTIVITY else "Laboratorio completo",
        "purpose": purpose,
        "attendees": attendees,
        "start_at": start,
        "end_at": end,
    }


def intervals_overlap(start_a, end_a, start_b, end_b) -> bool:
    return as_utc(start_a) < as_utc(end_b) and as_utc(start_b) < as_utc(end_a)


def reservations_conflict(existing: dict, candidate: dict) -> bool:
    """Una reserva completa bloquea todo; actividades distintas pueden coexistir."""
    if existing.get("status") != STATUS_APPROVED:
        return False
    if not intervals_overlap(
        existing.get("start_at"), existing.get("end_at"),
        candidate.get("start_at"), candidate.get("end_at"),
    ):
        return False
    if SCOPE_FULL_LAB in (existing.get("scope_type"), candidate.get("scope_type")):
        return True
    return normalize_activity(existing.get("activity")) == normalize_activity(candidate.get("activity"))


def find_conflict(existing_reservations: list, candidate: dict, exclude_id: str = ""):
    for reservation in existing_reservations:
        if reservation.get("id") == exclude_id:
            continue
        if reservations_conflict(reservation, candidate):
            return reservation
    return None


def submit_reservation(storage, user: dict, scope_type: str, activity: str,
                       purpose: str, attendees, start_at, end_at):
    permissions.require_cap(user, permissions.CAP_RESERVE, "No tienes permiso para reservar.")
    data = validate_reservation(scope_type, activity, purpose, attendees, start_at, end_at)
    conflict = find_conflict(storage.get_reservations(status=STATUS_APPROVED), data)
    if conflict:
        raise ValueError(
            f"Existe una reserva aprobada que se cruza con ese horario: {conflict.get('activity')}."
        )
    row = storage.create_reservation(data, user)
    # Aviso a los perfiles maestro (en segundo plano si hay SMTP); el resultado
    # queda en email_notified / email_error de la reserva.
    notified, message = email_events.notify_reservation_created(storage, row, user)
    row["email_notified"], row["email_error"] = notified, "" if notified else message
    return row, notified, message


def review_reservation(storage, reservation_id: str, decision: str,
                       reviewer: dict, notes: str = ""):
    if not permissions.has_cap(reviewer, permissions.CAP_MANAGE_RESERVATIONS):
        return False, "Solo un supervisor autorizado o un administrador pueden revisar reservas."
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        return False, "La decisión debe ser approved o rejected."
    reservation = storage.get_reservation(reservation_id)
    if not reservation:
        return False, "La reserva no existe."
    if reservation.get("status") != STATUS_PENDING:
        return False, "Solo se pueden revisar reservas pendientes."
    from core import dispatch      # la misma regla que las solicitudes: nadie aprueba lo suyo

    if (decision == STATUS_APPROVED and reservation.get("requester_id") == reviewer.get("id")
            and not dispatch.allow_self_approval()):
        return False, "Es tu propia reserva: la aprueba otra persona."
    if decision == STATUS_APPROVED:
        conflict = find_conflict(
            storage.get_reservations(status=STATUS_APPROVED), reservation, exclude_id=reservation_id
        )
        if conflict:
            return False, f"Conflicto con la reserva aprobada: {conflict.get('activity')}."
    try:
        storage.update_reservation_status(
            reservation_id, decision, reviewer.get("institutional_email", ""), notes
        )
    except ValueError as exc:
        # La capa de datos re-verifica el cruce dentro de su lock (otra aprobacion
        # simultanea pudo ocupar el horario despues de la comprobacion anterior).
        return False, str(exc)
    email_events.notify_reservation_reviewed(storage, reservation, decision, reviewer, notes)
    return True, "Reserva aprobada." if decision == STATUS_APPROVED else "Reserva rechazada."


def cancel_reservation(storage, reservation_id: str, actor: dict):
    reservation = storage.get_reservation(reservation_id)
    if not reservation:
        return False, "La reserva no existe."
    owner = reservation.get("requester_id") == actor.get("id")
    privileged = permissions.has_cap(actor, permissions.CAP_MANAGE_RESERVATIONS)
    if not (owner or privileged):
        return False, "No tienes permiso para cancelar esta reserva."
    if reservation.get("status") in FINAL_STATUSES:
        return False, "La reserva ya está cerrada."
    storage.update_reservation_status(
        reservation_id, STATUS_CANCELLED, actor.get("institutional_email", ""), "Cancelada"
    )
    return True, "Reserva cancelada."