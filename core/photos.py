# -*- coding: utf-8 -*-
"""core/photos.py - Fotos de los objetos para la trazabilidad.

Flujo de una foto (st.camera_input o st.file_uploader, JPG/PNG):

1. `process_image` la normaliza: aplica la orientacion EXIF de la camara,
   descarta TODOS los metadatos (EXIF con GPS, modelo del telefono,
   comentarios, XMP), la reduce a 1024 px de lado largo y la guarda como JPEG
   de calidad ~75 (objetivo: menos de 150 KB). Tambien genera la miniatura de
   256 px. Esto NO cambio con la migracion a AWS: es procesamiento local,
   independiente de donde se guarde el archivo.
2. `add_photo` publica el JPEG en Amazon S3, en `fotos/<codigo>/<fecha>-
   <azar>.jpg` (mismo convenio de rutas que la version anterior en GitHub), y
   registra en UNA sola transaccion de DynamoDB su fila en `item_photos` y el
   evento `photo_added` de la cadena de custodia (ver core/storage.py).
3. Sin el bucket S3 configurado la foto queda solo en el disco local del
   contenedor (se pierde al reiniciarse la tarea de ECS: ver el aviso).

Las descargas se guardan en cache (st.cache_data y una copia en disco): cada
foto se baja de S3 una sola vez por tarea. Las miniaturas se generan a partir
de la foto y no ocupan espacio en el bucket.

Agregar fotos: profesor y maestro. Eliminarlas: solo el maestro. Ningun error
de red o de imagen lanza excepcion hacia la interfaz: se devuelve (ok, mensaje).
"""

import hashlib
import io
import json
import logging
import os
import re
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
import streamlit as st
from botocore.exceptions import ClientError
from PIL import Image, ImageOps

from core import permissions, traceability
from core.config import safe_secret

logger = logging.getLogger(__name__)

MAX_SIDE = 1024            # lado largo de la foto guardada
THUMB_SIDE = 256           # lado largo de la miniatura
JPEG_QUALITY = 75
THUMB_QUALITY = 70
TARGET_BYTES = 150 * 1024  # objetivo de peso de cada foto
# Si la foto pesa mas que el objetivo (mucho detalle o ruido) se prueba, en
# orden, con menos calidad y luego con menos resolucion; nunca por debajo de esto.
_ENCODE_STEPS = ((MAX_SIDE, JPEG_QUALITY), (MAX_SIDE, 65), (MAX_SIDE, 55), (896, 55), (768, 50), (640, 50))
MAX_INPUT_MB = 20
MAX_INPUT_BYTES = MAX_INPUT_MB * 1024 * 1024
MAX_INPUT_PIXELS = 25_000_000  # ~ una foto de 25 MP (cualquier celular): una de 60 MP ocupaba ~180 MB al decodificarse
UPLOAD_TYPES = ("jpg", "jpeg", "png")
MAX_NOTE_CHARS = 200
DEFAULT_REMOTE_DIR = "fotos"

KIND_REGISTRO = traceability.PHOTO_KIND_REGISTRO
KIND_ESTADO = traceability.PHOTO_KIND_ESTADO
KIND_INVENTARIO = traceability.PHOTO_KIND_INVENTARIO
KINDS = traceability.PHOTO_KINDS
KIND_LABELS = traceability.PHOTO_KIND_LABELS

LOCAL_ONLY_WARNING = (
    "**Las fotos se están guardando solo en este servidor**: el bucket S3 no está configurado "
    "(`PHOTOS_S3_BUCKET`) y se **perderán** al reiniciarse la tarea de ECS."
)
_LOCAL_ONLY_NOTE = "Quedó solo en este servidor: configura el bucket S3 para conservarla."

_S3_CLIENT = None


def _s3():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        region = str(safe_secret("AWS_REGION", "") or "").strip() or None
        _S3_CLIENT = boto3.client("s3", region_name=region) if region else boto3.client("s3")
    return _S3_CLIENT


class PhotoError(ValueError):
    """Error explicable al usuario (imagen invalida, fallo de S3...)."""


