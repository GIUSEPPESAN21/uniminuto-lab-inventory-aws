# -*- coding: utf-8 -*-
"""core/nfc.py - Chips NFC para la trazabilidad y el conteo de inventario.

Cada chip (NTAG213/215/216) guarda UN registro NDEF de tipo URI que apunta a la
app con el codigo del producto o de la ubicacion:

    https://<app>/escanear?nfc=2-1-01-01-001&s=K7Q2MX9AB0

Al acercar el telefono (Android o iPhone, sin instalar nada) el sistema abre esa
URL en el navegador. La app guarda el toque (prueba de que la persona estuvo
frente al producto), muestra el producto y, si quien toca tiene una ruta
verificable o hay un conteo de inventario abierto, lo aplica alli (ver
`handle_tap` y views/nfc_tap.py).

- `s` es una firma corta (HMAC-SHA256 con el secreto NFC_SECRET) para que una
  URL no se pueda inventar escribiendo un codigo a mano. Sin NFC_SECRET las URL
  van sin firma y se aceptan igual (el codigo de barras impreso ya es publico).
- El registro de chips (cuales se grabaron y cuales ya se probaron) se deriva de
  eventos de la hoja `trace_events`: grabado, retirado y cada toque. No hay
  hoja nueva en la base.

Logica pura (sin Streamlit): se prueba con FakeStorage.
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from core import barcode, inventory_count, location, traceability
from core.config import safe_secret
from core.service_requests import STATUS_APPROVED, TYPE_PRODUCT

QUERY_CODE = "nfc"          # parametro de la URL con el codigo
QUERY_SIGNATURE = "s"       # parametro de la URL con la firma
TAG_PAGE = "escanear"       # pagina a la que apunta la URL (si algo falla, se cae en Escanear)
MAX_CODE_LENGTH = 64
SIGNATURE_LENGTH = 10       # 10 caracteres Crockford = 50 bits
DEDUP_SECONDS = 60          # el mismo chip tocado dos veces seguidas cuenta una sola vez
_SIGNATURE_VERSION = "lab-nfc-v1"
_ALPHABET = traceability.RECEIPT_ALPHABET  # sin I, L, O ni U

# Resultado de comprobar la firma de un toque.
SIG_UNSIGNED = "unsigned"   # no hay NFC_SECRET: se acepta sin firma
SIG_VALID = "valid"
SIG_MISSING = "missing"     # hay NFC_SECRET y la URL no trae firma
SIG_INVALID = "invalid"     # la firma no corresponde al codigo
SIGNATURE_MESSAGES = {
    SIG_MISSING: "Este chip se grabó sin firma y la app exige firma (NFC_SECRET): el toque no se registró. "
                 "Vuelve a grabarlo con la URL de Chips NFC → Grabar etiquetas.",
    SIG_INVALID: "La firma de este chip no es válida (la URL se alteró o se grabó con otro NFC_SECRET): el "
                 "toque no se registró. Vuelve a grabarlo con la URL de Chips NFC → Grabar etiquetas.",
}

# Capacidad NDEF util (bytes) de los chips NTAG mas comunes.
CHIP_CAPACITY = {"NTAG213": 137, "NTAG215": 496, "NTAG216": 868}
RECOMMENDED_CHIP = "NTAG215"
# Abreviaturas de prefijo del registro URI (NFC Forum): ocupan 1 byte.
_URI_PREFIXES = ("https://www.", "http://www.", "https://", "http://")

# A donde lleva un toque despues de procesarlo (claves de las paginas en app.py).
TARGET_SCAN = "escanear"
TARGET_ROUTE = "trazabilidad"
TARGET_COUNT = "nfc"

TAG_WRITTEN = "written"     # grabado (todavia nadie lo ha tocado)
TAG_TESTED = "tested"       # ya se toco al menos una vez: funciona
TAG_STATUS_LABELS = {TAG_WRITTEN: "Grabado (sin probar)", TAG_TESTED: "Probado"}
NO_TAG_LABEL = "Sin chip"


# ---------------------------------------------------------------------------
# Configuracion (Secrets)
# ---------------------------------------------------------------------------

def nfc_secret() -> str:
    """Secreto para firmar las URL de los chips (vacio = sin firma)."""
    return str(safe_secret("NFC_SECRET", "") or "").strip()


def _clean_base(value) -> str:
    value = str(value or "").strip().rstrip("/")
    parts = urlsplit(value)
    return value if parts.scheme in ("http", "https") and parts.netloc else ""


def configured_app_url() -> str:
    """APP_URL de los Secrets (direccion publica de la app) o "" si no es valida."""
    return _clean_base(safe_secret("APP_URL", ""))


def origin_of(url) -> str:
    """scheme://host de una URL (la que ve el navegador, st.context.url)."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def app_base_url(context_url: str = None) -> str:
    """Direccion base de las URL de los chips: APP_URL si esta configurada; si no,
    el origen de la direccion con la que se abrio la app."""
    return configured_app_url() or origin_of(context_url)


