# -*- coding: utf-8 -*-
"""views/nfc.py - Chips NFC (solo profesor/maestro).

- Inventario con el telefono: abrir un conteo, verificar productos tocando su
  chip NFC (o escribiendo/escaneando el codigo), ver el avance y cerrar con un
  resumen. Las cantidades solo cambian si el maestro confirma los ajustes.
- Grabar etiquetas: la URL de cada producto/ubicacion para grabarla con la app
  gratuita NFC Tools y el registro de chips grabados y probados.
- Toques recientes: quien toco que chip y cuando.

El toque en si (la URL que abre el chip) se procesa en views/nfc_tap.py."""

import pandas as pd
import streamlit as st

from core import inventory_count as counting
from core import excel_safe, nfc, permissions, traceability
from core.labels import ITEM_TYPE_ICONS, ITEM_TYPE_NAMES
from core.ui import (
    badge_html, card, card_header, centered_columns, empty_state, feature_cards, guard_cap, more_button,
    page_header, paginate, persistent_tabs, section_title, stat_cards, timeline,
)
from views.nfc_tap import COUNT_FOCUS_KEY

NOTICE_KEY = "nfc_count_notice"
TAG_NOTICE_KEY = "nfc_tag_notice"
AUTO_REFRESH_SECONDS = 10
MAX_TABLE_ROWS = 400
_FEEDBACK = {"success": st.success, "warning": st.warning, "info": st.info, "danger": st.error}

_SCOPE_CHOICES = {
    "Todo el inventario": counting.SCOPE_ALL,
    "Una estantería": counting.SCOPE_SHELF,
    "Un contenedor": counting.SCOPE_CONTAINER,
}
_STATUS_FILTERS = ("Todos", nfc.NO_TAG_LABEL, nfc.TAG_STATUS_LABELS[nfc.TAG_WRITTEN], nfc.TAG_STATUS_LABELS[nfc.TAG_TESTED])
WHERE_CHIPS = "Estanterías, pisos y contenedores"     # dónde van los chips: es la vista por omisión
_TYPE_FILTERS = {
    WHERE_CHIPS: "chips", "Todo": None, "Productos": ("standalone", "child"), "Contenedores": ("master",), "Ubicaciones": ("location",),
}
_STATUS_TONE = {nfc.TAG_TESTED: "success", nfc.TAG_WRITTEN: "warning"}
CARDS_PER_ROW = 2
CARDS_PER_PAGE = 12


def _notice(ok, message: str, key: str = NOTICE_KEY) -> None:
    st.session_state[key] = ("success" if ok else "danger", message)


def _show_notice(key: str = NOTICE_KEY) -> None:
    notice = st.session_state.pop(key, None)
    if notice:
        _FEEDBACK.get(notice[0], st.info)(notice[1])


def _methods_text(methods) -> str:
    return ", ".join(counting.METHOD_LABELS.get(method, method) for method in methods or [])


# ---------------------------------------------------------------------------
# Inventario con el telefono
# ---------------------------------------------------------------------------

def _shelves(items: list) -> list:
    found = {counting.shelf_of(str(item.get("id") or "")) for item in items}
    found.discard(None)
    return sorted(found) or [1, 2, 3]


