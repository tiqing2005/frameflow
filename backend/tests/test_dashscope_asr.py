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


def _write_fake_chunks(command: list[str], *payloads: bytes) -> list[Path]:
    pattern = str(command[-1])
    chunks: list[Path] = []
    for index, payload in enumerate(payloads):
        chunk = Path(pattern.replace("%05d", f"{index:05d}"))
        chunk.write_bytes(payload)
        chunks.append(chunk)
    return chunks


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
    explicit_mp3 = client.get(f"/api/v1/asr/source/{token}/audio.mp3")
    assert explicit_mp3.status_code == 200
    assert explicit_mp3.content == b"RIFF-test"
    assert explicit_mp3.headers["content-type"] == "audio/mpeg"
    assert 'filename="audio.mp3"' in explicit_mp3.headers["content-disposition"]
    assert client.get(f"/api/v1/asr/source/{token}/audio.wav").status_code == 404
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
    prepared_paths: list[Path] = []
    ffmpeg_command: list[str] = []

    def fake_ffmpeg(command, **_kwargs):
        ffmpeg_command.extend(command)
        _write_fake_chunks(command, b"ID3-compact-audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        if request.url.path.endswith("/services/audio/asr/transcription"):
            body = json.loads(request.content)
            assert len(body["input"]["file_urls"]) == 1
            seen_url = body["input"]["file_urls"][0]
            assert seen_url.endswith("/audio.mp3")
            token = seen_url.rsplit("/", 2)[-2]
            prepared_path = resolve_asr_source_token(token, settings)
            assert prepared_path is not None
            prepared_paths.append(prepared_path)
            assert prepared_path.parent == settings.data_dir / "private" / "asr-staging"
            assert prepared_path.suffix == ".mp3"
            assert prepared_path.read_bytes() == b"ID3-compact-audio"
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.path.endswith("/tasks/task-1"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"transcription_url": "https://result.test/final.json"}
                        ],
                    }
                },
            )
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
    assert prepared_paths and all(not path.exists() for path in prepared_paths)
    assert source.read_bytes() == b"RIFF-test"
    assert ffmpeg_command[0] == "ffmpeg-test"
    assert ffmpeg_command[ffmpeg_command.index("-ac") + 1] == "1"
    assert ffmpeg_command[ffmpeg_command.index("-ar") + 1] == "16000"
    assert ffmpeg_command[ffmpeg_command.index("-b:a") + 1] == "8k"
    assert ffmpeg_command[ffmpeg_command.index("-segment_time") + 1] == "75"
    assert ffmpeg_command[ffmpeg_command.index("-segment_format") + 1] == "mp3"
    assert Path(ffmpeg_command[-1]).name.startswith(".")
    assert Path(ffmpeg_command[-1]).name.endswith(".tmp.mp3")
    assert "%05d" in Path(ffmpeg_command[-1]).name


