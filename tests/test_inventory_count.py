# -*- coding: utf-8 -*-
"""core/inventory_count.py: sesiones de conteo, avance, diferencias, cierre y
ajustes (solo el maestro, sin tocar cantidades por defecto)."""

from datetime import datetime, timedelta, timezone

import pytest

from core import inventory_count as counting
from core import nfc, traceability

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def lab(storage):
    storage.add_item("2-1-01-00-000", name="Electrónica", item_type="master")
    storage.add_item("2-1-01-01-001", name="Arduino UNO", item_type="child", parent_id="2-1-01-00-000", quantity=5)
    storage.add_item("2-1-01-01-002", name="Protoboard", item_type="child", parent_id="2-1-01-00-000", quantity=2)
    storage.add_item("3-2-04-00-000", name="Lego", item_type="master")
    storage.add_item("3-2-04-01-001", name="Pines 4x2", item_type="child", parent_id="3-2-04-00-000", quantity=40)
    storage.add_item("LAB-MIC-01", name="Microscopio", quantity=1)
    storage.add_item("VIEJO-01", name="Dado de baja", status="retired", quantity=1)
    return {
        "storage": storage,
        "student": storage.add_user("ana@uniminuto.edu.co", full_name="Ana", role="estudiante"),
        "prof": storage.add_user("prof@uniminuto.edu.co", full_name="Profe", role="profesor"),
        "master": storage.add_user("maestro@uniminuto.edu.co", full_name="Maestro", role="maestro"),
    }


def _items(storage):
    return [dict(item) for item in storage.items.values()]


def _report(lab, session):
    storage = lab["storage"]
    return counting.progress(session, _items(storage), counting.session_marks(storage, session["id"]),
                             storage.get_availability_map())


# --- Alcance --------------------------------------------------------------------

def test_normalize_scope():
    assert counting.normalize_scope(None) == {"kind": "all"}
    assert counting.normalize_scope({"kind": "shelf", "estanteria": "2"}) == {"kind": "shelf", "estanteria": 2}
    assert counting.normalize_scope({"kind": "container", "id": " 2-1-01-00-000 "})["id"] == "2-1-01-00-000"
    for bad in ({"kind": "shelf"}, {"kind": "shelf", "estanteria": 0}, {"kind": "container"}, {"kind": "otro"}):
        with pytest.raises(ValueError):
            counting.normalize_scope(bad)


def test_expected_items_by_scope(lab):
    items = _items(lab["storage"])
    codes = lambda scope: [i["id"] for i in counting.expected_items(items, scope)]  # noqa: E731
    everything = codes({"kind": "all"})
    assert "2-1-01-00-000" not in everything and "VIEJO-01" not in everything  # sin contenedores ni bajas
    assert everything == ["2-1-01-01-001", "2-1-01-01-002", "3-2-04-01-001", "LAB-MIC-01"]
    assert codes({"kind": "shelf", "estanteria": 2}) == ["2-1-01-01-001", "2-1-01-01-002"]
    assert codes({"kind": "shelf", "estanteria": 3}) == ["3-2-04-01-001"]
    assert codes({"kind": "container", "id": "3-2-04-00-000"}) == ["3-2-04-01-001"]


def test_describe_scope():
    assert counting.describe_scope({"kind": "all"}) == "Todo el inventario"
    assert counting.describe_scope({"kind": "shelf", "estanteria": 2}) == "Estantería 2"
    assert "Lego" in counting.describe_scope({"kind": "container", "id": "3-2-04-00-000", "name": "Lego"})


def test_resolve_code_tolerates_case_and_padding(lab):
    items = _items(lab["storage"])
    assert counting.resolve_code("2-1-01-01-001", items)["name"] == "Arduino UNO"
    assert counting.resolve_code(" 2-1-1-1-1 ", items)["name"] == "Arduino UNO"
    assert counting.resolve_code("lab-mic-01", items)["name"] == "Microscopio"
    assert counting.resolve_code("9-9-99-99-999", items) is None
    assert counting.resolve_code("", items) is None


def test_parse_quantity():
    assert counting.parse_quantity(None) is None and counting.parse_quantity("  ") is None
    assert counting.parse_quantity("7") == 7 and counting.parse_quantity(3.0) == 3
    for bad in ("-1", "2.5", "abc", True):
        with pytest.raises(ValueError):
            counting.parse_quantity(bad)


# --- Sesion ---------------------------------------------------------------------

def test_only_managers_open_a_count_and_only_one_at_a_time(lab):
    storage = lab["storage"]
    ok, message, _ = counting.start_session(storage, lab["student"], "Mi conteo")
    assert not ok and "supervisor" in message
    ok, _, session = counting.start_session(storage, lab["prof"], "  Conteo   de\n octubre ", now=NOW)
    assert ok and session["name"] == "Conteo de octubre" and session["scope"] == {"kind": "all"}
    assert counting.open_session(storage)["id"] == session["id"]
    ok, message, current = counting.start_session(storage, lab["master"], "Otro")
    assert not ok and "ya hay un conteo abierto" in message.lower() and current["id"] == session["id"]


