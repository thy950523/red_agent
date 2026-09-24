"""Portrait-to-Bikini-Bottom-fish matching and image generation service.

Run with: uvicorn app:app --reload
"""

import base64
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
import json
import logging
import os
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from uuid import uuid4

import cv2
import numpy as np
import torch
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool


ROOT = Path(__file__).resolve().parent
IMAGES = ROOT / "images"
# 01.jpg is a collage of three characters, not one matchable character.
FISH = sorted(IMAGES.glob("*.png"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MODEL_NAME = "openai/clip-vit-base-patch32"
FACE_MODEL = ROOT / "models" / "face_detection_yunet_2026may.onnx"
Image.MAX_IMAGE_PIXELS = 20_000_000
RESULTS = ROOT / ".results"
ARCHIVE = ROOT / "generated_archive"
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
ARK_MODEL = "doubao-seedream-5-0-flash-260915"
GENERATION_PROMPT = (
    "请将图一的真人照片完整转化为一张风格 A 的二维卡通插画。**画面仍然是图一的画面，只是人物变成按照图二绘制的比奇堡路人鱼。**\n"
    "**图一控制内容和布局：**保持图一的画幅比例、裁切、景别、镜头角度、人物在画面中的位置与大小、身体姿势、手势、头部朝向、表情、服装、随身物品，"
    "以及背景主要物体的相对位置。图一拍到哪里就画到哪里，不扩图，不强制补出全身。\n"
    "**图一的头发必须完整保留：**保留图一的发色、分缝、发际线、头发长度、发量、发束走向及具体发型，包括辫子、刘海或扎发等可见细节。"
    "把这些头发用图二的平面卡通画法重新绘制，自然长在鱼角色的头部上。不得用图二的帽子遮住或替换图一的头发，也不得擅自改变发型。\n"
    "**图二控制鱼类造型和画风：**将图二的简化鱼头、卡通鱼眼、鱼嘴、鱼类肤色、鳍状手臂和鱼类身体比例应用到图一的人物位置，"
    "使所有可见身体部位都成为统一的二维卡通鱼。让鱼角色做图一的动作，并用鱼的眼神、眉形与嘴形表现图一真人的情绪。"
    "不要照搬图二的姿势、帽子、服装或背景；图一的服装与物品要用同一套卡通画法重绘。\n"
    "**固定风格 A：**可爱、明快的手绘二维电视动画；清晰而略有手绘起伏的深色轮廓线；鲜明协调的大色块平涂；每个主要形体最多一层简单的硬边阴影；"
    "大而清楚的卡通眼睛；没有写实纹理、复杂光影或 3D 体积感。将图一的背景也按这套画法重绘，保留图一的地点与空间布局，可少量加入海底世界的装饰元素。\n"
    "**严格避免：**真人脸贴在鱼身上、真实皮肤或五官、真人身体加鱼鳞或鱼尾、半写实鱼人、美人鱼尾巴、图二的帽子挡住头发、丢失或改动图一的发型、"
    "照搬图二的服装和姿势、改变图一构图、照片质感、3D 渲染、油画笔触、塑料光泽、文字、水印、界面元素。"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="比奇堡路人鱼趣味匹配")
app.mount("/images", StaticFiles(directory=IMAGES), name="images")


class Matcher:
    def __init__(self):
        from transformers import AutoProcessor, CLIPModel

        self.processor = AutoProcessor.from_pretrained(MODEL_NAME)
        self.model = CLIPModel.from_pretrained(MODEL_NAME).eval()
        self.reference_features = torch.cat(
            [self._encode(Image.open(path).convert("RGB")) for path in FISH]
        )

    def _encode(self, image: Image.Image) -> torch.Tensor:
        inputs = self.processor(images=image, return_tensors="pt")
        with torch.inference_mode():
            result = self.model.get_image_features(**inputs)
        vector = result.pooler_output if hasattr(result, "pooler_output") else result
        return torch.nn.functional.normalize(vector, dim=-1)

    def rank(self, image: Image.Image, count: int = 3):
        scores = (self._encode(image) @ self.reference_features.T)[0]
        indices = torch.topk(scores, k=min(count, len(FISH))).indices.tolist()
        return [(FISH[index], float(scores[index])) for index in indices]


class FaceCounter:
    def __init__(self):
        self.detector = cv2.FaceDetectorYN_create(
            str(FACE_MODEL), "", (320, 320), 0.75, 0.3, 5000
        )
        self.lock = Lock()

    def count(self, image: Image.Image) -> int:
        frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        height, width = frame.shape[:2]
        if max(width, height) > 960:
            scale = 960 / max(width, height)
            frame = cv2.resize(
                frame,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        height, width = frame.shape[:2]
        with self.lock:
            self.detector.setInputSize((width, height))
            _, faces = self.detector.detect(frame)
        return 0 if faces is None else len(faces)


@lru_cache(maxsize=1)
def get_face_counter() -> FaceCounter:
    return FaceCounter()


def count_faces(image: Image.Image) -> int:
    return get_face_counter().count(image)


@lru_cache(maxsize=1)
def get_matcher() -> Matcher:
    return Matcher()


@app.get("/")
def demo():
    return FileResponse(ROOT / "demo.html")


async def read_image(file: UploadFile) -> Image.Image:
    if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(400, "请上传 JPG、PNG 或 WebP 照片")

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "照片不能超过 20 MB")

    try:
        image = ImageOps.exif_transpose(Image.open(BytesIO(raw))).convert("RGB")
        image.thumbnail((2048, 2048))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise HTTPException(400, "无法读取这张照片") from None
    return image


@app.post("/match")
async def match(request: Request, file: UploadFile = File(...)):
    image = await read_image(file)
    face_count = await run_in_threadpool(count_faces, image)
    if face_count == 0:
        raise HTTPException(
            422,
            detail={"code": "NO_FACE", "message": "请上传一张包含人脸的图片"},
        )

    # The uploaded portrait is processed in memory and is not saved on disk.
    ranked = await run_in_threadpool(lambda: get_matcher().rank(image))
    matches = [
        {
            "id": path.stem,
            "imageUrl": str(request.url_for("images", path=path.name)),
        }
        for path, _score in ranked
    ]
    return {"match": matches[0], "alternatives": matches[1:]}


def generate_with_ark(portrait: Image.Image, fish_path: Path) -> Image.Image:
    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        raise HTTPException(503, "生图服务尚未配置")

    portrait_bytes = BytesIO()
    portrait.save(portrait_bytes, format="JPEG", quality=90)
    payload = {
        "model": ARK_MODEL,
        "prompt": GENERATION_PROMPT,
        "image": [
            "data:image/jpeg;base64," + base64.b64encode(portrait_bytes.getvalue()).decode(),
            "data:image/png;base64," + base64.b64encode(fish_path.read_bytes()).decode(),
        ],
        "response_format": "url",
        "size": "2K",
        "stream": False,
        "watermark": True,
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


def archive_generated_image(image: Image.Image, fish_id: str) -> str:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    image_path = ARCHIVE / f"{token}.png"
    pending_path = ARCHIVE / f"{token}.pending.png"
    image.save(pending_path, format="PNG")
    os.replace(pending_path, image_path)
    metadata = {
        "id": token,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "fishId": fish_id,
        "model": ARK_MODEL,
        "prompt": GENERATION_PROMPT,
        "imageOrder": ["uploaded_portrait", f"images/{fish_id}.png"],
        "size": {"width": image.width, "height": image.height},
    }
    (ARCHIVE / f"{token}.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return token


@app.post("/generate")
async def generate(request: Request, fishId: str = Form(...), file: UploadFile = File(...)):
    fish_path = next((path for path in FISH if path.stem == fishId), None)
    if fish_path is None:
        raise HTTPException(400, "无效的路人鱼编号")

    portrait = await read_image(file)
    generated = await run_in_threadpool(generate_with_ark, portrait, fish_path)
    token = await run_in_threadpool(archive_generated_image, generated, fishId)
    return {"imageUrl": str(request.url_for("get_result", token=token))}


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
