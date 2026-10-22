# -*- coding: utf-8 -*-
"""Punto 5 de v1.15: rendimiento del despacho.

- Las listas de Despacho se calculan una vez por versión de datos y solo con
  etapas abiertas.
- ``SYNC_MODE=deferred`` (apagado por defecto): la escritura solo toca la caché y
  un vaciado único escribe el archivo y publica en GitHub.
- ``scripts/bench_concurrency.py`` corre de punta a punta (con parámetros mínimos).
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import auth, dispatch, service_requests
from core import storage as storage_module
from core.storage import LabStorage

CODE = "LAB-MIC-01"
ROLES = ("estudiante", "operario", "encargado", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    hashed = auth.hash_password("ClaveSegura123")
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", hashed, role)
              for role in ROLES}
    storage.save_item({"name": "Microscopio", "item_type": "standalone", "quantity": 500}, CODE, is_new=True)
    return storage, people


def _order(storage, people, quantity=1):
    row, _, _ = service_requests.submit_request(storage, people["estudiante"], "product", CODE, quantity)
    assert service_requests.review_request(storage, row["id"], "approved", people["encargado"])[0]
    return row["id"]


def _deliver(storage, people, request_id):
    operator, supervisor = people["operario"], people["encargado"]
    assert dispatch.claim_request(storage, request_id, operator)[0]
    assert dispatch.start_picking(storage, request_id, operator)[0]
    assert dispatch.register_reading(storage, request_id, operator, CODE, "manual")[0]
    assert dispatch.complete_picking(storage, request_id, operator)[0]
    assert dispatch.receive_request(storage, request_id, supervisor)[0]
    assert dispatch.deliver_request(storage, request_id, supervisor)[0]


# --- listas: una vez por versión y solo etapas abiertas ---------------------------------

def test_lists_show_only_open_stages(lab):
    storage, people = lab
    open_one, claimed, delivered = (_order(storage, people) for _ in range(3))
    assert dispatch.claim_request(storage, claimed, people["operario"])[0]
    _deliver(storage, people, delivered)
    operator, supervisor = people["operario"], people["encargado"]
    assert [r["id"] for r in dispatch.queue_for(storage, operator)] == [open_one]
    assert [r["id"] for r in dispatch.my_orders(storage, operator)] == [claimed]
    assert dispatch.review_queue(storage, supervisor) == []              # lo entregado ya no se revisa
    assert [r["id"] for r, _ in dispatch.claimed_orders(storage, supervisor)] == [claimed]
    assert delivered not in {order["request"]["id"] for order in dispatch.open_orders(storage)}


def test_the_open_orders_are_computed_once_per_data_version(lab, monkeypatch):
    storage, people = lab
    for _ in range(3):
        _order(storage, people)
    reads = []
    original = LabStorage.get_trace_events
    monkeypatch.setattr(LabStorage, "get_trace_events",
                        lambda self, *a, **k: (reads.append(k.get("request_id")), original(self, *a, **k))[1])
    operator, supervisor = people["operario"], people["encargado"]
    for _ in range(5):                                                   # 5 refrescos de cada lista y persona
        dispatch.queue_for(storage, operator)
        dispatch.my_orders(storage, operator)
        dispatch.review_queue(storage, supervisor)
    assert len(reads) == 3                                               # una lectura por pedido abierto, UNA vez
    assert dispatch.claim_request(storage, dispatch.queue_for(storage, operator)[0]["id"], operator)[0]
    reads.clear()
    dispatch.queue_for(storage, operator)
    assert len(reads) == 3                                               # una escritura invalida la caché


def test_closed_orders_are_remembered_and_not_read_again(lab, monkeypatch):
    storage, people = lab
    done = _order(storage, people)
    _deliver(storage, people, done)
    pending = _order(storage, people)
    dispatch.open_orders(storage)
    assert done in dispatch._orders_cache["closed"]
    seen = []
    original = LabStorage.get_service_requests
    monkeypatch.setattr(LabStorage, "get_service_requests",
                        lambda self, *a, **k: (seen.append(k.get("exclude_ids")), original(self, *a, **k))[1])
    reads = []
    original_events = LabStorage.get_trace_events
    monkeypatch.setattr(LabStorage, "get_trace_events",
                        lambda self, *a, **k: (reads.append(k.get("request_id")), original_events(self, *a, **k))[1])
    assert dispatch.claim_request(storage, pending, people["operario"])[0]   # nueva versión
    dispatch.my_orders(storage, people["operario"])
    assert done in (seen[-1] or set()) and done not in reads             # el cerrado no se vuelve a leer
    assert pending in reads


def test_a_reload_from_the_file_forgets_what_was_closed(lab):
    storage, people = lab
    request_id = _order(storage, people)
    _deliver(storage, people, request_id)
    dispatch.open_orders(storage)
    assert request_id in dispatch._orders_cache["closed"]
    storage_module._cached_dfs = None                                    # p. ej. se descargó otra base de GitHub
    storage.refresh_data()
    dispatch.open_orders(storage)
    assert request_id in dispatch._orders_cache["closed"]                # lo vuelve a aprender del archivo
    generation = storage.get_data_state()["generation"]
    storage_module._cached_dfs = None
    storage.refresh_data()
    assert storage.get_data_state()["generation"] == generation + 1


def test_stores_without_data_state_are_never_cached(lab):
    class Bare:
        def __init__(self, inner):
            self.inner, self.calls = inner, 0

        def get_service_requests(self, **kwargs):
            self.calls += 1
            return self.inner.get_service_requests(**kwargs)

        def get_trace_events(self, **kwargs):
            return self.inner.get_trace_events(**kwargs)

    storage, people = lab
    _order(storage, people)
    bare = Bare(storage)
    dispatch.open_orders(bare)
    dispatch.open_orders(bare)
    assert bare.calls == 2


# --- SYNC_MODE=deferred -----------------------------------------------------------------

class FakeGithub:
    """Sustituye a ``_github_request``: cuenta PUT y permite fallos y conflictos."""

    def __init__(self):
        self.sha, self.puts, self.gets, self.put_status, self.remote_changes = "sha-0", 0, 0, 200, False
        self.content = None          # bytes del archivo que "tiene" GitHub (para probar la descarga)

    def __call__(self, method, url, attempts=3, session=None, **kwargs):
        if method == "GET":
            self.gets += 1
            if self.content is None and self.puts == 0 and not self.remote_changes:
                return SimpleNamespace(status_code=404, text="", json=lambda: {})   # aún no existe en GitHub
            sha = "otra-version" if self.remote_changes else self.sha
            body = {"sha": sha}
            if self.content is not None:
                import base64
                body.update(encoding="base64", content=base64.b64encode(self.content).decode("ascii"))
            return SimpleNamespace(status_code=200, text="", json=lambda: body)
        self.puts += 1
        if self.put_status != 200:
            return SimpleNamespace(status_code=self.put_status, text="boom", json=lambda: {})
        self.sha = f"sha-{self.puts}"
        return SimpleNamespace(status_code=200, text="", json=lambda: {"content": {"sha": self.sha}})


@pytest.fixture
def deferred(monkeypatch):
    github = FakeGithub()
    settings = {"SYNC_MODE": "deferred", "SYNC_DEFER_SECONDS": 0, "SYNC_RETRY_SECONDS": 0.05}
    monkeypatch.setattr(storage_module, "safe_secret", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(storage_module, "_is_github_configured", lambda: True)
    monkeypatch.setattr(storage_module, "_github_api_url", lambda: "https://test.invalid/contents/db.xlsx")
    monkeypatch.setattr(storage_module, "_github_request", github)
    storage_module._set_remote_sha(github.sha)
    github.settings = settings
    return github


def _wait(condition, seconds=5.0):
    limit = time.time() + seconds
    while time.time() < limit:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _file_users(path):
    import pandas as pd

    return pd.read_excel(path, sheet_name="users", dtype=str).fillna("")


def test_sync_mode_is_normal_unless_deferred_is_requested(monkeypatch):
    assert storage_module.sync_mode() == "sync"
    for value, expected in (("deferred", "deferred"), (" DEFERRED ", "deferred"), ("otro", "sync"), ("", "sync")):
        monkeypatch.setattr(storage_module, "safe_secret", lambda key, default=None, v=value: v if key == "SYNC_MODE" else default)
        assert storage_module.sync_mode() == expected


def test_normal_mode_still_writes_the_file_and_publishes_on_every_write(monkeypatch):
    github = FakeGithub()
    monkeypatch.setattr(storage_module, "_is_github_configured", lambda: True)
    monkeypatch.setattr(storage_module, "_github_api_url", lambda: "https://test.invalid/contents/db.xlsx")
    monkeypatch.setattr(storage_module, "_github_request", github)
    storage_module._set_remote_sha(github.sha)
    storage = LabStorage()
    before = github.puts
    storage.create_user("Ana", "ana@uniminuto.edu.co", "x", "estudiante")
    assert github.puts == before + 1 and len(_file_users(storage_module.EXCEL_PATH)) == 1
    assert storage_module.get_sync_status()["mode"] == "sync" and not storage_module.get_sync_status()["pending"]


def test_deferred_writes_touch_only_the_cache_until_the_flush(lab, deferred):
    storage, people = lab
    storage_module.flush_now()
    deferred.settings["SYNC_DEFER_SECONDS"] = 60          # el hilo de fondo no interviene en esta prueba
    puts, before = deferred.puts, len(_file_users(storage_module.EXCEL_PATH))
    for number in range(5):
        storage.create_user(f"Persona {number}", f"p{number}@uniminuto.edu.co", "x", "estudiante")
    assert len(storage.get_all_users()) == before + 5                     # todas las sesiones leen la caché al instante
    assert len(_file_users(storage_module.EXCEL_PATH)) == before          # el archivo aún no cambió
    assert deferred.puts == puts                                          # ni se publicó nada
    status = storage_module.get_sync_status()
    assert status["mode"] == "deferred" and status["pending"] is True
    assert storage_module.flush_now() is True
    assert deferred.puts == puts + 1                                      # UNA publicación para las 5 escrituras
    assert len(_file_users(storage_module.EXCEL_PATH)) == before + 5
    assert storage_module.get_sync_status()["pending"] is False
    assert storage_module.flush_now() is True and deferred.puts == puts + 1   # sin nada pendiente no hace nada


def test_the_background_flusher_publishes_a_burst_once(lab, deferred):
    storage, people = lab
    storage_module.flush_now()
    deferred.settings["SYNC_DEFER_SECONDS"] = 0.3
    puts = deferred.puts
    for number in range(6):
        storage.create_user(f"Persona {number}", f"q{number}@uniminuto.edu.co", "x", "estudiante")
    assert _wait(lambda: storage_module.get_sync_status()["pending"] is False)
    assert deferred.puts - puts <= 2                                      # 6 escrituras, a lo sumo 2 publicaciones
    assert len(_file_users(storage_module.EXCEL_PATH)) == len(storage.get_all_users())


def test_a_failed_publish_stays_pending_and_is_retried(lab, deferred):
    storage, people = lab
    storage_module.flush_now()
    deferred.put_status = 500
    storage.create_user("Ana", "ana2@uniminuto.edu.co", "x", "estudiante")
    assert _wait(lambda: storage_module.get_sync_status()["ok"] is False)
    assert storage_module.get_sync_status()["pending"] is True            # no se da por publicado
    deferred.put_status = 200
    assert _wait(lambda: storage_module.get_sync_status()["pending"] is False)   # el reintento automático lo logra
    assert storage_module.get_sync_status()["ok"] is True


def test_a_remote_conflict_is_flagged_and_does_not_spin(lab, deferred):
    storage, people = lab
    storage_module.flush_now()
    deferred.remote_changes = True
    storage.create_user("Ana", "ana3@uniminuto.edu.co", "x", "estudiante")
    assert _wait(lambda: storage_module.get_sync_status()["conflict"] is True)
    puts = deferred.puts
    time.sleep(0.4)
    assert deferred.puts == puts                                          # espera a quien administra, no reintenta
    assert storage_module.get_sync_status()["pending"] is True


def test_retry_sync_flushes_synchronously_in_deferred_mode(lab, deferred):
    storage, people = lab
    storage_module.flush_now()
    deferred.settings["SYNC_DEFER_SECONDS"] = 60
    storage.create_user("Ana", "ana4@uniminuto.edu.co", "x", "estudiante")
    assert storage.retry_sync() is True
    assert storage_module.get_sync_status()["pending"] is False


def test_the_first_write_creates_the_file_even_when_deferred(deferred, tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "EXCEL_PATH", str(tmp_path / "nuevo.xlsx"))
    monkeypatch.setattr(storage_module, "_cached_dfs", None)
    deferred.settings["SYNC_DEFER_SECONDS"] = 60
    LabStorage()
    assert Path(storage_module.EXCEL_PATH).exists()


def test_a_pull_discards_the_pending_file_write(lab, deferred):
    storage, people = lab
    storage_module.flush_now()
    deferred.settings["SYNC_DEFER_SECONDS"] = 60
    deferred.content = Path(storage_module.EXCEL_PATH).read_bytes()      # lo que ya publicó GitHub
    storage.create_user("Ana", "ana5@uniminuto.edu.co", "x", "estudiante")
    assert storage_module._sync_state["file_dirty"] is True
    with storage_module._excel_lock:
        assert storage_module._github_pull() is True                      # el archivo descargado manda
    assert storage_module._sync_state["file_dirty"] is False
    assert storage_module.flush_now() is True                             # y no vuelve a pisarlo con lo viejo
    assert storage.get_user_by_email("ana5@uniminuto.edu.co") is None


# --- el banco de pruebas corre de punta a punta -----------------------------------------

def test_the_concurrency_bench_runs_end_to_end(monkeypatch, capsys):
    root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root / "scripts"))
    for name in ("EXCEL_PATH", "_is_github_configured", "_github_api_url", "_github_request", "safe_secret"):
        monkeypatch.setattr(storage_module, name, getattr(storage_module, name))   # el banco los restaura
    original_path = storage_module.EXCEL_PATH
    sys.modules.pop("bench_concurrency", None)
    import bench_concurrency

    code = bench_concurrency.main([
        "--mode", "deferred", "--operators", "2", "--supervisors", "2", "--orders", "3",
        "--latency-min", "0.001", "--latency-max", "0.002", "--arrival", "0.05", "--refresh", "0.05",
        "--timeout", "120", "--threshold", "5",
    ])
    out = capsys.readouterr().out
    assert code == 0 and "SYNC_MODE=deferred" in out and "WRITES" in out and "3/3 pedidos completos" in out
    assert storage_module.EXCEL_PATH == original_path                      # nunca deja apuntando a su base temporal
    assert "bench-despacho-" not in storage_module.EXCEL_PATH
