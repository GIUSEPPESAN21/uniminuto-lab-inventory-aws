# -*- coding: utf-8 -*-
from core import notifications


def test_configured_admin_emails_are_normalized_and_deduplicated(monkeypatch):
    monkeypatch.setattr(
        notifications, "safe_secret",
        lambda key, default=None: "ADMIN@UNIMINUTO.EDU.CO; admin@uniminuto.edu.co other@uniminuto.edu.co"
        if key == "ADMIN_NOTIFICATION_EMAILS" else default,
    )
    assert notifications.get_admin_notification_emails() == [
        "admin@uniminuto.edu.co", "other@uniminuto.edu.co"
    ]


def test_active_professors_and_masters_are_fallback_recipients(storage, monkeypatch):
    monkeypatch.setattr(notifications, "safe_secret", lambda key, default=None: default)
    storage.add_user("student@uniminuto.edu.co", role="estudiante")
    storage.add_user("teacher@uniminuto.edu.co", role="profesor")
    storage.add_user("master@uniminuto.edu.co", role="maestro")
    storage.add_user("disabled@uniminuto.edu.co", role="maestro", status="disabled")
    assert notifications.get_admin_notification_emails(storage) == [
        "master@uniminuto.edu.co", "teacher@uniminuto.edu.co"
    ]


def test_email_without_recipients_or_config_never_raises(monkeypatch):
    monkeypatch.setattr(notifications, "safe_secret", lambda key, default=None: default)
    ok, message = notifications.send_email_notification("Asunto", "Cuerpo", [])
    assert ok is False and "correos" in message
    ok, message = notifications.send_email_notification(
        "Asunto", "Cuerpo", ["admin@uniminuto.edu.co"]
    )
    assert ok is False and "configurado" in message


def test_email_uses_tls_login_and_sanitizes_subject(monkeypatch):
    secrets = {
        "SMTP_HOST": "smtp.example.edu.co", "SMTP_PORT": 587,
        "SMTP_USERNAME": "bot@example.edu.co", "SMTP_PASSWORD": "secret",
        "SMTP_FROM_EMAIL": "bot@example.edu.co", "SMTP_USE_TLS": True,
        "SMTP_USE_SSL": False,
    }
    monkeypatch.setattr(notifications, "safe_secret", lambda key, default=None: secrets.get(key, default))

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.update(host=host, port=port, timeout=timeout)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self, context):
            calls["tls"] = True
        def login(self, username, password):
            calls["login"] = (username, password)
        def send_message(self, message):
            calls["message"] = message

    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    ok, message = notifications.send_email_notification(
        "Nueva\nsolicitud", "Detalle", ["ADMIN@UNIMINUTO.EDU.CO"]
    )
    assert ok is True and "1 destinatario" in message
    assert calls["tls"] is True
    assert calls["login"] == ("bot@example.edu.co", "secret")
    assert calls["message"]["Subject"] == "Nueva solicitud"
    assert calls["message"]["To"] == "admin@uniminuto.edu.co"