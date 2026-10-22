# -*- coding: utf-8 -*-
"""Ubicaciones con codigo (estanterias, pisos, mesas y zonas): reglas de los
codigos y su no colision con contenedores/productos, alta en bloque con
LabStorage real, exclusion de prestamos/solicitudes/reportes, arbol, contenido,
etiquetas y auditoria de calidad."""

import io

import pytest
from PIL import Image

from core import barcode, data_quality, labels, loans, places, reports, service_requests
from core.storage import LabStorage

ACTOR = "prof@uniminuto.edu.co"


# --- codigos -------------------------------------------------------------------------

@pytest.mark.parametrize("code, kind", [
    ("2-0-00-00-000", "estanteria"),
    ("1-0-00-00-000", "estanteria"),
    ("2-1-00-00-000", "piso"),
    ("3-6-00-00-000", "piso"),
    ("M1-E0", "mesa"),
    ("M2-E0", "mesa"),
    ("E3-LM00", "zona"),
    ("2-0-0-0-0", "estanteria"),            # ceros de relleno omitidos
])
def test_structured_location_codes_are_recognised(code, kind):
    assert barcode.location_code(code)["kind"] == kind
    assert barcode.validate_location_code(code)["format"] == barcode.FORMAT_LOCATION


def test_builders_produce_canonical_codes():
    assert barcode.build_shelf_code(2) == "2-0-00-00-000"
    assert barcode.build_floor_code(2, 1) == "2-1-00-00-000"
    assert barcode.build_table_code(1) == "M1-E0"
    assert barcode.LEGO_ZONE_CODE == "E3-LM00"
    with pytest.raises(ValueError, match="Estanteria"):
        barcode.build_shelf_code(4)
    with pytest.raises(ValueError, match="Piso"):
        barcode.build_floor_code(2, 7)
    with pytest.raises(ValueError, match="Mesa"):
        barcode.build_table_code(3)


@pytest.mark.parametrize("code", ["2-0-00-00-000", "2-1-00-00-000", "M1-E0", "E3-LM00"])
def test_location_codes_never_collide_with_inventory_codes(code):
    """parse_code (items normales) los rechaza: un contenedor, caja o producto
    nunca puede tomar el codigo de una ubicacion."""
    assert barcode.detect_format(code) is None
    assert not barcode.is_valid_code(code)
    with pytest.raises(ValueError):
        barcode.validate_code_format(code)


@pytest.mark.parametrize("code", [
    "2-1-01-00-000", "2-1-01-01-000", "2-1-01-01-001", "M1-E2", "E3-LM07",
])
def test_inventory_codes_are_not_location_codes(code):
    assert barcode.location_code(code) is None
    with pytest.raises(ValueError, match="no puede usarse para una ubicación"):
        barcode.validate_location_code(code)


@pytest.mark.parametrize("code", ["4-0-00-00-000", "2-7-00-00-000", "2-0-01-00-000", "2-0-00-01-000",
                                  "M3-E0", "m1-e0", "LAB MIC", "", "ABCDEFGHIJKLMN"])
def test_invalid_location_codes_are_rejected(code):
    with pytest.raises(ValueError):
        barcode.validate_location_code(code)


@pytest.mark.parametrize("code", ["SALA-A", "E2-P1-IZQ", "0012345"])
def test_free_codes_are_valid_for_zones(code):
    assert barcode.validate_location_code(code)["kind"] == "zona"
    assert barcode.location_code(code) is None      # solo los estructurados se reconocen solos


def test_existing_code_rules_are_untouched():
    for code in ("2-1-00-00-000", "2-0-00-00-000", "M1-E0", "E3-LM00"):
        with pytest.raises(ValueError):
            barcode.parse_code(code)
    assert barcode.parse_code("2-1-01-00-000")["format"] == barcode.FORMAT_STANDARD
    assert barcode.parse_code("M1-E2")["format"] == barcode.FORMAT_MESA


def test_describe_parsed_and_helpers():
    assert barcode.describe_parsed(barcode.location_code("2-1-00-00-000")) == "Estantería 2 · Piso 1"
    assert barcode.describe_location(barcode.location_code("M2-E0")) == "Mesa de trabajo 2"
    assert barcode.location_kind("SALA-A", True) == "zona"
    assert barcode.location_kind("SALA-A") == ""


