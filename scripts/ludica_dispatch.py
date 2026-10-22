# -*- coding: utf-8 -*-
"""Genera pedidos reproducibles para comparar escenarios A/B/C de la lúdica.

No abre la base de producción. Recibe una lista ya anonimizada de productos y
produce un manifiesto que las pruebas o el facilitador pueden exportar.
"""

import argparse
import csv
import io
import json
import random

SCENARIOS = {
    "A": "Manual",
    "B": "Programa + pistola de barras",
    "C": "Programa + chip NFC",
}
DEFAULT_CYCLE_MINUTES = 10


def generate_orders(items: list, count: int, seed: int = 0) -> list:
    candidates = [
        item for item in items
        if item.get("status", "active") != "retired"
        and item.get("item_type") not in {"master", "location"}
        and int(item.get("available", item.get("quantity", 0)) or 0) > 0
    ]
    if not candidates:
        return []
    rng = random.Random(seed)
    rows = []
    for number in range(1, max(0, int(count)) + 1):
        item = rng.choice(candidates)
        maximum = min(3, int(item.get("available", item.get("quantity", 0)) or 0))
        rows.append({
            "order": number, "item_id": item.get("id", ""),
            "item_name": item.get("name", ""), "quantity": rng.randint(1, maximum),
        })
    return rows


def scenario_manifest(items: list, count: int, seed: int = 0,
                      cycle_minutes: int = DEFAULT_CYCLE_MINUTES) -> dict:
    orders = generate_orders(items, count, seed)
    return {
        "seed": seed, "cycle_minutes": int(cycle_minutes),
        "scenarios": [
            {"id": key, "label": label, "orders": [dict(row) for row in orders]}
            for key, label in SCENARIOS.items()
        ],
    }


def manifest_csv(manifest: dict) -> bytes:
    output = io.StringIO(newline="")
    fields = ["scenario", "scenario_label", "cycle_minutes", "order", "item_id",
              "item_name", "quantity"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for scenario in manifest.get("scenarios", []):
        for order in scenario.get("orders", []):
            writer.writerow({
                "scenario": scenario.get("id"), "scenario_label": scenario.get("label"),
                "cycle_minutes": manifest.get("cycle_minutes"), **order,
            })
    return output.getvalue().encode("utf-8-sig")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON con una lista anonimizada de productos")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--minutes", type=int, default=DEFAULT_CYCLE_MINUTES)
    args = parser.parse_args(argv)
    with open(args.input, encoding="utf-8") as source:
        items = json.load(source)
    print(json.dumps(scenario_manifest(items, args.count, args.seed, args.minutes),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