def test_dashscope_multiple_chunks_are_submitted_once_downloaded_in_source_order_and_cleaned(
    runtime, monkeypatch
):
    _client, _worker, _database, settings = runtime
    settings.asr_provider = "dashscope"
    settings.asr_model = "paraformer-v2"
    settings.asr_timeout = 5
    settings.dashscope_api_key = "test-key"
    settings.dashscope_base_url = "https://dashscope.test/api/v1"
    settings.dashscope_public_base_url = "https://frameflow.test"
    settings.dashscope_signing_secret = "test-signing-secret"
    settings.dashscope_poll_seconds = 0.01
    source = settings.data_dir / "private" / "sources" / "long.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"RIFF-long-audio")
    submitted_urls: list[str] = []
    prepared_paths: list[Path] = []
    download_order: list[int] = []

    def fake_ffmpeg(command, **_kwargs):
        _write_fake_chunks(
            command,
            b"ID3-" + b"a" * 75_000,
            b"ID3-" + b"b" * 75_000,
            b"ID3-" + b"c" * 20_000,
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services/audio/asr/transcription"):
            body = json.loads(request.content)
            submitted_urls.extend(body["input"]["file_urls"])
            assert len(submitted_urls) == 3
            for file_url in submitted_urls:
                token = file_url.rsplit("/", 2)[-2]
                prepared = resolve_asr_source_token(token, settings)
                assert prepared is not None
                prepared_paths.append(prepared)
            staging_dir = settings.data_dir / "private" / "asr-staging"
            assert all(path.exists() for path in prepared_paths)
            assert not list(staging_dir.glob(".*.tmp.mp3"))
            return httpx.Response(200, json={"output": {"task_id": "task-chunks"}})
        if request.url.path.endswith("/tasks/task-chunks"):
            results = [
                {
                    "file_url": submitted_urls[index],
                    "transcription_url": f"https://result.test/{index}.json",
                }
                for index in reversed(range(3))
            ]
            return httpx.Response(
                200,
                json={"output": {"task_status": "SUCCEEDED", "results": results}},
            )
        if request.url.host == "result.test":
            index = int(request.url.path.strip("/").split(".")[0])
            download_order.append(index)
            return httpx.Response(
                200,
                json={"transcripts": [{"text": f"分片{index + 1}"}]},
            )
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

    assert text == "分片1\n分片2\n分片3"
    assert provider == "dashscope/paraformer-v2"
    assert download_order == [0, 1, 2]
    assert len(set(submitted_urls)) == 3
    assert prepared_paths and all(not path.exists() for path in prepared_paths)
    assert list((settings.data_dir / "private" / "asr-staging").iterdir()) == []
    assert source.read_bytes() == b"RIFF-long-audio"


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
        _write_fake_chunks(command, b"partial-one", b"partial-two")
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
        _write_fake_chunks(command, b"ID3-compact-audio", b"ID3-second-chunk")
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


def test_dashscope_submit_retries_transient_network_errors_then_recovers(
    runtime, monkeypatch
):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    submit_attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_attempts
        if request.url.path.endswith("/services/audio/asr/transcription"):
            submit_attempts += 1
            if submit_attempts == 1:
                raise httpx.ReadTimeout("temporary timeout", request=request)
            if submit_attempts == 2:
                raise httpx.ConnectError("temporary disconnect", request=request)
            return httpx.Response(200, json={"output": {"task_id": "task-submit-retry"}})
        if request.url.path.endswith("/tasks/task-submit-retry"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"transcription_url": "https://result.test/final.json"}
                        ],
                    }
                },
            )
        if request.url.host == "result.test":
            return httpx.Response(200, json={"transcripts": [{"text": "提交重试成功"}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _mock_dashscope_client(monkeypatch, handler)
    monkeypatch.setattr("app.asr.time.sleep", sleeps.append)

    text, provider = _dashscope_submit_and_wait(source, settings)

    assert text == "提交重试成功"
    assert provider == "dashscope/paraformer-v2"
    assert submit_attempts == 3
    assert sleeps == pytest.approx([0.25, 0.5, 0.01])


def test_dashscope_submit_network_retries_are_bounded(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    submit_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_attempts
        submit_attempts += 1
        raise httpx.ConnectError("network remains unavailable", request=request)

    _mock_dashscope_client(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_NETWORK_ERROR"
    assert submit_attempts == 3


def test_dashscope_submit_network_retries_stop_at_total_deadline(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    settings.asr_timeout = 0.3
    submit_attempts = 0
    clock = {"now": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submit_attempts
        submit_attempts += 1
        raise httpx.ConnectError("network remains unavailable", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("app.asr.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "app.asr.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(
        "app.asr.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_TIMEOUT"
    assert submit_attempts == 2
    assert clock["now"] == pytest.approx(settings.asr_timeout)


def test_dashscope_poll_retries_transient_network_errors_then_recovers(
    runtime, monkeypatch, caplog
):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    poll_attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_attempts
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(
                200,
                json={
                    "request_id": "request-submit",
                    "output": {"task_id": "task-retry"},
                },
            )
        if request.url.path.endswith("/tasks/task-retry"):
            poll_attempts += 1
            if poll_attempts == 1:
                raise httpx.ReadTimeout("temporary timeout", request=request)
            if poll_attempts == 2:
                raise httpx.ConnectError("temporary disconnect", request=request)
            if poll_attempts == 3:
                return httpx.Response(
                    200,
                    json={"output": {"task_status": "RUNNING"}},
                )
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"transcription_url": "https://result.test/final.json"}
                        ],
                    }
                },
            )
        if request.url.host == "result.test":
            return httpx.Response(
                200,
                json={"transcripts": [{"text": "transient errors recovered"}]},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _mock_dashscope_client(monkeypatch, handler)
    monkeypatch.setattr("app.asr.time.sleep", sleeps.append)
    caplog.set_level("WARNING", logger="app.asr")

    text, provider = _dashscope_submit_and_wait(source, settings)

    assert text == "transient errors recovered"
    assert provider == "dashscope/paraformer-v2"
    assert poll_attempts == 4
    assert sleeps == pytest.approx([0.01, 0.25, 0.5, 0.01])
    retry_records = [
        record for record in caplog.records if "event=dashscope_asr_poll_retry" in record.message
    ]
    assert len(retry_records) == 2
    assert "attempt=1" in retry_records[0].message
    assert "error_type=ReadTimeout" in retry_records[0].message
    assert "attempt=2" in retry_records[1].message
    assert "error_type=ConnectError" in retry_records[1].message


def test_dashscope_poll_network_retries_stop_at_total_deadline(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    settings.asr_timeout = 0.3
    settings.dashscope_poll_seconds = 0.1
    poll_attempts = 0
    clock = {"now": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_attempts
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "task-deadline"}})
        poll_attempts += 1
        raise httpx.ConnectError("network remains unavailable", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("app.asr.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "app.asr.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(
        "app.asr.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_TIMEOUT"
    assert captured.value.category == "transient"
    assert poll_attempts == 1
    assert clock["now"] == pytest.approx(settings.asr_timeout)


def test_dashscope_poll_recovers_after_three_consecutive_network_errors(
    runtime, monkeypatch
):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    poll_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_attempts
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "task-fourth-poll"}})
        if request.url.path.endswith("/tasks/task-fourth-poll"):
            poll_attempts += 1
            if poll_attempts <= 3:
                raise httpx.ConnectError("temporary poll failure", request=request)
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"transcription_url": "https://result.test/final.json"}
                        ],
                    }
                },
            )
        if request.url.host == "result.test":
            return httpx.Response(200, json={"transcripts": [{"text": "第四次轮询成功"}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    _mock_dashscope_client(monkeypatch, handler)

    text, provider = _dashscope_submit_and_wait(source, settings)

    assert text == "第四次轮询成功"
    assert provider == "dashscope/paraformer-v2"
    assert poll_attempts == 4


def test_dashscope_result_download_retries_network_errors_then_recovers(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    result_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal result_attempts
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "task-result"}})
        if request.url.path.endswith("/tasks/task-result"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"transcription_url": "https://result.test/final.json"}
                        ],
                    }
                },
            )
        result_attempts += 1
        if result_attempts < 3:
            raise httpx.ReadTimeout("result host is temporarily slow", request=request)
        return httpx.Response(200, json={"transcripts": [{"text": "结果下载重试成功"}]})

    _mock_dashscope_client(monkeypatch, handler)

    text, provider = _dashscope_submit_and_wait(source, settings)

    assert text == "结果下载重试成功"
    assert provider == "dashscope/paraformer-v2"
    assert result_attempts == 3


