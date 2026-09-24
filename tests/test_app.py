from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

import app as service


client = TestClient(service.app)
FACE_SAMPLE = Path(__file__).parent / "fixtures" / "astronaut-face.jpg"


def test_rejects_non_image_upload():
    response = client.post("/match", files={"file": ("note.txt", b"hello", "text/plain")})
    assert response.status_code == 400


def test_match_requires_a_face(monkeypatch):
    monkeypatch.setattr(service, "count_faces", lambda _image: 0)

    class FakeMatcher:
        def rank(self, _image):
            pytest.fail("matching must not run for an invalid face count")

    monkeypatch.setattr(service, "get_matcher", lambda: FakeMatcher())
    photo = BytesIO()
    Image.new("RGB", (64, 64), "white").save(photo, format="PNG")

    response = client.post(
        "/match", files={"file": ("portrait.png", photo.getvalue(), "image/png")}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "请上传一张包含人脸的图片"


def test_yunet_counts_zero_one_and_two_faces():
    single = Image.open(FACE_SAMPLE).convert("RGB")
    double = Image.new("RGB", (single.width * 2, single.height))
    double.paste(single, (0, 0))
    double.paste(single, (single.width, 0))

    assert service.count_faces(Image.new("RGB", (512, 512), "white")) == 0
    assert service.count_faces(single) == 1
    assert service.count_faces(double) == 2


def test_match_reaches_ranker_for_one_real_face(monkeypatch):
    class FakeMatcher:
        def rank(self, _image):
            return [(service.IMAGES / "22.png", 0.9)]

    monkeypatch.setattr(service, "get_matcher", lambda: FakeMatcher())
    response = client.post(
        "/match",
        files={"file": ("astronaut.jpg", FACE_SAMPLE.read_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200
    assert response.json()["match"]["id"] == "22"


def test_match_reaches_ranker_for_two_real_faces(monkeypatch):
    class FakeMatcher:
        def rank(self, _image):
            return [(service.IMAGES / "22.png", 0.9)]

    monkeypatch.setattr(service, "get_matcher", lambda: FakeMatcher())
    face = Image.open(FACE_SAMPLE).convert("RGB")
    double = Image.new("RGB", (face.width * 2, face.height))
    double.paste(face, (0, 0))
    double.paste(face, (face.width, 0))
    image_bytes = BytesIO()
    double.save(image_bytes, format="PNG")

    response = client.post(
        "/match",
        files={"file": ("two-faces.png", image_bytes.getvalue(), "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["match"]["id"] == "22"


def test_mock_generation_returns_uploaded_photo_url_without_provider(monkeypatch, tmp_path):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setattr(service, "RESULTS", tmp_path)
    photo = BytesIO()
    Image.new("RGB", (64, 64), (23, 145, 201)).save(photo, format="PNG")

    response = client.post(
        "/generate",
        data={"fishId": "22"},
        files={"file": ("portrait.png", photo.getvalue(), "image/png")},
    )

    assert response.status_code == 200
    result = client.get(response.json()["imageUrl"])
    assert result.status_code == 200
    assert Image.open(BytesIO(result.content)).getpixel((20, 20)) == (23, 145, 201)


def test_generate_rejects_invalid_fish_id(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "RESULTS", tmp_path)
    photo = BytesIO()
    Image.new("RGB", (64, 64), "green").save(photo, format="PNG")
    response = client.post(
        "/generate",
        data={"fishId": "01"},
        files={"file": ("portrait.png", photo.getvalue(), "image/png")},
    )
    assert response.status_code == 400


def test_mock_generation_accepts_image_larger_than_old_8mb_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "RESULTS", tmp_path)
    photo = BytesIO()
    Image.new("RGB", (64, 64), "green").save(photo, format="PNG")
    padded_photo = photo.getvalue() + b"\0" * (9 * 1024 * 1024)

    response = client.post(
        "/generate",
        data={"fishId": "22"},
        files={"file": ("portrait.png", padded_photo, "image/png")},
    )
    assert response.status_code == 200
