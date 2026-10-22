# -*- coding: utf-8 -*-
"""v1.20 · ruta obligatoria del picking (estantería → piso → contenedor → caja → unidades) y llegada por NFC."""

import pytest

from core import dispatch, nfc, permissions, pick_route, picking, service_requests, traceability

SHELF, FLOOR = "2-0-00-00-000", "2-1-00-00-000"
CONTAINER, BOX, ITEM = "2-1-01-00-000", "2-1-01-02-000", "2-1-01-02-005"
STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Olga", "institutional_email": "olga@uniminuto.edu.co", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Pablo", "institutional_email": "pablo@uniminuto.edu.co", "role": "encargado"}
ADMIN = {"id": "m1", "full_name": "Maria", "institutional_email": "maria@uniminuto.edu.co", "role": "maestro"}


@pytest.fixture
def lab(storage):
    storage.add_item(SHELF, name="Estantería 2", item_type="location")
    storage.add_item(FLOOR, name="Piso 1", item_type="location", parent_id=SHELF)
    storage.add_item(CONTAINER, name="Electrónica", item_type="master")
    storage.add_item("2-1-01-02-000", name="Caja de resistencias", item_type="child", parent_id=CONTAINER, quantity=9)
    storage.add_item(ITEM, name="Resistencia 220", item_type="child", parent_id=CONTAINER, quantity=100)
    for person in (STUDENT, OPERATOR, SUPERVISOR, ADMIN):
        storage.users[person["id"]] = {**person, "status": "active"}
    return storage


def _claimed(storage, item=ITEM, quantity=2):
    request, _, _ = service_requests.submit_request(storage, STUDENT, "product", item, quantity)
    assert request
    assert service_requests.review_request(storage, request["id"], "approved", SUPERVISOR)[0]
    assert dispatch.claim_request(storage, request["id"], OPERATOR)[0]
    assert dispatch.start_picking(storage, request["id"], OPERATOR)[0]
    return request["id"]


def _read(storage, request_id, code, method="barcode_gun", actor=OPERATOR):
    return dispatch.register_reading(storage, request_id, actor, code, method)


def test_the_route_to_read_is_shelf_floor_container_and_the_box_is_a_checkbox_step(lab):
    points = pick_route.checkpoints(lab, ITEM)
    assert [p["key"] for p in points] == ["estanteria", "piso", "contenedor"]
    assert [p["label_code"] for p in points] == [SHELF, FLOOR, CONTAINER]
    assert pick_route.box_step(lab, ITEM)["title"] == "Caja 02"            # la caja no se lee: se marca
    assert pick_route.box_step(lab, "LIBRE-NO-EXISTE") is None


def test_the_box_as_the_product_is_also_only_a_checkbox(lab):
    assert [p["key"] for p in pick_route.checkpoints(lab, BOX)] == ["estanteria", "piso", "contenedor"]
    assert pick_route.box_step(lab, BOX)["title"] == "Caja 02"


def test_the_route_is_read_in_order_and_reading_the_box_is_never_required(lab):
    request_id = _claimed(lab)
    ok, message, _ = _read(lab, request_id, ITEM)                    # intenta saltarse la ruta
    assert not ok and "Primero confirma la ruta" in message and SHELF in message
    ok, message, _ = _read(lab, request_id, CONTAINER)               # el contenedor antes que la estantería
    assert not ok and SHELF in message
    for code in (SHELF, FLOOR):
        assert _read(lab, request_id, code)[0]
    ok, message, _ = _read(lab, request_id, CONTAINER, "nfc")
    assert ok and "Ruta completa" in message and "Caja 02" in message and "casilla" in message
    ok, message, count = _read(lab, request_id, BOX)                 # leer la caja no hace falta ni cuenta
    assert ok and count == 0 and "no se lee" in message
    assert [step["code"] for step in picking.route_steps("o1", request_id)] == [SHELF, FLOOR, CONTAINER]


