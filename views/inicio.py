# -*- coding: utf-8 -*-
"""views/inicio.py - Dashboard principal: bienvenida, indicadores, accesos
rapidos y panel de alertas."""

import streamlit as st

from core import dispatch, permissions
from core import loans as loans_core
from core.labels import ITEM_TYPE_NAMES
from core.ui import (
    ROLE_LABELS, ROLE_SHORT_LABELS, alert_list, empty_state, greeting, hero, load_error, long_date_es,
    quick_actions, section_title, stat_cards,
)

_ROLE_PITCH = {
    "estudiante": "Solicita y recibe materiales del laboratorio desde un solo lugar.",
    "operario": "Toma pedidos autorizados y registra cada unidad durante el picking.",
    "encargado": "Aprueba, autoriza, recibe y entrega solicitudes del laboratorio.",
    "profesor": "Gestiona el inventario, los préstamos y las reservas de tus cursos.",
    "maestro": "Visión completa del laboratorio: inventario, usuarios, préstamos y alertas.",
}
_ROLE_ICON = {"estudiante": "🎓", "operario": "📦", "encargado": "🧑‍🔬", "profesor": "👨‍🏫", "maestro": "🛡️"}

# (clave en st.session_state["pages"], etiqueta, icono, descripcion)
_ACTIONS = [
    ("escanear", "Buscar", "🔎", "Escanea un código o escribe el nombre de un producto."),
    ("solicitudes", "Solicitudes", "📝", "Pide un producto, un servicio o una reserva y sigue su avance."),
    ("despacho", "Despacho", "📦", "Toma pedidos, registra picking y confirma entregas."),
    ("trazabilidad", "Trazabilidad", "🧭", "Revisa el recorrido de los pedidos y de cada producto."),
    ("prestamos", "Ver préstamos", "📋", "Consulta qué está prestado y qué falta devolver."),
    ("inventario", "Ir a Inventario", "📦", "Registra, edita e imprime etiquetas de productos."),
    ("ubicaciones", "Estanterías y mesas", "🗄️", "Crea estanterías, pisos, mesas de trabajo y sus etiquetas."),
    ("reportes", "Reportes", "📊", "Indicadores y exportaciones del laboratorio."),
]


_ROLE_GUIDE = (
    "el **{cliente}** solicita y confirma que recibió; el **{operario}** toma pedidos y hace el picking; "
    "el **{supervisor}** aprueba, autoriza operarios, recibe y entrega; el **{admin}** gestiona usuarios y ajustes."
)


def _quick_guide():
    roles = _ROLE_GUIDE.format(
        cliente=ROLE_SHORT_LABELS[permissions.ROLE_STUDENT], operario=ROLE_SHORT_LABELS[permissions.ROLE_OPERATOR],
        supervisor=ROLE_SHORT_LABELS[permissions.ROLE_ATTENDANT], admin=ROLE_SHORT_LABELS[permissions.ROLE_MASTER],
    )
    with st.expander(":material/lightbulb: Guía rápida: ¿cómo funciona este sistema?", expanded=False):
        st.markdown(f"""
        1. **{ITEM_TYPE_NAMES['master']}**: la caja o kit físico que agrupa varios productos.
        2. **{ITEM_TYPE_NAMES['child']}**: una subdivisión dentro de un Contenedor Principal
           para una característica concreta (ej. "Resistencias 220 Ω").
        3. **{ITEM_TYPE_NAMES['standalone']}**: un producto con su propio código, sin contenedor.
        4. En **:material/search: Buscar** escribe el nombre de un producto o escanea su código con tu lector USB; desde
           ahí puedes pedirlo, **darle salida** (llevártelo prestado) o **reingresarlo** cuando lo devuelvas.
        5. En **:material/edit_note: Solicitudes** pides un producto, un servicio o una reserva y ves en qué paso va
           (enviada, aprobada, preparando, lista para reclamar). Los responsables reciben correo si SMTP está configurado.
        6. En **:material/assignment: Préstamos** puedes ver en todo momento qué tienes prestado (o, si eres administrador
           o supervisor autorizado, quién tiene qué en todo el laboratorio).
        7. **Roles:** {roles}
        """)


def _alerts(user, items, availability, my_open_loans, overdue) -> list:
    rows = []
    if permissions.has_cap(user, permissions.CAP_VIEW_ALL_LOANS):
        for loan in overdue:
            rows.append({
                "icon": "⏰", "tone": "danger",
                "title": f"Vencido: {loan.get('item_name')}",
                "text": f"Prestado a {loan.get('user_name')}",
            })
    elif permissions.has_cap(user, permissions.CAP_CHECKOUT):
        for loan in my_open_loans:
            if loans_core.is_overdue(loan):
                rows.append({
                    "icon": "⏰", "tone": "danger",
                    "title": f"Tu préstamo de {loan.get('item_name')} está vencido",
                    "text": "Devuélvelo en el laboratorio lo antes posible.",
                })
    # Las alertas de existencias son del inventario: solo quien lo edita las ve.
    if permissions.has_cap(user, permissions.CAP_EDIT_INVENTORY):
        for item in items:
            if (
                item.get("item_type") != "master"
                and item.get("min_stock_alert") is not None
                and availability.get(item["id"], 0) <= item.get("min_stock_alert", 0)
            ):
                rows.append({
                    "icon": "📉", "tone": "warning",
                    "title": item.get("name"),
                    "text": f"Disponibilidad baja ({availability.get(item['id'], 0)} u.)",
                })
    return rows


