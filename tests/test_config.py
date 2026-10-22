# -*- coding: utf-8 -*-
from core.config import safe_secret, has_secret


def test_safe_secret_never_raises_without_secrets_file():
    assert safe_secret("ANYTHING", "default") == "default"
    assert safe_secret("ANYTHING") is None


def test_has_secret_false_without_secrets_file():
    assert has_secret("GITHUB_TOKEN") is False
