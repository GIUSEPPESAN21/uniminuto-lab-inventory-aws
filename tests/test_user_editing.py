# -*- coding: utf-8 -*-
import pytest

from core import auth


def test_validate_profile_update_normalizes_editable_fields(storage):
    user = storage.add_user("old@uniminuto.edu.co")
    changes, error = auth.validate_profile_update(
        storage, user["id"], "  Ana   Prueba ", " NEW@UNIMINUTO.EDU.CO ",
        " Ingeniería   Industrial ", " ID-2026 ",
    )
    assert error is None
    assert changes == {
        "full_name": "Ana Prueba",
        "institutional_email": "new@uniminuto.edu.co",
        "program_or_department": "Ingeniería Industrial",
        "student_id": "ID-2026",
    }


def test_validate_profile_update_rejects_duplicate_email(storage):
    user = storage.add_user("one@uniminuto.edu.co")
    storage.add_user("two@uniminuto.edu.co")
    changes, error = auth.validate_profile_update(
        storage, user["id"], "Ana Prueba", "two@uniminuto.edu.co", "Ingeniería", "ID1"
    )
    assert changes is None and "otra cuenta" in error


@pytest.mark.parametrize(
    "full_name,email,program,student_id,expected",
    [
        ("Ana", "ana@uniminuto.edu.co", "Ingeniería", "ID1", "apellido"),
        ("Ana Prueba", "ana@gmail.com", "Ingeniería", "ID1", "institucional"),
        ("Ana Prueba", "ana@uniminuto.edu.co", "", "ID1", "programa"),
        ("Ana Prueba", "ana@uniminuto.edu.co", "Ingeniería", "", "ID"),
    ],
)
def test_validate_profile_update_rejects_invalid_fields(
    storage, full_name, email, program, student_id, expected
):
    user = storage.add_user("old@uniminuto.edu.co")
    changes, error = auth.validate_profile_update(
        storage, user["id"], full_name, email, program, student_id
    )
    assert changes is None and expected.lower() in error.lower()