@dataclass(frozen=True)
class ProcessedPhoto:
    data: bytes        # JPEG limpio, <= MAX_SIDE px
    thumbnail: bytes   # JPEG limpio, <= THUMB_SIDE px
    width: int
    height: int

    @property
    def size_kb(self) -> float:
        return round(len(self.data) / 1024, 1)


# ---------------------------------------------------------------------------
# Procesamiento de la imagen
# ---------------------------------------------------------------------------

def _open_image(data: bytes) -> Image.Image:
    if not data:
        raise PhotoError("La imagen está vacía.")
    if len(data) > MAX_INPUT_BYTES:
        raise PhotoError(f"La imagen pesa más de {MAX_INPUT_MB} MB.")
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_INPUT_PIXELS:
            raise PhotoError("La imagen tiene demasiados megapíxeles.")
        # JPEG: se decodifica ya reducida (2, 4 u 8 veces) si sobra resolucion;
        # una foto de 12 MP se procesa mucho mas rapido. No afecta a PNG.
        img.draft("RGB", (MAX_SIDE, MAX_SIDE))
        img.load()
    except PhotoError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise PhotoError("El archivo no es una imagen JPG o PNG válida.") from exc
    return img


def _to_srgb(img: Image.Image) -> Image.Image:
    """Convierte a sRGB si la foto trae un perfil de color (p. ej. Display P3
    del iPhone): al descartar el perfil los colores no se ven apagados."""
    icc = img.info.get("icc_profile")
    if not icc or img.mode not in ("RGB", "RGBA"):
        return img
    try:
        from PIL import ImageCms

        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        target = ImageCms.createProfile("sRGB")
        return ImageCms.profileToProfile(img, source, target, outputMode=img.mode) or img
    except Exception:  # sin littlecms o perfil danado: se usan los pixeles tal cual
        return img