@pytest.mark.parametrize("child, parent, ok", [
    ("2-1-00-00-000", "2-0-00-00-000", True),
    ("2-1-00-00-000", "3-0-00-00-000", False),
    ("E3-LM00", "3-0-00-00-000", True),
    ("E3-LM00", "2-0-00-00-000", False),
    ("M1-E0", "2-0-00-00-000", False),
    ("M1-E0", "SALA-A", True),
    ("SALA-A", "2-1-00-00-000", True),
])
def test_parent_compatibility(child, parent, ok):
    assert (barcode.location_parent_problem(child, parent) == "") is ok


# --- guardado -----------------------------------------------------------------------------

@pytest.fixture
def db():
    return LabStorage()


def _save_place(db, code, name="Lugar", parent="", **extra):
    db.save_item({"name": name, "item_type": "location", "parent_id": parent, **extra}, code,
                 is_new=True, actor_email=ACTOR)


def test_save_item_accepts_location_codes_only_for_location_items(db):
    _save_place(db, "2-0-00-00-000", "Estantería 2")
    assert db.get_item("2-0-00-00-000")["item_type"] == "location"
    with pytest.raises(ValueError):
        db.save_item({"name": "Intruso", "item_type": "standalone", "quantity": 1}, "2-1-00-00-000",
                     is_new=True, actor_email=ACTOR)
    with pytest.raises(ValueError, match="no puede usarse para una ubicación"):
        _save_place(db, "2-1-01-00-000", "Contenedor disfrazado")
    with pytest.raises(ValueError, match="Ya existe"):
        _save_place(db, "2-0-00-00-000", "Repetida")


def test_location_has_no_stock_even_if_quantity_is_given(db):
    _save_place(db, "M1-E0", "Mesa de trabajo 1", quantity=5, min_stock_alert=3)
    item = db.get_item("M1-E0")
    assert item["quantity"] == 0 and item["min_stock_alert"] == 0
    assert db.get_available_quantity("M1-E0") == 0


def test_floor_must_be_inside_its_own_shelf(db):
    _save_place(db, "3-0-00-00-000", "Estantería 3")
    with pytest.raises(ValueError, match="va dentro de la Estantería 2"):
        _save_place(db, "2-1-00-00-000", "Piso", parent="3-0-00-00-000")
    with pytest.raises(ValueError, match="no existe"):
        _save_place(db, "2-1-00-00-000", "Piso", parent="2-0-00-00-000")
    db.save_item({"name": "Caja", "item_type": "master"}, "2-1-01-00-000", is_new=True, actor_email=ACTOR)
    with pytest.raises(ValueError, match="no es una ubicacion"):
        _save_place(db, "2-1-00-00-000", "Piso", parent="2-1-01-00-000")


def test_a_product_cannot_hang_from_a_location(db):
    _save_place(db, "2-0-00-00-000", "Estantería 2")
    with pytest.raises(ValueError):
        db.save_item({"name": "Hijo", "item_type": "child", "parent_id": "2-0-00-00-000"}, "2-1-01-01-000",
                     is_new=True, actor_email=ACTOR)


def test_editing_a_location_keeps_it_without_stock(db):
    _save_place(db, "2-0-00-00-000", "Estantería 2")
    item = db.get_item("2-0-00-00-000")
    db.save_item({**item, "name": "Estantería 2 (norte)", "quantity": 9}, "2-0-00-00-000", is_new=False,
                 actor_email=ACTOR)
    after = db.get_item("2-0-00-00-000")
    assert after["name"] == "Estantería 2 (norte)" and after["quantity"] == 0


def test_codes_survive_the_excel_round_trip(db):
    from core import storage as storage_module

    plan = places.plan_shelf(2, 2, [])
    db.save_items_bulk(plan["payloads"], actor_email=ACTOR)
    storage_module._cached_dfs = None
    assert db.get_item("2-1-00-00-000")["parent_id"] == "2-0-00-00-000"
    assert db.get_item("2-0-00-00-000")["item_type"] == "location"


