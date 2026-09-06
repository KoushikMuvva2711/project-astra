"""Phase 0 acceptance: the app boots, and nothing that reads data is unauthenticated."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_liveness_is_open_and_says_nothing():
    """Docker and the tunnel probe this. It must not require auth or leak state."""
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_is_harmless():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "astra"


@pytest.mark.parametrize("path", ["/health/ready", "/health/agents"])
def test_data_endpoints_reject_missing_token(path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/health/ready", "/health/agents"])
def test_data_endpoints_reject_wrong_token(path):
    r = client.get(path, headers={"Authorization": "Bearer definitely-not-the-token"})
    assert r.status_code == 401


def test_malformed_authorization_header_rejected():
    for header in ["", "Token abc", "Bearer", "bearer x"]:
        r = client.get("/health/ready", headers={"Authorization": header})
        assert r.status_code == 401, f"accepted malformed header: {header!r}"
