# -*- coding: utf-8 -*-
"""core/storage.py - Capa de base de datos: Amazon DynamoDB (+ el bucket S3 de
core/photos.py para los JPEG). Reemplaza el Excel local + sincronizacion
manual a GitHub por SHA de la version anterior.

Migracion de infraestructura, no de producto: cada metodo publico de
``LabStorage`` conserva el MISMO nombre, firma y contrato de ``views/*`` y del
resto de ``core/*``; lo que cambia es como se guardan los datos. Ver
docs/architecture/aws-migration-mapping.md para la justificacion completa
(por que una tabla por hoja, por que estos GSIs, que deuda tecnica se elimina
y que se gana: escrituras condicionales en vez de SHA/conflicto manual, y una
base lista para correr en mas de una instancia de ECS).

Lo que YA NO EXISTE a proposito (era la deuda tecnica de la version Excel):
- La "sincronizacion" con GitHub, su SHA remoto y el conflicto que resolvia el
  maestro: cada escritura en DynamoDB ya es inmediatamente consistente.
- La cache completa del libro en RAM de un solo proceso.
Los metodos publicos que exponian eso (``get_sync_status``, ``retry_sync``,
``resolve_sync_conflict``...) SIGUEN existiendo -- los llama ``core/ui.py`` y
``app.py`` sin cambios -- pero ahora devuelven un estado "siempre sincronizado"
en vez de simularlo.

Nota de alcance para quien continue esta migracion: ``data_state()``/
``get_events_index()`` siguen cacheando EN MEMORIA DE ESTE PROCESO (igual que
antes). Eso sigue siendo seguro con una sola instancia de la app; si
``core/dispatch.py`` u otro modulo cachea algo por ``storage.data_state()
["version"]`` y la app corre en mas de una tarea de ECS, esa cache queda
desincronizada entre tareas -- no es un bug nuevo de esta migracion (el codigo
original tenia la misma limitacion), pero migrar storage.py por si solo NO
habilita todavia multiples instancias: falta ademas migrar el estado de
picking (core/picking.py, item 3 de la hoja de ruta) y auditar esas cachés.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from core import barcode
from core import reservations as reservation_rules
from core.config import safe_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config y conexion a DynamoDB
# ---------------------------------------------------------------------------
# El nombre de cada tabla es "<prefijo>-<hoja con guiones>" (ver infra/
# uniminuto_infra/data_stack.py). Las credenciales NO se leen de aqui: boto3
# las toma del rol de la tarea de ECS (o de `aws configure`/`aws login` en
# desarrollo local), nunca de un secreto en el codigo.

TABLE_PREFIX = str(safe_secret("DYNAMODB_TABLE_PREFIX", "uniminuto-lab") or "uniminuto-lab").strip() or "uniminuto-lab"
_AWS_REGION = str(safe_secret("AWS_REGION", "") or "").strip() or None

_dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION) if _AWS_REGION else boto3.resource("dynamodb")
_client = _dynamodb.meta.client  # cliente de bajo nivel con auto-marshalling (ver skill aws-sdk-python-usage)

_TABLE_NAMES = (
    "items", "users", "user_email_guards", "professors_whitelist", "loans",
    "reservations", "service_requests", "item_history", "settings",
    "trace_events", "notification_log", "item_photos", "picking_state",
)

_tables: dict = {}


def _table(name: str):
    table = _tables.get(name)
    if table is None:
        table = _dynamodb.Table(f"{TABLE_PREFIX}-{name.replace('_', '-')}")
        _tables[name] = table
    return table


# Columnas conocidas de cada "hoja" (ahora tabla). Se conserva el mismo nombre
# y forma que la version Excel: varios metodos solo guardan columnas conocidas
# (filtran entradas arbitrarias) y varias pruebas existentes importan esto
# directamente.
SHEET_COLUMNS = {
    "items": [
        "id", "name", "category", "description", "item_type", "parent_id",
        "unit", "quantity", "location", "min_stock_alert", "status",
        "created_by", "updated_at", "loose_pieces",
    ],
    "users": [
        "id", "full_name", "student_id", "institutional_email", "password_hash", "role",
        "program_or_department", "status", "created_at",
        "must_change_password", "password_changed_at", "session_epoch",
    ],
    "professors_whitelist": ["institutional_email"],
    "loans": [
        "id", "item_id", "item_name", "parent_id", "quantity", "user_id",
        "user_name", "user_role", "checkout_at", "expected_return_at",
        "return_at", "status", "notes", "request_id",
        "operator_id", "operator_name",
    ],
    "reservations": [
        "id", "scope_type", "activity", "purpose", "attendees", "start_at", "end_at",
        "requester_id", "requester_name", "requester_email", "status", "reviewed_by",
        "reviewed_at", "review_notes", "email_notified", "email_error", "created_at", "updated_at",
    ],
    "service_requests": [
        "id", "request_type", "item_id", "item_name", "quantity", "service_name",
        "description", "needed_at", "requester_id", "requester_name", "requester_email",
        "status", "reviewed_by", "reviewed_at", "review_notes", "email_notified",
        "email_error", "created_at", "updated_at", "order_id",
    ],
    "item_history": [
        "id", "item_id", "timestamp", "type", "quantity_change",
        "actor_user_id", "details",
    ],
    "settings": ["key", "value", "updated_by", "updated_at"],
    "trace_events": [
        "id", "event_type", "request_id", "seq", "item_id", "loan_id",
        "user_id", "owner_id", "actor_id", "actor_name", "actor_email",
        "actor_role", "receipt", "details", "created_at",
    ],
    "notification_log": [
        "id", "event_type", "subject", "recipients", "recipient_count", "status",
        "error", "reference", "actor_email", "created_at",
    ],
    "item_photos": [
        "id", "item_id", "path", "sha", "taken_by", "taken_at", "kind", "note",
    ],
}

NOTIFICATION_LOG_LIMIT = 500
VALID_ITEM_TYPES = ("master", "child", "standalone", "location")
LOCATION_TYPE = "location"
PICKING_STATE_TTL_SECONDS = 6 * 3600  # igual al MAX_AGE_SECONDS que tenia core/picking.py


class RemoteUnavailableError(RuntimeError):
    """Conservada por compatibilidad de import (``app.py`` la captura).

    Con DynamoDB no hay un "remoto" separado de una copia local que pueda
    quedar inalcanzable con datos locales obsoletos: cada lectura ya es contra
    la fuente de verdad. ``LabStorage.__init__`` ya no la lanza; se deja
    definida para que el ``except RemoteUnavailableError`` de ``app.py`` siga
    siendo valido sin tocar ese archivo."""


# ---------------------------------------------------------------------------
# Helpers de bajo nivel
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex[:20]


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(value, default: int = 0) -> int:
    try:
        if isinstance(value, Decimal):
            return int(value)
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _native(value):
    """Decimal (lo que devuelve DynamoDB para numeros) a int/float nativo."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _clean(record: dict) -> dict:
    return {k: _native(v) for k, v in (record or {}).items()}


