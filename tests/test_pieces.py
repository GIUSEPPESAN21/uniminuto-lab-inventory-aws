# -*- coding: utf-8 -*-
"""v1.19 · retiro de piezas (Lego y similares) sin inventario por pieza."""

import pytest

from core import loans, pieces, traceability
from core.storage import LabStorage

ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role) for role in ROLES}
    other = storage.create_user("Otra Persona", "otra@uniminuto.edu.co", "x", "estudiante")
    people["otra"] = other
    storage.save_item({"name": "Lego Technic", "item_type": "standalone", "quantity": 2, "loose_pieces": "1"},
                      "LAB-LEGO-1", is_new=True, actor=people["maestro"])
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 2}, "LAB-MIC-1",
                      is_new=True, actor=people["maestro"])
    return storage, people


def test_flag_is_saved_kept_across_edits_and_cleared_on_purpose(lab):
    storage, people = lab
    assert pieces.enabled(storage.get_item("LAB-LEGO-1")) and not pieces.enabled(storage.get_item("LAB-MIC-1"))
    storage.save_item({"name": "Lego Technic 2", "item_type": "standalone", "quantity": 2}, "LAB-LEGO-1",
                      actor=people["maestro"])                             # edición que no menciona la casilla
    assert pieces.enabled(storage.get_item("LAB-LEGO-1"))
    storage.save_item({"name": "Lego Technic", "quantity": 2, "loose_pieces": ""}, "LAB-LEGO-1",
                      actor=people["maestro"])
    assert not pieces.enabled(storage.get_item("LAB-LEGO-1"))


def test_taking_and_returning_pieces_does_not_touch_stock_or_loans(lab):
    storage, people = lab
    me = people["estudiante"]
    ok, message = pieces.take(storage, "LAB-LEGO-1", 40, me, "Taller de robótica")
    assert ok and "40" in message
    assert pieces.balance_of(storage, "LAB-LEGO-1", me["id"]) == 40
    assert storage.get_available_quantity("LAB-LEGO-1") == 2            # el juego sigue disponible
    assert storage.get_all_loans() == []
    ok, message = pieces.give_back(storage, "LAB-LEGO-1", 15, me)
    assert ok and "25" in message
    assert pieces.balance_of(storage, "LAB-LEGO-1", me["id"]) == 25
    assert pieces.give_back(storage, "LAB-LEGO-1", 25, me)[0]
    assert pieces.balances(storage, "LAB-LEGO-1") == []


def test_validation(lab):
    storage, people = lab
    me = people["estudiante"]
    for bad in (0, -3, "x", None, pieces.MAX_PIECES + 1):
        assert not pieces.take(storage, "LAB-LEGO-1", bad, me)[0]
    assert not pieces.take(storage, "LAB-MIC-1", 1, me)[0]                  # no se retira por piezas
    assert not pieces.take(storage, "NO-EXISTE", 1, me)[0]
    assert not pieces.give_back(storage, "LAB-LEGO-1", 1, me)[0]            # no tiene nada pendiente
    pieces.take(storage, "LAB-LEGO-1", 5, me)
    assert not pieces.give_back(storage, "LAB-LEGO-1", 6, me)[0]            # más de lo que tiene


def test_balances_are_per_person_and_only_staff_can_return_for_others(lab):
    storage, people = lab
    pieces.take(storage, "LAB-LEGO-1", 10, people["estudiante"])
    pieces.take(storage, "LAB-LEGO-1", 4, people["otra"])
    assert [(row["user_name"], row["pieces"]) for row in pieces.balances(storage, "LAB-LEGO-1")] == [
        ("Persona estudiante", 10), ("Otra Persona", 4)]
    assert pieces.total_out(storage, "LAB-LEGO-1") == 14
    with pytest.raises(PermissionError):                                     # otro cliente no devuelve por ti
        pieces.give_back(storage, "LAB-LEGO-1", 1, people["otra"], for_user=people["estudiante"])
    ok, _ = pieces.give_back(storage, "LAB-LEGO-1", 10, people["profesor"], for_user=people["estudiante"])
    assert ok and pieces.balance_of(storage, "LAB-LEGO-1", people["estudiante"]["id"]) == 0
    assert pieces.balance_of(storage, "LAB-LEGO-1", people["otra"]["id"]) == 4


def test_operator_cannot_take_pieces(lab):
    storage, people = lab
    with pytest.raises(PermissionError):
        pieces.take(storage, "LAB-LEGO-1", 1, people["operario"])


def test_movements_are_in_the_chain_of_custody_in_plain_language(lab):
    storage, people = lab
    me = people["estudiante"]
    pieces.take(storage, "LAB-LEGO-1", 8, me, "Práctica")
    pieces.give_back(storage, "LAB-LEGO-1", 3, people["profesor"], for_user=me)
    events = [event for event in storage.get_trace_events(item_id="LAB-LEGO-1")
              if event["event_type"] in traceability.PIECE_EVENTS]
    assert [event["event_type"] for event in events] == [traceability.EVENT_PIECES_TAKEN,
                                                         traceability.EVENT_PIECES_RETURNED]
    assert all(event["owner_id"] == me["id"] for event in events)
    assert traceability.describe_event(events[0]) == "Retiró 8 pieza(s) · «Práctica»"
    assert "Devolvió 3 pieza(s) (registrado por Persona profesor)" == traceability.describe_event(events[1])


def test_suggestion_by_name_or_category():
    assert pieces.looks_like_pieces({"name": "Kit LEGO Spike"})
    assert pieces.looks_like_pieces({"name": "Juego", "category": "Bloques de construcción"})
    assert not pieces.looks_like_pieces({"name": "Multímetro", "category": "Electrónica"})


def test_pieces_do_not_block_a_normal_loan_of_the_set(lab):
    storage, people = lab
    pieces.take(storage, "LAB-LEGO-1", 5, people["estudiante"])
    assert loans.checkout(storage, "LAB-LEGO-1", 1, people["estudiante"])[0]


# --- vistas reales ------------------------------------------------------------------------

def _view_script():
    import streamlit as st

    from core.storage import LabStorage
    from views import escanear

    st.session_state.storage = LabStorage()
    st.session_state.user = st.session_state.test_user
    escanear._render_item_actions(st.session_state.storage.get_item(st.session_state.test_item)
                                  | {"available": 2})


def _open(person, item_id):
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_view_script, default_timeout=60)
    at.session_state["test_user"] = {k: person[k] for k in ("id", "full_name", "role", "institutional_email")}
    at.session_state["test_item"] = item_id
    at.run()
    assert not at.exception
    return at


def test_buscar_card_offers_pieces_only_for_flagged_items(lab):
    storage, people = lab
    flagged = _open(people["estudiante"], "LAB-LEGO-1")
    assert any(button.label == "Retirar piezas" for button in flagged.button)
    plain = _open(people["estudiante"], "LAB-MIC-1")
    assert not any(button.label == "Retirar piezas" for button in plain.button)
    [b for b in flagged.button if b.label == "Retirar piezas"][0].click().run()
    assert pieces.balance_of(storage, "LAB-LEGO-1", people["estudiante"]["id"]) == 1
    again = _open(people["estudiante"], "LAB-LEGO-1")
    assert any(button.label == "Devolver piezas" for button in again.button)
    staff = _open(people["profesor"], "LAB-LEGO-1")
    assert any(expander.label.startswith("Quién tiene piezas") for expander in staff.expander)
