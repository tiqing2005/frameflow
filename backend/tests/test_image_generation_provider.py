from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.image_generation import ImageGenerationFailure, generate_image


def png_bytes(width: int = 96, height: int = 64) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (32, 96, 160)).save(output, format="PNG")
    return output.getvalue()


def image_settings(tmp_path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "database_url": f"sqlite:///{(tmp_path / 'data' / 'test.db').as_posix()}",
        "image_api_base_url": "https://images.example.invalid/v1",
        "image_api_key": "unit-test-image-key",
        "image_model": "unit-image-model",
        "image_timeout": 1,
        "image_max_response_bytes": 2 * 1024 * 1024,
        "image_max_output_bytes": 1024 * 1024,
        "image_max_pixels": 2_000_000,
        "frontend_dir": tmp_path / "frontend",
    }
    values.update(overrides)
    return Settings(**values)


def response_transport(content: bytes, *, status_code: int = 200, headers=None):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content, headers=headers or {})

    return httpx.MockTransport(handler)


def json_transport(body: dict, *, status_code: int = 200):
    return response_transport(
        json.dumps(body, ensure_ascii=False).encode("utf-8"), status_code=status_code
    )


def test_provider_posts_one_base64_image_and_normalizes_png(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(png_bytes()).decode("ascii"),
                        "revised_prompt": "整理后的提示词",
                    }
                ],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    result = generate_image(
        "城市夜景，无文字",
        "16:9",
        image_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )

    assert captured["url"] == "https://images.example.invalid/v1/images/generations"
    assert captured["authorization"] == "Bearer unit-test-image-key"
    assert captured["body"] == {
        "model": "unit-image-model",
        "prompt": "城市夜景，无文字",
        "size": "1536x1024",
        "n": 1,
        "response_format": "b64_json",
    }
    assert result.provider == "openai-compatible"
    assert result.model == "unit-image-model"
    assert result.revised_prompt == "整理后的提示词"
    assert result.usage == {"input_tokens": 7, "total_tokens": 7}
    with Image.open(io.BytesIO(result.png_bytes)) as generated:
        assert generated.format == "PNG"
        assert generated.mode == "RGB"
        assert generated.size == (1280, 720)
        assert not generated.info


@pytest.mark.parametrize(
    ("aspect_ratio", "provider_size", "normalized_size"),
    [
        ("1:1", "1024x1024", (1024, 1024)),
        ("9:16", "1024x1536", (720, 1280)),
    ],
)
def test_provider_maps_supported_aspect_ratios(
    tmp_path, aspect_ratio, provider_size, normalized_size
):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"b64_json": base64.b64encode(png_bytes()).decode("ascii")}
                ]
            },
        )

    result = generate_image(
        "测试画幅",
        aspect_ratio,
        image_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )

    assert captured["size"] == provider_size
    assert (result.width, result.height) == normalized_size


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ({"data": [{"b64_json": "%%%not-base64%%%"}]}, "IMAGE_INVALID_RESPONSE"),
        (
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(b"plain text, not an image").decode(
                            "ascii"
                        )
                    }
                ]
            },
            "IMAGE_INVALID_CONTENT",
        ),
        ({"data": [{"url": "http://127.0.0.1/private"}]}, "IMAGE_INVALID_RESPONSE"),
        ({"data": []}, "IMAGE_INVALID_RESPONSE"),
        ({"data": [{"b64_json": "AA=="}, {"b64_json": "AA=="}]}, "IMAGE_INVALID_RESPONSE"),
    ],
)
def test_provider_rejects_malformed_or_url_only_results(tmp_path, body, expected_code):
    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "不可信响应",
            "1:1",
            image_settings(tmp_path),
            transport=json_transport(body),
        )

    assert captured.value.code == expected_code
    assert captured.value.retryable is False


def test_provider_rejects_response_larger_than_limit_before_json_parse(tmp_path):
    settings = image_settings(tmp_path, image_max_response_bytes=32)

    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "过大响应",
            "1:1",
            settings,
            transport=response_transport(b"{" + b"x" * 64 + b"}"),
        )

    assert captured.value.code == "IMAGE_RESPONSE_TOO_LARGE"


