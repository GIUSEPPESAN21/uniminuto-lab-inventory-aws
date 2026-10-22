# -*- coding: utf-8 -*-
"""core/nfc.py: URL de los chips, firma, lectura del toque, ruta verificable por
NFC, registro de chips y decision de que hacer con cada toque."""

from datetime import datetime, timedelta, timezone

import pytest

from core import inventory_count, nfc, traceability

BASE = "https://lab-uniminuto.streamlit.app"
SECRET = "clave-de-prueba"
CODE = "2-1-01-01-001"
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


# --- URL, firma y lectura --------------------------------------------------------

def test_build_tag_url_without_secret_has_no_signature():
    assert nfc.build_tag_url(CODE, BASE) == f"{BASE}/escanear?nfc={CODE}"


def test_build_tag_url_with_secret_adds_a_short_signature():
    url = nfc.build_tag_url(CODE, BASE + "/", SECRET)
    assert url.startswith(f"{BASE}/escanear?nfc={CODE}&s=")
    signature = url.rsplit("s=", 1)[1]
    assert len(signature) == nfc.SIGNATURE_LENGTH
    assert all(ch in traceability.RECEIPT_ALPHABET for ch in signature)


def test_build_tag_url_encodes_the_code():
    url = nfc.build_tag_url("LAB_MIC.01", BASE)
    assert url.endswith("nfc=LAB_MIC.01")
    assert "%" not in url
    assert nfc.parse_tap({"nfc": "LAB MIC"})["error"]  # los espacios no son un codigo


@pytest.mark.parametrize("bad", ["", "   ", "4-2-05-12-001", "M3-E1", "con espacio", "ñandú"])
def test_build_tag_url_rejects_invalid_codes(bad):
    with pytest.raises(ValueError):
        nfc.build_tag_url(bad, BASE)


@pytest.mark.parametrize("base", ["", "lab.streamlit.app", "ftp://x.y", "https://"])
def test_build_tag_url_requires_a_public_base(base):
    with pytest.raises(ValueError, match="APP_URL"):
        nfc.build_tag_url(CODE, base)


def test_signature_is_deterministic_and_depends_on_code_and_secret():
    assert nfc.sign_code(CODE, SECRET) == nfc.sign_code(CODE, SECRET)
    assert nfc.sign_code(CODE, SECRET) != nfc.sign_code("2-1-01-01-002", SECRET)
    assert nfc.sign_code(CODE, SECRET) != nfc.sign_code(CODE, "otra-clave")
    assert nfc.sign_code(CODE, "") == ""


def test_check_signature_states():
    good = nfc.sign_code(CODE, SECRET)
    assert nfc.check_signature(CODE, "", "") == nfc.SIG_UNSIGNED
    assert nfc.check_signature(CODE, "lo-que-sea", "") == nfc.SIG_UNSIGNED
    assert nfc.check_signature(CODE, good, SECRET) == nfc.SIG_VALID
    assert nfc.check_signature(CODE, good.lower(), SECRET) == nfc.SIG_VALID
    assert nfc.check_signature(CODE, "", SECRET) == nfc.SIG_MISSING
    assert nfc.check_signature(CODE, "AAAAAAAAAA", SECRET) == nfc.SIG_INVALID
    assert nfc.check_signature("2-1-01-01-002", good, SECRET) == nfc.SIG_INVALID
    assert nfc.signature_accepted(nfc.SIG_UNSIGNED) and nfc.signature_accepted(nfc.SIG_VALID)
    assert not nfc.signature_accepted(nfc.SIG_MISSING) and not nfc.signature_accepted(nfc.SIG_INVALID)


def test_parse_tap():
    assert nfc.parse_tap({}) is None
    assert nfc.parse_tap({"otro": "1"}) is None
    assert nfc.parse_tap({"nfc": CODE, "s": "ABC"}) == {"code": CODE, "signature": "ABC", "error": ""}
    assert nfc.parse_tap({"nfc": [CODE, "otro"]})["code"] == "otro"  # repetido: gana el ultimo
    assert nfc.parse_tap({"nfc": "  "})["error"]
    assert nfc.parse_tap({"nfc": "x" * 200}) == {"code": "", "signature": "", "error": nfc.parse_tap({"nfc": "x" * 200})["error"]}
    assert nfc.parse_tap({"nfc": "x" * 200})["error"]


