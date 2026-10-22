# -*- coding: utf-8 -*-
"""Material para la lúdica: CSV de eventos sin datos personales y protección de
la base real frente a los scripts de siembra y reinicio.

El CSV de eventos tiene EXACTAMENTE estas columnas, en este orden:

    sesion_id, timestamp, estacion, tipo_evento, item_codigo, transaccion_id,
    usuario_sim, resultado_sistema

- ``estacion``: el puesto (cliente, operario, supervisor o administrador), nunca
  quién lo ocupa.
- ``transaccion_id`` y ``usuario_sim`` son seudónimos con sal (SHA-256 truncado): se
  pueden cruzar filas de un mismo pedido o de una misma persona dentro del archivo,
  pero no se puede volver al id, al nombre ni al correo. Ningún nombre, correo, ID
  de estudiante, texto libre ni mensaje del sistema (que puede nombrar personas)
  sale en el archivo.
- ``resultado_sistema``: ``ok`` o ``rechazado``.
"""

import csv
import hashlib
import io
import os
from datetime import datetime, timezone

from core import dispatch, excel_safe, permissions, traceability
from core.config import safe_secret

COLUMNS = (
    "sesion_id", "timestamp", "estacion", "tipo_evento", "item_codigo",
    "transaccion_id", "usuario_sim", "resultado_sistema",
)
SETTING_SESSION_ID = "ludica_session_id"
SANDBOX_KEY = "ludica_sandbox"       # marca que solo la siembra escribe en una base de simulación
REAL_DB_NAMES = frozenset({"uniminuto_lab_db.xlsx"})  # nombre por omisión de la base real (en minúsculas)
DEFAULT_SALT = "ludica"

_STATION = {
    permissions.ROLE_STUDENT: "cliente", permissions.ROLE_OPERATOR: "operario",
    permissions.ROLE_ATTENDANT: "supervisor", permissions.ROLE_PROFESSOR: "supervisor",
    permissions.ROLE_MASTER: "administrador",
}
RESULT_OK, RESULT_REJECTED = "ok", "rechazado"

# Eventos que entran al CSV (el resto: inicios de sesión, cambios de rol, conteos…, no).
_LIFECYCLE = traceability.DISPATCH_LIFECYCLE_EVENTS - {traceability.EVENT_ORDER_REQUESTED}
_ATTEMPT_FAILED = traceability.EVENT_ORDER_ACTION_FAILED
_SCAN_FAILED = traceability.EVENT_ORDER_SCAN_FAILED
_NFC_TAP = traceability.EVENT_NFC_TAP


def pseudonym(value, salt: str = DEFAULT_SALT, size: int = 8) -> str:
    """Seudónimo estable (por sal) de un id; vacío si no hay id."""
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(f"{salt}:{text}".encode("utf-8")).hexdigest()[:size]


def station_of(role) -> str:
    return _STATION.get(str(role or ""), "sistema")


