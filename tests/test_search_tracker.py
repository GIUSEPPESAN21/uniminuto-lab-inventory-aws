# -*- coding: utf-8 -*-
"""v1.17 · búsqueda única y seguimiento sencillo del pedido para el cliente."""

import pytest

from core import dispatch, search, service_requests, tracker
from core.storage import LabStorage

ITEMS = [
    {"id": "LAB-MIC-01", "name": "Microscopio óptico", "category": "Óptica", "description": "Binocular 40x",
     "location": "Gabinete azul", "item_type": "standalone", "status": "active"},
    {"id": "RES-220", "name": "Resistencia 220 Ω", "category": "Electrónica", "description": "Carbón 1/4 W",
     "location": "Caja 04", "item_type": "child", "status": "active"},
    {"id": "RES-330", "name": "Resistencia 330 Ω", "category": "Electrónica", "description": "",
     "location": "Caja 04", "item_type": "child", "status": "active"},
    {"id": "OLD-1", "name": "Resistencia vieja", "category": "", "description": "", "location": "",
     "item_type": "child", "status": "retired"},
    {"id": "2-1-01-00-000", "name": "Contenedor 01", "category": "", "description": "", "location": "",
     "item_type": "master", "status": "active"},
]


def _ids(rows):
    return [row["id"] for row in rows]


# --- búsqueda ---------------------------------------------------------------------------------

def test_normalize_ignores_accents_and_case():
    assert search.normalize("  ÓPTICA Resistencía ") == "optica resistencia"
    assert search.normalize(None) == ""


def test_exact_code_wins_and_names_match_without_accents():
    assert _ids(search.search_items(ITEMS, "res-330"))[0] == "RES-330"
    assert _ids(search.search_items(ITEMS, "microscopio optico")) == ["LAB-MIC-01"]
    assert _ids(search.search_items(ITEMS, "OPTICA")) == ["LAB-MIC-01"]               # por categoría


def test_every_word_must_match_and_retired_items_never_appear():
    assert _ids(search.search_items(ITEMS, "resistencia 220")) == ["RES-220"]
    assert "OLD-1" not in _ids(search.search_items(ITEMS, "resistencia"))
    assert search.search_items(ITEMS, "resistencia zzz") == [] and search.search_items(ITEMS, "  ") == []


def test_kinds_limit_the_results_and_the_limit_is_respected():
    assert _ids(search.search_items(ITEMS, "contenedor", kinds={"standalone", "child"})) == []
    assert _ids(search.search_items(ITEMS, "contenedor")) == ["2-1-01-00-000"]
    many = [{"id": f"X-{n}", "name": f"Tornillo {n}", "item_type": "standalone"} for n in range(30)]
    assert len(search.search_items(many, "tornillo")) == search.MAX_RESULTS
    assert len(search.search_items(many, "tornillo", limit=3)) == 3


def test_request_ids_are_recognised():
    assert search.looks_like_request_id("#a1b2c3") and search.looks_like_request_id("A1B2C3D4")
    assert not search.looks_like_request_id("microscopio") and not search.looks_like_request_id("#xyz")


# --- seguimiento del cliente ------------------------------------------------------------------

@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role)
              for role in ("estudiante", "operario", "encargado", "maestro")}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 5}, "LAB-MIC-01", is_new=True)
    return storage, people


def _track(storage, request_id):
    request = storage.get_service_request(request_id)
    return tracker.client_tracker(request, storage.get_trace_events(request_id=request_id))


def _states(result):
    return [step["state"] for step in result["steps"]]


def test_product_order_walks_through_the_client_friendly_steps(lab):
    storage, people = lab
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", "LAB-MIC-01", 2)
    rid = row["id"]
    result = _track(storage, rid)
    assert result["headline"].startswith("Enviada") and _states(result)[0] == "current"

    assert service_requests.review_request(storage, rid, "approved", people["encargado"])[0]
    result = _track(storage, rid)
    assert result["headline"].startswith("Aprobada") and _states(result)[:2] == ["done", "current"]

    assert dispatch.claim_request(storage, rid, people["operario"])[0]
    assert _track(storage, rid)["headline"] == "Preparando tu pedido"
    assert dispatch.confirm_manual_picking(storage, rid, people["operario"], True)[0]
    assert _track(storage, rid)["headline"] == "Preparando tu pedido"
    assert dispatch.receive_request(storage, rid, people["encargado"])[0]
    result = _track(storage, rid)
    assert result["headline"] == "Listo para reclamar" and not result["can_confirm"]
    assert _states(result) == ["done", "done", "done", "current", "pending"]

    assert dispatch.deliver_request(storage, rid, people["encargado"])[0]
    result = _track(storage, rid)
    assert result["can_confirm"] and "Recibí mi pedido" in result["hint"]

    assert dispatch.confirm_client_receipt(storage, rid, people["estudiante"])[0]
    result = _track(storage, rid)
    assert result["headline"] == "Completado" and _states(result) == ["done"] * 5 and not result["can_confirm"]


def test_rejected_cancelled_and_returned_orders_end_clearly(lab):
    storage, people = lab
    rejected, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", "LAB-MIC-01", 1)
    assert service_requests.review_request(storage, rejected["id"], "rejected", people["encargado"], "sin stock")[0]
    result = _track(storage, rejected["id"])
    assert result["headline"] == "Rechazada" and result["tone"] == "danger" and "blocked" in _states(result)

    cancelled, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", "LAB-MIC-01", 1)
    assert service_requests.cancel_request(storage, cancelled["id"], people["estudiante"])[0]
    assert _track(storage, cancelled["id"])["headline"] == "Cancelada"


def test_service_and_reservation_trackers(lab):
    storage, people = lab
    row, _, _ = service_requests.submit_request(
        storage, people["estudiante"], "service", service_name="Impresión 3D", description="Una pieza")
    assert _track(storage, row["id"])["headline"].startswith("Enviada")
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    result = _track(storage, row["id"])
    assert result["headline"] == "Aprobada" and len(result["steps"]) == 4
    assert tracker.reservation_tracker({"status": "pending"})["headline"].startswith("Enviada")
    assert tracker.reservation_tracker({"status": "approved"})["tone"] == "success"
    assert tracker.reservation_tracker({"status": "rejected"})["headline"] == "Rechazada"
