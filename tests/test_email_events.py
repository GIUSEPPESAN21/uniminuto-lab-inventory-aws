# -*- coding: utf-8 -*-
"""Correos automaticos (core/email_events.py): destinatarios, contenido,
preferencias por evento, envio en segundo plano, fallos y registro. El
servidor SMTP es siempre falso (smtplib reemplazado): ninguna prueba sale a la red."""

import json
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core import email_events, loans, notifications, reservations, service_requests
from core import storage as storage_module
from core.storage import LabStorage

SMTP_SECRETS = {
    "SMTP_HOST": "smtp.servidor-falso.edu.co", "SMTP_PORT": 587,
    "SMTP_USERNAME": "bot@uniminuto.edu.co", "SMTP_PASSWORD": "clave-de-prueba",
    "SMTP_FROM_EMAIL": "bot@uniminuto.edu.co", "SMTP_USE_TLS": True, "SMTP_USE_SSL": False,
}


class FakeSMTP:
    """Servidor SMTP de mentira: guarda cada mensaje en `sent`. `gate` permite
    frenar el envio (para probar que la interfaz no espera) y `fail_with`
    simula un rechazo del servidor."""
    sent = []
    gate = None
    fail_with = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        pass

    def login(self, username, password):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with

    def send_message(self, message):
        if FakeSMTP.gate is not None:
            FakeSMTP.gate.wait(5)
        FakeSMTP.sent.append(message)


def _secrets(values):
    return lambda key, default=None: values.get(key, default)


@pytest.fixture
def smtp(monkeypatch):
    """SMTP configurado con un servidor falso; espera a que terminen los hilos."""
    FakeSMTP.sent, FakeSMTP.gate, FakeSMTP.fail_with = [], None, None
    monkeypatch.setattr(notifications, "safe_secret", _secrets(dict(SMTP_SECRETS)))
    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    yield FakeSMTP
    if FakeSMTP.gate is not None:
        FakeSMTP.gate.set()
    assert email_events.wait_for_pending(5)


@pytest.fixture
def lab(storage):
    """FakeStorage con registro de correos y usuarios de cada rol."""
    storage.log = []
    storage.add_notification_log = lambda entry: storage.log.append(dict(entry)) or entry
    storage.get_notification_log = lambda limit=50: list(reversed(storage.log))[:limit] if limit else storage.log
    storage.add_user("maestra@uniminuto.edu.co", full_name="Marta Maestra", role="maestro")
    storage.add_user("MAESTRA@uniminuto.edu.co ", full_name="Duplicada", role="maestro")
    storage.add_user("otro.maestro@uniminuto.edu.co", full_name="Otro Maestro", role="maestro")
    storage.add_user("inactivo@uniminuto.edu.co", full_name="Inactivo", role="maestro", status="disabled")
    storage.add_user("noreply@uniminuto.edu.co", full_name="Buzón", role="maestro")
    storage.add_user("sin-arroba", full_name="Roto", role="maestro")
    storage.add_user("profe@uniminuto.edu.co", full_name="Pablo Profe", role="profesor")
    student = storage.add_user("ana@uniminuto.edu.co", full_name="Ana Prueba", role="estudiante",
                               student_id="ID-123")
    storage.student = {key: value for key, value in student.items() if key != "password_hash"}
    storage.add_item("LAB-MIC-01", name="Microscopio", quantity=3, location="Estante 1")
    return storage


def _future(days=1):
    return datetime.now(timezone.utc) + timedelta(days=days)


def _body(message, subtype):
    return message.get_body(preferencelist=(subtype,)).get_content()


# ---------------------------------------------------------------------------
# Destinatarios
# ---------------------------------------------------------------------------

def test_recipients_are_active_masters_plus_admin_list_without_duplicates(lab, monkeypatch):
    monkeypatch.setattr(notifications, "safe_secret", _secrets({
        "ADMIN_NOTIFICATION_EMAILS": "coordinacion@uniminuto.edu.co; Maestra@uniminuto.edu.co anonimo@uniminuto.edu.co",
    }))
    assert email_events.manager_recipients(lab) == [
        "coordinacion@uniminuto.edu.co", "maestra@uniminuto.edu.co", "otro.maestro@uniminuto.edu.co",
    ]
    sources = dict(email_events.recipient_sources(lab))
    assert sources["maestra@uniminuto.edu.co"] == email_events.SOURCE_MASTER   # gana el primer origen
    assert sources["coordinacion@uniminuto.edu.co"] == email_events.SOURCE_ADMIN


