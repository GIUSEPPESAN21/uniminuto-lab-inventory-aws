# -*- coding: utf-8 -*-
"""Inventario (views/inventario.py) con la app real: catalogo agrupado por
contenedor y flujo «✨ Generar productos desde la descripción» de punta a punta
sobre los 3 contenedores reales del laboratorio."""

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import inventory_suggestions as sug  # noqa: E402
from core import storage as storage_module  # noqa: E402
from core.storage import LabStorage  # noqa: E402
from test_inventory_suggestions import (  # noqa: E402
    ACTOR, C1, C2, C3, C4, C4_CHILDREN, C4_TITLES, C5, C5_COLORS, EXPECTED, LAB, REAL, default_payloads,
)


def _script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inventario

    st.session_state.storage = LabStorage()
    st.session_state.user = {
        "id": "p1", "role": "profesor", "full_name": "Prof Uno",
        "institutional_email": "prof@uniminuto.edu.co",
    }
    inventario.render()


@pytest.fixture
def db():
    storage = LabStorage()
    for container in REAL:
        data = {k: container[k] for k in ("name", "category", "item_type", "location", "description")}
        storage.save_item(data, container["id"], is_new=True, actor_email=ACTOR)
    return storage


@pytest.fixture
def writes(monkeypatch):
    calls = []
    original = storage_module._write_and_sync

    def counting(dfs):
        calls.append(len(dfs["items"]))
        original(dfs)

    monkeypatch.setattr(storage_module, "_write_and_sync", counting)
    return calls


