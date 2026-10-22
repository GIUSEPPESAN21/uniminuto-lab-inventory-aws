# -*- coding: utf-8 -*-
"""core/data_quality.py: auditoria del inventario y correcciones seguras.

El fixture `real_containers` reproduce los 3 Contenedores Principales que hoy
tiene la base en produccion (nombres, ubicaciones y descripciones tal cual,
con sus errores), sin ningun dato personal."""

import pytest

from core import data_quality as dq
from core.storage import LabStorage

ACTOR = "prof.ficticio@uniminuto.edu.co"

DESC_1 = ("Contiene Piezas Lego de Pines 4x2 - 2x2 - 2x1 \nAdemás, Contiene un Caja con Separadores "
          "de Fichas y Pieza lego Lisas de 2x2")
DESC_2 = ("Contiene Piezas Lego de Pines 6x2 - 6x1 - 4x1 - Bases de 1 pin \nAdemás, Contiene una Caja "
          "con Puertas y Ventanas, Piezas lego Lisas de 3x2 - 4x2 - 6x2, Hay piezas con Biseles "
          "3x2 - 2x2 - 4x1 - 2x1 - 1x0")
DESC_3 = "Contiene Piezas Lego de Pines 4x2 - 3x2 - 2x1."


def _item(item_id, **kwargs):
    base = {
        "id": item_id, "name": "Producto", "category": "General", "description": "",
        "item_type": "standalone", "parent_id": "", "unit": "unidad", "quantity": 1,
        "location": "", "min_stock_alert": 0, "status": "active", "created_by": ACTOR, "updated_at": "",
    }
    base.update(kwargs)
    return base


def _real_rows():
    return [
        _item("2-1-01-00-000", name="Contenedor 1", category="Piezas Lego", description=DESC_1,
              item_type="master", quantity=0, location="Estantería- 2; Piso-1; Contenedor-1."),
        _item("2-1-02-00-000", name="Contendor 2", category="Piezas Lego", description=DESC_2,
              item_type="master", quantity=0, location="Estantería- 2; Piso-1; Contenedor-2."),
        _item("2-1-03-00-000", name="Contendor 3", category="Piezas Lego", description=DESC_3,
              item_type="master", quantity=0, location="Estantería- 2; Piso-1; Contenedor-3."),
    ]


@pytest.fixture
def real_containers():
    return _real_rows()


def _rules(findings, item_id=None):
    return {f.rule for f in findings if item_id is None or f.item_id == item_id}


def _one(findings, rule, item_id=None):
    matches = [f for f in findings if f.rule == rule and (item_id is None or f.item_id == item_id)]
    assert len(matches) == 1, f"se esperaba un hallazgo {rule} ({item_id}), hay {len(matches)}"
    return matches[0]


# --- ubicacion derivada del codigo -------------------------------------------------------

@pytest.mark.parametrize("code, expected", [
    ("2-1-01-00-000", "Estantería 2 · Piso 1 · Contenedor 01"),
    ("2-1-01-01-000", "Estantería 2 · Piso 1 · Contenedor 01 · Caja 01"),
    ("3-6-12-04-017", "Estantería 3 · Piso 6 · Contenedor 12 · Caja 04 · Ítem 017"),
    ("M1-E2", "Mesa de trabajo 1 · Equipo 2"),
    ("E3-LM07", "Estantería 3 · Exhibición Lego · Modelo 07"),
    (" 2-1-01-00-000 ", "Estantería 2 · Piso 1 · Contenedor 01"),
])
def test_suggest_location_from_code(code, expected):
    assert dq.suggest_location_from_code(code) == expected


@pytest.mark.parametrize("code", ["LAB-MIC-01", "0012345", "", None, "4-2-05-12-001", "Caja 1"])
def test_free_or_invalid_codes_have_no_derived_location(code):
    assert dq.suggest_location_from_code(code) is None


def test_parse_location_understands_the_handwritten_format():
    parsed = dq.parse_location_text("Estantería- 2; Piso-1; Contenedor-1.")
    assert parsed == {"fields": {"estanteria": 2, "piso": 1, "contenedor": 1}, "leftover": ""}


