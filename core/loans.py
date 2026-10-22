# -*- coding: utf-8 -*-
"""
core/loans.py - Logica de negocio de salida (checkout) y reingreso (checkin).

La cantidad total de un item (items.quantity) solo cambia por Alta/Ajuste/Baja.
La disponibilidad se calcula en vivo como quantity - suma(prestamos abiertos),
de forma que siempre queda una auditoria completa de quien tiene que en 'loans'.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from core import email_events, permissions

logger = logging.getLogger(__name__)

BOGOTA_TZ = ZoneInfo("America/Bogota")

# --- Retiro por autoservicio (Escanear) ---------------------------------------------------
# Quién puede dar salida sin pasar por Despacho lo decide el administrador. Por omisión
# (``libre``) todo sigue como en v1.15; las otras dos opciones endurecen solo a quien NO
# aprueba solicitudes (los clientes): el personal que aprueba siempre puede dar salida.
SETTING_SELF_CHECKOUT = "self_checkout_mode"
MODE_FREE, MODE_APPROVED_ONLY, MODE_OFF = "libre", "solo_aprobado", "apagado"
SELF_CHECKOUT_MODES = {
    MODE_FREE: ("Libre", "Cualquier cliente retira lo que esté disponible desde Buscar, sin aprobación."),
    MODE_APPROVED_ONLY: ("Solo con solicitud aprobada",
                         "El cliente solo retira por Buscar lo que un supervisor ya aprobó, y hasta la cantidad aprobada."),
    MODE_OFF: ("Apagado", "Nadie del lado cliente retira por Buscar: todo pasa por Solicitudes y Despacho."),
}
DEFAULT_SELF_CHECKOUT = MODE_FREE


def self_checkout_mode(storage) -> str:
    try:
        value = str(storage.get_setting(SETTING_SELF_CHECKOUT, DEFAULT_SELF_CHECKOUT) or "").strip().lower()
    except Exception:       # un ajuste ilegible nunca debe frenar una salida
        value = ""
    return value if value in SELF_CHECKOUT_MODES else DEFAULT_SELF_CHECKOUT


def set_self_checkout_mode(storage, actor: dict, mode: str):
    """Fija el modo de autoservicio (solo quien administra el despacho). Devuelve (ok, mensaje)."""
    permissions.require_cap(actor, permissions.CAP_DISPATCH_SETTINGS,
                            "Solo un administrador cambia el retiro por autoservicio.")
    mode = str(mode or "").strip().lower()
    if mode not in SELF_CHECKOUT_MODES:
        return False, "Elige libre, solo con solicitud aprobada o apagado."
    storage.set_setting(SETTING_SELF_CHECKOUT, mode, (actor or {}).get("institutional_email", ""))
    return True, f"Retiro por autoservicio: {SELF_CHECKOUT_MODES[mode][0].lower()}."


def is_self_checkout_restricted(user: dict) -> bool:
    """El modo de autoservicio limita a quien no aprueba solicitudes (el personal queda libre)."""
    return not permissions.has_cap(user, permissions.CAP_APPROVE)


def deadline_from_date(day: date) -> datetime:
    """Fecha limite de devolucion elegida en la interfaz (solo dia, sin hora).

    Se guarda como medianoche UTC de ese dia: ese valor exacto es la marca de
    "fecha sin hora" que `due_instant` interpreta (y que ya usan los prestamos
    registrados antes de esta version)."""
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def due_instant(expected: datetime) -> datetime:
    """Instante a partir del cual un prestamo se considera vencido.

    Una fecha limite sin hora (medianoche UTC exacta) vence al TERMINAR ese dia
    en Colombia, no a las 7 p. m. del dia anterior (medianoche UTC). Una fecha
    con hora concreta se respeta tal cual."""
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    expected = expected.astimezone(timezone.utc)
    if expected.timetz().replace(tzinfo=None) == time.min:
        return datetime.combine(expected.date() + timedelta(days=1), time.min, tzinfo=BOGOTA_TZ)
    return expected


def checkout(storage, item_id: str, quantity: int, user: dict, expected_return_at=None, notes: str = "",
             request_id: str = ""):
    """Salida de ``quantity`` unidades. ``request_id`` es la solicitud aprobada que se retira;
    cuando el modo de autoservicio es «solo con solicitud aprobada» es obligatoria para un cliente."""
    permissions.require_cap(user, permissions.CAP_CHECKOUT, "No tienes permiso para registrar salidas.")
    if quantity <= 0:
        return False, "La cantidad debe ser mayor a 0.", None
    blocked = _self_checkout_blocked(storage, user, item_id, quantity, request_id)
    if blocked:
        return False, blocked, None

    item = storage.get_item(item_id)
    if not item:
        return False, f"El item '{item_id}' no existe.", None
    if item.get("item_type") == "master":
        return False, "No se puede dar salida a un Contenedor Principal, solo a los items que tiene dentro.", None
    if item.get("item_type") == "location":
        return False, ("No se puede dar salida a una ubicación (estantería, piso, mesa o zona), solo a los "
                       "productos que guarda."), None

    available = storage.get_available_quantity(item_id)
    if quantity > available:
        return False, f"Stock insuficiente. Disponible: {available}.", None

    try:
        loan = storage.create_loan(item, quantity, user, expected_return_at=expected_return_at, notes=notes)
    except ValueError as exc:
        # La capa de datos re-verifica el stock dentro de su lock: otra persona
        # pudo sacar unidades entre la comprobacion anterior y este punto.
        return False, str(exc), None
    email_events.notify_loan_checkout(storage, loan, item, user)
    return True, f"Salida registrada: '{item.get('name')}' x{quantity} para {user.get('full_name')}.", loan


def _self_checkout_blocked(storage, user: dict, item_id: str, quantity: int, request_id: str) -> str:
    """Motivo por el que el modo de autoservicio impide esta salida ("" si puede seguir)."""
    mode = self_checkout_mode(storage)
    if mode == MODE_FREE or not is_self_checkout_restricted(user):
        return ""
    if mode == MODE_OFF:
        return ("El retiro por autoservicio está apagado: pide el producto desde Solicitudes y "
                "el laboratorio te lo entrega.")
    from core import traceability

    approved = {request["id"]: request for request in traceability.pending_pickups(storage, user, item_id)}
    request = approved.get(request_id)
    if request is None:
        return ("Solo puedes retirar por Buscar un producto que ya tenga una solicitud aprobada a tu nombre. "
                "Pídelo desde Solicitudes.")
    if quantity > int(request.get("quantity") or 0):
        return f"Tu solicitud aprobada es de {int(request.get('quantity') or 0)} unidad(es); no puedes retirar más."
    return ""


def checkin(storage, loan_id: str, actor_user: dict):
    """Reingreso directo, solo para quien tiene `checkin_any_loan` (profesor, administrador).
    El cliente devuelve por el flujo de ``core.returns`` (solicitud → verificación → almacenamiento)."""
    permissions.require_cap(actor_user, permissions.CAP_CHECKIN_ANY,
                            "Tu devolución la verifica un supervisor: usa «Devolver» en Préstamos.")
    ok, msg = storage.return_loan(
        loan_id, actor_email=actor_user.get("institutional_email", ""), actor=actor_user,
    )
    if ok:
        email_events.notify_loan_checkin(storage, loan_id, actor_user)
    return ok, msg


def is_overdue(loan: dict, now: datetime = None) -> bool:
    if loan.get("status") != "out":
        return False
    expected = loan.get("expected_return_at")
    if not expected:
        return False
    return due_instant(expected) < (now or datetime.now(timezone.utc))


def get_overdue_loans(storage) -> list:
    open_loans = storage.get_all_loans(status="out")
    return [l for l in open_loans if is_overdue(l)]


def get_loans_for_view(storage, user: dict) -> list:
    """Quien tiene `view_all_loans` (profesor/administrador) ve todos los préstamos
    abiertos; los demás, solo los suyos."""
    if permissions.has_cap(user, permissions.CAP_VIEW_ALL_LOANS):
        return storage.get_all_loans(status="out")
    return storage.get_open_loans_for_user(user["id"])


def can_checkin_loan(user: dict, loan: dict) -> bool:
    """True si ``user`` puede reingresar ``loan`` directo (vistas y pruebas comparten la regla)."""
    return permissions.has_cap(user, permissions.CAP_CHECKIN_ANY)