# ---------------------------------------------------------------------------
# Firma, URL y lectura del toque
# ---------------------------------------------------------------------------

def sign_code(code: str, secret: str) -> str:
    """Firma corta y determinista de `code` ("" sin secreto)."""
    if not secret:
        return ""
    payload = f"{_SIGNATURE_VERSION}|{code}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    number = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(SIGNATURE_LENGTH):
        number, remainder = divmod(number, len(_ALPHABET))
        chars.append(_ALPHABET[remainder])
    return "".join(chars)


def check_signature(code: str, signature: str, secret: str) -> str:
    """SIG_UNSIGNED, SIG_VALID, SIG_MISSING o SIG_INVALID."""
    if not secret:
        return SIG_UNSIGNED
    signature = str(signature or "").strip().upper()
    if not signature:
        return SIG_MISSING
    # En bytes: compare_digest con texto no ASCII lanza TypeError (una firma escrita a mano no debe tumbar el toque).
    expected = sign_code(code, secret)
    return SIG_VALID if hmac.compare_digest(signature.encode("utf-8"), expected.encode("utf-8")) else SIG_INVALID


def signature_accepted(status: str) -> bool:
    return status in (SIG_UNSIGNED, SIG_VALID)


def validate_tag_code(code) -> str:
    """Codigo listo para grabar: sin espacios alrededor y con un formato valido
    de core/barcode.py (lanza ValueError con el motivo)."""
    code = str(code or "").strip()
    if not code:
        raise ValueError("El código está vacío.")
    if barcode.location_code(code):
        return code        # estantería, piso, mesa o zona: sus códigos (2-1-00-00-000…) no son de producto, pero llevan chip
    barcode.validate_code_format(code)
    return code


def build_tag_url(code, base_url: str, secret: str = "") -> str:
    """URL que se graba en el chip: <base>/escanear?nfc=<codigo>[&s=<firma>]."""
    code = validate_tag_code(code)
    base = _clean_base(base_url)
    if not base:
        raise ValueError("Falta la dirección pública de la app: configura APP_URL en los Secrets.")
    url = f"{base}/{TAG_PAGE}?{QUERY_CODE}={quote(code, safe='')}"
    signature = sign_code(code, secret)
    return f"{url}&{QUERY_SIGNATURE}={signature}" if signature else url


def ndef_size(url: str) -> int:
    """Bytes que ocupa la URL como mensaje NDEF (un registro URI corto)."""
    raw = str(url or "")
    rest = len(raw.encode("utf-8"))
    for prefix in _URI_PREFIXES:
        if raw.startswith(prefix):
            rest -= len(prefix)
            break
    payload = 1 + rest  # 1 byte de prefijo abreviado
    header = 4 if payload < 256 else 7  # banderas + largo del tipo + largo del contenido + tipo "U"
    return header + payload


def chips_that_fit(url: str) -> list:
    size = ndef_size(url)
    return [chip for chip, capacity in CHIP_CAPACITY.items() if size <= capacity]


