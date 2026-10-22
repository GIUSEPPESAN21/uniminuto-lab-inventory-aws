# -*- coding: utf-8 -*-
"""Notificaciones opcionales por WhatsApp (Twilio) y correo SMTP.

Un fallo de proveedor nunca invalida la operación principal: las solicitudes
se guardan primero y la interfaz informa si la notificación no pudo enviarse.
Las credenciales solo se leen desde st.secrets y nunca se registran.

Los correos automaticos por evento (quien pidio que, a quien se avisa, envio en
segundo plano y registro) viven en core/email_events.py; este modulo solo sabe
leer la configuracion SMTP y hablar con el servidor.
"""

import logging
import re
import smtplib
import socket
import ssl
import threading
from email.message import EmailMessage

from core.config import safe_secret

logger = logging.getLogger(__name__)

WHATSAPP_SECRETS = [
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM_NUMBER", "DESTINATION_WHATSAPP_NUMBER",
]
_EMAIL_SPLIT_RE = re.compile(r"[,;\s]+")

# Secrets del correo, en el orden en que se explican en la pantalla del maestro.
SMTP_SECRET_KEYS = (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL",
    "SMTP_USE_TLS", "SMTP_USE_SSL",
)
SMTP_NOT_CONFIGURED_MESSAGE = "Correo no configurado: faltan SMTP_HOST o SMTP_FROM_EMAIL."
SMTP_BAD_PORT_MESSAGE = "SMTP_PORT debe ser un número entero."
NO_RECIPIENTS_MESSAGE = "No hay correos administrativos configurados o activos."

