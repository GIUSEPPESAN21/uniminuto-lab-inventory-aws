# -*- coding: utf-8 -*-
"""Roles técnicos, etiquetas visibles y autorización por capacidades.

Las etiquetas se pueden renombrar sin alterar las reglas. Las vistas ocultan
acciones no disponibles, pero cada función de dominio vuelve a validar la
capacidad correspondiente.
"""

ROLE_STUDENT = "estudiante"
ROLE_OPERATOR = "operario"
ROLE_ATTENDANT = "encargado"
ROLE_PROFESSOR = "profesor"
ROLE_MASTER = "maestro"
ROLES = (ROLE_STUDENT, ROLE_OPERATOR, ROLE_ATTENDANT, ROLE_PROFESSOR, ROLE_MASTER)

# Única fuente de las etiquetas visibles; los ids técnicos de arriba no cambian.
# Cada etiqueta dice el cargo en la vida real del laboratorio entre paréntesis, así
# «Supervisor» no se confunde entre quien encarga el laboratorio y quien es profesor.
ROLE_LABELS = {
    ROLE_STUDENT: "Estudiante (Cliente)",
    ROLE_OPERATOR: "Operario (Auxiliar de laboratorio)",
    ROLE_ATTENDANT: "Supervisor (Encargado del laboratorio)",
    ROLE_PROFESSOR: "Supervisor (Profesor)",
    ROLE_MASTER: "Administrador",
}
# Etiqueta corta para frases del tipo «Nombre (cargo)», donde un segundo paréntesis estorba.
ROLE_SHORT_LABELS = {
    ROLE_STUDENT: "Cliente", ROLE_OPERATOR: "Operario", ROLE_ATTENDANT: "Supervisor",
    ROLE_PROFESSOR: "Supervisor", ROLE_MASTER: "Administrador",
}

CAP_REQUEST = "request"
CAP_VIEW_OWN_REQUESTS = "view_own_requests"
CAP_CANCEL_OWN_REQUEST = "cancel_own_request"
CAP_CONFIRM_RECEIPT = "confirm_receipt"
CAP_RESERVE = "reserve"
CAP_CHECKOUT = "checkout"
CAP_VIEW_ALL_LOANS = "view_all_loans"
CAP_CHECKIN_ANY = "checkin_any_loan"
CAP_APPROVE = "approve"
CAP_AUTHORIZE_DISPATCH = "authorize_dispatch"
CAP_VIEW_DISPATCH_QUEUE = "view_dispatch_queue"
CAP_CLAIM = "claim"
CAP_RELEASE = "release"
CAP_PICK = "pick"
CAP_RECEIVE_AND_DELIVER = "receive_and_deliver"
CAP_VERIFY_RETURN = "verify_return"
CAP_STORE_RETURN = "store_return"
CAP_REASSIGN = "reassign"
CAP_VIEW_ALL_TRACES = "view_all_traces"
CAP_EDIT_INVENTORY = "edit_inventory"
CAP_MANAGE_USERS = "manage_users"
CAP_MANAGE_NFC = "manage_nfc"
CAP_MANAGE_LOCATIONS = "manage_locations"
CAP_REPORTS = "reports"
CAP_DISPATCH_REPORTS = "dispatch_reports"
CAP_DISPATCH_SETTINGS = "dispatch_settings"
CAP_RESOLVE_SYNC = "resolve_sync"
CAP_PURGE_DATA = "purge_data"
CAP_MANAGE_EMAIL = "manage_email"
CAP_ADJUST_COUNTS = "adjust_counts"
CAP_MANAGE_RESERVATIONS = "manage_reservations"
CAP_DELETE_PHOTOS = "delete_photos"