def _render_count_start(storage, user: dict) -> None:
    section_title("Inventario con el teléfono", icon=":material/fact_check:",
                  caption="Recorre el laboratorio con el celular: cada chip NFC que toques (o cada código que "
                          "escribas o escanees aquí) marca ese producto como verificado.")
    feature_cards([
        {"icon": "▶️", "title": "1 · Abre el conteo", "text": "Elige qué vas a contar: todo, una estantería o un contenedor."},
        {"icon": "📲", "title": "2 · Toca los chips",
         "text": "Acerca el teléfono al chip de cada producto; sin chip, escribe o escanea el código aquí. "
                 "Si quieres, anota cuántas unidades hay."},
        {"icon": "🏁", "title": "3 · Revisa y cierra",
         "text": "Mira lo que falta y cierra: se guarda un resumen. Las cantidades no cambian salvo que un "
                 "administrador confirme los ajustes."},
    ])
    items = storage.get_all_items()
    masters = sorted((item for item in items if item.get("item_type") == "master"),
                     key=lambda item: counting.code_order(item.get("id")))
    choice = st.radio("¿Qué vas a contar?", list(_SCOPE_CHOICES), horizontal=True, key="nfc_count_scope")
    kind = _SCOPE_CHOICES[choice]
    with st.form("nfc_count_start"):
        name = st.text_input("Nombre del conteo", value=counting.default_name(), max_chars=80)
        scope = {"kind": kind}
        if kind == counting.SCOPE_SHELF:
            scope["estanteria"] = st.selectbox("Estantería", _shelves(items))
        elif kind == counting.SCOPE_CONTAINER:
            options = {item["id"]: item for item in masters}
            if options:
                picked = st.selectbox("Contenedor", list(options),
                                      format_func=lambda code: f"{options[code].get('name')} ({code})")
                scope.update(id=picked, name=(options.get(picked) or {}).get("name", ""))
            else:
                st.caption("Todavía no hay contenedores registrados.")
        submitted = st.form_submit_button(":material/play_arrow: Iniciar conteo", type="primary",
                                          use_container_width=True)
    if submitted:
        ok, message, _ = counting.start_session(storage, user, name, scope)
        _notice(ok, message)
        st.rerun()


def _render_history(storage) -> None:
    sessions = counting.closed_sessions(storage)
    if not sessions:
        return
    section_title("Conteos anteriores", icon=":material/history:")
    for row in paginate(sessions, "nfc_history", 6):
        expected = int(row.get("expected", 0) or 0)
        verified = int(row.get("verified", 0) or 0)
        done = round(100 * verified / expected) if expected else 0
        with card(f"nfc_hist_{row.get('id') or row.get('closed_at')}"):
            card_header(row.get("name") or "Conteo", subtitle=f"{counting.describe_scope(row.get('scope'))} · "
                        f"cerrado el {traceability.fmt_local(row.get('closed_at'))}", icon="🧮",
                        badges=[{"text": f"{verified}/{expected} verificados ({done} %)",
                                 "tone": "success" if done >= 100 else "warning"},
                                *([{"text": f"{len(row['differences'])} diferencia(s)", "tone": "danger", "icon": "⚠️"}]
                                  if row.get("differences") else [])])
            with st.expander("Ver detalle"):
                st.caption(f"Abierto por {row.get('started_by') or '—'} el "
                           f"{traceability.fmt_local(row.get('started_at'))} · cerrado por {row.get('closed_by') or '—'}")
                if row.get("notes"):
                    st.caption(f"Notas: {row['notes']}")
                missing = row.get("missing") or []
                if missing:
                    st.markdown(f"**Sin verificar ({row.get('missing_total', len(missing))}):** " + ", ".join(missing))
                if row.get("differences"):
                    st.markdown("**Diferencias encontradas**")
                    st.dataframe(pd.DataFrame([{
                        "Código": diff.get("code"), "Producto": diff.get("name"), "Contadas": diff.get("counted"),
                        "Disponible en el sistema": diff.get("available"), "En préstamo": diff.get("on_loan"),
                    } for diff in row["differences"]]), hide_index=True, use_container_width=True)
                if row.get("adjusted"):
                    st.markdown("**Cantidades ajustadas**: " + ", ".join(
                        f"{adj.get('code')} ({adj.get('from')} → {adj.get('to')})" for adj in row["adjusted"]))
                if row.get("extra"):
                    st.caption("Verificados fuera del alcance: " + ", ".join(row["extra"]))
    more_button(len(sessions), "nfc_history", 6)