def render():
    user = st.session_state.user
    storage = st.session_state.storage
    role = user["role"]
    first_name = (user.get("full_name") or "").split(" ")[0]

    hero(
        f"{greeting()}, {first_name}",
        subtitle=_ROLE_PITCH.get(role, "Gestión del laboratorio de ingeniería."),
        eyebrow=long_date_es(),
        icon=_ROLE_ICON.get(role, "👋"),
        badges=[
            {"text": ROLE_LABELS.get(role, role.capitalize()), "icon": "👤"},
            {"text": user.get("program_or_department") or "UNIMINUTO", "icon": "🏛️"},
        ],
    )

    # El operario trabaja con pedidos, no con el catálogo: no se le cargan ni se le
    # muestran existencias, préstamos ajenos ni usuarios.
    operator_only = (permissions.has_cap(user, permissions.CAP_VIEW_DISPATCH_QUEUE)
                     and not permissions.has_cap(user, permissions.CAP_CHECKOUT))
    items, my_open_loans, overdue = [], [], []
    if not operator_only:
        try:
            # Las ubicaciones (estanterias, pisos, mesas) no son items del catalogo.
            items = [i for i in storage.get_all_items() if i.get("item_type") != "location"]
            my_open_loans = storage.get_open_loans_for_user(user["id"])
            overdue = loans_core.get_overdue_loans(storage)
        except Exception as e:
            load_error("No se pudieron cargar las estadísticas", e)
            items, my_open_loans, overdue = [], [], []

    if operator_only:
        try:
            queue, mine = dispatch.queue_for(storage, user), dispatch.my_orders(storage, user)
        except Exception as e:
            load_error("No se pudo cargar la cola de despacho", e)
            queue, mine = [], []
        stat_cards([
            {"label": "Pedidos por tomar", "value": len(queue), "icon": "📥", "tone": "info"},
            {"label": "Mis pedidos", "value": len(mine), "icon": "📦", "tone": "info"},
        ])
    elif not permissions.has_cap(user, permissions.CAP_VIEW_ALL_LOANS):
        my_overdue = len([l for l in my_open_loans if loans_core.is_overdue(l)])
        stat_cards([
            {"label": "Items en catálogo", "value": len(items), "icon": "📦", "tone": "info"},
            {"label": "Mis préstamos activos", "value": len(my_open_loans), "icon": "📋", "tone": "info"},
            {"label": "Vencidos (míos)", "value": my_overdue, "icon": "⚠️",
             "tone": "danger" if my_overdue else "neutral"},
        ])
    else:
        all_open = storage.get_all_loans(status="out")
        users = storage.get_all_users()
        stat_cards([
            {"label": "Items en catálogo", "value": len(items), "icon": "📦", "tone": "info"},
            {"label": "Préstamos activos", "value": len(all_open), "icon": "📋", "tone": "info"},
            {"label": "Vencidos", "value": len(overdue), "icon": "⚠️",
             "tone": "danger" if overdue else "neutral"},
            {"label": "Usuarios registrados", "value": len(users), "icon": "👥", "tone": "neutral"},
        ])

    if not items and not operator_only:
        st.info(
            ":material/science: El inventario todavía está vacío."
            + (
                " Ve a **:material/inventory_2: Inventario → Nuevo item** (o **Importar CSV masivo**) "
                "para registrar los primeros productos del laboratorio."
                if permissions.has_cap(user, permissions.CAP_EDIT_INVENTORY)
                else " Pídele a un supervisor o al administrador del laboratorio que registre los primeros productos."
            )
        )

    pages = st.session_state.get("pages", {})
    actions = [
        {"page": pages.get(key), "label": label, "icon": icon, "description": description}
        for key, label, icon, description in _ACTIONS
    ]
    rows = []
    if items:
        try:
            rows = _alerts(user, items, storage.get_availability_map(), my_open_loans, overdue)
        except Exception as e:
            load_error("No se pudieron cargar las alertas", e)

    # Una sola columna centrada: las alertas pendientes van primero (son lo que
    # hay que atender); sin alertas, la confirmacion va despues de los accesos.
    if rows:
        section_title("Alertas", icon=":material/notifications:", caption=f"{len(rows)} aviso(s) activos")
        alert_list(rows)
    section_title("Accesos rápidos", icon=":material/bolt:", caption="Lo que más se usa, a un clic.")
    quick_actions(actions, columns=4)
    if not rows:
        section_title("Alertas", icon=":material/notifications:")
        empty_state("Sin alertas por el momento.", "Todo está en orden en el laboratorio.", icon=":material/check_circle:")

    st.markdown('<div style="height:0.75rem"></div>', unsafe_allow_html=True)
    _quick_guide()
