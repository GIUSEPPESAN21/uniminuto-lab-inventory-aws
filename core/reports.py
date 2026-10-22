# -*- coding: utf-8 -*-
"""
core/reports.py - Analitica de uso del laboratorio y exportacion de la base de datos.
"""

import io

import pandas as pd

from core import excel_safe


def most_borrowed_items(loans: list, top_n: int = 10) -> pd.DataFrame:
    if not loans:
        return pd.DataFrame(columns=["Item", "Veces Prestado", "Unidades Totales"])
    df = pd.DataFrame(loans)
    summary = df.groupby("item_name").agg(
        **{"Veces Prestado": ("id", "count"), "Unidades Totales": ("quantity", "sum")}
    ).reset_index().rename(columns={"item_name": "Item"})
    return summary.sort_values("Veces Prestado", ascending=False).head(top_n)


def average_loan_duration_hours(loans: list) -> float:
    returned = [l for l in loans if l.get("status") == "returned" and l.get("checkout_at") and l.get("return_at")]
    if not returned:
        return 0.0
    durations = [(l["return_at"] - l["checkout_at"]).total_seconds() / 3600 for l in returned]
    return sum(durations) / len(durations)


def top_users_by_loans(loans: list, top_n: int = 10) -> pd.DataFrame:
    if not loans:
        return pd.DataFrame(columns=["Usuario", "Rol", "Prestamos"])
    df = pd.DataFrame(loans)
    summary = df.groupby(["user_name", "user_role"]).size().reset_index(name="Prestamos")
    summary = summary.rename(columns={"user_name": "Usuario", "user_role": "Rol"})
    return summary.sort_values("Prestamos", ascending=False).head(top_n)


def loans_per_day(loans: list, days: int = 30) -> pd.DataFrame:
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    rows = [l for l in loans if l.get("checkout_at") and pd.Timestamp(l["checkout_at"]) >= cutoff]
    if not rows:
        return pd.DataFrame(columns=["Fecha", "Salidas"])
    df = pd.DataFrame(rows)
    df["Fecha"] = pd.to_datetime(df["checkout_at"]).dt.date
    daily = df.groupby("Fecha").size().reset_index(name="Salidas")
    return daily


def items_by_category(items: list) -> pd.DataFrame:
    # Las ubicaciones (estanterias, pisos, mesas, zonas) no son items del inventario.
    relevant = [i for i in items if i.get("status") == "active" and i.get("item_type") != "location"]
    if not relevant:
        return pd.DataFrame(columns=["Categoria", "Items"])
    df = pd.DataFrame(relevant)
    df["category"] = df["category"].replace("", "Sin categoria").fillna("Sin categoria")
    summary = df.groupby("category").size().reset_index(name="Items").rename(columns={"category": "Categoria"})
    return summary.sort_values("Items", ascending=False)


def export_full_database(items: list, users: list, loans: list) -> io.BytesIO:
    df_items = pd.DataFrame(items)
    df_users = pd.DataFrame(users)
    if not df_users.empty and "password_hash" in df_users.columns:
        df_users = df_users.drop(columns=["password_hash"])
    df_loans = pd.DataFrame(loans)

    output = io.BytesIO()
    frames = {name: df for name, df in (("items", df_items), ("users", df_users), ("loans", df_loans))
              if not df.empty}
    excel_safe.write_frames(output, frames)   # un texto «=…» no se vuelve fórmula en el archivo descargado
    output.seek(0)
    return output
