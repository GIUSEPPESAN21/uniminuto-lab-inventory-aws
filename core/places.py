# -*- coding: utf-8 -*-
"""
core/places.py - Ubicaciones fisicas con codigo propio: estanterias, pisos,
mesas de trabajo y zonas (item_type "location").

Una ubicacion es un item mas de la hoja `items` (no cambia el esquema): su
codigo es un codigo de ubicacion (ver core/barcode.py), no tiene stock (cantidad
0: no se presta ni se solicita) y su `parent_id` apunta a la ubicacion que la
contiene (el piso a su estanteria). Los contenedores y productos NO se enlazan a
la ubicacion por `parent_id`: quedan dentro de ella por su codigo GLIOPS
(2-1-01-00-000 esta en la Estanteria 2, Piso 1) o, si tienen codigo libre, por
la ubicacion escrita ("Estanteria 2 · Piso 1").

Funciones puras (sin Streamlit ni base de datos): planes de alta en bloque
("Estanteria 2 con 4 pisos" en una sola escritura), arbol de ubicaciones y lo
que guarda cada una.
"""

import re
from collections import defaultdict

from core import barcode
from core.data_quality import normalize_text, parse_location_text

LOCATION_TYPE = "location"
SHELF, FLOOR, TABLE, ZONE = (
    barcode.LOCATION_SHELF, barcode.LOCATION_FLOOR, barcode.LOCATION_TABLE, barcode.LOCATION_ZONE,
)
KIND_NAMES = dict(barcode.LOCATION_KIND_NAMES)
KIND_ICONS = {
    SHELF: ":material/shelves:",
    FLOOR: ":material/layers:",
    TABLE: ":material/table_restaurant:",
    ZONE: ":material/map:",
}
KIND_ORDER = (SHELF, FLOOR, TABLE, ZONE)
HISTORY_DETAILS = "Ubicacion creada desde Inventario > Ubicaciones."
LEGO_ZONE_NAME = "Exhibición Lego"

STATUS_NEW = "Nueva"
STATUS_EXISTS = "Ya registrada"


# ---------------------------------------------------------------------------
# Identificacion
# ---------------------------------------------------------------------------

def is_location(item: dict) -> bool:
    return (item or {}).get("item_type") == LOCATION_TYPE


def is_loanable(item: dict) -> bool:
    """Productos que se prestan o solicitan: ni contenedores ni ubicaciones."""
    return (item or {}).get("item_type") not in ("master", LOCATION_TYPE)


def kind_of(item_or_code) -> str:
    """Tipo de ubicacion (estanteria, piso, mesa o zona) de un item o codigo."""
    if isinstance(item_or_code, dict):
        return barcode.location_kind(str(item_or_code.get("id") or "").strip(), is_location(item_or_code))
    return barcode.location_kind(str(item_or_code or "").strip())


def kind_name(item_or_code) -> str:
    return KIND_NAMES.get(kind_of(item_or_code), "")


def default_name(code: str) -> str:
    """Nombre sugerido para un codigo de ubicacion estructurado ("" si es libre)."""
    place = barcode.location_code(code)
    if not place:
        return ""
    if place.get("lego"):
        return LEGO_ZONE_NAME
    return barcode.describe_location(place)


def code_order(item_or_code) -> tuple:
    """Orden natural por codigo (2-1 antes que 2-10)."""
    code = item_or_code.get("id") if isinstance(item_or_code, dict) else item_or_code
    parts = re.split(r"(\d+)", str(code or ""))
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts if p)


def _tree_order(item: dict) -> tuple:
    kind = kind_of(item)
    return (KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER), code_order(item))


def structural_parent(code: str) -> str:
    """Ubicacion que contiene a `code` por su propio codigo: el piso esta en su
    estanteria y la exhibicion Lego en la Estanteria 3. "" si no aplica."""
    place = barcode.location_code(code)
    if not place:
        return ""
    if place["kind"] == FLOOR:
        return barcode.build_shelf_code(place["estanteria"])
    if place.get("lego"):
        return barcode.build_shelf_code(3)
    return ""


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _active_index(items: list) -> dict:
    return {str(i.get("id") or "").strip(): i for i in items or [] if i.get("status") != "retired"}


