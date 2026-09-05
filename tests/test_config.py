"""Config: need() enforces presence, fixing the old silent-default smell."""
import pytest

from backend.config import settings


def test_need_returns_value():
    assert settings.need("anthropic/claude-opus-5", "GEN_MODEL") == "anthropic/claude-opus-5"


def test_need_raises_on_none():
    with pytest.raises(RuntimeError):
        settings.need(None, "MISSING_VAR")


def test_need_raises_on_empty_string():
    with pytest.raises(RuntimeError):
        settings.need("", "MISSING_VAR")
