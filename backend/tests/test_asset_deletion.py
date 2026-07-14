from __future__ import annotations

import base64
from pathlib import Path

from app.models import Asset


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
TEXT = "人工智能帮助团队提升效率，同时需要可靠的数据安全与可解释素材匹配。"


def _upload(client, name: str = "待删除上传素材") -> dict:
    response = client.post(
        "/api/v1/assets",
        data={"name": name, "tags": "测试,删除", "keywords": "上传,清理"},
        files={"file": ("deletable.png", PNG, "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_uploaded_asset_delete_removes_record_file_and_records_audit(runtime):
    client, _worker, database, _settings = runtime
    uploaded = _upload(client)
    with database.session() as session:
        asset = session.get(Asset, uploaded["id"])
        storage_path = Path(asset.storage_path)
        poster_path = storage_path.with_name(f"{storage_path.stem}-poster.jpg")
        poster_path.write_bytes(b"generated-poster")
        asset.thumbnail_storage_path = str(poster_path)
    assert storage_path.is_file()
    assert poster_path.is_file()

    deleted = client.delete(f"/api/v1/assets/{uploaded['id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(uploaded["file_url"]).status_code == 404
    assert not storage_path.exists()
    assert not poster_path.exists()
    with database.session() as session:
        assert session.get(Asset, uploaded["id"]) is None

    audit = client.get("/api/v1/audit").json()["items"]
    event = next(item for item in audit if item["action"] == "asset.deleted")
    assert event["entity_id"] == uploaded["id"]
    assert event["before"]["name"] == "待删除上传素材"


def test_asset_delete_protects_seed_and_selected_assets(runtime):
    client, worker, _database, _settings = runtime
    protected = client.delete("/api/v1/assets/seed-technology")
    assert protected.status_code == 409
    assert protected.json()["code"] == "SEED_ASSET_PROTECTED"

    uploaded = _upload(client, "项目正在使用的素材")
    created = client.post(
        "/api/v1/projects/text",
        json={"title": "素材删除保护", "text": TEXT},
        headers={"Idempotency-Key": "asset-delete-in-use"},
    )
    assert created.status_code == 202, created.text
    assert worker.run_once() is True
    project_id = created.json()["project"]["id"]
    project = client.get(f"/api/v1/projects/{project_id}").json()
    segment_id = project["segments"][0]["id"]
    selected = client.put(
        f"/api/v1/segments/{segment_id}/selection",
        json={"asset_id": uploaded["id"]},
    )
    assert selected.status_code == 200, selected.text

    conflict = client.delete(f"/api/v1/assets/{uploaded['id']}")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ASSET_IN_USE"
    assert conflict.json()["details"]["selection_count"] == 1


def test_asset_delete_returns_not_found(runtime):
    client, _worker, _database, _settings = runtime
    response = client.delete("/api/v1/assets/missing-asset")
    assert response.status_code == 404
    assert response.json()["code"] == "ASSET_NOT_FOUND"