# ---------------------------------------------------------------------------
# Planes de alta (se guardan con storage.save_items_bulk: una sola escritura)
# ---------------------------------------------------------------------------

def _payload(code: str, name: str, parent_id: str = "", description: str = "", category: str = "",
             location: str = "") -> dict:
    return {
        "id": code, "name": name, "category": category, "description": description,
        "item_type": LOCATION_TYPE, "parent_id": parent_id, "unit": "unidad", "quantity": 0,
        "location": location, "min_stock_alert": 0, "status": "active",
    }


def _row(code: str, name: str, parent_id: str, status: str) -> dict:
    return {"code": code, "name": name, "kind": kind_name(code) or KIND_NAMES[ZONE],
            "parent_id": parent_id, "status": status}


def _plan(rows: list, payloads: list, errors: list) -> dict:
    return {"rows": rows, "payloads": [] if errors else payloads, "errors": errors}


def plan_shelf(estanteria, floors, items: list, name: str = "", description: str = "",
               category: str = "") -> dict:
    """Plan para registrar la Estanteria `estanteria` y sus pisos 1..`floors`.

    Lo que ya esta registrado se muestra como tal y no se vuelve a crear (sirve
    para agregar despues los pisos que falten). Los pisos quedan dentro de la
    estanteria. Devuelve {"rows", "payloads", "errors"}; `payloads` va vacio si
    hay errores."""
    rows, payloads, errors = [], [], []
    try:
        shelf_code = barcode.build_shelf_code(estanteria)
        floors = int(floors)
    except (TypeError, ValueError) as exc:
        return _plan(rows, payloads, [str(exc)])
    if not 0 <= floors <= barcode.PISO_MAX:
        return _plan(rows, payloads, [f"El número de pisos debe estar entre 0 y {barcode.PISO_MAX}."])

    active = _active_index(items)
    shelf_number = int(estanteria)
    shelf_name = _clean(name) or f"Estantería {shelf_number}"

    def add(code, item_name, parent_id, extra_description=""):
        existing = active.get(code)
        if existing is not None and not is_location(existing):
            errors.append(f"El código {code} ya pertenece a «{existing.get('name')}», que no es una ubicación.")
            return
        if existing is not None:
            rows.append(_row(code, existing.get("name") or item_name, existing.get("parent_id") or "",
                             STATUS_EXISTS))
            return
        rows.append(_row(code, item_name, parent_id, STATUS_NEW))
        payloads.append(_payload(code, item_name, parent_id, extra_description, _clean(category)))

    add(shelf_code, shelf_name, "", (description or "").strip())
    for floor in range(1, floors + 1):
        add(barcode.build_floor_code(shelf_number, floor), f"Estantería {shelf_number} · Piso {floor}", shelf_code)
    return _plan(rows, payloads, errors)


def plan_location(code: str, name: str, items: list, parent_id: str = "", description: str = "",
                  category: str = "") -> dict:
    """Plan para registrar UNA ubicacion (mesa, exhibicion Lego, zona, sala o
    subnivel; tambien una estanteria o un piso sueltos).

    Valida el codigo como codigo de ubicacion, exige nombre para los codigos
    libres y comprueba que la ubicacion que la contiene exista y sea compatible.
    Sin `parent_id`, un piso o la exhibicion Lego quedan dentro de su
    estanteria si ya esta registrada."""
    code, name, parent_id = (code or "").strip(), _clean(name), (parent_id or "").strip()
    rows, errors = [], []
    if not code:
        return _plan(rows, [], ["Escribe el código de la ubicación."])
    try:
        barcode.validate_location_code(code)
    except ValueError as exc:
        return _plan(rows, [], [str(exc)])

    active = _active_index(items)
    name = name or default_name(code)
    existing = active.get(code)
    if existing is not None:
        if not is_location(existing):
            return _plan(rows, [], [f"El código {code} ya pertenece a «{existing.get('name')}», "
                                    "que no es una ubicación."])
        rows.append(_row(code, existing.get("name") or name, existing.get("parent_id") or "", STATUS_EXISTS))
        return _plan(rows, [], errors)
    if not name:
        errors.append("Escribe el nombre de la ubicación (ej. «Sala de electrónica»).")

    if parent_id:
        parent = active.get(parent_id)
        if parent is None or not is_location(parent):
            errors.append(f"La ubicación {parent_id} no está registrada.")
        elif parent_id == code:
            errors.append("Una ubicación no puede estar dentro de sí misma.")
        else:
            problem = barcode.location_parent_problem(code, parent_id)
            if problem:
                errors.append(problem)
    else:
        candidate = structural_parent(code)
        if candidate and is_location(active.get(candidate)):
            parent_id = candidate

    # El codigo estructurado ya dice donde esta; una zona libre hereda el nombre de la que la contiene.
    location_text = ""
    if not barcode.is_location_code(code) and parent_id and active.get(parent_id):
        location_text = _clean(active[parent_id].get("name"))
    rows.append(_row(code, name, parent_id, STATUS_NEW))
    payload = _payload(code, name, parent_id, (description or "").strip(), _clean(category), location_text)
    return _plan(rows, [payload], errors)


