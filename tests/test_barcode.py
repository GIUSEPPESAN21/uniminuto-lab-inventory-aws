# -*- coding: utf-8 -*-
from core import barcode


def test_scan_not_found(storage):
    result = barcode.scan(storage, "NO-EXISTE")
    assert result["status"] == "not_found"
    assert result["barcode"] == "NO-EXISTE"


def test_scan_empty_code_returns_error(storage):
    result = barcode.scan(storage, "   ")
    assert result["status"] == "error"


def test_scan_master_returns_children_with_availability(storage):
    storage.add_item("CAJA-001", name="Caja de resistencias", item_type="master")
    storage.add_item("RES-01", name="Resistencia 220", item_type="child", parent_id="CAJA-001", quantity=10)

    result = barcode.scan(storage, "CAJA-001")

    assert result["status"] == "found_master"
    assert len(result["children"]) == 1
    assert result["children"][0]["available"] == 10


def test_scan_child_includes_parent_breadcrumb(storage):
    storage.add_item("CAJA-001", name="Caja de resistencias", item_type="master")
    storage.add_item("RES-01", name="Resistencia 220", item_type="child", parent_id="CAJA-001", quantity=10)

    result = barcode.scan(storage, "RES-01")

    assert result["status"] == "found_item"
    assert result["parent"]["id"] == "CAJA-001"
    assert result["item"]["available"] == 10


def test_scan_standalone_item_has_no_parent(storage):
    storage.add_item("MULT-01", name="Multimetro", item_type="standalone", quantity=4)

    result = barcode.scan(storage, "MULT-01")

    assert result["status"] == "found_item"
    assert result["parent"] is None


def test_scan_retired_item_returns_error(storage):
    storage.add_item("MULT-01", name="Multimetro", status="retired")
    result = barcode.scan(storage, "MULT-01")
    assert result["status"] == "error"


def test_scan_retired_item_marks_the_code_as_reusable(storage):
    storage.add_item("MULT-01", name="Multimetro", status="retired")
    result = barcode.scan(storage, "MULT-01")
    assert result["retired"] is True and result["barcode"] == "MULT-01"


def test_scan_of_a_permanently_deleted_code_offers_registration(storage):
    result = barcode.scan(storage, "2-1-01-00-000")      # no existe: se elimino
    assert result["status"] == "not_found" and result["barcode"] == "2-1-01-00-000"