def test_provider_rejects_decoded_output_larger_than_limit(tmp_path):
    settings = image_settings(tmp_path, image_max_output_bytes=32)
    body = {"data": [{"b64_json": base64.b64encode(b"x" * 64).decode("ascii")}]}

    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image("过大图片", "1:1", settings, transport=json_transport(body))

    assert captured.value.code == "IMAGE_OUTPUT_TOO_LARGE"


def test_provider_rejects_image_over_pixel_limit(tmp_path):
    settings = image_settings(tmp_path, image_max_pixels=10_000)
    body = {
        "data": [
            {"b64_json": base64.b64encode(png_bytes(200, 200)).decode("ascii")}
        ]
    }

    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image("过大像素", "1:1", settings, transport=json_transport(body))

    assert captured.value.code == "IMAGE_INVALID_DIMENSIONS"


def test_provider_rate_limit_is_safe_for_bounded_automatic_retry(tmp_path):
    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "繁忙",
            "1:1",
            image_settings(tmp_path),
            transport=response_transport(b"provider detail must not escape", status_code=429),
        )

    assert captured.value.code == "IMAGE_PROVIDER_BUSY"
    assert captured.value.retryable is True
    assert captured.value.ambiguous_submission is False
    assert "provider detail" not in captured.value.message


@pytest.mark.parametrize("status_code", [500, 501, 502, 503, 504, 599])
def test_provider_server_errors_require_manual_retry_as_ambiguous(
    tmp_path, status_code
):
    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "网关结果未知",
            "1:1",
            image_settings(tmp_path),
            transport=response_transport(
                b"provider detail must not escape", status_code=status_code
            ),
        )

    assert captured.value.code == "IMAGE_PROVIDER_RESULT_UNKNOWN"
    assert captured.value.retryable is False
    assert captured.value.ambiguous_submission is True
    assert "provider detail" not in captured.value.message


@pytest.mark.parametrize(
    "error_type", [httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError]
)
def test_provider_post_submission_network_errors_are_manual_and_ambiguous(
    tmp_path, error_type
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("unit test interrupted provider response", request=request)

    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "网络中断结果未知",
            "1:1",
            image_settings(tmp_path),
            transport=httpx.MockTransport(handler),
        )

    assert captured.value.code == "IMAGE_NETWORK_ERROR"
    assert captured.value.retryable is False
    assert captured.value.ambiguous_submission is True


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_provider_connection_failures_are_safe_for_bounded_automatic_retry(
    tmp_path, error_type
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("unit test connection failure", request=request)

    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "连接建立失败",
            "1:1",
            image_settings(tmp_path),
            transport=httpx.MockTransport(handler),
        )

    assert captured.value.code == "IMAGE_CONNECT_FAILED"
    assert captured.value.retryable is True
    assert captured.value.ambiguous_submission is False


def test_provider_read_timeout_is_ambiguous_and_not_automatically_retryable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("unit test timeout", request=request)

    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "超时",
            "1:1",
            image_settings(tmp_path),
            transport=httpx.MockTransport(handler),
        )

    assert captured.value.code == "IMAGE_PROVIDER_TIMEOUT"
    assert captured.value.retryable is False
    assert captured.value.ambiguous_submission is True


def test_provider_rejects_redirect_without_following_it(tmp_path):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "重定向",
            "1:1",
            image_settings(tmp_path),
            transport=httpx.MockTransport(handler),
        )

    assert captured.value.code == "IMAGE_REDIRECT_REJECTED"
    assert calls == 1


def test_provider_configuration_is_required_and_not_synthesized(tmp_path):
    with pytest.raises(ImageGenerationFailure) as captured:
        generate_image(
            "未配置",
            "1:1",
            image_settings(tmp_path, image_api_key=None),
        )

    assert captured.value.code == "IMAGE_NOT_CONFIGURED"
