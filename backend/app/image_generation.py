from __future__ import annotations

import base64
import binascii
import io
import ipaddress
import json
import time
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings


PROVIDER_NAME: Final = "openai-compatible"
PROMPT_VERSION: Final = "image-generation-v1"
IMAGE_GENERATION_HARD_MAX_ATTEMPTS: Final[int] = 4
_TARGET_SIZES: Final = {
    "16:9": (1280, 720),
    "1:1": (1024, 1024),
    "9:16": (720, 1280),
}
_PROVIDER_SIZES: Final = {
    "16:9": "1536x1024",
    "1:1": "1024x1024",
    "9:16": "1024x1536",
}
_REQUEST_CONTEXT: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "frameflow_image_request_context", default=(None, None)
)


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    png_bytes: bytes
    width: int
    height: int
    provider: str
    model: str
    duration_ms: int
    usage: dict[str, int]
    revised_prompt: str | None = None


class ImageGenerationFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        ambiguous_submission: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.ambiguous_submission = ambiguous_submission


@contextmanager
def image_generation_request_context(
    *, model: str | None, idempotency_key: str | None
):
    """Attach durable request identity without changing provider call sites.

    Keeping the public three-argument ``generate_image`` call stable also makes
    provider fakes straightforward, while ContextVar remains thread-safe.
    """

    token = _REQUEST_CONTEXT.set((model, idempotency_key))
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validated_endpoint(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ImageGenerationFailure(
            "IMAGE_INVALID_BASE_URL", "图像生成服务地址无效"
        ) from exc
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
    ):
        raise ImageGenerationFailure("IMAGE_INVALID_BASE_URL", "图像生成服务地址无效")
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise ImageGenerationFailure(
            "IMAGE_INSECURE_BASE_URL", "图像生成服务必须使用 HTTPS"
        )
    normalized = base_url.rstrip("/")
    if normalized.endswith("/images/generations"):
        return normalized
    return f"{normalized}/images/generations"


