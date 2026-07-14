from __future__ import annotations

import io

from PIL import Image
from sqlalchemy import select

from app.image_generation import GeneratedImage
from app.image_worker import DurableImageWorker
from app.models import Asset, ImageGeneration


def _generated_result() -> GeneratedImage:
    output = io.BytesIO()
    Image.new("RGB", (1280, 720), (32, 96, 160)).save(output, format="PNG")
    return GeneratedImage(
        png_bytes=output.getvalue(),
        width=1280,
        height=720,
        provider="openai-compatible",
        model="unit-image-model",
        duration_ms=8,
        usage={},
    )


def test_project_delete_fences_generations_and_preserves_accepted_asset(
    runtime, monkeypatch
):
    client, core_worker, database, settings = runtime
    settings.image_api_base_url = "https://images.example.invalid/v1"
    settings.image_api_key = "unit-test-only"
    settings.image_model = "unit-image-model"
    settings.image_daily_limit = 0
    settings.image_max_pending = 20

    created = client.post(
        "/api/v1/projects/text",
        json={"title": "删除项目生图栅栏", "text": "绿色城市与公共交通。"},
        headers={"Idempotency-Key": "project-delete-image-project"},
    )
    assert created.status_code == 202, created.text
    assert core_worker.run_once() is True
    project_id = created.json()["project"]["id"]
    segment = client.get(f"/api/v1/projects/{project_id}").json()["segments"][0]

    accepted = client.post(
        f"/api/v1/segments/{segment['id']}/image-generations",
        json={"auto_import": True, "auto_select": True, "aspect_ratio": "16:9"},
        headers={"Idempotency-Key": "project-delete-image-accepted"},
    )
    assert accepted.status_code == 202, accepted.text

    provider_calls = 0

    def fake_generate(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return _generated_result()

    monkeypatch.setattr("app.image_worker.generate_image", fake_generate)
    image_worker = DurableImageWorker(database, settings, worker_id="delete-test-image")
    assert image_worker.run_once() is True
    accepted_detail = client.get(
        f"/api/v1/image-generations/{accepted.json()['generation']['id']}"
    ).json()
    asset_id = accepted_detail["asset"]["id"]

    queued = client.post(
        f"/api/v1/segments/{segment['id']}/image-generations",
        json={"auto_import": True, "auto_select": True, "aspect_ratio": "16:9"},
        headers={"Idempotency-Key": "project-delete-image-queued"},
    )
    assert queued.status_code == 202, queued.text
    queued_id = queued.json()["generation"]["id"]
    staging = (
        settings.data_dir
        / "private"
        / "image-generations"
        / "staging"
        / queued_id
    )
    staging.mkdir(parents=True)
    (staging / "pending.submitted.json").write_text("{}", encoding="utf-8")

    deleted = client.delete(f"/api/v1/projects/{project_id}")
    assert deleted.status_code == 204, deleted.text

    with database.session() as session:
        assert session.scalars(
            select(ImageGeneration).where(ImageGeneration.project_id == project_id)
        ).all() == []
        assert session.get(ImageGeneration, queued_id) is None
        assert session.get(Asset, asset_id) is not None
    assert not staging.exists()
    assert image_worker.run_once() is False
    assert provider_calls == 1
    assert any(item["id"] == asset_id for item in client.get("/api/v1/assets").json()["items"])