def test_parse_location_keeps_extra_references_and_reads_label_routes():
    assert dq.parse_location_text("Estante 2, piso 1 (lado izquierdo)")["leftover"] == "lado izquierdo"
    assert dq.parse_location_text("RUTA: E2 › P1 › C01 › CJ03")["fields"] == {
        "estanteria": 2, "piso": 1, "contenedor": 1, "caja": 3,
    }
    assert dq.parse_location_text("Contendor N° 4")["fields"] == {"contenedor": 4}
    assert dq.parse_location_text("") == {"fields": {}, "leftover": ""}


def test_location_conflicts_compare_the_text_against_the_code():
    assert dq.location_conflicts("2-1-01-00-000", "Estantería- 2; Piso-1; Contenedor-1.") == []
    assert dq.location_conflicts("2-1-01-00-000", "Estantería 3, Piso 1, Contenedor 2") == [
        ("estanteria", 3, 2), ("contenedor", 2, 1),
    ]
    # El codigo es de nivel contenedor: una caja en la ubicacion lo contradice.
    assert dq.location_conflicts("2-1-01-00-000", "Contenedor 1, caja 3") == [("caja", 3, 0)]
    assert dq.location_conflicts("M1-E2", "Mesa de trabajo 2") == [("mesa", 2, 1)]
    assert dq.location_conflicts("LAB-MIC-01", "Estantería 9") == []


# --- errores de digitacion y medidas ---------------------------------------------------------

def test_typos_are_detected_against_the_vocabulary():
    assert dq.find_typos("Contendor 2") == [("Contendor", "Contenedor")]
    assert dq.find_typos("CONTENDOR 2") == [("CONTENDOR", "CONTENEDOR")]
    assert dq.find_typos("piezsa lego") == [("piezsa", "pieza")]
    assert dq.find_typos("Estanteira 2") == [("Estanteira", "Estantería")]       # transposicion
    assert dq.find_typos("Caja de Resistencis 220") == [("Resistencis", "Resistencias")]


@pytest.mark.parametrize("text", [
    "Contenedor 1", "Estanteria 2", "Multimetro digital", "Ejes lisos", "Buzzer piezo",
    "Puerto USB", "Casa", "Caja", "Kit de robótica", "", None,
])
def test_valid_words_and_accent_only_differences_are_not_typos(text):
    assert dq.find_typos(text) == []


def test_fix_typos_only_replaces_the_wrong_word():
    text = "Contendor 2  (Contendor azul)\nOtro"
    assert dq.fix_typos(text, dq.find_typos(text)) == "Contenedor 2  (Contenedor azul)\nOtro"
    assert dq.fix_typos("Sin cambios", []) == "Sin cambios"


def test_invalid_dimensions():
    assert dq.invalid_dimensions(DESC_2) == ["1x0"]
    assert dq.invalid_dimensions("0x4 y 0X4 y 2 x 0") == ["0x4", "2x0"]
    assert dq.invalid_dimensions(DESC_1) == []
    assert dq.invalid_dimensions("Tornillo M4 x 20mm, 2.5x1") == []


# --- contenido descrito ---------------------------------------------------------------------

def test_contents_of_the_real_containers_are_extracted():
    assert dq.extract_described_contents(DESC_1) == [
        {"kind": "piezas", "label": "Piezas Lego de Pines", "sizes": ["4x2", "2x2", "2x1"]},
        {"kind": "caja", "label": "Caja con Separadores de Fichas", "sizes": []},
        {"kind": "piezas", "label": "Pieza lego Lisas", "sizes": ["2x2"]},
    ]
    assert dq.extract_described_contents(DESC_2) == [
        {"kind": "piezas", "label": "Piezas Lego de Pines", "sizes": ["6x2", "6x1", "4x1"]},
        {"kind": "piezas", "label": "Bases de 1 pin", "sizes": []},
        {"kind": "caja", "label": "Caja con Puertas y Ventanas", "sizes": []},
        {"kind": "piezas", "label": "Piezas lego Lisas", "sizes": ["3x2", "4x2", "6x2"]},
        {"kind": "piezas", "label": "Piezas con Biseles", "sizes": ["3x2", "2x2", "4x1", "2x1", "1x0"]},
    ]
    assert dq.extract_described_contents(DESC_3) == [
        {"kind": "piezas", "label": "Piezas Lego de Pines", "sizes": ["4x2", "3x2", "2x1"]},
    ]
    assert dq.summarize_contents(dq.extract_described_contents(DESC_2)) == {"piece_types": 12, "boxes": 1}