def _first(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else ""
    return "" if value is None else str(value)


def parse_tap(params):
    """Toque NFC desde los parametros de la URL: {"code", "signature", "error"};
    None si la URL no trae ?nfc=."""
    if not params or QUERY_CODE not in params:
        return None
    code = _first(params.get(QUERY_CODE)).strip()
    signature = _first(params.get(QUERY_SIGNATURE)).strip()[:32]
    error = ""
    if not code:
        error = "El chip NFC no trae ningún código."
    elif len(code) > MAX_CODE_LENGTH or not code.isprintable() or re.search(r"\s", code):
        error = "El chip NFC trae un código no válido."
        code = ""
    return {"code": code, "signature": signature, "error": error}


# ---------------------------------------------------------------------------
# Toques (eventos nfc_tap)
# ---------------------------------------------------------------------------

def _now(now=None) -> datetime:
    return traceability.to_datetime(now) or datetime.now(timezone.utc)


def _actor_fields(actor: dict) -> dict:
    actor = actor or {}
    return {
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""),
        "actor_role": actor.get("role", ""),
    }


def tap_code(event: dict) -> str:
    return str((event or {}).get("item_id") or traceability.event_details(event).get("code") or "")


# Dónde van los chips: solo estanterías, pisos y contenedores. Al tocarlos la app reconoce que la persona llegó.
ARRIVAL_SHELF, ARRIVAL_FLOOR, ARRIVAL_CONTAINER = "estantería", "piso", "contenedor"
_ARRIVAL_ARTICLE = {ARRIVAL_SHELF: "a la", ARRIVAL_FLOOR: "al", ARRIVAL_CONTAINER: "al"}


def arrival_kind(item: dict) -> str:
    """«estantería», «piso» o «contenedor» si ``item`` es uno de los lugares donde van chips NFC; si no, ''."""
    item = item or {}
    if item.get("item_type") == "master":
        return ARRIVAL_CONTAINER
    if item.get("item_type") == "location":
        place = barcode.location_code(str(item.get("id") or ""))
        if place and place.get("kind") == barcode.LOCATION_SHELF:
            return ARRIVAL_SHELF
        if place and place.get("kind") == barcode.LOCATION_FLOOR:
            return ARRIVAL_FLOOR
    return ""


def arrival_message(storage, item: dict) -> str:
    """«Llegaste al contenedor «X» (código) · 5 producto(s).» o '' si el chip no es de un lugar."""
    kind = arrival_kind(item)
    if not kind:
        return ""
    text = f"Llegaste {_ARRIVAL_ARTICLE[kind]} {kind} «{item.get('name') or item.get('id')}» ({item.get('id')})"
    if kind == ARRIVAL_CONTAINER:
        try:
            count = len(storage.get_children(item["id"]))
        except Exception:
            count = 0
        text += f" · {count} producto(s) dentro" if count else " · todavía sin productos"
    return text


def record_tap(storage, user: dict, code: str, item: dict = None, signed: bool = False,
               count_session: str = "", now=None):
    """Guarda UN evento `nfc_tap` y devuelve (evento, repetido).

    Si la misma persona toco el mismo chip hace menos de DEDUP_SECONDS (en el
    mismo contexto), no se escribe otra vez: un doble toque o una recarga no
    cuentan doble."""
    now = _now(now)
    previous = storage.get_trace_events(user_id=(user or {}).get("id", ""), item_id=code,
                                        event_type=traceability.EVENT_NFC_TAP)
    if previous:
        last = previous[-1]
        when = traceability.to_datetime(last.get("created_at"))
        same_context = traceability.event_details(last).get("count_session", "") == (count_session or "")
        if when and same_context and 0 <= (now - when).total_seconds() < DEDUP_SECONDS:
            return last, True
    details = {"code": code, "item_name": (item or {}).get("name", ""), "signed": bool(signed)}
    if arrival_kind(item):
        details["arrival"] = arrival_kind(item)
    if count_session:
        details["count_session"] = count_session
    event = storage.add_trace_event({
        "event_type": traceability.EVENT_NFC_TAP, "request_id": "", "item_id": code, "loan_id": "",
        "user_id": (user or {}).get("id", ""), **_actor_fields(user), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": now.isoformat(),
    })
    return event, False


