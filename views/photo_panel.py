# -*- coding: utf-8 -*-
"""views/photo_panel.py - Fotos de los objetos en la interfaz.

- Inventario: accion «📷 Foto» en cada tarjeta (ultima foto + tomar/subir una)
  y galeria en el editor del item (la mas reciente primero; el maestro puede
  eliminar).
- Escanear: la ultima foto del objeto (ayuda a reconocerlo) y, para profesor y
  maestro, «Foto de estado (opcional)» al dar salida o reingresar.

El procesamiento y el guardado viven en core/photos.py. Dos cuidados:
- El contenido de los popovers solo se ejecuta si estan abiertos
  (on_change="rerun"): el catalogo no descarga fotos que nadie esta mirando.
- La camara del navegador (st.camera_input) solo se enciende con su
  interruptor; «Subir foto» en el celular tambien deja tomarla con la camara.
"""

import streamlit as st

from core import photos, traceability
from core.ui import badge_html, centered_columns, section_title

GALLERY_LIMIT = 12     # fotos visibles de entrada en la galeria del editor
GALLERY_PER_ROW = 3
SCAN_WIDTH = 360       # ancho de la foto en Escanear (en celular, el ancho disponible)
_NO_PHOTO = ":material/hide_image: La foto no está disponible en este momento."


def _names(storage, records: list) -> dict:
    """Nombre de quien tomo cada foto (correo -> nombre)."""
    names = {}
    for email in {r.get("taken_by") for r in records if r.get("taken_by")}:
        try:
            found = storage.get_user_by_email(email)
        except Exception:
            found = None
        names[email] = (found or {}).get("full_name") or email
    return names


def _show(data, caption: str = None, width=photos.THUMB_SIDE) -> None:
    if data is None:
        st.caption(_NO_PHOTO)
        return
    st.image(data, caption=caption, width=width)


def _photos_of(storage, item_id: str) -> list:
    try:
        return storage.get_item_photos(item_id)
    except Exception:  # la foto nunca debe tumbar la pagina que la muestra
        return []


# ---------------------------------------------------------------------------
# Tomar o subir una foto
# ---------------------------------------------------------------------------

def render_capture(storage, user: dict, item: dict, key_prefix: str, default_kind: str = photos.KIND_REGISTRO) -> None:
    """Tipo de foto, camara o archivo, nota y «Guardar foto» (profesor/maestro)."""
    if not photos.can_add(user):
        return
    round_key = f"{key_prefix}_round"
    current = st.session_state.get(round_key, 0)
    base = f"{key_prefix}_{current}"
    notice = st.session_state.pop(f"{key_prefix}_notice", None)
    if notice:
        st.success(notice, icon=":material/photo_camera:")
    if not photos.github_enabled():
        st.warning(photos.LOCAL_ONLY_WARNING, icon=":material/cloud_off:")

    kind = st.radio(
        "Tipo de foto", photos.KINDS, index=photos.KINDS.index(default_kind), horizontal=True,
        format_func=lambda value: photos.KIND_LABELS[value], key=f"{base}_kind",
        help="Registro: al dar de alta el objeto · Estado: al dar salida o reingresar · Inventario: en un conteo.",
    )
    use_camera = st.toggle(
        "Usar la cámara aquí", key=f"{base}_camera_on",
        help="Enciende la cámara del navegador. En el celular, «Subir foto» también permite tomarla con la cámara.",
    )
    if use_camera:
        shot = st.camera_input("Toma la foto", key=f"{base}_camera")
    else:
        shot = st.file_uploader(
            "Subir foto (JPG o PNG)", type=list(photos.UPLOAD_TYPES), key=f"{base}_file",
            max_upload_size=photos.MAX_INPUT_MB,
            help="Se reduce a 1024 px y se le quitan los datos de ubicación (GPS) antes de guardarla.",
        )
    note = st.text_input("Nota (opcional)", key=f"{base}_note", max_chars=photos.MAX_NOTE_CHARS,
                         placeholder="Ej: caja completa, rayón en la carcasa...")
    if st.button(":material/save: Guardar foto", key=f"{base}_save", type="primary",
                 disabled=shot is None, use_container_width=True):
        with st.spinner("Guardando la foto..."):
            ok, message, _ = photos.add_photo(storage, item, shot.getvalue(), user, kind, note)
        if ok:
            # Widgets nuevos en la siguiente vuelta: la misma foto no se guarda dos veces.
            st.session_state[round_key] = current + 1
            st.session_state[f"{key_prefix}_notice"] = message
            st.rerun()
        st.error(message)


# ---------------------------------------------------------------------------
# Inventario: accion de las tarjetas y galeria del editor
# ---------------------------------------------------------------------------

def photo_popover(item: dict, key_prefix: str = "inv_photo") -> None:
    """«📷 Foto» / «📷 Fotos (n)» de una tarjeta del catalogo."""
    storage, user = st.session_state.storage, st.session_state.user
    try:
        count = storage.get_photo_counts().get(item["id"], 0)
    except Exception:
        count = 0
    popover = st.popover(
        f"Fotos ({count})" if count else "Foto", icon=":material/photo_camera:", type="tertiary",
        key=f"{key_prefix}_{item['id']}", on_change="rerun",
        help="Ver la última foto o agregar una (cámara o archivo)",
    )
    if not popover.open:
        return
    with popover:
        records = _photos_of(storage, item["id"])
        if records:
            latest = records[0]
            _show(photos.thumbnail_bytes(latest), photos.describe(latest, _names(storage, [latest])))
            st.caption(f"{len(records)} foto(s) · la galería completa está en «Editar».")
        else:
            st.caption("Todavía no tiene fotos.")
        render_capture(storage, user, item, key_prefix=f"{key_prefix}_{item['id']}_new",
                       default_kind=photos.KIND_INVENTARIO if records else photos.KIND_REGISTRO)


