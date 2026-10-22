# -*- coding: utf-8 -*-
"""Fotos en la interfaz (AppTest): accion «Foto» del catalogo, galeria del editor
y foto en Escanear, por rol. Sin GitHub: las fotos quedan en el disco temporal."""

import io

import pytest

pytest.importorskip("streamlit.testing.v1")

from PIL import Image  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from core import barcode, photos, traceability  # noqa: E402
from core.storage import LabStorage  # noqa: E402

CODE = "LAB-001"
USERS = {
    "profesor": {"id": "p1", "role": "profesor", "full_name": "Prof Uno", "institutional_email": "prof@uniminuto.edu.co"},
    "maestro": {"id": "m1", "role": "maestro", "full_name": "Ana Maestra", "institutional_email": "ana@uniminuto.edu.co"},
    "estudiante": {"id": "s1", "role": "estudiante", "full_name": "Laura", "institutional_email": "laura@uniminuto.edu.co"},
}


def _script():
    import streamlit as st

    from core.storage import LabStorage
    from views import escanear, inventario

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    {"inventario": inventario, "escanear": escanear}[st.session_state.page].render()


def _jpeg(color=(10, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture
def db():
    storage = LabStorage()
    storage.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": 2}, CODE, is_new=True,
                      actor_email="prof@uniminuto.edu.co")
    return storage


def _open(page, role, **state):
    at = AppTest.from_function(_script, default_timeout=30)
    at.session_state["page"] = page
    at.session_state["test_user"] = USERS[role]
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    assert not at.exception, at.exception
    return at


def _text(at):
    captions = [c for image in at.image for c in image.captions]
    return " ".join([m.value for m in at.markdown] + [c.value for c in at.caption] + captions
                    + [w.value for w in at.warning] + [s.value for s in at.success])


def _photo_images(at):
    """Imagenes de fotos (el catalogo tambien pinta la vista previa de la etiqueta)."""
    return [i for i in at.image if not i.captions[0].startswith("Vista previa")]


def _uploaders(at):
    return [u for u in at.file_uploader if "foto" in u.label.lower()]


def _add(db, role="profesor", **kwargs):
    ok, message, record = photos.add_photo(db, db.get_item(CODE), _jpeg(), USERS[role], **kwargs)
    assert ok, message
    return record


def test_catalog_card_has_a_lazy_photo_popover(db):
    at = _open("inventario", "profesor")
    assert _uploaders(at) == []                            # cerrado: no se ejecuta su contenido
    at = _open("inventario", "profesor", **{f"inv_photo_{CODE}": True})
    assert len(_uploaders(at)) == 1
    assert "Todavía no tiene fotos" in _text(at)
    assert "solo en este servidor" in _text(at)             # aviso: GitHub no configurado


def test_professor_uploads_a_photo_from_the_catalog(db):
    at = _open("inventario", "profesor", **{f"inv_photo_{CODE}": True})
    _uploaders(at)[0].set_value(("foto.jpg", _jpeg(), "image/jpeg"))
    at.run()
    save = next(b for b in at.button if b.key and b.key.endswith("_save"))
    assert not save.disabled
    at.text_input(key=f"inv_photo_{CODE}_new_0_note").input("caja completa")
    at.run()
    next(b for b in at.button if b.key and b.key.endswith("_save")).click().run()
    assert not at.exception

    records = db.get_item_photos(CODE)
    assert len(records) == 1 and records[0]["note"] == "caja completa" and records[0]["kind"] == "registro"
    assert records[0]["taken_by"] == "prof@uniminuto.edu.co"
    assert len(db.get_trace_events(item_id=CODE, event_type=traceability.EVENT_PHOTO_ADDED)) == 1
    assert "Foto de registro guardada" in _text(at)
    assert len(_photo_images(at)) == 1


def test_save_is_disabled_without_a_picture(db):
    at = _open("inventario", "profesor", **{f"inv_photo_{CODE}": True})
    save = next(b for b in at.button if b.key and b.key.endswith("_save"))
    assert save.disabled


def test_editor_gallery_lists_photos_and_only_master_can_delete(db):
    first = _add(db)
    second = _add(db, kind="estado", note="rayon")
    at = _open("inventario", "profesor", editing_item_id=CODE)
    assert len(_photo_images(at)) == 2 and "Fotos del objeto" in _text(at)
    assert [i for i in db.get_item_photos(CODE)][0]["id"] == second["id"]    # la ultima primero
    assert not any(b.label.endswith("Eliminar foto") for b in at.button)

    at = _open("inventario", "maestro", editing_item_id=CODE)
    delete = next(b for b in at.button if b.key == f"photo_delete_{first['id']}")
    delete.click().run()
    assert "¿Eliminar esta foto?" in _text(at)
    next(b for b in at.button if b.key == f"photo_delete_yes_{first['id']}").click().run()
    assert not at.exception
    assert [p["id"] for p in db.get_item_photos(CODE)] == [second["id"]]
    assert len(db.get_trace_events(item_id=CODE, event_type=traceability.EVENT_PHOTO_DELETED)) == 1


def test_editor_without_photos_invites_to_add_one(db):
    at = _open("inventario", "profesor", editing_item_id=CODE)
    assert "todavía no tiene fotos" in _text(at)
    assert any("Agregar foto" in e.label for e in at.expander)


def test_scan_shows_the_latest_photo_to_students_without_adding_controls(db):
    _add(db, note="tal como llega")
    at = _open("escanear", "estudiante", scan_result=barcode.scan(db, CODE))
    assert len(_photo_images(at)) == 1 and "Así se ve" in _text(at) and "tal como llega" in _text(at)
    assert _uploaders(at) == [] and not any("Foto de estado" in str(p) for p in at.main)

    at = _open("escanear", "estudiante", scan_result=barcode.scan(db, CODE), **{f"scan_photo_{CODE}": True})
    assert _uploaders(at) == []                             # el estudiante nunca puede agregar


def test_professor_adds_a_state_photo_while_scanning(db):
    at = _open("escanear", "profesor", scan_result=barcode.scan(db, CODE), **{f"scan_photo_{CODE}": True})
    kind = at.radio(key=f"scan_photo_{CODE}_new_0_kind")
    assert kind.value == "estado"                           # por defecto: foto de estado
    _uploaders(at)[0].set_value(("estado.jpg", _jpeg(), "image/jpeg"))
    at.run()
    next(b for b in at.button if b.key and b.key.endswith("_save")).click().run()
    assert not at.exception
    records = db.get_item_photos(CODE)
    assert len(records) == 1 and records[0]["kind"] == "estado"
    # El prestamo sigue funcionando igual que antes
    assert db.get_available_quantity(CODE) == 2


def test_scan_of_a_container_shows_its_photo_and_children_thumbnails(db):
    db.save_item({"name": "Caja", "item_type": "master"}, "2-1-01-00-000", is_new=True, actor_email="x@u.co")
    db.save_item({"name": "Resistencias", "item_type": "child", "parent_id": "2-1-01-00-000", "quantity": 5},
                 "2-1-01-01-000", is_new=True, actor_email="x@u.co")
    for code in ("2-1-01-00-000", "2-1-01-01-000"):
        ok, message, _ = photos.add_photo(db, db.get_item(code), _jpeg(), USERS["profesor"])
        assert ok, message
    at = _open("escanear", "estudiante", scan_result=barcode.scan(db, "2-1-01-00-000"))
    assert len(_photo_images(at)) == 2
