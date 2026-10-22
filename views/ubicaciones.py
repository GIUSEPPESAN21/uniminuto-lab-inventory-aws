# -*- coding: utf-8 -*-
"""views/ubicaciones.py - Estanterias, pisos, mesas de trabajo y zonas con
etiqueta propia (item_type "location", ver core/places.py).

- `render`: pestaña «Ubicaciones» de Inventario: mapa (arbol) con lo que guarda
  cada una, etiquetas imprimibles y altas: «Estantería 2 con 4 pisos» en una
  sola escritura, y mesas, exhibición Lego o zonas sueltas.
- `render_scan_result`: lo que se muestra en Escanear al leer la etiqueta de una
  ubicación (registrada o no): qué guarda y cómo llegar.

Solo profesor/maestro crean o editan; cualquiera que escanee ve lo que guarda.
"""

import pandas as pd
import streamlit as st

from core import barcode, labels, permissions, places
from core.ui import (
    badge_html, empty_state, esc, guard_cap, icon_html, load_error, page_header, section_title,
    stat_cards,
)
from views.location_guide import render_location_guide

FLASH_KEY = "places_flash"
_SUBTLE = "color:var(--subtle-text-color); font-size:0.85rem;"
MAX_LISTED = 40

KIND_TABLE = "Mesa de trabajo"
KIND_LEGO = "Exhibición Lego"
KIND_ZONE = "Zona, sala o subnivel (código libre)"
PLACE_KINDS = [KIND_TABLE, KIND_LEGO, KIND_ZONE]

_PLAN_COLUMNS = {"code": "Código", "kind": "Tipo", "name": "Nombre", "status": "Estado"}


# ---------------------------------------------------------------------------
# Piezas compartidas
# ---------------------------------------------------------------------------

def _plan_frame(plan: dict) -> pd.DataFrame:
    rows = [{label: row[key] for key, label in _PLAN_COLUMNS.items()} for row in plan["rows"]]
    return pd.DataFrame(rows, columns=list(_PLAN_COLUMNS.values()))


def _availability(storage) -> dict:
    try:
        return storage.get_availability_map()
    except Exception:
        return {}


def _contents_lines(contents: dict, availability: dict) -> list:
    """Texto (markdown) de lo que guarda una ubicacion: contenedores con sus
    productos y productos sueltos."""
    by_container = places.children_by_container(contents["products"])
    lines = []
    for container in contents["containers"]:
        kids = by_container.get(container["id"], [])
        lines.append(f"**{container.get('name')}** (`{container['id']}`) · {len(kids)} producto(s)")
        for kid in kids:
            lines.append(f"- {kid.get('name')} (`{kid['id']}`) · {availability.get(kid['id'], 0)} disponibles")
    for product in contents["loose"]:
        lines.append(f"- {product.get('name')} (`{product['id']}`) · {availability.get(product['id'], 0)} disponibles")
    return lines


def _render_contents(contents: dict, availability: dict) -> None:
    """Qué hay en una ubicacion (dentro de un expander o de la pagina de escaneo)."""
    inner = contents["sublocations"]
    if inner:
        st.markdown(
            "Dentro: " + " ".join(badge_html(f"{i.get('name')}", "neutral") for i in inner),
            unsafe_allow_html=True,
        )
    lines = _contents_lines(contents, availability)
    if not lines and not inner:
        st.caption("Todavía no hay contenedores ni productos registrados aquí.")
        return
    for line in lines[:MAX_LISTED]:
        st.markdown(line)
    if len(lines) > MAX_LISTED:
        st.caption(f"Se muestran {MAX_LISTED} de {len(lines)} líneas.")


