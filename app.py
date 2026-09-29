"""Portrait-to-Bikini-Bottom-fish matching and image generation service.

Run with: uvicorn app:app --reload
"""

import base64
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import logging
import os
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from uuid import uuid4

from admin_stats import router as admin_router
from match_engine import EXTRACTOR, FaceProblem, match_photo
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
import runtime_config
import widget_auth


ROOT = Path(__file__).resolve().parent
IMAGES = ROOT / "images"
# 01.jpg is a collage of three characters, not one matchable character.
FISH = sorted(IMAGES.glob("*.png"))
Image.MAX_IMAGE_PIXELS = 20_000_000
RESULTS = ROOT / ".results"
ARCHIVE = ROOT / "generated_archive"
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
# 默认值；后台“运行时配置”里保存过的值会覆盖它们（见 runtime_config.py）。
ARK_MODEL = runtime_config.SPEC["ark_model"]["default"]
GENERATION_PROMPT = runtime_config.GENERATION_PROMPT_DEFAULT
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

app = FastAPI(title="比奇堡路人鱼趣味匹配")
app.mount("/images", StaticFiles(directory=IMAGES), name="images")
app.include_router(admin_router)


class LoginCode(BaseModel):
    code: str


@app.get("/auth/config")
def get_widget_auth_config():
    return {"authEnabled": widget_auth.auth_enabled()}


@app.post("/auth/xhs")
async def login_widget(payload: LoginCode):
    if not widget_auth.auth_enabled():
        raise HTTPException(400, "本地开发模式无需登录")
    if not payload.code or len(payload.code) > 256:
        raise HTTPException(400, "登录凭证无效")
    open_id = await run_in_threadpool(widget_auth.exchange_code, payload.code)
    token = await run_in_threadpool(widget_auth.issue_session, open_id)
    return {"token": token, "openid": open_id,
            "quota": await run_in_threadpool(widget_auth.quota, open_id)}


@app.get("/auth/quota")
async def get_widget_quota(request: Request, openid: str | None = None):
    if not widget_auth.auth_enabled():
        return {"limit": None, "used": 0, "remaining": None}
    open_id = await run_in_threadpool(
        widget_auth.require_matching_open_id, request.headers.get("authorization"), openid
    )
    return await run_in_threadpool(widget_auth.quota, open_id)


def count_faces(image: Image.Image) -> int:
    try:
        return EXTRACTOR.detect(image)[1]
    except FaceProblem as error:
        if error.code == "NO_FACE":
            return 0
        raise


@app.get("/")
def demo():
    # 匿名访客编号：仅用于统计参与人数，不含任何个人信息。
    response = FileResponse(ROOT / "demo.html")
    response.set_cookie(
        "vid", uuid4().hex, max_age=365 * 24 * 3600, httponly=True, samesite="lax"
    )
    return response


def client_ip_hash(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "")
    return sha256(ip.encode("utf-8")).hexdigest()[:16]


async def read_image(file: UploadFile) -> Image.Image:
    if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(400, "请上传 JPG、PNG 或 WebP 照片")

    max_bytes = runtime_config.get("max_upload_mb") * 1024 * 1024
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(413, f"照片不能超过 {runtime_config.get('max_upload_mb')} MB")

    try:
        image = ImageOps.exif_transpose(Image.open(BytesIO(raw))).convert("RGB")
        image.thumbnail((2048, 2048))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise HTTPException(400, "无法读取这张照片") from None
    return image


@app.post("/match")
async def match(request: Request, response: Response, file: UploadFile = File(...),
                openid: str | None = Form(None)):
    if widget_auth.auth_enabled() and openid is not None:
        await run_in_threadpool(widget_auth.require_matching_open_id,
                                request.headers.get("authorization"), openid)
    started = perf_counter()
    image = await read_image(file)
    decode_ms = (perf_counter() - started) * 1000
    try:
        # Uploaded portrait stays in memory; references were loaded at import.
        ranked, timings = await run_in_threadpool(match_photo, image)
    except FaceProblem as error:
        logger.info("match_rejected code=%s decode_ms=%.1f total_ms=%.1f", error.code, decode_ms, (perf_counter() - started) * 1000)
        raise HTTPException(422, detail={"code": error.code, "message": error.message}) from None
    matches = [
        {
            "id": fish_id,
            "imageUrl": str(request.url_for("images", path=f"{fish_id}.webp")),
        }
        for fish_id, _score in ranked
    ]
    total_ms = (perf_counter() - started) * 1000
    response.headers["Server-Timing"] = ", ".join(
        f"{name};dur={value:.2f}" for name, value in (
            ("decode", decode_ms),
            ("face", timings["face_detection_ms"]),
            ("features", timings["feature_extraction_ms"]),
            ("score", timings["scoring_ms"]),
            ("total", total_ms),
        )
    )
    logger.info("match_timing decode_ms=%.1f face_detection_ms=%.1f feature_extraction_ms=%.1f scoring_ms=%.1f total_ms=%.1f faces=%d", decode_ms, timings["face_detection_ms"], timings["feature_extraction_ms"], timings["scoring_ms"], total_ms, timings["face_count"])
    return {"match": matches[0], "alternatives": matches[1:]}


