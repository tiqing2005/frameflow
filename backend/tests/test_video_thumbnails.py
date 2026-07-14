from __future__ import annotations

import subprocess
from pathlib import Path

from sqlalchemy import select

from app.models import Asset
from app.thumbnails import VIDEO_THUMBNAIL_PLACEHOLDER_URL


MP4_HEADER = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 256
JPEG = b"\xff\xd8\xff" + b"poster" * 32 + b"\xff\xd9"


def test_seed_videos_expose_persisted_image_posters(runtime):
    client, _worker, database, _settings = runtime
    videos = client.get("/api/v1/assets?kind=video").json()["items"]
    assert len(videos) >= 6
    assert all(item["thumbnail_url"] != item["file_url"] for item in videos)
    assert all(item["thumbnail_mime_type"] == "image/jpeg" for item in videos)

    poster = client.get(videos[0]["thumbnail_url"])
    assert poster.status_code == 200
    assert poster.headers["content-type"].startswith("image/jpeg")
    assert poster.content.startswith(b"\xff\xd8\xff")

    with database.session() as session:
        assets = session.scalars(select(Asset).where(Asset.kind == "video")).all()
        assert all(asset.thumbnail_storage_path for asset in assets)
        assert all(Path(asset.thumbnail_storage_path).is_file() for asset in assets)


def test_uploaded_video_generates_thumbnail_once_and_reports_canonical_mime(runtime, monkeypatch):
    client, _worker, database, _settings = runtime
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        Path(command[-1]).write_bytes(JPEG)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.thumbnails.subprocess.run", fake_run)
    uploaded = client.post(
        "/api/v1/assets",
        data={"name": "上传视频封面", "tags": "演示", "keywords": "视频"},
        files={"file": ("demo.mp4", MP4_HEADER, "application/octet-stream")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = uploaded.json()
    assert payload["mime_type"] == "video/mp4"
    assert payload["thumbnail_mime_type"] == "image/jpeg"
    assert payload["thumbnail_url"] != payload["file_url"]
    assert len(calls) == 1

    poster = client.get(payload["thumbnail_url"])
    assert poster.status_code == 200
    assert poster.headers["content-type"].startswith("image/jpeg")
    assert poster.content == JPEG
    client.get("/api/v1/assets?kind=video")
    client.get(payload["thumbnail_url"])
    assert len(calls) == 1

    with database.session() as session:
        asset = session.get(Asset, payload["id"])
        assert Path(asset.thumbnail_storage_path).is_file()


def test_thumbnail_failure_uses_image_fallback_instead_of_mp4(runtime, monkeypatch):
    client, _worker, database, _settings = runtime

    def missing_ffmpeg(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr("app.thumbnails.subprocess.run", missing_ffmpeg)
    uploaded = client.post(
        "/api/v1/assets",
        data={"name": "无 ffmpeg 视频", "tags": "演示", "keywords": "回退"},
        files={"file": ("fallback.mp4", MP4_HEADER, "video/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = uploaded.json()
    assert payload["thumbnail_url"] == VIDEO_THUMBNAIL_PLACEHOLDER_URL
    assert payload["thumbnail_url"] != payload["file_url"]
    assert payload["thumbnail_mime_type"] == "image/svg+xml"
    poster = client.get(payload["thumbnail_url"])
    assert poster.status_code == 200
    assert poster.headers["content-type"].startswith("image/svg+xml")
    assert poster.content.startswith(b"<svg")

    with database.session() as session:
        asset = session.get(Asset, payload["id"])
        assert asset.thumbnail_storage_path is None