def _parse_dt(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_record_dates(record: dict, fields: tuple) -> dict:
    for field in fields:
        record[field] = _parse_dt(record.get(field))
    return record


def _item_from_dict(data: dict) -> dict:
    item = _clean(data)
    for col in ("quantity", "min_stock_alert"):
        item[col] = _int(item.get(col))
    item.pop("on_loan", None)  # contabilidad interna (ver create_loan); no es parte del esquema publico
    return item


def _user_from_dict(data: dict) -> dict:
    user = _clean(data)
    user["session_epoch"] = _int(user.get("session_epoch"))
    return user


def _loan_from_dict(data: dict) -> dict:
    loan = _clean(data)
    loan["quantity"] = _int(loan.get("quantity"))
    for field in ("checkout_at", "expected_return_at", "return_at"):
        loan[field] = _parse_dt(loan.get(field))
    return loan


def _reservation_from_dict(data: dict) -> dict:
    record = _clean(data)
    record["attendees"] = _int(record.get("attendees"))
    record["email_notified"] = bool(record.get("email_notified"))
    return _parse_record_dates(record, ("start_at", "end_at", "reviewed_at", "created_at", "updated_at"))


def _service_request_from_dict(data: dict) -> dict:
    record = _clean(data)
    record["quantity"] = _int(record.get("quantity"))
    record["email_notified"] = bool(record.get("email_notified"))
    return _parse_record_dates(record, ("needed_at", "reviewed_at", "created_at", "updated_at"))


_NON_RETRYABLE = (ValueError, KeyError, TypeError, AttributeError, PermissionError)


def with_retry(func):
    """Reintenta (3 veces, espera exponencial) fallos transitorios de DynamoDB
    (throttling, timeouts de red). Nombre heredado: ``firestore_retry``."""
    def wrapper(*args, **kwargs):
        delay = 1
        last_exception = None
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except _NON_RETRYABLE:
                raise
            except Exception as e:
                last_exception = e
                logger.warning(f"Intento {attempt + 1} fallo en {func.__name__}: {e}. Reintentando...")
                if attempt < 2:
                    time.sleep(delay)
                    delay *= 2
        logger.error(f"Todos los reintentos fallaron para {func.__name__}.")
        raise last_exception
    return wrapper


firestore_retry = with_retry  # alias retrocompatible


def _authorize(actor, capability: str, message: str = "No tienes permiso para esta acción.") -> None:
    if actor is None:
        return
    from core import permissions
    permissions.require_cap(actor, capability, message)


# ---------------------------------------------------------------------------
# Estado "de proceso" (cache local y estado de sincronizacion ver docstring)
# ---------------------------------------------------------------------------

_data_state_lock = Lock()
_data_state = {"version": 0, "generation": 1, "last_read_at": None, "last_poll_at": 0.0}


def _bump_data_version(read: bool = False) -> int:
    with _data_state_lock:
        _data_state["version"] += 1
        if read:
            _data_state["last_read_at"] = datetime.now(timezone.utc)
        return _data_state["version"]


def data_state() -> dict:
    with _data_state_lock:
        return dict(_data_state)


def get_sync_status() -> dict:
    """DynamoDB no tiene "pendiente de publicar" ni conflicto: cada escritura
    que ya devolvio control esta consistente. Se conserva la forma del dict
    para que ``core/ui.py`` (sin cambios) siga funcionando."""
    return {
        "configured": True, "ok": True, "message": "", "last_op": None,
        "last_at": datetime.now(timezone.utc).isoformat(), "repo": "DynamoDB",
        "db_path": TABLE_PREFIX, "conflict": False, "mode": "dynamodb", "pending": False,
    }


def sync_mode() -> str:
    return "dynamodb"


def flush_now() -> bool:
    return True


_write_times_ms: list = []


def write_stats() -> dict:
    recent = sorted(_write_times_ms)
    if not recent:
        return {"last_ms": None, "p95_ms": None, "count": 0}
    return {"last_ms": _write_times_ms[-1], "p95_ms": recent[min(len(recent) - 1, int(len(recent) * 0.95))],
            "count": len(recent)}


def _record_write_time(started: float) -> None:
    _write_times_ms.append(round((time.perf_counter() - started) * 1000))
    del _write_times_ms[:-50]


# ---------------------------------------------------------------------------
# Transacciones multi-tabla (reemplazan el lock unico + un solo Excel)
# ---------------------------------------------------------------------------

def _transact(items: list) -> None:
    """``items``: lista de dicts con una clave Put/Update/Delete/ConditionCheck,
    en el formato nativo de ``transact_write_items`` (hasta 100 por llamada)."""
    if not items:
        return
    started = time.perf_counter()
    _client.transact_write_items(TransactItems=items)
    _record_write_time(started)
    _bump_data_version(read=True)


def _put(table_name: str, item: dict) -> dict:
    return {"Put": {"TableName": f"{TABLE_PREFIX}-{table_name.replace('_', '-')}", "Item": item}}


def _put_if_absent(table_name: str, item: dict, key_attr: str = "id") -> dict:
    entry = _put(table_name, item)
    entry["Put"]["ConditionExpression"] = f"attribute_not_exists(#{key_attr})"
    entry["Put"]["ExpressionAttributeNames"] = {f"#{key_attr}": key_attr}
    return entry


def _delete(table_name: str, key: dict) -> dict:
    return {"Delete": {"TableName": f"{TABLE_PREFIX}-{table_name.replace('_', '-')}", "Key": key}}


def _update(table_name: str, key: dict, update_expr: str, names: dict = None, values: dict = None,
            condition: str = None) -> dict:
    entry = {"Update": {
        "TableName": f"{TABLE_PREFIX}-{table_name.replace('_', '-')}", "Key": key,
        "UpdateExpression": update_expr,
    }}
    if names:
        entry["Update"]["ExpressionAttributeNames"] = names
    if values:
        entry["Update"]["ExpressionAttributeValues"] = values
    if condition:
        entry["Update"]["ConditionExpression"] = condition
    return entry


def _condition_check(table_name: str, key: dict, condition: str, values: dict = None, names: dict = None) -> dict:
    """``ConditionCheck``: verifica una condicion dentro de una TransactWriteItems
    SIN modificar el item. Un ``TransactWriteItems`` no admite dos operaciones
    sobre el MISMO item (incluso un Update y un ConditionCheck chocarian), asi
    que esto solo se usa cuando ningun otro item de la transaccion ya toca esa
    misma fila (si lo hace, la condicion se agrega directamente a ese Update)."""
    entry = {"ConditionCheck": {
        "TableName": f"{TABLE_PREFIX}-{table_name.replace('_', '-')}", "Key": key,
        "ConditionExpression": condition,
    }}
    if names:
        entry["ConditionCheck"]["ExpressionAttributeNames"] = names
    if values:
        entry["ConditionCheck"]["ExpressionAttributeValues"] = values
    return entry


def _reserve_seq(request_id: str, count: int = 1) -> int:
    """Reserva ``count`` numeros de secuencia consecutivos para ``request_id``
    con un contador atomico (``ADD``) en la propia fila de ``service_requests``:
    sin esto, dos escrituras de trazabilidad simultaneas del mismo pedido
    podrian repetir ``seq`` (la version Excel lo evitaba con un lock global de
    proceso, que ya no existe con varias instancias)."""
    response = _table("service_requests").update_item(
        Key={"id": request_id},
        UpdateExpression="ADD seq_counter :n",
        ExpressionAttributeValues={":n": count},
        ReturnValues="UPDATED_NEW",
    )
    new_total = _int(response["Attributes"]["seq_counter"])
    return new_total - count + 1


def _trace_event_row(event: dict) -> dict:
    columns = SHEET_COLUMNS["trace_events"]
    row = {column: "" for column in columns}
    row.update({key: "" if value is None else value for key, value in (event or {}).items() if key in columns})
    row["owner_id"] = row["owner_id"] or row["user_id"]
    row["user_id"] = row["user_id"] or row["owner_id"]
    row["id"] = _new_id()
    row["created_at"] = row["created_at"] or _now_str()
    if row.get("seq") == "":
        row.pop("seq")
    return row


def _admin_audit_row(actor: dict, event_type: str, details: dict) -> dict:
    return _trace_event_row({
        "event_type": event_type, "user_id": actor.get("id", ""), "owner_id": actor.get("id", ""),
        "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
        "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
        "details": json.dumps(details, ensure_ascii=False),
    })


# ---------------------------------------------------------------------------
# Validacion de items (reglas de negocio puras; sin cambios respecto al
# formato de codigos GLIOPS V3 de core/barcode.py)
# ---------------------------------------------------------------------------

def _validate_location_data(parent_id: str, get_item_fn, custom_id: str, is_new: bool) -> None:
    if not is_new:
        return
    barcode.validate_location_code(custom_id)
    if not parent_id:
        return
    if parent_id == custom_id:
        raise ValueError("Una ubicacion no puede estar dentro de si misma.")
    parent = get_item_fn(parent_id)
    if not parent:
        raise ValueError(f"La ubicacion '{parent_id}' no existe.")
    if parent.get("item_type") != LOCATION_TYPE:
        raise ValueError(f"'{parent_id}' no es una ubicacion (estanteria, piso, mesa o zona).")
    if parent.get("status") == "retired":
        raise ValueError(f"La ubicacion '{parent_id}' esta dada de baja.")
    problem = barcode.location_parent_problem(custom_id, parent_id)
    if problem:
        raise ValueError(problem)


def _validate_item_data(data: dict, get_item_fn, custom_id: str, is_new: bool = False) -> None:
    """Misma regla que la version Excel; ``get_item_fn(id)`` sustituye el
    filtro sobre el DataFrame ``df_items`` (una consulta puntual a DynamoDB en
    vez de filtrar una copia completa en memoria)."""
    item_type = data.get("item_type", "standalone")
    parent_id = str(data.get("parent_id") or "").strip()
    if parent_id.lower() in ("nan", "none"):
        parent_id = ""

    if item_type == LOCATION_TYPE:
        _validate_location_data(parent_id, get_item_fn, custom_id, is_new)
        return

    if is_new:
        barcode.validate_code_format(custom_id)

    if item_type not in VALID_ITEM_TYPES:
        raise ValueError(f"Tipo de item invalido: '{item_type}'.")

    if item_type == "child":
        if not parent_id:
            raise ValueError("Un Contenedor de Característica debe tener un Contenedor Principal asignado.")
        if parent_id == custom_id:
            raise ValueError("Un item no puede ser su propio contenedor.")
        parent = get_item_fn(parent_id)
        if not parent:
            raise ValueError(f"El Contenedor Principal '{parent_id}' no existe.")
        if parent.get("item_type") != "master":
            raise ValueError(f"'{parent_id}' no es un Contenedor Principal valido.")
    else:
        if parent_id:
            raise ValueError("Solo un Contenedor de Característica puede tener un Contenedor Principal asignado.")


# ---------------------------------------------------------------------------
# Clase principal
# ---------------------------------------------------------------------------

class LabStorage:
    """Capa de base de datos sobre Amazon DynamoDB."""

    def __init__(self):
        pass  # no hay archivo que crear ni "pull" inicial: las tablas ya existen (ver infra/)

    def is_github_sync_active(self) -> bool:
        return False

    def get_sync_status(self) -> dict:
        return get_sync_status()

    def get_data_state(self) -> dict:
        return data_state()

    def refresh_data(self, force_remote: bool = False) -> dict:
        """Boton «Actualizar»: con DynamoDB cada lectura ya es contra la fuente
        de verdad, no hay nada que "traer"; solo se marca la lectura como reciente."""
        with _data_state_lock:
            _data_state["last_read_at"] = datetime.now(timezone.utc)
        return data_state()

    def poll_remote(self, interval_seconds: int = 60) -> bool:
        """Sin efecto: no existe una copia "remota" distinta de lo que ya se lee."""
        return False

    def flush_pending(self) -> bool:
        return True

    def retry_sync(self) -> bool:
        return True

    def resolve_sync_conflict(self, keep: str, actor: dict = None):
        if keep not in ("remote", "local"):
            raise ValueError("keep debe ser 'remote' o 'local'.")
        _authorize(actor, "resolve_sync", "Solo un administrador resuelve conflictos de sincronización.")
        return True, "No aplica: con DynamoDB no hay conflictos de sincronización que resolver."

    # ------------------------------------------------------------------
    # ITEMS
    # ------------------------------------------------------------------

    def _cascade_ids(self, item_id: str) -> list:
        """El item y, si es Contenedor Principal o ubicacion, lo que contiene."""
        item = self.get_item(item_id)
        ids = [item_id]
        if not item:
            return ids
        if item.get("item_type") == "master":
            ids.extend(child["id"] for child in self.get_children(item_id, include_retired=True))
        elif item.get("item_type") == LOCATION_TYPE:
            pending = [item_id]
            while pending:
                current = pending.pop()
                for child in self.get_children(current, include_retired=True):
                    if child.get("item_type") == LOCATION_TYPE and child["id"] not in ids:
                        ids.append(child["id"])
                        pending.append(child["id"])
        return ids

    def _purge_item_records(self, ids: list, actor_email: str = "") -> dict:
        """Borra DEFINITIVAMENTE todo rastro de ``ids``: el item, su historial y
        sus prestamos ya devueltos; cancela solicitudes pendientes/aprobadas que
        apuntaban a ellos. El llamador ya comprobo que no hay prestamos abiertos."""
        counts = {"items": 0, "history": 0, "loans": 0, "requests_cancelled": 0, "photos": 0}
        for item_id in ids:
            if self.get_item(item_id):
                _table("items").delete_item(Key={"id": item_id})
                counts["items"] += 1
            for row in self._query_gsi("item_history", "item_id-timestamp-index", "item_id", item_id):
                _table("item_history").delete_item(Key={"id": row["id"]})
                counts["history"] += 1
            for row in self._query_gsi("loans", "item_id-status-index", "item_id", item_id):
                _table("loans").delete_item(Key={"id": row["id"]})
                counts["loans"] += 1
            for row in self._query_gsi("item_photos", "item_id-taken_at-index", "item_id", item_id):
                _table("item_photos").delete_item(Key={"id": row["id"]})
                counts["photos"] += 1
            requests = [r for r in self.get_service_requests() if r.get("item_id") == item_id
                        and r.get("status") in ("pending", "approved")]
            for request in requests:
                self.update_service_request_status(request["id"], "cancelled", actor_email,
                                                   "Producto eliminado del inventario.")
                counts["requests_cancelled"] += 1
        return counts

    def _query_gsi(self, table_name: str, index_name: str, pk_name: str, pk_value: str,
                   sk_name: str = None, sk_value=None) -> list:
        key_cond = Key(pk_name).eq(pk_value)
        if sk_name and sk_value is not None:
            key_cond = key_cond & Key(sk_name).eq(sk_value)
        items, start_key = [], None
        while True:
            kwargs = {"IndexName": index_name, "KeyConditionExpression": key_cond}
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            response = _table(table_name).query(**kwargs)
            items.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return items

    def _scan_all(self, table_name: str, filter_expression=None) -> list:
        items, start_key = [], None
        while True:
            kwargs = {}
            if filter_expression is not None:
                kwargs["FilterExpression"] = filter_expression
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            response = _table(table_name).scan(**kwargs)
            items.extend(response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return items

    @with_retry
    def save_item(self, data: dict, custom_id: str, is_new: bool = False, actor_email: str = "",
                  details: str = None, actor: dict = None):
        _authorize(actor, "edit_inventory", "No tienes permiso para crear o editar ítems.")
        existing = self.get_item(custom_id)

        if is_new and existing is not None:
            if existing.get("status") != "retired":
                raise ValueError("Ya existe un item con ese codigo.")
            self._purge_item_records(self._cascade_ids(custom_id), actor_email)
            existing = None

        item_type = existing["item_type"] if existing is not None else data.get("item_type", "standalone")
        parent_id = existing["parent_id"] if existing is not None else (data.get("parent_id", "") or "")
        _validate_item_data({"item_type": item_type, "parent_id": parent_id}, self.get_item, custom_id,
                            is_new=(existing is None))

        old_quantity = _int(existing.get("quantity")) if existing is not None else 0
        new_quantity = _int(data.get("quantity", 0))
        min_stock_alert = _int(data.get("min_stock_alert", 0))
        if item_type == LOCATION_TYPE:
            new_quantity = min_stock_alert = 0
        quantity_delta = new_quantity - old_quantity if existing is not None else new_quantity

        history_type = "Alta" if is_new else "Ajuste"
        details = details or ("Item creado en el sistema." if is_new else "Item actualizado manualmente.")

        row = {
            "id": custom_id, "name": data.get("name", ""), "category": data.get("category", ""),
            "description": data.get("description", ""), "item_type": item_type, "parent_id": parent_id,
            "unit": data.get("unit", "unidad"), "quantity": new_quantity, "location": data.get("location", ""),
            "min_stock_alert": min_stock_alert, "status": data.get("status", "active"),
            "created_by": data.get("created_by", actor_email) if existing is None else existing.get("created_by", ""),
            "updated_at": _now_str(),
            "on_loan": _int(existing.get("on_loan")) if existing is not None else 0,
            "loose_pieces": ("1" if str(data["loose_pieces"]).strip().lower() in ("1", "true", "si", "sí", "x")
                             else "") if "loose_pieces" in data
            else ((existing or {}).get("loose_pieces") or ""),
        }
        hist_row = {
            "id": _new_id(), "item_id": custom_id, "timestamp": _now_str(), "type": history_type,
            "quantity_change": quantity_delta, "actor_user_id": actor_email, "details": details,
        }
        # existing is None == alta real (incluye el codigo liberado por una baja
        # que _purge_item_records ya limpio arriba): la condicion evita que dos
        # altas simultaneas con el mismo codigo se pisen entre sí.
        items_entry = _put_if_absent("items", row) if existing is None else _put("items", row)
        transact_items = [items_entry, _put("item_history", hist_row)]
        try:
            _transact(transact_items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise ValueError("Ya existe un item con ese codigo.") from exc
            raise
        logger.info(f"Item guardado/actualizado: {custom_id}")

    def bulk_upsert_items(self, rows: list, actor_email: str = "", actor: dict = None) -> dict:
        """Crea/actualiza varios items. Cada fila se valida con las mismas
        reglas de ``save_item``; las validas se escriben en lote (``batch_writer``
        en vez de un solo commit de GitHub, que ya no existe)."""
        _authorize(actor, "edit_inventory", "No tienes permiso para importar ítems.")
        created, updated, errors = [], [], []
        new_rows, hist_rows = [], []
        for raw in rows:
            custom_id = str(raw.get("id", "")).strip()
            name = str(raw.get("name", "")).strip()
            if not custom_id or not name:
                errors.append(f"Fila omitida: id o nombre vacios ({raw}).")
                continue
            item_type = str(raw.get("item_type", "standalone")).strip() or "standalone"
            parent_id = str(raw.get("parent_id", "") or "").strip()
            existing = self.get_item(custom_id)
            was_retired = bool(existing) and existing.get("status") == "retired"
            is_new = existing is None or was_retired
            try:
                _validate_item_data({"item_type": item_type, "parent_id": parent_id}, self.get_item, custom_id,
                                    is_new=is_new)
            except ValueError as exc:
                errors.append(f"'{custom_id}': {exc}")
                continue
            if was_retired:
                self._purge_item_records(self._cascade_ids(custom_id), actor_email)
            no_stock = item_type == LOCATION_TYPE
            row = {
                "id": custom_id, "name": name, "category": str(raw.get("category", "") or ""),
                "description": str(raw.get("description", "") or ""), "item_type": item_type,
                "parent_id": parent_id, "unit": str(raw.get("unit", "unidad") or "unidad"),
                "quantity": 0 if no_stock else _int(raw.get("quantity", 0)),
                "location": str(raw.get("location", "") or ""),
                "min_stock_alert": 0 if no_stock else _int(raw.get("min_stock_alert", 0)),
                "status": "active", "created_by": actor_email, "updated_at": _now_str(), "on_loan": 0,
            }
            new_rows.append(row)
            hist_rows.append({
                "id": _new_id(), "item_id": custom_id, "timestamp": _now_str(),
                "type": "Alta" if is_new else "Ajuste", "quantity_change": row["quantity"],
                "actor_user_id": actor_email, "details": "Importacion masiva (CSV).",
            })
            (updated if existing and not was_retired else created).append(custom_id)

        if new_rows:
            with _table("items").batch_writer() as batch:
                for row in new_rows:
                    batch.put_item(Item=row)
            with _table("item_history").batch_writer() as batch:
                for row in hist_rows:
                    batch.put_item(Item=row)
            _bump_data_version(read=True)
        return {"created": created, "updated": updated, "errors": errors}

    def retire_item(self, item_id: str, actor_email: str = "", actor: dict = None):
        _authorize(actor, "edit_inventory", "No tienes permiso para dar de baja ítems.")
        item = self.get_item(item_id)
        if not item:
            return False, "El item no existe."
        ids_to_retire = self._cascade_ids(item_id) if item.get("item_type") in ("master", LOCATION_TYPE) else [item_id]

        for iid in ids_to_retire:
            if self._query_gsi("loans", "item_id-status-index", "item_id", iid, "status", "out"):
                return False, "No se puede dar de baja: hay prestamos abiertos de este item o de items dentro de el."

        now = _now_str()
        with _table("items").batch_writer() as batch, _table("item_history").batch_writer() as hist_batch:
            for iid in ids_to_retire:
                record = self.get_item(iid)
                if record:
                    record["status"] = "retired"
                    record["updated_at"] = now
                    batch.put_item(Item=record)
                hist_batch.put_item(Item={
                    "id": _new_id(), "item_id": iid, "timestamp": now, "type": "Baja", "quantity_change": 0,
                    "actor_user_id": actor_email,
                    "details": "Item dado de baja." if iid == item_id else f"Baja en cascada (contenedor {item_id}).",
                })
        _bump_data_version(read=True)
        return True, "Item dado de baja correctamente."

    def delete_item(self, item_id: str, actor_email: str = "", actor: dict = None):
        _authorize(actor, "edit_inventory", "No tienes permiso para eliminar ítems.")
        item_id = (item_id or "").strip()
        item = self.get_item(item_id)
        if not item:
            return False, "El item no existe."
        ids = self._cascade_ids(item_id)
        for iid in ids:
            if self._query_gsi("loans", "item_id-status-index", "item_id", iid, "status", "out"):
                return False, "No se puede eliminar: hay prestamos abiertos de este item o de items dentro de el."
        name = item["name"]
        counts = self._purge_item_records(ids, actor_email)
        logger.info(f"Item eliminado definitivamente: {item_id} ({counts}) por {actor_email or 'sistema'}")
        extra = f" y {len(ids) - 1} item(s) que contenia" if len(ids) > 1 else ""
        return True, f"'{name}'{extra} se elimino definitivamente de la base de datos."

    @with_retry
    def get_delete_impact(self, item_id: str) -> dict:
        item = self.get_item(item_id)
        if not item:
            return {"exists": False, "contained_items": 0, "open_loans": 0, "closed_loans": 0, "open_requests": 0}
        ids = self._cascade_ids(item_id)
        open_loans = closed_loans = 0
        for iid in ids:
            for loan in self._query_gsi("loans", "item_id-status-index", "item_id", iid):
                if loan.get("status") == "out":
                    open_loans += 1
                else:
                    closed_loans += 1
        open_requests = sum(1 for r in self.get_service_requests()
                            if r.get("item_id") in ids and r.get("status") in ("pending", "approved"))
        impact = {"exists": True, "contained_items": len(ids) - 1, "open_loans": open_loans,
                  "closed_loans": closed_loans, "open_requests": open_requests}
        if item.get("item_type") == LOCATION_TYPE:
            impact["item_type"] = LOCATION_TYPE
        return impact

    @with_retry
    def item_code_in_use(self, code: str) -> bool:
        item = self.get_item((code or "").strip())
        return bool(item) and item.get("status") != "retired"

    @with_retry
    def get_item(self, item_id: str):
        response = _table("items").get_item(Key={"id": item_id})
        item = response.get("Item")
        return _item_from_dict(item) if item else None

    @with_retry
    def get_all_items(self, include_retired: bool = False) -> list:
        items = [_item_from_dict(record) for record in self._scan_all("items")]
        if not include_retired:
            items = [i for i in items if i.get("status") != "retired"]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @with_retry
    def get_children(self, parent_id: str, include_retired: bool = False) -> list:
        rows = self._query_gsi("items", "parent_id-index", "parent_id", parent_id)
        items = [_item_from_dict(record) for record in rows]
        if not include_retired:
            items = [i for i in items if i.get("status") != "retired"]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @with_retry
    def get_all_masters(self) -> list:
        rows = self._scan_all("items", Attr("item_type").eq("master") & Attr("status").ne("retired"))
        items = [_item_from_dict(record) for record in rows]
        return sorted(items, key=lambda x: (x.get("name") or "").lower())

    @with_retry
    def get_available_quantity(self, item_id: str) -> int:
        item = self.get_item(item_id)
        if not item:
            return 0
        return max(_int(item.get("quantity")) - _int(item.get("on_loan")), 0)

    @with_retry
    def get_availability_map(self) -> dict:
        """Disponibilidad de TODOS los items en una sola pasada. A diferencia de
        la version Excel (que sumaba `loans` abiertos por item), aqui se lee
        directo el contador `on_loan` que mantiene `create_loan`/`return_loan`
        de forma atomica en el propio item: un Scan en vez de dos lecturas."""
        rows = self._scan_all("items")
        return {row["id"]: max(_int(row.get("quantity")) - _int(row.get("on_loan")), 0) for row in rows}

    # ------------------------------------------------------------------
    # USUARIOS
    # ------------------------------------------------------------------

    @with_retry
    def get_user_by_email(self, email: str):
        rows = self._query_gsi("users", "institutional_email-index", "institutional_email",
                               email.lower().strip())
        return _user_from_dict(rows[0]) if rows else None

    @with_retry
    def get_user_by_id(self, user_id: str):
        response = _table("users").get_item(Key={"id": user_id})
        item = response.get("Item")
        return _user_from_dict(item) if item else None

    def create_user(self, full_name: str, email: str, password_hash: str, role: str, program: str = "",
                    student_id: str = "", status: str = "active") -> dict:
        normalized = email.lower().strip()
        row = {
            "id": _new_id(), "full_name": full_name, "student_id": student_id,
            "institutional_email": normalized, "password_hash": password_hash, "role": role,
            "program_or_department": program, "status": status, "created_at": _now_str(), "session_epoch": 0,
        }
        guard = {"id": normalized, "institutional_email": normalized, "user_id": row["id"]}
        try:
            _transact([_put_if_absent("user_email_guards", guard), _put("users", row)])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise ValueError("Ya existe una cuenta registrada con ese correo.") from exc
            raise
        return row

    def update_user(self, user_id: str, changes: dict, actor: dict = None):
        allowed = {
            "full_name", "student_id", "institutional_email", "password_hash",
            "role", "program_or_department", "status",
            "must_change_password", "password_changed_at", "session_epoch",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Campos de usuario no permitidos: {', '.join(sorted(unknown))}.")
        existing = self.get_user_by_id(user_id)
        if not existing:
            return None
        changes = dict(changes)
        old_role = str(existing.get("role") or "")

        if {"role", "status"} & set(changes):
            from core import permissions
            if actor is None:
                raise PermissionError("Cambiar roles o estados requiere un actor autorizado.")
            permissions.require_cap(actor, permissions.CAP_MANAGE_USERS,
                                    "Solo el administrador puede cambiar roles o estados.")
            if actor.get("id") == user_id:
                raise PermissionError("No puedes cambiar tu propio rol ni deshabilitarte.")
        elif actor is not None and actor.get("id") != user_id:
            from core import permissions
            permissions.require_cap(actor, permissions.CAP_MANAGE_USERS,
                                    "Solo el administrador puede editar a otra persona.")

        new_email = None
        if "institutional_email" in changes:
            new_email = str(changes["institutional_email"] or "").lower().strip()
            if new_email != existing["institutional_email"] and self.get_user_by_email(new_email):
                raise ValueError("Ya existe otra cuenta con ese correo institucional.")
            changes["institutional_email"] = new_email
        if "role" in changes:
            from core import permissions
            if changes["role"] not in permissions.ROLES:
                raise ValueError("Rol de usuario inválido.")
        if "status" in changes and changes["status"] not in ("active", "disabled"):
            raise ValueError("Estado de usuario inválido.")

        revoking = "password_hash" in changes or (
            actor is not None and (
                ("role" in changes and str(changes["role"]) != old_role)
                or ("status" in changes and str(changes["status"]) != str(existing.get("status")))
            )
        )
        if revoking and "session_epoch" not in changes:
            changes["session_epoch"] = _int(existing.get("session_epoch")) + 1

        updated = dict(existing)
        updated.update(changes)
        transact_items = [_put("users", updated)]
        if new_email and new_email != existing["institutional_email"]:
            transact_items = [
                _delete("user_email_guards", {"id": existing["institutional_email"]}),
                _put_if_absent("user_email_guards",
                               {"id": new_email, "institutional_email": new_email, "user_id": user_id}),
            ] + transact_items
        if actor is not None and "role" in changes and str(changes["role"]) != old_role:
            transact_items.append(_put("trace_events", _trace_event_row({
                "event_type": "role_changed", "user_id": user_id, "owner_id": user_id,
                "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
                "actor_email": actor.get("institutional_email", ""), "actor_role": actor.get("role", ""),
                "details": json.dumps({"from_role": old_role, "to_role": changes["role"]}, ensure_ascii=False),
            })))
        try:
            _transact(transact_items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise ValueError("Ya existe otra cuenta con ese correo institucional.") from exc
            raise
        return _user_from_dict(updated)

    @with_retry
    def get_all_users(self) -> list:
        users = [_user_from_dict(record) for record in self._scan_all("users")]
        return sorted(users, key=lambda x: (x.get("full_name") or "").lower())

    def count_users(self) -> int:
        return len(self._scan_all("users"))

    def count_masters(self) -> int:
        return len(self._scan_all("users", Attr("role").eq("maestro")))

    # ------------------------------------------------------------------
    # LISTA BLANCA DE PROFESORES
    # ------------------------------------------------------------------

    @with_retry
    def is_email_whitelisted_professor(self, email: str) -> bool:
        response = _table("professors_whitelist").get_item(Key={"institutional_email": email.lower().strip()})
        return "Item" in response

    @with_retry
    def get_whitelist(self) -> list:
        return sorted(row["institutional_email"] for row in self._scan_all("professors_whitelist"))

    def add_to_whitelist(self, email: str, actor: dict = None):
        _authorize(actor, "manage_users", "Solo un administrador modifica la lista blanca.")
        email = email.lower().strip()
        if self.is_email_whitelisted_professor(email):
            return
        _table("professors_whitelist").put_item(Item={"institutional_email": email})
        if actor is not None:
            self.add_trace_event(_admin_audit_row(actor, "whitelist_changed", {"action": "added", "email": email}))

    def remove_from_whitelist(self, email: str, actor: dict = None):
        _authorize(actor, "manage_users", "Solo un administrador modifica la lista blanca.")
        email = email.lower().strip()
        present = self.is_email_whitelisted_professor(email)
        _table("professors_whitelist").delete_item(Key={"institutional_email": email})
        if present and actor is not None:
            self.add_trace_event(_admin_audit_row(actor, "whitelist_changed", {"action": "removed", "email": email}))

    # ------------------------------------------------------------------
    # PRESTAMOS (loans)
    # ------------------------------------------------------------------

    def create_loan(self, item: dict, quantity: int, user: dict, expected_return_at=None,
                    notes: str = "", request_id: str = "", operator: dict = None) -> dict:
        if int(quantity) <= 0:
            raise ValueError("La cantidad debe ser mayor a cero.")
        current = self.get_item(item["id"])
        if current and current.get("status") == "retired":
            raise ValueError("El producto fue dado de baja: no se puede prestar.")
        row = {
            "id": _new_id(), "item_id": item["id"], "item_name": item.get("name", ""),
            "parent_id": item.get("parent_id", "") or "", "quantity": int(quantity), "user_id": user["id"],
            "user_name": user.get("full_name", ""), "user_role": user.get("role", ""),
            "checkout_at": _now_str(), "status": "out", "notes": notes, "request_id": request_id,
            "operator_id": (operator or {}).get("id", ""), "operator_name": (operator or {}).get("full_name", ""),
        }
        if expected_return_at:
            row["expected_return_at"] = expected_return_at.isoformat()
        dispatched = f" · despachado por {operator.get('full_name')}" if operator and operator.get("full_name") else ""
        hist_row = {
            "id": _new_id(), "item_id": item["id"], "timestamp": _now_str(), "type": "Salida",
            "quantity_change": -int(quantity), "actor_user_id": user.get("institutional_email", ""),
            "details": f"Prestado a {user.get('full_name', '')} ({row['id']}){dispatched}",
        }
        self._reserve_stock_and_write(item["id"], int(quantity), [_put("loans", row), _put("item_history", hist_row)])
        return row

    def _reserve_stock_and_write(self, item_id: str, quantity: int, extra_items: list) -> None:
        """``ADD on_loan`` con una condicion que nunca deja prestar mas de lo
        disponible, en la MISMA transaccion que el resto de la escritura: el
        contador en el item sustituye la suma de `loans` abiertos que hacia la
        version Excel (mas rapido de leer y, a diferencia de esa version, a
        prueba de carreras entre instancias)."""
        update = _update(
            "items", {"id": item_id}, "ADD on_loan :q",
            values={":q": quantity, ":avail": quantity},
            condition="quantity - if_not_exists(on_loan, :zero) >= :avail",
        )
        update["Update"]["ExpressionAttributeValues"][":zero"] = 0
        try:
            _transact([update] + extra_items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                available = self.get_available_quantity(item_id)
                raise ValueError(f"Stock insuficiente. Disponible: {available}.") from exc
            raise

    def return_loan(self, loan_id: str, actor_email: str = "", actor: dict = None,
                    quantity: int = None, note: str = ""):
        response = _table("loans").get_item(Key={"id": loan_id})
        loan = response.get("Item")
        if not loan:
            return False, "El prestamo no existe."
        if actor is not None:
            from core import permissions
            if not permissions.has_cap(actor, permissions.CAP_CHECKIN_ANY):
                raise PermissionError("No puedes reingresar un préstamo directamente.")
        if loan.get("return_at"):
            return False, "Este prestamo ya fue devuelto."

        item_id = loan["item_id"]
        borrowed = _int(loan.get("quantity"))
        qty = borrowed if quantity is None else int(quantity)
        if qty <= 0 or qty > borrowed:
            return False, f"La cantidad devuelta debe estar entre 1 y {borrowed}."
        partial = qty < borrowed

        loan_update = _update(
            "loans", {"id": loan_id},
            "SET quantity = :remaining" if partial else "SET return_at = :now, #st = :returned",
            names=None if partial else {"#st": "status"},
            values={":remaining": borrowed - qty} if partial else {":now": _now_str(), ":returned": "returned"},
            condition="attribute_not_exists(return_at)",
        )
        details = f"Reingreso de prestamo {loan_id}" + (f" ({qty} de {borrowed})" if partial else "")
        if note:
            details += f" · {note}"
        hist_row = {
            "id": _new_id(), "item_id": item_id, "timestamp": _now_str(), "type": "Reingreso",
            "quantity_change": qty, "actor_user_id": actor_email, "details": details,
        }
        release = _update("items", {"id": item_id}, "ADD on_loan :q", values={":q": -qty})
        try:
            _transact([loan_update, release, _put("item_history", hist_row)])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                return False, "Este prestamo ya fue devuelto."
            raise
        if partial:
            return True, f"Reingreso parcial: {qty} de {borrowed}. Quedan {borrowed - qty} por devolver."
        return True, "Reingreso registrado correctamente."

    @with_retry
    def get_open_loans_for_item(self, item_id: str) -> list:
        rows = self._query_gsi("loans", "item_id-status-index", "item_id", item_id, "status", "out")
        return [_loan_from_dict(r) for r in rows]

    @with_retry
    def get_open_loans_for_user(self, user_id: str) -> list:
        rows = self._query_gsi("loans", "user_id-index", "user_id", user_id)
        loans = [_loan_from_dict(r) for r in rows if r.get("status") == "out"]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)

    @with_retry
    def get_all_loans(self, status: str = None) -> list:
        rows = self._scan_all("loans", Attr("status").eq(status) if status else None)
        loans = [_loan_from_dict(r) for r in rows]
        return sorted(loans, key=lambda x: x.get("checkout_at") or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)

    @with_retry
    def get_item_history(self, item_id: str) -> list:
        rows = self._query_gsi("item_history", "item_id-timestamp-index", "item_id", item_id)
        history = [_clean(r) for r in rows]
        return sorted(history, key=lambda x: x.get("timestamp") or "", reverse=True)

    # ------------------------------------------------------------------
    # RESERVAS DE LABORATORIO
    # ------------------------------------------------------------------

    def _approved_reservations(self) -> list:
        rows = self._scan_all("reservations", Attr("status").eq("approved"))
        return [_reservation_from_dict(r) for r in rows]

    def create_reservation(self, data: dict, user: dict) -> dict:
        conflict = reservation_rules.find_conflict(self._approved_reservations(), data)
        if conflict:
            raise ValueError(f"Existe una reserva aprobada que se cruza con ese horario: {conflict.get('activity')}.")
        now = _now_str()
        row = {
            "id": _new_id(), "scope_type": data["scope_type"], "activity": data.get("activity", ""),
            "purpose": data.get("purpose", ""), "attendees": int(data.get("attendees", 1)),
            "start_at": data["start_at"].isoformat(), "end_at": data["end_at"].isoformat(),
            "requester_id": user["id"], "requester_name": user.get("full_name", ""),
            "requester_email": user.get("institutional_email", ""), "status": "pending",
            "email_notified": False, "created_at": now, "updated_at": now,
        }
        _table("reservations").put_item(Item=row)
        _bump_data_version(read=True)
        return row

    @with_retry
    def get_reservation(self, reservation_id: str):
        response = _table("reservations").get_item(Key={"id": reservation_id})
        item = response.get("Item")
        return _reservation_from_dict(item) if item else None

    @with_retry
    def get_reservations(self, status: str = None, user_id: str = None) -> list:
        if user_id:
            rows = self._query_gsi("reservations", "requester_id-index", "requester_id", user_id)
            if status:
                rows = [r for r in rows if r.get("status") == status]
        elif status:
            rows = self._scan_all("reservations", Attr("status").eq(status))
        else:
            rows = self._scan_all("reservations")
        reservations = [_reservation_from_dict(r) for r in rows]
        aware_min = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(reservations, key=lambda row: row.get("start_at") or aware_min)

    def update_reservation_status(self, reservation_id: str, status: str, reviewer_email: str,
                                  notes: str = "") -> bool:
        reservation = self.get_reservation(reservation_id)
        if not reservation:
            return False
        if status in ("approved", "rejected") and reservation.get("status") != "pending":
            raise ValueError("La reserva ya fue revisada por otra persona.")
        if status == "approved":
            conflict = reservation_rules.find_conflict(self._approved_reservations(), reservation,
                                                        exclude_id=reservation_id)
            if conflict:
                raise ValueError(f"Conflicto con la reserva aprobada: {conflict.get('activity')}.")
        now = _now_str()
        try:
            _table("reservations").update_item(
                Key={"id": reservation_id},
                UpdateExpression="SET #st = :status, reviewed_by = :by, reviewed_at = :at, "
                                 "review_notes = :notes, updated_at = :now",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":status": status, ":by": reviewer_email, ":at": now,
                                          ":notes": notes, ":now": now, ":pending": "pending"},
                ConditionExpression="#st = :pending",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError("La reserva ya fue revisada por otra persona.") from exc
            raise
        return True

    def update_reservation_notification(self, reservation_id: str, sent: bool, error: str = "") -> bool:
        try:
            _table("reservations").update_item(
                Key={"id": reservation_id},
                UpdateExpression="SET email_notified = :sent, email_error = :error, updated_at = :now",
                ExpressionAttributeValues={":sent": bool(sent), ":error": error, ":now": _now_str()},
                ConditionExpression="attribute_exists(id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ------------------------------------------------------------------
    # SOLICITUDES DE PRODUCTOS Y SERVICIOS
    # ------------------------------------------------------------------

    @staticmethod
    def _service_request_row(data: dict, user: dict, now: str, order_id: str = "") -> dict:
        return {
            "id": _new_id(), "request_type": data["request_type"], "item_id": data.get("item_id", ""),
            "item_name": data.get("item_name", ""), "quantity": int(data.get("quantity", 0)),
            "service_name": data.get("service_name", ""), "description": data.get("description", ""),
            "needed_at": data.get("needed_at", ""), "requester_id": user["id"],
            "requester_name": user.get("full_name", ""), "requester_email": user.get("institutional_email", ""),
            "status": "pending", "email_notified": False, "created_at": now, "updated_at": now,
            "order_id": order_id or data.get("order_id", ""), "seq_counter": 0,
        }

    def create_service_request(self, data: dict, user: dict) -> dict:
        return self.create_service_requests([data], user)[0]

    def create_service_requests(self, rows: list, user: dict, order_id: str = "") -> list:
        now = _now_str()
        created = [self._service_request_row(data, user, now, order_id) for data in rows]
        with _table("service_requests").batch_writer() as batch:
            for row in created:
                batch.put_item(Item=row)
        _bump_data_version(read=True)
        return created

    @with_retry
    def get_service_request(self, request_id: str):
        response = _table("service_requests").get_item(Key={"id": request_id})
        item = response.get("Item")
        return _service_request_from_dict(item) if item else None

    @with_retry
    def get_service_requests(self, status: str = None, user_id: str = None, exclude_ids=None) -> list:
        if status:
            rows = self._query_gsi("service_requests", "status-created_at-index", "status", status)
            if user_id:
                rows = [r for r in rows if r.get("requester_id") == user_id]
        elif user_id:
            rows = self._query_gsi("service_requests", "requester_id-index", "requester_id", user_id)
        else:
            rows = self._scan_all("service_requests")
        if exclude_ids:
            exclude = set(exclude_ids)
            rows = [r for r in rows if r.get("id") not in exclude]
        requests = [_service_request_from_dict(r) for r in rows]
        aware_min = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(requests, key=lambda row: row.get("created_at") or aware_min, reverse=True)

    def update_service_request_status(self, request_id: str, status: str, reviewer_email: str,
                                      notes: str = "") -> bool:
        now = _now_str()
        try:
            _table("service_requests").update_item(
                Key={"id": request_id},
                UpdateExpression="SET #st = :status, reviewed_by = :by, reviewed_at = :at, "
                                 "review_notes = :notes, updated_at = :now",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":status": status, ":by": reviewer_email, ":at": now, ":notes": notes,
                                          ":now": now},
                ConditionExpression="attribute_exists(id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def update_service_request_notification(self, request_id: str, sent: bool, error: str = "") -> bool:
        try:
            _table("service_requests").update_item(
                Key={"id": request_id},
                UpdateExpression="SET email_notified = :sent, email_error = :error, updated_at = :now",
                ExpressionAttributeValues={":sent": bool(sent), ":error": error, ":now": _now_str()},
                ConditionExpression="attribute_exists(id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def transition_request(self, request_id: str, event_type: str, actor: dict, details: dict = None):
        """Valida y aplica una transicion de solicitud en UNA transaccion.

        La version Excel serializaba TODAS las escrituras con un lock de
        proceso; aqui la atomicidad la da ``TransactWriteItems`` mas un
        ``ConditionCheck`` sobre ``updated_at`` del pedido (optimista): si otra
        transicion lo cambio entre la lectura y la escritura, la transaccion se
        cancela y se reintenta con datos frescos (hasta 3 veces) en vez de
        arriesgar un doble despacho."""
        from core import dispatch

        actor = dict(actor or {})
        for attempt in range(3):
            request = self.get_service_request(request_id)
            if not request:
                return False, "La solicitud no existe.", None
            request_events = self.get_trace_events(request_id=request_id)
            item_id = request.get("item_id", "") or ""
            open_loan = None
            if item_id:
                open_rows = self._query_gsi("loans", "request_id-index", "request_id", request_id)
                open_rows = [r for r in open_rows if r.get("status") == "out"]
                open_loan = _loan_from_dict(open_rows[0]) if open_rows else None
            available = self.get_available_quantity(item_id) if item_id else None
            plan = dispatch.plan_transition(request, request_events, actor, event_type, details,
                                            available=available, open_loan=open_loan)
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            expected_updated_at = request.get("updated_at")
            expected_updated_at = (expected_updated_at.isoformat() if hasattr(expected_updated_at, "isoformat")
                                   else expected_updated_at)

            def event_row(kind, payload, seq, loan_id=""):
                return _trace_event_row({
                    "event_type": kind, "request_id": request_id, "seq": seq, "item_id": item_id,
                    "loan_id": loan_id, "user_id": request.get("requester_id", ""),
                    "owner_id": request.get("requester_id", ""), "actor_id": actor.get("id", ""),
                    "actor_name": actor.get("full_name", ""), "actor_email": actor.get("institutional_email", ""),
                    "actor_role": actor.get("role", ""),
                    "details": json.dumps(payload, ensure_ascii=False, default=str), "created_at": now,
                })

            # Un unico item de transaccion por fila de `service_requests`: si esta
            # transicion no cambia su status, el ConditionCheck por si solo basta
            # para que la escritura falle si otra transicion la cambio primero.
            request_guard = _condition_check(
                "service_requests", {"id": request_id}, condition="updated_at = :expected",
                values={":expected": expected_updated_at},
            )

            try:
                if not plan["ok"]:
                    seq = _reserve_seq(request_id)
                    row = event_row(plan["failure_kind"], plan["failure_details"], seq)
                    _transact([request_guard, _put("trace_events", row)])
                    return False, plan["message"], row

                quantity = _int(request.get("quantity"))
                loan_id = plan.get("loan_id", "")
                write_items = [] if plan["status"] else [request_guard]
                if plan["loan_action"] == "create":
                    loan_id = _new_id()
                    item_row = self.get_item(item_id)
                    requester = self.get_user_by_id(request.get("requester_id", ""))
                    loan = {
                        "id": loan_id, "item_id": item_id, "item_name": request.get("item_name", ""),
                        "parent_id": (item_row or {}).get("parent_id", "") or "", "quantity": quantity,
                        "user_id": request.get("requester_id", ""), "user_name": request.get("requester_name", ""),
                        "user_role": (requester or {}).get("role", "") or "", "checkout_at": now,
                        "status": "out", "notes": f"Picking de la solicitud {request_id}", "request_id": request_id,
                        "operator_id": actor.get("id", ""), "operator_name": actor.get("full_name", ""),
                    }
                    history = {
                        "id": _new_id(), "item_id": item_id, "timestamp": now, "type": "Salida",
                        "quantity_change": -quantity, "actor_user_id": actor.get("institutional_email", ""),
                        "details": f"Picking de la solicitud {request_id} a nombre de "
                                   f"{request.get('requester_name', '')} ({loan_id}) · "
                                   f"despachado por {actor.get('full_name', '') or 'un operario'}",
                    }
                    reserve = _update(
                        "items", {"id": item_id}, "ADD on_loan :q",
                        values={":q": quantity, ":avail": quantity, ":zero": 0},
                        condition="quantity - if_not_exists(on_loan, :zero) >= :avail",
                    )
                    write_items += [reserve, _put("loans", loan), _put("item_history", history)]
                elif plan["loan_action"] == "reuse":
                    write_items.append(_update(
                        "loans", {"id": loan_id}, "SET operator_id = :oid, operator_name = :oname",
                        values={":oid": actor.get("id", ""), ":oname": actor.get("full_name", "")},
                    ))
                elif plan["loan_action"] == "close":
                    loan_row = _table("loans").get_item(Key={"id": loan_id}).get("Item") or {}
                    returned = _int(loan_row.get("quantity"))
                    history = {
                        "id": _new_id(), "item_id": item_id, "timestamp": now, "type": "Reingreso",
                        "quantity_change": returned, "actor_user_id": actor.get("institutional_email", ""),
                        "details": f"Devuelto al estante desde la solicitud {request_id} ({loan_id})",
                    }
                    write_items += [
                        _update("loans", {"id": loan_id}, "SET return_at = :now, #st = :returned",
                               names={"#st": "status"}, values={":now": now, ":returned": "returned"}),
                        _update("items", {"id": item_id}, "ADD on_loan :q", values={":q": -returned}),
                        _put("item_history", history),
                    ]

                if plan["status"]:
                    status_update = _update(
                        "service_requests", {"id": request_id},
                        "SET #st = :status, reviewed_by = :by, reviewed_at = :at, review_notes = :notes, "
                        "updated_at = :now",
                        names={"#st": "status"},
                        values={":status": plan["status"], ":by": actor.get("institutional_email", ""),
                               ":at": now, ":notes": plan["details"].get("notes", "") or plan["details"].get("reason", ""),
                               ":now": now, ":expected": expected_updated_at},
                        condition="updated_at = :expected",
                    )
                    write_items.append(status_update)

                events_needed = 1 + len(plan["extra_events"])
                first_seq = _reserve_seq(request_id, events_needed)
                row = event_row(event_type, plan["details"], first_seq, loan_id)
                write_items.append(_put("trace_events", row))
                for offset, (kind, payload) in enumerate(plan["extra_events"], start=1):
                    write_items.append(_put("trace_events", event_row(kind, payload, first_seq + offset, loan_id)))

                _transact(write_items)
                return True, plan["message"], row
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "TransactionCanceledException" and attempt < 2:
                    continue
                raise
        return False, "La solicitud cambio mientras se procesaba; intenta de nuevo.", None

    REVOKED_SESSIONS_KEY = "revoked_sessions"
    REVOKED_SESSIONS_LIMIT = 1000
    PURGE_NOISE_EVENTS = ("session_login", "session_logout", "session_expired", "nfc_tap")

    def operational_data_counts(self) -> dict:
        requests = self._scan_all("service_requests")
        events = self._scan_all("trace_events")
        reservations = self._scan_all("reservations")
        notifications = self._scan_all("notification_log")
        loans = self._scan_all("loans")
        return {
            "solicitudes": len(requests),
            "trazabilidad": sum(1 for e in events if e.get("request_id")),
            "reservas": len(reservations),
            "correos": len(notifications),
            "sesiones_nfc": sum(1 for e in events if e.get("event_type") in self.PURGE_NOISE_EVENTS),
            "prestamos_cerrados": sum(1 for l in loans if l.get("status") != "out"),
            "prestamos_abiertos": sum(1 for l in loans if l.get("status") == "out"),
        }

    def purge_operational_data(self, actor: dict, requests: bool = True, reservations: bool = True,
                               mail_log: bool = True, session_noise: bool = True,
                               closed_loans: bool = False) -> dict:
        _authorize(actor, "purge_data", "Solo un administrador puede borrar datos operativos.")
        removed = {}
        if requests:
            rows = self._scan_all("service_requests")
            removed["solicitudes"] = len(rows)
            with _table("service_requests").batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key={"id": row["id"]})
            events = self._scan_all("trace_events", Attr("request_id").ne(""))
            removed["trazabilidad"] = len(events)
            with _table("trace_events").batch_writer() as batch:
                for row in events:
                    batch.delete_item(Key={"id": row["id"]})
            with _table("loans").batch_writer() as batch:
                for loan in self._scan_all("loans", Attr("request_id").ne("")):
                    loan["request_id"] = ""
                    batch.put_item(Item=loan)
        if reservations:
            rows = self._scan_all("reservations")
            removed["reservas"] = len(rows)
            with _table("reservations").batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key={"id": row["id"]})
        if mail_log:
            rows = self._scan_all("notification_log")
            removed["correos"] = len(rows)
            with _table("notification_log").batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key={"id": row["id"]})
        if session_noise:
            rows = self._scan_all("trace_events", Attr("event_type").is_in(list(self.PURGE_NOISE_EVENTS)))
            removed["sesiones_nfc"] = len(rows)
            with _table("trace_events").batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key={"id": row["id"]})
        if closed_loans:
            rows = self._scan_all("loans", Attr("status").ne("out"))
            removed["prestamos_cerrados"] = len(rows)
            with _table("loans").batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key={"id": row["id"]})
        if actor is not None:
            self.add_trace_event(_admin_audit_row(actor, "data_purged", {"removed": removed}))
        from core import dispatch, picking
        dispatch.reset_orders_cache()
        picking.reset()
        return removed

    def reset_simulation(self) -> dict:
        if str(self.get_setting("ludica_sandbox", "") or "").strip() != "1":
            raise PermissionError("Esta base no está marcada como simulación de la lúdica: no se reinicia.")
        removed = {}
        for sheet in ("loans", "service_requests", "trace_events", "reservations", "notification_log"):
            rows = self._scan_all(sheet)
            removed[sheet] = len(rows)
            with _table(sheet).batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key={"id": row["id"]})
        history = self._scan_all("item_history")
        kept = [r for r in history if r.get("type") == "Alta"]
        removed["item_history"] = len(history) - len(kept)
        with _table("item_history").batch_writer() as batch:
            for row in history:
                if row.get("type") != "Alta":
                    batch.delete_item(Key={"id": row["id"]})
        _table("settings").delete_item(Key={"key": self.REVOKED_SESSIONS_KEY})
        return removed

    def get_revoked_sessions(self, now: int = None) -> dict:
        moment = int(now if now is not None else time.time())
        try:
            data = json.loads(self.get_setting(self.REVOKED_SESSIONS_KEY, "") or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(sid): int(exp) for sid, exp in data.items()
                if isinstance(exp, (int, float)) and int(exp) > moment}

    def add_revoked_session(self, sid: str, exp: int, now: int = None) -> None:
        sid = str(sid or "").strip()
        if not sid:
            return
        current = self.get_revoked_sessions(now)
        current[sid] = int(exp)
        if len(current) > self.REVOKED_SESSIONS_LIMIT:
            keep = sorted(current, key=current.get, reverse=True)[:self.REVOKED_SESSIONS_LIMIT]
            current = {key: current[key] for key in keep}
        self.set_setting(self.REVOKED_SESSIONS_KEY, json.dumps(current, separators=(",", ":")), "session")

    def revoke_user_sessions(self, user_id: str, actor: dict) -> dict:
        from core import permissions
        target = self.get_user_by_id(user_id)
        if not target:
            raise ValueError("La cuenta no existe.")
        if actor.get("id") != user_id:
            permissions.require_cap(actor, permissions.CAP_MANAGE_USERS)
        epoch = _int(target.get("session_epoch")) + 1
        return self.update_user(user_id, {"session_epoch": epoch})

    # ------------------------------------------------------------------
    # CONFIGURACION DEL LABORATORIO (clave / valor)
    # ------------------------------------------------------------------

    @with_retry
    def get_setting(self, key: str, default=None):
        response = _table("settings").get_item(Key={"key": key})
        item = response.get("Item")
        if not item:
            return default
        value = item.get("value")
        return default if value in (None, "") else value

    def set_setting(self, key: str, value: str, actor_email: str = "") -> None:
        _table("settings").put_item(Item={"key": key, "value": value, "updated_by": actor_email,
                                          "updated_at": _now_str()})

    # ------------------------------------------------------------------
    # TRAZABILIDAD (ver core/traceability.py)
    # ------------------------------------------------------------------

    def add_trace_event(self, event: dict) -> dict:
        payload = dict(event or {})
        payload.setdefault("owner_id", payload.get("user_id", ""))
        payload.setdefault("user_id", payload.get("owner_id", ""))
        request_id = str(payload.get("request_id") or "")
        if request_id and not str(payload.get("seq") or "").strip():
            payload["seq"] = _reserve_seq(request_id)
        row = _trace_event_row(payload)
        _table("trace_events").put_item(Item=row)
        _bump_data_version(read=True)
        return dict(row)

    @with_retry
    def get_trace_events(self, request_id: str = None, item_id: str = None, user_id: str = None,
                         event_type: str = None, loan_id: str = None, receipt: str = None,
                         owner_id: str = None, actor_id: str = None) -> list:
        """Eventos que cumplen TODOS los filtros dados. Usa el GSI mas selectivo
        disponible (request_id > item_id > loan_id > actor_id) para la primera
        lectura y filtra el resto EN MEMORIA sobre ese resultado; sin ninguno de
        esos cuatro cae a un Scan completo de la tabla (poco frecuente: solo
        consultas por `user_id`/`owner_id`/`event_type`/`receipt` solos, que a
        esta escala siguen siendo aceptables). ``actor_id`` es un filtro nuevo
        -- no existia en la version Excel -- agregado porque ya se aprovisiono
        su GSI; no rompe a quien llamaba sin ese argumento."""
        indexed_by = None
        if request_id is not None:
            rows = self._query_gsi("trace_events", "request_id-seq-index", "request_id", request_id)
            indexed_by = "request_id"
        elif item_id is not None:
            rows = self._query_gsi("trace_events", "item_id-created_at-index", "item_id", item_id)
            indexed_by = "item_id"
        elif loan_id is not None:
            rows = self._query_gsi("trace_events", "loan_id-created_at-index", "loan_id", loan_id)
            indexed_by = "loan_id"
        elif actor_id is not None:
            rows = self._query_gsi("trace_events", "actor_id-created_at-index", "actor_id", actor_id)
            indexed_by = "actor_id"
        else:
            rows = self._scan_all("trace_events")

        remaining = {"request_id": request_id, "item_id": item_id, "loan_id": loan_id, "actor_id": actor_id,
                    "user_id": user_id, "owner_id": owner_id, "event_type": event_type, "receipt": receipt}
        remaining.pop(indexed_by, None)
        for column, value in remaining.items():
            if value is not None:
                rows = [r for r in rows if r.get(column) == value]
        events = [_clean(r) for r in rows]
        return sorted(events, key=lambda row: (str(row.get("created_at") or ""), _int(row.get("seq"))))

    def get_health(self) -> dict:
        tables = ("items", "users", "loans", "reservations", "service_requests", "item_history",
                 "settings", "trace_events", "notification_log", "item_photos")
        rows = {name: len(self._scan_all(name)) for name in tables}
        return {"rows": rows, "write": write_stats(), "sync": get_sync_status()}

    def get_events_index(self) -> dict:
        """``{request_id: [eventos]}`` de TODAS las solicitudes, cacheado por
        version EN ESTE PROCESO (ver nota de alcance al inicio del archivo:
        no es seguro entre instancias de ECS hasta migrar tambien picking_state)."""
        version = data_state()["version"]
        if getattr(self, "_events_index_cache", None) and self._events_index_cache[0] == version:
            return self._events_index_cache[1]
        index = {}
        for event in self.get_trace_events():
            index.setdefault(event.get("request_id") or "", []).append(event)
        self._events_index_cache = (version, index)
        return index

    # ------------------------------------------------------------------
    # REGISTRO DE CORREOS AUTOMATICOS
    # ------------------------------------------------------------------

    def add_notification_log(self, entry: dict) -> dict:
        columns = SHEET_COLUMNS["notification_log"]
        row = {column: "" for column in columns}
        row.update({key: "" if value is None else str(value) for key, value in entry.items() if key in columns})
        row["id"] = _new_id()
        row["created_at"] = row["created_at"] or _now_str()
        row["log_bucket"] = "LOG"
        _table("notification_log").put_item(Item=row)
        self._trim_notification_log()
        return dict(row)

    def _trim_notification_log(self) -> None:
        rows = self._query_gsi("notification_log", "log_bucket-created_at-index", "log_bucket", "LOG")
        excess = len(rows) - NOTIFICATION_LOG_LIMIT
        if excess <= 0:
            return
        rows.sort(key=lambda r: r.get("created_at") or "")
        with _table("notification_log").batch_writer() as batch:
            for row in rows[:excess]:
                batch.delete_item(Key={"id": row["id"]})

    @with_retry
    def get_notification_log(self, limit: int = 50) -> list:
        rows = self._query_gsi("notification_log", "log_bucket-created_at-index", "log_bucket", "LOG")
        rows = [_clean(r) for r in rows]
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return rows[:limit] if limit else rows

    # ------------------------------------------------------------------
    # ALTA DE VARIOS ITEMS NUEVOS EN UNA SOLA ESCRITURA
    # ------------------------------------------------------------------

    @with_retry
    def save_items_bulk(self, items: list, actor_email: str = "", details: str = None,
                        actor: dict = None) -> list:
        items = list(items or [])
        only_locations = bool(items) and all(row.get("item_type") == LOCATION_TYPE for row in items)
        _authorize(actor, "manage_locations" if only_locations else "edit_inventory",
                   "No tienes permiso para crear ubicaciones." if only_locations
                   else "No tienes permiso para crear ítems.")
        if not items:
            return []

        def whole(value, field):
            raw = "" if value is None else str(value).strip()
            if raw.lower() in ("", "nan", "none"):
                return 0
            try:
                number = float(raw)
            except ValueError:
                raise ValueError(f"{field} debe ser un numero entero.") from None
            if not number.is_integer() or number < 0:
                raise ValueError(f"{field} debe ser un numero entero mayor o igual a 0.")
            return int(number)

        rows, errors, reused, batch_ids, batch_lookup = [], [], [], set(), {}
        for position, raw in enumerate(items, start=1):
            custom_id = str(raw.get("id", "") or "").strip()
            name = str(raw.get("name", "") or "").strip()
            label = f"Fila {position} ({custom_id or 'sin codigo'})"
            if not custom_id or not name:
                errors.append(f"{label}: codigo y nombre son obligatorios.")
                continue
            if custom_id in batch_ids:
                errors.append(f"{label}: el codigo esta repetido en la misma tanda.")
                continue
            existing = self.get_item(custom_id)
            if existing and existing.get("status") != "retired":
                errors.append(f"{label}: Ya existe un item con ese codigo.")
                continue

            item_type = str(raw.get("item_type", "standalone") or "standalone").strip()
            parent_id = str(raw.get("parent_id", "") or "").strip()
            lookup = (lambda pid: batch_lookup.get(pid) or self.get_item(pid))
            try:
                quantity = whole(raw.get("quantity", 0), "Cantidad")
                min_alert = whole(raw.get("min_stock_alert", 0), "Umbral de alerta")
                if item_type == LOCATION_TYPE:
                    quantity = min_alert = 0
                _validate_item_data({"item_type": item_type, "parent_id": parent_id}, lookup, custom_id, is_new=True)
                if parent_id and parent_id not in batch_ids:
                    parent = self.get_item(parent_id)
                    if parent and parent.get("status") == "retired":
                        raise ValueError(f"El Contenedor Principal '{parent_id}' esta dado de baja.")
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
                continue

            batch_ids.add(custom_id)
            if existing:
                reused.append(custom_id)
            row = {
                "id": custom_id, "name": name, "category": str(raw.get("category", "") or ""),
                "description": str(raw.get("description", "") or ""), "item_type": item_type,
                "parent_id": parent_id, "unit": str(raw.get("unit", "") or "").strip() or "unidad",
                "quantity": quantity, "location": str(raw.get("location", "") or ""),
                "min_stock_alert": min_alert, "status": "active", "created_by": raw.get("created_by") or actor_email,
                "updated_at": _now_str(), "on_loan": 0,
            }
            batch_lookup[custom_id] = row
            rows.append(row)

        if errors:
            raise ValueError("No se creo ningun item. " + " ".join(errors))

        for custom_id in reused:
            self._purge_item_records(self._cascade_ids(custom_id), actor_email)

        now = _now_str()
        history = [{
            "id": _new_id(), "item_id": row["id"], "timestamp": now, "type": "Alta",
            "quantity_change": row["quantity"], "actor_user_id": actor_email,
            "details": raw.get("details") or details or "Item creado en el sistema.",
        } for row, raw in zip(rows, items) if row["id"] == raw.get("id", "").strip()]
        with _table("items").batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=row)
        with _table("item_history").batch_writer() as batch:
            for row in history:
                batch.put_item(Item=row)
        logger.info(f"{len(rows)} item(s) creados en una sola escritura por {actor_email or 'sistema'}.")
        return [row["id"] for row in rows]

    # ------------------------------------------------------------------
    # FOTOS DE LOS OBJETOS (indice; los JPEG viven en S3, ver core/photos.py)
    # ------------------------------------------------------------------

    def add_item_photo(self, photo: dict, trace_event: dict = None) -> dict:
        columns = SHEET_COLUMNS["item_photos"]
        row = {column: "" for column in columns}
        row.update({key: "" if value is None else str(value) for key, value in photo.items() if key in columns})
        row["id"] = row["id"] or _new_id()
        row["taken_at"] = row["taken_at"] or _now_str()
        if not row["item_id"] or not row["path"]:
            raise ValueError("La foto necesita el codigo del item y la ruta del archivo.")
        if not self.get_item(row["item_id"]):
            raise ValueError("El item no existe.")
        transact_items = [_put("item_photos", row)]
        if trace_event:
            transact_items.append(_put("trace_events", _trace_event_row(trace_event)))
        _transact(transact_items)
        return dict(row)

    @with_retry
    def get_item_photos(self, item_id: str) -> list:
        rows = self._query_gsi("item_photos", "item_id-taken_at-index", "item_id", item_id)
        rows = [_clean(r) for r in rows]
        return sorted(rows, key=lambda row: row.get("taken_at") or "", reverse=True)

    @with_retry
    def get_item_photo(self, photo_id: str):
        response = _table("item_photos").get_item(Key={"id": photo_id})
        item = response.get("Item")
        return _clean(item) if item else None

    def get_photo_counts(self) -> dict:
        counts: dict = {}
        for row in self._scan_all("item_photos"):
            item_id = str(row.get("item_id") or "")
            counts[item_id] = counts.get(item_id, 0) + 1
        return counts

    def delete_item_photo(self, photo_id: str, trace_event: dict = None):
        removed = self.get_item_photo(photo_id)
        if not removed:
            return None
        transact_items = [_delete("item_photos", {"id": photo_id})]
        if trace_event:
            transact_items.append(_put("trace_events", _trace_event_row(trace_event)))
        _transact(transact_items)
        return removed
