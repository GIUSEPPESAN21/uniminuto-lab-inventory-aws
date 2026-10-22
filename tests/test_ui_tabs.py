# -*- coding: utf-8 -*-
"""Las pestañas de las vistas recuerdan cuál está abierta tras un ``st.rerun()``."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_views_never_use_bare_st_tabs_except_the_login_screen():
    offenders = []
    for path in sorted((ROOT / "views").glob("*.py")):
        if path.name == "login.py":          # sin sesión no hay nada que recordar
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bst\.tabs\(", line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], f"usa core.ui.persistent_tabs: {offenders}"