def _focus_mark_form(storage, user: dict, session: dict, item: dict, method: str) -> None:
    with st.form(f"nfc_count_qty_{item['id']}", clear_on_submit=True):
        qty = st.number_input("Unidades contadas en el estante", min_value=0, step=1, value=None,
                              placeholder="Opcional")
        submitted = st.form_submit_button(":material/save: Guardar cantidad", use_container_width=True)
    if submitted:
        if qty is None:
            st.warning("Escribe la cantidad contada (o deja el producto solo como verificado).")
            return
        ok, message, _ = counting.record_mark(storage, session, user, item["id"], int(qty), method, item)
        _notice(ok, message)
        st.session_state[COUNT_FOCUS_KEY] = {"code": item["id"], "method": method}
        st.rerun()


def _render_focus(storage, user: dict, session: dict, items: list, report: dict) -> None:
    """El producto que se acaba de tocar o marcar: estado y cantidad contada."""
    focus = st.session_state.get(COUNT_FOCUS_KEY)
    if not focus:
        return
    item = counting.resolve_code(focus.get("code"), items)
    with st.container(border=True):
        if focus.get("message"):
            _FEEDBACK.get(focus.get("tone"), st.success)(f":material/nfc: {focus['message']}")
        if not item:
            st.warning(f"«{focus.get('code')}» no está en el inventario.")
        elif item.get("item_type") == "master":
            children = [row for row in report["rows"] if row["parent_id"] == item["id"]]
            done = len([row for row in children if row["verified"]])
            st.markdown(f"**{item.get('name')}** (`{item['id']}`) es un contenedor: toca o escribe el código de "
                        f"cada producto. Verificados aquí: {done} de {len(children)}.")
            pending = [row for row in children if not row["verified"]]
            if pending:
                st.caption("Faltan: " + ", ".join(f"{row['name']} ({row['code']})" for row in pending[:30]))
        else:
            row = next((r for r in report["rows"] if r["code"] == item["id"]), None)
            st.markdown(f"**{item.get('name')}** · `{item['id']}`")
            if row is None:
                st.caption("Este producto no está en el alcance del conteo; quedó registrado como «fuera del alcance».")
            else:
                counted = "—" if row["counted"] is None else row["counted"]
                st.caption(f"Debería haber {row['available']} en el estante · {row['on_loan']} en préstamo · "
                           f"total {row['quantity']} · contadas: {counted}")
            _focus_mark_form(storage, user, session, item, focus.get("method") or counting.METHOD_CODE)
        if st.button("Listo", key="nfc_count_focus_done", icon=":material/check:"):
            st.session_state.pop(COUNT_FOCUS_KEY, None)
            st.rerun()


def _render_mark_form(storage, user: dict, session: dict, items: list) -> None:
    with st.form("nfc_count_mark", clear_on_submit=True):
        code = st.text_input("Código del producto (sin chip NFC)",
                             placeholder="Escanea con el lector o escribe el código",
                             help="El lector USB escribe el código y presiona Enter por ti.")
        qty = st.number_input("Unidades contadas en el estante (opcional)", min_value=0, step=1, value=None,
                              placeholder="Opcional")
        submitted = st.form_submit_button(":material/check_circle: Marcar como verificado", type="primary",
                                          use_container_width=True)
    if not submitted:
        return
    item = counting.resolve_code(code, items)
    if not (code or "").strip():
        st.warning("Escribe o escanea el código del producto.")
    elif not item:
        st.warning(f"«{code.strip()}» no está en el inventario.")
    elif item.get("item_type") == "master":
        st.session_state[COUNT_FOCUS_KEY] = {"code": item["id"], "method": counting.METHOD_CODE}
        st.rerun()
    else:
        ok, message, _ = counting.record_mark(storage, session, user, item["id"],
                                              None if qty is None else int(qty), counting.METHOD_CODE, item)
        _notice(ok, message)
        st.session_state[COUNT_FOCUS_KEY] = {"code": item["id"], "method": counting.METHOD_CODE}
        st.rerun()


def _report_table(rows: list, verified: bool) -> pd.DataFrame:
    data = []
    for row in rows[:MAX_TABLE_ROWS]:
        entry = {"Código": row["code"], "Producto": row["name"], "Ubicación": row["location"],
                 "En el estante (sistema)": row["available"]}
        if verified:
            entry.update({
                "Contadas": row["counted"], "Diferencia": row["difference"], "Cómo": _methods_text(row["methods"]),
                "Hora": traceability.fmt_local(row["at"], time_only=True), "Por": row["by"],
            })
        data.append(entry)
    return pd.DataFrame(data)