@st.dialog("Foto del objeto", width="large")
def _photo_dialog(record: dict, caption: str) -> None:
    _show(photos.photo_bytes(record), caption, "stretch")


def _delete_controls(storage, user: dict, record: dict) -> None:
    pending_key = "photo_delete_pending"
    if st.session_state.get(pending_key) != record["id"]:
        if st.button(":material/delete: Eliminar foto", key=f"photo_delete_{record['id']}", use_container_width=True):
            st.session_state[pending_key] = record["id"]
            st.rerun()
        return
    st.warning("¿Eliminar esta foto? El borrado queda en la cadena de custodia.")
    yes, no = st.columns(2)
    if yes.button("Sí, eliminar", key=f"photo_delete_yes_{record['id']}", type="primary", use_container_width=True):
        with st.spinner("Eliminando la foto..."):
            ok, message = photos.delete_photo(storage, record["id"], user)
        st.session_state[pending_key] = None
        st.session_state.photo_gallery_notice = (ok, message)
        st.rerun()
    if no.button("Cancelar", key=f"photo_delete_no_{record['id']}", use_container_width=True):
        st.session_state[pending_key] = None
        st.rerun()


def _photo_tile(storage, user: dict, record: dict, names: dict) -> None:
    with st.container(border=True):
        _show(photos.thumbnail_bytes(record))
        author = names.get(record.get("taken_by")) or record.get("taken_by") or ""
        when = traceability.fmt_local(record.get("taken_at"))
        st.markdown(
            f'<div style="text-align:center;">{badge_html(photos.KIND_LABELS.get(record.get("kind"), "Foto"), "info", "📷")}</div>',
            unsafe_allow_html=True,
        )
        st.caption(" · ".join(part for part in (when, author) if part))
        if record.get("note"):
            st.caption(f":material/notes: {record['note']}")
        if st.button(":material/zoom_in: Ver completa", key=f"photo_view_{record['id']}", use_container_width=True):
            _photo_dialog(record, photos.describe(record, names))
        if photos.can_delete(user):
            _delete_controls(storage, user, record)


def render_gallery(storage, user: dict, item: dict) -> None:
    """Galeria del editor del item: agregar foto y fotos, la mas reciente primero."""
    st.markdown("---")
    section_title("Fotos del objeto", icon="📷",
                  caption="La más reciente primero. Ayudan a reconocerlo y documentan su estado.")
    notice = st.session_state.pop("photo_gallery_notice", None)
    if notice:
        (st.success if notice[0] else st.error)(notice[1])
    records = _photos_of(storage, item["id"])
    if photos.can_add(user):
        with st.expander(":material/add_a_photo: Agregar foto", expanded=not records):
            render_capture(storage, user, item, key_prefix=f"gallery_photo_{item['id']}",
                           default_kind=photos.KIND_INVENTARIO if records else photos.KIND_REGISTRO)
    if not records:
        st.caption("Este objeto todavía no tiene fotos.")
        return

    visible = records
    if len(records) > GALLERY_LIMIT and not st.toggle(f"Mostrar las {len(records)} fotos", key=f"photo_all_{item['id']}"):
        visible = records[:GALLERY_LIMIT]
    photos.prefetch(visible)  # descarga en paralelo lo que falte en este servidor
    names = _names(storage, visible)
    st.caption(f"{len(records)} foto(s).")
    for start in range(0, len(visible), GALLERY_PER_ROW):
        row = visible[start:start + GALLERY_PER_ROW]
        for column, record in zip(centered_columns(len(row), GALLERY_PER_ROW), row):
            with column:
                _photo_tile(storage, user, record, names)


# ---------------------------------------------------------------------------
# Escanear
# ---------------------------------------------------------------------------

def render_scan_photo(item: dict, compact: bool = False, key_suffix: str = "") -> None:
    """Ultima foto del objeto escaneado (todos los roles) y, para profesor y
    maestro, «Foto de estado (opcional)». `compact`: miniatura (productos de un
    contenedor escaneado). `key_suffix` distingue varias tarjetas del mismo objeto en una
    pantalla (dos pedidos del mismo producto en Despacho)."""
    storage, user = st.session_state.storage, st.session_state.user
    records = _photos_of(storage, item["id"])
    if records:
        latest = records[0]
        caption = f"Así se ve · {photos.describe(latest, _names(storage, [latest]))}"
        if compact:
            _show(photos.thumbnail_bytes(latest), caption, photos.THUMB_SIDE)
        else:
            _show(photos.photo_bytes(latest), caption, SCAN_WIDTH)
    if not photos.can_add(user):
        return
    key = f"scan_photo_{item['id']}{'_' + key_suffix if key_suffix else ''}"
    popover = st.popover("Foto de estado (opcional)", icon=":material/add_a_photo:", type="tertiary",
                         key=key, on_change="rerun",
                         help="Documenta cómo está el objeto al dar salida o al reingresarlo.")
    if not popover.open:
        return
    with popover:
        if not records:
            st.caption(f"«{item.get('name')}» todavía no tiene fotos.")
        render_capture(storage, user, item, key_prefix=f"{key}_new", default_kind=photos.KIND_ESTADO)
