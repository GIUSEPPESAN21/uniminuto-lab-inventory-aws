# -*- coding: utf-8 -*-
"""core/pieces.py - Retiro de piezas de un producto, sin inventario por pieza.

Un juego de Lego (o bloques, kits, rompecabezas…) es UN producto del inventario, pero la gente se lleva
*piezas* de él. Marcar el producto con «Se retiran piezas» (columna ``loose_pieces``) permite registrar
cuántas se llevó cada persona sin dar de alta cada pieza: no cambia el stock del producto ni crea
préstamos; el saldo de cada persona se deduce de la cadena de custodia (eventos ``pieces_taken`` y
``pieces_returned``), así que además queda quién tuvo cuántas y cuándo."""

import json
import re
from datetime import datetime, timezone

from core import permissions, traceability

MAX_PIECES = 9999
# Productos que, por su nombre o categoría, casi seguro se usan por piezas: se sugiere activar la opción.
_PIECE_WORDS = re.compile(
    r"\b(lego|legos|bloque|bloques|ladrillo|ladrillos|armable|armables|rompecabezas|tangram|fichas?|"
    r"piezas?|mecano|meccano|construcci[oó]n)\b", re.IGNORECASE)


def looks_like_pieces(item: dict) -> bool:
    text = " ".join(str((item or {}).get(key) or "") for key in ("name", "category"))
    return bool(_PIECE_WORDS.search(text))


def enabled(item: dict) -> bool:
    return str((item or {}).get("loose_pieces") or "").strip().lower() in ("1", "true", "si", "sí", "x")


def _parse(pieces) -> int:
    try:
        number = int(pieces)
    except (TypeError, ValueError):
        raise ValueError("Escribe cuántas piezas, con un número entero.") from None
    if not 1 <= number <= MAX_PIECES:
        raise ValueError(f"Las piezas deben estar entre 1 y {MAX_PIECES}.")
    return number


def _record(storage, item: dict, kind: str, owner: dict, actor: dict, pieces: int, note: str, extra: dict = None):
    details = {"pieces": pieces, "notes": str(note or "").strip(), **(extra or {})}
    return storage.add_trace_event({
        "event_type": kind, "request_id": "", "item_id": item["id"], "loan_id": "",
        "user_id": owner.get("id", ""), "owner_id": owner.get("id", ""),
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
        "receipt": "", "details": json.dumps({**details, "owner_name": owner.get("full_name", "")},
                                             ensure_ascii=False),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def balances(storage, item_id: str) -> list:
    """Quién tiene piezas del producto: ``[{"user_id", "user_name", "pieces", "since"}]``, de más a menos."""
    held = {}
    for event in storage.get_trace_events(item_id=item_id):
        kind = event.get("event_type")
        if kind not in traceability.PIECE_EVENTS:
            continue
        details = traceability.event_details(event)
        owner = event.get("owner_id") or event.get("user_id") or ""
        row = held.setdefault(owner, {"user_id": owner, "user_name": details.get("owner_name") or "", "pieces": 0,
                                      "since": ""})
        if details.get("owner_name"):
            row["user_name"] = details["owner_name"]
        amount = int(details.get("pieces") or 0)
        if kind == traceability.EVENT_PIECES_TAKEN:
            row["pieces"] += amount
            row["since"] = row["since"] or str(event.get("created_at") or "")
        else:
            row["pieces"] = max(0, row["pieces"] - amount)
            if row["pieces"] == 0:
                row["since"] = ""
    return sorted((row for row in held.values() if row["pieces"] > 0), key=lambda row: -row["pieces"])


def balance_of(storage, item_id: str, user_id: str) -> int:
    return next((row["pieces"] for row in balances(storage, item_id) if row["user_id"] == user_id), 0)


def take(storage, item_id: str, pieces, actor: dict, note: str = ""):
    """El usuario retira ``pieces`` del producto. Devuelve ``(ok, mensaje)``."""
    permissions.require_cap(actor, permissions.CAP_CHECKOUT, "No tienes permiso para retirar piezas.")
    item = storage.get_item(item_id)
    if not item or item.get("status") == "retired":
        return False, "El producto no existe."
    if not enabled(item):
        return False, "Este producto no se retira por piezas."
    try:
        number = _parse(pieces)
    except ValueError as exc:
        return False, str(exc)
    _record(storage, item, traceability.EVENT_PIECES_TAKEN, actor, actor, number, note)
    total = balance_of(storage, item_id, actor.get("id", ""))
    return True, f"Retiraste {number} pieza(s) de «{item.get('name')}». En total tienes {total}."


def give_back(storage, item_id: str, pieces, actor: dict, note: str = "", for_user: dict = None):
    """Devuelve piezas. Cada persona devuelve las suyas; quien tiene `checkin_any_loan` registra la
    devolución de otra persona (``for_user``). Nunca se devuelve más de lo que esa persona tiene."""
    permissions.require_cap(actor, permissions.CAP_CHECKOUT, "No tienes permiso para devolver piezas.")
    owner = for_user or actor
    if owner.get("id") != actor.get("id"):
        permissions.require_cap(actor, permissions.CAP_CHECKIN_ANY,
                                "Solo puedes devolver tus propias piezas.")
    item = storage.get_item(item_id)
    if not item:
        return False, "El producto no existe."
    try:
        number = _parse(pieces)
    except ValueError as exc:
        return False, str(exc)
    held = balance_of(storage, item_id, owner.get("id", ""))
    if held <= 0:
        return False, "No tiene piezas pendientes de este producto."
    if number > held:
        return False, f"Solo tiene {held} pieza(s) pendientes."
    extra = {"registered_by": actor.get("full_name", "")} if owner.get("id") != actor.get("id") else None
    _record(storage, item, traceability.EVENT_PIECES_RETURNED, owner, actor, number, note, extra)
    left = held - number
    return True, f"Devueltas {number} pieza(s)." + (f" Quedan {left} por devolver." if left else " No queda nada pendiente.")


def total_out(storage, item_id: str) -> int:
    return sum(row["pieces"] for row in balances(storage, item_id))