def test_start_session_default_name_and_bad_scope(lab):
    storage = lab["storage"]
    ok, message, _ = counting.start_session(storage, lab["prof"], scope={"kind": "shelf"})
    assert not ok and "estantería" in message.lower()
    ok, _, session = counting.start_session(storage, lab["prof"], "", now=NOW)
    assert ok and session["name"] == "Conteo 09/10/2026"


def test_marks_progress_missing_and_extra(lab):
    storage, prof = lab["storage"], lab["prof"]
    _, _, session = counting.start_session(storage, prof, "Estantería 2", {"kind": "shelf", "estanteria": 2}, now=NOW)
    report = _report(lab, session)
    assert report["total"] == 2 and report["verified_count"] == 0 and report["missing_count"] == 2

    # toque de chip NFC durante el conteo
    outcome = nfc.handle_tap(storage, prof, "2-1-01-01-001", secret="", now=NOW + timedelta(minutes=1))
    assert outcome["target"] == nfc.TARGET_COUNT and outcome["count_session"] == session["id"]
    # codigo escrito con cantidad contada
    ok, message, _ = counting.record_mark(storage, session, prof, "2-1-01-01-002", 2, now=NOW + timedelta(minutes=2))
    assert ok and "2 u. contadas" in message
    # producto fuera del alcance
    counting.record_mark(storage, session, prof, "LAB-MIC-01", None, now=NOW + timedelta(minutes=3))

    report = _report(lab, session)
    assert report["verified_count"] == 2 and report["missing_count"] == 0 and report["ratio"] == 1.0
    assert report["nfc_count"] == 1 and report["counted_count"] == 1
    assert [row["code"] for row in report["extra"]] == ["LAB-MIC-01"]
    first = next(row for row in report["rows"] if row["code"] == "2-1-01-01-001")
    assert first["methods"] == ["nfc"] and first["counted"] is None and first["by"] == "Profe"


def test_difference_uses_available_not_total(lab):
    storage, prof = lab["storage"], lab["prof"]
    storage.create_loan(storage.get_item("2-1-01-01-001"), 2, lab["student"])  # 5 en total, 2 prestadas
    _, _, session = counting.start_session(storage, prof, "Todo", now=NOW)
    counting.record_mark(storage, session, prof, "2-1-01-01-001", 3)   # 3 en el estante = lo esperado
    counting.record_mark(storage, session, prof, "2-1-01-01-002", 1)   # 1 de 2: falta una
    report = _report(lab, session)
    arduino = next(r for r in report["rows"] if r["code"] == "2-1-01-01-001")
    proto = next(r for r in report["rows"] if r["code"] == "2-1-01-01-002")
    assert arduino["available"] == 3 and arduino["on_loan"] == 2 and arduino["difference"] == 0
    assert proto["difference"] == -1
    assert [r["code"] for r in report["differences"]] == ["2-1-01-01-002"]


def test_last_count_wins_and_nfc_plus_quantity_merge(lab):
    storage, prof = lab["storage"], lab["prof"]
    _, _, session = counting.start_session(storage, prof, "Todo", now=NOW)
    nfc.handle_tap(storage, prof, "LAB-MIC-01", secret="", now=NOW + timedelta(minutes=1))
    counting.record_mark(storage, session, prof, "LAB-MIC-01", 5, "nfc", now=NOW + timedelta(minutes=2))
    counting.record_mark(storage, session, prof, "LAB-MIC-01", 1, "nfc", now=NOW + timedelta(minutes=3))
    row = next(r for r in _report(lab, session)["rows"] if r["code"] == "LAB-MIC-01")
    assert row["counted"] == 1 and row["methods"] == ["nfc"]


def test_record_mark_validations(lab):
    storage, prof = lab["storage"], lab["prof"]
    _, _, session = counting.start_session(storage, prof, "Todo", now=NOW)
    assert not counting.record_mark(storage, session, lab["student"], "LAB-MIC-01")[0]
    assert not counting.record_mark(storage, None, prof, "LAB-MIC-01")[0]
    assert not counting.record_mark(storage, session, prof, "  ")[0]
    ok, message, _ = counting.record_mark(storage, session, prof, "LAB-MIC-01", "-3")
    assert not ok and "entero" in message
    assert storage.get_trace_events(event_type=traceability.EVENT_COUNT_MARK) == []