def test_app_base_url_prefers_app_url_secret(monkeypatch):
    assert nfc.app_base_url("https://x.streamlit.app/escanear?a=1") == "https://x.streamlit.app"
    assert nfc.app_base_url("") == ""
    assert nfc.app_base_url(None) == ""
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: {"APP_URL": "https://lab.edu.co/app/"}.get(key, default))
    assert nfc.app_base_url("https://x.streamlit.app/") == "https://lab.edu.co/app"
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: {"APP_URL": "no-es-url"}.get(key, default))
    assert nfc.app_base_url("https://x.streamlit.app/") == "https://x.streamlit.app"


def test_nfc_secret_reads_the_secret(monkeypatch):
    assert nfc.nfc_secret() == ""
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: {"NFC_SECRET": " abc "}.get(key, default))
    assert nfc.nfc_secret() == "abc"


def test_ndef_size_and_chip_fit():
    url = nfc.build_tag_url(CODE, BASE, SECRET)
    # https:// se abrevia a 1 byte: 4 de cabecera + 1 de prefijo + resto de la URL
    assert nfc.ndef_size(url) == 4 + 1 + len(url) - len("https://")
    assert nfc.chips_that_fit(url) == ["NTAG213", "NTAG215", "NTAG216"]
    assert nfc.chips_that_fit("https://x.y/" + "a" * 200) == ["NTAG215", "NTAG216"]
    assert nfc.chips_that_fit("https://x.y/" + "a" * 600) == ["NTAG216"]


# --- Toques ----------------------------------------------------------------------

def _lab(storage):
    storage.add_item("2-1-01-00-000", name="Electrónica", item_type="master")
    storage.add_item(CODE, name="Arduino UNO", item_type="child", parent_id="2-1-01-00-000", quantity=3)
    storage.add_item("2-1-01-01-002", name="Protoboard", item_type="child", parent_id="2-1-01-00-000", quantity=2)
    student = storage.add_user("ana@uniminuto.edu.co", full_name="Ana", role="estudiante")
    prof = storage.add_user("prof@uniminuto.edu.co", full_name="Profe", role="profesor")
    return student, prof


def _approved_request(storage, student, item_id=CODE):
    request = storage.create_service_request(
        {"request_type": "product", "item_id": item_id, "item_name": "Arduino UNO", "quantity": 1,
         "created_at": NOW.isoformat()}, student)
    storage.update_service_request_status(request["id"], "approved", "prof@uniminuto.edu.co")
    return storage.get_service_request(request["id"])


def test_handle_tap_records_one_event_for_a_known_item(storage):
    student, _ = _lab(storage)
    outcome = nfc.handle_tap(storage, student, CODE, secret="", now=NOW)
    assert outcome["ok"] and outcome["target"] == nfc.TARGET_SCAN
    assert outcome["item"]["name"] == "Arduino UNO"
    events = storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)
    assert len(events) == 1
    assert events[0]["item_id"] == CODE and events[0]["user_id"] == student["id"]
    assert traceability.event_details(events[0])["signed"] is False


def test_handle_tap_same_chip_twice_counts_once(storage):
    student, _ = _lab(storage)
    nfc.handle_tap(storage, student, CODE, secret="", now=NOW)
    again = nfc.handle_tap(storage, student, CODE, secret="", now=NOW + timedelta(seconds=20))
    assert again["ok"] and again["duplicate"]
    assert len(storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)) == 1
    later = nfc.handle_tap(storage, student, CODE, secret="", now=NOW + timedelta(seconds=nfc.DEDUP_SECONDS + 1))
    assert not later["duplicate"]
    assert len(storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)) == 2


def test_handle_tap_requires_a_valid_signature_when_a_secret_is_set(storage):
    student, _ = _lab(storage)
    missing = nfc.handle_tap(storage, student, CODE, signature="", secret=SECRET, now=NOW)
    forged = nfc.handle_tap(storage, student, CODE, signature="AAAAAAAAAA", secret=SECRET, now=NOW)
    assert not missing["ok"] and missing["signature"] == nfc.SIG_MISSING
    assert not forged["ok"] and forged["signature"] == nfc.SIG_INVALID
    assert storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP) == []
    good = nfc.handle_tap(storage, student, CODE, signature=nfc.sign_code(CODE, SECRET), secret=SECRET, now=NOW)
    assert good["ok"] and good["signature"] == nfc.SIG_VALID
    event = storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)[0]
    assert traceability.event_details(event)["signed"] is True


