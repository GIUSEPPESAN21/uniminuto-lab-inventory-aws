# -*- coding: utf-8 -*-
"""v1.20 · pedido de varios productos en una sola solicitud (cliente) y su gestión por grupo."""

import pytest

from core import dispatch, service_requests, traceability

STUDENT = {"id": "s1", "full_name": "Ana", "institutional_email": "ana@uniminuto.edu.co", "role": "estudiante"}
OPERATOR = {"id": "o1", "full_name": "Olga", "institutional_email": "olga@uniminuto.edu.co", "role": "operario"}
SUPERVISOR = {"id": "p1", "full_name": "Pablo", "institutional_email": "pablo@uniminuto.edu.co", "role": "encargado"}


@pytest.fixture
def lab(storage):
    storage.add_item("LAB-A", name="Pinza", quantity=5)
    storage.add_item("LAB-B", name="Alicate", quantity=3)
    storage.add_item("LAB-C", name="Tijera", quantity=1)
    for person in (STUDENT, OPERATOR, SUPERVISOR):
        storage.users[person["id"]] = {**person, "status": "active"}
    storage.set_setting("picking_route_required", "0")
    return storage


def _order(storage, lines=None, note="Para la práctica"):
    lines = lines or [{"item_id": "LAB-A", "quantity": 2}, {"item_id": "LAB-B", "quantity": 1}]
    rows, _, _ = service_requests.submit_order(storage, STUDENT, lines, description=note)
    return rows


def test_one_order_creates_one_line_per_product_sharing_an_order_id(lab):
    rows = _order(lab)
    assert [(r["item_id"], r["quantity"]) for r in rows] == [("LAB-A", 2), ("LAB-B", 1)]
    assert len({r["order_id"] for r in rows}) == 1 and rows[0]["order_id"]
    assert all(r["status"] == "pending" and r["description"] == "Para la práctica" for r in rows)
    assert [r["order_id"] for r in service_requests.order_rows(lab, rows[0]["order_id"])] == [rows[0]["order_id"]] * 2


def test_a_single_line_is_a_normal_request_and_duplicates_are_merged(lab):
    rows = _order(lab, [{"item_id": "LAB-A", "quantity": 1}])
    assert len(rows) == 1 and not rows[0].get("order_id")
    rows = _order(lab, [{"item_id": "LAB-A", "quantity": 1}, {"item_id": "LAB-A", "quantity": 2}])
    assert len(rows) == 1 and rows[0]["quantity"] == 3


def test_everything_is_validated_before_anything_is_created(lab):
    before = len(lab.get_service_requests())
    for lines, expected in (
        ([{"item_id": "LAB-A", "quantity": 1}, {"item_id": "LAB-C", "quantity": 4}], "Tijera"),    # sin stock
        ([{"item_id": "LAB-A", "quantity": 1}, {"item_id": "NO-EXISTE", "quantity": 1}], "NO-EXISTE"),
        ([{"item_id": "LAB-A", "quantity": 0}, {"item_id": "LAB-B", "quantity": 1}], "mayor a cero"),
        ([], "al menos un producto"),
    ):
        with pytest.raises(ValueError, match=expected):
            service_requests.submit_order(lab, STUDENT, lines)
    assert len(lab.get_service_requests()) == before             # ninguna línea quedó creada
    too_many = [{"item_id": f"X{i}", "quantity": 1} for i in range(service_requests.MAX_ORDER_LINES + 1)]
    with pytest.raises(ValueError, match="hasta"):
        service_requests.merge_lines(too_many)


def test_groups_keep_an_order_together_and_singles_alone(lab):
    rows = _order(lab)
    single = _order(lab, [{"item_id": "LAB-C", "quantity": 1}])[0]
    mixed = [rows[0], single, rows[1]]
    groups = service_requests.group_by_order(mixed)
    assert [len(g["rows"]) for g in groups] == [2, 1]
    assert groups[0]["order_id"] == rows[0]["order_id"] and groups[1]["order_id"] == ""


def test_the_supervisor_approves_or_rejects_the_whole_order(lab):
    rows = _order(lab)
    ok, message = service_requests.review_order(lab, rows[0]["order_id"], "approved", SUPERVISOR, "ok")
    assert ok and "2" in message
    assert {lab.get_service_request(r["id"])["status"] for r in rows} == {"approved"}
    assert not service_requests.review_order(lab, rows[0]["order_id"], "approved", SUPERVISOR)[0]   # ya no hay pendientes
    other = _order(lab)
    assert service_requests.review_order(lab, other[0]["order_id"], "rejected", SUPERVISOR)[0]
    assert {lab.get_service_request(r["id"])["status"] for r in other} == {"rejected"}
    assert not service_requests.review_order(lab, _order(lab)[0]["order_id"], "approved", STUDENT)[0]