def test_professors_are_added_only_when_the_master_enables_it(lab):
    assert "profe@uniminuto.edu.co" not in email_events.manager_recipients(lab)
    email_events.save_preferences(lab, {}, include_professors=True)
    assert "profe@uniminuto.edu.co" in email_events.manager_recipients(lab)


@pytest.mark.parametrize("raw, expected", [
    ("Ana@UNIMINUTO.edu.co", "ana@uniminuto.edu.co"),
    ("noreply@uniminuto.edu.co", ""), ("anonimo@uniminuto.edu.co", ""), ("guest@uniminuto.edu.co", ""),
    ("alguien@example.com", ""), ("alguien@lab.test", ""), ("sin-arroba", ""), ("a@b@c.co", ""),
    ("con espacio@uniminuto.edu.co", ""), ("x@uniminuto.edu.co\nBcc: y@z.co", ""), ("", ""), (None, ""),
])
def test_deliverable_email_rejects_invalid_and_anonymous_addresses(raw, expected):
    assert notifications.deliverable_email(raw) == expected


# ---------------------------------------------------------------------------
# Contenido
# ---------------------------------------------------------------------------

def test_request_email_says_who_requested_what_and_when(lab, monkeypatch):
    monkeypatch.setattr(notifications, "safe_secret", _secrets({"APP_URL": "https://lab.streamlit.app/"}))
    request = {
        "id": "abcdef123456", "request_type": "product", "item_id": "LAB-MIC-01", "item_name": "Microscopio",
        "quantity": 2, "description": "Práctica <script>alert(1)</script>",
        "needed_at": "2026-10-12T15:30:00+00:00", "created_at": "2026-10-10T13:05:00+00:00",
        "requester_name": "Ana Prueba", "requester_email": "ana@uniminuto.edu.co",
    }
    content = email_events.request_created_content(request, lab.student)
    assert content.subject == "[Laboratorio] Nueva solicitud: Microscopio — Ana Prueba (Cliente)"
    for expected in ("Ana Prueba", "Cliente", "ana@uniminuto.edu.co", "ID-123", "Microscopio (LAB-MIC-01)",
                     "2 unidad(es)", "12/10/2026 10:30 a. m.", "10/10/2026 8:05 a. m. (hora de Bogotá)",
                     "#abcdef", "https://lab.streamlit.app/solicitudes"):
        assert expected in content.text, expected
    assert "<script>" not in content.html and "&lt;script&gt;" in content.html
    assert 'href="https://lab.streamlit.app/solicitudes"' in content.html


def test_service_request_and_reservation_subjects_name_the_service_and_activity(lab):
    service = email_events.request_created_content(
        {"request_type": "service", "service_name": "Impresión 3D", "description": "Pieza"},
        {"full_name": "Pablo Profe", "role": "profesor", "institutional_email": "profe@uniminuto.edu.co"},
    )
    assert service.subject == "[Laboratorio] Nueva solicitud: Impresión 3D — Pablo Profe (Supervisor)"
    assert "Servicio del laboratorio" in service.text and "Abrir la aplicación" not in service.text
    reservation = email_events.reservation_created_content(
        {"id": "r1", "scope_type": "activity", "activity": "Robótica", "purpose": "Semillero", "attendees": 12,
         "start_at": "2026-10-20T19:00:00+00:00", "end_at": "2026-10-20T21:00:00+00:00"}, lab.student,
    )
    assert reservation.subject == "[Laboratorio] Nueva reserva: Robótica — Ana Prueba (Cliente)"
    assert "20/10/2026 2:00 p. m." in reservation.text and "Asistentes: 12" in reservation.text


def test_checkin_email_flags_a_late_return(lab):
    loan = {"id": "L1", "item_id": "LAB-MIC-01", "item_name": "Microscopio", "quantity": 1, "user_id": "u",
            "checkout_at": _future(-5), "expected_return_at": _future(-2), "return_at": _future(0)}
    content = email_events.loan_checkin_content(loan, lab.student, {"id": "otro", "full_name": "Pablo Profe",
                                                                      "role": "profesor"})
    assert content.subject == "[Laboratorio] Devolución: Microscopio x1 — Ana Prueba (Cliente)"
    assert "Devuelto con retraso" in content.text and "Registró la devolución: Pablo Profe (Supervisor)" in content.text


