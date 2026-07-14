from __future__ import annotations

import base64
import json

import httpx

from app.config import Settings
from app.llm import suggest_asset_tags_detailed
from app.vision import suggest_visual_asset_tags


JPEG = b"\xff\xd8\xff\xe0normalized-jpeg\xff\xd9"


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "database_url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        "vision_provider": "openai-compatible",
        "vision_base_url": "https://vision.example.invalid/v1",
        "vision_api_key": "vision-secret-key",
        "vision_model": "gpt-4o-mini",
        "vision_timeout": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


def _success_transport(payload: dict | None = None, *, capture: dict | None = None):
    result = payload or {
        "tags": ["城市", "夜景"],
        "keywords": ["高楼", "灯光", "街道"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.read())
        if capture is not None:
            capture["url"] = str(request.url)
            capture["authorization"] = request.headers["Authorization"]
            capture["payload"] = request_payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(result, ensure_ascii=False)}}
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            },
        )

    return httpx.MockTransport(handler)


def test_visual_success_uses_multimodal_json_object_payload(tmp_path):
    captured: dict = {}

    result = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path),
        transport=_success_transport(capture=captured),
    )

    assert result.status == "succeeded"
    assert result.degraded is False
    assert result.tags == ["城市", "夜景"]
    assert result.keywords == ["高楼", "灯光", "街道"]
    assert result.usage == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    }
    assert captured["url"] == "https://vision.example.invalid/v1/chat/completions"
    assert captured["authorization"] == "Bearer vision-secret-key"
    payload = captured["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    user_content = payload["messages"][1]["content"]
    assert [part["type"] for part in user_content] == ["text", "image_url"]
    data_url = user_content[1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_url.partition(",")[2]) == JPEG
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "filename" not in serialized.casefold()
    assert "storage_path" not in serialized


def test_visual_output_is_nonempty_deduplicated_and_truncated(tmp_path):
    long_tag = "这是一个长度明显超过二十个字符的中文画面主题标签"
    payload = {
        "tags": [" 城市 ", "城市", long_tag, "夜景", "灯光", "建筑", "街道", "多余"],
        "keywords": [" 高楼 ", "高楼", "车流"],
    }

    result = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path),
        transport=_success_transport(payload),
    )

    assert result.status == "succeeded"
    assert result.tags[0] == "城市"
    assert len(result.tags) == 6
    assert len(result.tags[1]) == 20
    assert result.keywords == ["高楼", "车流"]


def test_visual_empty_labels_are_an_invalid_response(tmp_path):
    result = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path),
        transport=_success_transport({"tags": [], "keywords": []}),
    )

    assert result.status == "degraded"
    assert result.error_code == "vision_invalid_response"
    assert result.tags == [] and result.keywords == []


def test_visual_disabled_or_missing_key_is_honestly_degraded(tmp_path):
    disabled = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path, vision_provider="none", vision_api_key=None),
    )
    missing_key = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path, vision_api_key=None),
    )

    assert disabled.error_code == "vision_not_configured"
    assert disabled.provider == "none"
    assert missing_key.error_code == "vision_not_configured"
    assert missing_key.degraded is True


def test_visual_rejects_plain_http_except_loopback(tmp_path):
    called = False

    def should_not_call(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    rejected = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path, vision_base_url="http://vision.example.invalid/v1"),
        transport=httpx.MockTransport(should_not_call),
    )
    accepted = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path, vision_base_url="http://127.0.0.1:8001/v1"),
        transport=_success_transport(),
    )

    assert rejected.error_code == "vision_invalid_base_url"
    assert called is False
    assert accepted.status == "succeeded"


def test_visual_does_not_follow_redirects(tmp_path):
    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "https://elsewhere.invalid/v1"})

    result = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path),
        transport=httpx.MockTransport(redirect),
    )

    assert result.error_code == "vision_redirect_rejected"
    assert "elsewhere.invalid" not in (result.error_message or "")


def test_visual_caps_streamed_response_at_256_kib(tmp_path):
    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (256 * 1024 + 1))

    result = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path),
        transport=httpx.MockTransport(oversized),
    )

    assert result.error_code == "vision_response_too_large"


def test_visual_error_messages_never_expose_provider_details(tmp_path):
    secret = "vision-secret-key"
    sensitive_url = "https://vision.example.invalid/private/customer"
    private_path = r"C:\private\customer.jpg"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"{secret} {sensitive_url} {private_path} provider body",
            request=request,
        )

    result = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path, vision_api_key=secret),
        transport=httpx.MockTransport(broken),
    )

    assert result.error_code == "vision_network_error"
    safe = result.error_message or ""
    assert secret not in safe
    assert sensitive_url not in safe
    assert private_path not in safe
    assert "provider body" not in safe


def test_visual_http_error_records_only_status(tmp_path):
    def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret provider response body")

    result = suggest_visual_asset_tags(
        JPEG,
        _settings(tmp_path),
        transport=httpx.MockTransport(forbidden),
    )

    assert result.error_code == "vision_http_error"
    assert "401" in (result.error_message or "")
    assert "secret provider response body" not in (result.error_message or "")


def test_text_tag_fallback_uses_fixed_safe_error_summary(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        llm_provider="openai-compatible",
        llm_base_url="https://text.example.invalid/v1",
        llm_api_key="text-secret-key",
        llm_model="text-model",
    )

    def broken(*_args, **_kwargs):
        raise httpx.ConnectError(
            r"text-secret-key https://text.example.invalid/private C:\private\asset.jpg"
        )

    monkeypatch.setattr("app.llm.httpx.post", broken)
    result = suggest_asset_tags_detailed("素材名", "素材描述", settings)

    assert result.status == "degraded"
    assert result.error_message == "文本标签模型网络请求失败，已转入规则降级流程"
    assert "text-secret-key" not in (result.error_message or "")