def generate_with_ark(portrait: Image.Image, fish_path: Path) -> Image.Image:
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        raise HTTPException(503, "生图服务尚未配置")
    model = runtime_config.get("ark_model")
    prompt = runtime_config.get("generation_prompt")

    portrait_bytes = BytesIO()
    portrait.save(portrait_bytes, format="JPEG", quality=90)
    payload = {
        "model": model,
        "prompt": prompt,
        "image": [
            "data:image/jpeg;base64," + base64.b64encode(portrait_bytes.getvalue()).decode(),
            "data:image/png;base64," + base64.b64encode(fish_path.read_bytes()).decode(),
        ],
        "response_format": "url",
        "size": runtime_config.get("generation_size"),
        "stream": False,
        "watermark": runtime_config.get("watermark"),
    }
    request = UrlRequest(
        ARK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=110) as response:
            result = json.load(response)
        image_url = result["data"][0]["url"]
        if not isinstance(image_url, str) or not image_url.startswith("https://"):
            raise ValueError("invalid image URL")
        with urlopen(image_url, timeout=30) as response:
            image_bytes = response.read(30 * 1024 * 1024 + 1)
        if len(image_bytes) > 30 * 1024 * 1024:
            raise ValueError("generated image is too large")
        return Image.open(BytesIO(image_bytes)).convert("RGB")
    except (HTTPError, URLError, OSError, ValueError, KeyError, IndexError, TypeError):
        logger.exception("Ark image generation failed")
        raise HTTPException(502, "图像生成失败，请稍后重试") from None


def archive_generated_image(
    image: Image.Image, fish_id: str, vid: str | None = None,
    ip_hash: str | None = None, open_id: str | None = None,
) -> str:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    image_path = ARCHIVE / f"{token}.png"
    pending_path = ARCHIVE / f"{token}.pending.png"
    image.save(pending_path, format="PNG")
    os.replace(pending_path, image_path)
    thumbnail = image.copy()
    thumbnail.thumbnail((320, 320))
    thumb_pending = ARCHIVE / f"{token}.thumb.pending.jpg"
    thumbnail.save(thumb_pending, format="JPEG", quality=80)
    os.replace(thumb_pending, ARCHIVE / f"{token}.thumb.jpg")
    metadata = {
        "id": token,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "fishId": fish_id,
        "model": runtime_config.get("ark_model"),
        "prompt": runtime_config.get("generation_prompt"),
        "imageOrder": ["uploaded_portrait", f"images/{fish_id}.png"],
        "size": {"width": image.width, "height": image.height},
        "vid": vid,
        "ipHash": ip_hash,
        "openId": open_id,
    }
    (ARCHIVE / f"{token}.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return token


@app.post("/generate")
async def generate(request: Request, fishId: str = Form(...), file: UploadFile = File(...),
                   openid: str | None = Form(None)):
    if not runtime_config.get("generation_enabled"):
        raise HTTPException(503, "图片生成已临时关闭，请稍后再来")
    open_id = None
    if widget_auth.auth_enabled():
        open_id = await run_in_threadpool(
            widget_auth.require_matching_open_id, request.headers.get("authorization"), openid
        )
    fish_path = next((path for path in FISH if path.stem == fishId), None)
    if fish_path is None:
        raise HTTPException(400, "无效的路人鱼编号")

    portrait = await read_image(file)
    attempt_id = await run_in_threadpool(widget_auth.reserve, open_id) if open_id else None
    try:
        generated = await run_in_threadpool(generate_with_ark, portrait, fish_path)
        vid = request.cookies.get("vid")
        ip_hash = client_ip_hash(request)
        token = await run_in_threadpool(archive_generated_image, generated, fishId, vid, ip_hash, open_id)
    except Exception:
        if attempt_id:
            await run_in_threadpool(widget_auth.finish, attempt_id, False)
        raise
    if attempt_id:
        await run_in_threadpool(widget_auth.finish, attempt_id, True)
    return {"imageUrl": str(request.url_for("get_result", token=token)),
            "quota": await run_in_threadpool(widget_auth.quota, open_id) if open_id else None}


@app.get("/result/{token}", name="get_result")
def get_result(token: str):
    if len(token) != 32 or not all(char in "0123456789abcdef" for char in token):
        raise HTTPException(404)
    path = ARCHIVE / f"{token}.png"
    if not path.is_file():
        path = RESULTS / f"{token}.png"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")
