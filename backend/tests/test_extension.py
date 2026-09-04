import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import sqlite_path
from app.main import app
from app.models import Credential
from app.security import fernet
from app.services.credentials import user_cookies


def test_sqlite_url_forms_resolve_to_paths():
    relative = Path(sqlite_path("sqlite:///./data/archiver.db"))
    assert relative.name == "archiver.db"
    assert relative.parent.name == "data"

    # The extra slash marks a root-anchored path; Path normalises the separator
    # per platform, so compare the anchored components rather than the string.
    absolute = Path(sqlite_path("sqlite:////data/archiver.db"))
    assert absolute.parts == (os.sep, "data", "archiver.db")

    assert sqlite_path("sqlite://") == ":memory:"


def test_rejects_non_sqlite_database_url():
    with pytest.raises(ValueError):
        sqlite_path("postgresql://localhost/archiver")


def test_pairing_code_exchanges_once_then_syncs_cookies():
    with TestClient(app) as client:
        created = client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).json()
        code = client.post("/api/me/pair").json()["code"]
        token = client.post("/api/extension/exchange", params={"code": code}).json()["token"]
        assert client.post("/api/extension/exchange", params={"code": code}).status_code == 400

        headers = {"Authorization": f"Bearer {token}"}
        response = client.put(
            "/api/extension/cookies",
            json={"cookies": {"NID_AUT": "aut-value", "NID_SES": "ses-value", "EVIL": "drop-me"}},
            headers=headers,
        )
        assert response.status_code == 200
        assert sorted(response.json()["names"]) == ["NID_AUT", "NID_SES"]

        stored = Credential.get(Credential.user == created["id"])
        assert "aut-value" not in stored.encrypted
        assert fernet().decrypt(stored.encrypted.encode())
        assert user_cookies([created["id"]]) == [{"NID_AUT": "aut-value", "NID_SES": "ses-value"}]
        assert client.get("/api/me").json()["cookie_status"] == "valid"


def test_cookie_sync_rejects_unrelated_names():
    with TestClient(app) as client:
        client.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        assert client.put("/api/extension/cookies", json={"cookies": {"SESSION": "x"}}).status_code == 422