def test_content_detection_edge_cases():
    assert dq.extract_described_contents("") == []
    assert dq.extract_described_contents("Ladrillos 2x4, 2x2 y 1x1") == [
        {"kind": "piezas", "label": "Ladrillos", "sizes": ["2x4", "2x2", "1x1"]},
    ]
    assert dq.describes_contents(DESC_3)
    assert dq.describes_contents("Incluye tornillos")
    assert not dq.describes_contents("Contenedor principal de componentes")
    assert not dq.describes_contents(None)


def test_suggest_child_codes_skips_taken_boxes():
    taken = ["2-1-01-00-000", "2-1-01-01-000"]
    assert dq.suggest_child_codes("2-1-01-00-000", taken, 2) == ["2-1-01-02-000", "2-1-01-03-000"]
    assert dq.suggest_child_codes("LAB-01", taken) == []
    assert dq.suggest_child_codes("no valido", taken) == []


# --- auditoria de los 3 contenedores reales --------------------------------------------------

def test_real_containers_have_no_integrity_errors(real_containers):
    findings = dq.audit_items(real_containers)
    assert not [f for f in findings if f.severity == dq.SEVERITY_ERROR]
    assert _rules(findings) == {
        "nothing_lendable", "name_typo", "undeclared_contents", "invalid_dimension",
        "generic_name", "location_format",
    }
    assert [f.severity for f in findings] == sorted(
        (f.severity for f in findings), key=dq.SEVERITIES.index
    )


def test_real_containers_typos_and_locations(real_containers):
    findings = dq.audit_items(real_containers)
    assert "name_typo" not in _rules(findings, "2-1-01-00-000")
    typo = _one(findings, "name_typo", "2-1-02-00-000")
    assert typo.severity == dq.SEVERITY_WARNING and typo.field == "name"
    assert typo.fix["changes"] == {"name": "Contenedor 2"}
    assert typo.fix["before"] == {"name": "Contendor 2"}

    location = _one(findings, "location_format", "2-1-03-00-000")
    assert location.severity == dq.SEVERITY_INFO
    assert location.fix["changes"] == {"location": "Estantería 2 · Piso 1 · Contenedor 03"}
    assert "location_conflict" not in _rules(findings)          # el texto y el codigo coinciden


def test_real_containers_contents_are_reported_as_unregistered(real_containers):
    findings = dq.audit_items(real_containers)
    first = _one(findings, "undeclared_contents", "2-1-01-00-000")
    assert "4 tipo(s) de pieza y 1 caja(s)" in first.message
    assert "2-1-01-01-000" in first.suggestion
    assert first.details == [
        "Piezas Lego de Pines: 4x2, 2x2, 2x1", "📦 Caja con Separadores de Fichas", "Pieza lego Lisas: 2x2",
    ]
    assert "12 tipo(s) de pieza" in _one(findings, "undeclared_contents", "2-1-02-00-000").message
    assert _one(findings, "invalid_dimension", "2-1-02-00-000").message.endswith("1x0.")
    assert _one(findings, "nothing_lendable").item_id == ""