def test_the_client_cancels_the_whole_order_until_the_lab_starts(lab):
    rows = _order(lab)
    ok, message = service_requests.cancel_order(lab, rows[0]["order_id"], STUDENT)
    assert ok and "2" in message
    assert {lab.get_service_request(r["id"])["status"] for r in rows} == {"cancelled"}
    assert not service_requests.cancel_order(lab, rows[0]["order_id"], STUDENT)[0]


def test_the_operator_takes_every_line_with_one_action_and_the_supervisor_closes_it(lab):
    rows = _order(lab)
    order_id = rows[0]["order_id"]
    service_requests.review_order(lab, order_id, "approved", SUPERVISOR)
    ok, message, taken = dispatch.claim_order(lab, order_id, OPERATOR)
    assert ok and len(taken) == 2 and "2 de 2" in message
    assert not dispatch.claim_order(lab, order_id, OPERATOR)[0]                # ya no quedan por tomar
    for row in rows:
        assert dispatch.start_picking(lab, row["id"], OPERATOR)[0]
        for _ in range(row["quantity"]):
            assert dispatch.register_reading(lab, row["id"], OPERATOR, row["item_id"], "manual")[0]
        assert dispatch.complete_picking(lab, row["id"], OPERATOR)[0]
    ok, message, done = dispatch.receive_order(lab, order_id, SUPERVISOR)
    assert ok and len(done) == 2
    ok, message, done = dispatch.deliver_order(lab, order_id, SUPERVISOR)
    assert ok and len(done) == 2
    for row in rows:
        events = lab.get_trace_events(request_id=row["id"])
        assert traceability.request_stage_key(lab.get_service_request(row["id"]), events) == traceability.STAGE_DELIVERED
    assert lab.get_available_quantity("LAB-A") == 3 and lab.get_available_quantity("LAB-B") == 2


def test_an_operator_cannot_receive_or_deliver_orders(lab):
    rows = _order(lab)
    with pytest.raises(PermissionError):
        dispatch.receive_order(lab, rows[0]["order_id"], OPERATOR)
    with pytest.raises(PermissionError):
        dispatch.claim_order(lab, rows[0]["order_id"], STUDENT)


# --- vistas reales ------------------------------------------------------------------------

def _solicitudes_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import solicitudes

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    solicitudes.render()


def test_the_basket_collects_products_and_sends_one_order():
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    from core.storage import LabStorage

    storage = LabStorage()
    student = storage.create_user("Ana Prueba", "ana@uniminuto.edu.co", "x", "estudiante")
    for code, name, qty in (("LAB-A", "Pinza", 5), ("LAB-B", "Alicate", 3)):
        storage.save_item({"name": name, "item_type": "standalone", "quantity": qty}, code, is_new=True,
                          actor_email="seed")
    at = AppTest.from_function(_solicitudes_script, default_timeout=60)
    at.session_state["test_user"] = {k: student[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.run()
    assert not at.exception
    for code in ("LAB-A", "LAB-B"):
        at.selectbox(key="request_product_choice").set_value(code).run()
        [b for b in at.button if "Agregar al pedido" in b.label][0].click().run()
        assert not at.exception
    assert [line["item_id"] for line in at.session_state["order_basket"]] == ["LAB-A", "LAB-B"]
    [b for b in at.button if "Enviar pedido de 2" in b.label][0].click().run()
    assert not at.exception
    rows = storage.get_service_requests(user_id=student["id"])
    assert len(rows) == 2 and len({r["order_id"] for r in rows}) == 1 and rows[0]["order_id"]
    assert at.session_state["order_basket"] == []


def _view_script():
    import importlib

    import streamlit as st

    from core.storage import LabStorage

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    importlib.import_module(f"views.{st.session_state.test_view}").render()


def _open(view, person):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_view"] = view
    at.run()
    assert not at.exception
    return at


def _real_order():
    from core.storage import LabStorage

    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role)
              for role in ("estudiante", "operario", "encargado")}
    for code, name, qty in (("LAB-A", "Pinza", 5), ("LAB-B", "Alicate", 3)):
        storage.save_item({"name": name, "item_type": "standalone", "quantity": qty}, code, is_new=True,
                          actor_email="seed")
    rows, _, _ = service_requests.submit_order(
        storage, people["estudiante"], [{"item_id": "LAB-A", "quantity": 2}, {"item_id": "LAB-B", "quantity": 1}],
        description="Taller de herramientas")
    return storage, people, rows


