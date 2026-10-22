# -*- coding: utf-8 -*-
"""views/piece_panel.py - «Retirar piezas» en la ficha de un producto que se usa por piezas (Lego y similares).

No hay inventario por pieza: cada persona dice cuántas se lleva y cuántas devuelve, y el panel muestra
quién tiene cuántas. Ver ``core/pieces.py``."""

import streamlit as st

from core import permissions, pieces
from core.ui import card_header, esc


def _result(ok: bool, message: str) -> None:
    (st.success if ok else st.error)(message)


def render(storage, user: dict, item: dict, key_prefix: str) -> None:
    if not pieces.enabled(item) or not permissions.has_cap(user, permissions.CAP_CHECKOUT):
        return
    holders = pieces.balances(storage, item["id"])
    mine = next((row["pieces"] for row in holders if row["user_id"] == user.get("id")), 0)
    with st.container(border=True, key=f"{key_prefix}_pieces"):
        card_header("Retirar piezas", subtitle="Sin inventario por pieza: dices cuántas te llevas y cuántas devuelves.",
                    icon=":material/extension:",
                    badges=[{"text": f"{sum(row['pieces'] for row in holders)} fuera", "tone": "info", "icon": "🧩"}]
                    if holders else None)
        with st.form(f"{key_prefix}_take", clear_on_submit=True):
            c1, c2 = st.columns([1, 2])
            amount = c1.number_input("Piezas", min_value=1, max_value=pieces.MAX_PIECES, value=1, step=1)
            note = c2.text_input("Para qué (opcional)", placeholder="Práctica, proyecto, taller…")
            if st.form_submit_button("Retirar piezas", type="primary", use_container_width=True):
                _result(*pieces.take(storage, item["id"], amount, user, note))
        if mine:
            with st.form(f"{key_prefix}_back", clear_on_submit=True):
                st.caption(f"Tienes **{mine}** pieza(s) de este producto.")
                back = st.number_input("Piezas que devuelves", min_value=1, max_value=mine, value=mine, step=1)
                if st.form_submit_button("Devolver piezas", use_container_width=True):
                    ok, message = pieces.give_back(storage, item["id"], back, user)
                    _result(ok, message)
                    if ok:
                        st.rerun()
        if holders and permissions.has_cap(user, permissions.CAP_CHECKIN_ANY):
            with st.expander(f"Quién tiene piezas ({len(holders)})"):
                for row in holders:
                    c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment="center")
                    c1.markdown(f"**{esc(row['user_name'] or row['user_id'])}** · {row['pieces']} pieza(s)")
                    quantity = c2.number_input("Devuelve", min_value=1, max_value=row["pieces"], value=row["pieces"],
                                               step=1, key=f"{key_prefix}_q_{row['user_id']}", label_visibility="collapsed")
                    if c3.button("Registrar", key=f"{key_prefix}_r_{row['user_id']}", use_container_width=True):
                        ok, message = pieces.give_back(
                            storage, item["id"], quantity, user,
                            for_user={"id": row["user_id"], "full_name": row["user_name"]})
                        _result(ok, message)
                        if ok:
                            st.rerun()
