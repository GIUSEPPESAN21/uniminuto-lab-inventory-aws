# -*- coding: utf-8 -*-
"""Punto 7 de v1.15: CSV de eventos de la lúdica (columnas exactas, sin datos personales)
y scripts de siembra/reinicio que se niegan a tocar la base real."""

import csv
import io
import os

import pytest

from core import auth, dispatch, ludica, service_requests, traceability
from core import storage as storage_module
from core.storage import LabStorage

CODE = "LAB-MIC-01"
EXPECTED_COLUMNS = ["sesion_id", "timestamp", "estacion", "tipo_evento", "item_codigo",
                    "transaccion_id", "usuario_sim", "resultado_sistema"]
ROLES = ("estudiante", "operario", "encargado", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    hashed = auth.hash_password("ClaveSegura123")
    people = {role: storage.create_user(f"Nombre Secreto {role}", f"{role}.privado@uniminuto.edu.co", hashed, role,
                                        student_id=f"ID-PRIV-{role}")
              for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 20}, CODE, is_new=True)
    return storage, people


def _run_order(storage, people, wrong_code=True):
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 2)
    service_requests.review_request(storage, row["id"], "approved", people["encargado"])
    operator, supervisor = people["operario"], people["encargado"]
    dispatch.claim_request(storage, row["id"], operator)
    dispatch.start_picking(storage, row["id"], operator)
    if wrong_code:
        dispatch.register_reading(storage, row["id"], operator, "OTRO-77", "manual")
    dispatch.register_reading(storage, row["id"], operator, CODE, "barcode_gun")
    dispatch.register_reading(storage, row["id"], operator, CODE, "nfc")
    dispatch.complete_picking(storage, row["id"], operator)
    dispatch.receive_request(storage, row["id"], supervisor)
    dispatch.deliver_request(storage, row["id"], supervisor)
    dispatch.confirm_client_receipt(storage, row["id"], people["estudiante"])
    return row["id"]


def _parse(data: bytes):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))


# --- CSV de eventos ----------------------------------------------------------------------

def test_the_csv_has_exactly_the_agreed_columns_in_order(lab):
    storage, people = lab
    _run_order(storage, people)
    data = ludica.events_csv(storage, people["encargado"])
    assert not data.startswith(b"\xef\xbb\xbf")                           # sin BOM: la primera columna es limpia
    assert data.decode("utf-8").splitlines()[0] == ",".join(EXPECTED_COLUMNS)
    assert list(ludica.COLUMNS) == EXPECTED_COLUMNS
    rows = _parse(data)
    assert rows and all(list(row) == EXPECTED_COLUMNS for row in rows)


def test_the_csv_has_no_personal_data_at_all(lab):
    storage, people = lab
    request_id = _run_order(storage, people)
    text = ludica.events_csv(storage, people["maestro"]).decode("utf-8")
    for role, person in people.items():
        for secret in (person["full_name"], person["institutional_email"], person["id"], f"ID-PRIV-{role}"):
            assert secret not in text
    assert request_id not in text and "uniminuto.edu.co" not in text and "Nombre Secreto" not in text
    assert "Microscopio" not in text                                       # tampoco nombres de producto ni texto libre


def test_rows_describe_the_whole_flow_by_station_with_stable_pseudonyms(lab):
    storage, people = lab
    _run_order(storage, people)
    rows = _parse(ludica.events_csv(storage, people["encargado"], sid="LUD-PRUEBA"))
    assert {row["sesion_id"] for row in rows} == {"LUD-PRUEBA"}
    kinds = [row["tipo_evento"] for row in rows]
    for expected in ("order_requested", "order_approved", "order_authorized", "order_claimed", "order_picked",
                     "order_received", "order_delivered", "order_client_confirmed"):
        assert expected in kinds
    by_kind = {row["tipo_evento"]: row for row in rows}
    assert by_kind["order_requested"]["estacion"] == "cliente"
    assert by_kind["order_claimed"]["estacion"] == "operario"
    assert by_kind["order_approved"]["estacion"] == "supervisor"
    assert len({row["transaccion_id"] for row in rows}) == 1               # un solo pedido → una transacción
    assert by_kind["order_claimed"]["usuario_sim"] != by_kind["order_approved"]["usuario_sim"]
    assert by_kind["order_claimed"]["usuario_sim"] and by_kind["order_picked"]["item_codigo"] == CODE
    assert [r["timestamp"] for r in rows] == sorted(r["timestamp"] for r in rows)
    other_salt = _parse(ludica.events_csv(storage, people["encargado"], salt="otra-sal"))
    assert other_salt[0]["transaccion_id"] != rows[0]["transaccion_id"]    # la sal cambia los seudónimos
    again = _parse(ludica.events_csv(storage, people["encargado"], sid="LUD-PRUEBA"))
    assert [r["usuario_sim"] for r in again] == [r["usuario_sim"] for r in rows]   # y son estables


