# -*- coding: utf-8 -*-
"""v1.16 · la lista blanca y la resolución de conflictos de sincronización exigen la capacidad
de administración en la capa de datos y dejan constancia de quién las hizo."""

import json

import pytest
from test_sync import FakeGitHub

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from core import permissions, traceability  # noqa: E402
from core import storage as sm  # noqa: E402
from core.storage import LabStorage  # noqa: E402

ROLES = ("estudiante", "operario", "encargado", "profesor", "maestro")


@pytest.fixture
def lab():
    storage = LabStorage()
    people = {role: storage.create_user(f"Persona {role}", f"{role}@uniminuto.edu.co", "x", role) for role in ROLES}
    return storage, people


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(sm.requests, "request", fake.request)
    monkeypatch.setattr(sm.time, "sleep", lambda _s: None)
    secrets = {"GITHUB_TOKEN": "token-de-prueba", "GITHUB_REPO": "org/repo-de-prueba"}
    monkeypatch.setattr(sm, "safe_secret", lambda key, default=None: secrets.get(key, default))
    return fake


def _events(storage, kind):
    return [event for event in storage.get_trace_events() if event.get("event_type") == kind]


def _details(event):
    return json.loads(event.get("details") or "{}")


# --- capacidades ------------------------------------------------------------------------------

def test_only_the_administrator_holds_the_new_capability():
    assert permissions.has_cap({"role": "maestro"}, permissions.CAP_RESOLVE_SYNC)
    for role in ("estudiante", "operario", "encargado", "profesor"):
        assert not permissions.has_cap({"role": role}, permissions.CAP_RESOLVE_SYNC)


def test_new_audit_events_are_registered_with_labels():
    for kind in (traceability.EVENT_WHITELIST_CHANGED, traceability.EVENT_SYNC_CONFLICT_RESOLVED):
        assert kind in traceability.EVENT_TYPES and kind in traceability.EVENT_LABELS


# --- lista blanca -------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["estudiante", "operario", "encargado", "profesor"])
def test_other_roles_cannot_change_the_whitelist(lab, role):
    storage, people = lab
    with pytest.raises(PermissionError):
        storage.add_to_whitelist("nuevo@uniminuto.edu.co", actor=people[role])
    assert storage.get_whitelist() == []
    storage.add_to_whitelist("existente@uniminuto.edu.co")                   # llamador interno de confianza
    with pytest.raises(PermissionError):
        storage.remove_from_whitelist("existente@uniminuto.edu.co", actor=people[role])
    assert storage.get_whitelist() == ["existente@uniminuto.edu.co"]
    assert _events(storage, "whitelist_changed") == []


def test_administrator_changes_to_the_whitelist_are_audited(lab):
    storage, people = lab
    admin = people["maestro"]
    storage.add_to_whitelist("  Nuevo@UniMinuto.edu.co ", actor=admin)
    storage.add_to_whitelist("nuevo@uniminuto.edu.co", actor=admin)           # repetido: sin cambio ni constancia
    storage.remove_from_whitelist("nuevo@uniminuto.edu.co", actor=admin)
    storage.remove_from_whitelist("nuevo@uniminuto.edu.co", actor=admin)      # ya no estaba: sin constancia
    events = _events(storage, "whitelist_changed")
    assert [_details(event)["action"] for event in events] == ["added", "removed"]
    assert all(event["actor_id"] == admin["id"] and event["actor_role"] == "maestro" for event in events)
    assert _details(events[0])["email"] == "nuevo@uniminuto.edu.co"
    assert storage.get_whitelist() == []


# --- conflicto de sincronización -------------------------------------------------------------------

def _make_conflict(storage, github):
    storage.save_item({"name": "A", "item_type": "standalone", "quantity": 1}, "LAB-1", is_new=True)
    github.set_external(b"OTRA VERSION")
    storage.save_item({"name": "B", "item_type": "standalone", "quantity": 1}, "LAB-2", is_new=True)
    assert sm.get_sync_status()["conflict"] is True


