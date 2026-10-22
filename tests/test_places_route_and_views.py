# -*- coding: utf-8 -*-
"""Ubicaciones en la ruta verificable (estanteria, piso y mesa con etiqueta) y en
las vistas reales: pestaña Ubicaciones de Inventario (alta en bloque, mapa,
etiquetas) y Escanear."""

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import barcode, location, places, traceability  # noqa: E402
from core.storage import LabStorage  # noqa: E402

PRODUCT = {"id": "2-1-01-01-001", "name": "Arduino UNO", "location": "Gabinete azul"}
PLACES = {"2-0-00-00-000": "Estantería 2", "2-1-00-00-000": "Estantería 2 · Piso 1"}
PROF = "prof@uniminuto.edu.co"


# --- puntos de control ---------------------------------------------------------------

def test_routes_are_unchanged_without_registered_places():
    points = location.build_route_checkpoints(PRODUCT)
    assert [p["label_code"] for p in points] == ["", "", "2-1-01-00-000", "2-1-01-01-000", "2-1-01-01-001"]
    assert "Sin etiqueta" in " ".join(s["detail"] for s in traceability.checkpoint_steps(points, current=0))


def test_registered_shelf_and_floor_get_their_label_in_the_route():
    points = location.build_route_checkpoints(PRODUCT, names=PLACES, places=PLACES)
    assert [p["label_code"] for p in points][:2] == ["2-0-00-00-000", "2-1-00-00-000"]
    assert points[0]["name"] == "Estantería 2" and "Escanea su etiqueta" in points[0]["hint"]
    assert location.compact_route(points) == "E2 › P1 › C01 › CJ01 › I001"


def test_only_the_registered_levels_are_labelled():
    points = location.build_route_checkpoints(PRODUCT, places=["2-1-00-00-000"])
    assert [p["label_code"] for p in points][:2] == ["", "2-1-00-00-000"]


def test_table_and_lego_routes_use_their_labels():
    mesa = location.build_route_checkpoints({"id": "M1-E2", "name": "Láser"}, places=["M1-E0"])
    assert [p["label_code"] for p in mesa] == ["M1-E0", "M1-E2"]
    lego = location.build_route_checkpoints({"id": "E3-LM07", "name": "Grúa"}, places=["3-0-00-00-000", "E3-LM00"])
    assert [p["label_code"] for p in lego] == ["3-0-00-00-000", "E3-LM00", "E3-LM07"]


def test_place_codes_lists_the_candidate_labels():
    assert location.place_codes(PRODUCT) == ["2-0-00-00-000", "2-1-00-00-000"]
    assert location.place_codes({"id": "M2-E4"}) == ["M2-E0"]
    assert location.place_codes({"id": "LAB-X-01"}) == []
    free = location.place_codes({"id": "LAB-X-01"}, {"id": "3-2-01-00-000"})
    assert free == ["3-0-00-00-000", "3-2-00-00-000"]


def test_a_location_item_has_its_own_route():
    floor = location.build_route_checkpoints({"id": "2-1-00-00-000", "name": "Piso 1"}, places=["2-0-00-00-000"])
    assert [p["key"] for p in floor] == ["estanteria", "piso"]
    assert [p["label_code"] for p in floor] == ["2-0-00-00-000", "2-1-00-00-000"]
    assert floor[-1]["name"] == "Piso 1"
    shelf = location.build_route_checkpoints({"id": "2-0-00-00-000", "name": "Estantería 2"})
    assert [p["label_code"] for p in shelf] == ["2-0-00-00-000"]
    guide = " ".join(location.build_location_guide({"id": "2-1-00-00-000", "name": "Piso"})["steps"])
    assert "Estantería 2" in guide and "Piso 1" in guide


def test_codes_match_tolerates_hand_typed_location_codes():
    assert location.codes_match("2-1-00-00-000", "2-1-0-0-0")
    assert location.codes_match("M1-E0", "m1-e0")
    assert not location.codes_match("2-1-00-00-000", "2-2-00-00-000")


