"""Minimal portrait-to-Bikini-Bottom-fish matching service.

Run with: uvicorn app:app --reload
"""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import Lock
import time
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
RESULT_TTL_SECONDS = 24 * 60 * 60

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


def cleanup_results():
    RESULTS.mkdir(exist_ok=True)
    cutoff = time.time() - RESULT_TTL_SECONDS
    for path in RESULTS.glob("*.png"):
        if path.stat().st_mtime < cutoff:
            path.unlink()


@app.post("/generate")
async def generate(request: Request, fishId: str = Form(...), file: UploadFile = File(...)):
    if fishId not in {path.stem for path in FISH}:
        raise HTTPException(400, "无效的路人鱼编号")

    portrait = await read_image(file)
    cleanup_results()
    token = uuid4().hex
    path = RESULTS / f"{token}.png"
    portrait.save(path, format="PNG")
    return {"imageUrl": str(request.url_for("get_result", token=token)), "mock": True}


@app.get("/result/{token}", name="get_result")
def get_result(token: str):
    if len(token) != 32 or not all(char in "0123456789abcdef" for char in token):
        raise HTTPException(404)
    path = RESULTS / f"{token}.png"
    if not path.is_file() or time.time() - path.stat().st_mtime > RESULT_TTL_SECONDS:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")
