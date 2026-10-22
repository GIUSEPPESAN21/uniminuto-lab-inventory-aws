#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reinicia una base de SIMULACIÓN de la lúdica: borra pedidos, préstamos, eventos,
reservas y correos, y deja personas, productos y ajustes como estaban.

    python scripts/reset_ludica.py --db simulacion.xlsx --yes

Se niega a correr sobre la base real: exige que el archivo lleve la marca
``ludica_sandbox`` (la escribe seed_ludica.py), rechaza el nombre de la base real y
no corre si hay GITHUB_TOKEN/GITHUB_REPO configurados.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    from core import dispatch, ludica, picking
    from core import storage as storage_module

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="archivo Excel de la simulación")
    parser.add_argument("--yes", action="store_true", help="confirma el reinicio")
    args = parser.parse_args(argv)

    try:
        path = ludica.assert_sandbox(args.db, creating=False)
    except ludica.RealDatabaseError as exc:
        print(f"RECHAZADO: {exc}", file=sys.stderr)
        return 2
    if not args.yes:
        print("RECHAZADO: falta --yes para confirmar el reinicio.", file=sys.stderr)
        return 2

    storage_module.EXCEL_PATH = path
    storage_module._cached_dfs = None
    storage_module._reset_sync_state()
    storage_module._is_github_configured = lambda: False
    try:
        removed = storage_module.LabStorage().reset_simulation()
    except PermissionError as exc:
        print(f"RECHAZADO: {exc}", file=sys.stderr)
        return 2
    picking.reset()
    dispatch.reset_orders_cache()
    print(f"Simulación reiniciada en {path}: " + ", ".join(f"{sheet}={count}" for sheet, count in removed.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