def session_id(storage) -> str:
    """Id de la sesión de la lúdica (Ajustes) o ``LUD-<escenario>-<fecha>`` por omisión."""
    try:
        configured = str(storage.get_setting(SETTING_SESSION_ID, "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return configured
    return f"LUD-{dispatch.scenario_key(storage)}-{datetime.now(timezone.utc):%Y%m%d}"


def _stamp(value) -> str:
    moment = traceability.to_datetime(value)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds") if moment else ""


def _row(sid: str, when, role, kind: str, item: str, request_id: str, actor_id: str, result: str,
         salt: str) -> dict:
    return {
        "sesion_id": sid, "timestamp": _stamp(when), "estacion": station_of(role),
        "tipo_evento": kind, "item_codigo": item or "",
        "transaccion_id": pseudonym(request_id, salt), "usuario_sim": pseudonym(actor_id, salt),
        "resultado_sistema": result,
    }


def event_rows(storage, sid: str = None, salt: str = DEFAULT_SALT, since=None) -> list:
    """Filas del CSV ordenadas por hora. ``since``: solo eventos desde ese instante."""
    sid = sid or session_id(storage)
    floor = traceability.to_datetime(since) if since else None
    rows = []
    for request in storage.get_service_requests():
        if request.get("request_type") != "product":
            continue
        rows.append(_row(sid, request.get("created_at"), permissions.ROLE_STUDENT,
                         traceability.EVENT_ORDER_REQUESTED, request.get("item_id"), request.get("id"),
                         request.get("requester_id"), RESULT_OK, salt))
    for event in storage.get_trace_events():
        kind, details = event.get("event_type"), traceability.event_details(event)
        request_id, item = event.get("request_id"), event.get("item_id")
        if kind in _LIFECYCLE or kind == _NFC_TAP:
            rows.append(_row(sid, event.get("created_at"), event.get("actor_role"), kind, item, request_id,
                             event.get("actor_id"), RESULT_OK, salt))
        elif kind == _ATTEMPT_FAILED:
            attempted = str(details.get("attempted_event") or kind)
            rows.append(_row(sid, event.get("created_at"), event.get("actor_role"), attempted, item, request_id,
                             event.get("actor_id"), RESULT_REJECTED, salt))
        elif kind == _SCAN_FAILED:
            scans = details.get("scans") or [{"code": details.get("scanned_code"), "at": event.get("created_at")}]
            for scan in scans:
                rows.append(_row(sid, scan.get("at") or event.get("created_at"), event.get("actor_role"), kind,
                                 scan.get("code") or item, request_id, event.get("actor_id"), RESULT_REJECTED, salt))
    if floor:
        rows = [row for row in rows if (traceability.to_datetime(row["timestamp"]) or floor) >= floor]
    return sorted(rows, key=lambda row: (row["timestamp"], row["tipo_evento"]))


def events_csv(storage, actor: dict, sid: str = None, salt: str = DEFAULT_SALT, since=None) -> bytes:
    """CSV de eventos de la lúdica (UTF-8, sin BOM, columnas exactas)."""
    permissions.require_cap(actor, permissions.CAP_DISPATCH_REPORTS)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in event_rows(storage, sid, salt, since):
        writer.writerow({key: excel_safe.csv_safe(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8")


# --- protección de la base real -----------------------------------------------------------

class RealDatabaseError(PermissionError):
    """Los scripts de la lúdica se niegan a tocar la base real."""


def sandbox_marked(path: str) -> bool:
    """True si el archivo Excel ``path`` lleva la marca de base de simulación."""
    if not path or not os.path.exists(path):
        return False
    try:
        import pandas as pd

        settings = pd.read_excel(path, sheet_name="settings", dtype=str, keep_default_na=False)
    except Exception:
        return False
    rows = settings[settings["key"] == SANDBOX_KEY]
    return not rows.empty and str(rows.iloc[0]["value"]).strip() == "1"


def github_sync_configured() -> bool:
    return bool(str(safe_secret("GITHUB_TOKEN", "") or "").strip()
                and str(safe_secret("GITHUB_REPO", "") or "").strip())


def assert_sandbox(path: str, creating: bool = False) -> str:
    """Valida que ``path`` es una base de SIMULACIÓN; devuelve su ruta absoluta.

    Se niega (``RealDatabaseError``) si: el nombre es el de la base real; hay
    sincronización con GitHub configurada (un reinicio se publicaría); o el
    archivo existe y no lleva la marca de simulación. Con ``creating=True`` se
    admite un archivo que todavía no existe."""
    if not path or not str(path).strip():
        raise RealDatabaseError("Indica la base de simulación con --db.")
    absolute = os.path.abspath(str(path))
    if os.path.basename(absolute).lower() in REAL_DB_NAMES:
        raise RealDatabaseError("Ese es el nombre de la base REAL del laboratorio; usa un archivo de simulación.")
    if github_sync_configured():
        raise RealDatabaseError(
            "Hay GITHUB_TOKEN y GITHUB_REPO configurados: este proceso podría publicar en la base real. "
            "Ejecuta el script sin esos Secrets."
        )
    if os.path.exists(absolute):
        if not sandbox_marked(absolute):
            raise RealDatabaseError("El archivo existe y no está marcado como base de simulación de la lúdica.")
    elif not creating:
        raise RealDatabaseError("La base de simulación no existe; créala primero con seed_ludica.py.")
    return absolute


# --- siembra de una base de simulación ----------------------------------------------------

DEFAULT_ITEMS = (
    ("LUD-001", "Resistencia 220 Ω (simulada)", 200), ("LUD-002", "Resistencia 1 kΩ (simulada)", 200),
    ("LUD-003", "LED rojo 5 mm (simulado)", 150), ("LUD-004", "Protoboard 830 puntos (simulada)", 40),
    ("LUD-005", "Arduino UNO (simulado)", 25), ("LUD-006", "Multímetro (simulado)", 12),
    ("LUD-007", "Cable dupont macho-macho (simulado)", 300), ("LUD-008", "Servomotor SG90 (simulado)", 30),
    ("LUD-009", "Sensor ultrasónico (simulado)", 20), ("LUD-010", "Pinzas de punta (simuladas)", 18),
    ("LUD-011", "Cautín 30 W (simulado)", 10), ("LUD-012", "Cinta de enmascarar (simulada)", 60),
)


def seed_simulation(storage, scenario: str = "B", clients: int = 8, operators: int = 8, supervisors: int = 4,
                    orders: int = 10, seed: int = 0, password: str = None, session: str = None,
                    items=None) -> dict:
    """Siembra una base de SIMULACIÓN: personas ficticias, productos, el escenario y la marca.

    Se niega si la base ya tiene usuarios y no es una simulación (nunca siembra
    sobre datos reales). Devuelve conteos y la clave de las cuentas de simulación."""
    import secrets as _secrets

    from core import auth, service_requests

    marked = str(storage.get_setting(SANDBOX_KEY, "") or "") == "1"
    if storage.count_users() and not marked:
        raise RealDatabaseError("La base tiene usuarios y no es una simulación: no se siembra sobre ella.")
    scenario = str(scenario or "").strip().upper()
    if scenario not in dispatch.SCENARIOS:
        raise ValueError("El escenario debe ser A, B o C.")
    password = password or _secrets.token_urlsafe(9)
    hashed = auth.hash_password(password)
    storage.set_setting(SANDBOX_KEY, "1", "ludica")           # primero la marca: el reinicio la exige
    storage.set_setting(dispatch.SETTING_SCENARIO, scenario, "ludica")
    if session:
        storage.set_setting(SETTING_SESSION_ID, session, "ludica")

    def people(role, prefix, count):
        return [storage.create_user(f"{prefix} {number:02d} (simulación)", f"{prefix.lower()}{number:02d}@ludica.invalid",
                                    hashed, role, program="Simulación", student_id=f"SIM-{prefix[:3].upper()}-{number:02d}")
                for number in range(1, max(0, int(count)) + 1)]

    created = {
        "clientes": people(permissions.ROLE_STUDENT, "Cliente", clients),
        "operarios": people(permissions.ROLE_OPERATOR, "Operario", operators),
        "supervisores": people(permissions.ROLE_ATTENDANT, "Supervisor", supervisors),
        "administradores": people(permissions.ROLE_MASTER, "Administrador", 1),
    }
    catalog = ([dict(row) for row in items] if items
               else [dict(zip(("id", "name", "quantity"), row)) for row in DEFAULT_ITEMS])
    for item in catalog:
        storage.save_item({"name": item["name"], "item_type": "standalone", "quantity": int(item.get("quantity", 10)),
                           "location": item.get("location", "Estante de simulación"), "category": "Simulación"},
                          item["id"], is_new=True, actor_email="ludica",
                          actor=None)   # llamador interno de confianza: siembra de una base de simulación
    submitted = 0
    if created["clientes"] and orders:
        import random

        rng = random.Random(seed)
        for number in range(int(orders)):
            item = rng.choice(catalog)
            client = created["clientes"][number % len(created["clientes"])]
            service_requests.submit_request(storage, client, "product", item["id"], rng.randint(1, 3))
            submitted += 1
    return {"clientes": len(created["clientes"]), "operarios": len(created["operarios"]),
            "supervisores": len(created["supervisores"]), "administradores": 1,
            "productos": len(catalog), "pedidos": submitted, "escenario": scenario, "clave": password}
