#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mide cuánto tardan las lecturas y las escrituras con una base grande (sintética, temporal, sin GitHub).

    python scripts/bench_storage.py                 # 2000 productos, 6000 préstamos, 4000 pedidos, 32000 eventos
    python scripts/bench_storage.py --items 500

No toca la base real ni usa credenciales: crea un libro temporal y lo borra al terminar."""

import argparse
import os
import random
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_REPO", None)

import pandas as pd  # noqa: E402

from core import barcode, dispatch, search, traceability  # noqa: E402
from core import storage as sm  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--items", type=int, default=2000)
    args = parser.parse_args(argv)
    os.environ["N_ITEMS"] = str(args.items)
    tmp = tempfile.mkdtemp(prefix="bench_storage_")
    sm.EXCEL_PATH = os.path.join(tmp, "bench.xlsx")
    sm._cached_dfs = None
    sm._reset_sync_state()
    sm._is_github_configured = lambda: False
    st = sm.LabStorage()
    rng = random.Random(1)
    dfs = sm._read_excel()
    def frame(sheet, rows): return pd.DataFrame(rows, columns=sm.SHEET_COLUMNS[sheet]).astype(object).fillna("")
    now=sm._now_str()
    N_ITEMS=int(os.environ.get("N_ITEMS",2000))
    items=[]
    for i in range(N_ITEMS):
        items.append({"id":f"LAB-{i:05d}","name":f"Producto {i} {rng.choice(['resistencia','sensor','cable','motor','placa'])}","category":rng.choice(["Electrónica","Óptica","Mecánica"]),"description":"desc "*5,"item_type":"standalone","parent_id":"","unit":"u","quantity":rng.randint(5,200),"location":"Estante 1","min_stock_alert":2,"status":"active","created_by":"x","updated_at":now})
    dfs["items"]=frame("items",items)
    users=[]
    for i in range(300):
        users.append({"id":uuid.uuid4().hex[:20],"full_name":f"Persona {i}","student_id":str(i),"institutional_email":f"p{i}@uniminuto.edu.co","password_hash":"x","role":"estudiante" if i>10 else "maestro","program_or_department":"Ing","status":"active","created_at":now,"must_change_password":"","password_changed_at":"","session_epoch":""})
    dfs["users"]=frame("users",users)
    loans=[]
    for i in range(6000):
        u=rng.choice(users); it=rng.choice(items)
        loans.append({"id":uuid.uuid4().hex[:20],"item_id":it["id"],"item_name":it["name"],"parent_id":"","quantity":rng.randint(1,3),"user_id":u["id"],"user_name":u["full_name"],"user_role":"estudiante","checkout_at":now,"expected_return_at":"","return_at":"" if i<500 else now,"status":"out" if i<500 else "returned","notes":"","request_id":"","operator_id":"","operator_name":""})
    dfs["loans"]=frame("loans",loans)
    reqs=[];events=[]
    for i in range(4000):
        u=rng.choice(users); it=rng.choice(items); rid=uuid.uuid4().hex[:20]
        reqs.append({"id":rid,"request_type":"product","item_id":it["id"],"item_name":it["name"],"quantity":1,"service_name":"","description":"","needed_at":"","requester_id":u["id"],"requester_name":u["full_name"],"requester_email":u["institutional_email"],"status":rng.choice(["approved","pending","rejected"]),"reviewed_by":"","reviewed_at":"","review_notes":"","email_notified":"","email_error":"","created_at":now,"updated_at":now})
        for s,k in enumerate(["order_requested","order_approved","order_authorized","order_claimed","order_picked","order_received","order_delivered","order_client_confirmed"]):
            events.append({"id":uuid.uuid4().hex[:20],"event_type":k,"request_id":rid,"seq":s+1,"item_id":it["id"],"loan_id":"","user_id":u["id"],"owner_id":u["id"],"actor_id":u["id"],"actor_name":u["full_name"],"actor_email":"","actor_role":"estudiante","receipt":"","details":"{}","created_at":now})
    dfs["service_requests"]=frame("service_requests",reqs); dfs["trace_events"]=frame("trace_events",events)
    hist=[{"id":uuid.uuid4().hex[:20],"item_id":rng.choice(items)["id"],"timestamp":now,"type":"Salida","quantity_change":-1,"actor_user_id":"x","details":"d"} for _ in range(20000)]
    dfs["item_history"]=frame("item_history",hist)

    sm._write_excel(dfs)
    sm._cached_dfs = None
    print(f"archivo: {os.path.getsize(sm.EXCEL_PATH) / 1e6:.1f} MB · {len(events)} eventos · {len(loans)} préstamos")
    st = sm.LabStorage()
    admin = [u for u in st.get_all_users() if u["role"] == "maestro"][0]
    client = [u for u in st.get_all_users() if u["role"] == "estudiante"][0]

    def timed(label, fn, n=1):
        fn()
        started = time.perf_counter()
        for _ in range(n):
            fn()
        print(f"{label:42s} {(time.perf_counter() - started) / n * 1000:9.1f} ms")

    timed("get_all_items", st.get_all_items)
    timed("100 × get_available_quantity", lambda: [st.get_available_quantity(f"LAB-{i:05d}") for i in range(100)])
    timed("get_all_loans", st.get_all_loans)
    timed("get_service_requests", st.get_service_requests)
    timed("get_service_requests(user_id)", lambda: st.get_service_requests(user_id=client["id"]), 5)
    timed("buscar «resistencia»", lambda: search.search_items(st.get_all_items(), "resistencia"))
    timed("escanear un código", lambda: barcode.scan(st, "LAB-00100"), 10)
    timed("dispatch_metrics (todos los pedidos)", lambda: dispatch.dispatch_metrics(st, admin))
    timed("visible_requests (administrador)", lambda: traceability.visible_requests(st, admin))

    def write():
        st.save_item({"name": "Nuevo", "item_type": "standalone", "quantity": 1}, f"NEW-{uuid.uuid4().hex[:6]}", is_new=True)

    timed("guardar un producto (escritura completa)", write, 3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
