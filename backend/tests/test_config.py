"""The device token is the only thing between a public tunnel and personal data.

These tests exist because the failure is silent: a placeholder token deploys
fine, serves traffic fine, and is wide open.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

BASE = {
    "ASTRA_DEVICE_TOKEN": "x" * 48,
    "POSTGRES_USER": "astra",
    "POSTGRES_PASSWORD": "pw",
    "POSTGRES_DB": "astra",
}


def _settings(**overrides):
    return Settings(**{**BASE, **overrides})


def test_placeholder_token_is_rejected():
    with pytest.raises(ValidationError, match="ASTRA_DEVICE_TOKEN"):
        _settings(ASTRA_DEVICE_TOKEN="change-me-before-exposing-anything")


def test_short_token_is_rejected():
    with pytest.raises(ValidationError, match="ASTRA_DEVICE_TOKEN"):
        _settings(ASTRA_DEVICE_TOKEN="tooshort")


def test_real_token_accepted():
    assert _settings().device_token == "x" * 48


def test_database_url_uses_asyncpg():
    assert _settings().database_url.startswith("postgresql+asyncpg://")


def test_alembic_url_uses_sync_driver():
    """Alembic runs synchronously; handing it the asyncpg URL fails at runtime."""
    assert _settings().sync_database_url.startswith("postgresql+psycopg2://")


def test_defaults_are_india_shaped():
    s = _settings()
    assert s.currency == "INR"
    assert s.timezone == "Asia/Kolkata"
