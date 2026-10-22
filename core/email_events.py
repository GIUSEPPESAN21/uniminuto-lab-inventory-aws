# -*- coding: utf-8 -*-
"""core/email_events.py - Correos automaticos de todo lo que se pide en el laboratorio.

Cada flujo (solicitudes, reservas, salidas, reingresos y revisiones) llama a
UNA funcion `notify_*`; este modulo resuelve todo lo demas:

- Contenido: asunto claro, p. ej. "[Laboratorio] Nueva solicitud: Microscopio
  — Ana Prueba (Estudiante)", y cuerpo con QUIEN (nombre, rol, correo e ID),
  QUE (producto y codigo, cantidad, servicio, fechas) y CUANDO (hora de
  Bogota), en texto plano + una alternativa HTML sencilla, con enlace a la app
  si existe el secret APP_URL.
- Destinatarios: perfiles maestro activos + ADMIN_NOTIFICATION_EMAILS (+ los
  profesores activos si el maestro lo activa), sin duplicados ni correos
  invalidos o anonimos. Las respuestas (aprobada / rechazada) van al solicitante.
- Preferencias: un interruptor por evento en la hoja `settings` (clave
  SETTINGS_KEY); todos activos por defecto.
- Envio: en un hilo en segundo plano, la interfaz nunca espera al servidor
  SMTP. Sin SMTP configurado no hay red ni hilo: se responde al instante.
- Registro: cada intento real (con SMTP configurado) queda en la hoja
  `notification_log`, que el maestro consulta en Reportes -> Correos.
- Nunca lanza: un correo fallido jamas invalida la operacion que lo origino.
"""

import html
import json
import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from core import notifications
from core.ui import BRAND_BLUE, BRAND_GOLD, ROLE_LABELS, ROLE_SHORT_LABELS

logger = logging.getLogger(__name__)

BOGOTA_TZ = ZoneInfo("America/Bogota")
SUBJECT_PREFIX = "[Laboratorio]"
SETTINGS_KEY = "email_notifications"
FOOTER = "Mensaje automático del Inventario de Laboratorio de Ingeniería · UNIMINUTO."

# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------
EVENT_REQUEST_CREATED = "request_created"
EVENT_RESERVATION_CREATED = "reservation_created"
EVENT_LOAN_CHECKOUT = "loan_checkout"
EVENT_LOAN_CHECKIN = "loan_checkin"
EVENT_REQUEST_REVIEWED = "request_reviewed"
EVENT_RESERVATION_REVIEWED = "reservation_reviewed"
EVENT_LOANS_OVERDUE = "loans_overdue"
EVENT_TEST = "test"

# Eventos que el maestro puede activar o desactivar (todos activos por defecto).
EVENTS = (
    EVENT_REQUEST_CREATED, EVENT_RESERVATION_CREATED, EVENT_LOAN_CHECKOUT, EVENT_LOAN_CHECKIN,
    EVENT_REQUEST_REVIEWED, EVENT_RESERVATION_REVIEWED, EVENT_LOANS_OVERDUE,
)
EVENT_LABELS = {
    EVENT_REQUEST_CREATED: "Nueva solicitud de producto o servicio",
    EVENT_RESERVATION_CREATED: "Nueva reserva de actividad o del laboratorio",
    EVENT_LOAN_CHECKOUT: "Salida de un préstamo (checkout)",
    EVENT_LOAN_CHECKIN: "Devolución de un préstamo (checkin)",
    EVENT_REQUEST_REVIEWED: "Solicitud aprobada o rechazada",
    EVENT_RESERVATION_REVIEWED: "Reserva aprobada o rechazada",
    EVENT_LOANS_OVERDUE: "Resumen de préstamos vencidos",
    EVENT_TEST: "Correo de prueba",
}
AUDIENCE_MANAGERS = "managers"
AUDIENCE_REQUESTER = "requester"
EVENT_AUDIENCE = {event: AUDIENCE_MANAGERS for event in (*EVENTS, EVENT_TEST)}
EVENT_AUDIENCE.update({EVENT_REQUEST_REVIEWED: AUDIENCE_REQUESTER, EVENT_RESERVATION_REVIEWED: AUDIENCE_REQUESTER})
EVENT_HELP = {
    EVENT_REQUEST_CREATED: "A los perfiles maestro: quién pidió qué producto o servicio y para cuándo.",
    EVENT_RESERVATION_CREATED: "A los perfiles maestro: quién reservó qué actividad y en qué horario.",
    EVENT_LOAN_CHECKOUT: "A los perfiles maestro: quién retiró qué material y hasta cuándo.",
    EVENT_LOAN_CHECKIN: "A los perfiles maestro: qué material volvió y si llegó a tiempo.",
    EVENT_REQUEST_REVIEWED: "Al solicitante, con la decisión y la observación del revisor.",
    EVENT_RESERVATION_REVIEWED: "Al solicitante, con la decisión y la observación del revisor.",
    EVENT_LOANS_OVERDUE: "A los perfiles maestro, al pulsar «Enviar resumen de vencidos».",
}

