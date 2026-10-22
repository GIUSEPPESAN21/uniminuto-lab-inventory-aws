# -*- coding: utf-8 -*-
"""Ruta verificable: puntos de control derivados del código, coincidencia de
etiquetas, explicación de lecturas equivocadas y máquina de estados de la ruta
(sin escribir en la base por cada paso)."""

from datetime import datetime, timedelta, timezone

import pytest

from core import location, traceability

PRODUCT = {"id": "2-1-01-01-001", "name": "Arduino UNO", "location": "Gabinete azul"}
T0 = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


def _route(item=PRODUCT, parent=None, names=None):
    return traceability.new_route(item, parent, {"id": "req1"}, names)


# --- puntos de control ---------------------------------------------------------------

def test_standard_product_route_has_every_level_and_the_label_of_each_container():
    points = location.build_route_checkpoints(PRODUCT)
    assert [p["key"] for p in points] == ["estanteria", "piso", "contenedor", "caja", "item"]
    assert [p["label_code"] for p in points] == ["", "", "2-1-01-00-000", "2-1-01-01-000", "2-1-01-01-001"]
    assert location.compact_route(points) == "E2 › P1 › C01 › CJ01 › I001"
    assert points[-1]["name"] == "Arduino UNO"


def test_box_and_container_level_items_end_at_their_own_label():
    box = location.build_route_checkpoints({"id": "2-1-03-04-000", "name": "Resistencias"})
    container = location.build_route_checkpoints({"id": "2-1-03-00-000", "name": "Electrónica"})
    assert [p["key"] for p in box] == ["estanteria", "piso", "contenedor", "caja"]
    assert box[-1]["label_code"] == "2-1-03-04-000"
    assert [p["key"] for p in container] == ["estanteria", "piso", "contenedor"]
    assert container[-1]["label_code"] == "2-1-03-00-000"


def test_free_code_inside_a_structured_container_inherits_its_route():
    points = location.build_route_checkpoints(
        {"id": "LAB-MIC-01", "name": "Microscopio"}, {"id": "2-1-02-01-000", "location": "Armario"},
    )
    assert [p["key"] for p in points] == ["estanteria", "piso", "contenedor", "caja", "producto"]
    assert points[3]["label_code"] == "2-1-02-01-000"
    assert points[-1]["label_code"] == "LAB-MIC-01"


def test_mesa_lego_and_unstructured_routes():
    mesa = location.build_route_checkpoints({"id": "M1-E2", "name": "Láser"})
    lego = location.build_route_checkpoints({"id": "E3-LM07", "name": "Grúa"})
    free = location.build_route_checkpoints({"id": "LAB-OSC-01", "name": "Osciloscopio", "location": "Armario norte"})
    lost = location.build_route_checkpoints({"id": "CODIGO HEREDADO", "name": "Equipo"})
    assert [p["label_code"] for p in mesa] == ["", "M1-E2"]
    assert [p["short"] for p in lego] == ["E3", "LEGO", "LM07"]
    assert free[0]["title"] == "Armario norte" and free[-1]["label_code"] == "LAB-OSC-01"
    assert "responsable" in lost[0]["hint"] and lost[-1]["label_code"] == "CODIGO HEREDADO"


def test_registered_names_are_shown_next_to_each_label():
    points = location.build_route_checkpoints(PRODUCT, names={"2-1-01-00-000": "Electrónica básica"})
    assert points[2]["name"] == "Electrónica básica"


def test_checkpoints_never_raise_on_empty_input():
    assert location.build_route_checkpoints(None)[0]["key"] == "referencia"


@pytest.mark.parametrize("expected, scanned, ok", [
    ("2-1-01-01-001", "2-1-01-01-001", True),
    ("2-1-01-01-001", "  2-1-01-01-001\n", True),
    ("2-1-01-01-001", "2-1-1-1-1", True),           # ceros de relleno omitidos a mano
    ("M1-E2", "m1-e2", True),
    ("LAB-MIC-01", "lab-mic-01", True),
    ("2-1-01-01-001", "2-1-01-01-002", False),
    ("2-1-01-01-001", "", False),
])
def test_codes_match_tolerates_hand_typing(expected, scanned, ok):
    assert location.codes_match(expected, scanned) is ok


# --- explicaciones de lecturas equivocadas -----------------------------------------------

@pytest.mark.parametrize("scanned, expected_text", [
    ("1-1-01-00-000", "Estantería 1; tu producto está en la Estantería 2, Piso 1"),
    ("2-3-01-00-000", "estás en el Piso 3. El tuyo es el Piso 1 (2 pisos de distancia)"),
    ("2-1-02-00-000", "Estás en el Contenedor 02; el tuyo es el 01, en el mismo piso"),
    ("2-1-01-03-000", "en la Caja 03; abre la Caja 01"),
    ("2-1-01-01-004", "ese es el Ítem 004; busca el Ítem 001"),
    ("M1-E2", "Mesa de trabajo 1; tu producto está en la Estantería 2, Piso 1"),
    ("XYZ ???", "No reconozco"),
])
def test_mismatch_explains_where_you_are_and_where_to_go(scanned, expected_text):
    assert expected_text in location.explain_mismatch("2-1-01-01-001", scanned)