def test_the_box_checkbox_is_mandatory_and_records_who_withdrew(lab):
    request_id = _claimed(lab, ITEM, 2)
    ok, message, _ = dispatch.confirm_units(lab, request_id, OPERATOR, 2, box_confirmed=True)
    assert not ok and "lee la ruta" in message                       # primero la ruta
    for code in (SHELF, FLOOR, CONTAINER):
        _read(lab, request_id, code)
    ok, message, _ = dispatch.confirm_units(lab, request_id, OPERATOR, 2)            # sin marcar la casilla
    assert not ok and "Marca la casilla de la Caja 02" in message
    ok, message, event = dispatch.confirm_units(lab, request_id, OPERATOR, 2, "sin novedad", box_confirmed=True)
    assert ok, message
    details = traceability.event_details(event)
    proof = details["box_confirmation"]
    assert proof["box"] == "Caja 02" and proof["confirmed_by_id"] == "o1" and proof["confirmed_by_name"] == "Olga"
    assert proof["confirmed_by_email"] == "olga@uniminuto.edu.co" and proof["units"] == 2 and proof["via"] == "checkbox"
    assert [r["method"] for r in details["readings"]] == ["count", "count"] and "label_read" not in details
    assert [step["key"] for step in details["route"]] == ["estanteria", "piso", "contenedor"]
    text = traceability.describe_event(event)
    assert "Ruta leída: estanteria › piso › contenedor" in text
    assert "Retiró de la Caja 02: lo confirmó Olga" in text
    assert lab.get_available_quantity(ITEM) == 98


def test_complete_picking_cannot_skip_the_box_confirmation(lab):
    request_id = _claimed(lab, ITEM, 1)
    for code in (SHELF, FLOOR, CONTAINER):
        _read(lab, request_id, code)
    ok, message, _ = dispatch.complete_picking(lab, request_id, OPERATOR)
    assert not ok and "Marca la casilla" in message
    ok, message, _ = dispatch.complete_picking(lab, request_id, OPERATOR,
                                               extra_details={"box_confirmation": {"box": "Caja 02"}})
    assert ok, message


def test_picking_cannot_be_completed_without_the_route_and_manual_confirmation_records_the_box_too(lab):
    request_id = _claimed(lab)
    ok, message, _ = dispatch.complete_picking(lab, request_id, OPERATOR, readings=[ITEM, ITEM])
    assert not ok and "lee la ruta" in message
    ok, message, _ = dispatch.confirm_manual_picking(lab, request_id, OPERATOR, True, "sin lector")
    assert not ok and "lee la ruta" in message                       # la confirmación manual tampoco se salta la ruta
    for code in (SHELF, FLOOR, CONTAINER):
        assert _read(lab, request_id, code)[0]
    ok, message, event = dispatch.confirm_manual_picking(lab, request_id, OPERATOR, True, "sin lector")
    assert ok, message
    proof = traceability.event_details(event)["box_confirmation"]
    assert proof["via"] == "manual" and proof["confirmed_by_name"] == "Olga"
    assert "(confirmación manual)" in traceability.describe_event(event)


def test_wrong_reads_become_failed_scans_and_repeated_route_taps_are_ignored(lab):
    request_id = _claimed(lab)
    assert not _read(lab, request_id, "2-1-02-00-000")[0]
    assert picking.failed("o1", request_id)
    assert _read(lab, request_id, SHELF, "nfc")[0]
    before = len(picking.route_steps("o1", request_id))
    ok, message, _ = _read(lab, request_id, SHELF, "nfc")             # el chip se toca dos veces: no avanza ni falla
    assert not ok and "Primero confirma la ruta" in message and FLOOR in message
    for code in (FLOOR, CONTAINER):
        _read(lab, request_id, code)
    ok, message, _ = _read(lab, request_id, CONTAINER, "nfc")         # ruta completa y se vuelve a tocar el contenedor
    assert ok and "ya estaba confirmado" in message
    assert len(picking.route_steps("o1", request_id)) == before + 2


def test_undo_removes_the_optional_label_and_then_the_last_route_point(lab):
    request_id = _claimed(lab)
    for code in (SHELF, FLOOR, CONTAINER, ITEM):
        _read(lab, request_id, code)
    dispatch.undo_reading(OPERATOR, request_id)                       # la etiqueta opcional del producto
    assert dispatch.current_readings(OPERATOR, request_id) == [] and len(picking.route_steps("o1", request_id)) == 3
    dispatch.undo_reading(OPERATOR, request_id)
    assert [s["key"] for s in picking.route_steps("o1", request_id)] == ["estanteria", "piso"]


def test_shelf_and_floor_are_only_required_when_registered(lab):
    del lab.items[SHELF], lab.items[FLOOR]
    assert [p["key"] for p in pick_route.checkpoints(lab, ITEM)] == ["contenedor"]


def test_products_without_a_box_read_their_own_label_once_instead_of_the_checkbox(lab):
    lab.add_item("LIBRE-1", name="Pinza", quantity=3)
    assert pick_route.checkpoints(lab, "LIBRE-1") == [] and pick_route.box_step(lab, "LIBRE-1") is None
    request_id = _claimed(lab, "LIBRE-1", 2)
    route = dispatch.route_progress(lab, OPERATOR, request_id)
    assert route["label_required"] and route["box"] == ""
    ok, message, _ = dispatch.confirm_units(lab, request_id, OPERATOR, 2)
    assert not ok and "etiqueta del producto" in message
    assert _read(lab, request_id, "LIBRE-1")[0]
    ok, message, event = dispatch.confirm_units(lab, request_id, OPERATOR, 2)
    assert ok, message
    details = traceability.event_details(event)
    assert details["label_read"]["code"] == "LIBRE-1" and "box_confirmation" not in details