def test_marks_of_other_sessions_do_not_leak(lab):
    storage, prof = lab["storage"], lab["prof"]
    _, _, first = counting.start_session(storage, prof, "Uno", now=NOW)
    counting.record_mark(storage, first, prof, "LAB-MIC-01")
    counting.close_session(storage, first, prof, _report(lab, first), now=NOW + timedelta(minutes=5))
    ok, _, second = counting.start_session(storage, prof, "Dos", now=NOW + timedelta(minutes=10))
    assert ok and counting.session_marks(storage, second["id"]) == []
    assert _report(lab, second)["verified_count"] == 0


# --- Cierre y ajustes -------------------------------------------------------------

def test_close_saves_one_summary_and_does_not_change_quantities(lab):
    storage, prof = lab["storage"], lab["prof"]
    _, _, session = counting.start_session(storage, prof, "Estantería 2", {"kind": "shelf", "estanteria": 2}, now=NOW)
    counting.record_mark(storage, session, prof, "2-1-01-01-001", 4)
    report = _report(lab, session)
    ok, message, event = counting.close_session(
        storage, session, prof, report, notes="  faltó\n una caja ", now=NOW + timedelta(hours=1))
    assert ok and "1 de 2 productos verificados" in message and event["event_type"] == traceability.EVENT_COUNT_CLOSED
    assert counting.open_session(storage) is None
    info = traceability.event_details(event)
    assert info["expected"] == 2 and info["verified"] == 1 and info["missing"] == ["2-1-01-01-002"]
    assert info["differences"][0]["code"] == "2-1-01-01-001" and info["notes"] == "faltó una caja"
    assert storage.items["2-1-01-01-001"]["quantity"] == 5  # sin ajuste automatico
    assert storage.history == []
    # no se puede cerrar dos veces
    assert not counting.close_session(storage, session, prof, report)[0]
    history = counting.closed_sessions(storage)
    assert len(history) == 1 and history[0]["name"] == "Estantería 2" and history[0]["closed_by"] == "Profe"


def test_students_cannot_close(lab):
    storage, prof = lab["storage"], lab["prof"]
    _, _, session = counting.start_session(storage, prof, "Todo", now=NOW)
    assert not counting.close_session(storage, session, lab["student"], _report(lab, session))[0]
    assert counting.open_session(storage) is not None


def test_only_the_master_can_adjust(lab):
    storage = lab["storage"]
    rows = [{"code": "2-1-01-01-001", "counted": 4}]
    applied, errors = counting.apply_adjustments(storage, {"name": "X"}, lab["prof"], rows)
    assert applied == [] and "administrador" in errors[0]
    assert counting.can_adjust(lab["master"]) and not counting.can_adjust(lab["prof"])


class _AdjustStorage:
    """FakeStorage + save_item/get_available_quantity para probar los ajustes."""

    def __init__(self, fake):
        self.fake = fake
        self.saved = []

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def save_item(self, data, custom_id, is_new=False, actor_email="", details=None, actor=None):
        self.saved.append((custom_id, data["quantity"], actor_email, details))
        self.fake.items[custom_id]["quantity"] = data["quantity"]


def test_adjustment_sets_total_to_counted_plus_on_loan(lab):
    fake = lab["storage"]
    fake.create_loan(fake.get_item("2-1-01-01-001"), 2, lab["student"])
    storage = _AdjustStorage(fake)
    rows = [
        {"code": "2-1-01-01-001", "counted": 2},   # 2 en estante + 2 prestadas = 4 (antes 5)
        {"code": "2-1-01-01-002", "counted": 2},   # igual al sistema: no se toca
        {"code": "LAB-MIC-01", "counted": None},   # sin cantidad: error
        {"code": "NO-EXISTE", "counted": 1},
        {"code": "3-2-04-01-001", "counted": "x"},
    ]
    applied, errors = counting.apply_adjustments(storage, {"name": "Conteo"}, lab["master"], rows)
    assert [(a["code"], a["from"], a["to"]) for a in applied] == [("2-1-01-01-001", 5, 4)]
    assert storage.saved[0][0] == "2-1-01-01-001" and storage.saved[0][1] == 4
    assert "Conteo" in storage.saved[0][3] and "2 en préstamo" in storage.saved[0][3]
    assert len(errors) == 3
    assert fake.items["2-1-01-01-002"]["quantity"] == 2


def test_close_records_the_adjustments(lab):
    storage, master = lab["storage"], lab["master"]
    _, _, session = counting.start_session(storage, master, "Todo", now=NOW)
    counting.record_mark(storage, session, master, "LAB-MIC-01", 0)
    adjusted = [{"code": "LAB-MIC-01", "from": 1, "to": 0}]
    ok, message, event = counting.close_session(storage, session, master, _report(lab, session), adjusted)
    assert ok and "1 cantidad(es) ajustada(s)" in message
    assert traceability.event_details(event)["adjusted"] == adjusted