# --- alta en bloque -------------------------------------------------------------------------

def test_plan_shelf_creates_the_shelf_and_its_floors():
    plan = places.plan_shelf(2, 4, [])
    assert [r["code"] for r in plan["rows"]] == [
        "2-0-00-00-000", "2-1-00-00-000", "2-2-00-00-000", "2-3-00-00-000", "2-4-00-00-000"]
    assert [p["name"] for p in plan["payloads"]][:2] == ["Estantería 2", "Estantería 2 · Piso 1"]
    assert plan["payloads"][1]["parent_id"] == "2-0-00-00-000"
    assert all(p["item_type"] == "location" and p["quantity"] == 0 for p in plan["payloads"])
    assert not plan["errors"]


def test_shelf_with_floors_is_one_single_write(db, monkeypatch):
    from core import storage as storage_module

    writes = []
    original = storage_module._write_and_sync
    monkeypatch.setattr(storage_module, "_write_and_sync", lambda dfs: (writes.append(1), original(dfs)))
    plan = places.plan_shelf(2, 4, db.get_all_items(include_retired=True))
    created = db.save_items_bulk(plan["payloads"], actor_email=ACTOR, details=places.HISTORY_DETAILS)
    assert len(created) == 5 and len(writes) == 1
    assert db.get_item("2-3-00-00-000")["parent_id"] == "2-0-00-00-000"
    assert db.get_item_history("2-1-00-00-000")[0]["type"] == "Alta"


def test_plan_only_adds_what_is_missing():
    existing = places.plan_shelf(2, 2, [])["payloads"]
    plan = places.plan_shelf(2, 4, existing)
    assert [r["status"] for r in plan["rows"]] == ["Ya registrada"] * 3 + ["Nueva"] * 2
    assert [p["id"] for p in plan["payloads"]] == ["2-3-00-00-000", "2-4-00-00-000"]
    assert places.plan_shelf(2, 2, existing)["payloads"] == []


def test_plan_shelf_validates_inputs_and_collisions():
    assert places.plan_shelf(4, 2, [])["errors"] and places.plan_shelf(4, 2, [])["payloads"] == []
    assert "pisos" in places.plan_shelf(2, 9, [])["errors"][0]
    clash = [{"id": "2-0-00-00-000", "name": "Raro", "item_type": "standalone", "status": "active"}]
    assert "no es una ubicación" in places.plan_shelf(2, 1, clash)["errors"][0]


def test_plan_location_for_table_lego_and_zone():
    assert places.plan_location("M1-E0", "", [])["payloads"][0]["name"] == "Mesa de trabajo 1"
    assert places.plan_location("E3-LM00", "", [])["payloads"][0]["name"] == "Exhibición Lego"
    assert "nombre" in places.plan_location("SALA-A", "", [])["errors"][0]
    zone = places.plan_location("SALA-A", "Sala A", [])
    assert zone["payloads"][0]["id"] == "SALA-A" and not zone["errors"]
    assert places.plan_location("2-1-01-00-000", "X", [])["errors"]
    assert places.plan_location("", "X", [])["errors"]


def test_plan_location_checks_the_parent():
    shelf = places.plan_shelf(3, 1, [])["payloads"]
    ok = places.plan_location("E3-LM00", "", shelf)
    assert ok["payloads"][0]["parent_id"] == "3-0-00-00-000" and not ok["errors"]
    assert places.plan_location("M1-E0", "", shelf, parent_id="3-0-00-00-000")["errors"]
    assert places.plan_location("SALA-A", "Sala", shelf, parent_id="9-0-00-00-000")["errors"]


# --- fuera de prestamos, solicitudes, reportes ---------------------------------------------------

def test_locations_cannot_be_loaned_or_requested(db):
    _save_place(db, "2-0-00-00-000", "Estantería 2")
    user = {"id": "u1", "full_name": "Ana", "role": "estudiante", "institutional_email": "ana@uniminuto.edu.co"}
    ok, message, loan = loans.checkout(db, "2-0-00-00-000", 1, user)
    assert not ok and loan is None and "ubicación" in message
    with pytest.raises(ValueError, match="no una ubicación"):
        service_requests.validate_request(db, "product", "2-0-00-00-000", 1)