def test_message_is_multipart_with_plain_text_and_html(lab, smtp):
    ok, _ = email_events.send_test_email(lab, {"full_name": "Marta Maestra", "role": "maestro",
                                               "institutional_email": "maestra@uniminuto.edu.co"})
    assert ok
    message = smtp.sent[-1]
    assert message.is_multipart() and message["Subject"] == "[Laboratorio] Correo de prueba"
    assert message["To"] == "maestra@uniminuto.edu.co"
    assert "Correo de prueba" in _body(message, "plain")
    assert "<!DOCTYPE html>" in _body(message, "html")
    assert lab.log[-1]["status"] == email_events.STATUS_SENT and lab.log[-1]["event_type"] == "test"


# ---------------------------------------------------------------------------
# Flujos reales: envio en segundo plano, preferencias y fallos
# ---------------------------------------------------------------------------

def test_new_request_is_emailed_to_masters_in_background_and_recorded(lab, smtp):
    row, notified, message = service_requests.submit_request(
        lab, lab.student, "product", "LAB-MIC-01", 2, description="Óptica", needed_at=_future(),
    )
    assert notified is True and "en envío a 2 destinatario(s)" in message
    assert email_events.wait_for_pending(5)
    sent = smtp.sent[-1]
    assert sent["To"] == "maestra@uniminuto.edu.co, otro.maestro@uniminuto.edu.co"
    assert sent["Subject"] == "[Laboratorio] Nueva solicitud: Microscopio — Ana Prueba (Cliente)"
    stored = lab.get_service_request(row["id"])
    assert stored["email_notified"] is True and stored["email_error"] == ""
    assert lab.log[-1]["reference"] == row["id"] and lab.log[-1]["recipient_count"] == 2


def test_sending_never_blocks_the_operation(lab, smtp):
    smtp.gate = threading.Event()                  # el servidor "se cuelga" hasta que se libere
    started = time.monotonic()
    ok, message, loan = loans.checkout(lab, "LAB-MIC-01", 1, lab.student)
    assert ok and loan and time.monotonic() - started < 1
    assert email_events.pending_count() == 1 and not smtp.sent
    smtp.gate.set()
    assert email_events.wait_for_pending(5)
    assert smtp.sent[-1]["Subject"].startswith("[Laboratorio] Salida: Microscopio x1 — Ana Prueba")


def test_checkin_and_reviews_reach_the_right_people(lab, smtp):
    _, _, loan = loans.checkout(lab, "LAB-MIC-01", 1, lab.student)
    loans.checkin(lab, loan["id"], {"id": "prof-1", "role": "profesor", "full_name": "Profe Prueba"})
    request = lab.create_service_request(
        {"request_type": "service", "service_name": "Corte láser", "description": "Plantilla"}, lab.student,
    )
    reviewer = {"id": "m", "full_name": "Marta Maestra", "role": "maestro",
                "institutional_email": "maestra@uniminuto.edu.co"}
    assert service_requests.review_request(lab, request["id"], "rejected", reviewer, "Falta el archivo")[0]
    assert email_events.wait_for_pending(5)
    subjects = {message["Subject"]: message for message in smtp.sent}
    assert "[Laboratorio] Devolución: Microscopio x1 — Ana Prueba (Cliente)" in subjects
    review = subjects["[Laboratorio] Tu solicitud fue rechazada: Corte láser"]
    assert review["To"] == "ana@uniminuto.edu.co"                     # solo al solicitante
    assert "Falta el archivo" in _body(review, "plain")


def test_reservation_created_and_approved_are_emailed(lab, smtp):
    start = (datetime.now(reservations.BOGOTA_TZ) + timedelta(days=2)).replace(hour=10, minute=0, second=0,
                                                                                microsecond=0)
    row, notified, _ = reservations.submit_reservation(
        lab, lab.student, "full_lab", "", "Feria de proyectos", 30, start, start + timedelta(hours=2),
    )
    assert notified is True
    reviewer = {"id": "m", "full_name": "Marta Maestra", "role": "maestro",
                "institutional_email": "maestra@uniminuto.edu.co"}
    assert reservations.review_reservation(lab, row["id"], "approved", reviewer)[0]
    assert email_events.wait_for_pending(5)
    subjects = [message["Subject"] for message in smtp.sent]
    assert "[Laboratorio] Nueva reserva: Laboratorio completo — Ana Prueba (Cliente)" in subjects
    assert "[Laboratorio] Tu reserva fue aprobada: Laboratorio completo" in subjects
    assert lab.get_reservation(row["id"])["email_notified"] is True