def test_handle_tap_unknown_or_empty_code_records_nothing(storage):
    student, _ = _lab(storage)
    assert not nfc.handle_tap(storage, student, "", secret="")["ok"]
    unknown = nfc.handle_tap(storage, student, "9-9-99-99-999", secret="")
    assert not unknown["ok"] and "no está registrado" in unknown["message"]
    storage.add_item("RETIRADO-01", name="Viejo", status="retired")
    assert not nfc.handle_tap(storage, student, "RETIRADO-01", secret="")["ok"]
    assert storage.get_trace_events() == []


def test_open_route_requests_excludes_picked_up_and_verified(storage):
    student, _ = _lab(storage)
    request = _approved_request(storage, student)
    assert [r["id"] for r in nfc.open_route_requests(storage, student)] == [request["id"]]
    storage.add_trace_event({"event_type": traceability.EVENT_ROUTE_VERIFIED, "request_id": request["id"],
                             "user_id": student["id"]})
    assert nfc.open_route_requests(storage, student) == []


def test_tap_on_a_label_of_an_open_route_targets_trazabilidad(storage):
    student, _ = _lab(storage)
    request = _approved_request(storage, student)
    container = nfc.handle_tap(storage, student, "2-1-01-00-000", secret="", now=NOW)
    assert container["target"] == nfc.TARGET_ROUTE and container["request_id"] == request["id"]
    assert "ruta" in container["message"]


def test_tap_outside_the_route_explains_where_to_go(storage):
    student, _ = _lab(storage)
    storage.add_item("M1-E2", name="Osciloscopio", quantity=1)
    _approved_request(storage, student)
    outcome = nfc.handle_tap(storage, student, "M1-E2", secret="", now=NOW)
    assert outcome["ok"] and outcome["target"] == nfc.TARGET_SCAN and outcome["tone"] == "warning"
    assert "no es parte de tu ruta" in outcome["message"]
    assert len(storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)) == 1


# --- Ruta verificable con toques --------------------------------------------------

def _route_for(storage, request):
    item = storage.get_item(request["item_id"])
    parent = storage.get_item(item["parent_id"])
    return traceability.new_route(item, parent, request)


def test_nfc_taps_confirm_checkpoints_with_method_nfc(storage):
    student, _ = _lab(storage)
    request = _approved_request(storage, student)
    nfc.handle_tap(storage, student, "2-1-01-00-000", secret="", now=NOW)
    route = _route_for(storage, request)
    result = nfc.merge_user_taps(storage, student, request, route)
    assert result and result["ok"]
    container = next(p for p in route["checkpoints"] if p["key"] == "contenedor")
    assert container["done"] and container["method"] == traceability.METHOD_NFC
    assert not traceability.is_complete(route)

    storage.add_item("2-1-01-01-000", name="Caja 01", item_type="child", parent_id="2-1-01-00-000")
    nfc.handle_tap(storage, student, "2-1-01-01-000", secret="", now=NOW + timedelta(minutes=1))
    nfc.handle_tap(storage, student, CODE, secret="", now=NOW + timedelta(minutes=2))
    result = nfc.merge_user_taps(storage, student, request, route)
    assert result["completed"] and traceability.is_complete(route)
    stats = traceability.route_stats(route)
    assert stats["labels"] == 3 and stats["nfc"] == 3 and stats["scanned"] == 3 and stats["full"]


def test_merge_taps_is_idempotent_and_ignores_foreign_taps(storage):
    student, prof = _lab(storage)
    request = _approved_request(storage, student)
    nfc.handle_tap(storage, student, "2-1-01-00-000", secret="", now=NOW)
    route = _route_for(storage, request)
    assert nfc.merge_user_taps(storage, student, request, route)
    snapshot = [dict(point) for point in route["checkpoints"]]
    assert nfc.merge_user_taps(storage, student, request, route) is None
    assert route["checkpoints"] == snapshot
    # el toque de otra persona no avanza mi ruta
    fresh = _route_for(storage, request)
    other = storage.add_user("luis@uniminuto.edu.co", role="estudiante")
    assert nfc.merge_user_taps(storage, other, request, fresh) is None
    assert nfc.merge_taps_into_route(fresh, []) is None


def test_taps_before_the_request_was_approved_are_ignored(storage):
    student, _ = _lab(storage)
    request = _approved_request(storage, student)
    request["reviewed_at"] = (NOW + timedelta(hours=1)).isoformat()
    nfc.handle_tap(storage, student, "2-1-01-00-000", secret="", now=NOW)
    route = _route_for(storage, request)
    assert nfc.merge_user_taps(storage, student, request, route) is None