def test_the_client_sees_one_grouped_order_with_a_cancel_for_all():
    pytest.importorskip("streamlit.testing.v1")
    storage, people, rows = _real_order()
    at = _open("solicitudes", people["estudiante"])
    text = " ".join(str(m.value) for m in at.markdown)
    assert f"Pedido {rows[0]['order_id'].upper()}" in text and "2 productos" in text
    assert [b for b in at.button if b.label == "Cancelar todo el pedido"]


def test_the_supervisor_reviews_the_group_and_the_operator_takes_it_whole():
    pytest.importorskip("streamlit.testing.v1")
    storage, people, rows = _real_order()
    at = _open("solicitudes", people["encargado"])
    approve = [b for b in at.button if "Aprobar pedido completo" in b.label]
    assert approve
    approve[0].click().run()
    assert {r["status"] for r in storage.get_service_requests()} == {"approved"}

    queue = _open("despacho", people["operario"])
    take = [b for b in queue.button if "Tomar todo el pedido" in b.label]
    assert take and "Taller de herramientas" in " ".join(str(i.value) for i in queue.info)
    take[0].click().run()
    assert not queue.exception
    assert all(dispatch.assigned_picker(storage.get_trace_events(request_id=r["id"])) == people["operario"]["id"]
               for r in rows)


def test_the_picking_panel_shows_the_route_steps_in_order():
    pytest.importorskip("streamlit.testing.v1")
    from core.storage import LabStorage

    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role)
              for role in ("estudiante", "operario", "encargado")}
    storage.save_item({"name": "Estantería 2", "item_type": "location"}, "2-0-00-00-000", is_new=True,
                      actor_email="seed")
    storage.save_item({"name": "Electrónica", "item_type": "master"}, "2-1-01-00-000", is_new=True, actor_email="seed")
    storage.save_item({"name": "Resistencia", "item_type": "child", "parent_id": "2-1-01-00-000", "quantity": 9},
                      "2-1-01-02-005", is_new=True, actor_email="seed")
    rows, _, _ = service_requests.submit_order(storage, people["estudiante"], [{"item_id": "2-1-01-02-005", "quantity": 1}])
    service_requests.review_request(storage, rows[0]["id"], "approved", people["encargado"])
    dispatch.claim_request(storage, rows[0]["id"], people["operario"])
    dispatch.start_picking(storage, rows[0]["id"], people["operario"])
    at = _open("despacho", people["operario"])
    text = " ".join(str(m.value) for m in at.markdown)
    assert "Estantería 2" in text and "lee `2-0-00-00-000`" in text and "Contenedor 01" in text
    confirm = [b for b in at.button if b.label == "Confirmar unidades y completar picking"][0]
    assert confirm.disabled                                         # la ruta está incompleta
    assert "Caja 02" in text and "no se lee" in text                # la caja aparece como paso de casilla, no de lectura

    # Con estantería y contenedor leídos, el panel pide la casilla de la caja y el conteo (las unidades no llevan código).
    for code in ("2-0-00-00-000", "2-1-01-00-000"):
        assert dispatch.register_reading(storage, rows[0]["id"], people["operario"], code, "nfc")[0]
    ready = _open("despacho", people["operario"])
    assert any("Ruta leída" in str(s.value) for s in ready.success)
    box = [c for c in ready.checkbox if c.key == f"box_ok_{rows[0]['id']}"][0]
    assert "Caja 02" in box.label and "Persona operario" in box.label and not box.disabled
    confirm = [b for b in ready.button if b.label == "Confirmar unidades y completar picking"][0]
    assert not confirm.disabled
    ready.checkbox(key=f"box_ok_{rows[0]['id']}").check()          # dentro de un formulario: se envía con el botón
    confirm.click().run()
    assert not ready.exception and not ready.error
    events = storage.get_trace_events(request_id=rows[0]["id"])
    assert traceability.request_stage_key(storage.get_service_request(rows[0]["id"]), events) == traceability.STAGE_PICKED
    picked = next(e for e in events if e["event_type"] == traceability.EVENT_ORDER_PICKED)
    proof = traceability.event_details(picked)["box_confirmation"]
    assert proof["confirmed_by_name"] == "Persona operario" and proof["box"] == "Caja 02"
