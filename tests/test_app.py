import base64
from datetime import datetime, timedelta, timezone as dt_timezone
from io import BytesIO
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.error import URLError

import admin_stats as stats_admin
from admin_stats import TZ_CN
from fastapi.testclient import TestClient
from PIL import Image
import numpy as np
import pytest

import app as service
import runtime_config
from match_engine import EXTRACTOR, MATCHER, FaceProblem


client = TestClient(service.app)
FACE_SAMPLE = Path(__file__).parent / "fixtures" / "astronaut-face.jpg"


@pytest.fixture(autouse=True)
def authenticated_generation(monkeypatch, tmp_path):
    monkeypatch.delenv("WIDGET_AUTH_ENABLED", raising=False)
    monkeypatch.setenv("GENERATION_DB_PATH", str(tmp_path.parent / f"{tmp_path.name}-quota.sqlite3"))
    monkeypatch.setenv("RUNTIME_CONFIG_PATH", str(tmp_path.parent / f"{tmp_path.name}-runtime-config.json"))
    monkeypatch.setattr(service.widget_auth, "require_open_id", lambda _header: "existing-test-user")


def force_runtime_config(monkeypatch, **overrides):
    real_get = runtime_config.get
    monkeypatch.setattr(runtime_config, "get", lambda key: overrides.get(key, real_get(key)))


def upload(content, filename="portrait.jpg", content_type="image/jpeg"):
    return client.post("/match", files={"file": (filename, content, content_type)})


def test_catalog_has_40_valid_ids_and_images():
    assert MATCHER.ids == [f"{i:02d}" for i in range(2, 42)]
    for fish_id in MATCHER.ids:
        with Image.open(service.IMAGES / f"{fish_id}.webp") as image:
            image.verify()
    assert len(service.FISH) == 40


def test_rejects_non_image_and_corrupt_image():
    response = upload(b"hello", "note.txt", "text/plain")
    assert response.status_code == 400
    assert response.json()["detail"] == "请上传 JPG、PNG 或 WebP 照片"
    response = upload(b"broken jpg")
    assert response.status_code == 400
    assert response.json()["detail"] == "无法读取这张照片"


def test_no_face_rejected_only_when_gate_enabled(monkeypatch):
    photo = BytesIO()
    Image.new("RGB", (512, 512), "white").save(photo, format="PNG")
    force_runtime_config(monkeypatch, face_check_enabled=True)
    response = upload(photo.getvalue(), "blank.png", "image/png")
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "NO_FACE", "message": "请上传一张包含人脸的图片"}


def test_face_gate_off_by_default_still_matches_whole_image():
    photo = BytesIO()
    Image.new("RGB", (512, 512), "white").save(photo, format="PNG")
    response = upload(photo.getvalue(), "blank.png", "image/png")
    assert response.status_code == 200
    data = response.json()
    ids = [data["match"]["id"]] + [item["id"] for item in data["alternatives"]]
    assert len(ids) == 3
    assert all(fish_id in MATCHER.ids for fish_id in ids)


def test_yunet_detects_single_and_two_faces():
    single = Image.open(FACE_SAMPLE).convert("RGB")
    double = Image.new("RGB", (single.width * 2, single.height))
    double.paste(single, (0, 0))
    double.paste(single, (single.width, 0))
    assert EXTRACTOR.detect(single)[1] == 1
    assert EXTRACTOR.detect(double)[1] == 2


def test_multiple_faces_selects_largest_then_center(monkeypatch):
    face = np.array([5, 5, 80, 80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, .95], dtype=np.float32)
    large = face.copy(); large[:4] = [180, 180, 100, 100]
    centered = large.copy(); centered[:2] = [200, 200]
    class Detector:
        def setInputSize(self, _size): pass
        def detect(self, _frame): return None, np.stack([face, large, centered])
    monkeypatch.setattr(EXTRACTOR, "detector", Detector())
    selected, count = EXTRACTOR.detect(Image.new("RGB", (500, 500)))
    assert count == 3
    assert selected[0] == 200


def test_weak_face_keeps_real_landmarks_when_gate_off(monkeypatch):
    face = np.array([5, 5, 80, 80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, .1], dtype=np.float32)
    class Detector:
        def setInputSize(self, _size): pass
        def detect(self, _frame): return None, np.array([face])
    monkeypatch.setattr(EXTRACTOR, "detector", Detector())
    selected, count = EXTRACTOR.detect(Image.new("RGB", (500, 500), "white"))
    assert count == 1
    assert selected[14] == pytest.approx(0.1)


