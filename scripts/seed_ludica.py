#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Siembra una base de SIMULACIÓN para la lúdica (personas y productos ficticios).

    python scripts/seed_ludica.py --db simulacion.xlsx --scenario B --orders 10

Se niega a correr sobre la base real: no acepta el nombre de la base real, exige
que NO haya GITHUB_TOKEN/GITHUB_REPO configurados (no publica nada), y solo
sobrescribe un archivo que ya sea una simulación (--force). Todo lo que crea
lleva el dominio .invalid y la marca ``ludica_sandbox``.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    from core import ludica
    from core import storage as storage_module

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="archivo Excel de la simulación (se crea)")
    parser.add_argument("--scenario", default="B", choices=("A", "B", "C"))
    parser.add_argument("--clients", type=int, default=8)
    parser.add_argument("--operators", type=int, default=8)
    parser.add_argument("--supervisors", type=int, default=4)
    parser.add_argument("--orders", type=int, default=10, help="pedidos pendientes a crear")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--session-id", default="", help="id de la sesión para el CSV de eventos")
    parser.add_argument("--password", default="", help="clave de las cuentas ficticias (por omisión, una aleatoria)")
    parser.add_argument("--force", action="store_true", help="reemplaza una simulación existente")
    args = parser.parse_args(argv)

    try:
        path = ludica.assert_sandbox(args.db, creating=True)
    except ludica.RealDatabaseError as exc:
        print(f"RECHAZADO: {exc}", file=sys.stderr)
        return 2
    if os.path.exists(path):
        if not args.force:
            print("RECHAZADO: la simulación ya existe; usa reset_ludica.py o --force.", file=sys.stderr)
            return 2
        os.remove(path)

    storage_module.EXCEL_PATH = path                      # SIEMPRE el archivo de simulación
    storage_module._cached_dfs = None
    storage_module._reset_sync_state()
    storage_module._is_github_configured = lambda: False  # nada se publica
    summary = ludica.seed_simulation(
        storage_module.LabStorage(), scenario=args.scenario, clients=args.clients, operators=args.operators,
        supervisors=args.supervisors, orders=args.orders, seed=args.seed,
        password=args.password or None, session=args.session_id or None,
    )
    password = summary.pop("clave")
    print(f"Simulación creada en {path}: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
    print(f"Clave de las cuentas ficticias (…@ludica.invalid): {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