def test_skipping_a_label_by_tap_leaves_it_inferred(storage):
    student, _ = _lab(storage)
    request = _approved_request(storage, student)
    nfc.handle_tap(storage, student, CODE, secret="", now=NOW)
    route = _route_for(storage, request)
    nfc.merge_user_taps(storage, student, request, route)
    assert traceability.is_complete(route)
    stats = traceability.route_stats(route)
    assert stats["inferred"] == 2 and not stats["full"]


def test_apply_code_default_method_is_still_scan():
    route = traceability.new_route({"id": CODE, "name": "Arduino"}, {"id": "2-1-01-00-000"})
    traceability.apply_code(route, "2-1-01-00-000")
    container = next(p for p in route["checkpoints"] if p["key"] == "contenedor")
    assert container["method"] == traceability.METHOD_SCAN
    traceability.apply_code(route, CODE, method="otro-metodo")
    assert route["checkpoints"][-1]["method"] == traceability.METHOD_SCAN


# --- Registro de chips ------------------------------------------------------------

def test_tag_registry_tracks_written_tested_and_removed(storage):
    student, prof = _lab(storage)
    assert nfc.load_registry(storage) == {}
    nfc.register_tag(storage, CODE, prof, chip="NTAG215", now=NOW)
    registry = nfc.load_registry(storage)
    assert registry[CODE]["status"] == nfc.TAG_WRITTEN and registry[CODE]["chip"] == "NTAG215"
    assert nfc.tag_status_label(registry[CODE]) == "Grabado (sin probar)"

    nfc.handle_tap(storage, student, CODE, secret="", now=NOW + timedelta(minutes=5))
    registry = nfc.load_registry(storage)
    assert registry[CODE]["status"] == nfc.TAG_TESTED and registry[CODE]["taps"] == 1
    assert registry[CODE]["last_tap_by"] == "Ana" and registry[CODE]["written_by"] == "Profe"

    nfc.unregister_tag(storage, CODE, prof, "perdido", now=NOW + timedelta(minutes=10))
    assert CODE not in nfc.load_registry(storage)
    assert nfc.tag_status_label(None) == nfc.NO_TAG_LABEL


def test_a_tap_alone_marks_the_chip_as_tested(storage):
    student, _ = _lab(storage)
    nfc.handle_tap(storage, student, CODE, secret="", now=NOW)
    assert nfc.load_registry(storage)[CODE]["status"] == nfc.TAG_TESTED


def test_register_tag_validates_the_code(storage):
    with pytest.raises(ValueError):
        nfc.register_tag(storage, "M3-E1", {"id": "x"})


def test_recent_taps_lists_newest_first(storage):
    student, prof = _lab(storage)
    nfc.handle_tap(storage, student, CODE, secret="", now=NOW)
    nfc.handle_tap(storage, prof, "2-1-01-01-002", secret="", now=NOW + timedelta(minutes=1))
    assert [nfc.tap_code(e) for e in nfc.recent_taps(storage)] == ["2-1-01-01-002", CODE]
    assert len(nfc.recent_taps(storage, limit=1)) == 1


def test_nfc_events_have_labels_and_custody_text(storage):
    student, _ = _lab(storage)
    nfc.handle_tap(storage, student, CODE, secret="", now=NOW)
    for kind in (traceability.EVENT_NFC_TAP, traceability.EVENT_COUNT_MARK, traceability.EVENT_COUNT_CLOSED):
        assert kind in traceability.EVENT_TYPES and kind in traceability.EVENT_LABELS
    steps = traceability.custody_timeline([], [], storage.get_trace_events(item_id=CODE))
    assert steps[0]["title"] == "Toque de chip NFC" and "Ana" in steps[0]["detail"]


def test_student_taps_never_open_a_count(storage):
    student, prof = _lab(storage)
    ok, _, _ = inventory_count.start_session(storage, prof, now=NOW)
    assert ok
    outcome = nfc.handle_tap(storage, student, CODE, secret="", now=NOW)
    assert outcome["target"] == nfc.TARGET_SCAN and not outcome["count_session"]


@pytest.mark.parametrize("code", ["2-0-00-00-000", "2-1-00-00-000", "M1-E2"])
def test_locations_can_carry_a_chip(code):
    """Regresión: «Estantería 2» y «Piso 1» (2-0-00-00-000, 2-1-00-00-000) mostraban «no se puede generar la URL»."""
    url = nfc.build_tag_url(code, BASE, SECRET)
    assert url.startswith(f"{BASE}/escanear?nfc={code}") and "&s=" in url