def test_merge_fixes_groups_the_safe_changes_per_item(real_containers):
    merged = dq.merge_fixes(dq.audit_items(real_containers))
    assert set(merged) == {"2-1-01-00-000", "2-1-02-00-000", "2-1-03-00-000"}
    assert merged["2-1-02-00-000"]["changes"] == {
        "name": "Contenedor 2", "location": "Estantería 2 · Piso 1 · Contenedor 02",
    }
    assert merged["2-1-01-00-000"]["changes"] == {"location": "Estantería 2 · Piso 1 · Contenedor 01"}
    summary = dq.summarize(dq.audit_items(real_containers), real_containers)
    assert summary == {"items": 3, "errors": 0, "warnings": 7, "infos": 6, "fixable": 5,
                       "items_with_problems": 3, "healthy_items": 0}


def test_findings_never_include_the_creator_email(real_containers):
    for finding in dq.audit_items(real_containers):
        assert ACTOR not in str(finding.as_dict())


def test_once_contents_are_registered_only_missing_sizes_are_reported(real_containers):
    items = real_containers + [
        _item("2-1-03-01-000", name="Ladrillo Lego 2x4", category="Piezas Lego", item_type="child",
              parent_id="2-1-03-00-000", quantity=40, location="Estantería 2 · Piso 1 · Contenedor 03 · Caja 01"),
        _item("2-1-03-02-000", name="Ladrillo Lego 2x3", category="Piezas Lego", item_type="child",
              parent_id="2-1-03-00-000", quantity=25, location="Estantería 2 · Piso 1 · Contenedor 03 · Caja 02"),
    ]
    findings = dq.audit_items(items)
    assert "undeclared_contents" not in _rules(findings, "2-1-03-00-000")
    partial = _one(findings, "contents_partially_registered", "2-1-03-00-000")
    assert partial.message.endswith("2x1.")                    # 4x2 == 2x4 y 3x2 == 2x3
    assert "nothing_lendable" not in _rules(findings)
    assert not [f for f in findings if f.item_id.startswith("2-1-03-0") and f.item_id != "2-1-03-00-000"]


# --- casos borde ----------------------------------------------------------------------------

def test_empty_inventory_has_no_findings():
    assert dq.audit_items([]) == []
    assert dq.audit_items(None) == []
    assert dq.summarize([], [])["items"] == 0


def test_duplicate_and_malformed_codes():
    items = [
        _item("LAB-001", name="Multímetro"), _item("LAB-001", name="Multímetro B"),
        _item("lab-001", name="Osciloscopio"), _item("4-2-05-12-001", name="Pinzas"),
        _item(" M1-E1", name="Fuente"), _item("", name="Sin código"),
    ]
    findings = dq.audit_items(items)
    assert _one(findings, "duplicate_id", "LAB-001").severity == dq.SEVERITY_ERROR
    assert "LAB-001, lab-001" in _one(findings, "id_case_collision").message
    assert _one(findings, "code_format", "4-2-05-12-001").severity == dq.SEVERITY_WARNING
    assert _one(findings, "id_whitespace", " M1-E1").severity == dq.SEVERITY_ERROR
    assert _one(findings, "missing_id", "").severity == dq.SEVERITY_ERROR


def test_hierarchy_errors():
    items = [
        _item("2-1-01-00-000", name="Contenedor A", item_type="master", quantity=0),
        _item("2-1-01-01-000", name="Tornillos", item_type="child", parent_id="", quantity=5),
        _item("2-1-01-02-000", name="Tuercas", item_type="child", parent_id="9-9-99", quantity=5),
        _item("2-1-01-03-000", name="Arandelas", item_type="child", parent_id="LAB-002", quantity=5),
        _item("LAB-002", name="Pinzas", item_type="standalone", parent_id="2-1-01-00-000", quantity=1),
        _item("2-1-01-04-000", name="Clavos", item_type="child", parent_id="2-1-01-04-000", quantity=5),
        _item("X-1", name="Raro", item_type="otro", quantity=1),
    ]
    findings = dq.audit_items(items)
    assert _one(findings, "orphan_child", "2-1-01-01-000").severity == dq.SEVERITY_ERROR
    assert _one(findings, "missing_parent", "2-1-01-02-000").severity == dq.SEVERITY_ERROR
    assert _one(findings, "parent_not_master", "2-1-01-03-000").severity == dq.SEVERITY_ERROR
    assert _one(findings, "unexpected_parent", "LAB-002").severity == dq.SEVERITY_ERROR
    assert _one(findings, "self_parent", "2-1-01-04-000").severity == dq.SEVERITY_ERROR
    assert _one(findings, "item_type", "X-1").severity == dq.SEVERITY_ERROR


