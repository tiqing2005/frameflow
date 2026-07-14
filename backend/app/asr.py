from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal

import httpx

from .config import Settings


logger = logging.getLogger(__name__)


DASHSCOPE_CHUNK_SECONDS = 75
DASHSCOPE_NETWORK_MAX_ATTEMPTS = 3
DASHSCOPE_POLL_REQUEST_TIMEOUT_SECONDS = 10.0


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
    "ASR_MEDIA_PREPROCESSOR_MISSING",
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


def _ffmpeg_executable() -> str:
    """Resolve ffmpeg without making DashScope depend on a system-wide install."""

    configured = os.getenv("FRAMEFLOW_FFMPEG_BINARY", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        raise TranscriptionError(
            "ASR_MEDIA_PREPROCESSOR_MISSING",
            "FRAMEFLOW_FFMPEG_BINARY 指向的 ffmpeg 不存在，无法准备云端转写音频",
            True,
            "dependency",
        )

    resolved = shutil.which("ffmpeg")
    if resolved:
        return resolved
    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if bundled.is_file():
            return str(bundled.resolve())
    except (ImportError, RuntimeError, OSError):
        pass
    raise TranscriptionError(
        "ASR_MEDIA_PREPROCESSOR_MISSING",
        "服务器未安装可用的 ffmpeg，无法将音视频准备为 DashScope 支持的音频",
        True,
        "dependency",
    )


@contextmanager
def _prepare_dashscope_audio(path: Path, settings: Settings) -> Iterator[tuple[Path, ...]]:
    """Create private 75-second MP3 chunks and remove all artifacts on exit.

    ffmpeg writes every chunk under a unique partial name. The complete set is
    validated and renamed before it is yielded, and signed URLs are only
    created after that yield. DashScope therefore cannot observe a partial
    chunk set, even though publishing several filesystem entries cannot itself
    be one operating-system rename.
    """

    staging_dir = settings.data_dir / "private" / "asr-staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    partial_pattern = staging_dir / f".{identifier}.%05d.tmp.mp3"
    prepared: list[Path] = []
    try:
        executable = _ffmpeg_executable()
        command = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "8k",
            "-f",
            "segment",
            "-segment_time",
            str(DASHSCOPE_CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mp3",
            str(partial_pattern),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, settings.asr_timeout),
                check=False,
            )
        except FileNotFoundError as exc:
            raise TranscriptionError(
                "ASR_MEDIA_PREPROCESSOR_MISSING",
                "服务器未安装可用的 ffmpeg，无法准备 DashScope 转写音频",
                True,
                "dependency",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError(
                "ASR_MEDIA_PREPROCESSING_TIMEOUT",
                "音视频压缩超过允许时间，尚未提交到 DashScope",
                True,
                "transient",
            ) from exc
        except OSError as exc:
            raise TranscriptionError(
                "ASR_MEDIA_PREPROCESSING_FAILED",
                "服务器无法启动音频预处理程序，尚未提交到 DashScope",
                True,
                "dependency",
            ) from exc

        partials = sorted(staging_dir.glob(f".{identifier}.*.tmp.mp3"))
        if (
            completed.returncode != 0
            or not partials
            or any(not partial.is_file() or partial.stat().st_size == 0 for partial in partials)
        ):
            raise TranscriptionError(
                "ASR_MEDIA_PREPROCESSING_FAILED",
                "无法从上传文件中提取可用音轨，请检查音视频格式或内容",
                False,
                "input",
            )
        for index, partial in enumerate(partials):
            published = staging_dir / f"{identifier}.{index:05d}.mp3"
            partial.replace(published)
            prepared.append(published)
        yield tuple(prepared)
    finally:
        for artifact in staging_dir.glob(f".{identifier}.*.tmp.mp3"):
            artifact.unlink(missing_ok=True)
        for artifact in prepared:
            artifact.unlink(missing_ok=True)


def _dashscope_transcribe(path: Path, settings: Settings) -> tuple[str, str]:
    # Validate provider configuration before spending CPU on media conversion.
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
    with _prepare_dashscope_audio(path, settings) as prepared:
        return _dashscope_submit_and_wait(prepared, settings)


def _dashscope_json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscriptionError(
            "ASR_PROVIDER_RESPONSE_INVALID",
            f"DashScope {context}返回了无法解析的 JSON",
            True,
            "transient",
        ) from exc
    if not isinstance(payload, dict):
        raise TranscriptionError(
            "ASR_PROVIDER_RESPONSE_INVALID",
            f"DashScope {context}响应结构无效",
            True,
            "transient",
        )
    return payload


def _dashscope_output(payload: dict[str, Any], context: str) -> dict[str, Any]:
    output = payload.get("output")
    if not isinstance(output, dict):
        raise TranscriptionError(
            "ASR_PROVIDER_RESPONSE_INVALID",
            f"DashScope {context}未返回有效 output",
            True,
            "transient",
        )
    return output


def _dashscope_request_timeout(deadline: float, phase: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TranscriptionError(
            "ASR_TIMEOUT",
            f"DashScope {phase}超过总等待时间",
            True,
            "transient",
        )
    return max(0.001, min(30.0, remaining))


def _dashscope_poll_request_timeout(deadline: float, phase: str) -> float:
    """Keep a lost status request from consuming a large share of the deadline."""

    return min(
        DASHSCOPE_POLL_REQUEST_TIMEOUT_SECONDS,
        _dashscope_request_timeout(deadline, phase),
    )


def _dashscope_poll_retry_delay(
    consecutive_failures: int,
    poll_seconds: float,
    remaining: float,
) -> float:
    """Bound poll retries by both an exponential cap and the task deadline."""

    base_delay = max(0.25, min(1.0, poll_seconds))
    exponent = min(max(0, consecutive_failures - 1), 5)
    return max(0.0, min(5.0, base_delay * (2**exponent), remaining))


def _dashscope_request_with_network_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    deadline: float,
    phase: str,
    retry_event: str,
    poll_seconds: float,
    **kwargs: Any,
) -> httpx.Response:
    """Retry transport failures without extending the task-wide deadline."""

    for attempt in range(1, DASHSCOPE_NETWORK_MAX_ATTEMPTS + 1):
        try:
            return client.request(
                method,
                url,
                timeout=_dashscope_request_timeout(deadline, phase),
                **kwargs,
            )
        except (httpx.InvalidURL, httpx.UnsupportedProtocol):
            raise
        except httpx.RequestError as exc:
            remaining = max(0.0, deadline - time.monotonic())
            if attempt >= DASHSCOPE_NETWORK_MAX_ATTEMPTS or remaining <= 0:
                logger.warning(
                    "event=%s_exhausted attempt=%s error_type=%s remaining_seconds=%.3f",
                    retry_event,
                    attempt,
                    type(exc).__name__,
                    remaining,
                )
                raise
            retry_delay = _dashscope_poll_retry_delay(attempt, poll_seconds, remaining)
            logger.warning(
                "event=%s attempt=%s error_type=%s retry_in_seconds=%.3f "
                "remaining_seconds=%.3f",
                retry_event,
                attempt,
                type(exc).__name__,
                retry_delay,
                remaining,
            )
            time.sleep(retry_delay)
    raise AssertionError("unreachable DashScope retry state")


def _dashscope_result_urls(
    results: list[Any],
    file_urls: list[str],
    payload: dict[str, Any],
    output: dict[str, Any],
) -> list[str]:
    """Return transcription URLs in the exact order of submitted media URLs."""

    parsed: list[tuple[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        subtask_status = str(item.get("subtask_status", "")).strip().upper()
        if subtask_status == "FAILED":
            raise _dashscope_failure(payload, output, fallback=item)
        nested = item.get("output")
        nested_output = nested if isinstance(nested, dict) else {}
        result_url = str(
            item.get("transcription_url")
            or nested_output.get("transcription_url")
            or ""
        ).strip()
        source_url = str(item.get("file_url") or nested_output.get("file_url") or "").strip()
        if not result_url:
            raise TranscriptionError(
                "ASR_PROVIDER_RESPONSE_INVALID",
                "DashScope 任务成功但至少一个分片未返回 transcription_url",
                True,
                "transient",
            )
        parsed.append((source_url, result_url))

    if len(parsed) != len(file_urls):
        raise TranscriptionError(
            "ASR_PROVIDER_RESPONSE_INVALID",
            f"DashScope 返回 {len(parsed)} 个分片结果，但提交了 {len(file_urls)} 个分片",
            True,
            "transient",
        )

    source_markers = [source_url for source_url, _result_url in parsed]
    if all(source_markers):
        by_source = {source_url: result_url for source_url, result_url in parsed}
        if len(by_source) != len(parsed) or set(by_source) != set(file_urls):
            raise TranscriptionError(
                "ASR_PROVIDER_RESPONSE_INVALID",
                "DashScope 分片结果与提交的 file_urls 无法一一对应",
                True,
                "transient",
            )
        return [by_source[file_url] for file_url in file_urls]
    if any(source_markers):
        raise TranscriptionError(
            "ASR_PROVIDER_RESPONSE_INVALID",
            "DashScope 仅为部分分片返回了 file_url，无法安全恢复顺序",
            True,
            "transient",
        )
    # Older compatible endpoints omit file_url but preserve input order.
    return [result_url for _source_url, result_url in parsed]


def _dashscope_failure(
    payload: dict[str, Any],
    output: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
) -> TranscriptionError:
    fallback = fallback or {}
    code = str(
        fallback.get("code")
        or output.get("code")
        or payload.get("code")
        or "TASK_FAILED"
    ).strip()
    provider_message = str(
        fallback.get("message")
        or output.get("message")
        or payload.get("message")
        or "供应商未提供失败原因"
    ).strip()
    detail = f"{code}: {provider_message}"[:500]
    normalized = detail.casefold()

    # File download failures are commonly temporary callback/network failures,
    # not proof that the user's media is invalid. Prefer a safe retry for every
    # unclassified provider-side task failure.
    if any(
        marker in normalized
        for marker in (
            "download",
            "timeout",
            "time_out",
            "network",
            "throttl",
            "rate",
            "internal",
            "unavailable",
            "temporar",
            "system_error",
            "service_error",
        )
    ):
        return TranscriptionError(
            "ASR_PROVIDER_UNAVAILABLE",
            f"DashScope 任务暂时失败（{detail}）",
            True,
            "transient",
        )
    if any(
        marker in normalized
        for marker in (
            "authentication",
            "unauthorized",
            "api_key",
            "apikey",
            "permission",
            "accessdenied",
            "model_not_found",
            "invalidmodel",
            "quota",
        )
    ):
        return TranscriptionError(
            "ASR_PROVIDER_CONFIGURATION_ERROR",
            f"DashScope 配置或权限错误（{detail}）",
            True,
            "configuration",
        )
    if any(
        marker in normalized
        for marker in (
            "invalidparameter",
            "invalid_parameter",
            "unsupported",
            "format",
            "no speech",
            "nospeech",
            "invalid audio",
            "invalidfile",
        )
    ):
        return TranscriptionError(
            "ASR_INPUT_REJECTED",
            f"DashScope 无法处理该媒体（{detail}）",
            False,
            "input",
        )
    return TranscriptionError(
        "ASR_PROVIDER_UNAVAILABLE",
        f"DashScope 任务失败（{detail}）",
        True,
        "transient",
    )


def _dashscope_submit_and_wait(
    paths: Path | tuple[Path, ...] | list[Path], settings: Settings
) -> tuple[str, str]:
    prepared_paths = [paths] if isinstance(paths, Path) else list(paths)
    if not prepared_paths:
        raise TranscriptionError(
            "ASR_MEDIA_PREPROCESSING_FAILED",
            "音频预处理未生成任何可提交分片",
            False,
            "input",
        )
    # DashScope examples always expose a media filename in the URL. Keep the
    # signed token in its own path segment and provide an explicit MP3 suffix
    # so provider-side downloaders can determine the format from both the URL
    # and response headers.
    file_urls = [
        f"{settings.dashscope_public_base_url}/api/v1/asr/source/"
        f"{create_asr_source_token(path, settings)}/audio.mp3"
        for path in prepared_paths
    ]
    deadline = time.monotonic() + settings.asr_timeout
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    phase = "提交任务"
    try:
        with httpx.Client(timeout=min(30.0, settings.asr_timeout)) as client:
            response = _dashscope_request_with_network_retries(
                client,
                "POST",
                f"{settings.dashscope_base_url}/services/audio/asr/transcription",
                deadline=deadline,
                phase=phase,
                retry_event="dashscope_asr_submit_retry",
                poll_seconds=settings.dashscope_poll_seconds,
                headers=headers,
                json={
                    "model": settings.asr_model,
                    "input": {"file_urls": file_urls},
                    "parameters": {},
                },
            )
            if response.status_code >= 400:
                raise _provider_http_error(response)
            submit_payload = _dashscope_json_object(response, "任务提交")
            submit_output = _dashscope_output(submit_payload, "任务提交")
            task_id = str(submit_output.get("task_id", "")).strip()
            if not task_id:
                raise TranscriptionError(
                    "ASR_PROVIDER_RESPONSE_INVALID", "DashScope 未返回 task_id", True, "transient"
                )
            request_id = str(submit_payload.get("request_id", "")).strip()
            logger.info(
                "DashScope ASR submitted task_id=%s request_id=%s model=%s chunks=%s",
                task_id,
                request_id or "-",
                settings.asr_model,
                len(file_urls),
            )
            result_urls: list[str] = []
            last_status = ""
            consecutive_poll_failures = 0
            next_poll_delay = settings.dashscope_poll_seconds
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                time.sleep(min(next_poll_delay, max(0.0, remaining)))
                if time.monotonic() >= deadline:
                    break
                phase = "查询任务状态"
                try:
                    poll = client.get(
                        f"{settings.dashscope_base_url}/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
                        timeout=_dashscope_poll_request_timeout(deadline, phase),
                    )
                except (httpx.InvalidURL, httpx.UnsupportedProtocol):
                    raise
                except httpx.RequestError as exc:
                    consecutive_poll_failures += 1
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        logger.warning(
                            "event=dashscope_asr_poll_deadline task_id=%s "
                            "request_id=%s attempt=%s error_type=%s",
                            task_id,
                            request_id or "-",
                            consecutive_poll_failures,
                            type(exc).__name__,
                        )
                        break
                    next_poll_delay = _dashscope_poll_retry_delay(
                        consecutive_poll_failures,
                        settings.dashscope_poll_seconds,
                        remaining,
                    )
                    logger.warning(
                        "event=dashscope_asr_poll_retry task_id=%s request_id=%s "
                        "attempt=%s error_type=%s retry_in_seconds=%.3f "
                        "remaining_seconds=%.3f",
                        task_id,
                        request_id or "-",
                        consecutive_poll_failures,
                        type(exc).__name__,
                        next_poll_delay,
                        remaining,
                    )
                    continue
                consecutive_poll_failures = 0
                next_poll_delay = settings.dashscope_poll_seconds
                if poll.status_code >= 400:
                    if poll.status_code == 404:
                        raise TranscriptionError(
                            "ASR_PROVIDER_TASK_NOT_FOUND",
                            "DashScope 暂时无法找到已提交的任务",
                            True,
                            "transient",
                        )
                    raise _provider_http_error(poll)
                poll_payload = _dashscope_json_object(poll, "任务状态")
                output = _dashscope_output(poll_payload, "任务状态")
                status = str(output.get("task_status", "")).strip().upper()
                if not status:
                    raise TranscriptionError(
                        "ASR_PROVIDER_RESPONSE_INVALID", "DashScope 任务状态无法解析", True, "transient"
                    )
                if status != last_status:
                    logger.info(
                        "DashScope ASR status task_id=%s request_id=%s status=%s",
                        task_id,
                        str(poll_payload.get("request_id", request_id) or "-"),
                        status,
                    )
                    last_status = status
                if status == "FAILED":
                    failure = _dashscope_failure(poll_payload, output)
                    logger.warning(
                        "DashScope ASR failed task_id=%s code=%s message=%s",
                        task_id,
                        failure.code,
                        failure.message,
                    )
                    raise failure
                if status in {"CANCELED", "CANCELLED"}:
                    raise TranscriptionError(
                        "ASR_PROVIDER_TASK_CANCELED",
                        "DashScope 任务被供应商取消，可重新执行",
                        True,
                        "transient",
                    )
                if status == "SUCCEEDED":
                    results = output.get("results")
                    if not isinstance(results, list):
                        raise TranscriptionError(
                            "ASR_PROVIDER_RESPONSE_INVALID",
                            "DashScope 成功响应缺少 results",
                            True,
                            "transient",
                        )
                    result_urls = _dashscope_result_urls(
                        results,
                        file_urls,
                        poll_payload,
                        output,
                    )
                    break
                if status not in {"PENDING", "RUNNING"}:
                    raise TranscriptionError(
                        "ASR_PROVIDER_RESPONSE_INVALID",
                        f"DashScope 返回未知任务状态：{status[:80]}",
                        True,
                        "transient",
                    )
            if not result_urls:
                raise TranscriptionError(
                    "ASR_TIMEOUT", "DashScope 语音识别任务等待超时", True, "transient"
                )
            chunk_texts: list[str] = []
            for index, result_url in enumerate(result_urls, start=1):
                phase = f"下载转写结果（{index}/{len(result_urls)}）"
                result = _dashscope_request_with_network_retries(
                    client,
                    "GET",
                    result_url,
                    deadline=deadline,
                    phase=phase,
                    retry_event="dashscope_asr_result_retry",
                    poll_seconds=settings.dashscope_poll_seconds,
                )
                if result.status_code >= 400:
                    if result.status_code in {404, 410}:
                        raise TranscriptionError(
                            "ASR_PROVIDER_RESULT_UNAVAILABLE",
                            "DashScope 转写结果地址已失效，可重新执行",
                            True,
                            "transient",
                        )
                    raise _provider_http_error(result)
                payload = _dashscope_json_object(result, "转写结果")
                transcripts = payload.get("transcripts")
                if not isinstance(transcripts, list):
                    raise TranscriptionError(
                        "ASR_PROVIDER_RESPONSE_INVALID",
                        "DashScope 转写结果缺少 transcripts",
                        True,
                        "transient",
                    )
                chunk_text = "\n".join(
                    str(item.get("text", "")).strip()
                    for item in transcripts
                    if isinstance(item, dict) and str(item.get("text", "")).strip()
                ).strip()
                if chunk_text:
                    chunk_texts.append(chunk_text)
            text = "\n".join(chunk_texts).strip()
            if not text:
                raise TranscriptionError(
                    "ASR_NO_SPEECH", "媒体中未识别到可用语音内容", False, "input"
                )
            logger.info(
                "DashScope ASR completed task_id=%s request_id=%s text_chars=%s",
                task_id,
                request_id or "-",
                len(text),
            )
            return text, f"dashscope/{settings.asr_model}"
    except httpx.TimeoutException as exc:
        logger.warning("DashScope ASR HTTP timeout phase=%s", phase)
        raise TranscriptionError(
            "ASR_TIMEOUT", f"DashScope {phase}请求超时", True, "transient"
        ) from exc
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