def _render_report(report: dict) -> None:
    percent = round(100 * report["ratio"])
    stat_cards([
        {"label": "Productos esperados", "value": report["total"], "icon": "📦"},
        {"label": "Verificados", "value": report["verified_count"], "icon": "✅", "tone": "success",
         "help": f"{percent} % · {report['nfc_count']} con chip NFC"},
        {"label": "Faltan", "value": report["missing_count"], "icon": "⏳",
         "tone": "warning" if report["missing_count"] else "success"},
        {"label": "Con diferencia", "value": report["difference_count"], "icon": "⚠️",
         "tone": "danger" if report["difference_count"] else "neutral", "help": "Contadas ≠ sistema"},
    ])
    st.progress(report["ratio"], text=f"{report['verified_count']} de {report['total']} verificados ({percent} %)")
    if report["missing"]:
        with st.expander(f":material/pending: Faltan por verificar ({report['missing_count']})", expanded=True):
            st.dataframe(_report_table(report["missing"], verified=False), hide_index=True, use_container_width=True)
    if report["verified"]:
        with st.expander(f":material/task_alt: Verificados ({report['verified_count']})"):
            st.dataframe(_report_table(report["verified"], verified=True), hide_index=True, use_container_width=True)
    if report["extra"]:
        with st.expander(f":material/help: Verificados fuera del alcance ({len(report['extra'])})"):
            st.dataframe(pd.DataFrame([{
                "Código": row["code"], "Producto": row["name"] or "No registrado", "Contadas": row["counted"],
                "Cómo": _methods_text(row["methods"]), "Por": row["by"],
            } for row in report["extra"]]), hide_index=True, use_container_width=True)
    if len(report["rows"]) > MAX_TABLE_ROWS:
        st.caption(f"Las tablas muestran hasta {MAX_TABLE_ROWS} productos.")


@st.fragment(run_every=AUTO_REFRESH_SECONDS)
def _live_report(session_id: str) -> None:
    """Avance que se actualiza solo: muestra los toques hechos desde otras
    pestañas u otros teléfonos sin recargar la página."""
    storage = st.session_state.storage
    session = counting.open_session(storage)
    if not session or session["id"] != session_id:
        st.info("Este conteo ya se cerró. Recarga la página para ver el resumen.")
        return
    report = counting.progress(session, storage.get_all_items(), counting.session_marks(storage, session_id),
                               storage.get_availability_map())
    _render_report(report)


def _render_close(storage, user: dict, session: dict, report: dict) -> None:
    with st.expander(":material/flag: Cerrar el conteo"):
        st.caption("Se guarda un resumen con lo verificado, lo que faltó y las diferencias. Las cantidades del "
                   "inventario no cambian, salvo los ajustes que un administrador marque y confirme aquí.")
        adjustable = counting.can_adjust(user)
        differences = [row for row in report["differences"] if row["counted"] is not None]
        if differences and not adjustable:
            st.info(f"Hay {len(differences)} diferencia(s) entre lo contado y el sistema. Quedarán en el resumen; "
                    "solo un administrador puede ajustar las cantidades.")
        with st.form("nfc_count_close"):
            edited = None
            confirm = False
            if differences and adjustable:
                st.markdown("**Ajustes de cantidad** (marca solo los que quieras aplicar)")
                table = pd.DataFrame([{
                    "Aplicar": False, "Código": row["code"], "Producto": row["name"], "Contadas": row["counted"],
                    "En préstamo": row["on_loan"], "Total actual": row["quantity"],
                    "Total nuevo": row["counted"] + row["on_loan"],
                } for row in differences])
                edited = st.data_editor(
                    table, hide_index=True, use_container_width=True, key="nfc_count_adjust",
                    disabled=[column for column in table.columns if column != "Aplicar"],
                    column_config={"Aplicar": st.column_config.CheckboxColumn("Aplicar", default=False)},
                )
                confirm = st.checkbox("Confirmo que quiero cambiar en el inventario las cantidades marcadas")
            notes = st.text_area("Notas (opcional)", max_chars=500, height=80)
            submitted = st.form_submit_button(":material/flag: Cerrar conteo y guardar resumen", type="primary",
                                              use_container_width=True)
        if not submitted:
            return
        chosen = []
        if edited is not None:
            codes = {str(code) for code, apply in zip(edited["Código"], edited["Aplicar"]) if bool(apply)}
            chosen = [row for row in differences if row["code"] in codes]
        if chosen and not confirm:
            st.error("Marca la casilla de confirmación para aplicar los ajustes (o desmarca los ajustes).")
            return
        applied, errors = counting.apply_adjustments(storage, session, user, chosen) if chosen else ([], [])
        ok, message, _ = counting.close_session(storage, session, user, report, applied, notes)
        if errors:
            message += " No se ajustaron: " + "; ".join(errors)
        _notice(ok and not errors, message)
        st.session_state.pop(COUNT_FOCUS_KEY, None)
        st.rerun()