def _run():
    at = AppTest.from_function(_script, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def _button(at, key):
    return at.button(key=key)


def _open_generator(at, container):
    _button(at, f"inv_gen_toggle_{container['id']}").click().run()
    assert not at.exception
    return at


def _text(elements) -> str:
    return " ".join(str(e.value) for e in elements)


def _editor(at):
    frames = [d.value for d in at.dataframe if list(d.value.columns) == list(sug.EDITOR_COLUMNS)]
    assert len(frames) == 1
    return frames[0]


def test_catalog_groups_by_container_and_offers_the_generator(db):
    at = _run()
    assert "3 item(s) encontrados." in _text(at.caption)
    toggles = [b for b in at.button if (b.key or "").startswith("inv_gen_toggle_")]
    # Ordenados por codigo (no por nombre: "Contendor 2" quedaria antes que "Contenedor 1").
    assert [b.key for b in toggles] == [f"inv_gen_toggle_{c['id']}" for c in REAL]
    assert all(b.label == ":material/auto_awesome: Generar productos desde la descripción" for b in toggles)
    assert "3 contenedor(es) describen productos que aún no están registrados" in _text(at.info)
    html = _text(at.markdown)
    assert "lab-stat" in html and "Pendientes de conteo" in html
    assert "5 sugerido(s) desde la descripción" in html and "13 sugerido(s)" in html
    assert "Contiene Piezas Lego de Pines 4x2 - 2x2 - 2x1" in html          # la descripcion en la tarjeta


def test_generator_shows_the_editable_preview_with_notes(db):
    at = _open_generator(_run(), C1)
    frame = _editor(at)
    assert list(zip(frame["Código"], frame["Nombre"], frame["Crear"])) == [
        (code, name, selected) for code, name, _feature, selected in EXPECTED[C1["id"]]
    ]
    assert frame["Cantidad"].tolist() == [0] * 5 and set(frame["Unidad"]) == {"unidad"}
    assert any(e.label == ":material/warning: Puntos para confirmar (3)" for e in at.expander)
    assert "«fichas» es un término genérico" in _text(at.markdown)
    create = _button(at, f"inv_gen_create_{C1['id']}")
    assert create.label == ":material/check_circle: Crear 5 producto(s)" and not create.disabled
    assert "Crear en una sola sincronización" in _text(at.markdown)            # timeline del flujo
    assert _button(at, f"inv_gen_toggle_{C1['id']}").label == ":material/close: Cerrar el generador"


def test_creating_from_the_preview_writes_once_and_closes(db, writes):
    at = _open_generator(_run(), C1)
    _button(at, f"inv_gen_create_{C1['id']}").click().run()
    assert not at.exception

    assert len(writes) == 1                                       # un solo commit para los 5 productos
    children = sorted(db.get_children(C1["id"]), key=lambda c: c["id"])
    assert [(c["id"], c["name"]) for c in children] == [(code, name) for code, name, *_ in EXPECTED[C1["id"]]]
    assert all(sug.is_pending_count(c) and c["created_by"] == "prof@uniminuto.edu.co" for c in children)
    assert "Se crearon 5 producto(s) en «Contenedor 1»; 5 quedan pendientes de conteo." in _text(at.success)
    assert at.session_state["inv_generate_for"] is None
    assert "8 item(s) encontrados." in _text(at.caption)
    assert "Cantidad por contar · unidad" in _text(at.caption)
    assert "5 por contar" in _text(at.markdown)

    # Al volver a abrirlo ya no hay nada nuevo que crear: no duplica.
    _open_generator(at, C1)
    assert "No hay productos nuevos por crear" in _text(at.markdown)
    assert "Ya registrados en este contenedor (no se duplican)" in _text(at.caption)


def test_container_2_creates_12_and_leaves_the_1x0_for_review(db, writes):
    at = _open_generator(_run(), C2)
    frame = _editor(at)
    assert frame["Crear"].tolist() == [True] * 12 + [False]
    assert "probablemente es «1x1»" in frame["Notas"].iloc[-1]
    # La medida imposible sale en «Puntos para confirmar» (junto con el aviso de la caja) y sin marcar.
    assert any(e.label == ":material/warning: Puntos para confirmar (2)" for e in at.expander)
    assert "Revisar medida" in _text(at.markdown) and frame["Característica"].iloc[-1] == "Con bisel 1x0"
    assert _button(at, f"inv_gen_create_{C2['id']}").label == ":material/check_circle: Crear 12 producto(s)"
    _button(at, f"inv_gen_create_{C2['id']}").click().run()
    assert not at.exception and len(writes) == 1
    created = {c["name"] for c in db.get_children(C2["id"])}
    assert len(created) == 12 and "Pieza Lego con bisel 1x0" not in created
    assert "1 sugerido(s) desde la descripción" in _text(at.markdown)


def test_cancel_closes_the_generator_without_writing(db, writes):
    at = _open_generator(_run(), C3)
    _button(at, f"inv_gen_cancel_{C3['id']}").click().run()
    assert not at.exception and writes == []
    assert at.session_state["inv_generate_for"] is None
    assert not [d for d in at.dataframe if list(d.value.columns) == list(sug.EDITOR_COLUMNS)]


def test_filters_and_list_view(db):
    db.save_items_bulk(default_payloads(C3, db), actor_email=ACTOR)
    db.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": 2, "min_stock_alert": 2},
                 "LAB-001", is_new=True, actor_email=ACTOR)
    at = _run()
    assert "7 item(s) encontrados." in _text(at.caption)
    assert "Ítems sin contenedor" in _text(at.markdown)

    at.selectbox(key="inv_stock").select("Pendiente de conteo").run()
    assert "3 item(s) encontrados." in _text(at.caption)
    at.selectbox(key="inv_stock").select("Stock bajo").run()
    assert "1 item(s) encontrados." in _text(at.caption) and "Multimetro" in _text(at.markdown)
    at.selectbox(key="inv_stock").select("Todos").run()

    at.text_input(key="inv_search").input("Contendor 3").run()       # el contenedor trae todos sus productos
    assert "4 item(s) encontrados." in _text(at.caption)
    at.text_input(key="inv_search").input("3x2").run()               # solo el producto que coincide
    assert "1 item(s) encontrados." in _text(at.caption)
    assert "Mostrando 1 de 3 producto(s)" in _text(at.caption)
    at.text_input(key="inv_search").input("").run()

    at.selectbox(key="inv_location").select(C3["location"]).run()   # incluye lo que esta dentro
    assert "4 item(s) encontrados." in _text(at.caption)
    at.selectbox(key="inv_location").select("Todos").run()

    at.radio(key="inv_view").set_value(":material/view_list: Lista").run()
    assert not at.exception
    assert "7 item(s) encontrados." in _text(at.caption)


def test_pending_product_editor_explains_what_to_do(db):
    db.save_items_bulk([sug.build_payload(
        {"code": "2-1-03-01-000", "name": "Pieza Lego con pines 4x2", "quantity": 0, "source": "x"}, C3,
    )], actor_email=ACTOR)
    at = AppTest.from_function(_script, default_timeout=60)
    at.session_state["editing_item_id"] = "2-1-03-01-000"
    at.run()
    assert not at.exception
    assert "pendiente de conteo" in _text(at.info)


def test_container_without_description_has_no_generator_and_html_is_escaped(db):
    db.save_item({"name": "<b>Caja</b>", "item_type": "master", "description": "<script>alert(1)</script>"},
                 "2-1-04-00-000", is_new=True, actor_email=ACTOR)
    db.save_item({"name": "Vacio", "item_type": "master"}, "2-1-05-00-000", is_new=True, actor_email=ACTOR)
    at = _run()
    keys = {b.key for b in at.button}
    assert "inv_gen_toggle_2-1-05-00-000" not in keys and "inv_gen_toggle_2-1-04-00-000" in keys
    html = _text(at.markdown)
    assert "&lt;script&gt;" in html and "<script>alert" not in html and "&lt;b&gt;Caja" in html