def test_code_level_and_child_outside_its_container():
    items = [
        _item("2-1-01-00-000", name="Contenedor A", item_type="master", quantity=0, description="Contiene cables"),
        _item("2-1-02-01-000", name="Cables", item_type="child", parent_id="2-1-01-00-000", quantity=3),
        _item("2-1-01-01-001", name="Contenedor B", item_type="master", quantity=0),
        _item("2-1-01-02-000", name="Resistencia", item_type="standalone", quantity=2),
    ]
    findings = dq.audit_items(items)
    outside = _one(findings, "child_outside_parent", "2-1-02-01-000")
    assert "Contenedor 02" in outside.message and "Contenedor 01" in outside.message
    assert "nivel ítem" in _one(findings, "code_level", "2-1-01-01-001").message
    assert "nivel caja" in _one(findings, "code_level", "2-1-01-02-000").message
    assert "code_level" not in _rules(findings, "2-1-02-01-000")


def test_quantities():
    items = [
        _item("2-1-01-00-000", name="Contenedor A", item_type="master", quantity=5),
        _item("2-1-01-01-000", name="Tornillos", item_type="child", parent_id="2-1-01-00-000", quantity=0),
        _item("LAB-1", name="Pinzas", quantity=-2),
        _item("LAB-2", name="Cautín", quantity="tres", min_stock_alert="x"),
        _item("LAB-3", name="Fuente", quantity="4", min_stock_alert=""),
    ]
    findings = dq.audit_items(items)
    assert _one(findings, "master_quantity", "2-1-01-00-000").severity == dq.SEVERITY_INFO
    assert _one(findings, "zero_quantity", "2-1-01-01-000").severity == dq.SEVERITY_WARNING
    assert _one(findings, "quantity_negative", "LAB-1").severity == dq.SEVERITY_ERROR
    assert _one(findings, "quantity_type", "LAB-2").severity == dq.SEVERITY_ERROR
    assert _one(findings, "min_stock_alert", "LAB-2").severity == dq.SEVERITY_WARNING
    assert not _rules(findings, "LAB-3") & {"quantity_type", "min_stock_alert", "zero_quantity"}


def test_categories_names_and_duplicates():
    items = [
        _item("LAB-1", name="Ladrillo 2x4", category="Piezas Lego", parent_id=""),
        _item("LAB-2", name="ladrillo  2x4", category="piezas lego"),
        _item("LAB-3", name="  Placa base", category="Piezas Lego"),
        _item("LAB-4", name="Sensor", category=""),
        _item("LAB-5", name="", category="General"),
    ]
    findings = dq.audit_items(items)
    variant = _one(findings, "category_variant", "LAB-2")
    assert variant.fix["changes"] == {"category": "Piezas Lego"}
    assert len([f for f in findings if f.rule == "duplicate_name"]) == 2
    assert _one(findings, "name_spaces", "LAB-3").fix["changes"] == {"name": "Placa base"}
    assert _one(findings, "missing_category", "LAB-4").severity == dq.SEVERITY_INFO
    assert _one(findings, "missing_name", "LAB-5").severity == dq.SEVERITY_ERROR


def test_same_name_in_different_containers_is_not_a_duplicate():
    items = [
        _item("2-1-01-00-000", name="Contenedor 1 · Lego", item_type="master", quantity=0),
        _item("2-1-02-00-000", name="Contenedor 2 · Lego", item_type="master", quantity=0),
        _item("2-1-01-01-000", name="Ladrillo 2x4", item_type="child", parent_id="2-1-01-00-000", quantity=9),
        _item("2-1-02-01-000", name="Ladrillo 2x4", item_type="child", parent_id="2-1-02-00-000", quantity=9),
    ]
    assert "duplicate_name" not in _rules(dq.audit_items(items))


