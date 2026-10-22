# -*- coding: utf-8 -*-
"""views/inventario.py - Catalogo agrupado por contenedor, alta/edicion/eliminacion
de items y contenedores, generador de productos desde la descripcion de un
contenedor, importacion CSV y configuracion de etiquetas.
Solo profesor/maestro pueden crear, editar o eliminar."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from core import barcode
from core import inventory_suggestions as suggestions
from core import labels
from core import permissions, pieces
from core.labels import (
    ITEM_TYPE_BY_CHOICE, ITEM_TYPE_CHOICES, ITEM_TYPE_HELP, ITEM_TYPE_ICONS, ITEM_TYPE_LABELS, ITEM_TYPE_NAMES,
)
from core.ui import (
    badge_html, centered_columns, empty_state, esc, guard_cap, icon_html, load_error, more_button, page_header, paginate,
    persistent_tabs, section_title, stat_cards, timeline,
)
from views import label_settings, photo_panel, ubicaciones
from views.code_input import render_code_input

ALL = "Todos"
VIEW_GROUPED = ":material/view_agenda: Por contenedor"
VIEW_LIST = ":material/view_list: Lista"

STOCK_PENDING = "Pendiente de conteo"
STOCK_EMPTY = "Sin disponibles"
STOCK_LOW = "Stock bajo"
STOCK_OK = "Disponible"
STOCK_FILTERS = [ALL, STOCK_OK, STOCK_LOW, STOCK_EMPTY, STOCK_PENDING]

GENERATOR_KEY = "inv_generate_for"        # id del contenedor con el generador abierto
GENERATOR_RESULT_KEY = "inv_generate_result"
GENERATOR_LABEL = ":material/auto_awesome: Generar productos desde la descripción"

_SUBTLE = "color:var(--subtle-text-color); font-size:0.85rem;"


def _delete_warning(impact: dict) -> str:
    """Texto de advertencia previo a eliminar (lo que se borrara de verdad)."""
    parts = [
        "Eliminar es **permanente**: se borran el item, su historial y sus préstamos ya devueltos, "
        "y su código queda libre para registrarse de nuevo."
    ]
    if impact.get("item_type") == "location":
        if impact.get("contained_items"):
            parts.append(
                f"Al ser una ubicación, también se eliminarán las {impact['contained_items']} ubicación(es) que "
                "contiene (los productos guardados en ella no se tocan)."
            )
    elif impact.get("contained_items"):
        parts.append(
            f"Al ser un {ITEM_TYPE_NAMES['master']}, también se eliminarán los "
            f"{impact['contained_items']} item(s) que contiene."
        )
    if impact.get("open_requests"):
        parts.append(
            f"Se cancelarán {impact['open_requests']} solicitud(es) pendiente(s) o aprobada(s) de este producto."
        )
    if impact.get("open_loans"):
        parts.append(
            f":material/warning: Hay {impact['open_loans']} préstamo(s) abierto(s): registra primero su reingreso; "
            "mientras tanto no se podrá eliminar."
        )
    return " ".join(parts)


def _edit_item_form(storage, item: dict, user: dict):
    st.markdown(f'<h2 style="text-align:center;">{icon_html("✏️")} Editando: {esc(item.get("name"))}</h2>',
                unsafe_allow_html=True)
    impact = storage.get_delete_impact(item["id"])
    if suggestions.is_pending_count(item):
        st.info(":material/hourglass_top: Este producto está pendiente de conteo: escribe su **Cantidad total** y guarda.")
    with st.form("edit_item_form"):
        name = st.text_input("Nombre", value=item.get("name", ""))
        category = st.text_input("Categoria", value=item.get("category", ""))
        description = st.text_area("Descripcion", value=item.get("description", ""))
        location = st.text_input("Ubicacion", value=item.get("location", ""))

        quantity, min_alert = item.get("quantity", 0), item.get("min_stock_alert", 0)
        loose_pieces = pieces.enabled(item)
        if item.get("item_type") not in ("master", "location"):
            quantity = st.number_input("Cantidad total", value=int(item.get("quantity", 0)), min_value=0, step=1)
            min_alert = st.number_input("Umbral de alerta", value=int(item.get("min_stock_alert", 0)), min_value=0, step=1)
            loose_pieces = st.checkbox("Se retiran por piezas (Lego, bloques, kits…)", value=pieces.enabled(item), key=f"edit_pieces_{item['id']}",
                                       help="Las personas dicen cuántas piezas se llevan y cuántas devuelven, sin dar de alta cada pieza ni tocar el stock.")
            if not pieces.enabled(item) and pieces.looks_like_pieces(item):
                st.caption(":material/lightbulb: Parece un producto que se usa por piezas: te conviene activar la casilla.")

        st.markdown("---")
        st.caption(_delete_warning(impact))
        confirm_delete = st.checkbox("Entiendo que se eliminará de forma permanente", key=f"confirm_delete_{item['id']}")
        c1, c2, c3 = st.columns(3)
        save = c1.form_submit_button("Guardar cambios", type="primary", use_container_width=True)
        delete = c2.form_submit_button(":material/delete: Eliminar definitivamente", use_container_width=True)
        cancel = c3.form_submit_button("Cancelar", use_container_width=True)

        if save:
            if not name:
                st.warning("El nombre no puede estar vacio.")
            else:
                data = {
                    **item, "name": name, "category": category, "description": description,
                    "location": location, "quantity": int(quantity), "min_stock_alert": int(min_alert),
                    "loose_pieces": "1" if loose_pieces else "",
                }
                try:
                    storage.save_item(data, item["id"], is_new=False, actor_email=user["institutional_email"],
                                      actor=user)
                    st.success("Item actualizado.")
                    st.session_state.editing_item_id = None
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

        if delete:
            if not confirm_delete:
                st.warning("Marca la casilla de confirmación para eliminar el item de forma permanente.")
            else:
                ok, msg = storage.delete_item(item["id"], actor_email=user["institutional_email"], actor=user)
                if ok:
                    st.toast(msg, icon=":material/delete:")
                    st.session_state.editing_item_id = None
                    st.session_state.scan_result = None  # evita mostrar el item recien eliminado
                    st.rerun()
                else:
                    st.error(msg)

        if cancel:
            st.session_state.editing_item_id = None
            st.rerun()

    photo_panel.render_gallery(storage, user, item)


# ---------------------------------------------------------------------------
# Catalogo: estado de stock, filtros y tarjetas
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Filters:
    search: str = ""
    category: str = ALL
    location: str = ALL
    item_type: str = ALL
    stock: str = ALL


def _stock_state(item: dict, available: int):
    """(texto, tono, icono) del stock de un producto; None para un contenedor."""
    if item.get("item_type") == "master":
        return None
    if suggestions.is_pending_count(item):
        return STOCK_PENDING, "warning", "⏳"
    if available <= 0:
        return STOCK_EMPTY, "danger", "⛔"
    if item.get("min_stock_alert", 0) and available <= item.get("min_stock_alert", 0):
        return STOCK_LOW, "warning", "⚠️"
    return STOCK_OK, "success", "✅"


def _code_order(item: dict) -> tuple:
    """Orden natural por codigo (2-1-02 antes que 2-1-10): sigue la estanteria y,
    dentro de un contenedor, el orden de sus cajas."""
    parts = re.split(r"(\d+)", str(item.get("id") or ""))
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts if p)


def _location_core(location) -> str:
    return str(location or "").strip().rstrip(" .;,")


def _location_within(location, selected: str) -> bool:
    """La ubicacion es la elegida o esta dentro de ella ("...Contenedor-1; Caja 02"
    esta dentro de "...Contenedor-1", pero "...Contenedor-10" no)."""
    loc, base = _location_core(location), _location_core(selected)
    return loc == base or (loc.startswith(base) and loc[len(base)] in ";,·")


def _search_hit(item: dict, search: str) -> bool:
    haystack = " ".join(str(item.get(k) or "") for k in ("name", "category", "id", "location")).lower()
    return search in haystack


def _matches(item: dict, flt: _Filters, available: int, ignore_search: bool = False) -> bool:
    if flt.search and not ignore_search and not _search_hit(item, flt.search):
        return False
    if flt.category != ALL and item.get("category") != flt.category:
        return False
    if flt.location != ALL and not _location_within(item.get("location"), flt.location):
        return False
    if flt.item_type != ALL and ITEM_TYPE_LABELS.get(item.get("item_type")) != flt.item_type:
        return False
    if flt.stock != ALL:
        state = _stock_state(item, available)
        if not state or state[0] != flt.stock:
            return False
    return True


def _label_popover(slot, item: dict, spec, label: str = ":material/label:") -> None:
    label_pdf, label_png = labels.item_label_files(item, spec)
    if not label_png:
        slot.write("")
        return
    with slot.popover(label, help=f"Etiqueta {spec.size_text}", use_container_width=True):
        st.download_button(
            f"PDF {spec.size_text} · imprimir", data=label_pdf,
            file_name=f"etiqueta_{item['id']}.pdf", mime="application/pdf",
            key=f"label_pdf_{item['id']}", type="primary", use_container_width=True,
        )
        st.download_button(
            "PNG · solo archivo", data=label_png,
            file_name=f"etiqueta_{item['id']}.png", mime="image/png",
            help="Para guardar o integrar. No lo imprimas desde la app Fotos: "
                 "la recorta y la agranda. Para imprimir usa el PDF.",
            key=f"label_png_{item['id']}", use_container_width=True,
        )
        st.caption(
            f"Abre el PDF con Edge o Chrome (Ctrl + P): papel USER de "
            f"{spec.size_text}, escala Predeterminado, sin márgenes. "
            "¿Sale mal? Revisa la pestaña :material/print: Etiquetas."
        )


def _edit_button(slot, item: dict, label: str = ":material/edit:") -> None:
    # Solo el boton de icono lleva ayuda: `help` envuelve el boton en un tooltip y
    # style.css (".stButton > button") deja de aplicarle el estilo de la app.
    help_text = "Editar o eliminar" if label == ":material/edit:" else None
    if slot.button(label, key=f"edit_{item['id']}", help=help_text, use_container_width=True):
        st.session_state.editing_item_id = item["id"]
        st.rerun()


def _title_html(item: dict, size: str = "1rem") -> str:
    icon = ITEM_TYPE_ICONS.get(item.get("item_type"), "")
    return (
        f'<div style="font-weight:700; font-size:{size}; line-height:1.3;">{icon_html(icon)} {esc(item.get("name"))}</div>'
        f'<div style="{_SUBTLE}">{esc(item.get("id"))}</div>'
    )


def _stock_line(item: dict, available: int) -> str:
    if suggestions.is_pending_count(item):
        return f"Cantidad por contar · {item.get('unit') or 'unidad'}"
    return f"Disponibles {available} de {item.get('quantity', 0)} · {item.get('unit') or 'unidad'}"


def _product_badges(item: dict, available: int, show_type: bool) -> str:
    badges = []
    if show_type:
        badges.append(badge_html(ITEM_TYPE_NAMES.get(item.get("item_type"), item.get("item_type")), "neutral",
                                 ITEM_TYPE_ICONS.get(item.get("item_type"))))
    state = _stock_state(item, available)
    if state:
        badges.append(badge_html(state[0], state[1], state[2]))
    return " ".join(badges)


def _product_card(item: dict, available: int, spec, show_type: bool = False) -> None:
    """Tarjeta compacta de un producto (en la cuadricula de su contenedor)."""
    with st.container(border=True):
        st.markdown(
            _title_html(item) + f'<div style="margin:0.35rem 0;">{_product_badges(item, available, show_type)}</div>',
            unsafe_allow_html=True,
        )
        st.caption(_stock_line(item, available))
        if item.get("location"):
            st.caption(f":material/location_on: {item.get('location')}")
        photo_panel.photo_popover(item)
        edit_col, label_col = st.columns(2)
        _edit_button(edit_col, item, ":material/edit: Editar")
        _label_popover(label_col, item, spec, ":material/label: Etiqueta")


def _product_grid(items: list, availability: dict, spec, show_type: bool = False, per_row: int = 3) -> None:
    """Tarjetas en filas de `per_row`, todas del mismo ancho y alto por fila; la
    ultima fila incompleta queda centrada (en celular se apilan)."""
    if not items:
        return
    with st.container(key=f"lab_product_grid_{items[0]['id']}"):
        for start in range(0, len(items), per_row):
            row = items[start:start + per_row]
            for column, item in zip(centered_columns(len(row), per_row), row):
                with column:
                    _product_card(item, availability.get(item["id"], 0), spec, show_type=show_type)


def _item_row(item: dict, available: int, spec) -> None:
    """Fila de un producto en la vista de lista (los contenedores usan su tarjeta)."""
    with st.container(border=True):
        info, stock, edit_col, label_col = st.columns([5, 3, 1, 1])
        badges = _product_badges(item, available, show_type=True)
        info.markdown(_title_html(item) + f'<div style="margin-top:0.35rem;">{badges}</div>', unsafe_allow_html=True)
        stock.caption(" · ".join(filter(None, [item.get("category") or "Sin categoría", item.get("location")])))
        stock.caption(_stock_line(item, available))
        with stock:
            photo_panel.photo_popover(item)
        _edit_button(edit_col, item)
        _label_popover(label_col, item, spec)


def _container_header(master: dict, children: list, availability: dict, suggested: int) -> None:
    states = Counter((_stock_state(c, availability.get(c["id"], 0)) or ("",))[0] for c in children)
    badges = [badge_html(master.get("category") or "Sin categoría", "info")]
    badges.append(badge_html(f"{len(children)} producto(s)", "neutral", "🧩"))
    if states[STOCK_PENDING]:
        badges.append(badge_html(f"{states[STOCK_PENDING]} por contar", "warning", "⏳"))
    alerts = states[STOCK_LOW] + states[STOCK_EMPTY]
    if alerts:
        badges.append(badge_html(f"{alerts} con stock bajo o agotado", "danger", "⚠️"))
    if suggested:
        badges.append(badge_html(f"{suggested} sugerido(s) desde la descripción", "info", "✨"))
    html = _title_html(master, size="1.15rem") + f'<div style="margin:0.4rem 0 0.2rem;">{" ".join(badges)}</div>'
    if master.get("location"):
        html += f'<div style="{_SUBTLE}">{icon_html("📍")} {esc(master.get("location"))}</div>'
    if (master.get("description") or "").strip():
        html += (
            f'<div style="{_SUBTLE} white-space:pre-line; margin-top:0.35rem; padding-left:0.6rem; '
            f'border-left:3px solid var(--border-color);">{icon_html("📝")} {esc(master.get("description").strip())}</div>'
        )
    st.markdown(html, unsafe_allow_html=True)


def _toggle_generator(master_id: str) -> None:
    current = st.session_state.get(GENERATOR_KEY)
    st.session_state[GENERATOR_KEY] = None if current == master_id else master_id


def _children_labels_button(slot, storage, master: dict, spec) -> None:
    """Un PDF con la etiqueta del contenedor y la de TODOS sus productos (no solo los que dejan ver los filtros),
    en seguidilla: igual que el PDF de una estantería con sus pisos en Ubicaciones."""
    group = labels.container_label_set(master, storage.get_children(master["id"]))
    if len(group) < 2:         # sin productos imprimibles basta el popover «Etiqueta» del propio contenedor
        return
    slot.download_button(
        f":material/print: Etiquetas del contenedor y sus {len(group) - 1} productos ({len(group)})",
        data=lambda: labels.generate_container_labels_pdf_bytes(master, storage.get_children(master["id"]), spec),
        file_name=f"etiquetas_{master['id']}.pdf", mime="application/pdf", key=f"inv_kids_pdf_{master['id']}",
        use_container_width=True,
        help="Una página por etiqueta, del tamaño real configurado: primero el contenedor y luego sus productos "
             "por código, para imprimirlas de corrido.",
    )


def _container_card(storage, user, master: dict, children: list, availability: dict, spec,
                    suggested: int, show_children: bool = True) -> None:
    """Contenedor Principal con sus productos y, si tiene descripcion, el
    generador de productos."""
    with st.container(border=True):
        _container_header(master, children, availability, suggested)
        photo_panel.photo_popover(master)
        has_description = bool((master.get("description") or "").strip())
        is_open = st.session_state.get(GENERATOR_KEY) == master["id"]
        edit_col, label_col, kids_col, gen_col = st.columns([1, 1, 3, 3])
        _edit_button(edit_col, master, ":material/edit: Editar")
        _label_popover(label_col, master, spec, ":material/label: Etiqueta")
        _children_labels_button(kids_col, storage, master, spec)
        if has_description:
            gen_col.button(
                ":material/close: Cerrar el generador" if is_open else GENERATOR_LABEL,
                key=f"inv_gen_toggle_{master['id']}", use_container_width=True,
                type="secondary" if is_open or not suggested else "primary",
                on_click=_toggle_generator, args=(master["id"],),
            )
        if is_open and has_description:
            _render_generator(storage, user, master)

        if not show_children:
            return
        if not children:
            st.caption(
                "Este contenedor todavía no tiene productos."
                + (" Usa «:material/auto_awesome: Generar productos desde la descripción» para crearlos." if has_description else "")
            )
            return
        _product_grid(children, availability, spec)


# ---------------------------------------------------------------------------
# Generador de productos desde la descripcion
# ---------------------------------------------------------------------------

def _editor_key(master_id: str, records: list) -> str:
    """Clave del editor ligada al contenido propuesto: si cambian las propuestas
    (p. ej. tras crear algunas), el editor arranca limpio en vez de aplicar
    ediciones viejas a filas distintas."""
    digest = hashlib.sha1(json.dumps(records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
    return f"inv_gen_editor_{master_id}_{digest}"


def _close_generator() -> None:
    st.session_state[GENERATOR_KEY] = None


def _render_generator(storage, user: dict, master: dict) -> None:
    section_title(
        "Generar productos desde la descripción", icon=":material/auto_awesome:",
        caption="Revisa la propuesta: puedes desmarcar, renombrar, cambiar el código y escribir la cantidad. "
                "Nada se guarda hasta que pulses «Crear».",
    )
    try:
        all_items = storage.get_all_items(include_retired=True)  # tambien los dados de baja: sus codigos no se proponen
    except Exception as exc:
        load_error("No se pudo leer el inventario", exc)
        return
    analysis = suggestions.suggest_products(master, all_items)
    proposals = analysis["proposals"]

    if analysis["existing"]:
        st.caption(
            "Ya registrados en este contenedor (no se duplican): "
            + ", ".join(f"{e['existing_name']} ({e['id']})" for e in analysis["existing"])
        )
    for warning in analysis["warnings"]:
        st.warning(warning)
    if not proposals:
        empty_state(
            "No hay productos nuevos por crear",
            "Todo lo que describe el contenedor ya está registrado."
            if analysis["existing"] else "La descripción no menciona productos que se puedan reconocer.",
            icon=":material/check_circle:",
        )
        st.button("Cerrar", key=f"inv_gen_close_{master['id']}", on_click=_close_generator)
        return

    records = suggestions.editor_records(proposals)
    edited = st.data_editor(
        pd.DataFrame(records, columns=list(suggestions.EDITOR_COLUMNS)),
        key=_editor_key(master["id"], records),
        hide_index=True, num_rows="fixed", use_container_width=True,
        height=min(38 + 35 * len(records), 600),  # todas las filas a la vista (hasta ~16)
        disabled=[suggestions.COL_FEATURE, suggestions.COL_NOTES],
        column_config={
            suggestions.COL_CREATE: st.column_config.CheckboxColumn(
                suggestions.COL_CREATE, help="Desmarca lo que no quieras crear.", width="small"),
            suggestions.COL_CODE: st.column_config.TextColumn(
                suggestions.COL_CODE, required=True, max_chars=20, width="medium",
                help="Código GLIOPS de nivel caja dentro de este contenedor."),
            suggestions.COL_NAME: st.column_config.TextColumn(
                suggestions.COL_NAME, required=True, max_chars=80, width="large"),
            suggestions.COL_FEATURE: st.column_config.TextColumn(suggestions.COL_FEATURE, width="medium"),
            suggestions.COL_QUANTITY: st.column_config.NumberColumn(
                suggestions.COL_QUANTITY, min_value=0, step=1, format="%d",
                help="Si la descripción no trae la cantidad: 0 = pendiente de conteo."),
            suggestions.COL_UNIT: st.column_config.TextColumn(suggestions.COL_UNIT, max_chars=20, width="small"),
            suggestions.COL_NOTES: st.column_config.TextColumn(suggestions.COL_NOTES, width="large"),
        },
    )
    rows = suggestions.rows_from_records(edited.to_dict(orient="records"), proposals)
    check = suggestions.validate_rows(rows, master, all_items)
    payloads, errors = check["payloads"], check["errors"]

    notes = [(p["name"], note) for p in proposals for note in p["notes"]]
    if notes:
        with st.expander(f":material/warning: Puntos para confirmar ({len(notes)})", expanded=True):
            for name, note in notes:
                st.markdown(f"- **{esc(name)}** — {esc(note)}")

    pending = sum(1 for p in payloads if not p["quantity"])
    ready = bool(payloads) and not errors
    timeline([
        {"title": "Descripción analizada", "detail": f"{len(proposals)} producto(s) propuestos", "state": "done"},
        {"title": "Revisión", "detail": f"{check['selected']} marcado(s) para crear",
         "state": "done" if check["selected"] else "current"},
        {"title": "Validación",
         "detail": f"{len(errors)} error(es) por corregir" if errors else (
             "Códigos, nombres y cantidades correctos" if check["selected"] else "Marca al menos un producto"),
         "state": "blocked" if errors else ("done" if check["selected"] else "pending")},
        {"title": "Crear en una sola sincronización",
         "detail": (f"{len(payloads)} producto(s)" + (f", {pending} pendiente(s) de conteo" if pending else ""))
         if ready else "Disponible cuando no haya errores",
         "state": "current" if ready else "pending"},
    ])
    if errors:
        st.error("Corrige estos puntos antes de crear:\n\n" + "\n".join(f"- {error}" for error in errors))
    for warning in check["warnings"]:
        st.warning(warning)

    create_col, cancel_col = st.columns([2, 1])
    create = create_col.button(
        f":material/check_circle: Crear {len(payloads)} producto(s)", type="primary", disabled=not ready,
        key=f"inv_gen_create_{master['id']}", use_container_width=True,
    )
    cancel_col.button("Cancelar", key=f"inv_gen_cancel_{master['id']}", use_container_width=True,
                      on_click=_close_generator)
    if create and ready:
        try:
            with st.spinner("Creando productos..."):
                created = storage.save_items_bulk(
                    payloads, actor_email=user["institutional_email"], actor=user,
                    details=suggestions.HISTORY_DETAILS.format(parent_id=master["id"]),
                )
        except ValueError as exc:
            st.error(str(exc))
            return
        message = f"Se crearon {len(created)} producto(s) en «{master.get('name')}»"
        message += f"; {pending} quedan pendientes de conteo." if pending else "."
        st.session_state[GENERATOR_KEY] = None
        st.session_state[GENERATOR_RESULT_KEY] = message
        st.rerun()


# ---------------------------------------------------------------------------
# Pestañas
# ---------------------------------------------------------------------------

def _render_catalog(storage, user: dict, spec) -> None:
    result = st.session_state.pop(GENERATOR_RESULT_KEY, None)
    if result:
        st.success(f":material/auto_awesome: {result}")

    try:
        # Las ubicaciones (estanterias, pisos, mesas) tienen su propia pestaña: no son productos.
        items = [i for i in storage.get_all_items() if i.get("item_type") != "location"]
    except Exception as e:
        load_error("No se pudo cargar el inventario", e)
        items = []
    availability = storage.get_availability_map() if items else {}

    masters = sorted((i for i in items if i.get("item_type") == "master"), key=_code_order)
    master_ids = {m["id"] for m in masters}
    products = [i for i in items if i.get("item_type") != "master"]
    states = Counter((_stock_state(i, availability.get(i["id"], 0)) or ("",))[0] for i in products)
    suggested = {
        m["id"]: len(suggestions.suggest_products(m, items)["proposals"])
        for m in masters if (m.get("description") or "").strip()
    }
    stat_cards([
        {"label": "Contenedores", "value": len(masters), "icon": ITEM_TYPE_ICONS["master"]},
        {"label": "Productos", "value": len(products), "icon": ITEM_TYPE_ICONS["child"]},
        {"label": "Unidades disponibles", "value": sum(availability.get(i["id"], 0) for i in products), "icon": "📦"},
        {"label": "Stock bajo o agotado", "value": states[STOCK_LOW] + states[STOCK_EMPTY], "icon": "⚠️",
         "tone": "danger" if states[STOCK_LOW] + states[STOCK_EMPTY] else "neutral"},
        {"label": "Pendientes de conteo", "value": states[STOCK_PENDING], "icon": "⏳",
         "tone": "warning" if states[STOCK_PENDING] else "neutral"},
    ])
    with_suggestions = sum(1 for count in suggested.values() if count)
    if with_suggestions:
        st.info(
            f":material/auto_awesome: {with_suggestions} contenedor(es) describen productos que aún no están registrados. "
            f"Abre «{GENERATOR_LABEL}» en su tarjeta para revisarlos y crearlos."
        )

    children_by_parent = defaultdict(list)
    for item in sorted(products, key=_code_order):
        if item.get("item_type") == "child" and item.get("parent_id") in master_ids:
            children_by_parent[item["parent_id"]].append(item)
    loose = [i for i in products if not (i.get("item_type") == "child" and i.get("parent_id") in master_ids)]

    f1, f2, f3 = st.columns([3, 2, 2])
    search = f1.text_input("Buscar por nombre, categoria, codigo o ubicacion", key="inv_search")
    categories = sorted({i.get("category") for i in items if i.get("category")})
    category = f2.selectbox("Categoria", [ALL] + categories, key="inv_category")
    item_type = f3.selectbox(
        "Tipo", [ALL] + [label for key, label in ITEM_TYPE_LABELS.items() if key != "location"], key="inv_type",
    )
    f4, f5, f6 = st.columns([3, 2, 2])
    top_locations = sorted({
        i.get("location") for i in masters + loose if i.get("location")
    })
    location = f4.selectbox("Ubicacion", [ALL] + top_locations, key="inv_location",
                            help="Incluye lo que está dentro de esa ubicación.")
    stock = f5.selectbox("Estado del stock", STOCK_FILTERS, key="inv_stock")
    view = f6.radio("Vista", [VIEW_GROUPED, VIEW_LIST], horizontal=True, key="inv_view")
    flt = _Filters((search or "").strip().lower(), category, location, item_type, stock)

    def available(item):
        return availability.get(item["id"], 0)

    if view == VIEW_LIST:
        visible = [i for i in items if _matches(i, flt, available(i))]
        st.caption(f"{len(visible)} item(s) encontrados.")
        if not visible:
            empty_state("No se encontraron items", "Prueba con otros filtros o registra uno nuevo.", icon=":material/search:")
        for item in paginate(visible, "inv_list", 30):
            if item.get("item_type") == "master":
                _container_card(storage, user, item, children_by_parent.get(item["id"], []), availability, spec,
                                suggested.get(item["id"], 0), show_children=False)
            else:
                _item_row(item, available(item), spec)
        more_button(len(visible), "inv_list", 30)
        return

    groups, found = [], 0
    for master in masters:
        master_hit = _matches(master, flt, 0)
        whole_container = bool(flt.search) and _search_hit(master, flt.search)
        kids = [c for c in children_by_parent.get(master["id"], [])
                if _matches(c, flt, available(c), ignore_search=whole_container)]
        if master_hit or kids:
            groups.append((master, kids, master_hit))
            found += int(master_hit) + len(kids)
    loose_visible = [i for i in loose if _matches(i, flt, available(i))]
    found += len(loose_visible)

    st.caption(f"{found} item(s) encontrados.")
    if not groups and not loose_visible:
        empty_state("No se encontraron items", "Prueba con otros filtros o registra uno nuevo.", icon=":material/search:")
        return
    if groups:
        section_title("Contenedores Principales", icon=ITEM_TYPE_ICONS["master"],
                      caption="Cada contenedor con los productos que guarda.")
        everything = [(master, children_by_parent.get(master["id"], [])) for master, _kids, _hit in groups]
        total = sum(len(labels.container_label_set(master, children)) for master, children in everything)
        if total > 1:
            st.download_button(
                f":material/print: PDF con las {total} etiquetas de estos {len(groups)} contenedor(es) y sus productos",
                data=lambda: labels.generate_catalog_labels_pdf_bytes(everything, spec),
                file_name="etiquetas_contenedores.pdf", mime="application/pdf", key="inv_all_labels_pdf",
                use_container_width=True,
                help="Cada contenedor seguido de sus productos, una etiqueta por página. Respeta los filtros: "
                     "los contenedores que ves arriba.")
    for master, kids, _hit in paginate(groups, "inv_groups", 15):
        filtered = len(kids) != len(children_by_parent.get(master["id"], []))
        _container_card(storage, user, master, kids, availability, spec, suggested.get(master["id"], 0))
        if filtered:
            st.caption(f"Mostrando {len(kids)} de {len(children_by_parent[master['id']])} producto(s) "
                       f"de «{master.get('name')}» por los filtros.")
    more_button(len(groups), "inv_groups", 15)
    if loose_visible:
        section_title("Ítems sin contenedor", icon=ITEM_TYPE_ICONS["standalone"],
                      caption="Ítems individuales y productos cuyo contenedor no está activo.")
        _product_grid(paginate(loose_visible, "inv_loose", 30), availability, spec, show_type=True)
        more_button(len(loose_visible), "inv_loose", 30)


def _render_new_item(storage, user: dict) -> None:
    st.caption("También puedes registrar un item nuevo directamente escaneando su código en la sección Buscar.")
    item_kind = st.radio("¿Qué quieres registrar?", ITEM_TYPE_CHOICES, horizontal=True, key="inv_new_kind")
    item_type = ITEM_TYPE_BY_CHOICE[item_kind]
    st.caption(ITEM_TYPE_HELP[item_type])

    # Fuera del formulario para que formato, numeracion y vista previa se
    # actualicen inmediatamente en cada interaccion.
    new_id = render_code_input(storage, item_type, key_prefix="inv_new_code")

    with st.form("inv_new_item_form"):
        name_label = "Nombre / Característica" if item_kind == ITEM_TYPE_NAMES["child"] else "Nombre"
        name_placeholder = "Ej: Resistencias 220 Ω" if item_kind == ITEM_TYPE_NAMES["child"] else None
        name = st.text_input(name_label, placeholder=name_placeholder)
        category = st.text_input("Categoria")
        is_master = item_kind == ITEM_TYPE_NAMES["master"]
        description = st.text_area(
            "Descripcion", height=80,
            placeholder="Ej: Contiene piezas Lego de pines 4x2 - 2x2 y una caja con puertas y ventanas"
            if is_master else None,
            help="Describe lo que guarda el contenedor: luego, en el Catálogo, «:material/auto_awesome: Generar productos "
                 "desde la descripción» te propondrá sus productos." if is_master else None,
        )
        location = st.text_input("Ubicacion fisica")

        parent_id = ""
        if item_kind == ITEM_TYPE_NAMES["child"]:
            masters = storage.get_all_masters()
            options = {f"{m['name']} ({m['id']})": m["id"] for m in masters}
            if options:
                choice = st.selectbox(ITEM_TYPE_NAMES["master"], list(options.keys()))
                parent_id = options.get(choice, "")
            else:
                st.warning("Todavía no hay Contenedores Principales creados.")

        quantity, min_alert, loose_pieces = 0, 0, False
        if item_kind != ITEM_TYPE_NAMES["master"]:
            quantity = st.number_input("Cantidad inicial", min_value=0, step=1, value=1, key="inv_new_qty")
            min_alert = st.number_input("Umbral de alerta", min_value=0, step=1, value=0, key="inv_new_alert")
            loose_pieces = st.checkbox("Se retiran por piezas (Lego, bloques, kits…)", key="inv_new_pieces",
                                       help="Las personas dicen cuántas piezas se llevan y cuántas devuelven, sin dar de alta cada pieza ni tocar el stock.")

        submitted = st.form_submit_button(":material/save: Registrar", type="primary", use_container_width=True)

        if submitted:
            new_id = (new_id or "").strip()
            code_error = None
            if new_id:
                try:
                    barcode.validate_code_format(new_id)
                except ValueError as e:
                    code_error = str(e)

            if not new_id or not name:
                st.error("Codigo de barras y nombre son obligatorios.")
            elif code_error:
                st.error(code_error)
            elif storage.item_code_in_use(new_id):
                st.error("Ya existe un item con ese codigo.")
            elif item_kind == ITEM_TYPE_NAMES["child"] and not parent_id:
                st.error(f"Debes seleccionar un {ITEM_TYPE_NAMES['master']}.")
            else:
                data = {
                    "name": name, "category": category, "description": description,
                    "item_type": item_type, "parent_id": parent_id, "unit": "unidad",
                    "quantity": int(quantity), "location": location,
                    "min_stock_alert": int(min_alert), "status": "active",
                    "created_by": user["institutional_email"], "loose_pieces": "1" if loose_pieces else "",
                }
                try:
                    storage.save_item(data, new_id, is_new=True, actor_email=user["institutional_email"],
                                      actor=user)
                    st.success(f"'{name}' registrado correctamente con el código {new_id}.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


def _render_import(storage, user: dict) -> None:
    st.caption(
        "Sube un CSV para registrar o actualizar muchos items de una sola vez "
        "(ideal para cargar el inventario inicial del laboratorio)."
    )
    st.caption(
        "Usa 'master' para un Contenedor Principal, 'child' para un Contenedor de "
        "Característica (con su 'parent_id' apuntando al maestro) y 'standalone' "
        "para un Ítem Individual en la columna item_type."
    )
    st.caption(barcode.FORMAT_HELP)
    st.caption(
        "La columna id se lee como texto, asi que los ceros a la izquierda (ej. 0012345) se "
        "conservan. Si editas el CSV en Excel, formatea esa columna como Texto antes de "
        "guardarlo: Excel los borra."
    )
    template_csv = (
        "id,name,category,description,item_type,parent_id,unit,quantity,location,min_stock_alert\n"
        "1-2-05-00-000,Caja de Electronica,Electronica,Contenedor principal de componentes,master,,unidad,0,Estante 3,0\n"
        "1-2-05-12-000,Resistencias 220 ohm,Electronica,Paquete de 10,child,1-2-05-00-000,paquete,20,Estante 3,5\n"
        "M1-E1,Multimetro digital,Instrumentacion,Fluke 115,standalone,,unidad,4,Mesa 1,1\n"
    )
    st.download_button(
        ":material/download: Descargar plantilla CSV", data=template_csv, file_name="plantilla_items_laboratorio.csv",
        mime="text/csv", use_container_width=True,
    )

    uploaded = st.file_uploader("Archivo CSV", type=["csv"])
    if uploaded is not None:
        try:
            df_upload = pd.read_csv(uploaded, dtype=str).fillna("")
            st.dataframe(df_upload, use_container_width=True, hide_index=True)

            if st.button(":material/upload: Procesar importacion", type="primary", use_container_width=True):
                rows = df_upload.to_dict(orient="records")
                with st.spinner("Importando items..."):
                    result = storage.bulk_upsert_items(rows, actor_email=user["institutional_email"], actor=user)
                st.success(
                    f"Importacion completada: {len(result['created'])} creados, "
                    f"{len(result['updated'])} actualizados."
                )
                if result["errors"]:
                    st.warning("Algunas filas no se importaron:")
                    for err in result["errors"]:
                        st.caption(f":material/warning: {err}")
                st.rerun()
        except Exception as e:
            st.error(f"No se pudo leer el CSV: {e}")


def render():
    storage = st.session_state.storage
    user = st.session_state.user

    if not guard_cap(user, permissions.CAP_EDIT_INVENTORY, "el Inventario"):
        return

    page_header("Inventario", icon=":material/inventory_2:", subtitle="Catálogo por contenedor, alta, edición e importación masiva")

    if st.session_state.get("editing_item_id"):
        item = storage.get_item(st.session_state.editing_item_id)
        if not item:
            st.error("El item ya no existe.")
            st.session_state.editing_item_id = None
        else:
            _edit_item_form(storage, item, user)
        return

    tab_catalogo, tab_nuevo, tab_import, tab_etiquetas, tab_ubicaciones = persistent_tabs(
        [":material/list_alt: Catalogo", ":material/add: Nuevo item", ":material/upload_file: Importar CSV masivo",
         ":material/print: Etiquetas", ":material/map: Ubicaciones"], "inventario",
    )
    label_spec = labels.load_label_spec(storage)

    with tab_catalogo:
        _render_catalog(storage, user, label_spec)

    with tab_nuevo:
        _render_new_item(storage, user)

    with tab_etiquetas:
        label_settings.render(storage, user)

    with tab_import:
        _render_import(storage, user)

    with tab_ubicaciones:
        ubicaciones.render(storage, user, label_spec)
