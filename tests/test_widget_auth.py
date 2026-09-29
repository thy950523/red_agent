from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import app as service
import widget_auth


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("WIDGET_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("GENERATION_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_CONFIG_PATH", str(tmp_path / "runtime-config.json"))
    monkeypatch.setattr(service, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(widget_auth, "exchange_code", lambda code: f"open-id-{code}")
    monkeypatch.setattr(service, "generate_with_ark", lambda _portrait, _fish: Image.new("RGB", (8, 8), "red"))
    return TestClient(service.app)


def photo():
    output = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(output, format="PNG")
    return output.getvalue()


def generate(client, token, openid="open-id-alice"):
    return client.post(
        "/generate", data={"fishId": "22", "openid": openid},
        files={"file": ("portrait.png", photo(), "image/png")},
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


def test_login_and_daily_limit_are_tied_to_verified_open_id(client):
    assert generate(client, "").status_code == 401
    login = client.post("/auth/xhs", json={"code": "alice"})
    assert login.status_code == 200
    token = login.json()["token"]
    assert login.json()["openid"] == "open-id-alice"
    assert login.json()["quota"] == {"limit": 5, "used": 0, "remaining": 5}
    assert client.get("/auth/quota?openid=open-id-alice", headers={"Authorization": f"Bearer {token}"}).json()["remaining"] == 5
    assert generate(client, token, "open-id-bob").status_code == 403
    assert client.post(
        "/match", data={"openid": "open-id-bob"},
        files={"file": ("portrait.png", photo(), "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 403
    for remaining in (4, 3, 2, 1, 0):
        response = generate(client, token)
        assert response.status_code == 200
        assert response.json()["quota"]["remaining"] == remaining
    assert generate(client, token).status_code == 429
    second_token = client.post("/auth/xhs", json={"code": "alice"}).json()["token"]
    assert generate(client, second_token).status_code == 429
    bob_token = client.post("/auth/xhs", json={"code": "bob"}).json()["token"]
    assert generate(client, bob_token, "open-id-bob").status_code == 200


def test_failed_generation_releases_quota(client, monkeypatch):
    token = client.post("/auth/xhs", json={"code": "alice"}).json()["token"]
    def failure(_portrait, _fish):
        raise service.HTTPException(502, "generation failed")
    monkeypatch.setattr(service, "generate_with_ark", failure)
    assert generate(client, token).status_code == 502
    assert client.get("/auth/quota?openid=open-id-alice", headers={"Authorization": f"Bearer {token}"}).json()["remaining"] == 5


def test_local_mode_skips_login_and_quota(client, monkeypatch):
    monkeypatch.setenv("WIDGET_AUTH_ENABLED", "0")
    assert client.get("/auth/config").json()["authEnabled"] is False
    response = client.post(
        "/generate", data={"fishId": "22"},
        files={"file": ("portrait.png", photo(), "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["quota"] is None
    assert not widget_auth.db_path().exists()


def test_reservations_are_atomic_and_reset_next_day(client, monkeypatch):
    widget_auth.connect().close()  # Initialize schema before concurrent reservations.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _reserve_result("same-open-id"), range(12)))
    assert results.count(True) == 5
    assert widget_auth.quota("same-open-id")["remaining"] == 0
    monkeypatch.setattr(widget_auth, "today", lambda: "2099-01-01")
    assert widget_auth.quota("same-open-id")["remaining"] == 5


def test_xhs_code_exchange_keeps_secret_on_server(monkeypatch):
    monkeypatch.setenv("XHS_APP_ID", "app-id")
    monkeypatch.setenv("XHS_APP_SECRET", "server-secret")
    monkeypatch.setattr(widget_auth, "_access_token", "")
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        data = ({"access_token": "platform-token", "expires_in": 7200}
                if len(requests) == 1 else {"open_id": "verified-open-id", "session_key": "private-key"})
        return BytesIO(json.dumps({"success": True, "data": data}).encode())

    monkeypatch.setattr(widget_auth, "urlopen", fake_urlopen)
    assert widget_auth.exchange_code("one-use-code") == "verified-open-id"
    assert requests[0].get_method() == "POST"
    assert requests[0].full_url == f"{widget_auth.XHS_BASE}/token"
    assert json.loads(requests[0].data) == {"appid": "app-id", "secret": "server-secret"}
    assert requests[1].get_method() == "GET"
    assert "code=one-use-code" in requests[1].full_url
    assert "server-secret" not in requests[1].full_url


def _reserve_result(open_id):
    try:
        widget_auth.reserve(open_id)
        return True
    except service.HTTPException as error:
        assert error.status_code == 429
        return False