def test_location_rules():
    items = [
        _item("2-1-01-01-001", name="Pinza", location="Estantería 3, piso 1, contenedor 1"),
        _item("2-1-01-01-002", name="Lupa", location=""),
        _item("2-1-01-01-003", name="Regla", location="Bodega"),
        _item("2-1-01-01-004", name="Compás", location="Estantería 2 · Piso 1 · Contenedor 01 · Caja 01 · Ítem 004"),
        _item("2-1-01-01-005", name="Escuadra", location="Estante 2 piso 1, junto a la ventana"),
        _item("LAB-9", name="Taladro", location=""),
        _item("LAB-10", name="Sierra", location="Bodega 2"),
    ]
    findings = dq.audit_items(items)
    conflict = _one(findings, "location_conflict", "2-1-01-01-001")
    assert conflict.fix["changes"] == {"location": "Estantería 2 · Piso 1 · Contenedor 01 · Caja 01 · Ítem 001"}
    assert _one(findings, "location_missing", "2-1-01-01-002").fix is not None
    assert _one(findings, "location_unstructured", "2-1-01-01-003").fix is None
    assert not {r for r in _rules(findings, "2-1-01-01-004") if r.startswith("location")}
    assert not {r for r in _rules(findings, "2-1-01-01-005") if r.startswith("location")}   # texto extra: se respeta
    assert _one(findings, "location_unknown", "LAB-9").severity == dq.SEVERITY_WARNING
    assert "location_unknown" not in _rules(findings, "LAB-10")


def test_child_with_free_code_inherits_the_parent_location():
    items = [
        _item("CONT-01", name="Kit de robótica", item_type="master", quantity=0, location="Mesa 2"),
        _item("SUB-01", name="Motores", item_type="child", parent_id="CONT-01", quantity=4),
    ]
    assert "location_unknown" not in _rules(dq.audit_items(items))


def test_name_that_contradicts_the_code():
    items = [_item("2-1-02-00-000", name="Contenedor 3", item_type="master", quantity=0,
                   location="Estantería 2 · Piso 1 · Contenedor 02")]
    finding = _one(dq.audit_items(items), "name_code_mismatch")
    assert "Contenedor 3 (el código dice 2)" in finding.message


def test_retired_items_are_skipped_but_their_active_children_are_flagged():
    items = [
        _item("2-1-01-00-000", name="Contendor viejo", item_type="master", quantity=0, status="retired"),
        _item("2-1-01-01-000", name="Tornillos", item_type="child", parent_id="2-1-01-00-000", quantity=3),
    ]
    findings = dq.audit_items(items)
    assert not _rules(findings, "2-1-01-00-000")
    assert _one(findings, "parent_retired", "2-1-01-01-000").severity == dq.SEVERITY_WARNING
    assert "name_typo" in _rules(dq.audit_items(items, include_retired=True), "2-1-01-00-000")


def test_raw_excel_rows_with_empty_cells_are_accepted():
    row = {"id": "2-1-01-00-000", "name": "Contenedor 1", "category": None, "description": float("nan"),
           "item_type": "master", "parent_id": None, "quantity": "0", "location": "None",
           "min_stock_alert": "", "status": ""}
    findings = dq.audit_items([row])
    assert "missing_category" in _rules(findings)
    assert _one(findings, "location_missing").fix["before"] == {"location": ""}


# --- aplicar correcciones -------------------------------------------------------------------

def test_apply_fix_returns_the_full_item_with_the_change(real_containers):
    fix = _one(dq.audit_items(real_containers), "name_typo", "2-1-02-00-000").fix
    data = dq.apply_fix(real_containers[1], fix)
    assert data["name"] == "Contenedor 2"
    assert data["description"] == DESC_2 and data["item_type"] == "master"


