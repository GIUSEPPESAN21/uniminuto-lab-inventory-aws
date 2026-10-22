# -*- coding: utf-8 -*-
"""Fixtures compartidas: un FakeStorage en memoria que cumple el mismo
'contrato' publico que core.storage.LabStorage, sin tocar pandas/Excel/GitHub,
para poder probar la logica de negocio de core/*.py de forma aislada y rapida.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Aísla TODA prueba de la red real.

    - `safe_secret` devuelve siempre el valor por defecto: un
      `.streamlit/secrets.toml` real en la máquina del desarrollador nunca
      activa el envío de correos ni apunta a un bucket S3 real.
    - Se reinician las cachés en memoria de picking/despacho.

    NOTA DE ALCANCE (migracion AWS): antes esta fixture tambien aislaba cada
    prueba del Excel real (EXCEL_PATH en `tmp_path`, caché en memoria, hilo de
    sincronizacion a GitHub) porque `core.storage.LabStorage` escribia a disco.
    Esa capa ya no existe: `LabStorage` ahora habla con DynamoDB real via
    boto3, y esta fixture NO lo mockea todavia. Las pruebas que usan la
    fixture `storage` (`FakeStorage`, mas abajo) siguen aisladas y deberian
    seguir pasando sin cambios; las que instancian `LabStorage()` directamente
    necesitan un mock de DynamoDB (p. ej. con `moto`) antes de poder correr
    sin credenciales de AWS reales -- ese trabajo queda pendiente, es un
    paquete separado y mas grande que esta migracion de storage.py."""
    from core import auth, dispatch, live_data, notifications, picking, session
    from core import storage as storage_module

    def no_secret(key, default=None):
        return default

    for module in (storage_module, auth, notifications, dispatch, session, live_data):
        monkeypatch.setattr(module, "safe_secret", no_secret)
    auth.reset_login_throttle()
    picking.reset()
    dispatch.reset_orders_cache()
    yield
    picking.reset()
    dispatch.reset_orders_cache()


