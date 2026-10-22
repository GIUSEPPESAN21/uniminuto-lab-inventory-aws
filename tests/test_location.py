# -*- coding: utf-8 -*-
from core.location import build_location_guide


def test_standard_item_guide_contains_every_physical_level():
    guide = build_location_guide({"id": "2-1-03-04-005", "name": "Arduino", "location": "Gabinete azul"})
    text = " ".join(guide["steps"])
    assert "Estantería 2" in text
    assert "Piso 1" in text
    assert "Contenedor 03" in text
    assert "Caja 04" in text
    assert "Ítem 005" in text
    assert "Gabinete azul" in text
    assert guide["structured"] is True


def test_container_level_guide_omits_non_applicable_box_and_item():
    guide = build_location_guide({"id": "2-1-01-00-000", "name": "Contenedor 1"})
    text = " ".join(guide["steps"])
    assert "Contenedor 01" in text
    assert "Caja" not in text
    assert "Ítem 000" not in text


def test_special_code_guides():
    mesa = " ".join(build_location_guide({"id": "M1-E2", "name": "Láser"})["steps"])
    lego = " ".join(build_location_guide({"id": "E3-LM07", "name": "Modelo"})["steps"])
    assert "Mesa de trabajo 1" in mesa and "Equipo 2" in mesa
    assert "Estantería 3" in lego and "Modelo 7" in lego


def test_free_code_uses_text_location_and_parent_fallback():
    guide = build_location_guide(
        {"id": "LAB-MIC-01", "name": "Microscopio", "location": ""},
        {"id": "CAJA 001", "location": "Armario norte"},
    )
    assert guide["structured"] is False
    assert any("Armario norte" in step for step in guide["steps"])


def test_unknown_location_never_raises_and_asks_for_help():
    guide = build_location_guide({"id": "CODIGO HEREDADO", "name": "Equipo"})
    assert any("responsable" in step for step in guide["steps"])