def test_route_state_machine_with_shelf_and_floor_labels():
    route = traceability.new_route(PRODUCT, None, {"id": "r1"}, PLACES, PLACES)
    assert traceability.confirm_arrival(route)["ok"] is False        # la estanteria tiene etiqueta
    shelf = traceability.apply_code(route, "2-0-00-00-000")
    assert shelf["ok"] and "Estantería 2 verificado" in shelf["message"]
    assert traceability.apply_code(route, "2-1-00-00-000")["ok"]
    for code in ("2-1-01-00-000", "2-1-01-01-000", "2-1-01-01-001"):
        result = traceability.apply_code(route, code)
    assert result["completed"] and traceability.route_stats(route)["full"]
    assert traceability.route_stats(route)["labels"] == 5


def test_skipping_the_shelf_label_leaves_it_inferred_until_scanned():
    route = traceability.new_route(PRODUCT, None, {"id": "r1"}, PLACES, PLACES)
    traceability.apply_code(route, "2-1-01-00-000")
    stats = traceability.route_stats(route)
    assert stats["inferred"] == 2 and stats["done"] == 3
    for code in ("2-1-01-01-000", "2-1-01-01-001"):
        traceability.apply_code(route, code)
    assert not traceability.route_stats(route)["full"]
    traceability.apply_code(route, "2-0-00-00-000")
    traceability.apply_code(route, "2-1-00-00-000")
    assert traceability.route_stats(route)["full"]


def test_scanning_a_wrong_place_label_explains_where_you_are():
    target = "2-1-01-01-001"
    other_floor = location.explain_mismatch(target, "2-3-00-00-000")
    assert "Piso 3" in other_floor and "Piso 1" in other_floor and "2 pisos" in other_floor
    assert "Estantería 1" in location.explain_mismatch(target, "1-0-00-00-000")
    assert "la correcta" in location.explain_mismatch(target, "2-0-00-00-000")
    assert "Mesa de trabajo 1" in location.explain_mismatch(target, "M1-E0")
    assert "Mesa 2" in location.explain_mismatch("M1-E2", "M2-E0")


# --- vistas ------------------------------------------------------------------------------------

def _inventory_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inventario

    st.session_state.storage = LabStorage()
    st.session_state.user = {"id": "p1", "role": "profesor", "full_name": "Prof Uno",
                             "institutional_email": "prof@uniminuto.edu.co"}
    inventario.render()


def _scan_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import escanear

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    escanear.render()


def _text(elements) -> str:
    return " ".join(str(e.value) for e in elements)


@pytest.fixture
def db():
    storage = LabStorage()
    storage.save_item({"name": "Electrónica", "item_type": "master", "location": ""}, "2-1-01-00-000",
                      is_new=True, actor_email=PROF)
    storage.save_item({"name": "Resistencias", "item_type": "child", "parent_id": "2-1-01-00-000", "quantity": 20},
                      "2-1-01-01-000", is_new=True, actor_email=PROF)
    return storage


