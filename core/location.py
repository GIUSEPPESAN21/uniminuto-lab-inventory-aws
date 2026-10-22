# -*- coding: utf-8 -*-
"""Guías de ubicación móviles derivadas de los códigos GLIOPS.

Este módulo no dibuja un plano: los documentos no incluyen coordenadas ni
puntos de entrada. Produce pasos deterministas y comprobables; para códigos
libres usa la ubicación textual registrada en el ítem o su contenedor padre.
"""

from core import barcode


def _add(steps: list, text: str) -> None:
    text = (text or "").strip()
    if text and text not in steps:
        steps.append(text)


def build_location_guide(item: dict, parent: dict = None) -> dict:
    """Devuelve título, resumen y pasos para llegar a un ítem.

    Nunca lanza por un código heredado: si no puede interpretarlo, degrada a
    `location` y finalmente a una instrucción de consulta al responsable.
    """
    item = item or {}
    parent = parent or {}
    code = str(item.get("id") or "").strip()
    name = str(item.get("name") or "Producto").strip()
    location_text = str(item.get("location") or parent.get("location") or "").strip()
    steps = []
    parsed = None

    navigable_formats = (barcode.FORMAT_STANDARD, barcode.FORMAT_MESA, barcode.FORMAT_LEGO)
    try:
        candidate = barcode.parse_code(code)
        parsed = candidate if candidate.get("format") in navigable_formats else None
    except ValueError:
        parsed = None

    # Un ítem puede tener código libre y heredar la ruta estructurada de su contenedor.
    if not parsed:
        parent_code = str(parent.get("id") or "").strip()
        if parent_code:
            try:
                candidate = barcode.parse_code(parent_code)
                parsed = candidate if candidate.get("format") in navigable_formats else None
            except ValueError:
                parsed = None

    if parsed and parsed.get("format") == barcode.FORMAT_STANDARD:
        _add(steps, "Dirígete al área de estanterías del laboratorio.")
        _add(steps, f"Ubica la Estantería {parsed['estanteria']}.")
        _add(steps, f"Ve al Piso {parsed['piso']} de esa estantería.")
        _add(steps, f"Busca el Contenedor {parsed['contenedor']:02d}.")
        if parsed.get("caja", 0) > 0:
            _add(steps, f"Dentro del contenedor, abre la Caja {parsed['caja']:02d}.")
        if parsed.get("item", 0) > 0:
            _add(steps, f"Localiza el Ítem {parsed['item']:03d} y confirma su etiqueta.")
    elif parsed and parsed.get("format") == barcode.FORMAT_MESA:
        _add(steps, f"Dirígete a la Mesa de trabajo {parsed['mesa']}.")
        _add(steps, f"Busca el Equipo {parsed['equipo']} y confirma su etiqueta.")
    elif parsed and parsed.get("format") == barcode.FORMAT_LEGO:
        _add(steps, "Dirígete a la Estantería 3.")
        _add(steps, "Ubica la zona de modelos Lego en exhibición.")
        _add(steps, f"Busca el Modelo {parsed['modelo']} y confirma su etiqueta.")

    place = None if parsed else barcode.location_code(code)
    if place:
        if place["kind"] in (barcode.LOCATION_SHELF, barcode.LOCATION_FLOOR):
            _add(steps, "Dirígete al área de estanterías del laboratorio.")
            _add(steps, f"Ubica la Estantería {place['estanteria']}.")
        if place["kind"] == barcode.LOCATION_FLOOR:
            _add(steps, f"Ve al Piso {place['piso']} de esa estantería.")
        if place["kind"] == barcode.LOCATION_TABLE:
            _add(steps, f"Dirígete a la Mesa de trabajo {place['mesa']}.")
        if place.get("lego"):
            _add(steps, "Dirígete a la Estantería 3 y ubica la zona de modelos Lego en exhibición.")
        parsed = place

    if location_text:
        _add(steps, f"Referencia física registrada: {location_text}.")

    if not steps:
        _add(steps, "Consulta al responsable del laboratorio: este producto aún no tiene una ruta estructurada.")

    _add(steps, f"Verifica que el nombre sea “{name}” y el código sea “{code or 'sin código'}”.")
    return {
        "title": f"Cómo llegar a {name}",
        "code": code,
        "location": location_text,
        "structured": bool(parsed),
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Ruta verificable: puntos de control con etiqueta escaneable
# ---------------------------------------------------------------------------
# Cada nivel físico con etiqueta (contenedor 2-1-01-00-000, caja 2-1-01-01-000,
# producto 2-1-01-01-001, equipo M1-E2, modelo E3-LM07) es un punto de control
# que se confirma escaneando o escribiendo su código. Los niveles sin etiqueta
# (estantería, piso, mesa, zona Lego, referencia textual) se confirman al
# llegar o quedan probados por la etiqueta de un nivel más profundo.

def navigable_code(code: str):
    """Código GLIOPS con ubicación (estándar, mesa o Lego) ya interpretado, o None."""
    try:
        parsed = barcode.parse_code(code)
    except ValueError:
        return None
    if parsed.get("format") in (barcode.FORMAT_STANDARD, barcode.FORMAT_MESA, barcode.FORMAT_LEGO):
        return parsed
    return None


def _checkpoint(key: str, title: str, short: str, hint: str, label_code: str = "", name: str = "") -> dict:
    return {
        "key": key, "title": title, "short": short, "hint": hint,
        "label_code": label_code, "name": name,
    }


def route_anchor(item: dict, parent: dict = None) -> str:
    """Código que fija la ruta: el del ítem si es GLIOPS navegable; si no, el de
    su contenedor padre; si tampoco, el propio código (ruta textual)."""
    item, parent = item or {}, parent or {}
    code = str(item.get("id") or "").strip()
    if navigable_code(code):
        return code
    parent_code = str(parent.get("id") or "").strip()
    if navigable_code(parent_code):
        return parent_code
    return code


def place_codes(item: dict, parent: dict = None) -> list:
    """Códigos de las etiquetas de ubicación que pueden estar en el camino hacia
    `item` (estantería, piso, mesa o zona Lego), para consultar cuáles están
    registradas y pasarlas a `build_route_checkpoints(..., places=...)`."""
    anchor = route_anchor(item, parent)
    parsed = navigable_code(anchor)
    if parsed and parsed["format"] == barcode.FORMAT_STANDARD:
        return [barcode.build_shelf_code(parsed["estanteria"]),
                barcode.build_floor_code(parsed["estanteria"], parsed["piso"])]
    if parsed and parsed["format"] == barcode.FORMAT_MESA:
        return [barcode.build_table_code(parsed["mesa"])]
    if parsed and parsed["format"] == barcode.FORMAT_LEGO:
        return [barcode.build_shelf_code(3), barcode.LEGO_ZONE_CODE]
    place = barcode.location_code(anchor)
    if place and place["kind"] == barcode.LOCATION_FLOOR:
        return [barcode.build_shelf_code(place["estanteria"])]
    return []


def _place_checkpoints(place: dict, anchor: str, names: dict, places: set) -> list:
    """Camino hacia una ubicación (estantería, piso, mesa o zona Lego): el último
    punto es su propia etiqueta."""
    points = []
    if place["kind"] in (barcode.LOCATION_SHELF, barcode.LOCATION_FLOOR):
        e = place["estanteria"]
        shelf_code = barcode.build_shelf_code(e)
        last = place["kind"] == barcode.LOCATION_SHELF
        points.append(_checkpoint(
            "estanteria", f"Estantería {e}", f"E{e}",
            f"Dirígete al área de estanterías y ubica la Estantería {e}." + (
                " Escanea su etiqueta." if last or shelf_code in places else ""),
            anchor if last else (shelf_code if shelf_code in places else ""),
            names.get(shelf_code, "") if last or shelf_code in places else "",
        ))
        if not last:
            points.append(_checkpoint(
                "piso", f"Piso {place['piso']}", f"P{place['piso']}",
                f"Ve al Piso {place['piso']} de la Estantería {e} y escanea su etiqueta.",
                anchor, names.get(anchor, ""),
            ))
    elif place["kind"] == barcode.LOCATION_TABLE:
        points.append(_checkpoint(
            "mesa", f"Mesa de trabajo {place['mesa']}", f"M{place['mesa']}",
            f"Dirígete a la Mesa de trabajo {place['mesa']} y escanea su etiqueta.",
            anchor, names.get(anchor, ""),
        ))
    else:
        points.append(_checkpoint("estanteria", "Estantería 3", "E3", "Dirígete a la Estantería 3."))
        points.append(_checkpoint(
            "zona", "Exhibición Lego", "LEGO",
            "Ubica la zona de modelos Lego en exhibición y escanea su etiqueta.",
            anchor, names.get(anchor, ""),
        ))
    return points


def build_route_checkpoints(item: dict, parent: dict = None, names: dict = None, places=None) -> list:
    """Puntos de control ordenados para llegar a `item`.

    `names` (opcional) mapea códigos de etiqueta a nombres registrados para
    mostrarlos junto al punto (p. ej. el nombre del contenedor). `places`
    (opcional) son los códigos de ubicación REGISTRADOS (ver `place_codes`): la
    estantería, el piso, la mesa o la zona Lego que tienen etiqueta se confirman
    escaneándola; los que no, al llegar. El último punto es SIEMPRE la etiqueta
    del propio producto. Nunca lanza.
    """
    item, parent, names = item or {}, parent or {}, names or {}
    places = set(places or ())

    def tagged(code: str) -> str:
        return code if code in places else ""

    def named(code: str) -> str:
        return names.get(code, "") if code in places else ""
    code = str(item.get("id") or "").strip()
    name = str(item.get("name") or "Producto").strip()
    location_text = str(item.get("location") or parent.get("location") or "").strip()
    anchor = route_anchor(item, parent)
    parsed = navigable_code(anchor)
    points = []

    if parsed and parsed["format"] == barcode.FORMAT_STANDARD:
        e, p, c = parsed["estanteria"], parsed["piso"], parsed["contenedor"]
        caja, num = parsed["caja"], parsed["item"]
        shelf_code, floor_code = barcode.build_shelf_code(e), barcode.build_floor_code(e, p)
        points.append(_checkpoint(
            "estanteria", f"Estantería {e}", f"E{e}",
            f"Dirígete al área de estanterías y ubica la Estantería {e}."
            + (" Escanea su etiqueta." if tagged(shelf_code) else ""),
            tagged(shelf_code), named(shelf_code),
        ))
        points.append(_checkpoint(
            "piso", f"Piso {p}", f"P{p}",
            f"Ve al Piso {p} de la Estantería {e}." + (" Escanea su etiqueta." if tagged(floor_code) else ""),
            tagged(floor_code), named(floor_code),
        ))
        container_code = anchor if caja == 0 else f"{e}-{p}-{c:02d}-00-000"
        points.append(_checkpoint(
            "contenedor", f"Contenedor {c:02d}", f"C{c:02d}",
            f"Busca el Contenedor {c:02d} en ese piso y escanea la etiqueta de su frente.",
            container_code, names.get(container_code, ""),
        ))
        if caja:
            box_code = anchor if num == 0 else f"{e}-{p}-{c:02d}-{caja:02d}-000"
            points.append(_checkpoint(
                "caja", f"Caja {caja:02d}", f"CJ{caja:02d}",
                f"Dentro del Contenedor {c:02d}, abre la Caja {caja:02d} y escanea su etiqueta.",
                box_code, names.get(box_code, ""),
            ))
        if num:
            points.append(_checkpoint(
                "item", f"Ítem {num:03d}", f"I{num:03d}",
                f"En la Caja {caja:02d}, localiza el Ítem {num:03d} y escanea su etiqueta.",
                anchor, names.get(anchor, ""),
            ))
    elif parsed and parsed["format"] == barcode.FORMAT_MESA:
        table_code = barcode.build_table_code(parsed["mesa"])
        points.append(_checkpoint(
            "mesa", f"Mesa de trabajo {parsed['mesa']}", f"M{parsed['mesa']}",
            f"Dirígete a la Mesa de trabajo {parsed['mesa']}."
            + (" Escanea su etiqueta." if tagged(table_code) else ""),
            tagged(table_code), named(table_code),
        ))
        points.append(_checkpoint(
            "equipo", f"Equipo {parsed['equipo']}", f"EQ{parsed['equipo']}",
            f"En la mesa, busca el Equipo {parsed['equipo']} y escanea su etiqueta.",
            anchor, names.get(anchor, ""),
        ))
    elif parsed and parsed["format"] == barcode.FORMAT_LEGO:
        shelf3, zone = barcode.build_shelf_code(3), barcode.LEGO_ZONE_CODE
        points.append(_checkpoint(
            "estanteria", "Estantería 3", "E3",
            "Dirígete a la Estantería 3." + (" Escanea su etiqueta." if tagged(shelf3) else ""),
            tagged(shelf3), named(shelf3),
        ))
        points.append(_checkpoint(
            "zona", "Exhibición Lego", "LEGO",
            "Ubica la zona de modelos Lego en exhibición." + (" Escanea su etiqueta." if tagged(zone) else ""),
            tagged(zone), named(zone),
        ))
        points.append(_checkpoint(
            "modelo", f"Modelo {parsed['modelo']:02d}", f"LM{parsed['modelo']:02d}",
            f"Busca el Modelo {parsed['modelo']:02d} y escanea su etiqueta.",
            anchor, names.get(anchor, ""),
        ))
    elif barcode.location_code(anchor):
        # El propio item es una ubicación (estantería, piso, mesa o zona Lego).
        points.extend(_place_checkpoints(barcode.location_code(anchor), anchor, names, places))
    elif location_text:
        points.append(_checkpoint(
            "referencia", location_text, "REF", f"Ve a la ubicación registrada: {location_text}.",
        ))
    else:
        points.append(_checkpoint(
            "referencia", "Responsable del laboratorio", "AYUDA",
            "Este producto no tiene ruta estructurada: pregunta al responsable dónde está.",
        ))

    if code and points[-1]["label_code"] != code:
        points.append(_checkpoint(
            "producto", name, "PROD", f"Busca «{name}» y escanea su etiqueta ({code}).", code, name,
        ))
    elif code:
        points[-1]["name"] = points[-1]["name"] or name
    return points


def compact_route(checkpoints: list) -> str:
    """Ruta corta con el mismo vocabulario de la etiqueta impresa (E2 › P1 › C01…)."""
    return " › ".join(point["short"] for point in checkpoints or [] if point.get("short"))


def codes_match(expected: str, scanned: str) -> bool:
    """¿El código leído corresponde a la etiqueta esperada?

    Tolera lo que pasa al escribir a mano: espacios alrededor, mayúsculas y ceros
    de relleno omitidos en los códigos GLIOPS (2-1-1-1-1 = 2-1-01-01-001)."""
    expected, scanned = (expected or "").strip(), (scanned or "").strip()
    if not expected or not scanned:
        return False
    if expected == scanned or expected.casefold() == scanned.casefold():
        return True
    first, second = navigable_code(expected), navigable_code(scanned.upper())
    if first and second and first == second:
        return True
    first, second = barcode.location_code(expected), barcode.location_code(scanned.upper())
    return bool(first and second and first == second)


def _describe_place(parsed: dict) -> str:
    if parsed["format"] == barcode.FORMAT_STANDARD:
        return f"la Estantería {parsed['estanteria']}, Piso {parsed['piso']}"
    if parsed["format"] == barcode.FORMAT_MESA:
        return f"la Mesa de trabajo {parsed['mesa']}"
    return "la exhibición Lego de la Estantería 3"


def _standard_hint(target: dict, scanned: dict) -> str:
    """Compara dos códigos estándar nivel por nivel y orienta hacia el destino."""
    e, p, c = target["estanteria"], target["piso"], target["contenedor"]
    if scanned["estanteria"] != e:
        return (f"Esa etiqueta es de la Estantería {scanned['estanteria']}; tu producto está en la "
                f"Estantería {e}, Piso {p}.")
    if scanned["piso"] != p:
        gap = abs(scanned["piso"] - p)
        return (f"Vas bien: es la Estantería {e}, pero estás en el Piso {scanned['piso']}. "
                f"El tuyo es el Piso {p} ({gap} piso{'s' if gap > 1 else ''} de distancia).")
    if scanned["contenedor"] != c:
        return (f"Estás en el Contenedor {scanned['contenedor']:02d}; el tuyo es el {c:02d}, "
                "en el mismo piso.")
    if target["caja"] == 0:
        return f"Ese código está dentro de tu contenedor; para esta ruta basta la etiqueta del Contenedor {c:02d}."
    if scanned["caja"] == 0:
        return f"Ese es tu Contenedor {c:02d}. Ahora abre la Caja {target['caja']:02d}."
    if scanned["caja"] != target["caja"]:
        return (f"Estás en el contenedor correcto, pero en la Caja {scanned['caja']:02d}; "
                f"abre la Caja {target['caja']:02d}.")
    if target["item"] == 0:
        return f"Ese código está dentro de tu Caja {target['caja']:02d}; para esta ruta basta la etiqueta de la caja."
    if scanned["item"] == 0:
        return f"Esa es tu Caja {target['caja']:02d}. Ahora busca el Ítem {target['item']:03d}."
    return (f"Estás en la caja correcta, pero ese es el Ítem {scanned['item']:03d}; "
            f"busca el Ítem {target['item']:03d}.")


def _inside(target: dict, scanned: dict) -> bool:
    """¿`scanned` (estándar) está dentro del lugar que marca `target` (estándar)?"""
    same_container = all(scanned[k] == target[k] for k in ("estanteria", "piso", "contenedor"))
    if not same_container:
        return False
    return target["caja"] == 0 or scanned["caja"] == target["caja"]


def _place_hint(target: dict, place: dict) -> str:
    """Orienta a quien escaneó la etiqueta de una ubicación (estantería, piso,
    mesa o zona Lego) que no es la del punto actual de su ruta."""
    here = barcode.location_phrase(place)
    if target["format"] == barcode.FORMAT_STANDARD:
        e, p = target["estanteria"], target["piso"]
        if place["kind"] == barcode.LOCATION_SHELF and place["estanteria"] == e:
            return f"Estás en la Estantería {e}, la correcta. Ahora ve al Piso {p}."
        if place["kind"] == barcode.LOCATION_FLOOR and place["estanteria"] == e:
            gap = abs(place["piso"] - p)
            return (f"Vas bien: es la Estantería {e}, pero estás en el Piso {place['piso']}. "
                    f"El tuyo es el Piso {p} ({gap} piso{'s' if gap > 1 else ''} de distancia).")
        return f"Esa etiqueta es de {here}; tu producto está en la Estantería {e}, Piso {p}."
    if target["format"] == barcode.FORMAT_MESA:
        if place["kind"] == barcode.LOCATION_TABLE:
            return (f"Estás en la Mesa {place['mesa']}; tu equipo está en la "
                    f"Mesa de trabajo {target['mesa']}.")
        return f"Esa etiqueta es de {here}; tu equipo está en la Mesa de trabajo {target['mesa']}."
    return f"Esa etiqueta es de {here}; tu producto está en la exhibición Lego de la Estantería 3."


def explain_mismatch(target_code: str, scanned_code: str, known_item: dict = None,
                     target_location: str = "", product_code: str = "") -> str:
    """Explica dónde está quien escaneó un código equivocado y hacia dónde ir.

    `target_code` es el código que fija la ruta (ver `route_anchor`);
    `product_code`, la etiqueta final cuando el producto tiene un código libre
    guardado dentro de un contenedor GLIOPS; `known_item` es el producto
    registrado con el código leído, si existe."""
    scanned_code = (scanned_code or "").strip()
    target = navigable_code(target_code)
    scanned = navigable_code(scanned_code) or navigable_code(scanned_code.upper())
    prefix = ""
    if known_item and known_item.get("name"):
        prefix = f"Escaneaste «{known_item['name']}» ({scanned_code}). "

    scanned_place = barcode.location_code(scanned_code.upper())
    if target and scanned_place and not scanned:
        return prefix + _place_hint(target, scanned_place)

    free_product = bool(product_code) and product_code != target_code
    if (free_product and target and scanned and target["format"] == barcode.FORMAT_STANDARD
            and scanned["format"] == barcode.FORMAT_STANDARD and _inside(target, scanned)):
        return f"{prefix}Estás en el lugar correcto; ahora busca la etiqueta «{product_code}» de tu producto."

    if not target:
        where = f" ({target_location})" if target_location else ""
        return (f"{prefix}Ese no es tu producto. Busca la etiqueta «{target_code}»{where}; "
                "si no la encuentras, pregunta al responsable.").strip()
    if not scanned:
        if prefix:
            return f"{prefix}No es tu producto: el tuyo está en {_describe_place(target)}."
        return (f"No reconozco «{scanned_code}» como una etiqueta del laboratorio. Revisa que "
                f"escaneaste la etiqueta correcta: tu producto está en {_describe_place(target)}.")

    if target["format"] == barcode.FORMAT_STANDARD and scanned["format"] == barcode.FORMAT_STANDARD:
        return prefix + _standard_hint(target, scanned)
    if target["format"] == barcode.FORMAT_MESA and scanned["format"] == barcode.FORMAT_MESA:
        if scanned["mesa"] != target["mesa"]:
            return (f"{prefix}Estás en la Mesa {scanned['mesa']}; tu equipo está en la "
                    f"Mesa de trabajo {target['mesa']}.")
        return (f"{prefix}Estás en la mesa correcta, pero ese es el Equipo {scanned['equipo']}; "
                f"busca el Equipo {target['equipo']}.")
    if target["format"] == barcode.FORMAT_LEGO and scanned["format"] == barcode.FORMAT_LEGO:
        return (f"{prefix}Ese es el Modelo {scanned['modelo']:02d}; el tuyo es el "
                f"Modelo {target['modelo']:02d}, en la misma exhibición.")
    return f"{prefix}Esa etiqueta es de {_describe_place(scanned)}; tu producto está en {_describe_place(target)}."