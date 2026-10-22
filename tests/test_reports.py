# -*- coding: utf-8 -*-
"""core/reports.py: analitica y exportacion (antes sin pruebas)."""

import io
from datetime import datetime, timedelta, timezone

import pandas as pd

from core import reports


def _loan(item, user, role="estudiante", qty=1, status="returned", days_ago=1, hours=2):
    out = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "id": f"{item}-{user}-{days_ago}", "item_name": item, "user_name": user, "user_role": role,
        "quantity": qty, "status": status, "checkout_at": out,
        "return_at": out + timedelta(hours=hours) if status == "returned" else None,
    }


def test_most_borrowed_items_ranks_by_times_borrowed():
    loans = [_loan("Multimetro", "Ana"), _loan("Multimetro", "Luis", qty=2), _loan("Pinzas", "Ana")]
    top = reports.most_borrowed_items(loans)
    assert list(top["Item"]) == ["Multimetro", "Pinzas"]
    assert list(top["Veces Prestado"]) == [2, 1]
    assert list(top["Unidades Totales"]) == [3, 1]


def test_reports_handle_empty_input():
    assert reports.most_borrowed_items([]).empty
    assert reports.top_users_by_loans([]).empty
    assert reports.loans_per_day([]).empty
    assert reports.items_by_category([]).empty
    assert reports.average_loan_duration_hours([]) == 0.0


def test_average_duration_ignores_open_loans():
    loans = [_loan("A", "Ana", hours=2), _loan("B", "Luis", hours=4), _loan("C", "Eva", status="out")]
    assert reports.average_loan_duration_hours(loans) == 3.0


def test_top_users_counts_loans_per_user():
    loans = [_loan("A", "Ana"), _loan("B", "Ana"), _loan("C", "Luis", role="profesor")]
    top = reports.top_users_by_loans(loans)
    assert top.iloc[0]["Usuario"] == "Ana" and top.iloc[0]["Prestamos"] == 2


def test_loans_per_day_only_counts_the_requested_window():
    loans = [_loan("A", "Ana", days_ago=1), _loan("B", "Ana", days_ago=2), _loan("C", "Ana", days_ago=90)]
    daily = reports.loans_per_day(loans, days=30)
    assert daily["Salidas"].sum() == 2


def test_items_by_category_uses_active_items_and_names_the_uncategorized():
    items = [
        {"status": "active", "category": "Electronica"}, {"status": "active", "category": "Electronica"},
        {"status": "active", "category": ""}, {"status": "retired", "category": "Mecanica"},
    ]
    summary = reports.items_by_category(items).set_index("Categoria")["Items"].to_dict()
    assert summary == {"Electronica": 2, "Sin categoria": 1}


def test_export_never_includes_password_hashes():
    users = [{"id": "1", "full_name": "Ana Perez", "password_hash": "$2b$secreto"}]
    buffer = reports.export_full_database(
        [{"id": "LAB-001", "name": "Multimetro"}], users, [{"id": "L1", "item_id": "LAB-001"}]
    )
    book = pd.read_excel(io.BytesIO(buffer.getvalue()), sheet_name=None)
    assert set(book) == {"items", "users", "loans"}
    assert "password_hash" not in book["users"].columns
    assert "$2b$secreto" not in buffer.getvalue().decode("latin-1")
