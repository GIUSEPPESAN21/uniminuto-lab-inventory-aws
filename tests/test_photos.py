# -*- coding: utf-8 -*-
"""Fotos de los objetos (core/photos.py): procesamiento de la imagen (orientacion,
tamano, metadatos), indice en la hoja item_photos, subida/descarga/borrado con un
GitHub simulado (sin red) y evento de trazabilidad."""

import base64
import hashlib
import io
import json
import os

import pytest
import requests
from PIL import Image

from core import photos, traceability
from core import storage as sm
from core.storage import LabStorage

PROF = {"id": "p1", "full_name": "Carlos Ruiz", "role": "profesor", "institutional_email": "carlos@uniminuto.edu.co"}
MASTER = {"id": "m1", "full_name": "Ana Maestra", "role": "maestro", "institutional_email": "ana@uniminuto.edu.co"}
STUDENT = {"id": "s1", "full_name": "Laura", "role": "estudiante", "institutional_email": "laura@uniminuto.edu.co"}
CODE = "LAB-001"


def make_jpeg(size=(400, 200), exif=True, color=(200, 30, 30)):
    img = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    if exif:
        data = Image.Exif()
        data[0x0112] = 6                      # girada 90 grados
        data[0x010F] = "TelefonoSecreto"      # fabricante
        gps = data.get_ifd(0x8825)
        gps[1], gps[2], gps[3], gps[4] = "N", (4.0, 36.0, 12.5), "W", (74.0, 4.0, 0.0)
        img.save(buffer, "JPEG", exif=data.tobytes(), comment=b"comentario secreto")
    else:
        img.save(buffer, "JPEG")
    return buffer.getvalue()


# --- procesamiento ----------------------------------------------------------------

def test_exif_orientation_is_applied_and_metadata_is_stripped():
    raw = make_jpeg()
    assert b"TelefonoSecreto" in raw and b"comentario secreto" in raw      # la entrada si los trae
    result = photos.process_image(raw)
    out = Image.open(io.BytesIO(result.data))
    assert out.size == (200, 400)                         # 6 = girada: ahora es vertical
    assert not out.getexif() and "exif" not in out.info
    for blob in (result.data, result.thumbnail):
        assert b"TelefonoSecreto" not in blob and b"comentario secreto" not in blob and b"Exif" not in blob


def test_large_photo_is_resized_to_1024_and_stays_small():
    big = Image.effect_noise((3000, 2000), 40).convert("RGB")
    buffer = io.BytesIO()
    big.save(buffer, "JPEG", quality=92)
    result = photos.process_image(buffer.getvalue())
    out = Image.open(io.BytesIO(result.data))
    assert max(out.size) <= photos.MAX_SIDE and out.format == "JPEG"
    assert len(result.data) <= photos.TARGET_BYTES
    assert (result.width, result.height) == out.size


def test_small_photo_is_not_enlarged_and_thumbnail_is_256():
    result = photos.process_image(make_jpeg((300, 300), exif=False))
    assert Image.open(io.BytesIO(result.data)).size == (300, 300)
    thumb = Image.open(io.BytesIO(result.thumbnail))
    assert max(thumb.size) == photos.THUMB_SIDE and thumb.format == "JPEG"
    assert max(Image.open(io.BytesIO(photos.make_thumbnail(result.data))).size) == photos.THUMB_SIDE


def test_png_with_transparency_becomes_a_white_background_jpeg():
    img = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    out = Image.open(io.BytesIO(photos.process_image(buffer.getvalue()).data))
    assert out.format == "JPEG" and out.getpixel((10, 10))[0] > 240


@pytest.mark.parametrize("data", [b"", b"no soy una imagen", b"\x89PNG\r\n\x1a\ntruncado"])
def test_invalid_images_raise_a_clear_error(data):
    with pytest.raises(photos.PhotoError):
        photos.process_image(data)


def test_oversized_input_is_refused(monkeypatch):
    monkeypatch.setattr(photos, "MAX_INPUT_BYTES", 100)
    with pytest.raises(photos.PhotoError, match="pesa más"):
        photos.process_image(make_jpeg())


