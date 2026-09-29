"""Fast, inspectable face-geometry matching across photos and cartoon fish.

The reference descriptors are human annotated in fish_features.json.  Their
values are deliberately editable; no nickname or image-wide embedding enters
the score.  Photos and fish share normalized, perceptual (0..1) axes.
"""

import json
import logging
from pathlib import Path
from threading import Lock
from time import perf_counter

import cv2
import numpy as np
from PIL import Image

import runtime_config


ROOT = Path(__file__).resolve().parent
FEATURES = ("face_roundness", "eye_size", "eye_spacing", "mouth_width", "mouth_open", "smile")
logger = logging.getLogger(__name__)


class FaceProblem(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FishMatcher:
    def __init__(self, catalog_path: Path = ROOT / "fish_features.json"):
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.weights = np.array([catalog["weights"][key] for key in FEATURES], dtype=np.float32)
        self.ids = [item["id"] for item in catalog["fish"]]
        expected = [f"{i:02d}" for i in range(2, 42)]
        if self.ids != expected:
            raise ValueError("fish catalog must contain exactly IDs 02..41 in order")
        missing = [fish_id for fish_id in self.ids if not (ROOT / "images" / f"{fish_id}.webp").is_file()]
        if missing:
            raise ValueError(f"missing fish images: {', '.join(missing)}")
        self.features = np.array(
            [[item["features"][key] for key in FEATURES] for item in catalog["fish"]],
            dtype=np.float32,
        )
        if not np.isfinite(self.features).all() or not ((self.features >= 0) & (self.features <= 1)).all():
            raise ValueError("fish features must be finite values in [0, 1]")
        if not np.isfinite(self.weights).all() or (self.weights <= 0).any():
            raise ValueError("feature weights must be finite and positive")

    def rank(self, photo_features: dict[str, float], count: int = 3):
        vector = np.array([photo_features[key] for key in FEATURES], dtype=np.float32)
        distances = np.sum(self.weights * np.abs(self.features - vector), axis=1) / self.weights.sum()
        order = np.argsort(distances, kind="stable")[:count]
        return [(self.ids[i], float(1 - distances[i])) for i in order]


class PhotoFaceExtractor:
    def __init__(self, model_path: Path = ROOT / "models/face_detection_yunet_2026may.onnx"):
        self.detector = cv2.FaceDetectorYN_create(str(model_path), "", (320, 320), 0.75, 0.3, 5000)
        self.lock = Lock()

    def detect(self, image: Image.Image):
        frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        original_h, original_w = frame.shape[:2]
        scale = min(1.0, 960 / max(original_w, original_h))
        if scale < 1:
            frame = cv2.resize(frame, (round(original_w * scale), round(original_h * scale)), interpolation=cv2.INTER_AREA)
        h, w = frame.shape[:2]
        with self.lock:
            self.detector.setInputSize((w, h))
            _, faces = self.detector.detect(frame)
        gate = runtime_config.get("face_check_enabled")
        if faces is None or len(faces) == 0:
            if gate:
                raise FaceProblem("NO_FACE", "请上传一张包含人脸的图片")
            # 门禁关闭：整图兜底照常出匹配，不拒绝照片。
            return self._fallback_face(image), 0
        # Multiple faces: largest area first; for similarly sized faces, the
        # one closest to the image center wins. No personality inference.
        def priority(face):
            x, y, fw, fh = face[:4]
            area = fw * fh
            center_distance = ((x + fw / 2 - w / 2) / w) ** 2 + ((y + fh / 2 - h / 2) / h) ** 2
            return (-area, center_distance)

        selected = sorted(faces, key=priority)[0].copy()
        if selected[14] < runtime_config.get("face_confidence_threshold") \
                or min(selected[2], selected[3]) < runtime_config.get("min_face_size_px"):
            if gate:
                raise FaceProblem("LOW_CONFIDENCE", "人脸不够清晰，请换一张正面照片")
            # 弱置信人脸仍带真实关键点，比整图兜底更准，直接采用。
        selected[:14] /= scale
        return selected, len(faces)

    @staticmethod
    def _fallback_face(image: Image.Image) -> np.ndarray:
        """无脸兜底：居中正方形当人脸区域，五点用规范比例，只有像素派生的
        特征轴（眼高、张嘴、微笑）携带真实信号，几何轴退化为固定值。"""
        width, height = image.size
        side = min(width, height)
        x, y = (width - side) / 2, (height - side) / 2
        face = np.zeros(15, dtype=np.float32)
        face[:2] = [x, y]
        face[2:4] = [side, side]
        face[4:6] = [x + side * .35, y + side * .40]
        face[6:8] = [x + side * .65, y + side * .40]
        face[8:10] = [x + side * .50, y + side * .55]
        face[10:12] = [x + side * .35, y + side * .72]
        face[12:14] = [x + side * .65, y + side * .72]
        face[14] = 1.0
        return face

    @staticmethod
    def _dark_extent(gray: np.ndarray, x: float, y: float, radius_x: int, radius_y: int):
        h, w = gray.shape
        left, right = max(0, round(x) - radius_x), min(w, round(x) + radius_x)
        top, bottom = max(0, round(y) - radius_y), min(h, round(y) + radius_y)
        patch = gray[top:bottom, left:right]
        if patch.size == 0:
            return 0.0, 0.0
        # Local contrast removes most illumination and skin-tone differences.
        threshold = min(float(np.percentile(patch, 32)), float(np.median(patch)) - 12)
        mask = (patch < threshold).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            return 0.0, 0.0
        component = max(stats[1:], key=lambda row: row[4])
        return float(component[2]) / max(1, 2 * radius_x), float(component[3]) / max(1, 2 * radius_y)

    def features(self, image: Image.Image, face: np.ndarray):
        x, y, width, height = face[:4]
        # Crop around the selected face; the remainder of the photo never enters scoring.
        pad_x, pad_y = width * 0.08, height * 0.08
        crop = image.crop((max(0, x - pad_x), max(0, y - pad_y), min(image.width, x + width + pad_x), min(image.height, y + height + pad_y)))
        gray = cv2.cvtColor(np.asarray(crop), cv2.COLOR_RGB2GRAY)
        if runtime_config.get("face_check_enabled") \
                and cv2.Laplacian(gray, cv2.CV_64F).var() < runtime_config.get("sharpness_threshold"):
            raise FaceProblem("LOW_CONFIDENCE", "人脸不够清晰，请换一张正面照片")
        eye1, eye2 = face[4:6], face[6:8]
        mouth1, mouth2 = face[10:12], face[12:14]
        eyes = sorted((eye1, eye2), key=lambda p: p[0])
        mouths = sorted((mouth1, mouth2), key=lambda p: p[0])
        eye_gap = abs(eyes[1][0] - eyes[0][0]) / width
        mouth_gap = abs(mouths[1][0] - mouths[0][0]) / width
        crop_left, crop_top = max(0, x - pad_x), max(0, y - pad_y)
        eye_extents = []
        for eye in eyes:
            ew, eh = self._dark_extent(gray, eye[0] - crop_left, eye[1] - crop_top, max(5, round(width * .13)), max(4, round(height * .10)))
            eye_extents.append((ew, eh))
        # Mouth interior: local dark connected region between YuNet's mouth corners.
        mx = (mouths[0][0] + mouths[1][0]) / 2 - crop_left
        my = (mouths[0][1] + mouths[1][1]) / 2 - crop_top
        _, mouth_height = self._dark_extent(gray, mx, my, max(5, round(width * .23)), max(4, round(height * .12)))
        mouth_radius = max(4, round(height * .12))
        left, right = max(0, round(mx - width * .23)), min(gray.shape[1], round(mx + width * .23))
        top, bottom = max(0, round(my - mouth_radius)), min(gray.shape[0], round(my + mouth_radius))
        mouth_patch = gray[top:bottom, left:right]
        dark = mouth_patch < np.percentile(mouth_patch, 30)
        interior_y = top + float(np.where(dark)[0].mean()) if dark.any() else my
        eye_height = float(np.mean([extent[1] for extent in eye_extents]))
        # Normalization intentionally compresses photo geometry to perceptual bins
        # comparable with the hand annotated cartoon reference sheet.
        return {
            "face_roundness": float(np.clip((width / height - .65) / .45, 0, 1)),
            "eye_size": float(np.clip((eye_height - .18) / .55, 0, 1)),
            "eye_spacing": float(np.clip((eye_gap - .22) / .38, 0, 1)),
            "mouth_width": float(np.clip((mouth_gap - .20) / .45, 0, 1)),
            "mouth_open": float(np.clip((mouth_height - .12) / .65, 0, 1)),
            "smile": float(np.clip(.5 + (interior_y - my) / height * 6, 0, 1)),
        }


MATCHER = FishMatcher()
EXTRACTOR = PhotoFaceExtractor()


def match_photo(image: Image.Image):
    started = perf_counter()
    face, face_count = EXTRACTOR.detect(image)
    detection_ms = (perf_counter() - started) * 1000
    extracted = perf_counter()
    features = EXTRACTOR.features(image, face)
    extraction_ms = (perf_counter() - extracted) * 1000
    scored = perf_counter()
    ranked = MATCHER.rank(features, count=runtime_config.get("match_count"))
    scoring_ms = (perf_counter() - scored) * 1000
    return ranked, {"face_detection_ms": detection_ms, "feature_extraction_ms": extraction_ms, "scoring_ms": scoring_ms, "face_count": face_count}