def _to_rgb(img: Image.Image) -> Image.Image:
    """Orientacion de la camara aplicada y modo RGB (transparencia sobre blanco)."""
    img = _to_srgb(ImageOps.exif_transpose(img) or img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


def _fit(img: Image.Image, side: int) -> Image.Image:
    """Reduce (nunca agranda) para que el lado largo mida como mucho `side`."""
    width, height = img.size
    scale = side / max(width, height)
    if scale >= 1:
        return img
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return img.resize(size, Image.Resampling.LANCZOS)


def _clean_copy(img: Image.Image) -> Image.Image:
    """Imagen NUEVA solo con los pixeles: sin EXIF (GPS, telefono, fecha),
    comentarios, XMP ni perfiles; Pillow conserva parte de `info` al guardar."""
    return Image.frombytes("RGB", img.size, img.convert("RGB").tobytes())


def _encode(img: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
    return buffer.getvalue()


def process_image(data: bytes) -> ProcessedPhoto:
    """Normaliza una foto de la camara o subida (ver el docstring del modulo).
    Lanza PhotoError si no es una imagen valida."""
    source = _to_rgb(_open_image(data))
    encoded, final = b"", source
    for side, quality in _ENCODE_STEPS:
        final = _clean_copy(_fit(source, side))
        encoded = _encode(final, quality)
        if len(encoded) <= TARGET_BYTES:
            break
    thumbnail = _encode(_clean_copy(_fit(final, THUMB_SIDE)), THUMB_QUALITY)
    return ProcessedPhoto(data=encoded, thumbnail=thumbnail, width=final.width, height=final.height)


def make_thumbnail(data: bytes) -> bytes:
    """Miniatura de 256 px (JPEG limpio) de una foto ya guardada."""
    return _encode(_clean_copy(_fit(_to_rgb(_open_image(data)), THUMB_SIDE)), THUMB_QUALITY)


def blob_sha(data: bytes) -> str:
    """Hash de contenido estable (mismo algoritmo que antes usaba GitHub para
    el `sha` del blob): sirve para que la cache local sepa si su copia sigue
    vigente sin tener que volver a bajar el archivo de S3."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---------------------------------------------------------------------------
# Rutas: repositorio y copia local
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _folder_name(item_id) -> str:
    name = _UNSAFE.sub("_", str(item_id or "").strip()).strip("._")
    return name[:80] or "sin_codigo"


def remote_dir() -> str:
    """Prefijo de las fotos en el bucket S3 (secreto opcional PHOTOS_S3_PREFIX)."""
    configured = str(safe_secret("PHOTOS_S3_PREFIX", DEFAULT_REMOTE_DIR) or "")
    return configured.strip().strip("/") or DEFAULT_REMOTE_DIR


def photo_path(item_id, now=None) -> str:
    """fotos/<codigo>/<AAAAMMDDTHHMMSSZ>-<azar>.jpg (el azar evita choques en el mismo segundo)."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{remote_dir()}/{_folder_name(item_id)}/{stamp}-{uuid.uuid4().hex[:6]}.jpg"


def local_root() -> str:
    """Carpeta de cache local (disco efimero de la tarea de ECS; solo adelanta
    lecturas repetidas del mismo archivo de S3 dentro de la misma tarea)."""
    configured = str(safe_secret("PHOTOS_CACHE_DIR", "") or "").strip()
    return configured or os.path.join(tempfile.gettempdir(), "uniminuto-lab-photos-cache")


def local_path(path: str) -> str:
    """Copia local de `path`. Nunca sale de la carpeta de datos aunque el indice
    se haya editado a mano (se ignoran '..' y rutas absolutas)."""
    parts = [part for part in str(path or "").replace("\\", "/").split("/") if part not in ("", ".", "..")]
    if not parts:
        raise PhotoError("Ruta de foto vacía.")
    return os.path.join(local_root(), *parts)


def _write_local(path: str, data: bytes) -> None:
    target = local_path(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _remove_local(path: str) -> None:
    try:
        os.remove(local_path(path))
    except (OSError, PhotoError):
        pass


def _read_local(path: str, sha: str = ""):
    """Bytes de la copia local, o None si no esta (o no coincide con `sha`)."""
    try:
        with open(local_path(path), "rb") as handle:
            data = handle.read()
    except (OSError, PhotoError):
        return None
    return data if not sha or blob_sha(data) == sha else None


# ---------------------------------------------------------------------------
# Amazon S3
# ---------------------------------------------------------------------------

def photos_bucket() -> str:
    return str(safe_secret("PHOTOS_S3_BUCKET", "") or "").strip()


def github_enabled() -> bool:
    """Nombre heredado (lo usan ``_fetch``/``prefetch``/``add_photo`` para decidir
    si hay un almacen remoto configurado): True si hay bucket S3."""
    return bool(photos_bucket())


def upload(path: str, data: bytes, message: str, session=None) -> str:
    """Sube un archivo NUEVO a S3 y devuelve su hash de contenido. ``message``
    y ``session`` se conservan en la firma por compatibilidad (la version
    anterior los usaba para el commit de GitHub y las pruebas); ``message`` se
    guarda como metadato del objeto, ``session`` ya no aplica con boto3."""
    sha = blob_sha(data)
    try:
        _s3().put_object(
            Bucket=photos_bucket(), Key=path, Body=data, ContentType="image/jpeg",
            Metadata={"sha": sha, "message": (message or "")[:256]},
        )
    except ClientError as exc:
        raise PhotoError(f"No se pudo subir la foto a S3: {exc}") from exc
    return sha


def download(path: str, session=None) -> bytes:
    """Contenido de un objeto del bucket. Lanza PhotoError si no se puede."""
    try:
        response = _s3().get_object(Bucket=photos_bucket(), Key=path)
    except _s3().exceptions.NoSuchKey as exc:
        raise PhotoError("La foto ya no está en el bucket de datos.") from exc
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise PhotoError("La foto ya no está en el bucket de datos.") from exc
        raise PhotoError(f"No se pudo descargar la foto de S3: {exc}") from exc
    with response["Body"] as body:
        return body.read()


def remove_remote(path: str, sha: str, message: str, session=None) -> None:
    """Borra un objeto del bucket (si ya no estaba, no es un error). ``sha`` y
    ``message`` se conservan en la firma por compatibilidad; S3 no tiene borrado
    condicional por hash de contenido como tenia la API de GitHub."""
    try:
        _s3().delete_object(Bucket=photos_bucket(), Key=path)
    except ClientError as exc:
        raise PhotoError(f"No se pudo eliminar la foto en S3: {exc}") from exc


# ---------------------------------------------------------------------------
# Alta y baja de fotos
# ---------------------------------------------------------------------------

def can_add(user: dict) -> bool:
    return permissions.has_cap(user, permissions.CAP_EDIT_INVENTORY)


def can_delete(user: dict) -> bool:
    return permissions.has_cap(user, permissions.CAP_DELETE_PHOTOS)


def _clean_note(note) -> str:
    return " ".join(str(note or "").split())[:MAX_NOTE_CHARS]


def _trace_event(event_type: str, record: dict, actor: dict, now: datetime, extra: dict = None) -> dict:
    details = {"photo_id": record.get("id", ""), "path": record.get("path", ""),
               "kind": record.get("kind", ""), "note": record.get("note", "")}
    details.update(extra or {})
    actor = actor or {}
    return {
        "event_type": event_type, "request_id": "", "item_id": record.get("item_id", ""), "loan_id": "",
        "user_id": "", "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
        "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": now.isoformat(),
    }


def add_photo(storage, item: dict, image_bytes: bytes, actor: dict, kind: str = KIND_REGISTRO,
              note: str = "", now=None, session=None):
    """Procesa, guarda y registra una foto. Devuelve (ok, mensaje, registro).

    Con el bucket S3 configurado, si la subida falla no se registra nada (se
    puede reintentar con la misma foto). Sin bucket, la foto queda en el disco
    local del contenedor."""
    if not can_add(actor):
        return False, "Solo un supervisor autorizado o un administrador pueden agregar fotos.", None
    if not item or not item.get("id"):
        return False, "El objeto no existe.", None
    if kind not in KINDS:
        return False, "Tipo de foto no válido.", None
    try:
        processed = process_image(image_bytes)
    except PhotoError as exc:
        return False, str(exc), None

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = photo_path(item["id"], now)
    sha = blob_sha(processed.data)
    remote = github_enabled()
    if remote:
        message = f"Foto {item['id']} ({kind}) [{now.strftime('%Y-%m-%d %H:%M:%S')} UTC]"
        try:
            sha = upload(path, processed.data, message, session=session)
        except Exception as exc:  # PhotoError o respuesta inesperada: nada queda registrado
            logger.error(f"No se pudo subir la foto {path}: {exc}")
            return False, f"No se pudo subir la foto a S3: {exc} Intenta de nuevo.", None
    try:
        _write_local(path, processed.data)  # copia de trabajo (o unica copia sin S3)
    except OSError as exc:
        if not remote:
            return False, f"No se pudo guardar la foto en este servidor: {exc}", None
        logger.warning(f"Foto {path} subida, pero sin copia local: {exc}")

    record = {
        "id": uuid.uuid4().hex[:20], "item_id": item["id"], "path": path, "sha": sha,
        "taken_by": (actor or {}).get("institutional_email", ""), "taken_at": now.isoformat(),
        "kind": kind, "note": _clean_note(note),
    }
    extra = {"width": processed.width, "height": processed.height, "size_kb": processed.size_kb,
             "stored": "s3" if remote else "local"}
    try:
        saved = storage.add_item_photo(record, trace_event=_trace_event(
            traceability.EVENT_PHOTO_ADDED, record, actor, now, extra))
    except Exception as exc:
        # El indice no se pudo escribir: se retira el archivo para no dejarlo huerfano.
        logger.error(f"No se pudo registrar la foto {path}: {exc}")
        _remove_local(path)
        if remote:
            try:
                remove_remote(path, sha, f"Retira foto no registrada {path}", session=session)
            except Exception:
                pass
        return False, f"No se pudo registrar la foto: {exc}", None

    label = KIND_LABELS[kind].lower()
    text = f"Foto de {label} guardada ({processed.size_kb:.0f} KB)."
    return True, text if remote else f"{text} {_LOCAL_ONLY_NOTE}", saved


def delete_photo(storage, photo_id: str, actor: dict, now=None, session=None):
    """Elimina una foto (repositorio, indice y copia local). Devuelve (ok, mensaje).
    El borrado queda en la cadena de custodia (evento `photo_deleted`)."""
    if not can_delete(actor):
        return False, "Solo un administrador puede eliminar fotos."
    record = storage.get_item_photo(photo_id)
    if not record:
        return False, "La foto ya no existe."
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if github_enabled() and record.get("sha"):
        try:
            remove_remote(record["path"], record["sha"], f"Elimina foto {record['path']}", session=session)
        except Exception as exc:  # el indice no se toca: la foto sigue visible
            logger.error(f"No se pudo eliminar la foto {record['path']}: {exc}")
            return False, f"No se pudo eliminar la foto en S3: {exc}"
    try:
        storage.delete_item_photo(photo_id, trace_event=_trace_event(
            traceability.EVENT_PHOTO_DELETED, record, actor, now))
    except Exception as exc:
        logger.error(f"No se pudo quitar del indice la foto {photo_id}: {exc}")
        return False, f"No se pudo eliminar la foto: {exc}"
    _remove_local(record["path"])
    return True, "Foto eliminada."


# ---------------------------------------------------------------------------
# Lectura con cache
# ---------------------------------------------------------------------------

def _fetch(path: str, sha: str = "", session=None) -> bytes:
    """Copia local si esta (y coincide con `sha`); si no, se descarga de S3
    y se deja en disco para la proxima vez."""
    data = _read_local(path, sha)
    if data is not None:
        return data
    if not github_enabled():
        raise PhotoError("La foto no está en este servidor (sin bucket S3 configurado).")
    data = download(path, session=session)
    try:
        _write_local(path, data)
    except OSError as exc:
        logger.warning(f"No se pudo guardar en disco la foto {path}: {exc}")
    return data


@st.cache_data(show_spinner=False, max_entries=200)
def _cached_photo(path: str, sha: str) -> bytes:
    # Clave (ruta, sha): el contenido de una clave nunca cambia. Si falla, la
    # excepcion no se guarda en cache y la proxima lectura lo vuelve a intentar.
    return _fetch(path, sha)


@st.cache_data(show_spinner=False, max_entries=1000)
def _cached_thumbnail(path: str, sha: str) -> bytes:
    return make_thumbnail(_cached_photo(path, sha))


def photo_bytes(record: dict):
    """JPEG completo (<= 1024 px) de un registro de `item_photos`, o None."""
    try:
        return _cached_photo(str(record.get("path") or ""), str(record.get("sha") or ""))
    except Exception as exc:  # red, S3, disco o archivo danado: la vista sigue sin la foto
        logger.warning(f"Foto no disponible {record.get('path')}: {exc}")
        return None


def thumbnail_bytes(record: dict):
    """Miniatura (<= 256 px) de un registro de `item_photos`, o None."""
    try:
        return _cached_thumbnail(str(record.get("path") or ""), str(record.get("sha") or ""))
    except Exception as exc:
        logger.warning(f"Miniatura no disponible {record.get('path')}: {exc}")
        return None


def prefetch(records: list, workers: int = 4, session=None) -> int:
    """Descarga en paralelo (al disco) las fotos que aun no estan en este
    servidor; asi una galeria no espera una descarga tras otra. Devuelve
    cuantas se descargaron. Nunca lanza."""
    if not github_enabled():
        return 0
    missing = [r for r in records or [] if r.get("path") and _read_local(r["path"], r.get("sha") or "") is None]
    if not missing:
        return 0

    def fetch(record):
        try:
            _fetch(record["path"], record.get("sha") or "", session=session)
            return 1
        except Exception:
            return 0

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(missing)))) as pool:
        return sum(pool.map(fetch, missing))


def clear_cache() -> None:
    _cached_photo.clear()
    _cached_thumbnail.clear()


def latest_photo(storage, item_id: str):
    photos = storage.get_item_photos(item_id)
    return photos[0] if photos else None


def describe(record: dict, names: dict = None) -> str:
    """«Estado · 10/10/2026 9:15 a. m. · Carlos Ruiz · nota» para pies de foto."""
    email = record.get("taken_by") or ""
    author = (names or {}).get(email) or email
    parts = [KIND_LABELS.get(record.get("kind"), "Foto"), traceability.fmt_local(record.get("taken_at")),
             author, record.get("note") or ""]
    return " · ".join(part for part in parts if part)