def test_blob_sha_matches_git():
    assert photos.blob_sha(b"hola") == hashlib.sha1(b"blob 4\0hola").hexdigest()


def test_paths_are_safe_and_stay_inside_the_data_folder():
    path = photos.photo_path("../../etc/pas swd")
    assert path.startswith("fotos/") and ".." not in path and " " not in path and path.endswith(".jpg")
    assert photos.local_path("../../../etc/passwd").startswith(photos.local_root())
    assert photos.photo_path(CODE) != photos.photo_path(CODE)       # el azar evita choques


# --- GitHub simulado --------------------------------------------------------------

class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload or {}, text
        self.content = b""

    def json(self):
        return self._payload


class FakeRepo:
    """API de contenidos: archivos por ruta; imita sha, 404, 422 y DELETE."""

    def __init__(self):
        self.files, self.calls, self.fail = {}, [], []

    def request(self, method, url, headers=None, json=None, timeout=None, **_):
        self.calls.append((method, url))
        assert "token" in headers["Authorization"]
        if self.fail:
            return Resp(self.fail.pop(0), text="boom")
        path = url.split("/contents/", 1)[1]
        stored = self.files.get(path)
        if method == "GET":
            if stored is None:
                return Resp(404)
            return Resp(200, {"sha": hashlib.sha1(b"blob %d\0" % len(stored) + stored).hexdigest(),
                              "encoding": "base64", "content": base64.b64encode(stored).decode()})
        if method == "PUT":
            if stored is not None and not json.get("sha"):
                return Resp(422, text="sha wasn't supplied")
            data = base64.b64decode(json["content"])
            self.files[path] = data
            return Resp(201, {"content": {"sha": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}})
        assert method == "DELETE"
        if stored is None:
            return Resp(404)
        del self.files[path]
        return Resp(200, {})


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def github(monkeypatch):
    secrets = {"GITHUB_TOKEN": "token-de-prueba", "GITHUB_REPO": "org/repo-de-prueba"}
    monkeypatch.setattr(sm, "safe_secret", lambda key, default=None: secrets.get(key, default))
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)
    # Nada de red: cualquier llamada que no pase por la sesion falsa revienta.
    def no_network(*_a, **_k):
        raise AssertionError("las pruebas no deben usar la red")
    monkeypatch.setattr(requests, "request", no_network)
    photos.clear_cache()
    yield
    photos.clear_cache()


@pytest.fixture
def db():
    storage = LabStorage()
    storage.save_item({"name": "Multimetro", "item_type": "standalone", "quantity": 2},
                      CODE, is_new=True, actor_email=PROF["institutional_email"])
    return storage


def _item(db):
    return db.get_item(CODE)


# --- alta con GitHub ----------------------------------------------------------------

def test_add_photo_uploads_indexes_and_traces(db, repo, github):
    ok, message, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, kind="estado",
                                           note="  rayon   en la carcasa ", session=repo)
    assert ok, message
    assert list(repo.files) == [record["path"]] and record["path"].startswith(f"fotos/{CODE}/")
    assert record["sha"] == photos.blob_sha(repo.files[record["path"]])
    assert record["taken_by"] == PROF["institutional_email"] and record["note"] == "rayon en la carcasa"
    assert max(Image.open(io.BytesIO(repo.files[record["path"]])).size) <= 1024
    assert db.get_item_photos(CODE) == [record] and db.get_photo_counts() == {CODE: 1}
    assert db.get_item_photo(record["id"])["kind"] == "estado"

    events = db.get_trace_events(item_id=CODE, event_type=traceability.EVENT_PHOTO_ADDED)
    assert len(events) == 1 and events[0]["actor_email"] == PROF["institutional_email"]
    info = json.loads(events[0]["details"])
    assert info["photo_id"] == record["id"] and info["kind"] == "estado" and info["stored"] == "github"
    # y aparece en la cadena de custodia
    steps = traceability.custody_timeline(db.get_item_history(CODE), [], events)
    photo_step = next(s for s in steps if s["title"] == "Foto agregada")
    assert photo_step["icon"] == "📷" and "Carlos Ruiz" in photo_step["detail"]
    assert "foto de estado" in photo_step["detail"] and "rayon" in photo_step["detail"]