def user_taps(storage, user_id: str, since=None) -> list:
    """Toques de una persona (del mas antiguo al mas reciente), opcionalmente desde `since`."""
    if not user_id:
        return []
    rows = storage.get_trace_events(user_id=user_id, event_type=traceability.EVENT_NFC_TAP)
    since = traceability.to_datetime(since)
    if since is None:
        return rows
    return [row for row in rows if (traceability.to_datetime(row.get("created_at")) or since) >= since]


def recent_taps(storage, limit: int = 100) -> list:
    """Ultimos toques de todo el laboratorio (el mas reciente primero)."""
    rows = storage.get_trace_events(event_type=traceability.EVENT_NFC_TAP)
    return list(reversed(rows))[:limit]


# ---------------------------------------------------------------------------
# Ruta verificable: un toque confirma el punto de control de su etiqueta
# ---------------------------------------------------------------------------

def _route_parts(storage, request: dict):
    item = storage.get_item(request.get("item_id")) or {"id": request.get("item_id"), "name": request.get("item_name")}
    parent = storage.get_item(item.get("parent_id")) if item.get("parent_id") else None
    return item, parent


def open_route_requests(storage, user: dict) -> list:
    """Solicitudes de producto aprobadas de `user` sin retirar y sin ruta guardada
    (la mas antigua primero): las que todavia se pueden recorrer."""
    if not user or not user.get("id"):
        return []
    rows = [
        row for row in storage.get_service_requests(status=STATUS_APPROVED, user_id=user["id"])
        if row.get("request_type") == TYPE_PRODUCT
    ]
    if not rows:
        return []
    done = {
        event.get("request_id")
        for event in storage.get_trace_events(user_id=user["id"])
        if event.get("event_type") in (traceability.EVENT_PICKED_UP, traceability.EVENT_ROUTE_VERIFIED)
    }
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    rows = [row for row in rows if row.get("id") not in done]
    return sorted(rows, key=lambda row: traceability.to_datetime(row.get("created_at")) or aware_min)


def route_label_codes(storage, request: dict) -> list:
    item, parent = _route_parts(storage, request)
    return [point["label_code"] for point in location.build_route_checkpoints(item, parent) if point.get("label_code")]


def requests_for_code(storage, user: dict, code: str) -> list:
    """Rutas abiertas de `user` en las que `code` es una de las etiquetas del camino."""
    return [
        request for request in open_route_requests(storage, user)
        if any(location.codes_match(label, code) for label in route_label_codes(storage, request))
    ]


def route_mismatch_hint(storage, request: dict, code: str, known_item: dict = None) -> str:
    """Orientacion cuando el chip tocado no es parte de la ruta de `request`."""
    item, parent = _route_parts(storage, request)
    anchor = location.route_anchor(item, parent)
    where = str(item.get("location") or (parent or {}).get("location") or "")
    hint = location.explain_mismatch(anchor, code, known_item, where, str(item.get("id") or ""))
    title = traceability.request_title(request)
    return f"Ese chip no es parte de tu ruta hacia «{title}». {hint}"


def _iso_seconds(value) -> str:
    dt = traceability.to_datetime(value)
    return dt.isoformat(timespec="seconds") if dt else ""