def _label_popover(slot, item: dict, spec, key_prefix: str = "loc") -> None:
    label_pdf, label_png = labels.item_label_files(item, spec)
    if not label_png:
        slot.write("")
        return
    with slot.popover(":material/label:", help=f"Etiqueta {spec.size_text}", use_container_width=True):
        st.download_button(
            f"PDF {spec.size_text} · imprimir", data=label_pdf,
            file_name=f"etiqueta_{item['id']}.pdf", mime="application/pdf",
            key=f"{key_prefix}_label_pdf_{item['id']}", type="primary", use_container_width=True,
        )
        st.download_button(
            "PNG · solo archivo", data=label_png,
            file_name=f"etiqueta_{item['id']}.png", mime="image/png",
            key=f"{key_prefix}_label_png_{item['id']}", use_container_width=True,
        )
        st.caption("Pégala en la estantería, el piso o la mesa: al escanearla se confirma el punto de la ruta.")


def _labels_pdf_button(slot, items: list, spec, label: str, file_name: str, key: str) -> None:
    printable = [i for i in items if labels.item_label_files(i, spec)[1]]
    if not printable:
        return
    slot.download_button(
        label, data=lambda: labels.generate_labels_pdf_bytes(printable, spec, title="Etiquetas de ubicación"),
        file_name=file_name, mime="application/pdf", key=key, use_container_width=True,
    )


# ---------------------------------------------------------------------------
# Altas
# ---------------------------------------------------------------------------

def _create(storage, user: dict, plan: dict, summary: str) -> None:
    try:
        with st.spinner("Creando ubicaciones..."):
            created = storage.save_items_bulk(
                plan["payloads"], actor_email=user["institutional_email"], details=places.HISTORY_DETAILS,
                actor=user,
            )
    except ValueError as exc:
        st.error(str(exc))
        return
    st.session_state[FLASH_KEY] = f"{summary}: {len(created)} ubicación(es) creada(s). Ya puedes imprimir sus etiquetas."
    st.rerun()


def _render_create_shelf(storage, user: dict, items: list) -> None:
    c1, c2, c3 = st.columns([1, 1, 2])
    shelf = c1.number_input("Estantería", min_value=barcode.ESTANTERIA_MIN, max_value=barcode.ESTANTERIA_MAX,
                            value=1, step=1, key="places_shelf_number")
    floors = c2.number_input("Pisos", min_value=0, max_value=barcode.PISO_MAX, value=4, step=1,
                             key="places_shelf_floors", help="Se crean los pisos 1 al número elegido.")
    name = c3.text_input("Nombre (opcional)", key="places_shelf_name", placeholder=f"Estantería {int(shelf)}")
    description = st.text_area("Descripción (opcional)", key="places_shelf_description", height=68,
                               placeholder="Ej: Estantería de electrónica junto a la ventana")
    plan = places.plan_shelf(shelf, floors, storage.get_all_items(include_retired=True), name, description)
    if plan["errors"]:
        st.error("\n\n".join(plan["errors"]))
    st.dataframe(_plan_frame(plan), hide_index=True, use_container_width=True)
    new = len(plan["payloads"])
    if not new and not plan["errors"]:
        st.info("Todo esto ya está registrado. Para agregar pisos, súbele el número de pisos.")
    if st.button(f":material/check_circle: Crear {new} ubicación(es) en una sola sincronización",
                 type="primary", disabled=not new, key="places_shelf_create", use_container_width=True):
        _create(storage, user, plan, f"Estantería {int(shelf)}")


def _render_create_place(storage, user: dict, items: list) -> None:
    kind = st.radio("¿Qué ubicación quieres registrar?", PLACE_KINDS, horizontal=True, key="places_kind")
    if kind == KIND_TABLE:
        mesa = st.selectbox("Mesa", [1, 2], key="places_table_number")
        code = barcode.build_table_code(mesa)
    elif kind == KIND_LEGO:
        code = barcode.LEGO_ZONE_CODE
    else:
        code = st.text_input("Código", key="places_zone_code", placeholder="Ej: SALA-A",
                             help=f"Letras sin tildes, números y - _ . (hasta {barcode.ALNUM_MAX_LEN} caracteres).").strip()
    registered = [i for i in items if places.is_location(i)]
    options = {"Ninguna": ""} | {f"{i.get('name')} ({i['id']})": i["id"] for i in sorted(registered, key=places.code_order)}
    c1, c2 = st.columns(2)
    name = c1.text_input("Nombre", key="places_name", placeholder=places.default_name(code) or "Ej: Sala de electrónica")
    parent_label = c2.selectbox("Está dentro de", list(options), key="places_parent")
    description = st.text_area("Descripción (opcional)", key="places_description", height=68)
    plan = places.plan_location(code, name, storage.get_all_items(include_retired=True),
                                options[parent_label], description)
    if code:
        st.caption(f"Código: `{code}`")
    for problem in plan["errors"]:
        st.error(problem)
    if plan["rows"] and plan["rows"][0]["status"] == places.STATUS_EXISTS:
        st.info(f"{code} ya está registrada como «{plan['rows'][0]['name']}».")
    if st.button(":material/save: Registrar ubicación", type="primary", key="places_create",
                 disabled=not plan["payloads"], use_container_width=True):
        _create(storage, user, plan, plan["rows"][0]["name"])