def test_gallery_is_latest_first(db, repo, github):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ids = [photos.add_photo(db, _item(db), make_jpeg(), PROF, now=start + timedelta(hours=h), session=repo)[2]["id"]
           for h in range(3)]
    assert [p["id"] for p in db.get_item_photos(CODE)] == ids[::-1]
    assert photos.latest_photo(db, CODE)["id"] == ids[-1]
    assert photos.latest_photo(db, "NO-EXISTE") is None


def test_upload_failure_registers_nothing_and_never_raises(db, repo, github):
    repo.fail = [403]
    ok, message, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    assert not ok and record is None and "GITHUB_TOKEN" in message
    assert db.get_item_photos(CODE) == [] and db.get_trace_events(item_id=CODE, event_type="photo_added") == []
    assert not repo.files


def test_network_error_is_reported_not_raised(db, github):
    class Down:
        def request(self, *_a, **_k):
            raise requests.ConnectionError("sin red")
    ok, message, _ = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=Down())
    assert not ok and "Sin conexión" in message and db.get_item_photos(CODE) == []


def test_transient_github_errors_are_retried(db, repo, github):
    repo.fail = [502]                      # un 5xx y luego funciona
    ok, _, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    assert ok and record["path"] in repo.files


def test_index_failure_removes_the_uploaded_file(db, repo, github, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("disco lleno")
    monkeypatch.setattr(db, "add_item_photo", boom)
    ok, message, _ = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    assert not ok and "disco lleno" in message and not repo.files


def test_permissions_and_validation(db, repo, github):
    assert photos.add_photo(db, _item(db), make_jpeg(), STUDENT, session=repo)[0] is False
    assert photos.add_photo(db, _item(db), make_jpeg(), PROF, kind="otra", session=repo)[0] is False
    assert photos.add_photo(db, None, make_jpeg(), PROF, session=repo)[0] is False
    ok, message, _ = photos.add_photo(db, _item(db), b"basura", PROF, session=repo)
    assert not ok and "imagen" in message
    assert not repo.files and db.get_item_photos(CODE) == []
    with pytest.raises(ValueError):
        db.add_item_photo({"item_id": "NO-EXISTE", "path": "fotos/x.jpg"})


# --- baja ------------------------------------------------------------------------

def test_only_master_deletes_and_it_leaves_a_trace(db, repo, github):
    _, _, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    assert photos.delete_photo(db, record["id"], PROF, session=repo)[0] is False
    assert repo.files and db.get_item_photos(CODE)

    ok, message = photos.delete_photo(db, record["id"], MASTER, session=repo)
    assert ok, message
    assert not repo.files and db.get_item_photos(CODE) == []
    assert not os.path.exists(photos.local_path(record["path"]))
    assert len(db.get_trace_events(item_id=CODE, event_type=traceability.EVENT_PHOTO_DELETED)) == 1
    assert photos.delete_photo(db, record["id"], MASTER, session=repo) == (False, "La foto ya no existe.")


def test_failed_remote_delete_keeps_the_photo(db, repo, github):
    _, _, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    repo.fail = [403]
    ok, message = photos.delete_photo(db, record["id"], MASTER, session=repo)
    assert not ok and db.get_item_photos(CODE) and repo.files


def test_deleting_the_item_removes_its_photos_from_the_index(db, repo, github):
    photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    ok, _ = db.delete_item(CODE, PROF["institutional_email"])
    assert ok and db.get_item_photos(CODE) == [] and db.get_photo_counts() == {}


# --- descarga y cache -------------------------------------------------------------

def test_download_goes_to_github_once_then_uses_disk_and_cache(db, repo, github, monkeypatch):
    _, _, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    os.remove(photos.local_path(record["path"]))          # contenedor nuevo: sin copia local
    monkeypatch.setattr(photos, "_cached_photo", photos._cached_photo)   # cache real
    real_download = photos.download
    calls = []
    monkeypatch.setattr(photos, "download", lambda path, session=None: calls.append(path) or real_download(path, session=repo))

    first = photos.photo_bytes(record)
    assert first == repo.files[record["path"]] and calls == [record["path"]]
    assert photos.photo_bytes(record) == first and len(calls) == 1          # cache de Streamlit
    photos.clear_cache()
    assert photos.photo_bytes(record) == first and len(calls) == 1          # copia en disco
    thumb = photos.thumbnail_bytes(record)
    assert max(Image.open(io.BytesIO(thumb)).size) <= photos.THUMB_SIDE


def test_corrupt_local_copy_is_redownloaded(db, repo, github, monkeypatch):
    _, _, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    with open(photos.local_path(record["path"]), "wb") as handle:
        handle.write(b"danado")
    real_download = photos.download
    monkeypatch.setattr(photos, "download", lambda path, session=None: real_download(path, session=repo))
    photos.clear_cache()
    assert photos.photo_bytes(record) == repo.files[record["path"]]


def test_unavailable_photo_returns_none_instead_of_raising(db, repo, github, monkeypatch):
    _, _, record = photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)
    os.remove(photos.local_path(record["path"]))
    repo.files.clear()                                     # alguien la borro del repo
    real_download = photos.download
    monkeypatch.setattr(photos, "download", lambda path, session=None: real_download(path, session=repo))
    photos.clear_cache()
    assert photos.photo_bytes(record) is None and photos.thumbnail_bytes(record) is None


