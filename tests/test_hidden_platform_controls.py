# -*- coding: utf-8 -*-
"""Los controles de plataforma (Share, favorito, editar, Deploy, menu ⋮) quedan
ocultos para todos; el logotipo y el boton de la barra lateral se conservan."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _css() -> str:
    return (ROOT / "style.css").read_text(encoding="utf-8")


def test_toolbar_controls_hidden_in_css():
    css = _css()
    start = css.index("Controles de plataforma ocultos")
    block = css[start:css.index("}", start)]
    for testid in ("stToolbarActions", "stAppDeployButton", "stMainMenu"):
        assert f'[data-testid="{testid}"]' in block
    assert "display: none !important" in block


def test_logo_and_sidebar_button_are_not_hidden():
    css = _css()
    start = css.index("Controles de plataforma ocultos")
    block = css[start:css.index("}", start)]
    assert "stHeaderLogo" not in block
    assert "stExpandSidebarButton" not in block
    assert "stToolbar\"]" not in block


def test_toolbar_mode_minimal_in_config():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["client"]["toolbarMode"] == "minimal"