def _render_count(storage, user: dict) -> None:
    _show_notice()
    session = counting.open_session(storage)
    if not session:
        st.session_state.pop(COUNT_FOCUS_KEY, None)
        _render_count_start(storage, user)
        _render_history(storage)
        return

    items = storage.get_all_items()
    report = counting.progress(session, items, counting.session_marks(storage, session["id"]),
                               storage.get_availability_map())
    section_title(f"Conteo abierto: {session['name']}", icon=":material/fact_check:",
                  caption=f"{counting.describe_scope(session['scope'])} · abierto por {session['started_by'] or '—'} "
                          f"el {traceability.fmt_local(session['started_at'])}")
    _render_focus(storage, user, session, items, report)
    st.caption(":material/nfc: Toca el chip NFC de cada producto con el teléfono: queda verificado aquí y esta "
               f"lista se actualiza sola cada {AUTO_REFRESH_SECONDS} s (también con los toques de otros teléfonos).")
    _render_mark_form(storage, user, session, items)
    _live_report(session["id"])
    _render_close(storage, user, session, report)


# ---------------------------------------------------------------------------
# Grabar etiquetas
# ---------------------------------------------------------------------------

_GUIDE = """
**Qué chips comprar.** Etiquetas NFC **NTAG215** (504 bytes; las NTAG213 de 144 bytes también sirven para
estas direcciones y NTAG216 deja más margen). Sobre estanterías o cajas **metálicas** usa etiquetas
*anti-metal* (*on-metal*): una etiqueta normal pegada al metal no se lee.

**Grabar un chip con NFC Tools** (app gratuita de wakdev para Android y iPhone 7 o posterior):

1. Busca abajo el producto o la ubicación y copia su URL con el botón de copiar del recuadro.
2. Abre **NFC Tools** → pestaña **Escribir** (*Write*) → **Agregar un registro** (*Add a record*) →
   **URL / URI**.
3. Pega la URL completa y toca **OK**. Si la app ya muestra `https://` delante, no lo repitas.
4. Toca **Escribir** (*Write*) y acerca el chip al teléfono: en Android, al centro de la parte trasera; en
   iPhone, al borde superior. Espera el aviso de «Escritura completada».
5. **Prueba el chip**: acerca otra vez el teléfono (con la pantalla desbloqueada). Debe abrirse la app en ese
   producto; al iniciar sesión el toque queda registrado y el chip aparece como «Probado».
6. Pega el chip junto a la etiqueta de código de barras, a la vista y fuera del metal.
7. *Opcional:* bloquea el chip para que nadie lo reescriba (NFC Tools → **Otros** → **Bloquear etiqueta**).
   **Es permanente**: hazlo solo después de probarlo. Si después cambias la dirección de la app (APP_URL) o
   la clave NFC_SECRET, un chip bloqueado no se puede regrabar y habrá que reemplazarlo.

**Leer un chip** no requiere instalar nada: Android (con NFC activado) y iPhone XS o posterior leen el chip
con solo acercar el teléfono y abren la URL en el navegador (en iPhone aparece una notificación: tócala).
En iPhone 7, 8 y X usa el botón **Lector de etiquetas NFC** del Centro de control. No se usa Web NFC: funciona
en cualquier navegador.

**Cada toque abre una pestaña nueva** del navegador; si la app te pide iniciar sesión, el toque espera y se
registra en cuanto entras. Los toques de la misma persona sobre el mismo chip en menos de un minuto cuentan una
sola vez.
"""


