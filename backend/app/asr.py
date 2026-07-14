from __future__ import annotations

import os
import queue
import threading
import base64
import binascii
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Literal

import httpx

from .config import Settings


ASRErrorCategory = Literal["input", "transient", "configuration", "dependency"]
REARMABLE_ASR_ERROR_CODES = {
    "ASR_OPENAI_KEY_MISSING",
    "ASR_DASHSCOPE_KEY_MISSING",
    "ASR_PROVIDER_INVALID",
    "ASR_PROVIDER_AUTH_ERROR",
    "ASR_PROVIDER_CONFIGURATION_ERROR",
    "ASR_MODEL_CONFIGURATION_ERROR",
    "ASR_MODEL_DOWNLOAD_NETWORK_ERROR",
    "ASR_LOCAL_DEPENDENCY_MISSING",
    "ASR_LOCAL_RUNTIME_MISSING",
}


class TranscriptionError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        category: ASRErrorCategory = "input",
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.category = category


_LOCAL_MODELS: dict[tuple[str, str, str, str], Any] = {}
_LOCAL_IN_FLIGHT: set[tuple[str, str, str, str]] = set()
_LOCAL_IN_FLIGHT_LOCK = threading.Lock()


def _run_with_timeout(operation, timeout: float) -> tuple[str, str]:
    """Run blocking local inference without letting it pin the worker forever.

    Python cannot safely kill an in-process native inference call. The timed-out
    daemon thread is therefore abandoned; worker fencing prevents its eventual
    result from being committed by an expired execution.
    """
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            result.put((True, operation()))
        except BaseException as exc:
            result.put((False, exc))

    thread = threading.Thread(target=target, name="frameflow-local-asr", daemon=True)
    thread.start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TranscriptionError(
            "ASR_LOCAL_TIMEOUT",
            f"本地语音识别超过 {timeout:g} 秒，已停止等待结果",
            True,
            "transient",
        ) from exc
    if ok:
        return value
    raise value


def _local_transcribe(path: Path, settings: Settings) -> tuple[str, str]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
        from opencc import OpenCC  # type: ignore
    except (ImportError, OSError) as exc:
        raise TranscriptionError(
            "ASR_LOCAL_DEPENDENCY_MISSING",
            "本地语音识别依赖或运行库不可用；请安装 local-asr 依赖并重启 Worker",
            True,
            "dependency",
        ) from exc
    download_root = settings.whisper_download_root or settings.data_dir / "models" / "whisper"
    hf_home = settings.hf_home or settings.data_dir / "models" / "huggingface"
    download_root.mkdir(parents=True, exist_ok=True)
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(hf_home))
    key = (
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
        str(download_root),
    )
    with _LOCAL_IN_FLIGHT_LOCK:
        if key in _LOCAL_IN_FLIGHT:
            raise TranscriptionError(
                "ASR_LOCAL_BUSY",
                "上一次本地语音识别仍在退出，请稍后再重新执行",
                True,
                "transient",
            )
        _LOCAL_IN_FLIGHT.add(key)
    try:
        model = _LOCAL_MODELS.get(key)
        if model is None:
            try:
                model = WhisperModel(
                    settings.whisper_model,
                    device=settings.whisper_device,
                    compute_type=settings.whisper_compute_type,
                    download_root=str(download_root),
                )
                _LOCAL_MODELS[key] = model
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}".lower()
                if any(
                    marker in detail
                    for marker in ("dll", "shared librar", "cudnn", "cublas", "onnxruntime")
                ):
                    raise TranscriptionError(
                        "ASR_LOCAL_RUNTIME_MISSING",
                        "本地语音识别运行库不可用，请安装所需运行库并重启 Worker",
                        True,
                        "dependency",
                    ) from exc
                if any(
                    marker in detail
                    for marker in (
                        "timeout",
                        "timed out",
                        "connection",
                        "network",
                        "download",
                        "http",
                    )
                ):
                    raise TranscriptionError(
                        "ASR_MODEL_DOWNLOAD_NETWORK_ERROR",
                        f"本地 Whisper 模型 {settings.whisper_model} 下载失败，请检查网络后重试",
                        True,
                        "transient",
                    ) from exc
                raise TranscriptionError(
                    "ASR_MODEL_CONFIGURATION_ERROR",
                    f"本地 Whisper 模型 {settings.whisper_model} 无法加载，请检查模型、设备和计算类型配置",
                    True,
                    "configuration",
                ) from exc
        try:
            segments, _info = model.transcribe(
                str(path),
                language="zh",
                initial_prompt=(
                    "以下是一段普通话录音。请使用简体中文准确转写，"
                    "保留数字、专有名词和自然标点。"
                ),
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=True,
            )
            text = "".join(str(segment.text).strip() for segment in segments).strip()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}".lower()
            if any(
                marker in detail
                for marker in ("dll", "shared librar", "cudnn", "cublas", "onnxruntime")
            ):
                raise TranscriptionError(
                    "ASR_LOCAL_RUNTIME_MISSING",
                    "本地语音识别运行库不可用，请安装所需运行库并重启 Worker",
                    True,
                    "dependency",
                ) from exc
            raise TranscriptionError(
                "ASR_INPUT_UNSUPPORTED",
                "本地 Whisper 无法解码或转写该媒体文件",
                False,
                "input",
            ) from exc
        text = OpenCC("t2s").convert(text).strip()
        if not text:
            raise TranscriptionError(
                "ASR_NO_SPEECH", "媒体中未识别到可用语音内容", False, "input"
            )
        return text, f"faster-whisper/{settings.whisper_model}"
    finally:
        with _LOCAL_IN_FLIGHT_LOCK:
            _LOCAL_IN_FLIGHT.discard(key)