def test_mismatch_names_the_scanned_product_and_handles_mesas_and_lego():
    text = location.explain_mismatch("2-1-01-01-001", "2-1-01-01-004", known_item={"name": "Sensor"})
    assert text.startswith("Escaneaste «Sensor» (2-1-01-01-004).")
    assert "Mesa 2; tu equipo está en la Mesa de trabajo 1" in location.explain_mismatch("M1-E2", "M2-E1")
    assert "busca el Equipo 2" in location.explain_mismatch("M1-E2", "M1-E5")
    assert "el tuyo es el Modelo 07" in location.explain_mismatch("E3-LM07", "E3-LM02")


def test_mismatch_for_free_coded_products_points_to_their_own_label():
    inside = location.explain_mismatch("2-1-02-01-000", "2-1-02-01-003", product_code="LAB-MIC-01")
    assert "Estás en el lugar correcto" in inside and "LAB-MIC-01" in inside
    free = location.explain_mismatch("LAB-OSC-01", "LAB-OTRO-01", target_location="Armario norte")
    assert "«LAB-OSC-01» (Armario norte)" in free


# --- máquina de estados -----------------------------------------------------------------

def test_full_route_by_scanning_every_label_in_order():
    route = _route()
    assert traceability.confirm_arrival(route, T0)["ok"]                      # Estantería
    assert traceability.confirm_arrival(route, T0 + timedelta(seconds=20))["ok"]  # Piso
    first = traceability.apply_code(route, "2-1-01-00-000", T0 + timedelta(seconds=40))
    assert first["ok"] and "Contenedor 01 verificado" in first["message"] and "Siguiente" in first["message"]
    traceability.apply_code(route, "2-1-01-01-000", T0 + timedelta(seconds=60))
    last = traceability.apply_code(route, "2-1-01-01-001", T0 + timedelta(seconds=95))
    assert last["completed"] and "¡Llegaste a tu producto!" in last["message"]
    stats = traceability.route_stats(route)
    assert stats["full"] and stats["scanned"] == stats["labels"] == 3
    assert stats["duration_s"] == 95 and stats["progress"] == 1.0
    assert [p["method"] for p in route["checkpoints"]] == ["arrival", "arrival", "scan", "scan", "scan"]


def test_scanning_the_container_proves_the_shelf_and_floor():
    route = _route()
    traceability.apply_code(route, "2-1-01-00-000", T0)
    assert [p["method"] for p in route["checkpoints"][:3]] == ["implied", "implied", "scan"]
    assert traceability.current_index(route) == 3


def test_jumping_ahead_marks_skipped_labels_as_inferred_and_they_can_be_scanned_later():
    route = _route()
    result = traceability.apply_code(route, "2-1-01-01-001", T0)
    assert result["completed"] and "Saltaste 2 etiquetas" in result["message"]
    stats = traceability.route_stats(route)
    assert stats["complete"] and not stats["full"] and stats["inferred"] == 2
    assert [state for _, state in traceability.breadcrumb(route)][2:4] == ["inferred", "inferred"]

    upgrade = traceability.apply_code(route, "2-1-01-00-000", T0 + timedelta(seconds=5))
    assert upgrade["ok"] and "ya no queda inferida" in upgrade["message"]
    traceability.apply_code(route, "2-1-01-01-000", T0 + timedelta(seconds=9))
    assert traceability.route_stats(route)["full"]


def test_wrong_label_counts_an_attempt_and_explains_the_way():
    route = _route()
    traceability.confirm_arrival(route, T0)
    traceability.confirm_arrival(route, T0)
    result = traceability.apply_code(route, "2-1-02-00-000", T0, known_item={"name": "Mecánica"})
    assert not result["ok"] and result["tone"] == "warning"
    assert "Escaneaste «Mecánica»" in result["message"] and "el tuyo es el 01" in result["message"]
    assert route["attempts"] == 1 and traceability.current_index(route) == 2


def test_repeated_or_empty_codes_and_arrival_on_a_labelled_point():
    route = _route()
    assert traceability.apply_code(route, "   ", T0)["tone"] == "warning"
    traceability.apply_code(route, "2-1-01-00-000", T0)
    again = traceability.apply_code(route, "2-1-01-00-000", T0)
    assert again["ok"] and again["tone"] == "info" and "Ya confirmaste" in again["message"]
    arrival = traceability.confirm_arrival(route, T0)
    assert not arrival["ok"] and "tiene etiqueta" in arrival["message"]


def test_completed_route_ignores_foreign_codes_and_is_json_serializable():
    import json

    route = _route()
    traceability.apply_code(route, "2-1-01-01-001", T0)
    assert traceability.apply_code(route, "9-9", T0)["tone"] == "info"
    assert json.loads(json.dumps(route))["item_id"] == "2-1-01-01-001"


def test_route_timeline_states_follow_the_progress():
    route = _route()
    traceability.apply_code(route, "2-1-01-00-000", T0)
    states = [step["state"] for step in traceability.route_timeline(route)]
    assert states == ["done", "done", "done", "current", "pending"]
    steps = traceability.route_timeline(route)
    assert "Etiqueta escaneada: 2-1-01-00-000" in steps[2]["detail"]
    assert "Etiqueta: 2-1-01-01-000" in steps[3]["detail"]
    assert steps[2]["time"] == "10:00:00 a. m."   # hora de Bogotá


def test_free_coded_product_route_completes_with_its_own_label():
    route = _route({"id": "LAB-MIC-01", "name": "Microscopio", "parent_id": "2-1-02-01-000"},
                   {"id": "2-1-02-01-000"})
    wrong = traceability.apply_code(route, "2-1-02-01-007", T0)
    assert "Estás en el lugar correcto" in wrong["message"]
    assert traceability.apply_code(route, "LAB-MIC-01", T0)["completed"]