def test_lab_container_fills_quantities_and_leaves_low_confidence_rows_unchecked(db, writes):
    db.save_item({k: LAB[k] for k in ("name", "category", "item_type", "location", "description")},
                 LAB["id"], is_new=True, actor_email=ACTOR)
    at = _open_generator(_run(), LAB)
    frame = _editor(at)
    assert list(zip(frame["Nombre"], frame["Cantidad"], frame["Crear"])) == [
        ("Resistencia de 220 Ω", 50, True), ("Condensador de 10 µF", 0, True),
        ("Cable jumper macho-hembra", 20, True), ("Pieza 0x2", 0, False),
    ]
    assert frame["Característica"].tolist() == ["220 Ω", "10 µF", "Jumper macho-hembra", "0x2"]
    assert any(e.label == ":material/warning: Puntos para confirmar (4)" for e in at.expander)
    assert "Cantidad tomada de la descripción (50)" in _text(at.markdown)
    create = _button(at, f"inv_gen_create_{LAB['id']}")
    assert create.label == ":material/check_circle: Crear 3 producto(s)" and not create.disabled

    create.click().run()
    assert not at.exception and writes == [4, 7]       # alta del contenedor + una sola escritura para los 3 productos
    children = {c["name"]: c for c in db.get_children(LAB["id"])}
    assert set(children) == {"Resistencia de 220 Ω", "Condensador de 10 µF", "Cable jumper macho-hembra"}
    assert children["Resistencia de 220 Ω"]["quantity"] == 50 and not sug.is_pending_count(children["Resistencia de 220 Ω"])
    assert children["Cable jumper macho-hembra"]["quantity"] == 20
    assert sug.is_pending_count(children["Condensador de 10 µF"])
    assert "Se crearon 3 producto(s) en «Contenedor 4»; 1 quedan pendientes de conteo." in _text(at.success)
    # Lo que quedo sin marcar sigue disponible para revisar.
    _open_generator(at, LAB)
    assert [n for n in _editor(at)["Nombre"]] == ["Pieza 0x2"]


def _save_master(db, container):
    db.save_item({k: container[k] for k in ("name", "category", "item_type", "location", "description")},
                 container["id"], is_new=True, actor_email=ACTOR)


def test_container_4_title_explanation_is_fully_registered_so_nothing_is_proposed(db, writes):
    _save_master(db, C4)
    payloads = [sug.build_payload({"code": code, "name": name, "quantity": 0, "unit": "unidad", "source": fragment}, C4)
                for code, name, fragment in C4_CHILDREN]
    db.save_items_bulk(payloads, actor_email=ACTOR)
    at = _open_generator(_run(), C4)
    assert "No hay productos nuevos por crear" in _text(at.markdown)
    assert "Ya registrados en este contenedor (no se duplican)" in _text(at.caption)
    assert not [d for d in at.dataframe if list(d.value.columns) == list(sug.EDITOR_COLUMNS)]
    assert "3 contenedor(es) describen productos que aún no están registrados" in _text(at.info)   # C4 ya no cuenta


def test_container_4_proposes_only_the_titles_that_are_missing(db):
    _save_master(db, C4)
    db.save_items_bulk([sug.build_payload({"code": code, "name": name, "quantity": 0, "unit": "unidad",
                                           "source": fragment}, C4)
                        for code, name, fragment in C4_CHILDREN[:3] + C4_CHILDREN[10:]],
                       actor_email=ACTOR)
    frame = _editor(_open_generator(_run(), C4))
    assert frame["Nombre"].tolist() == C4_TITLES[4:]        # «Bolsa de llantas» ya existe renombrada: se reconoce por su fragmento
    assert frame["Cantidad"].tolist() == [0] * 6 and frame["Crear"].tolist() == [True] * 6
    assert frame["Característica"].iloc[0].startswith("Paquete de ejes")


def test_container_5_prose_becomes_one_row_per_color(db, writes):
    _save_master(db, C5)
    at = _open_generator(_run(), C5)
    frame = _editor(at)
    assert frame["Nombre"].tolist() == [f"Pieza Lego 2x1 {c}" for c in C5_COLORS] + ["Pieza Lego 2x1 verde lima"]
    assert frame["Cantidad"].tolist() == [0] * 10 + [2] and frame["Crear"].tolist() == [True] * 11
    assert any(e.label == ":material/warning: Puntos para confirmar (2)" for e in at.expander)
    create = _button(at, f"inv_gen_create_{C5['id']}")
    assert create.label == ":material/check_circle: Crear 11 producto(s)"
    create.click().run()
    assert not at.exception
    children = {c["name"]: c for c in db.get_children(C5["id"])}
    assert len(children) == 11 and children["Pieza Lego 2x1 verde lima"]["quantity"] == 2
    assert "Se crearon 11 producto(s) en «Contenedor 5»; 10 quedan pendientes de conteo." in _text(at.success)