def _provider_error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500].lower()
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict):
            values = [error.get("code"), error.get("type"), error.get("message")]
            return " ".join(str(value) for value in values if value).lower()
        return str(error).lower()
    return str(payload).lower()


def _provider_http_error(response: httpx.Response) -> TranscriptionError:
    status = response.status_code
    detail = _provider_error_text(response)
    if status in {401, 403}:
        return TranscriptionError(
            "ASR_PROVIDER_AUTH_ERROR",
            "语音识别服务认证失败，请检查 API Key、项目权限或组织配置",
            True,
            "configuration",
        )
    if status in {408, 425}:
        return TranscriptionError(
            "ASR_PROVIDER_TIMEOUT",
            f"语音识别服务暂时未完成请求（HTTP {status}）",
            True,
            "transient",
        )
    if status == 429:
        return TranscriptionError(
            "ASR_PROVIDER_RATE_LIMITED",
            "语音识别服务当前请求过多，请稍后重试",
            True,
            "transient",
        )
    if status >= 500:
        return TranscriptionError(
            "ASR_PROVIDER_UNAVAILABLE",
            f"语音识别服务暂时不可用（HTTP {status}）",
            True,
            "transient",
        )
    configuration_markers = (
        "api key",
        "api_key",
        "authentication",
        "permission",
        "organization",
        "project",
        "model",
        "model_not_found",
        "endpoint",
        "not found",
    )
    if status in {404, 405} or any(marker in detail for marker in configuration_markers):
        return TranscriptionError(
            "ASR_PROVIDER_CONFIGURATION_ERROR",
            f"语音识别 Provider 配置无效，请检查服务地址、Provider 和模型（HTTP {status}）",
            True,
            "configuration",
        )
    if status in {400, 413, 415, 422}:
        return TranscriptionError(
            "ASR_INPUT_REJECTED",
            f"语音识别服务无法处理该媒体内容或格式（HTTP {status}）",
            False,
            "input",
        )
    return TranscriptionError(
        "ASR_PROVIDER_CONFIGURATION_ERROR",
        f"语音识别 Provider 拒绝请求，请检查服务配置（HTTP {status}）",
        True,
        "configuration",
    )