@pytest.mark.parametrize("role", ["estudiante", "operario", "encargado", "profesor"])
def test_other_roles_cannot_resolve_a_sync_conflict(lab, github, role):
    storage, people = lab
    _make_conflict(storage, github)
    for keep in ("local", "remote"):
        with pytest.raises(PermissionError):
            storage.resolve_sync_conflict(keep, actor=people[role])
    assert sm.get_sync_status()["conflict"] is True and github.content == b"OTRA VERSION"
    assert _events(storage, "sync_conflict_resolved") == []


def test_administrator_resolves_keeping_local_and_it_is_recorded(lab, github):
    storage, people = lab
    _make_conflict(storage, github)
    ok, _ = storage.resolve_sync_conflict("local", actor=people["maestro"])
    assert ok and sm.get_sync_status()["conflict"] is False
    events = _events(storage, "sync_conflict_resolved")
    assert len(events) == 1 and _details(events[0]) == {"kept": "local"}
    assert events[0]["actor_id"] == people["maestro"]["id"]
    assert github.content == open(sm.EXCEL_PATH, "rb").read()                # y la constancia también se publicó


def test_administrator_resolves_keeping_remote_and_it_is_recorded(lab, github):
    storage, people = lab
    storage.save_item({"name": "A", "item_type": "standalone", "quantity": 1}, "LAB-1", is_new=True)
    good_remote = github.content
    storage.save_item({"name": "B", "item_type": "standalone", "quantity": 1}, "LAB-2", is_new=True)
    github.set_external(good_remote)              # alguien restaura una versión anterior válida
    storage.save_item({"name": "C", "item_type": "standalone", "quantity": 1}, "LAB-3", is_new=True)
    assert sm.get_sync_status()["conflict"] is True

    ok, _ = storage.resolve_sync_conflict("remote", actor=people["maestro"])
    assert ok and storage.get_item("LAB-3") is None
    assert [_details(event) for event in _events(storage, "sync_conflict_resolved")] == [{"kept": "remote"}]


def test_a_failed_resolution_leaves_no_record_and_an_invalid_choice_is_rejected(lab, github):
    storage, people = lab
    _make_conflict(storage, github)
    github.set_external(b"roto")                   # descarga inválida: «remote» se rechaza
    ok, _ = storage.resolve_sync_conflict("remote", actor=people["maestro"])
    assert ok is False and _events(storage, "sync_conflict_resolved") == []
    with pytest.raises(ValueError):
        storage.resolve_sync_conflict("ambas", actor=people["maestro"])
    with pytest.raises(PermissionError):           # la capacidad se verifica antes que nada
        storage.resolve_sync_conflict("local", actor=people["profesor"])


# --- la interfaz -------------------------------------------------------------------------------------

def _banner_script():
    import streamlit as st

    from core import ui

    class Storage:
        def get_sync_status(self):
            return {"configured": True, "ok": False, "conflict": True, "message": "Hay un conflicto.",
                    "repo": "org/repo", "db_path": "base.xlsx"}

        def resolve_sync_conflict(self, keep, actor=None):
            st.session_state.setdefault("calls", []).append((keep, (actor or {}).get("id")))
            return True, "Listo."

    ui.sync_status_banner(Storage(), st.session_state["current"])


def _banner(role):
    at = AppTest.from_function(_banner_script, default_timeout=30)
    at.session_state["current"] = {"id": f"id-{role}", "role": role, "full_name": role}
    return at.run()


def test_banner_offers_no_controls_to_a_supervisor():
    at = _banner("profesor")
    assert any("Pide a un administrador" in info.value for info in at.info)
    assert not at.checkbox and not at.button


def test_administrator_must_confirm_before_choosing_a_version():
    at = _banner("maestro")
    assert len(at.checkbox) == 1 and len(at.button) == 2
    assert all(button.disabled for button in at.button)
    at.checkbox[0].check().run()
    assert not any(button.disabled for button in at.button)
    [button for button in at.button if button.key == "sync_keep_local"][0].click().run()
    assert at.session_state["calls"] == [("local", "id-maestro")]