STATUS_SENT = "sent"
STATUS_FAILED = "failed"

DISABLED_MESSAGE = "El aviso por correo de este evento está desactivado (Reportes → Correos)."
QUEUED_MESSAGE = "Aviso por correo en envío a {count} destinatario(s)."
PREPARE_FAILED_MESSAGE = "No se pudo preparar el aviso por correo."
NO_REQUESTER_EMAIL_MESSAGE = "El solicitante no tiene un correo institucional válido."
NO_OVERDUE_MESSAGE = "No hay préstamos vencidos: no se envió ningún correo."

SOURCE_MASTER = "Administrador"
SOURCE_ADMIN = "ADMIN_NOTIFICATION_EMAILS"
SOURCE_PROFESSOR = "Supervisor"

_TYPE_PRODUCT = "product"
_SCOPE_FULL_LAB = "full_lab"
_APPROVED = "approved"


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


# ---------------------------------------------------------------------------
# Preferencias (hoja settings)
# ---------------------------------------------------------------------------

def _flag(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def default_preferences() -> dict:
    return {"events": {event: True for event in EVENTS}, "include_professors": False}


def load_preferences(storage) -> dict:
    """Interruptores guardados por el maestro; un dato ausente o dañado vuelve
    a los de fabrica (todo activo, sin profesores): avisar nunca debe fallar."""
    prefs = default_preferences()
    getter = getattr(storage, "get_setting", None)
    if getter is None:
        return prefs
    try:
        raw = getter(SETTINGS_KEY)
        data = json.loads(raw) if raw else {}
    except Exception:
        return prefs
    if not isinstance(data, dict):
        return prefs
    events = data.get("events")
    if isinstance(events, dict):
        for event in EVENTS:
            if event in events:
                prefs["events"][event] = _flag(events[event], True)
    prefs["include_professors"] = _flag(data.get("include_professors"), False)
    return prefs


def save_preferences(storage, events: dict, include_professors: bool = False, actor_email: str = "") -> dict:
    """Guarda los interruptores (solo eventos conocidos) en una sola escritura."""
    prefs = default_preferences()
    for event in EVENTS:
        if event in (events or {}):
            prefs["events"][event] = bool(events[event])
    prefs["include_professors"] = bool(include_professors)
    storage.set_setting(SETTINGS_KEY, json.dumps(prefs, sort_keys=True), actor_email=actor_email)
    return prefs


def is_event_enabled(storage, event: str, prefs: dict = None) -> bool:
    if event == EVENT_TEST:
        return True
    prefs = prefs or load_preferences(storage)
    return bool(prefs["events"].get(event, True))


# ---------------------------------------------------------------------------
# Destinatarios
# ---------------------------------------------------------------------------

def recipient_sources(storage, prefs: dict = None) -> list:
    """[(correo, origen), ...] para los avisos a responsables: perfiles maestro
    activos, la lista ADMIN_NOTIFICATION_EMAILS y, si el maestro lo activo, los
    profesores activos. Sin duplicados (gana el primer origen) ni correos
    invalidos o anonimos."""
    prefs = prefs or load_preferences(storage)
    users = []
    if storage is not None:
        try:
            users = storage.get_all_users()
        except Exception as exc:
            logger.warning(f"No se pudieron leer los usuarios para los correos: {exc}")

    def active(role):
        return sorted(
            str(user.get("institutional_email") or "") for user in users
            if user.get("status") == "active" and user.get("role") == role
        )

    groups = [(SOURCE_MASTER, active("maestro")), (SOURCE_ADMIN, notifications.get_configured_admin_emails())]
    if prefs.get("include_professors"):
        groups.append((SOURCE_PROFESSOR, active("profesor")))
    seen, result = set(), []
    for source, emails in groups:
        for raw in emails:
            email = notifications.deliverable_email(raw)
            if email and email not in seen:
                seen.add(email)
                result.append((email, source))
    return result


def manager_recipients(storage, prefs: dict = None) -> list:
    """Correos de los responsables (ver recipient_sources), ordenados."""
    return sorted(email for email, _source in recipient_sources(storage, prefs))


def _requester_recipients(email) -> list:
    email = notifications.deliverable_email(email)
    return [email] if email else []


# ---------------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------------

def _to_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_local(value) -> str:
    """Fecha y hora de Bogota legible (09/10/2026 3:05 p. m.); "" si no hay fecha."""
    dt = _to_datetime(value)
    if not dt:
        return ""
    local = dt.astimezone(BOGOTA_TZ)
    hour = local.strftime("%I:%M").lstrip("0")
    return f"{local.strftime('%d/%m/%Y')} {hour} {'a. m.' if local.hour < 12 else 'p. m.'}"


def _format_deadline(value) -> str:
    """Fecha limite de un prestamo: solo el dia si se eligio sin hora (se guarda
    como medianoche UTC, ver core.loans.deadline_from_date)."""
    dt = _to_datetime(value)
    if not dt:
        return ""
    utc = dt.astimezone(timezone.utc)
    if utc.timetz().replace(tzinfo=None) == time.min:
        return utc.strftime("%d/%m/%Y")
    return format_local(dt)


def _now_text() -> str:
    return f"{format_local(datetime.now(timezone.utc))} (hora de Bogotá)"


def _when_text(value) -> str:
    return f"{format_local(value)} (hora de Bogotá)" if _to_datetime(value) else _now_text()


def _one_line(value) -> str:
    return " ".join(str(value if value is not None else "").split())


def _clip(value, limit: int = 70) -> str:
    text = _one_line(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _short_id(value) -> str:
    return f"#{str(value)[:6]}" if value else ""


def role_label(role) -> str:
    return ROLE_LABELS.get(role or "", str(role or "Usuario").capitalize())


def role_short_label(role) -> str:
    return ROLE_SHORT_LABELS.get(role or "", str(role or "Usuario").capitalize())


def _person(user: dict) -> str:
    return f"{_one_line(user.get('full_name')) or 'Sin nombre'} ({role_short_label(user.get('role'))})"


def _who_rows(user: dict) -> list:
    return [
        ("Nombre", user.get("full_name") or "Sin nombre"),
        ("Rol", role_label(user.get("role"))),
        ("Correo", user.get("institutional_email")),
        ("ID estudiante", user.get("student_id")),
    ]


def _item_text(name, code) -> str:
    name, code = _one_line(name), _one_line(code)
    if name and code:
        return f"{name} ({code})"
    return name or code


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _text_body(title: str, intro: str, blocks: list, action: str, link: str) -> str:
    lines = [title, ""]
    if intro:
        lines.append(intro)
    for heading, rows in blocks:
        lines += ["", heading.upper()]
        lines += [f"  {label}: {value}" for label, value in rows]
    if action:
        lines += ["", action]
    if link:
        lines += ["", f"Abrir la aplicación: {link}"]
    lines += ["", "--", FOOTER]
    return "\n".join(lines)


def _html_body(title: str, intro: str, blocks: list, action: str, link: str) -> str:
    """HTML sencillo con estilos en linea (los clientes de correo ignoran las
    hojas de estilo): encabezado azul con filete amarillo y una tabla por bloque."""
    cell = "padding:6px 8px;border-bottom:1px solid #E5EAF2;vertical-align:top;"
    parts = [f'<p style="margin:0;">{_esc(intro)}</p>'] if intro else []
    for heading, rows in blocks:
        body = "".join(
            f'<tr><td style="{cell}color:#5A6B85;width:38%;">{_esc(label)}</td>'
            f'<td style="{cell}">{_esc(value)}</td></tr>'
            for label, value in rows
        )
        parts.append(
            f'<h3 style="font-size:13px;color:{BRAND_BLUE};text-transform:uppercase;letter-spacing:.06em;'
            f'margin:18px 0 6px;">{_esc(heading)}</h3>'
            f'<table role="presentation" style="width:100%;border-collapse:collapse;font-size:14px;">{body}</table>'
        )
    if action:
        parts.append(f'<p style="margin:18px 0 0;">{_esc(action)}</p>')
    if link:
        parts.append(
            f'<p style="margin:18px 0 0;"><a href="{_esc(link)}" style="display:inline-block;'
            f"background:{BRAND_BLUE};color:#FFFFFF;text-decoration:none;padding:10px 18px;border-radius:8px;"
            'font-weight:bold;">Abrir la aplicación</a></p>'
        )
    return (
        '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;background:#F4F6FA;">'
        '<div style="max-width:600px;margin:0 auto;padding:16px;font-family:Arial,Helvetica,sans-serif;'
        'color:#0E1A2E;">'
        f'<div style="background:{BRAND_BLUE};color:#FFFFFF;padding:14px 18px;border-radius:10px 10px 0 0;'
        f'border-bottom:4px solid {BRAND_GOLD};">'
        '<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;">'
        "Laboratorio de Ingeniería · UNIMINUTO</div>"
        f'<div style="font-size:19px;font-weight:bold;margin-top:4px;">{_esc(title)}</div></div>'
        '<div style="background:#FFFFFF;padding:18px;border:1px solid #C8D1DE;border-top:0;'
        f'border-radius:0 0 10px 10px;">{"".join(parts)}</div>'
        f'<p style="font-size:12px;color:#5A6B85;text-align:center;margin:12px 0 0;">{_esc(FOOTER)}</p>'
        "</div></body></html>"
    )


def compose(subject: str, title: str, intro: str, sections: list, action: str = "",
            page: str = "") -> EmailContent:
    """Arma el correo en texto plano y HTML.

    sections: [(titulo, [(etiqueta, valor), ...]), ...]; las filas sin valor se
    omiten. `page` es la ruta de la pagina en la app (p. ej. "solicitudes"), que
    se enlaza si existe el secret APP_URL. Todo texto se escapa en el HTML."""
    url = notifications.get_app_url()
    link = f"{url}/{page}" if url and page else url
    blocks = []
    for heading, rows in sections:
        rows = [(label, _one_line(value)) for label, value in rows if value is not None]
        rows = [(label, value) for label, value in rows if value]
        if rows:
            blocks.append((heading, rows))
    return EmailContent(
        _clip(f"{SUBJECT_PREFIX} {_one_line(subject)}", 160),
        _text_body(title, intro, blocks, action, link),
        _html_body(title, intro, blocks, action, link),
    )


# ---------------------------------------------------------------------------
# Contenido de cada evento
# ---------------------------------------------------------------------------

def _requester(record: dict, user: dict = None) -> dict:
    """Datos de quien pidio: los de su cuenta y, si faltan, los guardados en el registro."""
    base = {"full_name": record.get("requester_name"), "institutional_email": record.get("requester_email")}
    return {**base, **{key: value for key, value in (user or {}).items() if value not in (None, "")}}


def _request_target(request: dict) -> str:
    if request.get("request_type") == _TYPE_PRODUCT:
        return request.get("item_name") or request.get("item_id") or "Producto"
    return request.get("service_name") or "Servicio"


def _request_rows(request: dict) -> list:
    if request.get("request_type") == _TYPE_PRODUCT:
        rows = [
            ("Tipo", "Producto del inventario"),
            ("Producto", _item_text(request.get("item_name"), request.get("item_id"))),
            ("Cantidad", f"{request.get('quantity') or 1} unidad(es)"),
        ]
    else:
        rows = [("Tipo", "Servicio del laboratorio"), ("Servicio", request.get("service_name"))]
    return rows + [
        ("Fecha requerida", format_local(request.get("needed_at")) or "No indicada"),
        ("Descripción", request.get("description")),
    ]


def request_created_content(request: dict, requester: dict = None) -> EmailContent:
    who = _requester(request, requester)
    target = _request_target(request)
    product = request.get("request_type") == _TYPE_PRODUCT
    return compose(
        f"Nueva solicitud: {_clip(target)} — {_person(who)}",
        "Nueva solicitud de producto" if product else "Nueva solicitud de servicio",
        f"{_person(who)} registró una solicitud en el laboratorio que espera aprobación.",
        [
            ("Quién la pide", _who_rows(who)),
            ("Qué pide", _request_rows(request)),
            ("Cuándo", [("Registrada", _when_text(request.get("created_at"))),
                        ("Referencia", _short_id(request.get("id")))]),
        ],
        action="Revísala en la aplicación (Solicitudes → Gestionar) para aprobarla o rechazarla.",
        page="solicitudes",
    )


def _reservation_rows(reservation: dict) -> list:
    full_lab = reservation.get("scope_type") == _SCOPE_FULL_LAB
    return [
        ("Alcance", "Laboratorio completo" if full_lab else "Actividad específica"),
        ("Actividad", None if full_lab else reservation.get("activity")),
        ("Inicio", format_local(reservation.get("start_at"))),
        ("Fin", format_local(reservation.get("end_at"))),
        ("Asistentes", reservation.get("attendees")),
        ("Propósito", reservation.get("purpose")),
    ]


def reservation_created_content(reservation: dict, requester: dict = None) -> EmailContent:
    who = _requester(reservation, requester)
    activity = reservation.get("activity") or "Laboratorio completo"
    return compose(
        f"Nueva reserva: {_clip(activity)} — {_person(who)}",
        "Nueva reserva del laboratorio",
        f"{_person(who)} solicitó una reserva que espera aprobación.",
        [
            ("Quién la pide", _who_rows(who)),
            ("Qué reserva", _reservation_rows(reservation)),
            ("Cuándo", [("Registrada", _when_text(reservation.get("created_at"))),
                        ("Referencia", _short_id(reservation.get("id")))]),
        ],
        action="Revísala en la aplicación (Solicitudes → Gestionar) para aprobarla o rechazarla.",
        page="solicitudes",
    )


def loan_checkout_content(loan: dict, item: dict, borrower: dict) -> EmailContent:
    item = item or {}
    name = loan.get("item_name") or item.get("name") or loan.get("item_id")
    quantity = loan.get("quantity") or 1
    return compose(
        f"Salida: {_clip(name)} x{quantity} — {_person(borrower)}",
        "Salida de un préstamo",
        f"{_person(borrower)} retiró material del laboratorio.",
        [
            ("Quién lo retira", _who_rows(borrower)),
            ("Qué retira", [
                ("Producto", _item_text(name, loan.get("item_id") or item.get("id"))),
                ("Cantidad", f"{quantity} unidad(es)"),
                ("Ubicación", item.get("location")),
                ("Devolver antes de", _format_deadline(loan.get("expected_return_at")) or "Sin fecha límite"),
                ("Notas", loan.get("notes")),
            ]),
            ("Cuándo", [("Salida registrada", _when_text(loan.get("checkout_at"))),
                        ("Despachado por", loan.get("operator_name")),
                        ("Referencia del préstamo", _short_id(loan.get("id")))]),
        ],
        action="Consulta quién tiene qué en la aplicación (Préstamos).",
        page="prestamos",
    )


def loan_checkin_content(loan: dict, borrower: dict, actor: dict = None) -> EmailContent:
    from core import loans as loans_core  # import diferido: core.loans importa este modulo

    name = loan.get("item_name") or loan.get("item_id")
    quantity = loan.get("quantity") or 1
    returned = _to_datetime(loan.get("return_at")) or datetime.now(timezone.utc)
    expected = _to_datetime(loan.get("expected_return_at"))
    if not expected:
        timing = "Sin fecha límite"
    elif returned > loans_core.due_instant(expected):
        timing = "Devuelto con retraso"
    else:
        timing = "Devuelto a tiempo"
    who = _who_rows(borrower)
    if actor and actor.get("id") and actor.get("id") != loan.get("user_id"):
        who.append(("Registró la devolución", _person(actor)))
    return compose(
        f"Devolución: {_clip(name)} x{quantity} — {_person(borrower)}",
        "Devolución de un préstamo",
        f"Volvió al laboratorio material prestado a {_person(borrower)}.",
        [
            ("Quién lo tenía", who),
            ("Qué devuelve", [
                ("Producto", _item_text(name, loan.get("item_id"))),
                ("Cantidad", f"{quantity} unidad(es)"),
                ("Salida", format_local(loan.get("checkout_at"))),
                ("Fecha límite", _format_deadline(expected)),
                ("Estado", timing),
                ("Despachado por", loan.get("operator_name")),
            ]),
            ("Cuándo", [("Devolución registrada", _when_text(returned)),
                        ("Referencia del préstamo", _short_id(loan.get("id")))]),
        ],
        action="Consulta el historial completo en la aplicación (Préstamos).",
        page="prestamos",
    )


def _decision_text(decision: str, feminine: bool = True) -> str:
    if decision == _APPROVED:
        return "aprobada" if feminine else "aprobado"
    return "rechazada" if feminine else "rechazado"


def request_reviewed_content(request: dict, decision: str, reviewer: dict, notes: str = "") -> EmailContent:
    who = _requester(request)
    verdict = _decision_text(decision)
    target = _request_target(request)
    product = request.get("request_type") == _TYPE_PRODUCT
    if decision != _APPROVED:
        action = "Si aún lo necesitas, crea una nueva solicitud con los ajustes indicados."
    elif product:
        action = ("Retira el producto en el laboratorio confirmando la salida en Buscar; en Trazabilidad "
                  "puedes seguir la ruta verificable hasta él.")
    else:
        action = "Sigue su avance (en curso → entregado) en Solicitudes o en Trazabilidad."
    first_name = _one_line(who.get("full_name")).split(" ")[0] or "Hola"
    return compose(
        f"Tu solicitud fue {verdict}: {_clip(target)}",
        f"Solicitud {verdict}",
        f"Hola, {first_name}. Tu solicitud de «{_clip(target, 120)}» fue {verdict} por {_person(reviewer)}.",
        [
            ("Tu solicitud", _request_rows(request)),
            ("Respuesta", [
                ("Decisión", verdict.capitalize()),
                ("Revisó", _person(reviewer)),
                ("Observación", notes or "Sin observaciones"),
                ("Fecha", _now_text()),
                ("Referencia", _short_id(request.get("id"))),
            ]),
        ],
        action=action,
        page="solicitudes",
    )


def reservation_reviewed_content(reservation: dict, decision: str, reviewer: dict, notes: str = "") -> EmailContent:
    who = _requester(reservation)
    verdict = _decision_text(decision)
    activity = reservation.get("activity") or "Laboratorio completo"
    first_name = _one_line(who.get("full_name")).split(" ")[0] or "Hola"
    action = ("Te esperamos en el laboratorio en el horario reservado." if decision == _APPROVED
              else "Si aún la necesitas, solicita otra reserva en un horario distinto.")
    return compose(
        f"Tu reserva fue {verdict}: {_clip(activity)}",
        f"Reserva {verdict}",
        f"Hola, {first_name}. Tu reserva «{_clip(activity, 120)}» fue {verdict} por {_person(reviewer)}.",
        [
            ("Tu reserva", _reservation_rows(reservation)),
            ("Respuesta", [
                ("Decisión", verdict.capitalize()),
                ("Revisó", _person(reviewer)),
                ("Observación", notes or "Sin observaciones"),
                ("Fecha", _now_text()),
                ("Referencia", _short_id(reservation.get("id"))),
            ]),
        ],
        action=action,
        page="solicitudes",
    )


def overdue_content(overdue: list, actor: dict = None) -> EmailContent:
    rows = [
        (f"{_one_line(loan.get('item_name') or loan.get('item_id'))} x{loan.get('quantity') or 1}",
         f"{_one_line(loan.get('user_name')) or 'Sin nombre'} ({role_short_label(loan.get('user_role'))}) · "
         f"debía devolverse el {_format_deadline(loan.get('expected_return_at'))}")
        for loan in overdue
    ]
    when = [("Resumen generado", _now_text())]
    if actor:
        when.append(("Enviado por", _person(actor)))
    return compose(
        f"Préstamos vencidos: {len(overdue)}",
        "Préstamos vencidos",
        f"Hay {len(overdue)} préstamo(s) que debieron devolverse y siguen fuera del laboratorio.",
        [("Vencidos", rows), ("Cuándo", when)],
        action="Contacta a cada persona y registra el reingreso en Buscar o en Préstamos.",
        page="prestamos",
    )


def diagnostic_content(actor: dict, prefs: dict = None) -> EmailContent:
    prefs = prefs or default_preferences()
    active = sum(1 for event in EVENTS if prefs["events"].get(event, True))
    status = notifications.smtp_status()
    return compose(
        "Correo de prueba",
        "Correo de prueba",
        "Si lees este mensaje, los correos automáticos del laboratorio funcionan.",
        [
            ("Enviado por", _who_rows(actor or {})),
            ("Configuración", [
                ("Conexión", status["security"]),
                ("Avisos activos", f"{active} de {len(EVENTS)}"),
                ("Incluye supervisores", "Sí" if prefs.get("include_professors") else "No"),
            ]),
            ("Cuándo", [("Enviado", _now_text())]),
        ],
    )


# ---------------------------------------------------------------------------
# Envio en segundo plano y registro
# ---------------------------------------------------------------------------

_pending_lock = threading.Lock()
_pending_threads = set()


def _run_in_background(target, name: str) -> None:
    def runner():
        try:
            target()
        except Exception as exc:  # el hilo nunca termina con una traza sin registrar
            logger.error(f"Fallo inesperado enviando un correo automatico: {type(exc).__name__}: {exc}")
        finally:
            with _pending_lock:
                _pending_threads.discard(threading.current_thread())

    thread = threading.Thread(target=runner, name=name, daemon=True)
    with _pending_lock:
        _pending_threads.add(thread)
    thread.start()


def pending_count() -> int:
    with _pending_lock:
        return len(_pending_threads)


def wait_for_pending(timeout: float = 10.0) -> bool:
    """Espera a que terminen los envios en curso (pruebas o cierre ordenado).
    Devuelve True si ya no queda ninguno."""
    deadline = _time.monotonic() + timeout
    while True:
        with _pending_lock:
            threads = list(_pending_threads)
        if not threads:
            return True
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return False
        threads[0].join(remaining)


def _safe_call(callback, *args) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception as exc:
        logger.warning(f"No se pudo guardar el resultado del correo: {type(exc).__name__}: {exc}")


def _record(storage, event: str, subject: str, recipients: list, ok: bool, message: str,
            reference: str = "", actor_email: str = "") -> None:
    adder = getattr(storage, "add_notification_log", None)
    if adder is None:
        return
    try:
        adder({
            "event_type": event, "subject": subject, "recipients": ", ".join(recipients),
            "recipient_count": len(recipients), "status": STATUS_SENT if ok else STATUS_FAILED,
            "error": "" if ok else message, "reference": reference or "", "actor_email": actor_email or "",
        })
    except Exception as exc:
        logger.warning(f"No se pudo registrar el correo en notification_log: {type(exc).__name__}: {exc}")


def _deliver(storage, event: str, content: EmailContent, recipients: list, on_done=None,
             reference: str = "", actor_email: str = "", log: bool = True) -> tuple:
    """Envia (o explica por que no), avisa a `on_done(ok, mensaje)` y registra."""
    if not recipients and EVENT_AUDIENCE.get(event) == AUDIENCE_REQUESTER:
        ok, message = False, NO_REQUESTER_EMAIL_MESSAGE
    else:
        try:
            ok, message = notifications.send_email_notification(
                content.subject, content.text, recipients, html_body=content.html
            )
        except Exception as exc:  # send_email_notification no lanza; un reemplazo podria
            logger.error(f"Fallo inesperado al enviar correo: {type(exc).__name__}: {exc}")
            ok, message = False, PREPARE_FAILED_MESSAGE
    _safe_call(on_done, ok, message)
    if log:
        _record(storage, event, content.subject, recipients, ok, message, reference, actor_email)
    return ok, message


def _dispatch(storage, event: str, build, on_done=None, reference: str = "", actor_email: str = "") -> tuple:
    """Nucleo de todos los avisos. `build(prefs)` devuelve (EmailContent, destinatarios).

    - SMTP sin configurar y nada que guardar (`on_done` None): no hace nada.
    - Evento desactivado: no envia y lo informa a `on_done`.
    - SMTP configurado: envia en segundo plano y responde de inmediato.
    - Sin SMTP: responde al instante con el motivo (no hay red que esperar).
    Devuelve (ok, mensaje) y nunca lanza."""
    try:
        configured = notifications.smtp_configured()
        if not configured and on_done is None:
            return False, notifications.SMTP_NOT_CONFIGURED_MESSAGE
        prefs = load_preferences(storage)
        if not is_event_enabled(storage, event, prefs):
            _safe_call(on_done, False, DISABLED_MESSAGE)
            return False, DISABLED_MESSAGE
        content, recipients = build(prefs)
        if not configured:
            return _deliver(storage, event, content, recipients, on_done, reference, actor_email, log=False)
        _run_in_background(
            lambda: _deliver(storage, event, content, recipients, on_done, reference, actor_email),
            name=f"lab-email-{event}",
        )
        if recipients:
            return True, QUEUED_MESSAGE.format(count=len(recipients))
        if EVENT_AUDIENCE.get(event) == AUDIENCE_REQUESTER:
            return False, NO_REQUESTER_EMAIL_MESSAGE
        return False, notifications.NO_RECIPIENTS_MESSAGE
    except Exception as exc:
        logger.error(f"No se pudo preparar el correo '{event}': {type(exc).__name__}: {exc}")
        _safe_call(on_done, False, PREPARE_FAILED_MESSAGE)
        return False, PREPARE_FAILED_MESSAGE


# ---------------------------------------------------------------------------
# API para los flujos (una linea en cada uno)
# ---------------------------------------------------------------------------

def notify_request_created(storage, request: dict, requester: dict = None) -> tuple:
    """Solicitud nueva -> responsables. El resultado queda en la propia solicitud
    (email_notified / email_error), como antes de este modulo."""
    request = request or {}

    def on_done(ok, message):
        storage.update_service_request_notification(request.get("id"), ok, "" if ok else message)

    return _dispatch(
        storage, EVENT_REQUEST_CREATED,
        lambda prefs: (request_created_content(request, requester), manager_recipients(storage, prefs)),
        on_done=on_done, reference=request.get("id", ""),
        actor_email=(requester or {}).get("institutional_email") or request.get("requester_email", ""),
    )


def notify_reservation_created(storage, reservation: dict, requester: dict = None) -> tuple:
    """Reserva nueva -> responsables. El resultado queda en la propia reserva."""
    reservation = reservation or {}

    def on_done(ok, message):
        storage.update_reservation_notification(reservation.get("id"), ok, "" if ok else message)

    return _dispatch(
        storage, EVENT_RESERVATION_CREATED,
        lambda prefs: (reservation_created_content(reservation, requester), manager_recipients(storage, prefs)),
        on_done=on_done, reference=reservation.get("id", ""),
        actor_email=(requester or {}).get("institutional_email") or reservation.get("requester_email", ""),
    )


def notify_loan_checkout(storage, loan: dict, item: dict, borrower: dict) -> tuple:
    """Salida registrada -> responsables."""
    loan, borrower = loan or {}, borrower or {}
    return _dispatch(
        storage, EVENT_LOAN_CHECKOUT,
        lambda prefs: (loan_checkout_content(loan, item, borrower), manager_recipients(storage, prefs)),
        reference=loan.get("id", ""), actor_email=borrower.get("institutional_email", ""),
    )


def _find_loan(storage, loan_id: str):
    for loan in storage.get_all_loans():
        if loan.get("id") == loan_id:
            return loan
    return None


def notify_loan_checkin(storage, loan_id: str, actor: dict) -> tuple:
    """Reingreso registrado -> responsables (el prestamo se busca solo si hay que avisar)."""
    def build(prefs):
        loan = _find_loan(storage, loan_id) or {"id": loan_id}
        borrower = (storage.get_user_by_id(loan["user_id"]) if loan.get("user_id") else None) or {
            "full_name": loan.get("user_name"), "role": loan.get("user_role"),
        }
        return loan_checkin_content(loan, borrower, actor), manager_recipients(storage, prefs)

    return _dispatch(storage, EVENT_LOAN_CHECKIN, build, reference=loan_id,
                     actor_email=(actor or {}).get("institutional_email", ""))


def notify_request_reviewed(storage, request: dict, decision: str, reviewer: dict, notes: str = "") -> tuple:
    """Solicitud aprobada o rechazada -> solicitante."""
    request = request or {}
    return _dispatch(
        storage, EVENT_REQUEST_REVIEWED,
        lambda prefs: (request_reviewed_content(request, decision, reviewer, notes),
                       _requester_recipients(request.get("requester_email"))),
        reference=request.get("id", ""), actor_email=(reviewer or {}).get("institutional_email", ""),
    )


def notify_reservation_reviewed(storage, reservation: dict, decision: str, reviewer: dict,
                                notes: str = "") -> tuple:
    """Reserva aprobada o rechazada -> solicitante."""
    reservation = reservation or {}
    return _dispatch(
        storage, EVENT_RESERVATION_REVIEWED,
        lambda prefs: (reservation_reviewed_content(reservation, decision, reviewer, notes),
                       _requester_recipients(reservation.get("requester_email"))),
        reference=reservation.get("id", ""), actor_email=(reviewer or {}).get("institutional_email", ""),
    )


def notify_overdue_loans(storage, actor: dict = None) -> tuple:
    """Resumen de prestamos vencidos -> responsables (lo pide el maestro a mano)."""
    from core import loans as loans_core  # import diferido: core.loans importa este modulo

    try:
        overdue = loans_core.get_overdue_loans(storage)
    except Exception as exc:
        logger.error(f"No se pudieron leer los prestamos vencidos: {exc}")
        return False, PREPARE_FAILED_MESSAGE
    if not overdue:
        return False, NO_OVERDUE_MESSAGE
    return _dispatch(
        storage, EVENT_LOANS_OVERDUE,
        lambda prefs: (overdue_content(overdue, actor), manager_recipients(storage, prefs)),
        actor_email=(actor or {}).get("institutional_email", ""),
    )


def send_test_email(storage, actor: dict, to_all: bool = False) -> tuple:
    """Correo de prueba, SINCRONO (quien lo pide espera el resultado): a quien lo
    pide o, con `to_all`, a todos los responsables. Queda en el registro."""
    try:
        prefs = load_preferences(storage)
        if to_all:
            recipients = manager_recipients(storage, prefs)
        else:
            recipients = _requester_recipients((actor or {}).get("institutional_email"))
        content = diagnostic_content(actor, prefs)
        return _deliver(storage, EVENT_TEST, content, recipients,
                        actor_email=(actor or {}).get("institutional_email", ""),
                        log=notifications.smtp_configured())
    except Exception as exc:
        logger.error(f"No se pudo preparar el correo de prueba: {type(exc).__name__}: {exc}")
        return False, PREPARE_FAILED_MESSAGE


def recent_log(storage, limit: int = 20) -> list:
    """Ultimos registros de correo (vacio si la base no tiene la hoja)."""
    getter = getattr(storage, "get_notification_log", None)
    if getter is None:
        return []
    try:
        return getter(limit=limit)
    except Exception as exc:
        logger.warning(f"No se pudo leer notification_log: {exc}")
        return []