def test_disabled_events_send_nothing(lab, smtp):
    email_events.save_preferences(lab, {"request_created": False, "loan_checkout": False}, actor_email="m@u.edu.co")
    row, notified, message = service_requests.submit_request(
        lab, lab.student, "service", service_name="Asesoría", description="Proyecto",
    )
    loans.checkout(lab, "LAB-MIC-01", 1, lab.student)
    assert email_events.wait_for_pending(5)
    assert notified is False and message == email_events.DISABLED_MESSAGE
    assert lab.get_service_request(row["id"])["email_error"] == email_events.DISABLED_MESSAGE
    assert smtp.sent == [] and lab.log == []
    assert email_events.load_preferences(lab)["events"]["reservation_created"] is True   # el resto sigue activo


def test_smtp_failure_is_logged_and_never_breaks_the_request(lab, smtp):
    smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"5.7.8 rejected")
    row, notified, _ = service_requests.submit_request(
        lab, lab.student, "service", service_name="Asesoría", description="Proyecto",
    )
    assert row["status"] == "pending" and notified is True        # la solicitud se guardo y el envio salio
    assert email_events.wait_for_pending(5)
    stored = lab.get_service_request(row["id"])
    assert stored["email_notified"] is False and "contraseña de aplicación" in stored["email_error"]
    assert lab.log[-1]["status"] == email_events.STATUS_FAILED
    assert "clave-de-prueba" not in json.dumps(lab.log)              # nunca se registra un secreto


