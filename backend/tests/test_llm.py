from __future__ import annotations

import json

import httpx

from app.config import Settings
from app.llm import OpenAICompatibleProvider, SemanticSegment, SemanticSegments, enhance_semantic_segments


TEXT = "人工智能提升工作效率。数据安全保护用户隐私。"


def settings(tmp_path, **overrides):
    values = {
        "data_dir": tmp_path,
        "database_url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        "llm_provider": "openai-compatible",
        "llm_api_key": "test-secret-key",
        "llm_model": "deepseek-chat",
        "llm_timeout": 0.2,
    }
    values.update(overrides)
    return Settings(**values)


def test_openai_compatible_semantic_enhancement_success(tmp_path):
    class FakeProvider:
        name = "openai-compatible"

        def complete_json(self, transcript):
            assert transcript == TEXT
            return SemanticSegments(
                segments=[
                    SemanticSegment(text="人工智能提升工作效率。", topic="智能办公", keywords=["人工智能", "效率"]),
                    SemanticSegment(text="数据安全保护用户隐私。", topic="数据安全", keywords=["安全", "隐私"]),
                ]
            )

    result = enhance_semantic_segments(TEXT, settings(tmp_path), provider=FakeProvider())
    assert result.degraded is False
    assert result.provider == "openai-compatible"
    assert [item["topic"] for item in result.segments] == ["智能办公", "数据安全"]


def test_builtin_deepseek_transport_retains_vendor_in_trace(tmp_path, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {
                                            "text": TEXT,
                                            "topic": "智能与安全",
                                            "keywords": ["人工智能", "数据安全", "用户隐私"],
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: FakeResponse())
    result = enhance_semantic_segments(
        TEXT,
        settings(tmp_path, llm_provider="deepseek", llm_model="DeepSeek-V4-Pro"),
    )

    assert result.degraded is False
    assert result.provider == "deepseek"
    assert result.model == "DeepSeek-V4-Pro"


def test_builtin_gemini_uses_compatible_transport_and_retains_vendor_in_trace(
    tmp_path, monkeypatch
):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "segments": [
                                        {
                                            "text": TEXT,
                                            "topic": "智能与安全",
                                            "keywords": ["人工智能", "数据安全", "用户隐私"],
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    def fake_post(endpoint, headers=None, json=None, timeout=None):
        captured["endpoint"] = endpoint
        captured["authorization"] = headers["Authorization"]
        captured["model"] = json["model"]
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = enhance_semantic_segments(
        TEXT,
        settings(
            tmp_path,
            llm_provider="gemini",
            llm_base_url="https://gemini-gateway.example.invalid/v1",
            llm_model="gemini-3.1-flash-lite-preview",
        ),
    )

    assert result.degraded is False
    assert result.provider == "gemini"
    assert result.model == "gemini-3.1-flash-lite-preview"
    assert captured == {
        "endpoint": "https://gemini-gateway.example.invalid/v1/chat/completions",
        "authorization": "Bearer test-secret-key",
        "model": "gemini-3.1-flash-lite-preview",
        "timeout": 0.2,
    }


def test_gemini_without_api_key_degrades_with_vendor_trace(tmp_path, monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("缺少 API Key 时不应发起模型请求")

    monkeypatch.setattr(httpx, "post", fail_if_called)

    result = enhance_semantic_segments(
        TEXT,
        settings(
            tmp_path,
            llm_provider="gemini",
            llm_api_key=None,
            llm_model="gemini-3.1-flash-lite-preview",
        ),
    )

    assert result.degraded is True
    assert result.provider == "gemini"
    assert result.model == "gemini-3.1-flash-lite-preview"
    assert "LLM_API_KEY 未配置" in result.error_message


def test_invalid_json_schema_response_degrades_to_rules(tmp_path, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"segments": [{"text": TEXT}]})}}]}

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: FakeResponse())
    result = enhance_semantic_segments(TEXT, settings(tmp_path))
    assert result.degraded is True
    assert result.status == "degraded"
    assert "JSON Schema" in result.error_message
    assert "".join(item["text"] for item in result.segments) == TEXT


def test_provider_usage_is_absent_unless_explicitly_reported(monkeypatch):
    content = json.dumps(
        {
            "segments": [
                {
                    "text": TEXT,
                    "topic": "测试主题",
                    "keywords": ["人工智能", "数据安全", "用户隐私"],
                }
            ]
        }
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: FakeResponse())
    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
        timeout=1,
    )

    provider.complete_json(TEXT)

    assert provider.usage == {}


def test_provider_explicit_zero_usage_is_preserved(monkeypatch):
    content = json.dumps(
        {
            "segments": [
                {
                    "text": TEXT,
                    "topic": "测试主题",
                    "keywords": ["人工智能", "数据安全", "用户隐私"],
                }
            ]
        }
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: FakeResponse())
    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
        timeout=1,
    )

    provider.complete_json(TEXT)

    assert provider.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def test_timeout_degrades_without_exposing_api_key(tmp_path):
    class TimeoutProvider:
        name = "openai-compatible"

        def complete_json(self, transcript):
            raise httpx.ReadTimeout("timed out")

    result = enhance_semantic_segments(TEXT, settings(tmp_path), provider=TimeoutProvider())
    assert result.degraded is True
    assert "超时" in result.error_message
    assert "test-secret-key" not in result.error_message
    assert "".join(item["text"] for item in result.segments) == TEXT


def test_suggest_asset_tags_returns_empty_when_rules(tmp_path):
    from app.llm import suggest_asset_tags

    s = settings(tmp_path, llm_provider="rules")
    assert suggest_asset_tags("城市夜景", "", s) == ([], [])


def test_suggest_asset_tags_without_key_retains_configured_vendor(tmp_path):
    from app.llm import suggest_asset_tags_detailed

    result = suggest_asset_tags_detailed(
        "城市夜景",
        "",
        settings(
            tmp_path,
            llm_provider="gemini",
            llm_api_key=None,
            llm_model="gemini-3.1-flash-lite-preview",
        ),
    )

    assert result.degraded is True
    assert result.provider == "gemini"
    assert result.model == "gemini-3.1-flash-lite-preview"
    assert "LLM_API_KEY 未配置" in result.error_message


def test_suggest_asset_tags_returns_tags_on_success(tmp_path, monkeypatch):
    from app import llm as llm_mod
    from app.llm import suggest_asset_tags

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {"message": {"content": json.dumps({"tags": ["城市", "夜景"], "keywords": ["航拍", "灯光"]})}}
                ]
            }

    captured = {}

    def fake_post(endpoint, headers=None, json=None, timeout=None):
        captured["endpoint"] = endpoint
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    tags, keywords = suggest_asset_tags("城市夜景", "", settings(tmp_path))
    assert tags == ["城市", "夜景"]
    assert keywords == ["航拍", "灯光"]
    assert "Bearer test-secret-key" == captured["headers"]["Authorization"]


def test_suggest_asset_tags_returns_empty_on_failure(tmp_path, monkeypatch):
    from app import llm as llm_mod
    from app.llm import suggest_asset_tags

    def fake_post(endpoint, headers=None, json=None, timeout=None):
        raise httpx.ConnectError("provider down")

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    tags, keywords = suggest_asset_tags("城市夜景", "", settings(tmp_path))
    assert tags == [] and keywords == []
