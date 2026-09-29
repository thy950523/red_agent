"""Admin-only statistics dashboard: login, aggregates and the image listing.

All numbers are derived from the JSON sidecar files in generated_archive/,
so there is no extra database to run. See /admin in a browser after setting
the ADMIN_PASSWORD environment variable (or reading .admin_password).
"""

import asyncio
import csv
import hashlib
import hmac
from io import StringIO
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

import runtime_config

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "generated_archive"
# 活动面向国内用户，统计按北京时间展示。中国无夏令时，固定 +8 即可。
TZ_CN = timezone(timedelta(hours=8))
SESSION_COOKIE = "admin_session"
PAGE_SIZE = 24
# 未设置 ADMIN_PASSWORD 环境变量且后台未自定义时的内置密码；公网部署务必改掉。
DEFAULT_ADMIN_PASSWORD = "xiawang123"

router = APIRouter(prefix="/admin")
logger = logging.getLogger(__name__)


def get_admin_password() -> str:
    override = runtime_config.get("admin_password")
    if override:
        return override
    return os.environ.get("ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD


def _session_ttl() -> timedelta:
    return timedelta(days=runtime_config.get("admin_session_days"))


def _signing_key() -> bytes:
    return hashlib.sha256(("red-agent-admin:" + get_admin_password()).encode("utf-8")).digest()


def _new_session_value() -> str:
    expires = int((datetime.now(timezone.utc) + _session_ttl()).timestamp())
    signature = hmac.new(
        _signing_key(), f"admin-session:{expires}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{expires}.{signature}"


def _session_is_valid(value: str) -> bool:
    try:
        expires_text, signature = value.split(".", 1)
        expires = int(expires_text)
    except ValueError:
        return False
    if datetime.now(timezone.utc).timestamp() >= expires:
        return False
    expected = hmac.new(
        _signing_key(), f"admin-session:{expires}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return bool(cookie) and _session_is_valid(cookie)


def _require_auth(request: Request) -> None:
    if not _is_authenticated(request):
        raise HTTPException(401, "未登录或会话已过期")


def _login_page(error: str | None) -> str:
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>路人鱼 · 数据后台登录</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0f172a; color: #e2e8f0; font-family: system-ui, sans-serif; }}
  .box {{ width: min(92vw, 360px); background: #1e293b; border: 1px solid #334155;
          border-radius: 16px; padding: 32px 28px; text-align: center; }}
  h1 {{ font-size: 20px; margin: 0 0 6px; }}
  p {{ color: #94a3b8; font-size: 13px; margin: 0 0 20px; }}
  .error {{ color: #f87171; margin: -8px 0 16px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 11px 12px; border-radius: 10px;
           border: 1px solid #475569; background: #0f172a; color: #e2e8f0;
           font-size: 15px; outline: none; }}
  input:focus {{ border-color: #38bdf8; }}
  button {{ width: 100%; margin-top: 14px; padding: 11px; border: 0; border-radius: 10px;
            background: #38bdf8; color: #0f172a; font-size: 15px; font-weight: 600;
            cursor: pointer; }}
  button:hover {{ background: #7dd3fc; }}
</style>
</head>
<body>
  <form class="box" method="post" action="/admin/login">
    <h1>🐟 路人鱼 · 数据后台</h1>
    <p>请输入管理密码（ADMIN_PASSWORD）</p>
    {error_html}
    <input type="password" name="password" placeholder="管理密码" autofocus required>
    <button type="submit">登 录</button>
  </form>
</body>
</html>"""


@router.get("", response_class=HTMLResponse)
def admin_page(request: Request):
    if _is_authenticated(request):
        return FileResponse(ROOT / "admin.html", headers={"Cache-Control": "no-store"})
    return HTMLResponse(_login_page(None))


@router.post("/login")
async def admin_login(request: Request, password: str = Form("")):
    expected = get_admin_password().encode("utf-8")
    if not hmac.compare_digest(password.encode("utf-8"), expected):
        await asyncio.sleep(1.5)  # 拖慢暴力尝试
        return HTMLResponse(_login_page("密码错误，请重试"), status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _new_session_value(),
        max_age=int(_session_ttl().total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/admin",
    )
    return response


@router.post("/logout")
def admin_logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


@router.get("/api/config")
def get_config(request: Request):
    _require_auth(request)
    return {
        "values": runtime_config.effective_values(),
        "overridden": sorted(runtime_config.overridden_keys()),
    }


@router.post("/api/config")
async def update_config(request: Request):
    _require_auth(request)
    try:
        payload = await request.json()
        values = runtime_config.update(payload)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return {
        "values": values,
        "overridden": sorted(runtime_config.overridden_keys()),
    }


def _load_records() -> list[dict]:
    """Parse every archive sidecar; skip unreadable ones instead of failing."""
    records = []
    for path in sorted(ARCHIVE.glob("*.json")):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(metadata["createdAt"]).astimezone(TZ_CN)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("统计后台跳过无法解析的归档记录 %s", path.name)
            continue
        vid = metadata.get("vid")
        ip_hash = metadata.get("ipHash")
        participant = vid or (f"ip:{ip_hash}" if ip_hash else path.stem)
        records.append(
            {
                "token": path.stem,
                "created": created,
                "fishId": str(metadata.get("fishId", "?")),
                "participant": participant,
            }
        )
    return records


@router.get("/api/summary")
def summary(request: Request):
    _require_auth(request)
    records = _load_records()
    today = datetime.now(TZ_CN).date()
    today_records = [record for record in records if record["created"].date() == today]
    fish_counts: dict[str, int] = {}
    for record in records:
        fish_counts[record["fishId"]] = fish_counts.get(record["fishId"], 0) + 1
    top_fish = sorted(fish_counts.items(), key=lambda item: -item[1])[:10]
    latest = max((record["created"] for record in records), default=None)
    return {
        "totalImages": len(records),
        "totalParticipants": len({record["participant"] for record in records}),
        "todayImages": len(today_records),
        "todayParticipants": len({record["participant"] for record in today_records}),
        "latestAt": latest.strftime("%Y-%m-%d %H:%M") if latest else None,
        "topFish": [{"fishId": fish_id, "count": count} for fish_id, count in top_fish],
    }


def _bucket_series(records, bucket_start: datetime, count: int, unit: str) -> list[dict]:
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    buckets: dict[datetime, dict] = {}
    for index in range(count):
        start = bucket_start + index * step
        buckets[start] = {"label": start.strftime("%m-%d %H时" if unit == "hour" else "%m-%d"),
                          "images": 0, "participants": set()}
    for record in records:
        key = record["created"].replace(minute=0, second=0, microsecond=0) if unit == "hour" \
            else record["created"].date()
        bucket = buckets.get(key)
        if bucket:
            bucket["images"] += 1
            bucket["participants"].add(record["participant"])
    return [
        {"label": bucket["label"], "images": bucket["images"],
         "participants": len(bucket["participants"])}
        for bucket in buckets.values()
    ]


@router.get("/api/timeseries")
def timeseries(request: Request, hours: int = 48, days: int = 30):
    _require_auth(request)
    records = _load_records()
    now_local = datetime.now(TZ_CN)
    hour_count = max(1, min(hours, 24 * 30))
    day_count = max(1, min(days, 365))
    hour_start = (now_local - timedelta(hours=hour_count - 1)).replace(
        minute=0, second=0, microsecond=0
    )
    day_start = (now_local - timedelta(days=day_count - 1)).date()
    return {
        "hourly": _bucket_series(records, hour_start, hour_count, "hour"),
        "daily": _bucket_series(records, day_start, day_count, "day"),
    }


@router.get("/api/images")
def images(request: Request, page: int = 1, pageSize: int = PAGE_SIZE):
    _require_auth(request)
    records = sorted(_load_records(), key=lambda record: record["created"], reverse=True)
    size = max(1, min(pageSize, 96))
    total_pages = max(1, math.ceil(len(records) / size))
    page = max(1, min(page, total_pages))
    items = []
    for record in records[(page - 1) * size : page * size]:
        thumb_path = ARCHIVE / f"{record['token']}.thumb.jpg"
        items.append(
            {
                "token": record["token"],
                "url": str(request.url_for("get_result", token=record["token"])),
                "thumbnail": (
                    str(request.url_for("admin_thumbnail", token=record["token"]))
                    if thumb_path.is_file()
                    else None
                ),
                "fishId": record["fishId"],
                "createdAt": record["created"].strftime("%Y-%m-%d %H:%M"),
            }
        )
    return {"total": len(records), "page": page, "totalPages": total_pages,
            "pageSize": size, "items": items}


@router.get("/thumb/{token}", name="admin_thumbnail")
def admin_thumbnail(token: str):
    if len(token) != 32 or not all(char in "0123456789abcdef" for char in token):
        raise HTTPException(404)
    path = ARCHIVE / f"{token}.thumb.jpg"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    # utf-8-sig 带 BOM，Excel 直接双击打开中文不乱码
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/daily.csv")
def export_daily(request: Request):
    _require_auth(request)
    by_day: dict[str, dict] = {}
    for record in _load_records():
        day = by_day.setdefault(
            record["created"].date().isoformat(), {"images": 0, "participants": set()}
        )
        day["images"] += 1
        day["participants"].add(record["participant"])
    rows = [
        [day, len(data["participants"]), data["images"]]
        for day, data in sorted(by_day.items())
    ]
    return _csv_response("daily-summary.csv", ["日期", "参与人数", "生成张数"], rows)


@router.get("/export/hourly.csv")
def export_hourly(request: Request):
    _require_auth(request)
    by_hour: dict[str, dict] = {}
    for record in _load_records():
        label = record["created"].replace(minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%d %H:00"
        )
        hour = by_hour.setdefault(label, {"images": 0, "participants": set()})
        hour["images"] += 1
        hour["participants"].add(record["participant"])
    rows = [
        [label, len(data["participants"]), data["images"]]
        for label, data in sorted(by_hour.items())
    ]
    return _csv_response("hourly-breakdown.csv", ["时间段", "参与人数", "生成张数"], rows)


@router.get("/export/records.csv")
def export_records(request: Request):
    _require_auth(request)
    records = sorted(_load_records(), key=lambda record: record["created"])
    rows = [
        [
            record["created"].strftime("%Y-%m-%d %H:%M:%S"),
            record["fishId"],
            str(request.url_for("get_result", token=record["token"])),
            record["participant"],
        ]
        for record in records
    ]
    return _csv_response(
        "generation-records.csv",
        ["生成时间(北京时间)", "鱼编号", "图片链接", "访客标识"],
        rows,
    )