def test_unexpected_errors_are_contained(lab, smtp, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    lab.update_service_request_notification = broken           # guardar el resultado falla
    monkeypatch.setattr(email_events, "manager_recipients", broken)
    assert email_events.notify_request_created(lab, {"id": "x"}, lab.student) == (
        False, email_events.PREPARE_FAILED_MESSAGE)
    assert email_events.notify_request_created(lab, None) == (False, email_events.PREPARE_FAILED_MESSAGE)
    lab.add_notification_log = broken                            # el registro falla: el correo igual sale
    monkeypatch.setattr(email_events, "manager_recipients", lambda storage, prefs=None: ["maestra@uniminuto.edu.co"])
    assert loans.checkout(lab, "LAB-MIC-01", 1, lab.student)[0]
    assert email_events.wait_for_pending(5) and smtp.sent


def test_without_smtp_nothing_is_attempted_or_logged(lab):
    original = lab.get_setting

    def only_the_checkout_mode(key, *args, **kwargs):
        if key != loans.SETTING_SELF_CHECKOUT:       # el modo de autoservicio no tiene que ver con el correo
            pytest.fail("sin SMTP no se debe leer nada")
        return original(key, *args, **kwargs)

    lab.get_setting = only_the_checkout_mode
    ok, _, loan = loans.checkout(lab, "LAB-MIC-01", 1, lab.student)
    assert ok and loans.checkin(lab, loan["id"], {"id": "prof-1", "role": "profesor"})[0]
    assert email_events.pending_count() == 0 and lab.log == []


def test_requester_without_valid_email_is_reported(lab, smtp):
    request = {"id": "r9", "request_type": "service", "service_name": "Asesoría", "requester_email": "noreply@x.co"}
    assert email_events.notify_request_reviewed(lab, request, "approved", {"full_name": "M", "role": "maestro"}) == (
        False, email_events.NO_REQUESTER_EMAIL_MESSAGE)
    assert email_events.wait_for_pending(5)
    assert smtp.sent == [] and lab.log[-1]["error"] == email_events.NO_REQUESTER_EMAIL_MESSAGE


def test_overdue_summary_lists_each_late_loan(lab, smtp):
    assert email_events.notify_overdue_loans(lab) == (False, email_events.NO_OVERDUE_MESSAGE)
    loans.checkout(lab, "LAB-MIC-01", 1, lab.student, expected_return_at=_future(-3))
    assert email_events.wait_for_pending(5)
    ok, _ = email_events.notify_overdue_loans(lab, lab.student)
    assert ok and email_events.wait_for_pending(5)
    summary = smtp.sent[-1]
    assert summary["Subject"] == "[Laboratorio] Préstamos vencidos: 1"
    assert "Microscopio x1" in _body(summary, "plain")


def test_corrupt_preferences_fall_back_to_everything_enabled(lab):
    lab.set_setting(email_events.SETTINGS_KEY, "{no es json")
    assert email_events.load_preferences(lab) == email_events.default_preferences()
    lab.set_setting(email_events.SETTINGS_KEY, json.dumps({"events": {"loan_checkin": "false", "otro": False}}))
    prefs = email_events.load_preferences(lab)
    assert prefs["events"]["loan_checkin"] is False and "otro" not in prefs["events"]


def test_smtp_status_never_exposes_values(monkeypatch):
    monkeypatch.setattr(notifications, "safe_secret", _secrets({"SMTP_USERNAME": "bot@uniminuto.edu.co"}))
    status = notifications.smtp_status()
    assert status["configured"] is False and status["missing"] == ["SMTP_HOST"]
    assert any("SMTP_PASSWORD" in warning for warning in status["warnings"])
    assert "bot@uniminuto.edu.co" not in json.dumps(status)
    monkeypatch.setattr(notifications, "safe_secret", _secrets(dict(SMTP_SECRETS, APP_URL="javascript:alert(1)")))
    status = notifications.smtp_status()
    assert status["configured"] is True and status["security"] == "STARTTLS"
    assert status["present"]["APP_URL"] is False                    # solo se enlazan URLs http(s)
    assert "clave-de-prueba" not in json.dumps(status)


# ---------------------------------------------------------------------------
# Hoja notification_log en el Excel real
# ---------------------------------------------------------------------------

def test_notification_log_persists_is_trimmed_and_migrates_old_databases(monkeypatch):
    with pd.ExcelWriter(storage_module.EXCEL_PATH, engine="openpyxl") as writer:
        for sheet, columns in storage_module.SHEET_COLUMNS.items():
            if sheet != "notification_log":
                pd.DataFrame(columns=columns).to_excel(writer, sheet_name=sheet, index=False)
    storage_module._cached_dfs = None
    db = LabStorage()
    assert db.get_notification_log() == []
    monkeypatch.setattr(storage_module, "NOTIFICATION_LOG_LIMIT", 3)
    for number in range(5):
        db.add_notification_log({"event_type": "test", "subject": f"Asunto {number}", "status": "sent",
                                 "recipient_count": 1, "created_at": f"2026-10-10T12:0{number}:00+00:00",
                                 "ignored": "x"})
    storage_module._cached_dfs = None                               # "reinicio" de la app
    rows = LabStorage().get_notification_log()
    assert [row["subject"] for row in rows] == ["Asunto 4", "Asunto 3", "Asunto 2"]
    assert "ignored" not in rows[0] and rows[0]["recipient_count"] == "1"
    assert db.get_notification_log(limit=1)[0]["subject"] == "Asunto 4"


# ---------------------------------------------------------------------------
# Vista: Reportes -> Correos (solo perfil maestro)
# ---------------------------------------------------------------------------

def _reportes_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import reportes

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    reportes.render()


def _open_reportes(role):
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_function(_reportes_script, default_timeout=60)
    at.session_state["test_user"] = {"id": "u1", "role": role, "full_name": "Marta Maestra",
                                     "institutional_email": "maestra@uniminuto.edu.co"}
    at.run()
    assert not at.exception, at.exception
    return at


def test_only_the_master_sees_the_emails_tab():
    assert any("Correos" in tab.label for tab in _open_reportes("maestro").tabs)
    assert not any("Correos" in tab.label for tab in _open_reportes("profesor").tabs)


def test_emails_tab_explains_missing_smtp_without_values():
    at = _open_reportes("maestro")
    page = " ".join(m.value for m in at.markdown) + " ".join(w.value for w in at.warning)
    assert "Sin configurar" in page and "SMTP_HOST" in page
    assert "smtp.gmail.com" in " ".join(code.value for code in at.code)
    test_button = next(b for b in at.button if "correo de prueba" in b.label)
    assert test_button.disabled                                   # sin SMTP no se puede probar


def test_master_can_save_event_toggles():
    at = _open_reportes("maestro")
    at.toggle(key="email_event_loan_checkin").set_value(False)
    next(b for b in at.button if "Guardar preferencias" in b.label).click()
    at.run()
    assert not at.exception, at.exception
    prefs = email_events.load_preferences(LabStorage())
    assert prefs["events"]["loan_checkin"] is False and prefs["events"]["request_created"] is True
