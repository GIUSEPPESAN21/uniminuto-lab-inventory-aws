# -*- coding: utf-8 -*-
"""Puntos 2 y 3 de v1.15: picking usable en teléfono, método real de lectura y
lecturas NFC que sobreviven a la pestaña nueva (dos sesiones de AppTest)."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth, dispatch, picking, service_requests, traceability  # noqa: E402
from core import storage as storage_module  # noqa: E402
from core.storage import LabStorage  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
PASSWORD = "ClaveSegura123"
CODE = "LAB-MIC-01"
ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    hashed = auth.hash_password(PASSWORD)
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", hashed, role)
              for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 5,
                       "location": "Gabinete azul"}, CODE, is_new=True)
    return storage, people


def _claimed_order(storage, people, quantity=2):
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, quantity)
    ok, message = service_requests.review_request(storage, row["id"], "approved", people["encargado"])
    assert ok, message
    ok, message, _ = dispatch.claim_request(storage, row["id"], people["operario"])
    assert ok, message
    return row["id"]


def _events(storage, request_id, kind):
    return storage.get_trace_events(request_id=request_id, event_type=kind)


# --- método real y escenario ------------------------------------------------------------

def test_scenario_is_set_in_settings_by_an_admin_and_fixes_the_typed_method(lab):
    storage, people = lab
    assert dispatch.scenario_key(storage) == "B" and dispatch.typed_method(storage) == "barcode_gun"
    with pytest.raises(PermissionError):
        dispatch.set_scenario(storage, people["operario"], "A")
    with pytest.raises(PermissionError):
        dispatch.set_scenario(storage, people["profesor"], "A")
    assert dispatch.set_scenario(storage, people["maestro"], "A")[0]
    assert dispatch.scenario_key(storage) == "A" and dispatch.typed_method(storage) == "manual"
    assert dispatch.set_scenario(storage, people["maestro"], "C")[0]
    assert dispatch.typed_method(storage) == "manual"                    # C: el teclado es solo respaldo
    ok, message = dispatch.set_scenario(storage, people["maestro"], "Z")
    assert not ok and "A, B o C" in message and dispatch.scenario_key(storage) == "C"


def test_legacy_and_unknown_methods_are_normalized():
    assert dispatch.normalize_method("barcode") == "barcode_gun"
    assert dispatch.normalize_method("NFC") == "nfc"
    assert dispatch.normalize_method("telepatia") == "" and dispatch.normalize_method(None) == ""


def test_the_recorded_method_is_the_real_one_per_reading(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    assert dispatch.start_picking(storage, request_id, people["operario"])[0]
    assert dispatch.register_reading(storage, request_id, people["operario"], CODE,
                                     dispatch.typed_method(storage))[0]
    assert dispatch.register_reading(storage, request_id, people["operario"], CODE, "nfc")[0]   # repetida: no cuenta
    assert dispatch.complete_picking(storage, request_id, people["operario"])[0]
    details = traceability.event_details(_events(storage, request_id, traceability.EVENT_ORDER_PICKED)[0])
    assert details["label_read"]["method"] == "barcode_gun"                    # cómo se leyó la etiqueta de la caja
    assert [r["method"] for r in details["readings"]] == ["count", "count"]    # las unidades se cuentan, no se leen
    assert details["methods"] == {"count": 2} and details["method"] == "count"


def test_an_invalid_method_is_rejected_by_the_transition(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people, quantity=1)
    ok, message, _ = dispatch.complete_picking(
        storage, request_id, people["operario"], [{"code": CODE, "method": "telepatia"}])
    assert not ok and "método no válido" in message
    assert storage.get_available_quantity(CODE) == 5                      # no salió nada del inventario
    assert dispatch.register_reading(storage, request_id, people["operario"], CODE, "telepatia")[0] is False


# --- una sola escritura al completar -----------------------------------------------------

def test_failed_readings_accumulate_and_are_saved_in_one_event_when_completing(lab, monkeypatch):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    writes = []
    original = storage_module._write_and_sync
    monkeypatch.setattr(storage_module, "_write_and_sync", lambda dfs: (writes.append(1), original(dfs)))

    assert dispatch.start_picking(storage, request_id, people["operario"])[0]
    operator = people["operario"]
    assert dispatch.register_reading(storage, request_id, operator, "OTRO-99", "manual")[0] is False
    assert dispatch.register_reading(storage, request_id, operator, "OTRO-98", "manual")[0] is False
    assert dispatch.register_reading(storage, request_id, operator, CODE, "barcode_gun")[0]
    assert dispatch.register_reading(storage, request_id, operator, CODE, "barcode_gun")[0]
    assert writes == []                                                   # leer no escribe en la base
    assert _events(storage, request_id, traceability.EVENT_ORDER_SCAN_FAILED) == []

    assert dispatch.complete_picking(storage, request_id, operator)[0]
    assert len(writes) == 1                                               # UNA escritura en total
    failures = _events(storage, request_id, traceability.EVENT_ORDER_SCAN_FAILED)
    assert len(failures) == 1
    info = traceability.event_details(failures[0])
    assert info["count"] == 2 and [s["code"] for s in info["scans"]] == ["OTRO-99", "OTRO-98"]
    assert picking.size() == 0                                            # el almacén se limpia


def test_a_rejected_nfc_tap_never_adds_a_reading(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people, quantity=1)
    operator = people["operario"]
    dispatch.start_picking(storage, request_id, operator)
    result = dispatch.register_active_reading(storage, operator, CODE, "nfc", accepted=False,
                                              reason="La firma del chip no es válida.")
    assert result == (False, "La firma del chip no es válida.")
    assert dispatch.current_readings(operator, request_id) == []
    assert picking.failed(operator["id"], request_id)[0]["method"] == "nfc"


def test_releasing_or_reassigning_discards_the_pending_readings(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    operator = people["operario"]
    dispatch.start_picking(storage, request_id, operator)
    dispatch.register_reading(storage, request_id, operator, CODE, "manual")
    assert dispatch.release_request(storage, request_id, operator, "me equivoqué")[0]
    assert picking.readings(operator["id"], request_id) == [] and picking.active_request(operator["id"]) == ""


def test_only_the_assigned_operator_can_start_or_read(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    other = storage.create_user("Otro operario", "otro@uniminuto.edu.co", "x", "operario")
    ok, message = dispatch.start_picking(storage, request_id, other)
    assert not ok and "otro operario" in message
    assert dispatch.register_reading(storage, request_id, other, CODE, "manual")[0] is False
    with pytest.raises(PermissionError):
        dispatch.start_picking(storage, request_id, people["estudiante"])


def test_picking_store_expires_and_is_bounded():
    picking.begin("u1", "r1", now=1000)
    picking.add_reading("u1", "r1", CODE, "manual", "t", now=1000)
    assert picking.active_request("u1", now=1000 + picking.MAX_AGE_SECONDS - 1) == "r1"
    assert picking.active_request("u1", now=1000 + picking.MAX_AGE_SECONDS + 1) == ""
    for number in range(picking.MAX_ENTRIES + 20):
        picking.begin(f"user{number}", "req", now=2000 + number)
    assert picking.size() <= picking.MAX_ENTRIES


# --- AppTest: panel arriba, ruta y foto, ajustes ----------------------------------------

def _despacho_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import despacho

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    despacho.render()


def _open_despacho(person):
    at = AppTest.from_function(_despacho_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.run()
    assert not at.exception
    return at


def _text(*groups) -> str:
    return " ".join(str(e.value) for group in groups for e in group)


def test_each_order_shows_route_and_location_and_the_picking_panel_comes_first(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    dispatch.start_picking(storage, request_id, people["operario"])
    at = _open_despacho(people["operario"])
    text = _text(at.markdown)
    assert "Ruta" in text and "Gabinete azul" in text
    html_chunks = [str(e.value) for e in at.markdown]
    panel_at = next(i for i, chunk in enumerate(html_chunks) if "Picking ·" in chunk)
    queue_at = next(i for i, chunk in enumerate(html_chunks) if "Mis pedidos (" in chunk or "Por tomar (" in chunk)
    assert panel_at < queue_at                                            # el panel va ANTES de las listas
    assert "Etiqueta del producto" in text                           # sin caja ni ruta: se lee la etiqueta
    assert any(b.label == "Confirmar unidades y completar picking" and b.disabled for b in at.button)


def test_the_typed_input_has_no_free_method_selector(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    dispatch.start_picking(storage, request_id, people["operario"])
    at = _open_despacho(people["operario"])
    assert not at.radio                                                   # el método ya no es un radio libre
    at.text_input[0].input(CODE)
    [b for b in at.button if b.label == "Registrar lectura"][0].click()
    at.run()
    assert not at.exception
    readings = dispatch.current_readings(people["operario"], request_id)
    assert [r["method"] for r in readings] == ["barcode_gun"]


def test_the_admin_sets_the_scenario_in_the_settings_tab_and_others_do_not_see_it(lab):
    storage, people = lab
    admin = _open_despacho(people["maestro"])
    assert any(tab.label == "Ajustes" for tab in admin.tabs)
    admin.selectbox(key="dispatch_scenario_choice").select("C")
    admin.run()
    admin.button(key="dispatch_scenario_save").click()
    admin.run()
    assert not admin.exception and storage.get_setting("dispatch_scenario") == "C"
    for role in ("operario", "encargado"):
        assert not any(tab.label == "Ajustes" for tab in _open_despacho(people[role]).tabs)


# --- AppTest con dos sesiones: el toque NFC abre una pestaña nueva ----------------------

def _login(at, email):
    at.text_input[0].input(email)
    at.text_input[1].input(PASSWORD)
    at.button[0].click().run()
    assert not at.exception


def test_an_nfc_tap_in_a_new_tab_adds_the_reading_to_the_open_picking(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people, quantity=2)
    operator = people["operario"]

    # Pestaña 1: el operario abre su picking y lee una unidad con la pistola.
    tab_one = _open_despacho(operator)
    assert dispatch.start_picking(storage, request_id, operator)[0]
    tab_one.run()
    tab_one.text_input[0].input(CODE)
    [b for b in tab_one.button if b.label == "Registrar lectura"][0].click()
    tab_one.run()
    assert [r["method"] for r in dispatch.current_readings(operator, request_id)] == ["barcode_gun"]

    # Pestaña 2: el toque del chip abre la app con ?nfc=... (otra sesión de Streamlit).
    tab_two = AppTest.from_file(APP_PATH, default_timeout=30)
    tab_two.query_params["nfc"] = CODE
    tab_two.run()
    _login(tab_two, "operario@uniminuto.edu.co")
    assert tab_two.session_state["user"]["role"] == "operario"
    assert "dispatch_active_request" not in tab_two.session_state          # nada vive en la sesión

    readings = dispatch.current_readings(operator, request_id)
    assert [r["method"] for r in readings] == ["barcode_gun"]              # la etiqueta ya estaba leída: no se duplica
    assert any("ya estaba leída" in str(e.value) for e in tab_two.success)  # y se avisa en la pestaña nueva

    # Pestaña 1: las unidades no llevan código; se cuentan y se confirman.
    tab_one.run()
    assert any(b.label == "Confirmar unidades y completar picking" and not b.disabled for b in tab_one.button)
    assert dispatch.confirm_units(storage, request_id, operator, 2)[0]
    assert storage.get_available_quantity(CODE) == 3


def test_an_nfc_tap_of_another_product_is_a_failed_reading_not_a_reading(lab):
    storage, people = lab
    storage.save_item({"name": "Osciloscopio", "item_type": "standalone", "quantity": 2}, "LAB-OSC-01",
                      is_new=True)
    request_id = _claimed_order(storage, people, quantity=1)
    operator = people["operario"]
    dispatch.start_picking(storage, request_id, operator)
    tab = AppTest.from_file(APP_PATH, default_timeout=30)
    tab.query_params["nfc"] = "LAB-OSC-01"
    tab.run()
    _login(tab, "operario@uniminuto.edu.co")
    assert dispatch.current_readings(operator, request_id) == []
    assert picking.failed(operator["id"], request_id)[0]["code"] == "LAB-OSC-01"
    assert _events(storage, request_id, traceability.EVENT_ORDER_SCAN_FAILED) == []   # aún sin escribir


def test_two_orders_of_the_same_product_do_not_collide_on_widget_keys(lab):
    """Regresión: dos pedidos del mismo producto en «Mis pedidos» repetían la clave del
    popover «Foto de estado» (StreamlitDuplicateElementKey) y tumbaban Despacho."""
    storage, people = lab
    for _ in range(2):
        row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
        assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
        assert dispatch.claim_request(storage, row["id"], people["maestro"])[0]
    at = _open_despacho(people["maestro"])           # el administrador puede tomar y adjuntar fotos
    assert not at.exception
    assert len([b for b in at.button if "Iniciar picking" in b.label]) == 2


# --- confirmación manual con constancia (v1.17) -------------------------------------------------

def test_manual_confirmation_needs_the_authorization_and_leaves_a_written_proof(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people, quantity=3)
    ok, message, _ = dispatch.confirm_manual_picking(storage, request_id, people["operario"], authorized=False)
    assert not ok and "autorización" in message
    assert storage.get_service_request(request_id) is not None
    assert not _events(storage, request_id, traceability.EVENT_ORDER_PICKED)

    ok, message, _ = dispatch.confirm_manual_picking(storage, request_id, people["operario"], True,
                                                     note="la pistola no tenía batería")
    assert ok, message
    [picked] = _events(storage, request_id, traceability.EVENT_ORDER_PICKED)
    details = traceability.event_details(picked)
    assert details["methods"] == {"manual": 3} and details["method"] == "manual"
    proof = details["manual_confirmation"]
    assert proof["operator_name"] == people["operario"]["full_name"] and proof["operator_id"] == people["operario"]["id"]
    assert "sin leer su código" in proof["message"] and proof["note"] == "la pistola no tenía batería"
    loans = storage.get_all_loans(status="out")
    assert len(loans) == 1 and loans[0]["quantity"] == 3 and loans[0]["operator_id"] == people["operario"]["id"]


def test_manual_confirmation_is_only_for_the_assigned_operator_and_not_twice(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    with pytest.raises(PermissionError):
        dispatch.confirm_manual_picking(storage, request_id, people["estudiante"], True)
    other = storage.create_user("Otro operario", "otro@uniminuto.edu.co", "x", "operario")
    ok, message, _ = dispatch.confirm_manual_picking(storage, request_id, other, True)
    assert not ok and "ya no está en tu picking" in message
    assert dispatch.confirm_manual_picking(storage, request_id, people["operario"], True)[0]
    ok, _, _ = dispatch.confirm_manual_picking(storage, request_id, people["operario"], True)
    assert not ok and len(storage.get_all_loans(status="out")) == 1        # no se descuenta dos veces


def test_manual_confirmation_panel_is_open_in_scenario_a_and_gated_by_the_checkbox(lab):
    storage, people = lab
    request_id = _claimed_order(storage, people)
    assert dispatch.set_scenario(storage, people["maestro"], "A")[0]
    assert dispatch.start_picking(storage, request_id, people["operario"])[0]
    at = _open_despacho(people["operario"])
    done = [b for b in at.button if b.label == "Confirmar y completar picking"][0]
    assert done.disabled and "sin leer su código" in _text(at.caption)
    at.checkbox[0].check().run()
    done = [b for b in at.button if b.label == "Confirmar y completar picking"][0]
    assert not done.disabled
    done.click().run()
    assert not at.exception
    assert _events(storage, request_id, traceability.EVENT_ORDER_PICKED)


def test_one_click_takes_the_order_and_opens_the_picking_panel(lab):
    storage, people = lab
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 2)
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    at = _open_despacho(people["operario"])
    [b for b in at.button if b.label == "Tomar y empezar picking"][0].click().run()
    assert not at.exception
    assert dispatch.assigned_picker(storage.get_trace_events(request_id=row["id"])) == people["operario"]["id"]
    assert picking.active_request(people["operario"]["id"]) == row["id"]            # el panel queda abierto
    assert "Picking · " in _text(at.markdown, at.caption, at.subheader) or any("Código leído" in t.label for t in at.text_input)