def test_rejections_appear_as_rejected_without_the_system_message(lab):
    storage, people = lab
    _run_order(storage, people)
    other = storage.create_user("Otro Operario", "otro.privado@uniminuto.edu.co", "x", "operario")
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, 1)
    service_requests.review_request(storage, row["id"], "approved", people["encargado"])
    dispatch.claim_request(storage, row["id"], people["operario"])
    ok, message, _ = dispatch.claim_request(storage, row["id"], other)       # el mensaje nombra a quien lo tiene
    assert not ok and "Nombre Secreto operario" in message
    rows = _parse(ludica.events_csv(storage, people["maestro"]))
    rejected = [r for r in rows if r["resultado_sistema"] == "rechazado"]
    assert {r["tipo_evento"] for r in rejected} >= {"order_scan_failed", "order_claimed"}
    assert any(r["tipo_evento"] == "order_scan_failed" and r["item_codigo"] == "OTRO-77" for r in rejected)
    assert "Nombre Secreto" not in ludica.events_csv(storage, people["maestro"]).decode("utf-8")
    assert {r["resultado_sistema"] for r in rows} == {"ok", "rechazado"}


def test_only_roles_with_dispatch_reports_can_export(lab):
    storage, people = lab
    for role in ("estudiante", "operario"):
        with pytest.raises(PermissionError):
            ludica.events_csv(storage, people[role])


def test_the_session_id_defaults_to_the_scenario_and_date_and_can_be_set(lab):
    storage, people = lab
    assert ludica.session_id(storage).startswith("LUD-B-")
    storage.set_setting(ludica.SETTING_SESSION_ID, "SESION-7")
    assert ludica.session_id(storage) == "SESION-7"


# --- siembra y reinicio ------------------------------------------------------------------

def _sandbox(tmp_path, monkeypatch, name="simulacion.xlsx", **seed):
    path = tmp_path / name
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(path))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    storage = LabStorage()
    summary = ludica.seed_simulation(storage, orders=4, **seed)
    return path, storage, summary


def test_seed_builds_a_marked_simulation_with_fictional_people(tmp_path, monkeypatch):
    path, storage, summary = _sandbox(tmp_path, monkeypatch, scenario="C", session="LUD-DEMO")
    assert ludica.sandbox_marked(str(path))
    assert (summary["clientes"], summary["operarios"], summary["supervisores"], summary["pedidos"]) == (8, 8, 4, 4)
    emails = [u["institutional_email"] for u in storage.get_all_users()]
    assert emails and all(email.endswith("@ludica.invalid") for email in emails)
    assert dispatch.scenario_key(storage) == "C" and ludica.session_id(storage) == "LUD-DEMO"
    assert len(storage.get_service_requests(status="pending")) == 4


def test_seed_refuses_a_database_that_has_real_users(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(tmp_path / "otra.xlsx"))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    storage = LabStorage()
    storage.create_user("Persona Real", "real@uniminuto.edu.co", "x", "maestro")
    with pytest.raises(ludica.RealDatabaseError):
        ludica.seed_simulation(storage)
    assert storage.count_users() == 1


def test_reset_clears_the_operational_data_and_keeps_people_and_products(tmp_path, monkeypatch):
    path, storage, summary = _sandbox(tmp_path, monkeypatch)
    people = {u["role"]: u for u in storage.get_all_users()}
    row = storage.get_service_requests()[0]
    service_requests.review_request(storage, row["id"], "approved", people["encargado"])
    assert storage.get_trace_events()
    removed = storage.reset_simulation()
    assert removed["service_requests"] == 4 and removed["trace_events"] >= 1
    assert storage.get_service_requests() == [] and storage.get_trace_events() == []
    assert storage.get_all_loans() == [] and storage.count_users() == 8 + 8 + 4 + 1
    assert len(storage.get_all_items()) == summary["productos"]
    assert {h["type"] for item in storage.get_all_items() for h in storage.get_item_history(item["id"])} == {"Alta"}
    assert ludica.sandbox_marked(str(path))                                # la marca y el escenario se conservan