def test_weak_face_rejected_when_gate_enabled(monkeypatch):
    face = np.array([5, 5, 80, 80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, .1], dtype=np.float32)
    class Detector:
        def setInputSize(self, _size): pass
        def detect(self, _frame): return None, np.array([face])
    monkeypatch.setattr(EXTRACTOR, "detector", Detector())
    force_runtime_config(monkeypatch, face_check_enabled=True)
    with pytest.raises(FaceProblem) as error:
        EXTRACTOR.detect(Image.new("RGB", (500, 500), "white"))
    assert error.value.code == "LOW_CONFIDENCE"


def test_real_face_returns_frontend_compatible_shape():
    response = upload(FACE_SAMPLE.read_bytes(), "astronaut.jpg")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"match", "alternatives"}
    assert set(data["match"]) == {"id", "imageUrl"}
    assert len(data["alternatives"]) == 2
    ids = [data["match"]["id"]] + [item["id"] for item in data["alternatives"]]
    assert len(set(ids)) == 3
    assert all(fish_id in MATCHER.ids for fish_id in ids)
    assert data["match"]["imageUrl"].endswith(f"/{ids[0]}.webp")
    assert all(name in response.headers["server-timing"] for name in ("decode", "face", "features", "score", "total"))


def test_two_faces_still_returns_one_match():
    single = Image.open(FACE_SAMPLE).convert("RGB")
    double = Image.new("RGB", (single.width * 2, single.height))
    double.paste(single, (0, 0))
    double.paste(single, (single.width, 0))
    photo = BytesIO(); double.save(photo, "JPEG")
    response = upload(photo.getvalue())
    assert response.status_code == 200
    assert response.json()["match"]["id"] in MATCHER.ids


def test_low_confidence_is_422(monkeypatch):
    def reject(_image):
        raise FaceProblem("LOW_CONFIDENCE", "人脸不够清晰，请换一张正面照片")
    monkeypatch.setattr(service, "match_photo", reject)
    response = upload(FACE_SAMPLE.read_bytes())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "LOW_CONFIDENCE"


