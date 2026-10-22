# -*- coding: utf-8 -*-
"""Pestaña 🖨️ Etiquetas de Inventario (AppTest con la app real): vista previa,
guardado del tamaño, tamaño personalizado y contenido opcional."""

import re

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import labels  # noqa: E402
from core.storage import LabStorage  # noqa: E402


def _inventory_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import inventario

    st.session_state.storage = LabStorage()
    st.session_state.user = {
        "id": "p1", "role": "profesor", "full_name": "Prof Uno",
        "institutional_email": "prof@uniminuto.edu.co",
    }
    inventario.render()


def _open():
    at = AppTest.from_function(_inventory_script)
    at.run()
    assert not at.exception
    return at


def test_label_tab_shows_saved_size_and_a_preview_of_the_classic_format():
    at = _open()
    assert any("Configuración guardada: **50 × 25 mm · 203 dpi**" in c.value for c in at.caption)
    assert any("formato de siempre" in c.value for c in at.caption)
    assert at.selectbox(key="label_cfg_preset").value == "50x25"
    assert {m.label for m in at.metric} >= {"Texto más pequeño", "Alto de las barras"}
    assert at.image  # vista previa a resolucion real
    assert at.button(key="label_cfg_save").disabled  # nada que guardar todavia


def _markdown(at) -> str:
    return " ".join(element.value for element in at.markdown)


def test_tab_tells_which_paper_to_define_in_the_driver():
    at = _open()
    text = _markdown(at)
    assert "Papel al imprimir: 50 × 25 mm (1,97 × 0,98 in)" in text
    assert "USER (50,8 × 50,8 mm)" in text  # el valor de fábrica que recortaba la etiqueta

    at.selectbox(key="label_cfg_preset").select("100x75").run()
    assert "Papel al imprimir: 100 × 75 mm (3,94 × 2,95 in)" in _markdown(at)


def test_tab_explains_the_photos_trap_and_each_symptom():
    at = _open()
    expanders = {expander.label: expander for expander in at.expander}
    steps = next(e for label, e in expanders.items() if "paso a paso" in label)
    assert steps.proto.expanded  # sin configurar todavía: la guía se muestra abierta
    assert any("sale mal" in label for label in expanders)
    text = _markdown(at)
    assert "No imprimas el PNG ni abras la etiqueta con la app Fotos" in text
    assert "Dithering" in text and "Predeterminado" in text and "Tamaño real" in text
    assert "Recortada y ampliada" in text and "Girada 90°" in text and "Borrosa" in text


def test_saving_a_new_size_is_persisted_for_every_label():
    at = _open()
    at.selectbox(key="label_cfg_preset").select("100x50").run()
    assert not at.button(key="label_cfg_save").disabled
    at.button(key="label_cfg_save").click().run()
    assert not at.exception

    spec = labels.load_label_spec(LabStorage())
    assert (spec.width_mm, spec.height_mm, spec.dpi) == (100, 50, 203)
    pdf, _ = labels.item_label_files({"id": "LAB-001", "name": "Multímetro"}, spec)
    media_box = re.search(rb"/MediaBox \[0 0 ([0-9.]+) ([0-9.]+)\]", pdf)
    assert float(media_box.group(1)) * 25.4 / 72 == pytest.approx(100, abs=0.01)
    assert any("100 × 50 mm · 203 dpi" in c.value for c in _open().caption)


def test_custom_size_resolution_and_content_are_saved():
    at = _open()
    at.selectbox(key="label_cfg_preset").select("custom").run()
    at.number_input(key="label_cfg_width").set_value(75.0)
    at.number_input(key="label_cfg_height").set_value(35.0)
    at.radio(key="label_cfg_dpi").set_value(300)
    at.checkbox(key="label_cfg_category").uncheck()
    at.run()
    assert not at.exception
    at.button(key="label_cfg_save").click().run()

    spec = labels.load_label_spec(LabStorage())
    assert (spec.width_mm, spec.height_mm, spec.dpi) == (75, 35, 300)
    assert "category" not in spec.content and "brand" in spec.content


def _scan_script():
    import streamlit as st

    from core import barcode, labels
    from core.storage import LabStorage
    from views import escanear

    storage = LabStorage()
    if not storage.get_item("LAB-001"):
        storage.save_item(
            {"name": "Multímetro", "item_type": "standalone", "quantity": 2},
            "LAB-001", is_new=True, actor_email="prof@uniminuto.edu.co",
        )
        labels.save_label_spec(storage, labels.LabelSpec(100, 50))
    st.session_state.storage = storage
    st.session_state.user = {
        "id": "e1", "role": "estudiante", "full_name": "Ana Prueba",
        "institutional_email": "ana@uniminuto.edu.co",
    }
    st.session_state.scan_result = barcode.scan(storage, "LAB-001")
    escanear.render()


def test_scan_page_offers_labels_in_the_configured_size():
    at = AppTest.from_function(_scan_script)
    at.run()
    assert not at.exception
    downloads = at.get("download_button")
    assert len(downloads) == 2
    assert downloads[0].proto.label == ":material/label: PDF 100 × 50 mm"
    assert downloads[1].proto.label == "PNG · solo archivo"
    assert "No lo imprimas desde la app Fotos" in downloads[1].proto.help
    assert any("100 × 50 mm · 203 dpi" in c.value for c in at.caption)