def test_apply_fix_refuses_when_the_item_changed(real_containers):
    fix = _one(dq.audit_items(real_containers), "name_typo", "2-1-02-00-000").fix
    edited = dict(real_containers[1], name="Contenedor de bases")
    with pytest.raises(dq.StaleFixError, match="cambió"):
        dq.apply_fix(edited, fix)
    with pytest.raises(dq.StaleFixError, match="ya no existe"):
        dq.apply_fix(None, fix)


def test_fixes_applied_through_the_real_storage_clear_the_findings(real_containers):
    storage = LabStorage()
    for row in real_containers:
        storage.save_item(row, row["id"], is_new=True, actor_email=ACTOR)

    findings = dq.audit_items(storage.get_all_items(include_retired=True))
    for item_id, fix in dq.merge_fixes(findings).items():
        data = dq.apply_fix(storage.get_item(item_id), fix)
        storage.save_item(data, item_id, is_new=False, actor_email=ACTOR,
                          details=f"Corrección de calidad de datos: {fix['label']}.")

    assert storage.get_item("2-1-02-00-000")["name"] == "Contenedor 2"
    assert storage.get_item("2-1-03-00-000")["location"] == "Estantería 2 · Piso 1 · Contenedor 03"
    assert storage.get_item("2-1-01-00-000")["description"] == DESC_1      # lo demas no cambia
    history = storage.get_item_history("2-1-02-00-000")
    assert history[0]["details"].startswith("Corrección de calidad de datos")
    remaining = _rules(dq.audit_items(storage.get_all_items(include_retired=True)))
    assert not remaining & {"name_typo", "location_format"}
    assert "undeclared_contents" in remaining                  # eso requiere registrar productos


# --- vista: Reportes → 🩺 Salud del inventario (AppTest) -------------------------------------

PROFESSOR_EMAIL = "prof.ficticio@uniminuto.edu.co"


def _reportes_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import reportes

    st.session_state.storage = LabStorage()
    st.session_state.user = {
        "id": "p1", "role": "profesor", "full_name": "Prof Ficticio",
        "institutional_email": "prof.ficticio@uniminuto.edu.co",
    }
    reportes.render()


@pytest.fixture
def seeded_storage():
    storage = LabStorage()
    for row in _real_rows():
        # Creados por OTRA persona: la vista nunca debe mostrar ese correo.
        storage.save_item(row, row["id"], is_new=True, actor_email="otra.persona@uniminuto.edu.co")
    return storage


def _open_reportes():
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_function(_reportes_script)
    at.run(timeout=60)
    assert not at.exception
    return at


def _button(at, label):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"no se encontró el botón {label!r}: {[b.label for b in at.button]}"
    return matches[0]


def _page_html(at):
    return " ".join(m.value for m in at.markdown)


def test_health_tab_shows_the_findings_grouped_by_severity(seeded_storage):
    at = _open_reportes()
    assert [e.label for e in at.expander] == [":material/warning: Avisos (7)", ":material/lightbulb: Sugerencias (6)"]
    html = _page_html(at)
    assert "Salud del inventario" in html and "Contendor 2" in html
    assert "Piezas con Biseles: 3x2, 2x2, 4x1, 2x1, 1x0" in html
    assert "lab-stat-grid" in html and "lab-badge--warning" in html
    assert "otra.persona@" not in html                         # nunca se muestra quien registro
    _button(at, ":material/build: Corregir a «Contenedor 2»")
    _button(at, "Revisar y aplicar todas (5)")


def test_a_safe_fix_is_only_written_after_confirmation(seeded_storage):
    at = _open_reportes()
    _button(at, ":material/build: Corregir a «Contenedor 2»").click().run(timeout=60)
    assert seeded_storage.get_item("2-1-02-00-000")["name"] == "Contendor 2"   # todavia nada
    assert "Revisa el cambio antes de guardarlo" in _page_html(at)

    _button(at, ":material/check_circle: Confirmar y guardar").click().run(timeout=60)
    assert not at.exception
    assert seeded_storage.get_item("2-1-02-00-000")["name"] == "Contenedor 2"
    assert any("Corrección aplicada en 1 ítem(s)" in s.value for s in at.success)
    history = seeded_storage.get_item_history("2-1-02-00-000")
    assert history[0]["details"] == "Corrección de calidad de datos: Corregir a «Contenedor 2»."
    assert history[0]["actor_user_id"] == PROFESSOR_EMAIL
    assert not any(b.label == ":material/build: Corregir a «Contenedor 2»" for b in at.button)