def merge_taps_into_route(route: dict, taps: list):
    """Aplica a `route` (estado de la sesion) los toques NFC que todavia no se
    aplicaron y cuyo codigo es una etiqueta del camino, en orden de hora.

    Es idempotente: recuerda los ids aplicados en route["nfc_taps"], asi que se
    puede llamar en cada recarga (sirve aunque el toque se hizo en otra pestaña).
    Devuelve el resultado (ver traceability.apply_code) del ultimo toque que hizo
    avanzar la ruta, o None si no cambio nada."""
    if not route:
        return None
    seen = set(route.get("nfc_taps") or [])
    labels = [point.get("label_code") for point in route.get("checkpoints") or [] if point.get("label_code")]
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    last = None
    for tap in sorted(taps or [], key=lambda row: traceability.to_datetime(row.get("created_at")) or aware_min):
        tap_id = tap.get("id")
        if not tap_id or tap_id in seen:
            continue
        seen.add(tap_id)
        code = tap_code(tap)
        if not any(location.codes_match(label, code) for label in labels):
            continue
        before = json.dumps(route.get("checkpoints"), sort_keys=True, default=str)
        result = traceability.apply_code(route, code, now=tap.get("created_at"), method=traceability.METHOD_NFC)
        stamp = _iso_seconds(tap.get("created_at"))
        if stamp and route.get("started_at") and stamp < route["started_at"]:
            route["started_at"] = stamp  # el toque fue antes que el primer paso de esta sesion
        if result.get("ok") and json.dumps(route.get("checkpoints"), sort_keys=True, default=str) != before:
            last = result
    route["nfc_taps"] = sorted(seen)
    return last


def merge_user_taps(storage, user: dict, request: dict, route: dict):
    """`merge_taps_into_route` con los toques de `user` desde que se aprobo `request`."""
    if not user or not request or not route:
        return None
    since = request.get("reviewed_at") or request.get("created_at")
    return merge_taps_into_route(route, user_taps(storage, user.get("id"), since))


# ---------------------------------------------------------------------------
# Que hacer con un toque (lo decide la logica; la vista solo navega)
# ---------------------------------------------------------------------------

def handle_tap(storage, user: dict, code: str, signature: str = "", secret: str = None, now=None) -> dict:
    """Procesa el toque de `user` sobre el chip de `code`.

    Devuelve {"ok", "tone", "message", "target", "code", "item", "event",
    "duplicate", "request_id", "count_session", "signature"}. `target` es la
    pagina a mostrar: TARGET_COUNT (profesor/maestro con un conteo abierto),
    TARGET_ROUTE (estudiante con una ruta que pasa por esa etiqueta) o TARGET_SCAN.
    Solo se guarda el toque si la firma es aceptada y el codigo existe."""
    secret = nfc_secret() if secret is None else secret
    code = str(code or "").strip()
    status = check_signature(code, signature, secret)
    outcome = {"ok": False, "tone": "warning", "message": "", "target": TARGET_SCAN, "code": code,
               "item": None, "event": None, "duplicate": False, "request_id": "", "count_session": "",
               "signature": status}
    if not code:
        return {**outcome, "message": "El chip NFC no trae ningún código."}
    if not signature_accepted(status):
        return {**outcome, "message": SIGNATURE_MESSAGES[status]}
    item = storage.get_item(code)
    if not item or item.get("status") == "retired":
        return {**outcome, "message": f"El chip apunta a «{code}», que no está registrado en el inventario."}
    outcome["item"] = item
    signed = status == SIG_VALID
    name = item.get("name") or code
    repeat = " (ya lo habías tocado hace un momento: cuenta una sola vez)"

    manager = traceability.is_manager(user)
    session = inventory_count.open_session(storage) if manager else None
    if session:
        event, duplicate = record_tap(storage, user, code, item, signed, count_session=session["id"], now=now)
        message = f"«{name}» quedó verificado en el conteo «{session['name']}»"
        return {**outcome, "ok": True, "tone": "success", "target": TARGET_COUNT, "event": event,
                "duplicate": duplicate, "count_session": session["id"],
                "message": message + (repeat if duplicate else "") + "."}

    routes = [] if manager else requests_for_code(storage, user, code)
    event, duplicate = record_tap(storage, user, code, item, signed, now=now)
    arrival = arrival_message(storage, item)
    outcome.update(ok=True, event=event, duplicate=duplicate, arrival=arrival_kind(item))
    if routes:
        request = routes[0]
        message = ((arrival + ". " if arrival else "")
                   + f"Toque registrado en tu ruta hacia «{traceability.request_title(request)}»"
                   + (repeat if duplicate else "") + ".")
        return {**outcome, "tone": "success", "target": TARGET_ROUTE, "request_id": request["id"], "message": message}

    message = ((arrival + (repeat if duplicate else "")) if arrival
               else f"Toque registrado: «{name}» ({code})" + (repeat if duplicate else "")) + "."
    pending = [] if manager else open_route_requests(storage, user)
    if pending:
        return {**outcome, "tone": "warning",
                "message": f"{message} {route_mismatch_hint(storage, pending[0], code, item)}"}
    return {**outcome, "tone": "success", "message": message}