def test_the_box_checkbox_stays_mandatory_even_if_the_route_reading_is_turned_off(lab):
    with pytest.raises(PermissionError):
        pick_route.set_required(lab, SUPERVISOR, False)
    assert pick_route.set_required(lab, ADMIN, False)[0] and not pick_route.required(lab)
    request_id = _claimed(lab)
    assert dispatch.route_progress(lab, OPERATOR, request_id)["box"] == "Caja 02"
    assert not dispatch.confirm_units(lab, request_id, OPERATOR, 2)[0]                   # la casilla sigue siendo obligatoria
    assert dispatch.confirm_units(lab, request_id, OPERATOR, 2, box_confirmed=True)[0]  # y sin leer la ruta alcanza
    assert pick_route.set_required(lab, ADMIN, True)[0] and pick_route.required(lab)
    assert permissions.has_cap(ADMIN, permissions.CAP_DISPATCH_SETTINGS)


def test_progress_for_the_panel(lab):
    request_id = _claimed(lab)
    _read(lab, request_id, SHELF)
    route = dispatch.route_progress(lab, OPERATOR, request_id)
    assert route["required"] and route["done"] == 1 and not route["complete"] and route["next"]["key"] == "piso"
    assert route["box"] == "Caja 02" and not route["label_required"]


# --- NFC: reconoce la llegada ---------------------------------------------------------------

def test_a_chip_on_a_container_shelf_or_floor_says_you_arrived(lab):
    for code, kind, text in ((CONTAINER, "contenedor", "Llegaste al contenedor «Electrónica»"),
                             (SHELF, "estantería", "Llegaste a la estantería «Estantería 2»"),
                             (FLOOR, "piso", "Llegaste al piso «Piso 1»")):
        outcome = nfc.handle_tap(lab, STUDENT, code, secret="")
        assert outcome["ok"] and outcome["arrival"] == kind and outcome["message"].startswith(text), outcome["message"]
    assert "2 producto(s) dentro" in nfc.handle_tap(lab, STUDENT, CONTAINER, secret="", now="2030-01-01T00:00:00+00:00")["message"]
    assert nfc.handle_tap(lab, STUDENT, ITEM, secret="")["arrival"] == ""     # un producto no es un lugar con chip


def test_the_tap_event_remembers_the_arrival_kind(lab):
    outcome = nfc.handle_tap(lab, STUDENT, CONTAINER, secret="")
    assert traceability.event_details(outcome["event"])["arrival"] == "contenedor"


def test_nfc_taps_advance_the_picking_route_from_a_new_tab(lab):
    request_id = _claimed(lab)
    for code in (SHELF, FLOOR, CONTAINER):
        outcome = nfc.handle_tap(lab, OPERATOR, code, secret="")
        ok, message = dispatch.register_active_reading(lab, OPERATOR, code, "nfc", accepted=outcome["ok"])
        assert ok, message
    assert [s["key"] for s in picking.route_steps("o1", request_id)] == ["estanteria", "piso", "contenedor"]
    assert all(s["method"] == "nfc" for s in picking.route_steps("o1", request_id))


# --- las unidades no llevan código: se cuentan -----------------------------------------------------

def test_the_count_must_match_the_order(lab):
    request_id = _claimed(lab, ITEM, 2)
    for code in (SHELF, FLOOR, CONTAINER):
        _read(lab, request_id, code)
    for bad in (1, 3, 0, "x", None):
        ok, message, _ = dispatch.confirm_units(lab, request_id, OPERATOR, bad, box_confirmed=True)
        assert not ok, bad
    assert "pedido es de 2" in dispatch.confirm_units(lab, request_id, OPERATOR, 5, box_confirmed=True)[1]
    assert dispatch.confirm_units(lab, request_id, OPERATOR, 2, box_confirmed=True)[0]


def test_the_count_method_cannot_be_used_to_fake_a_label_read(lab):
    request_id = _claimed(lab, ITEM, 1)
    for code in (SHELF, FLOOR, CONTAINER):
        _read(lab, request_id, code)
    ok, message, _ = _read(lab, request_id, ITEM, "count")
    assert not ok and "solo para confirmar" in message
    assert dispatch.current_readings(OPERATOR, request_id) == []
    with pytest.raises(PermissionError):
        dispatch.confirm_units(lab, request_id, STUDENT, 1, box_confirmed=True)