def _openai_transcribe(path: Path, mime_type: str | None, settings: Settings) -> tuple[str, str]:
    if not settings.openai_api_key:
        raise TranscriptionError(
            "ASR_OPENAI_KEY_MISSING",
            "OpenAI-compatible 语音识别未配置服务端 OPENAI_API_KEY",
            True,
            "configuration",
        )
    try:
        with path.open("rb") as handle, httpx.Client(timeout=settings.asr_timeout) as client:
            response = client.post(
                f"{settings.openai_base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data={"model": settings.asr_model, "response_format": "json"},
                files={"file": (path.name, handle, mime_type or "application/octet-stream")},
            )
        if response.status_code >= 400:
            raise _provider_http_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TranscriptionError(
                "ASR_PROVIDER_RESPONSE_INVALID",
                "语音识别服务返回了无法解析的响应",
                True,
                "transient",
            ) from exc
        text = str(payload.get("text", "")).strip() if isinstance(payload, dict) else ""
        if not text:
            raise TranscriptionError(
                "ASR_NO_SPEECH", "媒体中未识别到可用语音内容", False, "input"
            )
        return text, f"openai-compatible/{settings.asr_model}"
    except httpx.TimeoutException as exc:
        raise TranscriptionError(
            "ASR_TIMEOUT", "语音识别服务响应超时", True, "transient"
        ) from exc
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        raise TranscriptionError(
            "ASR_PROVIDER_CONFIGURATION_ERROR",
            "语音识别服务地址无效，请检查 FRAMEFLOW_OPENAI_BASE_URL",
            True,
            "configuration",
        ) from exc
    except httpx.RequestError as exc:
        raise TranscriptionError(
            "ASR_NETWORK_ERROR", "暂时无法连接语音识别服务", True, "transient"
        ) from exc