def test_generation_requires_ark_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    photo = BytesIO()
    Image.new("RGB", (64, 64), (23, 145, 201)).save(photo, format="PNG")

    response = client.post(
        "/generate",
        data={"fishId": "22", "openid": "existing-test-user"},
        files={"file": ("portrait.png", photo.getvalue(), "image/png")},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "生图服务尚未配置"
    assert not list(tmp_path.iterdir())


def test_generation_sends_portrait_first_and_fish_second(monkeypatch, tmp_path):
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    portrait = BytesIO()
    Image.new("RGB", (64, 64), (23, 145, 201)).save(portrait, format="PNG")
    generated = BytesIO()
    Image.new("RGB", (64, 64), (201, 45, 23)).save(generated, format="PNG")
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        if len(requests) == 1:
            return BytesIO(
                json.dumps({"data": [{"url": "https://example.test/generated.png"}]}).encode()
            )
        assert request == "https://example.test/generated.png"
        return BytesIO(generated.getvalue())

    monkeypatch.setattr(service, "urlopen", fake_urlopen)
    response = client.post(
        "/generate",
        data={"fishId": "22", "openid": "existing-test-user"},
        files={"file": ("portrait.png", portrait.getvalue(), "image/png")},
    )

    assert response.status_code == 200
    assert "mock" not in response.json()
    request, _timeout = requests[0]
    assert request.full_url == service.ARK_URL
    assert request.get_header("Authorization") == "Bearer test-key"
    payload = json.loads(request.data)
    assert payload["model"] == "doubao-seedream-5-0-flash-260915"
    assert payload["prompt"] == service.GENERATION_PROMPT
    assert payload["response_format"] == "url"
    assert payload["size"] == "2K"
    assert payload["stream"] is False
    assert payload["watermark"] is False
    assert len(payload["image"]) == 2
    assert payload["image"][0].startswith("data:image/jpeg;base64,")
    assert payload["image"][1].startswith("data:image/png;base64,")
    image_one = base64.b64decode(payload["image"][0].split(",", 1)[1])
    image_two = base64.b64decode(payload["image"][1].split(",", 1)[1])
    assert Image.open(BytesIO(image_one)).size == (64, 64)
    assert image_two == (service.IMAGES / "22.png").read_bytes()
    result = client.get(response.json()["imageUrl"])
    assert result.status_code == 200
    assert Image.open(BytesIO(result.content)).getpixel((20, 20)) == (201, 45, 23)
    token = response.json()["imageUrl"].rsplit("/", 1)[-1]
    metadata = json.loads((tmp_path / f"{token}.json").read_text(encoding="utf-8"))
    assert metadata["fishId"] == "22"
    assert metadata["prompt"] == service.GENERATION_PROMPT
    assert metadata["imageOrder"] == ["uploaded_portrait", "images/22.png"]
    assert metadata["size"] == {"width": 64, "height": 64}


def test_generation_provider_failure_returns_error_without_saving_photo(monkeypatch, tmp_path):
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    monkeypatch.setattr(
        service, "urlopen", lambda _request, timeout: (_ for _ in ()).throw(URLError("offline"))
    )
    portrait = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(portrait, format="PNG")

    response = client.post(
        "/generate",
        data={"fishId": "22", "openid": "existing-test-user"},
        files={"file": ("portrait.png", portrait.getvalue(), "image/png")},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "图像生成失败，请稍后重试"
    assert not list(tmp_path.iterdir())


def test_generate_rejects_invalid_fish_id(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    photo = BytesIO()
    Image.new("RGB", (64, 64), "green").save(photo, format="PNG")
    response = client.post(
        "/generate",
        data={"fishId": "01", "openid": "existing-test-user"},
        files={"file": ("portrait.png", photo.getvalue(), "image/png")},
    )
    assert response.status_code == 400


def test_generation_accepts_image_larger_than_old_8mb_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    monkeypatch.setattr(
        service, "generate_with_ark", lambda _portrait, _fish: Image.new("RGB", (8, 8), "red")
    )
    photo = BytesIO()
    Image.new("RGB", (64, 64), "green").save(photo, format="PNG")
    padded_photo = photo.getvalue() + b"\0" * (9 * 1024 * 1024)

    response = client.post(
        "/generate",
        data={"fishId": "22", "openid": "existing-test-user"},
        files={"file": ("portrait.png", padded_photo, "image/png")},
    )
    assert response.status_code == 200


def test_archived_result_remains_available_after_24_hours(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    image = Image.new("RGB", (16, 16), "green")
    token = service.archive_generated_image(image, "22")
    path = tmp_path / f"{token}.png"
    old_timestamp = path.stat().st_mtime - 3 * 24 * 60 * 60
    os.utime(path, (old_timestamp, old_timestamp))

    response = client.get(f"/result/{token}")
    assert response.status_code == 200
    assert Image.open(BytesIO(response.content)).getpixel((8, 8)) == (0, 128, 0)


def archive_record(directory, token, created_cst, fish_id="22", vid=None, ip_hash=None):
    metadata = {
        "id": token,
        "createdAt": created_cst.astimezone(dt_timezone.utc).isoformat(),
        "fishId": fish_id,
        "vid": vid,
        "ipHash": ip_hash,
    }
    (directory / f"{token}.json").write_text(json.dumps(metadata), encoding="utf-8")
    Image.new("RGB", (8, 8), "red").save(directory / f"{token}.png", format="PNG")


def admin_client(monkeypatch, password="pw-secret"):
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    fresh = TestClient(service.app)
    fresh.post("/admin/login", data={"password": password})
    return fresh


def test_admin_requires_login(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-secret")
    fresh = TestClient(service.app)

    page = fresh.get("/admin")
    assert page.status_code == 200
    assert 'name="password"' in page.text
    assert 'id="cards"' not in page.text
    assert fresh.get("/admin/api/summary").status_code == 401
    assert fresh.get("/admin/api/timeseries").status_code == 401
    assert fresh.get("/admin/api/images").status_code == 401


def test_admin_default_password_when_env_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    async def instant(_seconds):
        pass

    monkeypatch.setattr(stats_admin.asyncio, "sleep", instant)
    fresh = TestClient(service.app)
    fresh.post("/admin/login", data={"password": "not-the-default"})
    assert fresh.get("/admin/api/summary").status_code == 401

    fresh = TestClient(service.app)
    assert fresh.post("/admin/login", data={"password": "xiawang123"}).status_code == 200
    assert fresh.get("/admin/api/summary").status_code == 200


def test_admin_login_and_logout_flow(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-secret")

    async def instant(_seconds):
        pass

    monkeypatch.setattr(stats_admin.asyncio, "sleep", instant)
    fresh = TestClient(service.app)

    wrong = fresh.post("/admin/login", data={"password": "not-the-password"})
    assert wrong.status_code == 401
    assert "密码错误" in wrong.text

    # TestClient 默认跟随重定向：登录成功后最终落到仪表盘页面
    good = fresh.post("/admin/login", data={"password": "pw-secret"})
    assert good.status_code == 200
    assert 'id="cards"' in good.text
    assert fresh.get("/admin/api/summary").status_code == 200

    fresh.post("/admin/logout")
    assert 'name="password"' in fresh.get("/admin").text
    assert fresh.get("/admin/api/summary").status_code == 401


def test_admin_summary_and_timeseries_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    monkeypatch.setattr(stats_admin, "ARCHIVE", tmp_path)
    now = datetime.now(TZ_CN)
    noon_today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    hour_25_ago = now - timedelta(hours=25)
    hour_49_ago = now - timedelta(hours=49)
    archive_record(tmp_path, "a1" * 16, hour_25_ago, vid="visitor-a")
    archive_record(tmp_path, "a2" * 16, hour_49_ago, vid="visitor-a")
    archive_record(tmp_path, "b1" * 16, noon_today, vid="visitor-b")
    archive_record(tmp_path, "b2" * 16, noon_today, vid="visitor-b")
    archive_record(
        tmp_path, "c1" * 16, noon_today - timedelta(days=3), vid=None, ip_hash="ab" * 8
    )

    fresh = admin_client(monkeypatch)
    summary = fresh.get("/admin/api/summary").json()
    assert summary["totalImages"] == 5
    # visitor-a、visitor-b、加上一条只有 IP 哈希的记录
    assert summary["totalParticipants"] == 3
    assert summary["todayImages"] == 2
    assert summary["todayParticipants"] == 1
    assert summary["latestAt"] is not None

    series = fresh.get("/admin/api/timeseries?hours=72&days=30").json()
    by_day = {item["label"]: item for item in series["daily"]}
    today_label = noon_today.strftime("%m-%d")
    assert by_day[today_label]["images"] == 2
    assert by_day[today_label]["participants"] == 1
    assert by_day[hour_25_ago.strftime("%m-%d")]["images"] == 1
    assert by_day[(noon_today - timedelta(days=3)).strftime("%m-%d")]["images"] == 1

    by_hour = {item["label"]: item for item in series["hourly"]}
    assert by_hour[hour_25_ago.replace(minute=0, second=0, microsecond=0).strftime("%m-%d %H时")][
        "images"
    ] == 1
    assert by_hour[hour_49_ago.replace(minute=0, second=0, microsecond=0).strftime("%m-%d %H时")][
        "participants"
    ] == 1


def test_admin_images_listing_and_thumbnails(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    monkeypatch.setattr(stats_admin, "ARCHIVE", tmp_path)
    now = datetime.now(TZ_CN)
    for index in range(3):
        archive_record(tmp_path, f"{index:02x}" * 16, now - timedelta(hours=index))

    fresh = admin_client(monkeypatch)
    data = fresh.get("/admin/api/images?page=1&pageSize=2").json()
    assert data["total"] == 3
    assert data["totalPages"] == 2
    assert [item["token"] for item in data["items"]] == ["00" * 16, "01" * 16]
    assert data["items"][0]["url"].endswith("/result/" + "00" * 16)
    assert data["items"][0]["thumbnail"] is None

    second_page = fresh.get("/admin/api/images?page=2&pageSize=2").json()
    assert [item["token"] for item in second_page["items"]] == ["02" * 16]

    missing = fresh.get(f"/admin/thumb/{'00' * 16}")
    assert missing.status_code == 404
    Image.new("RGB", (64, 64), "red").save(tmp_path / f"{'00' * 16}.thumb.jpg", format="JPEG")
    found = fresh.get(f"/admin/thumb/{'00' * 16}")
    assert found.status_code == 200

    listing = fresh.get("/admin/api/images?page=1&pageSize=2").json()
    assert listing["items"][0]["thumbnail"].endswith(f"/admin/thumb/{'00' * 16}")

    unauthenticated = TestClient(service.app)
    assert unauthenticated.get("/admin/api/images").status_code == 401


def test_archive_generated_image_creates_thumbnail_and_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    token = service.archive_generated_image(
        Image.new("RGB", (640, 480), "green"), "22", vid="visitor-a", ip_hash="ab" * 8
    )

    thumbnail = Image.open(tmp_path / f"{token}.thumb.jpg")
    assert max(thumbnail.size) <= 320
    metadata = json.loads((tmp_path / f"{token}.json").read_text(encoding="utf-8"))
    assert metadata["vid"] == "visitor-a"
    assert metadata["ipHash"] == "ab" * 8


def test_generate_records_participant_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    generated = BytesIO()
    Image.new("RGB", (64, 64), (201, 45, 23)).save(generated, format="PNG")

    def fake_urlopen(request, timeout):
        if getattr(request, "full_url", request) == service.ARK_URL:
            return BytesIO(
                json.dumps({"data": [{"url": "https://example.test/generated.png"}]}).encode()
            )
        return BytesIO(generated.getvalue())

    monkeypatch.setattr(service, "urlopen", fake_urlopen)
    photo = BytesIO()
    Image.new("RGB", (64, 64), (23, 145, 201)).save(photo, format="PNG")

    fresh = TestClient(service.app)
    fresh.cookies.set("vid", "visitor-123")
    response = fresh.post(
        "/generate",
        data={"fishId": "22", "openid": "existing-test-user"},
        files={"file": ("portrait.png", photo.getvalue(), "image/png")},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )

    assert response.status_code == 200
    token = response.json()["imageUrl"].rsplit("/", 1)[-1]
    metadata = json.loads((tmp_path / f"{token}.json").read_text(encoding="utf-8"))
    assert metadata["vid"] == "visitor-123"
    assert metadata["ipHash"] == sha256(b"203.0.113.9").hexdigest()[:16]


def test_demo_sets_anonymous_visitor_cookie():
    fresh = TestClient(service.app)
    response = fresh.get("/")
    assert "vid=" in response.headers.get("set-cookie", "")


def test_admin_export_csvs(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "ARCHIVE", tmp_path)
    monkeypatch.setattr(stats_admin, "ARCHIVE", tmp_path)
    now = datetime.now(TZ_CN)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    archive_record(tmp_path, "a1" * 16, noon, vid="visitor-a", fish_id="22")
    archive_record(
        tmp_path, "a2" * 16, noon + timedelta(minutes=30), vid="visitor-b", fish_id="07"
    )
    archive_record(tmp_path, "b1" * 16, noon - timedelta(days=1), vid="visitor-a")

    fresh = admin_client(monkeypatch)

    daily = fresh.get("/admin/export/daily.csv")
    assert daily.status_code == 200
    assert "text/csv" in daily.headers["content-type"]
    assert "attachment" in daily.headers["content-disposition"]
    daily_lines = daily.content.decode("utf-8-sig").strip().splitlines()
    assert daily_lines[0] == "日期,参与人数,生成张数"
    assert daily_lines[1] == f"{(noon - timedelta(days=1)).date().isoformat()},1,1"
    assert daily_lines[2] == f"{noon.date().isoformat()},2,2"

    hourly_lines = fresh.get("/admin/export/hourly.csv").content.decode("utf-8-sig").strip().splitlines()
    assert hourly_lines[0] == "时间段,参与人数,生成张数"
    assert len(hourly_lines) == 3  # 两天各一个非空时段 + 表头
    assert hourly_lines[1].endswith(",1,1")
    assert hourly_lines[2].endswith(",2,2")

    record_lines = fresh.get("/admin/export/records.csv").content.decode("utf-8-sig").strip().splitlines()
    assert record_lines[0] == "生成时间(北京时间),鱼编号,图片链接,访客标识"
    assert "/result/" + "b1" * 16 in record_lines[1]
    assert "/result/" + "a1" * 16 in record_lines[2]
    assert "22" in record_lines[2] and "visitor-a" in record_lines[2]

    assert TestClient(service.app).get("/admin/export/daily.csv").status_code == 401