def test_the_storage_refuses_to_reset_a_database_without_the_marker(lab):
    storage, people = lab
    _run_order(storage, people)
    with pytest.raises(PermissionError):
        storage.reset_simulation()
    assert storage.get_service_requests()                                  # nada se borró


def test_assert_sandbox_rejects_the_real_name_github_and_unmarked_files(tmp_path, monkeypatch):
    with pytest.raises(ludica.RealDatabaseError, match="REAL"):
        ludica.assert_sandbox(str(tmp_path / "UNIMINUTO_LAB_DB.xlsx"), creating=True)
    with pytest.raises(ludica.RealDatabaseError):
        ludica.assert_sandbox("", creating=True)
    unmarked = tmp_path / "datos.xlsx"
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(unmarked))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    LabStorage().create_user("Persona Real", "real@uniminuto.edu.co", "x", "maestro")
    with pytest.raises(ludica.RealDatabaseError, match="no está marcado"):
        ludica.assert_sandbox(str(unmarked))
    with pytest.raises(ludica.RealDatabaseError, match="no existe"):
        ludica.assert_sandbox(str(tmp_path / "falta.xlsx"))
    monkeypatch.setattr(ludica, "safe_secret", lambda key, default=None: "x" if key.startswith("GITHUB") else default)
    with pytest.raises(ludica.RealDatabaseError, match="GITHUB"):
        ludica.assert_sandbox(str(tmp_path / "nueva.xlsx"), creating=True)


def _script(name):
    import importlib
    import sys
    from pathlib import Path

    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def test_the_scripts_seed_and_reset_a_simulation_and_refuse_the_real_database(tmp_path, monkeypatch, capsys):
    for name in ("EXCEL_PATH", "_is_github_configured"):                      # los scripts los parchean: se restauran
        monkeypatch.setattr(storage_module, name, getattr(storage_module, name))
    seed, reset = _script("seed_ludica"), _script("reset_ludica")
    sim = str(tmp_path / "sim.xlsx")

    assert seed.main(["--db", str(tmp_path / "UNIMINUTO_LAB_DB.xlsx")]) == 2         # el nombre de la base real
    assert "RECHAZADO" in capsys.readouterr().err
    assert seed.main(["--db", sim, "--orders", "3", "--clients", "2", "--operators", "1", "--supervisors", "1",
                      "--password", "ClaveSim123"]) == 0
    out = capsys.readouterr().out
    assert "Simulación creada" in out and "pedidos=3" in out and "ClaveSim123" in out
    assert seed.main(["--db", sim]) == 2                                              # ya existe, sin --force
    assert reset.main(["--db", sim]) == 2 and "--yes" in capsys.readouterr().err     # falta confirmar
    assert reset.main(["--db", sim, "--yes"]) == 0
    assert "service_requests=3" in capsys.readouterr().out

    real_looking = tmp_path / "datos-reales.xlsx"
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(real_looking))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    LabStorage().create_user("Persona Real", "real@uniminuto.edu.co", "x", "maestro")
    assert reset.main(["--db", str(real_looking), "--yes"]) == 2                      # sin la marca, no
    assert "no está marcado" in capsys.readouterr().err
    assert len(LabStorage().get_all_users()) == 1                                     # y no se tocó
    assert os.path.basename(real_looking) not in sim


def test_a_simulated_run_exports_a_clean_csv_and_reset_empties_it(tmp_path, monkeypatch):
    path, storage, _ = _sandbox(tmp_path, monkeypatch)
    people = {u["role"]: u for u in storage.get_all_users()}
    pending = storage.get_service_requests(status="pending")[0]
    service_requests.review_request(storage, pending["id"], "approved", people["encargado"])
    rows = _parse(ludica.events_csv(storage, people["maestro"]))
    assert any(r["tipo_evento"] == "order_approved" for r in rows)
    assert all(not r["usuario_sim"].endswith("@ludica.invalid") for r in rows)
    storage.reset_simulation()
    assert _parse(ludica.events_csv(storage, people["maestro"])) == []
    assert traceability.STAGE_APPROVED == "approved"