# ---------------------------------------------------------------------------
# Arbol de ubicaciones
# ---------------------------------------------------------------------------

def effective_parent(item: dict, locations_by_id: dict) -> str:
    """La ubicacion que contiene a `item`: su `parent_id` si es una ubicacion
    activa; si no, la que indica su propio codigo (un piso creado antes que su
    estanteria igual se muestra dentro de ella)."""
    parent_id = str(item.get("parent_id") or "").strip()
    if parent_id and parent_id in locations_by_id and parent_id != item.get("id"):
        return parent_id
    candidate = structural_parent(str(item.get("id") or ""))
    return candidate if candidate in locations_by_id else ""


def _locations(items: list) -> dict:
    return {i["id"]: i for i in items or [] if is_location(i) and i.get("status") != "retired"}


def _parent_map(locations: dict) -> dict:
    parents = {code: effective_parent(item, locations) for code, item in locations.items()}
    # Un ciclo (datos editados a mano) no debe colgar la interfaz: se corta.
    for code in parents:
        seen, current = {code}, parents[code]
        while current:
            if current in seen:
                parents[code] = ""
                break
            seen.add(current)
            current = parents.get(current, "")
    return parents


def location_tree(items: list) -> list:
    """Ubicaciones activas como arbol: [{"item", "depth", "children": [...]}].
    Primero las estanterias (con sus pisos), luego mesas y zonas."""
    locations = _locations(items)
    parents = _parent_map(locations)
    children = defaultdict(list)
    roots = []
    for item in sorted(locations.values(), key=_tree_order):
        parent = parents[item["id"]]
        (children[parent] if parent else roots).append(item)

    def build(item, depth):
        return {"item": item, "depth": depth, "children": [build(c, depth + 1) for c in children[item["id"]]]}

    return [build(root, 0) for root in roots]


def flatten(nodes: list) -> list:
    """Nodos del arbol en orden de lectura (padre antes que sus hijos)."""
    flat = []
    for node in nodes or []:
        flat.append(node)
        flat.extend(flatten(node["children"]))
    return flat


def descendants(code: str, items: list) -> list:
    """Ubicaciones activas dentro de `code`, a cualquier profundidad."""
    locations = _locations(items)
    parents = _parent_map(locations)
    found, pending = [], [code]
    while pending:
        current = pending.pop(0)
        for child_code in sorted((c for c, p in parents.items() if p == current), key=code_order):
            if child_code != code and locations[child_code] not in found:
                found.append(locations[child_code])
                pending.append(child_code)
    return found


def count_by_kind(items: list) -> dict:
    counts = {kind: 0 for kind in KIND_ORDER}
    for item in _locations(items).values():
        counts[kind_of(item) or ZONE] += 1
    return counts


# ---------------------------------------------------------------------------
# Lo que guarda una ubicacion
# ---------------------------------------------------------------------------

