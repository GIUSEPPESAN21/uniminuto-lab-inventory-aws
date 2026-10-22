# -*- coding: utf-8 -*-
"""Búsqueda única de la app: un solo cuadro para códigos escaneados, nombres y pedidos.

No necesita que la persona sepa qué escribe: un código exacto abre el producto o la
ubicación, un texto busca por nombre, categoría, descripción o ubicación (sin importar
tildes ni mayúsculas) y ``#a1b2c3`` abre un pedido."""

import re
import unicodedata

MAX_RESULTS = 12
_HEX_ID = re.compile(r"^#?[0-9a-f]{4,20}$")


def normalize(text) -> str:
    """Minúsculas y sin tildes: «Resistencia» y «resistencía» coinciden."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char)).strip()


def looks_like_request_id(query: str) -> bool:
    """``#a1b2c3`` o el id completo (hexadecimal): se busca también entre los pedidos."""
    return bool(_HEX_ID.match(normalize(query)))


def _score(item: dict, tokens: list, needle: str) -> int:
    code = normalize(item.get("id"))
    name = normalize(item.get("name"))
    if code == needle:
        return 1000
    haystack = " ".join(normalize(item.get(key)) for key in ("name", "category", "description", "location"))
    if not all(token in f"{code} {haystack}" for token in tokens):
        return 0
    score = 100
    if code.startswith(needle):
        score += 400
    if name == needle:
        score += 300
    elif name.startswith(needle):
        score += 200
    elif needle in name:
        score += 120
    score += sum(40 for token in tokens if token in name)
    return score


def search_items(items: list, query: str, limit: int = MAX_RESULTS, kinds=None) -> list:
    """Ítems que coinciden con ``query`` (todas las palabras), los mejores primero.

    ``kinds``: tipos permitidos (``None`` = todos). Los dados de baja nunca aparecen."""
    needle = normalize(query)
    if not needle:
        return []
    tokens = needle.split()
    scored = []
    for item in items or []:
        if item.get("status") == "retired":
            continue
        if kinds is not None and item.get("item_type") not in kinds:
            continue
        score = _score(item, tokens, needle)
        if score:
            scored.append((score, normalize(item.get("name")), item))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:limit]]
