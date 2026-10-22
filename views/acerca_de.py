# -*- coding: utf-8 -*-
"""views/acerca_de.py - Informacion del proyecto y como usarlo."""

import streamlit as st

from core.labels import ITEM_TYPE_NAMES
from core.ui import ROLE_LABELS, feature_cards, hero, section_title


def render():
    hero(
        "Inventario de Laboratorio UNIMINUTO",
        subtitle="Trazabilidad de equipos y materiales del laboratorio de ingeniería",
        eyebrow="Acerca del proyecto",
        icon=":material/info:",
    )

    with st.container(border=True, key="lab_card_acerca_intro"):
        st.markdown(
            "Este sistema fue creado para el **laboratorio de ingeniería de UNIMINUTO**, donde "
            "conviven muchos tipos de productos identificados con una serie propia de códigos "
            "de barras. Su objetivo es saber en todo momento **qué equipos y materiales existen, "
            "dónde están y quién los tiene prestados**, sin depender de planillas sueltas."
        )

    section_title("¿Cómo funciona?", icon=":material/extension:", caption="Tres tipos de elementos organizan todo el inventario.")
    feature_cards([
        {"icon": "🗄️", "title": ITEM_TYPE_NAMES["master"],
         "text": "La caja, kit o gabinete físico que agrupa productos relacionados."},
        {"icon": "🧩", "title": ITEM_TYPE_NAMES["child"],
         "text": "Una subdivisión dentro de un Contenedor Principal para una característica "
                 "concreta (ej. \"Resistencias 220 Ω\", \"Tornillos M4\")."},
        {"icon": "🔹", "title": ITEM_TYPE_NAMES["standalone"],
         "text": "Un producto con su propio código, sin contenedor."},
    ])

    section_title("Roles institucionales", icon=":material/group:")
    feature_cards([
        {"icon": "🎓", "title": ROLE_LABELS["estudiante"],
         "text": "Solicita productos y servicios, reserva el laboratorio, ve lo suyo y confirma que recibió su pedido."},
        {"icon": "📦", "title": ROLE_LABELS["operario"],
         "text": "Toma los pedidos autorizados, recorre el laboratorio, hace el picking unidad por unidad "
                 "y lo entrega al supervisor. No aprueba ni ve inventario ni reportes."},
        {"icon": "🧑‍💼", "title": ROLE_LABELS["encargado"],
         "text": "Aprueba solicitudes, autoriza operarios, recibe el picking, lo revisa y lo entrega al cliente. "
                 "Ve la trazabilidad y los tiempos; no edita el inventario."},
        {"icon": "🧑‍🏫", "title": ROLE_LABELS["profesor"],
         "text": "Hace lo mismo que el encargado y además administra productos, ubicaciones, fotos, chips NFC, "
                 "reservas, reportes y todos los préstamos."},
        {"icon": "🔐", "title": ROLE_LABELS["maestro"],
         "text": "Gestiona usuarios, roles y contraseñas, configura los correos y tiene visibilidad total del laboratorio."},
    ])

    st.info(
        "Para dudas sobre el uso del sistema o solicitudes de soporte, contacta al "
        "administrador del laboratorio desde tu programa académico.",
        icon=":material/chat:",
    )