# ---------------------------------------------------------------------------
# Mapa de ubicaciones
# ---------------------------------------------------------------------------

def _row_html(item: dict, depth: int, contents: dict) -> str:
    kind = places.kind_name(item) or places.KIND_NAMES[places.ZONE]
    pad = "&nbsp;&nbsp;&nbsp;&nbsp;" * depth + ("└ " if depth else "")
    badges = [badge_html(kind, "info")]
    badges.append(badge_html(f"{len(contents['containers'])} contenedor(es)", "neutral", "🗄️"))
    badges.append(badge_html(f"{len(contents['products'])} producto(s)", "neutral", "🧩"))
    html = (f'<div style="font-weight:700; line-height:1.3;">{pad}{icon_html("📍")} {esc(item.get("name"))}</div>'
            f'<div style="{_SUBTLE}">{pad}{esc(item["id"])}</div>'
            f'<div style="margin:0.35rem 0;">{" ".join(badges)}</div>')
    if (item.get("description") or "").strip():
        html += f'<div style="{_SUBTLE} white-space:pre-line;">{esc(item["description"].strip())}</div>'
    return html


def _render_tree(storage, user: dict, items: list, spec) -> None:
    tree = places.flatten(places.location_tree(items))
    if not tree:
        empty_state("Todavía no hay ubicaciones",
                    "Crea una estantería con sus pisos, una mesa o una zona para imprimir sus etiquetas.",
                    icon=":material/map:")
        return
    availability = _availability(storage)
    all_places = [node["item"] for node in tree]
    _labels_pdf_button(st, all_places, spec, f":material/print: PDF con las {len(all_places)} etiquetas",
                       "etiquetas_ubicaciones.pdf", "places_all_labels")
    for node in tree:
        item = node["item"]
        contents = places.contents_of(item["id"], items)
        with st.container(border=True):
            info, edit_col, label_col = st.columns([6, 1, 1])
            info.markdown(_row_html(item, node["depth"], contents), unsafe_allow_html=True)
            if edit_col.button(":material/edit:", key=f"places_edit_{item['id']}", help="Editar o eliminar",
                               use_container_width=True):
                st.session_state.editing_item_id = item["id"]
                st.rerun()
            _label_popover(label_col, item, spec)
            if node["children"]:
                group = [item] + [n["item"] for n in places.flatten(node["children"])]
                _labels_pdf_button(st, group, spec, f":material/print: Etiquetas de «{item.get('name')}» y lo que contiene ({len(group)})",
                                   f"etiquetas_{item['id']}.pdf", f"places_group_{item['id']}")
            with st.expander("Qué guarda"):
                _render_contents(contents, availability)