def _read_limited_response(response: httpx.Response, limit: int) -> bytes:
    raw_length = response.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > limit:
                raise ImageGenerationFailure(
                    "IMAGE_RESPONSE_TOO_LARGE", "图像生成服务响应超过大小限制"
                )
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ImageGenerationFailure(
                "IMAGE_RESPONSE_TOO_LARGE", "图像生成服务响应超过大小限制"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _usage_from_body(body: object) -> dict[str, int]:
    if not isinstance(body, dict) or not isinstance(body.get("usage"), dict):
        return {}
    usage: dict[str, int] = {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    for canonical, fields in aliases.items():
        for field in fields:
            value = body["usage"].get(field)
            if type(value) is int and value >= 0:
                usage[canonical] = value
                break
    return usage


def _decode_and_normalize(
    encoded: str, aspect_ratio: str, settings: Settings
) -> tuple[bytes, int, int]:
    if not isinstance(encoded, str) or not encoded:
        raise ImageGenerationFailure(
            "IMAGE_INVALID_RESPONSE", "图像生成服务未返回有效图片"
        )
    estimated = (len(encoded) * 3) // 4
    if estimated > settings.image_max_output_bytes:
        raise ImageGenerationFailure(
            "IMAGE_OUTPUT_TOO_LARGE", "生成图片超过大小限制"
        )
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageGenerationFailure(
            "IMAGE_INVALID_RESPONSE", "图像生成服务返回的图片编码无效"
        ) from exc
    if not raw or len(raw) > settings.image_max_output_bytes:
        raise ImageGenerationFailure(
            "IMAGE_OUTPUT_TOO_LARGE", "生成图片超过大小限制"
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as opened:
                width, height = opened.size
                if (
                    width < 64
                    or height < 64
                    or width * height > settings.image_max_pixels
                    or getattr(opened, "n_frames", 1) != 1
                ):
                    raise ImageGenerationFailure(
                        "IMAGE_INVALID_DIMENSIONS", "生成图片尺寸无效或超过像素限制"
                    )
                opened.load()
                normalized = ImageOps.exif_transpose(opened).convert("RGB")
    except ImageGenerationFailure:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ImageGenerationFailure(
            "IMAGE_INVALID_CONTENT", "图像生成服务返回的文件不是安全的静态图片"
        ) from exc

    target = _TARGET_SIZES.get(aspect_ratio)
    if target is None:
        raise ImageGenerationFailure("IMAGE_INVALID_ASPECT_RATIO", "不支持的图片比例")
    fitted = ImageOps.fit(
        normalized,
        target,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )
    output = io.BytesIO()
    # Re-encoding removes metadata and gives the downstream asset pipeline one
    # deterministic, non-animated format irrespective of provider quirks.
    fitted.save(output, format="PNG", optimize=True)
    content = output.getvalue()
    if not content or len(content) > settings.image_max_output_bytes:
        raise ImageGenerationFailure(
            "IMAGE_NORMALIZED_TOO_LARGE", "标准化后的图片超过大小限制"
        )
    return content, target[0], target[1]


def generate_image(
    prompt: str,
    aspect_ratio: str,
    settings: Settings,
    *,
    model: str | None = None,
    idempotency_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> GeneratedImage:
    started = time.perf_counter()
    context_model, context_key = _REQUEST_CONTEXT.get()
    selected_model = (model or context_model or settings.image_model).strip()
    idempotency_key = idempotency_key or context_key
    if not settings.image_api_base_url or not settings.image_api_key or not selected_model:
        raise ImageGenerationFailure(
            "IMAGE_NOT_CONFIGURED", "图像生成服务尚未配置"
        )
    endpoint = _validated_endpoint(settings.image_api_base_url)
    provider_size = _PROVIDER_SIZES.get(aspect_ratio)
    if provider_size is None:
        raise ImageGenerationFailure("IMAGE_INVALID_ASPECT_RATIO", "不支持的图片比例")
    payload = {
        "model": selected_model,
        "prompt": prompt,
        "size": provider_size,
        "n": 1,
        "response_format": "b64_json",
    }
    timeout = httpx.Timeout(
        settings.image_timeout,
        connect=min(15.0, settings.image_timeout),
        write=min(30.0, settings.image_timeout),
        pool=min(10.0, settings.image_timeout),
    )
    client_kwargs: dict[str, object] = {
        "timeout": timeout,
        "follow_redirects": False,
        "verify": True,
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    try:
        with httpx.Client(**client_kwargs) as client:
            headers = {
                "Authorization": f"Bearer {settings.image_api_key}",
                "Content-Type": "application/json",
            }
            if idempotency_key:
                # OpenAI-compatible gateways that honor this header can avoid a
                # second bill when a worker recovers an ambiguous stale lease.
                headers["Idempotency-Key"] = idempotency_key[:200]
            with client.stream(
                "POST",
                endpoint,
                headers=headers,
                json=payload,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise ImageGenerationFailure(
                        "IMAGE_REDIRECT_REJECTED", "图像生成服务返回了不安全的重定向"
                    )
                if response.status_code >= 400:
                    retryable = response.status_code == 429
                    # This is a side-effecting POST. Any provider 5xx may be
                    # emitted after the upstream accepted or billed the work,
                    # so only an explicit user action may authorize another call.
                    ambiguous_submission = 500 <= response.status_code < 600
                    raise ImageGenerationFailure(
                        (
                            "IMAGE_PROVIDER_RESULT_UNKNOWN"
                            if ambiguous_submission
                            else (
                                "IMAGE_PROVIDER_BUSY"
                                if retryable
                                else "IMAGE_PROVIDER_REJECTED"
                            )
                        ),
                        (
                            "服务商请求结果未知，请确认任务状态后再手动重试"
                            if ambiguous_submission
                            else (
                                "图像生成服务暂时繁忙，请稍后重试"
                                if retryable
                                else f"图像生成请求被服务拒绝（HTTP {response.status_code}）"
                            )
                        ),
                        retryable=retryable,
                        ambiguous_submission=ambiguous_submission,
                    )
                response_body = _read_limited_response(
                    response, settings.image_max_response_bytes
                )
    except ImageGenerationFailure:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise ImageGenerationFailure(
            "IMAGE_CONNECT_FAILED", "无法连接图像生成服务，请稍后重试", retryable=True
        ) from exc
    except httpx.TimeoutException as exc:
        # A read timeout can happen after the provider accepted and billed the
        # request. Do not auto-retry an ambiguous external side effect.
        raise ImageGenerationFailure(
            "IMAGE_PROVIDER_TIMEOUT",
            "图像生成服务响应超时，请确认后手动重试",
            ambiguous_submission=True,
        ) from exc
    except httpx.RequestError as exc:
        raise ImageGenerationFailure(
            "IMAGE_NETWORK_ERROR",
            "图像生成响应中断，请确认后手动重试",
            ambiguous_submission=True,
        ) from exc

    try:
        body = json.loads(response_body)
        if not isinstance(body, dict):
            raise TypeError("response must be an object")
        items = body.get("data")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise TypeError("response data must contain exactly one image")
        encoded = items[0].get("b64_json")
        revised_prompt = items[0].get("revised_prompt")
        if revised_prompt is not None and not isinstance(revised_prompt, str):
            revised_prompt = None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ImageGenerationFailure(
            "IMAGE_INVALID_RESPONSE", "图像生成服务返回格式无效"
        ) from exc
    png, width, height = _decode_and_normalize(encoded, aspect_ratio, settings)
    return GeneratedImage(
        png_bytes=png,
        width=width,
        height=height,
        provider=PROVIDER_NAME,
        model=selected_model,
        duration_ms=max(0, int((time.perf_counter() - started) * 1_000)),
        usage=_usage_from_body(body),
        revised_prompt=(revised_prompt or "")[:2_000] or None,
    )
