from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app.asr import (
    TranscriptionError,
    _dashscope_submit_and_wait,
    create_asr_source_token,
    resolve_asr_source_token,
    transcribe_file,
)


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
    prepared_path: Path | None = None
    ffmpeg_command: list[str] = []

    def fake_ffmpeg(command, **_kwargs):
        ffmpeg_command.extend(command)
        Path(command[-1]).write_bytes(b"ID3-compact-audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url, prepared_path
        if request.url.path.endswith("/services/audio/asr/transcription"):
            body = json.loads(request.content)
            seen_url = body["input"]["file_urls"][0]
            token = seen_url.rsplit("/", 1)[-1]
            prepared_path = resolve_asr_source_token(token, settings)
            assert prepared_path is not None
            assert prepared_path.parent == settings.data_dir / "private" / "asr-staging"
            assert prepared_path.suffix == ".mp3"
            assert prepared_path.read_bytes() == b"ID3-compact-audio"
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.path.endswith("/tasks/task-1"):
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED", "results": [{"transcription_url": "https://result.test/final.json"}]}})
        if request.url.host == "result.test":
            return httpx.Response(200, json={"transcripts": [{"text": "这是阿里百炼返回的字幕。"}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("app.asr.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.asr._ffmpeg_executable", lambda: "ffmpeg-test")
    monkeypatch.setattr("app.asr.subprocess.run", fake_ffmpeg)
    monkeypatch.setattr(
        "app.asr.httpx.Client",
        lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
    )
    text, provider = transcribe_file(source, "audio/wav", settings)
    assert text == "这是阿里百炼返回的字幕。"
    assert provider == "dashscope/paraformer-v2"
    assert seen_url.startswith("https://frameflow.test/api/v1/asr/source/")
    assert prepared_path is not None and not prepared_path.exists()
    assert source.read_bytes() == b"RIFF-test"
    assert ffmpeg_command[0] == "ffmpeg-test"
    assert ffmpeg_command[ffmpeg_command.index("-ac") + 1] == "1"
    assert ffmpeg_command[ffmpeg_command.index("-ar") + 1] == "16000"
    assert ffmpeg_command[ffmpeg_command.index("-b:a") + 1] == "24k"
    assert Path(ffmpeg_command[-1]).name.startswith(".")
    assert Path(ffmpeg_command[-1]).name.endswith(".tmp.mp3")


def test_dashscope_ffmpeg_failure_is_explicit_and_cleans_partial_file(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    settings.asr_provider = "dashscope"
    settings.asr_model = "paraformer-v2"
    settings.dashscope_api_key = "test-key"
    settings.dashscope_public_base_url = "https://frameflow.test"
    settings.dashscope_signing_secret = "test-signing-secret"
    source = settings.data_dir / "private" / "sources" / "no-audio.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"not-a-real-video")

    def failed_ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, "", "no audio stream")

    monkeypatch.setattr("app.asr._ffmpeg_executable", lambda: "ffmpeg-test")
    monkeypatch.setattr("app.asr.subprocess.run", failed_ffmpeg)

    with pytest.raises(TranscriptionError) as captured:
        transcribe_file(source, "video/mp4", settings)

    assert captured.value.code == "ASR_MEDIA_PREPROCESSING_FAILED"
    assert captured.value.category == "input"
    assert captured.value.retryable is False
    staging_dir = settings.data_dir / "private" / "asr-staging"
    assert list(staging_dir.iterdir()) == []
    assert source.exists()


def test_dashscope_provider_failure_still_removes_prepared_audio(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    settings.asr_provider = "dashscope"
    settings.asr_model = "paraformer-v2"
    settings.dashscope_api_key = "test-key"
    settings.dashscope_base_url = "https://dashscope.test/api/v1"
    settings.dashscope_public_base_url = "https://frameflow.test"
    settings.dashscope_signing_secret = "test-signing-secret"
    source = settings.data_dir / "private" / "sources" / "sample.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"RIFF-test")

    def fake_ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"ID3-compact-audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    transport = httpx.MockTransport(lambda _request: httpx.Response(503, text="unavailable"))
    real_client = httpx.Client
    monkeypatch.setattr("app.asr._ffmpeg_executable", lambda: "ffmpeg-test")
    monkeypatch.setattr("app.asr.subprocess.run", fake_ffmpeg)
    monkeypatch.setattr(
        "app.asr.httpx.Client",
        lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
    )

    with pytest.raises(TranscriptionError) as captured:
        transcribe_file(source, "audio/wav", settings)

    assert captured.value.code == "ASR_PROVIDER_UNAVAILABLE"
    staging_dir = settings.data_dir / "private" / "asr-staging"
    assert list(staging_dir.iterdir()) == []


def _configure_dashscope(settings, source: Path) -> None:
    settings.asr_model = "paraformer-v2"
    settings.asr_timeout = 5
    settings.dashscope_api_key = "test-key"
    settings.dashscope_base_url = "https://dashscope.test/api/v1"
    settings.dashscope_public_base_url = "https://frameflow.test"
    settings.dashscope_signing_secret = "test-signing-secret"
    settings.dashscope_poll_seconds = 0.01
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"ID3-test")


def _mock_dashscope_client(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("app.asr.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.asr.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )


def test_dashscope_download_failure_is_retryable_and_preserves_provider_detail(
    runtime, monkeypatch
):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(
                200,
                json={
                    "request_id": "request-submit",
                    "output": {"task_id": "task-download"},
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "request-poll",
                "output": {
                    "task_status": "FAILED",
                    "code": "InvalidFile.DownloadFile",
                    "message": "Download audio file failed.",
                },
            },
        )

    _mock_dashscope_client(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_PROVIDER_UNAVAILABLE"
    assert captured.value.category == "transient"
    assert captured.value.retryable is True
    assert "InvalidFile.DownloadFile" in captured.value.message


def test_dashscope_success_without_result_url_is_invalid_response(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "task-no-result"}})
        return httpx.Response(
            200,
            json={"output": {"task_status": "SUCCEEDED", "results": [{}]}},
        )

    _mock_dashscope_client(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_PROVIDER_RESPONSE_INVALID"
    assert "transcription_url" in captured.value.message


def test_dashscope_non_object_poll_response_is_structured_error(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "task-bad-json"}})
        return httpx.Response(200, json=[])

    _mock_dashscope_client(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_PROVIDER_RESPONSE_INVALID"
    assert captured.value.category == "transient"


def test_dashscope_timeout_identifies_the_request_phase(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timeout", request=request)

    _mock_dashscope_client(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_TIMEOUT"
    assert "提交任务" in captured.value.message