def test_locations_are_left_out_of_availability_alerts_and_reports(db):
    _save_place(db, "2-0-00-00-000", "Estantería 2")
    db.save_item({"name": "Multímetro", "item_type": "standalone", "quantity": 2, "category": "Medición"},
                 "LAB-MUL-01", is_new=True, actor_email=ACTOR)
    summary = reports.items_by_category(db.get_all_items(include_retired=True))
    assert summary["Items"].sum() == 1


def test_deleting_a_shelf_removes_its_floors_but_not_the_products(db):
    plan = places.plan_shelf(2, 2, [])
    db.save_items_bulk(plan["payloads"], actor_email=ACTOR)
    db.save_item({"name": "Caja", "item_type": "master"}, "2-1-01-00-000", is_new=True, actor_email=ACTOR)
    impact = db.get_delete_impact("2-0-00-00-000")
    assert impact["item_type"] == "location" and impact["contained_items"] == 2
    ok, _ = db.delete_item("2-0-00-00-000", actor_email=ACTOR)
    assert ok
    assert db.get_item("2-1-00-00-000") is None and db.get_item("2-2-00-00-000") is None
    assert db.get_item("2-1-01-00-000") is not None


# --- arbol y contenido -------------------------------------------------------------------------------

INVENTORY = [
    {"id": "2-1-01-00-000", "name": "Electrónica", "item_type": "master", "status": "active", "location": ""},
    {"id": "2-1-01-01-000", "name": "Resistencias", "item_type": "child", "parent_id": "2-1-01-00-000",
     "status": "active", "quantity": 20},
    {"id": "2-2-01-00-000", "name": "Mecánica", "item_type": "master", "status": "active"},
    {"id": "3-1-01-00-000", "name": "Lego", "item_type": "master", "status": "active"},
    {"id": "LAB-MIC-01", "name": "Microscopio", "item_type": "standalone", "status": "active",
     "location": "Estantería 2 · Piso 1", "quantity": 1},
    {"id": "M1-E2", "name": "Cortadora", "item_type": "standalone", "status": "active", "quantity": 1},
    {"id": "E3-LM07", "name": "Grúa", "item_type": "standalone", "status": "active", "quantity": 1},
]


def _items_with_places():
    return INVENTORY + places.plan_shelf(2, 3, [])["payloads"] + places.plan_shelf(3, 1, [])["payloads"] + [
        *places.plan_location("M1-E0", "", [])["payloads"],
        *places.plan_location("E3-LM00", "", places.plan_shelf(3, 0, [])["payloads"])["payloads"],
    ]


def _ids(rows):
    return [row["id"] for row in rows]


def test_tree_nests_floors_under_their_shelf():
    tree = places.location_tree(_items_with_places())
    assert [n["item"]["id"] for n in tree] == ["2-0-00-00-000", "3-0-00-00-000", "M1-E0"]
    assert [c["item"]["id"] for c in tree[0]["children"]] == ["2-1-00-00-000", "2-2-00-00-000", "2-3-00-00-000"]
    flat = places.flatten(tree)
    assert [n["depth"] for n in flat][:4] == [0, 1, 1, 1]


def test_a_floor_registered_before_its_shelf_still_hangs_from_it():
    items = [{"id": "2-1-00-00-000", "name": "Piso", "item_type": "location", "status": "active"},
             {"id": "2-0-00-00-000", "name": "Estantería", "item_type": "location", "status": "active"}]
    tree = places.location_tree(items)
    assert len(tree) == 1 and tree[0]["children"][0]["item"]["id"] == "2-1-00-00-000"


def test_contents_by_code_hierarchy_and_written_location():
    items = _items_with_places()
    floor = places.contents_of("2-1-00-00-000", items)
    assert _ids(floor["containers"]) == ["2-1-01-00-000"]
    assert _ids(floor["products"]) == ["2-1-01-01-000", "LAB-MIC-01"]     # el libre entra por su ubicacion escrita
    assert _ids(floor["loose"]) == ["LAB-MIC-01"]
    shelf = places.contents_of("2-0-00-00-000", items)
    assert _ids(shelf["containers"]) == ["2-1-01-00-000", "2-2-01-00-000"]
    assert _ids(shelf["sublocations"]) == ["2-1-00-00-000", "2-2-00-00-000", "2-3-00-00-000"]
    assert "3-1-01-00-000" not in _ids(shelf["containers"])