def test_prefetch_downloads_only_what_is_missing(db, repo, github, monkeypatch):
    records = [photos.add_photo(db, _item(db), make_jpeg(), PROF, session=repo)[2] for _ in range(3)]
    os.remove(photos.local_path(records[0]["path"]))
    os.remove(photos.local_path(records[1]["path"]))
    real_download = photos.download
    monkeypatch.setattr(photos, "download", lambda path, session=None: real_download(path, session=repo))
    assert photos.prefetch(records) == 2
    assert photos.prefetch(records) == 0


# --- sin GitHub -------------------------------------------------------------------

def test_without_github_the_photo_stays_on_local_disk_with_a_warning(db):
    assert not photos.github_enabled()
    ok, message, record = photos.add_photo(db, _item(db), make_jpeg(), PROF)
    assert ok and "solo en este servidor" in message
    assert os.path.exists(photos.local_path(record["path"]))
    assert os.path.dirname(photos.local_path(record["path"])).startswith(os.path.dirname(sm.EXCEL_PATH))
    assert json.loads(db.get_trace_events(item_id=CODE)[-1]["details"])["stored"] == "local"
    assert photos.photo_bytes(record) == open(photos.local_path(record["path"]), "rb").read()
    assert photos.delete_photo(db, record["id"], MASTER)[0]
    assert not os.path.exists(photos.local_path(record["path"]))


def test_photo_index_sheet_is_migrated_into_an_existing_database(db):
    import pandas as pd
    # Una base de la version anterior no tiene la hoja item_photos.
    sheets = {name: sm._read_excel(name)[name] for name in sm.SHEET_COLUMNS if name != "item_photos"}
    with pd.ExcelWriter(sm.EXCEL_PATH, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    sm._cached_dfs = None
    old = LabStorage()
    assert old.get_item_photos(CODE) == [] and old.get_photo_counts() == {}
    ok, _, _ = photos.add_photo(old, old.get_item(CODE), make_jpeg(), PROF)
    assert ok and len(old.get_item_photos(CODE)) == 1


def test_describe_uses_names_and_local_time():
    record = {"kind": "estado", "taken_at": "2026-10-10T14:15:00+00:00",
              "taken_by": PROF["institutional_email"], "note": "ok"}
    text = photos.describe(record, {PROF["institutional_email"]: "Carlos Ruiz"})
    assert text == "Estado · 10/10/2026 9:15 a. m. · Carlos Ruiz · ok"