_CLIENT = {
    CAP_REQUEST, CAP_VIEW_OWN_REQUESTS, CAP_CANCEL_OWN_REQUEST,
    CAP_CONFIRM_RECEIPT, CAP_RESERVE, CAP_CHECKOUT,
}
# Los operarios (auxiliares) guardan en el estante lo que el cliente devolvió y el supervisor verificó.
_DISPATCH = {CAP_VIEW_DISPATCH_QUEUE, CAP_CLAIM, CAP_RELEASE, CAP_PICK, CAP_STORE_RETURN}
_ATTEND = {
    CAP_APPROVE, CAP_AUTHORIZE_DISPATCH, CAP_RECEIVE_AND_DELIVER,
    CAP_REASSIGN, CAP_RELEASE, CAP_VIEW_ALL_TRACES, CAP_DISPATCH_REPORTS,
    # Los supervisores también toman pedidos y hacen picking (como el auxiliar de laboratorio).
    CAP_VIEW_DISPATCH_QUEUE, CAP_CLAIM, CAP_PICK, CAP_STORE_RETURN,
    # El supervisor verifica lo que el cliente devuelve (estado y cantidad) antes de guardarlo.
    CAP_VERIFY_RETURN,
}
_PROFESSOR = {
    CAP_EDIT_INVENTORY, CAP_MANAGE_NFC, CAP_MANAGE_LOCATIONS,
    CAP_REPORTS, CAP_MANAGE_RESERVATIONS,
    CAP_VIEW_ALL_LOANS, CAP_CHECKIN_ANY,
}
_ADMIN = {
    CAP_MANAGE_USERS, CAP_MANAGE_EMAIL, CAP_ADJUST_COUNTS, CAP_DELETE_PHOTOS, CAP_DISPATCH_SETTINGS,
    CAP_RESOLVE_SYNC, CAP_PURGE_DATA,
}

ROLE_CAPS = {
    ROLE_STUDENT: frozenset(_CLIENT),
    ROLE_OPERATOR: frozenset(_DISPATCH),
    ROLE_ATTENDANT: frozenset(_CLIENT | _ATTEND),
    ROLE_PROFESSOR: frozenset(_CLIENT | _ATTEND | _PROFESSOR),
    ROLE_MASTER: frozenset(_CLIENT | _DISPATCH | _ATTEND | _PROFESSOR | _ADMIN),
}

# Compatibilidad para módulos externos; el código nuevo usa capacidades.
MANAGER_ROLES = (ROLE_ATTENDANT, ROLE_PROFESSOR, ROLE_MASTER)
ADMIN_ROLES = (ROLE_MASTER,)


def roles_with(capability: str) -> tuple:
    """Roles técnicos que tienen ``capability`` (p. ej. quién puede recibir un pedido reasignado)."""
    return tuple(role for role in ROLES if capability in ROLE_CAPS.get(role, ()))


def has_role(user: dict, roles) -> bool:
    return bool(user) and user.get("role") in roles


def has_cap(user: dict, capability: str) -> bool:
    """True si el rol técnico actual concede ``capability``."""
    return bool(user) and capability in ROLE_CAPS.get(user.get("role"), ())


def require_cap(user: dict, capability: str, message: str = "No tienes permiso para esta acción.") -> None:
    """Autorización de dominio; nunca debe sustituirse por ocultar un botón."""
    if not has_cap(user, capability):
        raise PermissionError(message)


def role_detail_label(role_or_user) -> str:
    """Etiqueta para quien asigna roles. Las etiquetas ya son únicas; se conserva por compatibilidad."""
    return role_label(role_or_user)


def role_label(role_or_user) -> str:
    role = role_or_user.get("role", "") if isinstance(role_or_user, dict) else str(role_or_user or "")
    return ROLE_LABELS.get(role, role or "Sin rol")


def role_short_label(role_or_user) -> str:
    """«Cliente», «Operario», «Supervisor» o «Administrador», para ir entre paréntesis."""
    role = role_or_user.get("role", "") if isinstance(role_or_user, dict) else str(role_or_user or "")
    return ROLE_SHORT_LABELS.get(role, role or "Sin rol")
