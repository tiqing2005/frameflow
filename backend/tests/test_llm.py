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
