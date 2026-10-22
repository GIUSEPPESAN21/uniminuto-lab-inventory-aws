# -*- coding: utf-8 -*-
"""v1.17 · «Empezar de cero»: el administrador vacía lo operativo (solicitudes, reservas, correos…) sin
tocar personas, productos ni préstamos abiertos, con constancia y copia local."""

import json
import os
from datetime import datetime, timedelta

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import dispatch, reservations, service_requests, traceability  # noqa: E402
from core import storage as sm  # noqa: E402
from core.storage import LabStorage  # noqa: E402

ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")
CODE = "LAB-MIC-01"


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role) for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 20}, CODE, is_new=True)
    # un pedido despachado (préstamo abierto ligado a la solicitud) y otro solo pedido
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 2)
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    assert dispatch.claim_request(storage, row["id"], people["operario"])[0]
    assert dispatch.confirm_manual_picking(storage, row["id"], people["operario"], True)[0]
    service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
    start = datetime.now(reservations.BOGOTA_TZ) + timedelta(days=2)
    reservations.submit_reservation(storage, people["estudiante"], reservations.SCOPE_ACTIVITY, "Práctica", "Robótica",
                                    10, start, start + timedelta(hours=2))
    storage.add_notification_log({"event_type": "x", "subject": "s", "recipients": "a@b.edu", "recipient_count": 1,
                                  "status": "sent"})
    for kind in ("session_login", "session_logout", "nfc_tap"):
        storage.add_trace_event({"event_type": kind, "user_id": people["estudiante"]["id"]})
    closed = storage.create_loan(storage.get_item(CODE), 1, people["profesor"])
    assert storage.return_loan(closed["id"], actor=people["profesor"])[0]
    return storage, people


def test_preview_counts_what_would_be_deleted(lab):
    storage, _ = lab
    counts = storage.operational_data_counts()
    assert counts["solicitudes"] == 2 and counts["reservas"] == 1 and counts["correos"] >= 1
    assert counts["trazabilidad"] >= 4 and counts["sesiones_nfc"] == 3
    assert counts["prestamos_abiertos"] == 1 and counts["prestamos_cerrados"] == 1


@pytest.mark.parametrize("role", ["estudiante", "operario", "encargado", "profesor"])
def test_only_the_administrator_can_purge(lab, role):
    storage, people = lab
    with pytest.raises(PermissionError):
        storage.purge_operational_data(people[role])
    assert storage.operational_data_counts()["solicitudes"] == 2


def test_default_purge_keeps_people_products_and_open_loans_and_leaves_a_record(lab):
    storage, people = lab
    removed = storage.purge_operational_data(people["maestro"])
    assert removed["solicitudes"] == 2 and removed["reservas"] == 1 and removed["sesiones_nfc"] == 3
    assert storage.get_service_requests() == [] and storage.get_reservations() == []
    assert storage.get_notification_log() == []
    assert len(storage.get_all_users()) == 5 and storage.get_item(CODE) is not None
    [open_loan] = storage.get_all_loans(status="out")
    assert open_loan["request_id"] == "" and storage.get_available_quantity(CODE) == 18   # 20 − 2 prestadas
    assert len(storage.get_all_loans()) == 2                       # el devuelto se conserva por omisión
    [audit] = [e for e in storage.get_trace_events() if e["event_type"] == "data_purged"]
    assert audit["actor_id"] == people["maestro"]["id"] and json.loads(audit["details"])["removed"]["solicitudes"] == 2
    assert [e for e in storage.get_trace_events() if e["request_id"]] == []
    assert os.path.exists(f"{sm.EXCEL_PATH}.bak")                  # copia local previa
    assert "Borró datos operativos" in traceability.describe_event(audit)


def test_options_are_respected_and_closed_loans_can_be_deleted(lab):
    storage, people = lab
    removed = storage.purge_operational_data(people["maestro"], requests=False, reservations=False, mail_log=False,
                                             session_noise=False, closed_loans=True)
    assert removed == {"prestamos_cerrados": 1}
    assert len(storage.get_service_requests()) == 2 and len(storage.get_reservations()) == 1
    assert [loan["status"] for loan in storage.get_all_loans()] == ["out"]


def test_dispatch_queues_forget_purged_orders(lab):
    storage, people = lab
    pending = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)[0]
    assert service_requests.review_request(storage, pending["id"], "approved", people["encargado"])[0]
    assert [r["id"] for r in dispatch.queue_for(storage, people["operario"])] == [pending["id"]]
    storage.purge_operational_data(people["maestro"])
    assert dispatch.queue_for(storage, people["operario"]) == []


def _script():
    import streamlit as st

    from core.storage import LabStorage
    from views import usuarios

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    usuarios.render()


def _open(person):
    at = AppTest.from_function(_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.run()
    assert not at.exception
    return at


def test_data_tab_needs_the_confirmation_word_and_reports_the_result(lab):
    storage, people = lab
    at = _open(people["maestro"])
    button = [b for b in at.button if "Borrar datos seleccionados" in b.label][0]
    button.click().run()
    assert any("Escribe BORRAR" in e.value for e in at.error)
    assert len(storage.get_service_requests()) == 2                 # sin la palabra no se borra nada
    word = [t for t in at.text_input if "BORRAR" in t.label][0]
    word.input("BORRAR")
    [b for b in at.button if "Borrar datos seleccionados" in b.label][0].click().run()
    assert not at.exception and storage.get_service_requests() == []
    assert any("Filas borradas" in e.value for e in at.success)     # el aviso sobrevive al rerun