def test_contents_of_tables_and_lego_and_unregistered_codes():
    items = _items_with_places()
    assert _ids(places.contents_of("M1-E0", items)["products"]) == ["M1-E2"]
    assert _ids(places.contents_of("E3-LM00", items)["products"]) == ["E3-LM07"]
    unregistered = places.contents_of("2-2-00-00-000", INVENTORY)       # sin registrar: igual muestra lo que guarda
    assert _ids(unregistered["containers"]) == ["2-2-01-00-000"]


def test_free_zone_collects_items_by_written_location():
    items = INVENTORY + [
        {"id": "SALA-A", "name": "Sala de electrónica", "item_type": "location", "status": "active"},
        {"id": "LAB-OSC-01", "name": "Osciloscopio", "item_type": "standalone", "status": "active",
         "location": "Sala de electrónica; Armario norte", "quantity": 1},
    ]
    assert _ids(places.contents_of("SALA-A", items)["products"]) == ["LAB-OSC-01"]


def test_retired_items_and_locations_are_ignored():
    items = [{**i, "status": "retired"} if i["id"] == "2-1-01-00-000" else i for i in _items_with_places()]
    assert "2-1-01-00-000" not in _ids(places.contents_of("2-1-00-00-000", items)["containers"])


# --- escaneo ---------------------------------------------------------------------------------------

def test_scanning_a_registered_location_and_an_unregistered_one(db):
    db.save_items_bulk(places.plan_shelf(2, 1, [])["payloads"], actor_email=ACTOR)
    found = barcode.scan(db, "2-1-00-00-000")
    assert found["status"] == "found_location" and found["place"]["kind"] == "piso"
    assert found["item"]["name"] == "Estantería 2 · Piso 1"
    missing = barcode.scan(db, "2-2-00-00-000")
    assert missing["status"] == "not_found" and missing["place"]["piso"] == 2
    assert "place" not in barcode.scan(db, "LAB-NADA-01")


def test_a_retired_location_scans_as_free(db):
    db.save_items_bulk(places.plan_shelf(2, 0, [])["payloads"], actor_email=ACTOR)
    db.retire_item("2-0-00-00-000", actor_email=ACTOR)
    assert barcode.scan(db, "2-0-00-00-000")["retired"] is True


# --- etiquetas ---------------------------------------------------------------------------------------

FLOOR = {"id": "2-1-00-00-000", "name": "Estantería 2 · Piso 1", "item_type": "location"}


def _decode(image):
    from test_labels import _scan_label

    return _scan_label(image)["text"]


@pytest.mark.parametrize("code, route", [
    ("2-0-00-00-000", "RUTA: E2"),
    ("2-1-00-00-000", "RUTA: E2 › P1"),
    ("M1-E0", "RUTA: MESA 1"),
    ("E3-LM00", "RUTA: E3 › LEGO"),
])
def test_location_label_route_line(code, route):
    assert labels._location_guide(code) == route


def test_location_label_shows_type_header_and_scans_back():
    layout = labels.layout_item_label(FLOOR)
    assert [line.text for line in layout.lines_for("type")] == ["Ubicación · Piso"]
    assert [line.text for line in layout.lines_for("notice")] == [labels.LOCATION_NOTICE_TEXT]
    assert [line.text for line in layout.lines_for("code")] == ["2-1-00-00-000"]
    assert layout.shortened == () and layout.omitted == ()
    png = labels.generate_item_label_png_bytes(FLOOR)
    assert _decode(Image.open(io.BytesIO(png))) == "2-1-00-00-000"


def test_location_labels_for_free_codes_and_pdf():
    zone = {"id": "SALA-A", "name": "Sala A", "item_type": "location"}
    assert [line.text for line in labels.layout_item_label(zone).lines_for("type")] == ["Ubicación · Zona"]
    assert labels.generate_item_label_pdf_bytes(FLOOR).startswith(b"%PDF-1.7")
    pdf, png = labels.item_label_files(FLOOR)
    assert pdf and png


