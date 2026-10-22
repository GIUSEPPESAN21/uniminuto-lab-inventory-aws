# -*- coding: utf-8 -*-
"""
core/config.py - Acceso seguro a st.secrets.

st.secrets lanza StreamlitSecretNotFoundError si NO existe ningun archivo
secrets.toml en absoluto (no solo si falta una clave puntual). Sin este
wrapper, correr la app o los tests en una maquina sin secrets configurados
tumba TODO el modulo que lo importe. safe_secret() degrada siempre a un
valor por defecto en vez de propagar la excepcion.
"""

import streamlit as st


def safe_secret(key: str, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def has_secret(key: str) -> bool:
    return safe_secret(key, None) not in (None, "")