def _open_inventory():
    at = AppTest.from_function(_inventory_script, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def test_inventory_has_the_locations_tab_and_creates_a_shelf_in_one_write(db, monkeypatch):
    from core import storage as storage_module

    writes = []
    original = storage_module._write_and_sync
    monkeypatch.setattr(storage_module, "_write_and_sync", lambda dfs: (writes.append(1), original(dfs)))
    at = _open_inventory()
    assert any("Ubicaciones" in t.label for t in at.tabs)
    at.number_input(key="places_shelf_number").set_value(2)
    at.number_input(key="places_shelf_floors").set_value(4)
    at.run()
    frame = at.dataframe[0].value
    assert frame["Código"].tolist() == [
        "2-0-00-00-000", "2-1-00-00-000", "2-2-00-00-000", "2-3-00-00-000", "2-4-00-00-000"]
    at.button(key="places_shelf_create").click().run()
    assert not at.exception
    assert len(writes) == 1
    assert db.get_item("2-4-00-00-000")["parent_id"] == "2-0-00-00-000"
    assert "5 ubicación(es) creada(s)" in _text(at.success)


def test_the_map_lists_locations_with_what_they_hold(db):
    db.save_items_bulk(places.plan_shelf(2, 2, db.get_all_items(include_retired=True))["payloads"], actor_email=PROF)
    at = _open_inventory()
    html = _text(at.markdown)
    assert "Estantería 2 · Piso 1" in html and "2-2-00-00-000" in html
    assert "1 contenedor(es)" in html                      # el piso 1 guarda el contenedor 2-1-01-00-000
    assert at.button(key="places_edit_2-1-00-00-000") is not None
    assert "Resistencias" in _text(at.markdown)


def test_locations_are_not_listed_as_catalog_products(db):
    db.save_items_bulk(places.plan_shelf(2, 2, [])["payloads"], actor_email=PROF)
    at = _open_inventory()
    assert "2 item(s) encontrados." in _text(at.caption)     # solo el contenedor y su producto


def test_register_a_work_table_and_a_free_zone(db):
    at = _open_inventory()
    at.radio(key="places_kind").set_value("Mesa de trabajo").run()
    at.button(key="places_create").click().run()
    assert not at.exception
    assert db.get_item("M1-E0")["name"] == "Mesa de trabajo 1"
    at.radio(key="places_kind").set_value("Zona, sala o subnivel (código libre)").run()
    at.text_input(key="places_zone_code").set_value("SALA-A").run()
    assert at.button(key="places_create").disabled                      # falta el nombre
    at.text_input(key="places_name").set_value("Sala de electrónica").run()
    at.button(key="places_create").click().run()
    assert db.get_item("SALA-A")["item_type"] == "location"


def test_editing_a_location_hides_the_stock_fields(db):
    db.save_items_bulk(places.plan_shelf(2, 1, [])["payloads"], actor_email=PROF)
    at = AppTest.from_function(_inventory_script, default_timeout=60)
    at.session_state["editing_item_id"] = "2-0-00-00-000"
    at.run()
    assert not at.exception
    assert not at.number_input
    assert "ubicación" in _text(at.caption) and "los productos guardados en ella no se tocan" in _text(at.caption)


def _scan(db, code, role="estudiante"):
    at = AppTest.from_function(_scan_script, default_timeout=60)
    user = db.create_user("Ana", f"{role}@uniminuto.edu.co", "x", role)
    at.session_state["test_user"] = {k: user[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["scan_result"] = barcode.scan(db, code)
    at.run()
    assert not at.exception
    return at


def test_scanning_a_registered_location_shows_what_it_holds(db):
    db.save_items_bulk(places.plan_shelf(2, 2, [])["payloads"], actor_email=PROF)
    at = _scan(db, "2-1-00-00-000")
    assert "Piso: **Estantería 2 · Piso 1**" in _text(at.success)
    assert "Electrónica" in _text(at.markdown) and "Resistencias" in _text(at.markdown)
    assert not any("Confirmar salida" in b.label for b in at.button)      # sin salida ni reingreso


def test_scanning_an_unregistered_location_offers_to_register_it(db):
    at = _scan(db, "M1-E0", role="profesor")
    assert "todavía no está registrada" in _text(at.warning)
    assert at.text_input[-1].value == "Mesa de trabajo 1"
    student = _scan(db, "M1-E0")
    assert "Pide a un supervisor" in _text(student.info)


def test_the_verifiable_route_asks_for_the_shelf_label_when_it_exists(db):
    from views import location_guide

    db.save_items_bulk(places.plan_shelf(2, 1, [])["payloads"], actor_email=PROF)
    checkpoints, registered = location_guide._route_checkpoints(db, db.get_item("2-1-01-01-000"),
                                                                db.get_item("2-1-01-00-000"))
    assert set(registered) == {"2-0-00-00-000", "2-1-00-00-000"}
    assert [p["label_code"] for p in checkpoints][:3] == ["2-0-00-00-000", "2-1-00-00-000", "2-1-01-00-000"]
    assert checkpoints[0]["name"] == "Estantería 2"


def _home_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inicio

    st.session_state.storage = LabStorage()
    st.session_state.user = {"id": "p1", "role": "profesor", "full_name": "Prof Uno",
                             "institutional_email": "prof@uniminuto.edu.co", "program_or_department": "Ing"}
    inicio.render()


def test_home_does_not_count_locations_nor_alert_about_their_stock(db):
    db.save_items_bulk(places.plan_shelf(2, 3, [])["payloads"], actor_email=PROF)
    at = AppTest.from_function(_home_script, default_timeout=60)
    at.run()
    assert not at.exception
    html = _text(at.markdown)
    assert "Disponibilidad baja" not in html and "Estantería 2" not in html