# Direcciones que nunca son una persona real: buzones anonimos o de "no
# responder" y dominios reservados para ejemplos y pruebas (RFC 2606).
_EMAIL_FORMAT_RE = re.compile(r"^[^@\s<>(),;:\"\[\]]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
_ANONYMOUS_LOCAL_PARTS = {
    "anonimo", "anónimo", "anonymous", "anon", "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "nobody", "invitado", "guest",
}
_RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
_RESERVED_SUFFIXES = (".invalid", ".test", ".example", ".localhost", ".local")


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _clean_email(value: str) -> str:
    value = (value or "").strip().lower()
    if "\n" in value or "\r" in value or value.count("@") != 1:
        return ""
    return value


def deliverable_email(value) -> str:
    """Correo normalizado si es una direccion real a la que vale la pena
    escribir; "" si es invalida, anonima ("noreply", "anonimo"...) o de un
    dominio reservado para ejemplos (example.com, .test, .invalid...)."""
    email = _clean_email(str(value or ""))
    if not email or not _EMAIL_FORMAT_RE.match(email):
        return ""
    local, domain = email.split("@")
    if local in _ANONYMOUS_LOCAL_PARTS:
        return ""
    if domain in _RESERVED_DOMAINS or domain.endswith(_RESERVED_SUFFIXES):
        return ""
    return email


def _configured_admin_emails() -> list:
    raw = safe_secret("ADMIN_NOTIFICATION_EMAILS", "")
    values = raw if isinstance(raw, (list, tuple)) else _EMAIL_SPLIT_RE.split(str(raw or ""))
    return sorted({_clean_email(v) for v in values if _clean_email(v)})


def get_configured_admin_emails() -> list:
    """Solo la lista ADMIN_NOTIFICATION_EMAILS de los Secrets (sin respaldo)."""
    return _configured_admin_emails()


def get_admin_notification_emails(storage=None) -> list:
    """Destinatarios explícitos o, como respaldo, profesores/maestros activos."""
    configured = _configured_admin_emails()
    if configured:
        return configured
    if not storage:
        return []
    try:
        return sorted({
            _clean_email(user.get("institutional_email", ""))
            for user in storage.get_all_users()
            if user.get("status") == "active"
            and user.get("role") in ("profesor", "maestro")
            and _clean_email(user.get("institutional_email", ""))
        })
    except Exception as exc:
        logger.warning(f"No se pudieron obtener destinatarios administrativos: {exc}")
        return []


def get_app_url() -> str:
    """Direccion publica de la app (secret APP_URL) para enlazarla en los
    correos; "" si no esta definida o no es una URL http(s)."""
    url = str(safe_secret("APP_URL", "") or "").strip()
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", url, flags=re.IGNORECASE):
        return ""
    return url.rstrip("/")


def _smtp_settings() -> dict:
    """Configuracion SMTP leida de los Secrets. `port` es None si no es entero."""
    username = str(safe_secret("SMTP_USERNAME", "") or "").strip()
    try:
        port = int(safe_secret("SMTP_PORT", 587) or 587)
    except (TypeError, ValueError):
        port = None
    use_ssl = _as_bool(safe_secret("SMTP_USE_SSL", False))
    return {
        "host": str(safe_secret("SMTP_HOST", "") or "").strip(),
        "username": username,
        "password": str(safe_secret("SMTP_PASSWORD", "") or ""),
        "from_email": _clean_email(str(safe_secret("SMTP_FROM_EMAIL", username) or username)),
        "port": port,
        "use_ssl": use_ssl,
        "use_tls": _as_bool(safe_secret("SMTP_USE_TLS", not use_ssl), default=not use_ssl),
    }


def smtp_configured() -> bool:
    """True si hay servidor, remitente y puerto valido: lo minimo para intentar
    un envio (la contraseña solo la puede comprobar el servidor)."""
    settings = _smtp_settings()
    return bool(settings["host"] and settings["from_email"] and settings["port"])


def smtp_status() -> dict:
    """Estado de la configuracion de correo para la pantalla del maestro, SIN
    valores: solo que secrets existen, cuales faltan y avisos de coherencia."""
    settings = _smtp_settings()
    present = {key: safe_secret(key, None) not in (None, "") for key in SMTP_SECRET_KEYS}
    present["ADMIN_NOTIFICATION_EMAILS"] = bool(_configured_admin_emails())
    present["APP_URL"] = bool(get_app_url())
    missing = [key for key, ok in (("SMTP_HOST", settings["host"]),
                                   ("SMTP_FROM_EMAIL", settings["from_email"])) if not ok]
    warnings = []
    if settings["port"] is None:
        warnings.append(SMTP_BAD_PORT_MESSAGE)
    if settings["username"] and not settings["password"]:
        warnings.append("Hay SMTP_USERNAME pero falta SMTP_PASSWORD: el servidor rechazará el acceso.")
    if settings["use_ssl"] and settings["port"] == 587:
        warnings.append("SMTP_USE_SSL = true se usa con el puerto 465; con el 587 usa SMTP_USE_TLS = true.")
    if settings["use_ssl"]:
        security = "SSL"
    elif settings["use_tls"]:
        security = "STARTTLS"
    else:
        security = "Sin cifrado"
    return {
        "configured": bool(settings["host"] and settings["from_email"] and settings["port"]),
        "present": present,
        "missing": missing,
        "warnings": warnings,
        "security": security,
        "login": bool(settings["username"]),
    }


def describe_smtp_error(exc: Exception) -> str:
    """Causa probable de un fallo SMTP en palabras simples, sin datos del servidor."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "el servidor rechazó el usuario o la contraseña (en Gmail usa una contraseña de aplicación)"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "el servidor rechazó los destinatarios"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "el servidor rechazó el remitente (SMTP_FROM_EMAIL)"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "el servidor no admite ese modo de conexión (revisa SMTP_USE_TLS y SMTP_USE_SSL)"
    if isinstance(exc, ssl.SSLError):
        return "falló la conexión segura (revisa SMTP_PORT, SMTP_USE_TLS y SMTP_USE_SSL)"
    if isinstance(exc, (socket.gaierror, smtplib.SMTPConnectError, ConnectionError, TimeoutError)):
        return "no se pudo conectar con el servidor (revisa SMTP_HOST y SMTP_PORT)"
    if isinstance(exc, smtplib.SMTPException):
        return "el servidor SMTP respondió con un error"
    if isinstance(exc, OSError):
        return "no se pudo conectar con el servidor (revisa SMTP_HOST y SMTP_PORT)"
    return "error inesperado al enviar"


def send_email_notification(subject: str, body: str, recipients: list, html_body: str = None) -> tuple:
    """Envía el correo por SMTP. Devuelve (ok, mensaje) y nunca propaga.

    `body` es el texto plano; con `html_body` el mensaje lleva además una
    alternativa HTML (multipart/alternative) y cada cliente muestra la mejor."""
    recipients = sorted({_clean_email(v) for v in (recipients or []) if _clean_email(v)})
    if not recipients:
        return False, NO_RECIPIENTS_MESSAGE

    settings = _smtp_settings()
    if settings["port"] is None:
        return False, SMTP_BAD_PORT_MESSAGE
    if not settings["host"] or not settings["from_email"]:
        return False, SMTP_NOT_CONFIGURED_MESSAGE

    message = EmailMessage()
    message["Subject"] = " ".join((subject or "Nueva solicitud").splitlines())
    message["From"] = settings["from_email"]
    message["To"] = ", ".join(recipients)
    message.set_content(body or "Nueva solicitud registrada.")
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        client_class = smtplib.SMTP_SSL if settings["use_ssl"] else smtplib.SMTP
        with client_class(settings["host"], settings["port"], timeout=15) as client:
            if settings["use_tls"] and not settings["use_ssl"]:
                client.starttls(context=context)
            if settings["username"]:
                client.login(settings["username"], settings["password"])
            client.send_message(message)
        return True, f"Correo enviado a {len(recipients)} destinatario(s)."
    except Exception as exc:
        logger.error(f"No se pudo enviar correo SMTP: {type(exc).__name__}: {exc}")
        return False, f"El correo no pudo enviarse: {describe_smtp_error(exc)}. Revisa la configuración SMTP."


def _get_whatsapp_client():
    values = {key: safe_secret(key, "") for key in WHATSAPP_SECRETS}
    if not all(values.values()):
        return None
    try:
        from twilio.rest import Client
        return Client(values["TWILIO_ACCOUNT_SID"], values["TWILIO_AUTH_TOKEN"])
    except Exception as exc:
        logger.warning(f"Twilio no disponible: {exc}")
        return None


def send_whatsapp_alert(message: str) -> bool:
    client = _get_whatsapp_client()
    if not client:
        return False
    try:
        from_number = safe_secret("TWILIO_WHATSAPP_FROM_NUMBER")
        to_number = safe_secret("DESTINATION_WHATSAPP_NUMBER")
        client.messages.create(
            from_=f"whatsapp:{from_number}", body=message, to=f"whatsapp:{to_number}"
        )
        return True
    except Exception as exc:
        logger.error(f"Error al enviar alerta de WhatsApp: {exc}")
        return False


def send_whatsapp_alert_async(message: str) -> None:
    """Envia la alerta en segundo plano: la respuesta de Twilio no debe retrasar
    la interfaz y su resultado nunca altera la operacion que la origino."""
    threading.Thread(target=send_whatsapp_alert, args=(message,), daemon=True).start()
