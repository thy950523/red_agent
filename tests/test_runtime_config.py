"""Admin dashboard runtime-config panel: API auth, validation and real effects."""

from io import BytesIO
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import admin_stats as stats_admin
import app as service
import runtime_config
import widget_auth

FACE_SAMPLE = Path(__file__).parent / "fixtures" / "astronaut-face.jpg"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.delenv("WIDGET_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-secret")
    monkeypatch.setenv("GENERATION_DB_PATH", str(tmp_path / "quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_CONFIG_PATH", str(tmp_path / "runtime-config.json"))
    monkeypatch.setattr(service.widget_auth, "require_open_id", lambda _header: "config-user")
    monkeypatch.setattr(
        service, "generate_with_ark", lambda _portrait, _fish: Image.new("RGB", (8, 8), "red")
    )
    monkeypatch.setattr(service, "ARCHIVE", tmp_path / "archive")
    real_get = runtime_config.get
    monkeypatch.setattr(
        runtime_config, "get",
        lambda key: 0 if key == "admin_login_rate_limit_seconds" else real_get(key),
    )
    stats_admin._login_attempts.clear()


def admin_client():
    client = TestClient(service.app)
    assert client.post("/admin/login", data={"password": "pw-secret"}).status_code == 200
    return client


def photo(name="portrait.png", color="blue", size=(32, 32)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return {"file": (name, output.getvalue(), "image/png")}


def login_widget(client, monkeypatch, code="alice"):
    monkeypatch.setattr(widget_auth, "exchange_code", lambda _code: f"open-id-{_code}")
    response = client.post("/auth/xhs", json={"code": code})
    assert response.status_code == 200
    return response.json()["token"]


def test_config_api_requires_admin_login():
    fresh = TestClient(service.app)
    assert fresh.get("/admin/api/config").status_code == 401
    assert fresh.post("/admin/api/config", json={"daily_limit": 3}).status_code == 401


def test_get_config_returns_effective_defaults_without_overrides():
    client = admin_client()
    data = client.get("/admin/api/config").json()
    assert data["values"]["daily_limit"] == 100
    assert data["values"]["generation_size"] == "2K"
    assert data["values"]["watermark"] is False
    assert data["values"]["generation_prompt"] == service.GENERATION_PROMPT
    assert data["overridden"] == []


def test_update_daily_limit_takes_effect_without_restart(monkeypatch):
    client = admin_client()
    data = client.post("/admin/api/config", json={"daily_limit": 2}).json()
    assert data["values"]["daily_limit"] == 2
    assert "daily_limit" in data["overridden"]

    token = login_widget(client, monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(2):
        response = client.post("/generate", data={"fishId": "22", "openid": "config-user"},
                               files=photo(), headers=headers)
        assert response.status_code == 200
    denied = client.post("/generate", data={"fishId": "22", "openid": "config-user"},
                         files=photo(), headers=headers)
    assert denied.status_code == 429
    assert "2 次" in denied.json()["detail"]
    assert client.get("/auth/quota", params={"openid": "config-user"},
                      headers=headers).json() == {"limit": 2, "used": 2, "remaining": 0}


def test_generation_toggle_returns_placeholder_but_match_works():
    client = admin_client()
    assert client.post("/admin/api/config",
                       json={"generation_enabled": False}).status_code == 200

    response = client.post("/generate", data={"fishId": "22", "openid": "config-user"},
                           files=photo())
    # 开关关闭后不再 503：随机回一张现成路人鱼图，前端流程照常。
    assert response.status_code == 200
    token = response.json()["imageUrl"].rsplit("/", 1)[-1]
    metadata = json.loads((service.ARCHIVE / f"{token}.json").read_text(encoding="utf-8"))
    assert metadata["mock"] is True
    assert metadata["fishId"] in {path.stem for path in service.FISH}

    with FACE_SAMPLE.open("rb") as handle:
        matched = client.post("/match", files={"file": ("face.jpg", handle.read(), "image/jpeg")})
    assert matched.status_code == 200


def test_upload_limit_change_rejects_oversized_photo():
    client = admin_client()
    assert client.post("/admin/api/config", json={"max_upload_mb": 1}).status_code == 200

    noisy = BytesIO()
    Image.frombytes("RGB", (800, 800), os.urandom(800 * 800 * 3)).save(noisy, format="PNG")
    assert len(noisy.getvalue()) > 1024 * 1024
    response = client.post("/generate", data={"fishId": "22", "openid": "config-user"},
                           files={"file": ("noisy.png", noisy.getvalue(), "image/png")})
    assert response.status_code == 413
    assert response.json()["detail"] == "照片不能超过 1 MB"


def test_config_validation_rejects_bad_values():
    client = admin_client()
    for payload in (
        {"daily_limit": 0},
        {"daily_limit": "5"},
        {"daily_limit": True},
        {"unknown_key": 1},
        {"generation_size": "8K"},
        {"face_confidence_threshold": 2},
        {"match_count": 1.5},
        {"ark_model": ""},
        {"generation_prompt": "   "},
        {"post_note_title": "x" * 101},
    ):
        response = client.post("/admin/api/config", json=payload)
        assert response.status_code == 400, payload
    # 被拒绝的请求不能落盘
    assert client.get("/admin/api/config").json()["overridden"] == []


def test_null_value_resets_override():
    client = admin_client()
    client.post("/admin/api/config", json={"daily_limit": 9})
    assert client.get("/admin/api/config").json()["values"]["daily_limit"] == 9
    data = client.post("/admin/api/config", json={"daily_limit": None}).json()
    assert data["values"]["daily_limit"] == 100
    assert "daily_limit" not in data["overridden"]


def test_auth_enabled_override_beats_environment(monkeypatch):
    client = admin_client()
    monkeypatch.setenv("WIDGET_AUTH_ENABLED", "0")
    assert client.post("/admin/api/config", json={"auth_enabled": True}).status_code == 200
    assert client.get("/auth/config").json()["authEnabled"] is True
    login_widget(client, monkeypatch, code="someone")

    client.post("/admin/api/config", json={"auth_enabled": False})
    assert client.get("/auth/config").json()["authEnabled"] is False
    response = client.post("/generate", data={"fishId": "22"}, files=photo())
    assert response.status_code == 200  # 游客模式不要求登录


def test_guest_mode_returns_fixed_openid_and_shared_daily_quota():
    client = admin_client()
    assert client.post("/admin/api/config", json={"auth_enabled": False}).status_code == 200

    # 关闭登录后 /auth/config 直接下发带标识的固定游客 openid
    config = client.get("/auth/config").json()
    assert config["authEnabled"] is False
    assert "未开启小红书登录" in config["openid"]
    guest_open_id = config["openid"]

    # /auth/xhs 不再 400：返回固定 openid（无会话）和共享限额
    login = client.post("/auth/xhs", json={"code": "whatever"})
    assert login.status_code == 200
    body = login.json()
    assert body["token"] is None
    assert body["openid"] == guest_open_id
    assert body["quota"] == {"limit": 100, "used": 0, "remaining": 100}

    # 游客共享限额真实生效：调低上限后超额请求被 429 拦下
    assert client.post("/admin/api/config", json={"daily_limit": 2}).status_code == 200
    for _ in range(2):
        assert client.post("/generate", data={"fishId": "22"}, files=photo()).status_code == 200
    denied = client.post("/generate", data={"fishId": "22"}, files=photo())
    assert denied.status_code == 429
    assert client.get("/auth/quota").json() == {"limit": 2, "used": 2, "remaining": 0}


def test_post_note_params_reach_widget_and_reset_to_empty():
    client = admin_client()
    assert client.get("/auth/config").json()["postNote"] == {"title": "", "content": "", "tags": ""}

    assert client.post(
        "/admin/api/config",
        json={"post_note_title": "我是比奇堡路人鱼", "post_note_content": "快来测测你是哪条鱼！",
              "post_note_tags": "路人鱼,比奇堡"},
    ).status_code == 200
    note = client.get("/auth/config").json()["postNote"]
    assert note == {"title": "我是比奇堡路人鱼", "content": "快来测测你是哪条鱼！", "tags": "路人鱼,比奇堡"}

    # 清空（提交 null）回落默认值，发布器不再预填
    client.post("/admin/api/config",
                json={"post_note_title": None, "post_note_content": None, "post_note_tags": None})
    assert client.get("/auth/config").json()["postNote"] == {"title": "", "content": "", "tags": ""}


def test_admin_password_override_invalidates_old_sessions(monkeypatch):
    async def instant(_seconds):
        pass

    monkeypatch.setattr(stats_admin.asyncio, "sleep", instant)
    client = admin_client()
    assert client.post("/admin/api/config", json={"admin_password": "brand-new"}).status_code == 200
    # 会话签名密钥派生自密码：改密后旧会话立即失效
    assert client.get("/admin/api/summary").status_code == 401

    fresh = TestClient(service.app)
    assert fresh.post("/admin/login", data={"password": "pw-secret"}).status_code == 401
    fresh.post("/admin/login", data={"password": "brand-new"})
    assert fresh.get("/admin/api/summary").status_code == 200

    fresh.post("/admin/api/config", json={"admin_password": None})
    reset = TestClient(service.app)
    reset.post("/admin/login", data={"password": "pw-secret"})
    assert reset.get("/admin/api/summary").status_code == 200


def test_config_persists_to_configured_path_and_survives_reload(tmp_path):
    config_path = tmp_path / "runtime-config.json"
    runtime_config.update({"watermark": True, "daily_limit": 7})
    assert json.loads(config_path.read_text(encoding="utf-8"))["daily_limit"] == 7
    assert runtime_config.get("watermark") is True
    assert runtime_config.get("daily_limit") == 7
    assert runtime_config.get("session_days") == 7  # 未覆盖的键仍是默认值
    assert not (service.ROOT / "runtime_config.json").exists()


def test_json_login_returns_structured_success_and_failure(monkeypatch):
    async def instant(_seconds):
        pass

    monkeypatch.setattr(stats_admin.asyncio, "sleep", instant)
    client = TestClient(service.app)

    wrong = client.post("/admin/login", data={"password": "nope"},
                        headers={"Accept": "application/json"})
    assert wrong.status_code == 401
    assert wrong.json() == {"ok": False, "message": "密码错误，请重试"}

    right = client.post("/admin/login", data={"password": "pw-secret"},
                        headers={"Accept": "application/json"})
    assert right.status_code == 200
    assert right.json() == {"ok": True, "redirect": "/admin"}
    assert "admin_session=" in right.headers.get("set-cookie", "")
    assert client.get("/admin/api/summary").status_code == 200


def test_login_rate_limit_blocks_rapid_attempts_from_same_ip(monkeypatch):
    async def instant(_seconds):
        pass

    monkeypatch.setattr(stats_admin.asyncio, "sleep", instant)
    real_get = runtime_config.get
    monkeypatch.setattr(
        runtime_config, "get",
        lambda key: 3 if key == "admin_login_rate_limit_seconds" else real_get(key),
    )
    client = TestClient(service.app)

    first = client.post("/admin/login", data={"password": "wrong"})
    assert first.status_code == 401
    assert "密码错误" in first.text

    # 同一 IP 3 秒内第二次尝试：HTML 与 JSON 模式都被 429 拦下
    blocked = client.post("/admin/login", data={"password": "wrong"})
    assert blocked.status_code == 429
    assert "尝试过于频繁" in blocked.text
    blocked_json = client.post("/admin/login", data={"password": "wrong"},
                               headers={"Accept": "application/json"})
    assert blocked_json.status_code == 429
    body = blocked_json.json()
    assert body["ok"] is False
    assert "尝试过于频繁" in body["message"]

    # 密码正确也同样受限，防止借成功登录绕过限速
    blocked_ok = client.post("/admin/login", data={"password": "pw-secret"},
                             headers={"Accept": "application/json"})
    assert blocked_ok.status_code == 429

    # 后台把间隔调成 0（关闭限速）后立即恢复
    monkeypatch.setattr(runtime_config, "get", real_get)
    allowed = client.post("/admin/login", data={"password": "pw-secret"})
    assert allowed.status_code == 200


def test_rate_limit_is_per_ip(monkeypatch):
    async def instant(_seconds):
        pass

    monkeypatch.setattr(stats_admin.asyncio, "sleep", instant)
    real_get = runtime_config.get
    monkeypatch.setattr(
        runtime_config, "get",
        lambda key: 3 if key == "admin_login_rate_limit_seconds" else real_get(key),
    )
    attacker = TestClient(service.app)
    assert attacker.post("/admin/login", data={"password": "wrong"},
                         headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401
    blocked = attacker.post("/admin/login", data={"password": "wrong"},
                            headers={"X-Forwarded-For": "203.0.113.9"})
    assert blocked.status_code == 429
    other = TestClient(service.app)
    assert other.post("/admin/login", data={"password": "pw-secret"},
                      headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200