def _code_verdict(code: str, place: dict):
    """True/False si el codigo GLIOPS del item lo ubica dentro/fuera de la
    ubicacion estructurada `place`; None si el codigo no dice nada (libre)."""
    try:
        parsed = barcode.parse_code(code)
    except ValueError:
        return None
    fmt, kind = parsed["format"], place["kind"]
    if fmt == barcode.FORMAT_STANDARD:
        if kind == SHELF:
            return parsed["estanteria"] == place["estanteria"]
        if kind == FLOOR:
            return (parsed["estanteria"], parsed["piso"]) == (place["estanteria"], place["piso"])
        return False
    if fmt == barcode.FORMAT_MESA:
        return kind == TABLE and parsed["mesa"] == place["mesa"]
    if fmt == barcode.FORMAT_LEGO:
        return bool(place.get("lego")) or (kind == SHELF and place["estanteria"] == 3)
    return None


def _text_within(text: str, base: str) -> bool:
    """El texto es `base` o empieza por `base` seguido de un separador."""
    text, base = normalize_text(text).rstrip(" .;,"), normalize_text(base).rstrip(" .;,")
    return bool(base) and (text == base or (text.startswith(base) and text[len(base)] in ";,·-/ "))


def _text_verdict(location_text: str, place_code: str, place: dict, place_name: str) -> bool:
    """¿La ubicacion escrita de un item con codigo libre esta en este lugar?"""
    text = str(location_text or "")
    if not text.strip():
        return False
    if place:
        fields = parse_location_text(text)["fields"]
        if place["kind"] == SHELF:
            return fields.get("estanteria") == place["estanteria"]
        if place["kind"] == FLOOR:
            return (fields.get("estanteria"), fields.get("piso")) == (place["estanteria"], place["piso"])
        if place["kind"] == TABLE:
            return fields.get("mesa") == place["mesa"]
        return "lego" in normalize_text(text) and fields.get("estanteria") in (None, 3)
    token = re.compile(rf"(?<![A-Za-z0-9]){re.escape(place_code)}(?![A-Za-z0-9])", re.IGNORECASE)
    return _text_within(text, place_name) or bool(token.search(text))


def _inside(item: dict, by_id: dict, scope: list) -> bool:
    parent = by_id.get(str(item.get("parent_id") or "").strip())
    for place_code, place, place_name in scope:
        if place:
            verdict = _code_verdict(item["id"], place)
            if verdict is None and parent is not None and not is_location(parent):
                verdict = _code_verdict(parent["id"], place)
            if verdict is not None:
                if verdict:
                    return True
                continue
        text = item.get("location") or (parent or {}).get("location") or ""
        if _text_verdict(text, place_code, place, place_name):
            return True
    return False


def contents_of(code: str, items: list) -> dict:
    """Lo que guarda la ubicacion `code` (registrada o no) y las ubicaciones que
    contiene: {"containers", "products", "loose", "sublocations"}.

    `containers` son los Contenedores Principales; `products`, todos los
    productos de adentro (tambien los que estan en esos contenedores); `loose`,
    los productos que no estan en ninguno de esos contenedores; `sublocations`,
    las ubicaciones directamente dentro de esta."""
    code = (code or "").strip()
    active = [i for i in items or [] if i.get("status") != "retired"]
    by_id = {i["id"]: i for i in active}
    locations = _locations(active)
    parents = _parent_map(locations)
    own = locations.get(code)
    scope = [(code, barcode.location_code(code), (own or {}).get("name") or "")]
    for inner in descendants(code, active):
        scope.append((inner["id"], barcode.location_code(inner["id"]), inner.get("name") or ""))

    inside = sorted((i for i in active if not is_location(i) and _inside(i, by_id, scope)), key=code_order)
    containers = [i for i in inside if i.get("item_type") == "master"]
    container_ids = {c["id"] for c in containers}
    products = [i for i in inside if i.get("item_type") != "master"]
    loose = [p for p in products if p.get("parent_id") not in container_ids]
    sublocations = sorted((locations[c] for c, p in parents.items() if p == code), key=_tree_order)
    return {"containers": containers, "products": products, "loose": loose, "sublocations": sublocations}


def children_by_container(products: list) -> dict:
    grouped = defaultdict(list)
    for product in products or []:
        grouped[product.get("parent_id") or ""].append(product)
    return grouped