def test_cancelling_a_fix_changes_nothing(seeded_storage):
    at = _open_reportes()
    _button(at, ":material/build: Normalizar ubicación").click().run(timeout=60)
    _button(at, "Cancelar").click().run(timeout=60)
    assert not at.exception
    assert seeded_storage.get_item("2-1-01-00-000")["location"] == "Estantería- 2; Piso-1; Contenedor-1."
    assert at.session_state["dq_pending"] is None


def test_all_safe_fixes_can_be_applied_at_once(seeded_storage):
    at = _open_reportes()
    _button(at, "Revisar y aplicar todas (5)").click().run(timeout=60)
    _button(at, ":material/check_circle: Confirmar y guardar").click().run(timeout=60)
    assert not at.exception
    names = [seeded_storage.get_item(code)["name"] for code in ("2-1-01-00-000", "2-1-02-00-000", "2-1-03-00-000")]
    assert names == ["Contenedor 1", "Contenedor 2", "Contenedor 3"]
    assert seeded_storage.get_item("2-1-02-00-000")["location"] == "Estantería 2 · Piso 1 · Contenedor 02"
    assert [e.label for e in at.expander] == [":material/warning: Avisos (5)", ":material/lightbulb: Sugerencias (3)"]
    assert not any(b.label.startswith("Revisar y aplicar todas") for b in at.button)


def _rename_meanwhile(storage, item_id, name):
    item = storage.get_item(item_id)
    storage.save_item({**item, "name": name}, item_id, actor_email=PROFESSOR_EMAIL)


def test_a_fix_is_refused_if_the_item_changed_meanwhile(seeded_storage):
    at = _open_reportes()
    _button(at, ":material/build: Corregir a «Contenedor 2»").click().run(timeout=60)
    _rename_meanwhile(seeded_storage, "2-1-02-00-000", "Contendor dos")    # sigue con el error

    _button(at, ":material/check_circle: Confirmar y guardar").click().run(timeout=60)
    assert not at.exception
    assert seeded_storage.get_item("2-1-02-00-000")["name"] == "Contendor dos"
    assert any("cambió después del análisis" in w.value for w in at.warning)


def test_a_pending_fix_is_dropped_if_someone_already_fixed_the_item(seeded_storage):
    at = _open_reportes()
    _button(at, ":material/build: Corregir a «Contenedor 2»").click().run(timeout=60)
    _rename_meanwhile(seeded_storage, "2-1-02-00-000", "Contenedor de bases")

    at.run(timeout=60)
    assert not at.exception
    assert seeded_storage.get_item("2-1-02-00-000")["name"] == "Contenedor de bases"
    assert any("ya no aplica" in w.value for w in at.warning)
    assert at.session_state["dq_pending"] is None


def test_findings_csv_is_readable_and_neutralizes_formulas():
    import io

    import pandas as pd

    from views import reportes

    items = _real_rows() + [_item("LAB-1", name="=HYPERLINK(\"x\")", category="")]
    data = reportes._findings_csv(dq.audit_items(items))
    assert data.startswith("﻿".encode("utf-8"))                     # Excel reconoce UTF-8
    table = pd.read_csv(io.BytesIO(data), encoding="utf-8-sig", dtype=str).fillna("")
    assert "Contendor 2" in set(table["item"])
    assert not any(cell.startswith("=") for cell in table["item"])
    assert "'=HYPERLINK(\"x\")" in set(table["item"])
    assert ACTOR not in data.decode("utf-8-sig")


def test_empty_inventory_shows_an_empty_state():
    at = _open_reportes()
    assert "El inventario está vacío" in _page_html(at)
    assert not at.expander
