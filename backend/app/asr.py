from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .config import Settings


class TranscriptionError(Exception):
    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


_LOCAL_MODELS: dict[tuple[str, str, str], Any] = {}


def _local_transcribe(path: Path, settings: Settings) -> tuple[str, str]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise TranscriptionError(
            "ASR_LOCAL_DEPENDENCY_MISSING",
            "本地语音识别未安装；请执行 pip install -r requirements-asr-local.txt",
            False,
        ) from exc
    key = (settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
    model = _LOCAL_MODELS.get(key)
    if model is None:
        try:
            model = WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
            _LOCAL_MODELS[key] = model
        except Exception as exc:
            raise TranscriptionError(
                "ASR_MODEL_LOAD_FAILED",
                f"本地 Whisper 模型 {settings.whisper_model} 加载或下载失败，请检查网络/磁盘后重试",
                True,
            ) from exc
    try:
        segments, _info = model.transcribe(
            str(path),
            language="zh",
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=True,
        )
        text = "".join(str(segment.text).strip() for segment in segments).strip()
    except Exception as exc:
        raise TranscriptionError(
            "ASR_LOCAL_TRANSCRIBE_FAILED",
            "本地 Whisper 无法解码或转写该媒体文件",
            False,
        ) from exc
    if not text:
        raise TranscriptionError("ASR_EMPTY_RESULT", "本地语音识别未返回有效字幕", True)
    return text, f"faster-whisper/{settings.whisper_model}"


def _openai_transcribe(path: Path, mime_type: str | None, settings: Settings) -> tuple[str, str]:
    if not settings.openai_api_key:
        raise TranscriptionError(
            "ASR_OPENAI_KEY_MISSING",
            "OpenAI-compatible 语音识别未配置服务端 OPENAI_API_KEY",
            False,
        )
    try:
        with path.open("rb") as handle, httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{settings.openai_base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data={"model": settings.asr_model, "response_format": "json"},
                files={"file": (path.name, handle, mime_type or "application/octet-stream")},
            )
        if response.status_code >= 400:
            retryable = response.status_code >= 500 or response.status_code == 429
            raise TranscriptionError(
                "ASR_PROVIDER_ERROR",
                f"语音识别服务返回异常（HTTP {response.status_code}）",
                retryable,
            )
        text = str(response.json().get("text", "")).strip()
        if not text:
            raise TranscriptionError("ASR_EMPTY_RESULT", "语音识别未返回有效字幕", True)
        return text, f"openai-compatible/{settings.asr_model}"
    except httpx.TimeoutException as exc:
        raise TranscriptionError("ASR_TIMEOUT", "语音识别服务响应超时", True) from exc
    except httpx.NetworkError as exc:
        raise TranscriptionError("ASR_NETWORK_ERROR", "暂时无法连接语音识别服务", True) from exc


def transcribe_file(path: Path, mime_type: str | None, settings: Settings) -> tuple[str, str]:
    """Transcribe through an optional server-side OpenAI-compatible endpoint.

    Text subtitle uploads are parsed locally. Media calls are real when a key is
    configured and fail explicitly otherwise; no API key ever reaches clients.
    """
    suffix = path.suffix.lower()
    if suffix in {".txt", ".srt", ".vtt"}:
        try:
            return path.read_text(encoding="utf-8-sig"), "local-subtitle-parser"
        except UnicodeDecodeError as exc:
            raise TranscriptionError("SUBTITLE_ENCODING", "字幕文件需使用 UTF-8 编码", False) from exc
    provider = settings.asr_provider
    if provider not in {"auto", "local", "openai"}:
        raise TranscriptionError(
            "ASR_PROVIDER_INVALID",
            "FRAMEFLOW_ASR_PROVIDER 仅支持 auto、local 或 openai",
            False,
        )
    if provider == "local":
        return _local_transcribe(path, settings)
    if provider == "openai":
        return _openai_transcribe(path, mime_type, settings)
    if settings.openai_api_key:
        return _openai_transcribe(path, mime_type, settings)
    # In auto mode local ASR is the no-key path. If the optional dependency is
    # absent the structured error tells the operator exactly how to enable it.
    return _local_transcribe(path, settings)