def _render_config_status(base: str) -> None:
    """Dos avisos de configuración, compactos: dónde abre el chip y si la URL va firmada."""
    left, right = st.columns(2)
    with left, st.container(border=True):
        if nfc.configured_app_url():
            st.markdown(f"{badge_html('Dirección fija', 'success', '✅')} **APP_URL**", unsafe_allow_html=True)
            st.caption(f"Los chips abren {base}.")
        elif base:
            st.markdown(f"{badge_html('Dirección provisional', 'warning', '⚠️')} **APP_URL**", unsafe_allow_html=True)
            st.caption(f"Las URL usan la dirección con la que abriste la app ({base}). Configura APP_URL en los Secrets "
                       "con la dirección pública definitiva para que los chips no dejen de funcionar si cambia.")
        else:
            st.markdown(f"{badge_html('Falta la dirección', 'danger', '⛔')} **APP_URL**", unsafe_allow_html=True)
            st.caption("No se pudo saber la dirección de la app: configura APP_URL en los Secrets.")
    with right, st.container(border=True):
        if nfc.nfc_secret():
            st.markdown(f"{badge_html('Firma activa', 'success', '🔏')} **NFC_SECRET**", unsafe_allow_html=True)
            st.caption("Cada URL lleva `&s=…` y la app rechaza las URL inventadas o alteradas.")
        else:
            st.markdown(f"{badge_html('Sin firma', 'warning', '🔓')} **NFC_SECRET**", unsafe_allow_html=True)
            st.caption("Cualquiera que conozca un código podría abrir su URL a mano. Configura NFC_SECRET en los "
                       "Secrets **antes** de grabar los chips.")


def _tag_rows(items: list, registry: dict, base: str, secret: str) -> list:
    rows = []
    for item in sorted(items, key=lambda row: counting.code_order(row.get("id"))):
        code = str(item.get("id") or "")
        entry = registry.get(code)
        try:
            url = nfc.build_tag_url(code, base, secret)
            problem = ""
        except ValueError as exc:
            url, problem = "", str(exc)
        rows.append({
            "item": item, "code": code, "entry": entry, "url": url, "problem": problem,
            "status": nfc.tag_status_label(entry),
        })
    return rows


def _overview(rows: list) -> None:
    """Cobertura de chips: de un vistazo, cuántos están probados, grabados o faltan."""
    tested = len([r for r in rows if r["entry"] and r["entry"]["status"] == nfc.TAG_TESTED])
    written = len([r for r in rows if r["entry"] and r["entry"]["status"] == nfc.TAG_WRITTEN])
    missing = len([r for r in rows if not r["entry"]])
    stat_cards([
        {"label": "Productos y ubicaciones", "value": len(rows), "icon": "📦"},
        {"label": "Con chip probado", "value": tested, "icon": "✅", "tone": "success"},
        {"label": "Grabados sin probar", "value": written, "icon": "⏳", "tone": "warning" if written else "neutral"},
        {"label": "Sin chip", "value": missing, "icon": "🏷️", "tone": "neutral"},
    ])
    if rows:
        st.progress(tested / len(rows), text=f"{tested} de {len(rows)} con chip probado ({round(100 * tested / len(rows))} %)")


