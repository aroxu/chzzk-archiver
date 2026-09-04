from fastapi.testclient import TestClient

from app.main import app


def test_setup_subscribe_and_isolation():
    with TestClient(app) as admin:
        assert admin.post("/api/auth/setup", json={"username": "admin", "password": "very-secret"}).status_code == 200
        invite = admin.post("/api/admin/invites").json()["token"]
        cid = "a" * 32
        assert admin.post("/api/subscriptions", json={"channel": cid}).status_code == 200
        with TestClient(app) as user:
            assert user.post(
                "/api/auth/register",
                json={"username": "viewer", "password": "very-secret", "invite": invite},
            ).status_code == 200
            assert user.get("/api/subscriptions").json() == []
            assert user.post("/api/subscriptions", json={"channel": cid}).status_code == 200
            assert len(user.get("/api/subscriptions").json()) == 1
        assert len(admin.get("/api/subscriptions").json()) == 1


def test_invite_is_single_use():
    with TestClient(app) as client:
        client.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        token = client.post("/api/admin/invites").json()["token"]
        assert client.post(
            "/api/auth/register",
            json={"username": "one", "password": "secret", "invite": token},
        ).status_code == 200
        assert client.post(
            "/api/auth/register",
            json={"username": "two", "password": "secret", "invite": token},
        ).status_code == 400


def test_setup_is_only_allowed_once():
    with TestClient(app) as client:
        assert client.post("/api/auth/setup", json={"username": "admin", "password": "secret"}).status_code == 200
        assert client.get("/api/auth/status").json() == {"setup_required": False}
        assert client.post("/api/auth/setup", json={"username": "other", "password": "secret"}).status_code == 409


def test_anonymous_requests_are_rejected():
    with TestClient(app) as client:
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/recordings").status_code == 401


def test_non_admin_cannot_issue_invites():
    with TestClient(app) as admin:
        admin.post("/api/auth/setup", json={"username": "admin", "password": "secret"})
        invite = admin.post("/api/admin/invites").json()["token"]
    with TestClient(app) as viewer:
        viewer.post("/api/auth/register", json={"username": "viewer", "password": "secret", "invite": invite})
        assert viewer.post("/api/admin/invites").status_code == 403
        assert viewer.get("/api/admin/overview").status_code == 403