def create_asr_source_token(path: Path, settings: Settings) -> str:
    if not settings.dashscope_signing_secret:
        raise TranscriptionError(
            "ASR_PROVIDER_CONFIGURATION_ERROR",
            "DashScope 未配置 FRAMEFLOW_ASR_URL_SIGNING_SECRET",
            True,
            "configuration",
        )
    try:
        relative = path.resolve().relative_to(settings.data_dir.resolve()).as_posix()
    except ValueError as exc:
        raise TranscriptionError(
            "ASR_PROVIDER_CONFIGURATION_ERROR",
            "待转写文件不在 FRAMEFLOW_DATA_DIR 内，无法生成临时访问地址",
            True,
            "configuration",
        ) from exc
    payload = json.dumps(
        {"path": relative, "exp": int(time.time()) + settings.dashscope_url_ttl_seconds},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(
        settings.dashscope_signing_secret.encode(), encoded, hashlib.sha256
    ).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def resolve_asr_source_token(token: str, settings: Settings) -> Path | None:
    if not settings.dashscope_signing_secret:
        return None
    try:
        encoded, signature_text = token.split(".", 1)
        encoded_bytes = encoded.encode()
        expected = hmac.new(
            settings.dashscope_signing_secret.encode(), encoded_bytes, hashlib.sha256
        ).digest()
        supplied = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        if not hmac.compare_digest(expected, supplied):
            return None
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        )
        if int(payload["exp"]) < int(time.time()):
            return None
        path = (settings.data_dir / str(payload["path"])).resolve()
        path.relative_to(settings.data_dir.resolve())
        return path if path.is_file() else None
    except (
        ValueError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None


def _dashscope_transcribe(path: Path, settings: Settings) -> tuple[str, str]:
    if not settings.dashscope_api_key:
        raise TranscriptionError(
            "ASR_DASHSCOPE_KEY_MISSING",
            "DashScope 语音识别未配置 DASHSCOPE_API_KEY",
            True,
            "configuration",
        )
    if not settings.dashscope_public_base_url:
        raise TranscriptionError(
            "ASR_PROVIDER_CONFIGURATION_ERROR",
            "DashScope 需要配置公网 HTTPS 地址 FRAMEFLOW_ASR_PUBLIC_BASE_URL",
            True,
            "configuration",
        )
    token = create_asr_source_token(path, settings)
    file_url = f"{settings.dashscope_public_base_url}/api/v1/asr/source/{token}"
    deadline = time.monotonic() + settings.asr_timeout
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    try:
        with httpx.Client(timeout=min(30.0, settings.asr_timeout)) as client:
            response = client.post(
                f"{settings.dashscope_base_url}/services/audio/asr/transcription",
                headers=headers,
                json={"model": settings.asr_model, "input": {"file_urls": [file_url]}, "parameters": {}},
            )
            if response.status_code >= 400:
                raise _provider_http_error(response)
            try:
                task_id = str(response.json().get("output", {}).get("task_id", "")).strip()
            except ValueError as exc:
                raise TranscriptionError(
                    "ASR_PROVIDER_RESPONSE_INVALID", "DashScope 未返回有效任务信息", True, "transient"
                ) from exc
            if not task_id:
                raise TranscriptionError(
                    "ASR_PROVIDER_RESPONSE_INVALID", "DashScope 未返回 task_id", True, "transient"
                )
            result_url = ""
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                time.sleep(min(settings.dashscope_poll_seconds, max(0.0, remaining)))
                poll = client.get(
                    f"{settings.dashscope_base_url}/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
                )
                if poll.status_code >= 400:
                    raise _provider_http_error(poll)
                try:
                    output = poll.json().get("output", {})
                except ValueError as exc:
                    raise TranscriptionError(
                        "ASR_PROVIDER_RESPONSE_INVALID", "DashScope 任务状态无法解析", True, "transient"
                    ) from exc
                status = str(output.get("task_status", ""))
                if status == "FAILED":
                    raise TranscriptionError(
                        "ASR_INPUT_REJECTED", "DashScope 无法识别该媒体文件", False, "input"
                    )
                if status == "SUCCEEDED":
                    for item in output.get("results", []):
                        if isinstance(item, dict):
                            result_url = str(
                                item.get("transcription_url")
                                or (item.get("output") or {}).get("transcription_url")
                                or ""
                            )
                            if result_url:
                                break
                    break
            if not result_url:
                raise TranscriptionError(
                    "ASR_TIMEOUT", "DashScope 语音识别任务等待超时", True, "transient"
                )
            result = client.get(result_url)
            if result.status_code >= 400:
                raise _provider_http_error(result)
            try:
                payload = result.json()
            except ValueError as exc:
                raise TranscriptionError(
                    "ASR_PROVIDER_RESPONSE_INVALID", "DashScope 转写结果无法解析", True, "transient"
                ) from exc
            text = "".join(
                str(item.get("text", "")).strip()
                for item in payload.get("transcripts", [])
                if isinstance(item, dict)
            ).strip()
            if not text:
                raise TranscriptionError(
                    "ASR_NO_SPEECH", "媒体中未识别到可用语音内容", False, "input"
                )
            return text, f"dashscope/{settings.asr_model}"
    except httpx.TimeoutException as exc:
        raise TranscriptionError("ASR_TIMEOUT", "DashScope 请求超时", True, "transient") from exc
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        raise TranscriptionError(
            "ASR_PROVIDER_CONFIGURATION_ERROR", "DashScope 服务或公网地址无效", True, "configuration"
        ) from exc
    except httpx.RequestError as exc:
        raise TranscriptionError(
            "ASR_NETWORK_ERROR", "暂时无法连接 DashScope 语音识别服务", True, "transient"
        ) from exc


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
            raise TranscriptionError(
                "SUBTITLE_ENCODING", "字幕文件需使用 UTF-8 编码", False, "input"
            ) from exc
    provider = settings.asr_provider
    if provider not in {"auto", "local", "openai", "dashscope"}:
        raise TranscriptionError(
            "ASR_PROVIDER_INVALID",
            "FRAMEFLOW_ASR_PROVIDER 仅支持 auto、local、openai 或 dashscope",
            True,
            "configuration",
        )
    if provider == "local":
        return _run_with_timeout(lambda: _local_transcribe(path, settings), settings.local_asr_timeout)
    if provider == "openai":
        return _openai_transcribe(path, mime_type, settings)
    if provider == "dashscope":
        return _dashscope_transcribe(path, settings)
    if settings.openai_api_key:
        return _openai_transcribe(path, mime_type, settings)
    # In auto mode local ASR is the no-key path. If the optional dependency is
    # absent the structured error tells the operator exactly how to enable it.
    return _run_with_timeout(lambda: _local_transcribe(path, settings), settings.local_asr_timeout)
