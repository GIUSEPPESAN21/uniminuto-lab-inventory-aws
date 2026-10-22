# -*- coding: utf-8 -*-
"""Flujo del chip NFC con la app real (AppTest + Excel temporal): el toque que
llega con ?nfc= antes del login se conserva y se registra al entrar, se aplica
una sola vez, confirma la ruta verificable y alimenta el conteo con el telefono."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import auth, inventory_count, nfc, traceability  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")
PASSWORD = "ClaveSegura123"
CODE = "2-1-01-01-001"


def _start(code=None, signature=None):
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    if code is not None:
        at.query_params["nfc"] = code
    if signature is not None:
        at.query_params["s"] = signature
    at.run()
    assert not at.exception
    return at, at.session_state["storage"]


def _seed(storage):
    storage.save_item({"name": "Electrónica", "item_type": "master"}, "2-1-01-00-000", is_new=True)
    storage.save_item({"name": "Arduino UNO", "item_type": "child", "parent_id": "2-1-01-00-000", "quantity": 3},
                      CODE, is_new=True)
    hashed = auth.hash_password(PASSWORD)
    return {
        "student": storage.create_user("Ana Estudiante", "ana@uniminuto.edu.co", hashed, "estudiante"),
        "prof": storage.create_user("Profe Gómez", "prof@uniminuto.edu.co", hashed, "profesor"),
        "master": storage.create_user("Maestro", "maestro@uniminuto.edu.co", hashed, "maestro"),
    }


def _login(at, email):
    at.text_input[0].input(email)
    at.text_input[1].input(PASSWORD)
    at.button[0].click().run()
    assert not at.exception


def _taps(storage):
    return storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)


def _texts(at):
    return " ".join(str(el.value) for group in (at.info, at.success, at.warning, at.error, at.markdown) for el in group)


def test_tap_before_login_is_kept_and_registered_after_login():
    at, storage = _start(CODE)
    users = _seed(storage)
    assert _taps(storage) == []                       # sin sesion no se registra nada
    assert any("Tocaste el chip NFC" in el.value for el in at.info)
    assert at.session_state[nfc_pending_key()]["code"] == CODE

    _login(at, "ana@uniminuto.edu.co")
    events = _taps(storage)
    assert len(events) == 1 and events[0]["item_id"] == CODE and events[0]["user_id"] == users["student"]["id"]
    assert at.session_state["scan_result"]["status"] == "found_item"
    assert "Toque registrado" in _texts(at)
    assert "nfc" not in at.query_params                # una recarga no vuelve a contarlo


def test_tap_with_a_session_already_open_goes_to_escanear():
    at, storage = _start()
    _seed(storage)
    _login(at, "ana@uniminuto.edu.co")
    at.query_params["nfc"] = CODE
    at.run()
    assert not at.exception
    assert len(_taps(storage)) == 1
    assert at.session_state["scan_result"]["item"]["name"] == "Arduino UNO"
    assert "nfc" not in at.query_params
    at.run()                                           # recarga: sigue siendo un solo toque
    assert len(_taps(storage)) == 1


def test_unknown_code_shows_a_warning_and_records_nothing():
    at, storage = _start("9-9-99-99-999")
    _seed(storage)
    _login(at, "ana@uniminuto.edu.co")
    assert _taps(storage) == []
    assert "no está registrado" in _texts(at)


def test_signature_is_enforced_when_a_secret_is_configured(monkeypatch):
    monkeypatch.setattr(nfc, "nfc_secret", lambda: "secreto")
    at, storage = _start(CODE, "AAAAAAAAAA")
    _seed(storage)
    _login(at, "ana@uniminuto.edu.co")
    assert _taps(storage) == []
    assert "firma" in _texts(at)

    at.query_params["nfc"] = CODE
    at.query_params["s"] = nfc.sign_code(CODE, "secreto")
    at.run()
    assert len(_taps(storage)) == 1


def test_nfc_tap_confirms_the_route_checkpoint_in_trazabilidad():
    at, storage = _start()
    users = _seed(storage)
    request = storage.create_service_request(
        {"request_type": "product", "item_id": CODE, "item_name": "Arduino UNO", "quantity": 1}, users["student"])
    storage.update_service_request_status(request["id"], "approved", "prof@uniminuto.edu.co")
    _login(at, "ana@uniminuto.edu.co")
    at.query_params["nfc"] = "2-1-01-00-000"
    at.run()
    assert not at.exception
    assert at.session_state["trace_focus_request"] == request["id"]
    route = at.session_state[f"trace_route_{request['id']}"]
    container = next(p for p in route["checkpoints"] if p["key"] == "contenedor")
    assert container["done"] and container["method"] == traceability.METHOD_NFC
    assert "nfc" not in at.query_params

    # el chip del producto completa la ruta (la caja intermedia queda inferida) y se guarda un comprobante
    storage.save_item({"name": "Caja", "item_type": "child", "parent_id": "2-1-01-00-000"}, "2-1-01-01-000",
                      is_new=True)
    at.query_params["nfc"] = "2-1-01-01-000"
    at.run()
    at.query_params["nfc"] = CODE
    at.run()
    assert not at.exception
    saved = traceability.route_event_for(storage, request["id"])
    assert saved is not None
    info = traceability.event_details(saved)
    assert info["nfc"] == 3 and info["scanned"] == 3
    assert {c["method"] for c in info["checkpoints"] if c["label_code"]} == {"nfc"}


def test_professor_tap_during_a_count_marks_the_product_verified():
    at, storage = _start()
    users = _seed(storage)
    ok, _, session = inventory_count.start_session(storage, users["prof"], "Prueba")
    assert ok
    _login(at, "prof@uniminuto.edu.co")
    at.query_params["nfc"] = CODE
    at.run()
    assert not at.exception
    marks = inventory_count.session_marks(storage, session["id"])
    assert [m["code"] for m in marks] == [CODE] and marks[0]["method"] == "nfc"
    assert at.session_state["nfc_count_focus"]["code"] == CODE
    assert "Conteo abierto: Prueba" in _texts(at)


def test_students_have_no_nfc_page_but_managers_do():
    at, storage = _start()
    _seed(storage)
    _login(at, "ana@uniminuto.edu.co")
    assert "nfc" not in at.session_state["pages"]
    at2, storage2 = _start()
    _login(at2, "prof@uniminuto.edu.co")
    assert "nfc" in at2.session_state["pages"]


# --- Pagina Chips NFC --------------------------------------------------------------

def _page_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import nfc as nfc_page

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    nfc_page.render()


def _open_page(user):
    at = AppTest.from_function(_page_script, default_timeout=30)
    at.session_state["test_user"] = {key: user[key] for key in ("id", "full_name", "role", "institutional_email")}
    at.run()
    assert not at.exception
    return at


def _button(at, text):
    return next(b for b in at.button if text in b.label)


def test_page_is_blocked_for_students():
    at, storage = _start()
    users = _seed(storage)
    page = _open_page(users["student"])
    assert any("No tienes permiso" in e.value for e in page.error)


def test_page_start_mark_and_close_a_count_without_changing_quantities():
    at, storage = _start()
    users = _seed(storage)
    page = _open_page(users["prof"])
    assert not storage.get_trace_events(event_type=traceability.EVENT_COUNT_STARTED)
    _button(page, "Iniciar conteo").click().run()
    assert not page.exception
    session = inventory_count.open_session(storage)
    assert session and session["scope"] == {"kind": "all"}

    code_input = next(t for t in page.text_input if "Código del producto" in t.label)
    code_input.input(CODE)
    next(n for n in page.number_input if "Unidades contadas" in n.label and "opcional" in n.label).set_value(2)
    _button(page, "Marcar como verificado").click().run()
    assert not page.exception
    marks = inventory_count.session_marks(storage, session["id"])
    assert [(m["code"], m["qty"], m["method"]) for m in marks] == [(CODE, 2, "codigo")]

    _button(page, "Cerrar conteo y guardar resumen").click().run()
    assert not page.exception
    assert inventory_count.open_session(storage) is None
    closed = inventory_count.closed_sessions(storage)
    assert closed[0]["verified"] == 1 and closed[0]["differences"][0]["code"] == CODE
    assert storage.get_item(CODE)["quantity"] == 3     # el conteo no toca las cantidades


def test_page_rejects_unknown_codes_in_a_count():
    at, storage = _start()
    users = _seed(storage)
    inventory_count.start_session(storage, users["prof"], "Prueba")
    page = _open_page(users["prof"])
    next(t for t in page.text_input if "Código del producto" in t.label).input("NO-EXISTE-9")
    _button(page, "Marcar como verificado").click().run()
    assert not page.exception
    assert any("no está en el inventario" in w.value for w in page.warning)
    assert storage.get_trace_events(event_type=traceability.EVENT_COUNT_MARK) == []


def _page_text(page) -> str:
    return " ".join(str(e.value) for e in list(page.markdown) + list(page.caption) + list(page.warning))


def test_tags_tab_lists_the_url_of_each_product(monkeypatch):
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: {
        "APP_URL": "https://lab.example.edu.co", "NFC_SECRET": "secreto"}.get(key, default))
    at, storage = _start()
    users = _seed(storage)
    page = _open_page(users["prof"])
    codes = [c.value for c in page.code]
    expected = nfc.build_tag_url("2-1-01-00-000", "https://lab.example.edu.co", "secreto")
    assert any(value in (expected, nfc.build_tag_url("2-1-01-01-001", "https://lab.example.edu.co", "secreto"))
               for value in codes)
    first = next(b for b in page.button if "Marcar grabado" in b.label)
    code = first.key.removeprefix("nfc_written_")
    first.click().run()
    assert not page.exception
    assert nfc.load_registry(storage)[code]["status"] == nfc.TAG_WRITTEN
    assert any(b.key == f"nfc_removed_{code}" for b in page.button)            # ahora se ofrece quitarlo


def test_locations_show_their_url_instead_of_an_error(monkeypatch):
    """Regresión: Estantería 2 / Piso 1 decían «No se puede generar la URL… Contenedor inválido»."""
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: {"APP_URL": "https://lab.example.edu.co"}.get(key, default))
    at, storage = _start()
    users = _seed(storage)
    storage.save_item({"name": "Estantería 2", "item_type": "location"}, "2-0-00-00-000", is_new=True)
    storage.save_item({"name": "Piso 1", "item_type": "location"}, "2-1-00-00-000", is_new=True)
    page = _open_page(users["prof"])
    assert not any("No se puede generar la URL" in w.value for w in page.warning)
    urls = [c.value for c in page.code]
    assert any("nfc=2-0-00-00-000" in u for u in urls) and any("nfc=2-1-00-00-000" in u for u in urls)


def test_overview_progress_and_filters(monkeypatch):
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: {"APP_URL": "https://lab.example.edu.co"}.get(key, default))
    at, storage = _start()
    users = _seed(storage)
    nfc.register_tag(storage, "2-1-01-01-001", users["prof"])
    page = _open_page(users["prof"])
    text = _page_text(page)
    assert "Con chip probado" in text and "Grabados sin probar" in text and "Sin chip" in text
    # Por omisión solo se ven los lugares donde van chips (estanterías, pisos, contenedores); un producto con chip
    # aparece al elegir «Todo».
    assert not [b for b in page.button if b.key == "nfc_removed_2-1-01-01-001"]
    page.selectbox(key="nfc_tag_type").set_value("Todo").run()
    page.segmented_control(key="nfc_tag_status").set_value(nfc.TAG_STATUS_LABELS[nfc.TAG_WRITTEN]).run()
    cards = [b for b in page.button if b.key and b.key.startswith(("nfc_removed_", "nfc_written_"))]
    assert [b.key for b in cards] == ["nfc_removed_2-1-01-01-001"]
    page.segmented_control(key="nfc_tag_status").set_value("Todos").run()
    page.selectbox(key="nfc_tag_type").set_value("Contenedores").run()
    assert {b.key for b in page.button if b.key and b.key.startswith("nfc_written_")} == {"nfc_written_2-1-01-00-000"}
    page.text_input(key="nfc_tag_search").input("zzz-no-existe").run()
    assert "Ningún producto coincide" in _page_text(page)


def test_recent_taps_are_listed_as_a_readable_timeline(monkeypatch):
    monkeypatch.setattr(nfc, "safe_secret", lambda key, default=None: {"APP_URL": "https://lab.example.edu.co"}.get(key, default))
    at, storage = _start()
    users = _seed(storage)
    item = storage.get_item("2-1-01-01-001")
    nfc.record_tap(storage, users["prof"], "2-1-01-01-001", item=item, signed=True)
    page = _open_page(users["prof"])
    text = _page_text(page)
    assert "tocó «Arduino UNO»" in text and "Firmado" in text


def nfc_pending_key():
    from views import nfc_tap
    return nfc_tap.PENDING_KEY