def render(storage, user: dict, spec) -> None:
    flash = st.session_state.pop(FLASH_KEY, None)
    if flash:
        st.success(f":material/check_circle: {flash}")
    try:
        items = storage.get_all_items()
    except Exception as exc:
        load_error("No se pudieron cargar las ubicaciones", exc)
        items = []
    counts = places.count_by_kind(items)
    stat_cards([
        {"label": "Estanterías", "value": counts[places.SHELF], "icon": "🗄️"},
        {"label": "Pisos", "value": counts[places.FLOOR], "icon": "📍"},
        {"label": "Mesas de trabajo", "value": counts[places.TABLE], "icon": "🛠️"},
        {"label": "Zonas y salas", "value": counts[places.ZONE], "icon": "🏫"},
    ])
    st.caption(
        "Cada estantería, piso, mesa o zona tiene su propia etiqueta con código de barras. Al escanearla "
        "muestra lo que guarda y, en la ruta verificable de Trazabilidad, el estudiante confirma ese punto "
        "escaneándola. Las ubicaciones no tienen stock ni se prestan."
    )
    section_title("Mapa de ubicaciones", icon=":material/map:")
    _render_tree(storage, user, items, spec)
    with st.expander(":material/add: Crear estantería con sus pisos", expanded=not items):
        _render_create_shelf(storage, user, items)
    with st.expander(":material/add_location: Registrar mesa de trabajo, exhibición Lego o zona"):
        _render_create_place(storage, user, items)
        st.caption(barcode.LOCATION_FORMAT_HELP)


def render_page() -> None:
    """Pagina «Ubicaciones» del menu lateral: el mismo gestor que la pestaña de
    Inventario, a un clic para profesor y maestro."""
    storage = st.session_state.storage
    user = st.session_state.user
    if not guard_cap(user, permissions.CAP_MANAGE_LOCATIONS, "las Ubicaciones"):
        return
    page_header(
        "Ubicaciones", icon=":material/map:",
        subtitle="Estanterías, pisos, mesas de trabajo y zonas con su código y su etiqueta",
    )
    render(storage, user, labels.load_label_spec(storage))


# ---------------------------------------------------------------------------
# Escanear
# ---------------------------------------------------------------------------

def _register_form(storage, user: dict, code: str, place: dict) -> None:
    """Alta rapida de una ubicacion cuyo codigo se acaba de escanear."""
    with st.form(f"places_scan_register_{code}"):
        name = st.text_input("Nombre", value=places.default_name(code))
        description = st.text_area("Descripción (opcional)", height=68)
        if st.form_submit_button(":material/save: Registrar ubicación", type="primary", use_container_width=True):
            plan = places.plan_location(code, name, storage.get_all_items(include_retired=True), "", description)
            if plan["errors"] or not plan["payloads"]:
                for problem in plan["errors"] or ["Esa ubicación ya está registrada."]:
                    st.error(problem)
                return
            try:
                storage.save_items_bulk(plan["payloads"], actor_email=user["institutional_email"],
                                        details=places.HISTORY_DETAILS, actor=user)
            except ValueError as exc:
                st.error(str(exc))
                return
            st.session_state.scan_result = barcode.scan(storage, code)
            st.rerun()


def render_scan_result(storage, user: dict, result: dict, spec, render_label=None) -> None:
    """Resultado de escanear una ubicacion: `found_location` (registrada) o
    `not_found` con `place` (codigo de ubicacion sin registrar)."""
    item = result.get("item")
    code = (item or {}).get("id") or result.get("barcode") or ""
    place = result.get("place") or {}
    all_items = storage.get_all_items()
    availability = _availability(storage)
    if item:
        st.success(f":material/location_on: {places.kind_name(item) or 'Ubicación'}: **{item['name']}** (`{item['id']}`)")
    else:
        st.warning(f"El código `{code}` es de una ubicación ({barcode.describe_location(place)}) que todavía no está registrada.")
    st.caption(f":material/menu_book: {barcode.describe_location(place)}")
    if item and item.get("description"):
        st.caption(item["description"])
    if item and render_label:
        render_label(item)
    if item:
        render_location_guide(item, key_prefix=f"scan_place_guide_{code}")
    elif not permissions.has_cap(user, permissions.CAP_MANAGE_LOCATIONS):
        st.info("Pide a un supervisor o al administrador del laboratorio que registre esta ubicación.")
    else:
        _register_form(storage, user, code, place)
    section_title("Qué guarda", icon=":material/inventory_2:")
    _render_contents(places.contents_of(code, all_items), availability)