# ---------------------------------------------------------------------------
# Registro de chips grabados
# ---------------------------------------------------------------------------

def register_tag(storage, code: str, actor: dict, chip: str = "", now=None):
    """Deja constancia de que se grabo el chip de `code` (devuelve el evento)."""
    code = validate_tag_code(code)
    details = {"chip": chip or "", "signed": bool(nfc_secret())}
    return storage.add_trace_event({
        "event_type": traceability.EVENT_NFC_TAG_WRITTEN, "request_id": "", "item_id": code, "loan_id": "",
        "user_id": (actor or {}).get("id", ""), **_actor_fields(actor), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": _now(now).isoformat(),
    })


def unregister_tag(storage, code: str, actor: dict, reason: str = "", now=None):
    """El chip de `code` se quito, se perdio o se reemplazara."""
    code = str(code or "").strip()
    details = {"reason": " ".join(str(reason or "").split())}
    return storage.add_trace_event({
        "event_type": traceability.EVENT_NFC_TAG_REMOVED, "request_id": "", "item_id": code, "loan_id": "",
        "user_id": (actor or {}).get("id", ""), **_actor_fields(actor), "receipt": "",
        "details": json.dumps(details, ensure_ascii=False), "created_at": _now(now).isoformat(),
    })


def tag_registry(events: list) -> dict:
    """{codigo: estado} a partir de los eventos de chips y toques, en orden de hora.

    Grabado → TAG_WRITTEN; cualquier toque → TAG_TESTED (un toque prueba que el
    chip existe y funciona, aunque nadie lo marcara como grabado); retirado →
    sale del registro hasta que se grabe o se toque otra vez."""
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    registry = {}
    for event in sorted(events or [], key=lambda row: traceability.to_datetime(row.get("created_at")) or aware_min):
        kind = event.get("event_type")
        code = tap_code(event) if kind == traceability.EVENT_NFC_TAP else str(event.get("item_id") or "")
        if not code:
            continue
        if kind == traceability.EVENT_NFC_TAG_REMOVED:
            registry.pop(code, None)
        elif kind == traceability.EVENT_NFC_TAG_WRITTEN:
            entry = registry.setdefault(code, {"taps": 0, "last_tap_at": "", "last_tap_by": ""})
            entry.update(status=TAG_WRITTEN, written_at=event.get("created_at", ""),
                         written_by=event.get("actor_name", ""),
                         chip=traceability.event_details(event).get("chip", ""))
        elif kind == traceability.EVENT_NFC_TAP:
            entry = registry.setdefault(code, {"taps": 0, "written_at": "", "written_by": "", "chip": ""})
            entry.update(status=TAG_TESTED, last_tap_at=event.get("created_at", ""),
                         last_tap_by=event.get("actor_name", ""))
            entry["taps"] = int(entry.get("taps") or 0) + 1
    return registry


def load_registry(storage) -> dict:
    events = []
    for kind in (traceability.EVENT_NFC_TAG_WRITTEN, traceability.EVENT_NFC_TAG_REMOVED, traceability.EVENT_NFC_TAP):
        events.extend(storage.get_trace_events(event_type=kind))
    return tag_registry(events)


def tag_status_label(entry) -> str:
    return TAG_STATUS_LABELS.get((entry or {}).get("status"), NO_TAG_LABEL)