def _tag_card(storage, user: dict, row: dict) -> None:
    item, entry = row["item"], row["entry"]
    kind = item.get("item_type")
    status_key = (entry or {}).get("status")
    badges = [{"text": row["status"], "tone": _STATUS_TONE.get(status_key, "neutral"),
               "icon": {"tested": "✅", "written": "⏳"}.get(status_key, "🏷️")},
              {"text": ITEM_TYPE_NAMES.get(kind, kind or ""), "tone": "info"}]
    with card(f"nfc_tag_{row['code']}", tone=_STATUS_TONE.get(status_key)):
        card_header(item.get("name") or row["code"], subtitle=f"{row['code']} · {item.get('location') or 'sin ubicación'}",
                    icon=ITEM_TYPE_ICONS.get(kind, "📦"), badges=badges)
        if entry and (entry.get("taps") or entry.get("written_at")):
            bits = []
            if entry.get("written_at"):
                bits.append(f"grabado por {entry.get('written_by') or '—'} el {traceability.fmt_local(entry['written_at'])}")
            if entry.get("last_tap_at"):
                bits.append(f"{entry.get('taps', 0)} toque(s), el último de {entry.get('last_tap_by') or '—'} "
                            f"({traceability.fmt_local(entry['last_tap_at'])})")
            st.caption(" · ".join(bits))
        if row["problem"]:
            st.warning(f"No se puede generar la URL: {row['problem']}")
            return
        url_col, action_col = st.columns(2)
        with url_col.popover(":material/link: URL del chip", use_container_width=True, key=f"nfc_url_pop_{row['code']}"):
            st.code(row["url"], language=None, wrap_lines=True)
            fits = nfc.chips_that_fit(row["url"])
            st.caption(f"Ocupa {nfc.ndef_size(row['url'])} bytes · cabe en: {', '.join(fits) if fits else 'ningún NTAG común'} "
                       f"· recomendado {nfc.RECOMMENDED_CHIP}.")
        if entry:
            if action_col.button(":material/delete: Quitar chip", key=f"nfc_removed_{row['code']}",
                                 use_container_width=True):
                nfc.unregister_tag(storage, row["code"], user, "Quitado desde Chips NFC")
                _notice(True, f"Chip de «{item.get('name')}» quitado del registro.", TAG_NOTICE_KEY)
                st.rerun()
        elif action_col.button(":material/nfc: Marcar grabado", key=f"nfc_written_{row['code']}", type="primary",
                               use_container_width=True):
            nfc.register_tag(storage, row["code"], user, chip=nfc.RECOMMENDED_CHIP)
            _notice(True, f"Chip de «{item.get('name')}» registrado como grabado. Pruébalo acercando el teléfono.",
                    TAG_NOTICE_KEY)
            st.rerun()


