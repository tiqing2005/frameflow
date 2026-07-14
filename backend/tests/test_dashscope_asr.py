from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.asr import create_asr_source_token, resolve_asr_source_token, transcribe_file


def test_signed_asr_source_is_retrievable(runtime):
    client, _worker, _database, settings = runtime
    settings.dashscope_signing_secret = "test-signing-secret"
    source = settings.data_dir / "private" / "sources" / "sample.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"RIFF-test")
    token = create_asr_source_token(source, settings)
    assert resolve_asr_source_token(token, settings) == source
    response = client.get(f"/api/v1/asr/source/{token}")
    assert response.status_code == 200
    assert response.content == b"RIFF-test"
    assert client.get(f"/api/v1/asr/source/{token}x").status_code == 404


def test_dashscope_submit_poll_and_download(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    settings.asr_provider = "dashscope"
    settings.asr_model = "paraformer-v2"
    settings.asr_timeout = 5
    settings.dashscope_api_key = "test-key"
    settings.dashscope_base_url = "https://dashscope.test/api/v1"
    settings.dashscope_public_base_url = "https://frameflow.test"
    settings.dashscope_signing_secret = "test-signing-secret"
    settings.dashscope_poll_seconds = 0.01
    source = settings.data_dir / "private" / "sources" / "sample.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"RIFF-test")
    seen_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        if request.url.path.endswith("/services/audio/asr/transcription"):
            body = json.loads(request.content)
            seen_url = body["input"]["file_urls"][0]
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.path.endswith("/tasks/task-1"):
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", "results": [{"transcription_url": "https://result.test/final.json"}]}})
        if request.url.host == "result.test":
            return httpx.Response(200, json={"transcripts": [{"text": "这是阿里百炼返回的字幕。"}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("app.asr.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.asr.httpx.Client",
        lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
    )
    text, provider = transcribe_file(source, "audio/wav", settings)
    assert text == "这是阿里百炼返回的字幕。"
    assert provider == "dashscope/paraformer-v2"
    assert seen_url.startswith("https://frameflow.test/api/v1/asr/source/")