def test_invalid_codes_are_still_rejected_by_labels():
    for code in ("2-7-00-00-000", "2-0-01-00-000", "M3-E0", "LAB MIC"):
        with pytest.raises(ValueError):
            labels.generate_label_image(code)
    assert labels.generate_item_label_pdf_bytes({"id": "2-7-00-00-000", "item_type": "location"}) is None


def test_classic_labels_do_not_change_for_inventory_codes():
    layout = labels.layout_item_label({"id": "2-1-01-00-000", "name": "Contenedor 1", "item_type": "master"})
    assert [line.text for line in layout.lines_for("type")] == ["Contenedor Principal"]
    assert [line.text for line in layout.lines_for("notice")] == [labels.LABEL_NOTICE_TEXT]


def test_multi_label_pdf_has_one_page_per_location():
    import pypdfium2

    items = places.plan_shelf(2, 2, [])["payloads"]
    pdf = labels.generate_labels_pdf_bytes(items + [{"id": "CODIGO MALO", "name": "x"}])
    document = pypdfium2.PdfDocument(pdf)
    assert len(document) == 3
    spec = labels.LabelSpec()
    assert document[0].get_size()[0] == pytest.approx(spec.width_mm * 72 / 25.4, abs=0.01)
    assert labels.generate_labels_pdf_bytes([]) is None


def test_single_label_pdf_is_unchanged_by_the_multipage_refactor():
    image = labels.generate_label_image("2-1-01-00-000", description="Contenedor 1")
    single = labels._label_image_to_pdf(image, labels.LabelSpec(), title="Etiqueta 2-1-01-00-000")
    multi = labels._labels_to_pdf([image], labels.LabelSpec(), "Etiqueta 2-1-01-00-000")
    assert single.count(b"/Type /Page ") == multi.count(b"/Type /Page ") == 1
    assert len(single) == len(multi)


# --- auditoria de calidad ------------------------------------------------------------------------------------

def _rules(items):
    return {(f.rule, f.item_id) for f in data_quality.audit_items(items)}


def test_data_quality_does_not_flag_healthy_locations():
    items = places.plan_shelf(2, 3, [])["payloads"] + places.plan_location("M1-E0", "", [])["payloads"]
    items += places.plan_location("SALA-A", "Sala A", [])["payloads"]
    assert data_quality.audit_items(items) == []


def test_locations_do_not_count_as_lendable_or_trigger_product_rules():
    only_places = places.plan_shelf(2, 1, [])["payloads"]
    assert not any(f.rule == "nothing_lendable" for f in data_quality.audit_items(only_places))
    flagged = {f.item_id for f in data_quality.audit_items(_items_with_places())
               if f.rule in {"zero_quantity", "missing_category", "location_unknown", "location_missing", "code_level"}}
    assert not {i["id"] for i in _items_with_places() if i["item_type"] == "location"} & flagged


def test_data_quality_flags_real_location_problems():
    items = [
        {"id": "2-0-00-00-000", "name": "Estantería 3", "item_type": "location", "status": "active"},
        {"id": "2-1-00-00-000", "name": "Piso 1", "item_type": "location", "status": "active",
         "parent_id": "3-0-00-00-000"},
        {"id": "3-0-00-00-000", "name": "Estantería 3", "item_type": "location", "status": "active"},
        {"id": "2-2-00-00-000", "name": "Piso 2", "item_type": "location", "status": "active", "quantity": 4},
        {"id": "2-3-00-00-000", "name": "Piso 3", "item_type": "location", "status": "active",
         "parent_id": "2-9-01-00-000"},
    ]
    found = _rules(items)
    assert ("name_code_mismatch", "2-0-00-00-000") in found
    assert ("location_parent", "2-1-00-00-000") in found
    assert ("location_quantity", "2-2-00-00-000") in found
    assert ("missing_parent", "2-3-00-00-000") in found


def test_a_location_with_a_product_code_is_an_error():
    found = _rules([{"id": "2-1-01-00-000", "name": "Raro", "item_type": "location", "status": "active"}])
    assert ("code_format", "2-1-01-00-000") in found