def _render_tags(storage, user: dict, rows: list, base: str) -> None:
    _show_notice(TAG_NOTICE_KEY)
    _render_config_status(base)
    with st.expander(":material/menu_book: Cómo grabar y probar un chip NFC", expanded=False):
        st.markdown(_GUIDE)
    if not rows:
        empty_state("Sin productos", "Registra productos en Inventario para grabar sus chips.", icon=":material/nfc:")
        return

    search_col, type_col = st.columns([3, 2])
    search = search_col.text_input(":material/search: Buscar", key="nfc_tag_search",
                                  placeholder="Código, nombre o ubicación").strip().lower()
    type_label = type_col.selectbox("Tipo", list(_TYPE_FILTERS), key="nfc_tag_type",
                                    help="Los chips NFC van en estanterías, pisos y contenedores; las cajas y los "
                                         "productos se leen con su código de barras.")
    status_filter = st.segmented_control("Estado del chip", _STATUS_FILTERS, default="Todos", key="nfc_tag_status") or "Todos"
    kinds = _TYPE_FILTERS[type_label]
    shown = [
        row for row in rows
        if (not search or search in " ".join(str(row["item"].get(k) or "") for k in ("id", "name", "location")).lower())
        and (status_filter == "Todos" or row["status"] == status_filter)
        and (kinds is None or (nfc.arrival_kind(row["item"]) != "" if kinds == "chips"
                               else row["item"].get("item_type") in kinds))
    ]
    if not shown:
        empty_state("Ningún producto coincide", "Prueba con otra búsqueda o cambia los filtros.", icon=":material/search:")
    else:
        st.caption(f"{len(shown)} de {len(rows)} producto(s) y ubicación(es).")
        visible = paginate(shown, "nfc_tags", CARDS_PER_PAGE)
        with st.container(key="lab_product_grid_nfc"):
            for start in range(0, len(visible), CARDS_PER_ROW):
                chunk = visible[start:start + CARDS_PER_ROW]
                for column, row in zip(centered_columns(len(chunk), CARDS_PER_ROW), chunk):
                    with column:
                        _tag_card(storage, user, row)
        more_button(len(shown), "nfc_tags", CARDS_PER_PAGE)

    with st.expander(":material/download: Tabla completa y exportación"):
        table = pd.DataFrame([{
            "Código": row["code"], "Nombre": row["item"].get("name"),
            "Tipo": ITEM_TYPE_NAMES.get(row["item"].get("item_type"), row["item"].get("item_type")),
            "Chip": row["status"],
            "Último toque": traceability.fmt_local((row["entry"] or {}).get("last_tap_at")),
            "URL": row["url"] or row["problem"],
        } for row in rows])
        st.dataframe(table, hide_index=True, use_container_width=True)
        st.download_button(":material/download: Descargar las URL (CSV)",
                           data=excel_safe.csv_safe_frame(table).to_csv(index=False).encode("utf-8-sig"),
                           file_name="chips_nfc.csv", mime="text/csv", use_container_width=True)


# ---------------------------------------------------------------------------
# Toques recientes
# ---------------------------------------------------------------------------

def _render_taps(storage) -> None:
    taps = nfc.recent_taps(storage, limit=200)
    if not taps:
        empty_state("Todavía no hay toques", "Cuando alguien toque un chip NFC con su teléfono aparecerá aquí.",
                    icon=":material/nfc:")
        return
    st.caption("Cada toque prueba que esa persona estuvo frente al chip (con la firma activa, no se puede inventar).")
    query = st.text_input(":material/search: Buscar por persona o producto", key="nfc_taps_search").strip().lower()
    rows = []
    for tap in taps:
        details = traceability.event_details(tap)
        who = tap.get("actor_name") or "Alguien"
        what = details.get("item_name") or nfc.tap_code(tap)
        if query and query not in f"{who} {what} {nfc.tap_code(tap)}".lower():
            continue
        context = ["Firmado" if details.get("signed") else "Sin firma"]
        if details.get("count_session"):
            context.append("durante un conteo")
        rows.append({"title": f"{who} tocó «{what}»", "detail": f"{nfc.tap_code(tap)} · {', '.join(context)}",
                     "time": traceability.fmt_local(tap.get("created_at")), "icon": "📲" if details.get("signed") else "👆",
                     "state": "done" if details.get("signed") else "pending"})
    if not rows:
        empty_state("Sin resultados", "Ningún toque coincide con la búsqueda.", icon=":material/search:")
        return
    timeline(paginate(rows, "nfc_taps", 25))
    more_button(len(rows), "nfc_taps", 25)


def render():
    storage = st.session_state.storage
    user = st.session_state.user
    if not guard_cap(user, permissions.CAP_MANAGE_NFC, "Chips NFC"):
        return
    page_header("Chips NFC", icon=":material/nfc:",
                subtitle="Inventario con el teléfono, grabación de chips y toques registrados")
    base = nfc.app_base_url(st.context.url)
    rows = _tag_rows(storage.get_all_items(), nfc.load_registry(storage), base, nfc.nfc_secret())
    _overview(rows)
    tab_count, tab_tags, tab_taps = persistent_tabs([
        ":material/fact_check: Inventario con el teléfono", ":material/nfc: Chips",
        ":material/history: Toques recientes",
    ], "nfc")
    with tab_count:
        _render_count(storage, user)
    with tab_tags:
        _render_tags(storage, user, rows, base)
    with tab_taps:
        _render_taps(storage)
