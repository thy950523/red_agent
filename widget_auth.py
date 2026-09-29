"""Xiaohongshu widget login and persistent, per-user generation quotas."""

from datetime import datetime, timedelta, timezone
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
from time import time
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

import runtime_config
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parent
TZ_CN = timezone(timedelta(hours=8))
# 认证关闭（游客模式）时统一使用的固定 openid；标识直接写进 openid 本身，
# 后台统计和出图存档里一眼就能认出这是未走小红书登录的游客流量。
GUEST_OPEN_ID = "guest:未开启小红书登录"
XHS_BASE = "https://miniapp.xiaohongshu.com/api/rmp"
_access_token = ""
_access_token_expires = 0.0
_token_lock = Lock()


def auth_enabled() -> bool:
    # 后台保存过 auth_enabled 后即以配置为准；未保存时跟随环境变量。
    return runtime_config.get("auth_enabled")


def db_path() -> Path:
    return Path(os.environ.get("GENERATION_DB_PATH", ROOT / ".generation_quota.sqlite3"))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute("PRAGMA busy_timeout = 15000")
    db.execute("""CREATE TABLE IF NOT EXISTS widget_sessions (
        token_hash TEXT PRIMARY KEY, open_id TEXT NOT NULL, expires_at INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS generation_attempts (
        id TEXT PRIMARY KEY, open_id TEXT NOT NULL, day TEXT NOT NULL,
        state TEXT NOT NULL, created_at INTEGER NOT NULL
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS generation_attempts_user_day
        ON generation_attempts (open_id, day, state)""")
    return db


@contextmanager
def database():
    with closing(connect()) as db:
        with db:
            yield db


def _xhs_call(path: str, params: dict, post: bool = False) -> dict:
    url = f"{XHS_BASE}/{path}"
    request = UrlRequest(
        url if post else f"{url}?{urlencode(params)}",
        data=json.dumps(params).encode("utf-8") if post else None,
        headers={"Content-Type": "application/json"} if post else {},
        method="POST" if post else "GET",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
    except (OSError, ValueError):
        raise HTTPException(502, "小红书登录服务暂时不可用") from None
    if not isinstance(result, dict) or result.get("success") is not True:
        raise HTTPException(502, "小红书登录验证失败，请重试")
    data = result.get("data")
    if not isinstance(data, dict):
        raise HTTPException(502, "小红书登录响应无效")
    return data


def exchange_code(code: str) -> str:
    """Only the server exchanges the one-use login code for an open_id."""
    app_id = os.environ.get("XHS_APP_ID", "")
    app_secret = os.environ.get("XHS_APP_SECRET", "")
    if not app_id or not app_secret:
        raise HTTPException(503, "小红书登录服务尚未配置")
    global _access_token, _access_token_expires
    with _token_lock:
        if not _access_token or time() >= _access_token_expires:
            data = _xhs_call("token", {"appid": app_id, "secret": app_secret}, post=True)
            token = data.get("access_token")
            if not isinstance(token, str) or not token:
                raise HTTPException(502, "小红书调用凭证无效")
            _access_token = token
            _access_token_expires = time() + max(60, int(data.get("expires_in", 7200)) - 120)
        access_token = _access_token
    data = _xhs_call("session", {"appid": app_id, "access_token": access_token, "code": code})
    open_id = data.get("open_id")
    if not isinstance(open_id, str) or not open_id:
        raise HTTPException(502, "未取得小红书用户标识")
    return open_id


def issue_session(open_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_in = runtime_config.get("session_days") * 24 * 3600
    with database() as db:
        db.execute("DELETE FROM widget_sessions WHERE expires_at < ?", (int(time()),))
        db.execute(
            "INSERT INTO widget_sessions VALUES (?, ?, ?)",
            (sha256(token.encode()).hexdigest(), open_id, int(time()) + expires_in),
        )
    return token


def require_open_id(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录小红书")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, "请先登录小红书")
    with database() as db:
        row = db.execute(
            "SELECT open_id FROM widget_sessions WHERE token_hash = ? AND expires_at > ?",
            (sha256(token.encode()).hexdigest(), int(time())),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "登录已过期，请重试")
    return row[0]


def require_matching_open_id(authorization: str | None, claimed_open_id: str | None) -> str:
    open_id = require_open_id(authorization)
    if not claimed_open_id:
        raise HTTPException(400, "缺少 openid")
    if claimed_open_id != open_id:
        raise HTTPException(403, "openid 与登录用户不一致")
    return open_id


def today() -> str:
    return datetime.now(TZ_CN).date().isoformat()


def quota(open_id: str) -> dict:
    limit = runtime_config.get("daily_limit")
    pending_seconds = runtime_config.get("pending_minutes") * 60
    with database() as db:
        used = db.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE open_id = ? AND day = ? "
            "AND (state = 'success' OR (state = 'pending' AND created_at > ?))",
            (open_id, today(), int(time()) - pending_seconds),
        ).fetchone()[0]
    return {"limit": limit, "used": used, "remaining": max(0, limit - used)}


def reserve(open_id: str) -> str:
    attempt_id = secrets.token_hex(16)
    now = int(time())
    day = today()
    limit = runtime_config.get("daily_limit")
    pending_seconds = runtime_config.get("pending_minutes") * 60
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "DELETE FROM generation_attempts WHERE state = 'pending' AND created_at <= ?",
            (now - pending_seconds,),
        )
        used = db.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE open_id = ? AND day = ?",
            (open_id, day),
        ).fetchone()[0]
        if used >= limit:
            raise HTTPException(429, f"今日 {limit} 次生成额度已用完，明天再来吧")
        db.execute(
            "INSERT INTO generation_attempts VALUES (?, ?, ?, 'pending', ?)",
            (attempt_id, open_id, day, now),
        )
    return attempt_id


def finish(attempt_id: str, success: bool) -> None:
    with database() as db:
        if success:
            db.execute("UPDATE generation_attempts SET state = 'success' WHERE id = ?", (attempt_id,))
        else:
            db.execute("DELETE FROM generation_attempts WHERE id = ? AND state = 'pending'", (attempt_id,))