class FakeStorage:
    def __init__(self):
        self.items = {}
        self.users = {}
        self.whitelist = set()
        self.loans = {}
        self.reservations = {}
        self.service_requests = {}
        self.settings = {}
        self.trace_events = []
        self.history = []

    # --- items ---
    def add_item(self, item_id, **kwargs):
        base = {
            "id": item_id, "name": "Item", "category": "", "description": "",
            "item_type": "standalone", "parent_id": "", "unit": "unidad",
            "quantity": 0, "location": "", "min_stock_alert": 0, "status": "active",
            "created_by": "", "updated_at": "",
        }
        base.update(kwargs)
        self.items[item_id] = base
        return base

    def get_item(self, item_id):
        return dict(self.items[item_id]) if item_id in self.items else None

    def get_children(self, parent_id):
        return [dict(i) for i in self.items.values() if i.get("parent_id") == parent_id and i.get("status") != "retired"]

    def get_all_masters(self):
        return [dict(i) for i in self.items.values() if i.get("item_type") == "master" and i.get("status") != "retired"]

    def get_available_quantity(self, item_id):
        item = self.get_item(item_id)
        if not item:
            return 0
        borrowed = sum(l["quantity"] for l in self.loans.values() if l["item_id"] == item_id and l["status"] == "out")
        return max(item.get("quantity", 0) - borrowed, 0)

    # --- users ---
    def add_user(self, email, **kwargs):
        base = {
            "id": uuid.uuid4().hex[:10], "full_name": "Usuario", "student_id": "",
            "institutional_email": email.lower(),
            "password_hash": "", "role": "estudiante", "program_or_department": "",
            "status": "active", "created_at": "",
        }
        base.update(kwargs)
        self.users[base["id"]] = base
        return base

    def get_user_by_email(self, email):
        for u in self.users.values():
            if u["institutional_email"] == email.lower().strip():
                return dict(u)
        return None

    def get_user_by_id(self, user_id):
        u = self.users.get(user_id)
        return dict(u) if u else None

    def create_user(self, full_name, email, password_hash, role, program="", student_id="", status="active"):
        return self.add_user(
            email, full_name=full_name, password_hash=password_hash, role=role,
            program_or_department=program, student_id=student_id, status=status,
        )

    def update_user(self, user_id, changes, actor=None):
        self.users[user_id].update(changes)
        return dict(self.users[user_id])

    def revoke_user_sessions(self, user_id, actor):
        user = self.users[user_id]
        user["session_epoch"] = int(user.get("session_epoch") or 0) + 1
        return dict(user)

    def get_all_users(self):
        return [dict(user) for user in self.users.values()]

    def count_masters(self):
        return len([u for u in self.users.values() if u["role"] == "maestro"])

    # --- whitelist ---
    def is_email_whitelisted_professor(self, email):
        return email.lower().strip() in self.whitelist

    def add_to_whitelist(self, email, actor=None):
        self.whitelist.add(email.lower().strip())

    def get_availability_map(self):
        return {item_id: self.get_available_quantity(item_id) for item_id in self.items}

    # --- loans ---
    def create_loan(self, item, quantity, user, expected_return_at=None, notes="", request_id="",
                    operator=None):
        available = self.get_available_quantity(item["id"]) if item["id"] in self.items else None
        if available is not None and quantity > available:
            raise ValueError(f"Stock insuficiente. Disponible: {available}.")
        loan_id = uuid.uuid4().hex[:10]
        loan = {
            "id": loan_id, "item_id": item["id"], "item_name": item.get("name", ""),
            "parent_id": item.get("parent_id", ""), "quantity": quantity,
            "user_id": user["id"], "user_name": user.get("full_name", ""),
            "user_role": user.get("role", ""), "checkout_at": datetime.now(timezone.utc),
            "expected_return_at": expected_return_at, "return_at": None,
            "status": "out", "notes": notes, "request_id": request_id,
            "operator_id": (operator or {}).get("id", ""),
            "operator_name": (operator or {}).get("full_name", ""),
        }
        self.loans[loan_id] = loan
        return loan

    def return_loan(self, loan_id, actor_email="", actor=None, quantity=None, note=""):
        loan = self.loans.get(loan_id)
        if not loan:
            return False, "El prestamo no existe."
        if actor is not None:
            from core import permissions
            if not permissions.has_cap(actor, permissions.CAP_CHECKIN_ANY):
                raise PermissionError("No puedes reingresar un préstamo directamente.")
        if loan["status"] == "returned":
            return False, "Este prestamo ya fue devuelto."
        qty = loan["quantity"] if quantity is None else int(quantity)
        if qty <= 0 or qty > loan["quantity"]:
            return False, f"La cantidad devuelta debe estar entre 1 y {loan['quantity']}."
        if qty < loan["quantity"]:
            loan["quantity"] -= qty
            return True, f"Reingreso parcial: {qty}."
        loan["status"] = "returned"
        loan["return_at"] = datetime.now(timezone.utc)
        return True, "Reingreso registrado correctamente."

    def get_open_loans_for_item(self, item_id):
        return [dict(l) for l in self.loans.values() if l["item_id"] == item_id and l["status"] == "out"]

    def get_open_loans_for_user(self, user_id):
        return [dict(l) for l in self.loans.values() if l["user_id"] == user_id and l["status"] == "out"]

    def get_all_loans(self, status=None):
        loans = list(self.loans.values())
        if status:
            loans = [l for l in loans if l["status"] == status]
        return [dict(l) for l in loans]


    # --- reservations ---
    def create_reservation(self, data, user):
        row = {
            **data, "id": uuid.uuid4().hex[:10], "requester_id": user["id"],
            "requester_name": user.get("full_name", ""),
            "requester_email": user.get("institutional_email", ""),
            "status": "pending", "reviewed_by": "", "review_notes": "",
            "email_notified": False, "email_error": "",
        }
        self.reservations[row["id"]] = row
        return dict(row)

    def get_reservation(self, reservation_id):
        row = self.reservations.get(reservation_id)
        return dict(row) if row else None

    def get_reservations(self, status=None, user_id=None):
        rows = list(self.reservations.values())
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if user_id:
            rows = [row for row in rows if row.get("requester_id") == user_id]
        return [dict(row) for row in rows]

    def update_reservation_status(self, reservation_id, status, reviewer_email, notes=""):
        row = self.reservations.get(reservation_id)
        if not row:
            return False
        row.update(status=status, reviewed_by=reviewer_email, review_notes=notes)
        return True

    def update_reservation_notification(self, reservation_id, sent, error=""):
        row = self.reservations.get(reservation_id)
        if not row:
            return False
        row.update(email_notified=bool(sent), email_error=error)
        return True

    # --- service requests ---
    def create_service_request(self, data, user):
        return self.create_service_requests([data], user)[0]

    def create_service_requests(self, rows, user, order_id=""):
        created = []
        for data in rows:
            row = {
                **data, "id": uuid.uuid4().hex[:10], "requester_id": user["id"],
                "requester_name": user.get("full_name", ""),
                "requester_email": user.get("institutional_email", ""),
                "status": "pending", "reviewed_by": "", "review_notes": "",
                "email_notified": False, "email_error": "", "order_id": order_id or data.get("order_id", ""),
            }
            self.service_requests[row["id"]] = row
            created.append(dict(row))
        return created

    def get_service_request(self, request_id):
        row = self.service_requests.get(request_id)
        return dict(row) if row else None

    def get_service_requests(self, status=None, user_id=None, exclude_ids=None):
        rows = list(self.service_requests.values())
        if exclude_ids:
            rows = [row for row in rows if row.get("id") not in set(exclude_ids)]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if user_id:
            rows = [row for row in rows if row.get("requester_id") == user_id]
        return [dict(row) for row in rows]

    def update_service_request_status(self, request_id, status, reviewer_email, notes=""):
        row = self.service_requests.get(request_id)
        if not row:
            return False
        row.update(status=status, reviewed_by=reviewer_email, review_notes=notes)
        return True

    def update_service_request_notification(self, request_id, sent, error=""):
        row = self.service_requests.get(request_id)
        if not row:
            return False
        row.update(email_notified=bool(sent), email_error=error)
        return True

    # --- settings ---
    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value, actor_email=""):
        self.settings[key] = value

    def get_revoked_sessions(self, now=None):
        import time as _time

        moment = int(now if now is not None else _time.time())
        return {sid: exp for sid, exp in getattr(self, "revoked_sessions", {}).items() if exp > moment}

    def add_revoked_session(self, sid, exp, now=None):
        self.__dict__.setdefault("revoked_sessions", {})[str(sid)] = int(exp)

    # --- trazabilidad ---
    def add_trace_event(self, event):
        columns = (
            "event_type", "request_id", "seq", "item_id", "loan_id", "user_id", "owner_id",
            "actor_id", "actor_name", "actor_email", "actor_role", "receipt", "details",
            "created_at",
        )
        row = {column: "" for column in columns}
        row.update({key: value for key, value in event.items() if key in columns})
        row["owner_id"] = row["owner_id"] or row["user_id"]
        if row["request_id"] and not row["seq"]:
            row["seq"] = 1 + max((int(e.get("seq") or 0) for e in self.trace_events
                                  if e.get("request_id") == row["request_id"]), default=0)
        row["id"] = uuid.uuid4().hex[:10]
        row["created_at"] = row["created_at"] or datetime.now(timezone.utc).isoformat()
        self.trace_events.append(row)
        return dict(row)

    def get_trace_events(self, request_id=None, item_id=None, user_id=None, event_type=None,
                         loan_id=None, receipt=None, owner_id=None):
        filters = {"request_id": request_id, "item_id": item_id, "user_id": user_id,
                   "event_type": event_type, "loan_id": loan_id, "receipt": receipt,
                   "owner_id": owner_id}
        rows = [
            dict(row) for row in self.trace_events
            if all(value is None or row.get(key) == value for key, value in filters.items())
        ]
        return sorted(rows, key=lambda row: (row.get("created_at") or "", int(row.get("seq") or 0)))

    def transition_request(self, request_id, event_type, actor, details=None):
        """Mismas reglas que LabStorage: `core.dispatch.plan_transition`."""
        from core import dispatch

        actor = dict(actor or {})
        request = self.get_service_request(request_id)
        if not request:
            return False, "La solicitud no existe.", None
        item_id = request.get("item_id") or ""
        open_loan = next((dict(loan) for loan in self.loans.values()
                          if loan.get("request_id") == request_id and loan["status"] == "out"), None)
        available = self.get_available_quantity(item_id) if item_id in self.items else None
        plan = dispatch.plan_transition(
            request, self.get_trace_events(request_id=request_id), actor, event_type, details,
            available=available, open_loan=open_loan,
        )

        def add(kind, payload, loan_id=""):
            return self.add_trace_event({
                "event_type": kind, "request_id": request_id, "item_id": item_id,
                "loan_id": loan_id, "user_id": request.get("requester_id", ""),
                "actor_id": actor.get("id", ""), "actor_name": actor.get("full_name", ""),
                "actor_email": actor.get("institutional_email", ""),
                "actor_role": actor.get("role", ""),
                "details": json.dumps(payload, ensure_ascii=False, default=str),
            })

        if not plan["ok"]:
            return False, plan["message"], add(plan["failure_kind"], plan["failure_details"])
        loan_id = plan.get("loan_id", "")
        if plan["loan_action"] == "create":
            requester = self.users.get(request.get("requester_id"), {})
            loan = self.create_loan(
                self.items[item_id], int(request.get("quantity") or 0),
                {"id": request.get("requester_id"), "full_name": request.get("requester_name", ""),
                 "role": requester.get("role", "")},
                notes=f"Picking de la solicitud {request_id}", request_id=request_id,
                operator=actor,
            )
            loan_id = loan["id"]
        elif plan["loan_action"] == "reuse":
            self.loans[loan_id].update(operator_id=actor.get("id", ""),
                                       operator_name=actor.get("full_name", ""))
        elif plan["loan_action"] == "close":
            self.return_loan(loan_id)
        if plan["status"]:
            self.service_requests[request_id].update(
                status=plan["status"], reviewed_by=actor.get("institutional_email", ""),
                review_notes=plan["details"].get("notes", "") or plan["details"].get("reason", ""),
            )
        row = add(event_type, plan["details"], loan_id)
        for kind, payload in plan["extra_events"]:
            add(kind, payload, loan_id)
        return True, plan["message"], row

    def get_item_history(self, item_id):
        return [dict(row) for row in self.history if row.get("item_id") == item_id]


@pytest.fixture
def storage():
    return FakeStorage()