def test_dashscope_result_download_network_retries_are_bounded(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    result_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal result_attempts
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "task-result-bounded"}})
        if request.url.path.endswith("/tasks/task-result-bounded"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"transcription_url": "https://result.test/final.json"}
                        ],
                    }
                },
            )
        result_attempts += 1
        raise httpx.ConnectError("result host unavailable", request=request)

    _mock_dashscope_client(monkeypatch, handler)

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_NETWORK_ERROR"
    assert result_attempts == 3


def test_dashscope_result_download_retries_stop_at_total_deadline(runtime, monkeypatch):
    _client, _worker, _database, settings = runtime
    source = settings.data_dir / "private" / "asr-staging" / "sample.mp3"
    _configure_dashscope(settings, source)
    settings.asr_timeout = 0.4
    settings.dashscope_poll_seconds = 0.1
    result_attempts = 0
    clock = {"now": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal result_attempts
        if request.url.path.endswith("/services/audio/asr/transcription"):
            return httpx.Response(200, json={"output": {"task_id": "task-result-deadline"}})
        if request.url.path.endswith("/tasks/task-result-deadline"):
            return httpx.Response(
                200,
                json={
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"transcription_url": "https://result.test/final.json"}
                        ],
                    }
                },
            )
        result_attempts += 1
        raise httpx.ConnectError("result host unavailable", request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr("app.asr.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "app.asr.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(
        "app.asr.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )

    with pytest.raises(TranscriptionError) as captured:
        _dashscope_submit_and_wait(source, settings)

    assert captured.value.code == "ASR_TIMEOUT"
    assert result_attempts == 2
    assert clock["now"] == pytest.approx(settings.asr_timeout)


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
