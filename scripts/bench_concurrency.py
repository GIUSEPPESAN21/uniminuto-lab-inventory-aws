#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Banco de pruebas de concurrencia del despacho (latencia de GitHub simulada).

Simula una lúdica con 8 operarios y 4 supervisores trabajando a la vez sobre una
base TEMPORAL (nunca la real) y mide cuánto tarda cada acción que escribe
(aprobar, tomar, completar picking, recibir, entregar, confirmar). Cada llamada a
la API de GitHub (GET del SHA y PUT del archivo) espera una latencia aleatoria
entre ``--latency-min`` y ``--latency-max`` segundos; no hay red de verdad.

Uso:
    python scripts/bench_concurrency.py                    # sync y deferred, 8 + 4
    python scripts/bench_concurrency.py --mode sync --orders 16
    python scripts/bench_concurrency.py --latency-min 0.02 --latency-max 0.04   # prueba rápida

Sale con código 1 si el p95 de las escrituras supera ``--threshold`` (2 s) en el
modo ``--mode`` elegido (en ``both`` se evalúa el modo SYNC_MODE por defecto).
"""

import argparse
import logging
import random
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WRITE_ACTIONS = ("submit", "approve", "claim", "complete", "receive", "deliver", "confirm")


class FakeGithub:
    """Sustituye a ``core.storage._github_request``: espera y responde como la API."""

    def __init__(self, low: float, high: float, seed: int = 0):
        self.low, self.high = low, high
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.sha, self.counter, self.calls, self.puts = "sha-0", 0, 0, 0

    def __call__(self, method, url, attempts=3, session=None, **kwargs):
        with self.lock:
            delay = self.rng.uniform(self.low, self.high)
            self.calls += 1
        time.sleep(delay)
        with self.lock:
            if method == "GET":
                if self.puts == 0:      # la base aún no existe en GitHub: la primera escritura la crea
                    return SimpleNamespace(status_code=404, text="", json=lambda: {})
                return SimpleNamespace(status_code=200, text="", json=lambda sha=self.sha: {"sha": sha})
            self.puts += 1
            self.counter += 1
            self.sha = f"sha-{self.counter}"
            return SimpleNamespace(status_code=200, text="",
                                   json=lambda sha=self.sha: {"content": {"sha": sha}})


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples: dict = {}
        self.errors: list = []

    def add(self, name: str, seconds: float) -> None:
        with self.lock:
            self.samples.setdefault(name, []).append(seconds)

    def error(self, message: str) -> None:
        with self.lock:
            self.errors.append(message)

    def timed(self, name: str, func, *args, **kwargs):
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            self.add(name, time.perf_counter() - started)


def percentile(values: list, q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(recorder: Recorder) -> dict:
    rows = {}
    for name, values in sorted(recorder.samples.items()):
        rows[name] = {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
                      "max": max(values), "mean": statistics.fmean(values)}
    writes = [v for name in WRITE_ACTIONS for v in recorder.samples.get(name, [])]
    rows["WRITES"] = {"n": len(writes), "p50": percentile(writes, 0.5), "p95": percentile(writes, 0.95),
                      "max": max(writes) if writes else 0.0, "mean": statistics.fmean(writes) if writes else 0.0}
    return rows


_PATCHED = ("EXCEL_PATH", "_is_github_configured", "_github_api_url", "_github_request", "safe_secret")


def _prepare_storage(mode: str, github: FakeGithub, workdir: str):
    from core import picking
    from core import storage as storage_module

    logging.getLogger().setLevel(logging.WARNING)       # sin una línea INFO por cada push simulado
    logging.getLogger("streamlit").setLevel(logging.ERROR)
    picking.reset()
    storage_module.EXCEL_PATH = str(Path(workdir) / "bench_db.xlsx")   # SIEMPRE una base temporal
    storage_module._cached_dfs = None
    storage_module._reset_sync_state()
    storage_module._is_github_configured = lambda: True
    storage_module._github_api_url = lambda: "https://bench.invalid/contents/bench_db.xlsx"
    storage_module._github_request = github
    settings = {"SYNC_MODE": mode, "SYNC_DEFER_SECONDS": 0.5}
    storage_module.safe_secret = lambda key, default=None: settings.get(key, default)
    storage_module._set_remote_sha(github.sha)
    return storage_module.LabStorage()


def _seed(storage, operators: int, supervisors: int):
    from core import auth

    hashed = auth.hash_password("ClaveBanco123")
    people = {
        "client": storage.create_user("Cliente Banco", "cliente@bench.invalid", hashed, "estudiante"),
        "operators": [storage.create_user(f"Operario {i}", f"op{i}@bench.invalid", hashed, "operario")
                      for i in range(operators)],
        "supervisors": [storage.create_user(f"Supervisor {i}", f"sup{i}@bench.invalid", hashed, "encargado")
                        for i in range(supervisors)],
    }
    for number in range(8):
        storage.save_item({"name": f"Producto {number}", "item_type": "standalone", "quantity": 500},
                          f"BENCH-{number:02d}", is_new=True)
    return people


def run_mode(mode: str, args) -> dict:
    from core import storage as storage_module

    originals = {name: getattr(storage_module, name) for name in _PATCHED}
    try:
        return _run_mode(mode, args)
    finally:
        storage_module._stop_flusher()
        storage_module._cached_dfs = None
        for name, value in originals.items():     # el banco no deja parches puestos
            setattr(storage_module, name, value)


def _run_mode(mode: str, args) -> dict:
    from core import dispatch, service_requests

    github = FakeGithub(args.latency_min, args.latency_max, args.seed)
    recorder = Recorder()
    with tempfile.TemporaryDirectory(prefix="bench-despacho-") as workdir:
        storage = _prepare_storage(mode, github, workdir)
        people = _seed(storage, args.operators, args.supervisors)
        client = people["client"]
        done = threading.Event()
        counters = {"submitted": 0, "confirmed": 0}
        counter_lock = threading.Lock()
        delivered = set()

        def client_loop():
            rng = random.Random(args.seed)
            for number in range(args.orders):
                item = f"BENCH-{number % 8:02d}"
                try:
                    recorder.timed("submit", service_requests.submit_request, storage, client,
                                   "product", item, rng.randint(1, 2))
                    with counter_lock:
                        counters["submitted"] += 1
                except Exception as exc:  # noqa: BLE001 - el banco informa, no se detiene
                    recorder.error(f"submit: {exc}")
                time.sleep(args.arrival)
            while not done.is_set():
                for request in storage.get_service_requests(status="approved", user_id=client["id"]):
                    if request["id"] in delivered:
                        continue
                    events = storage.get_trace_events(request_id=request["id"])
                    from core import traceability
                    if traceability.request_stage_key(request, events) == traceability.STAGE_DELIVERED:
                        ok, _, _ = recorder.timed("confirm", dispatch.confirm_client_receipt, storage,
                                                  request["id"], client)
                        if ok:
                            delivered.add(request["id"])
                            with counter_lock:
                                counters["confirmed"] += 1
                if counters["confirmed"] >= args.orders:
                    done.set()
                time.sleep(args.refresh / 5)

        def supervisor_loop(supervisor):
            from core import traceability
            while not done.is_set():
                recorder.timed("read:review_queue", dispatch.review_queue, storage, supervisor)
                for request in recorder.timed("read:pending", storage.get_service_requests, status="pending"):
                    if request.get("request_type") == "product":
                        recorder.timed("approve", service_requests.review_request, storage, request["id"],
                                       "approved", supervisor)
                for request in dispatch.review_queue(storage, supervisor):
                    events = storage.get_trace_events(request_id=request["id"])
                    stage = traceability.request_stage_key(request, events)
                    if stage == traceability.STAGE_PICKED:
                        recorder.timed("receive", dispatch.receive_request, storage, request["id"], supervisor)
                    elif stage == traceability.STAGE_RECEIVED:
                        recorder.timed("deliver", dispatch.deliver_request, storage, request["id"], supervisor)
                time.sleep(args.refresh)

        def operator_loop(operator):
            rng = random.Random(operator["id"])
            while not done.is_set():
                rows = recorder.timed("read:queue_for", dispatch.queue_for, storage, operator)
                recorder.timed("read:my_orders", dispatch.my_orders, storage, operator)
                if not rows:
                    time.sleep(args.refresh)
                    continue
                request = rng.choice(rows)
                ok, _, _ = recorder.timed("claim", dispatch.claim_request, storage, request["id"], operator)
                if not ok:
                    continue            # otro operario lo tomó primero: es lo esperable
                dispatch.start_picking(storage, request["id"], operator)
                for _ in range(int(request.get("quantity") or 1)):
                    dispatch.register_reading(storage, request["id"], operator, request["item_id"], "barcode_gun")
                ok, message, _ = recorder.timed("complete", dispatch.complete_picking, storage,
                                                request["id"], operator)
                if not ok:
                    recorder.error(f"complete: {message}")

        threads = [threading.Thread(target=client_loop, name="client", daemon=True)]
        threads += [threading.Thread(target=supervisor_loop, args=(s,), name=f"sup-{i}", daemon=True)
                    for i, s in enumerate(people["supervisors"])]
        threads += [threading.Thread(target=operator_loop, args=(o,), name=f"op-{i}", daemon=True)
                    for i, o in enumerate(people["operators"])]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        done.wait(timeout=args.timeout)
        done.set()
        for thread in threads:
            thread.join(timeout=10)
        wall = time.perf_counter() - started
        flush_started = time.perf_counter()
        flush = getattr(sys.modules["core.storage"], "flush_now", None)
        if flush:
            flush()
        flush_seconds = time.perf_counter() - flush_started
        return {"mode": mode, "wall": wall, "flush": flush_seconds, "confirmed": counters["confirmed"],
                "orders": args.orders, "puts": github.puts, "calls": github.calls,
                "stats": summarize(recorder), "errors": recorder.errors[:10]}


def print_report(result: dict) -> None:
    print(f"\n=== SYNC_MODE={result['mode']} · {result['confirmed']}/{result['orders']} pedidos completos "
          f"en {result['wall']:.1f} s · {result['puts']} PUT a GitHub ({result['calls']} llamadas) "
          f"· vaciado final {result['flush']:.1f} s ===")
    print(f"{'acción':<22}{'n':>5}{'p50':>9}{'p95':>9}{'máx':>9}")
    for name, row in result["stats"].items():
        print(f"{name:<22}{row['n']:>5}{row['p50']:>9.2f}{row['p95']:>9.2f}{row['max']:>9.2f}")
    for error in result["errors"]:
        print(f"  ! {error}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("sync", "deferred", "both"), default="both")
    parser.add_argument("--operators", type=int, default=8)
    parser.add_argument("--supervisors", type=int, default=4)
    parser.add_argument("--orders", type=int, default=16)
    parser.add_argument("--latency-min", type=float, default=1.0, help="segundos por llamada a GitHub")
    parser.add_argument("--latency-max", type=float, default=2.0)
    parser.add_argument("--arrival", type=float, default=0.5, help="segundos entre pedidos nuevos")
    parser.add_argument("--refresh", type=float, default=5.0, help="segundos entre lecturas de las listas")
    parser.add_argument("--threshold", type=float, default=2.0, help="p95 máximo aceptado (s) de las escrituras")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    modes = ("sync", "deferred") if args.mode == "both" else (args.mode,)
    results = []
    for mode in modes:
        result = run_mode(mode, args)
        print_report(result)
        results.append(result)
    judged = results[0]
    p95 = judged["stats"]["WRITES"]["p95"]
    verdict = "CUMPLE" if p95 <= args.threshold else "NO CUMPLE"
    print(f"\np95 de escrituras en SYNC_MODE={judged['mode']}: {p95:.2f} s (umbral {args.threshold:.1f} s) → {verdict}")
    if len(results) == 2:
        print(f"p95 de escrituras en SYNC_MODE={results[1]['mode']}: "
              f"{results[1]['stats']['WRITES']['p95']:.2f} s")
    return 0 if p95 <= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
