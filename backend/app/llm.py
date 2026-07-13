from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import Settings
from .nlp import clean_transcript, extract_keywords, infer_topic, segment_text


PROMPT_VERSION = "semantic-segments-v1"


class SemanticSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=500)
    topic: str = Field(min_length=1, max_length=40)
    keywords: list[str] = Field(min_length=1, max_length=5)

    @field_validator("keywords")
    @classmethod
    def unique_keywords(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned or len(cleaned) != len(set(cleaned)):
            raise ValueError("keywords must be non-empty and unique")
        return cleaned


class SemanticSegments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    segments: list[SemanticSegment] = Field(min_length=1, max_length=50)


class ChatProvider(Protocol):
    name: str

    def complete_json(self, transcript: str) -> SemanticSegments: ...


class LLMResponseError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.usage: dict[str, int] = {}

    def complete_json(self, transcript: str) -> SemanticSegments:
        schema = SemanticSegments.model_json_schema()
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是中文视频字幕编辑。只输出符合给定 JSON Schema 的 JSON。"
                        "按语义分段，但每段 text 必须逐字来自原文；不得改写、遗漏或调换内容。"
                        "topic 用简短中文主题，keywords 提取 3-5 个不同关键词。Schema: "
                        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "frameflow_semantic_segments",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        endpoint = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        response = httpx.post(endpoint, headers=headers, json=payload, timeout=self.timeout)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Some DeepSeek-compatible gateways implement JSON mode but not the
            # newer json_schema request field. Local Pydantic validation remains strict.
            if exc.response.status_code not in {400, 422}:
                raise
            payload["response_format"] = {"type": "json_object"}
            response = httpx.post(endpoint, headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage")
            self.usage = {}
            if isinstance(usage, dict):
                for canonical, provider_field in (
                    ("input_tokens", "prompt_tokens"),
                    ("output_tokens", "completion_tokens"),
                    ("total_tokens", "total_tokens"),
                ):
                    if provider_field in usage and usage[provider_field] is not None:
                        self.usage[canonical] = int(usage[provider_field])
            raw = json.loads(content)
            result = SemanticSegments.model_validate(raw)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise LLMResponseError("模型返回内容不符合语义分段 JSON Schema") from exc
        expected = "".join(clean_transcript(transcript).split())
        actual = "".join("".join(item.text for item in result.segments).split())
        if actual != expected:
            raise LLMResponseError("模型分段未完整保留原字幕")
        return result


@dataclass(frozen=True, slots=True)
class SemanticEnhancement:
    segments: list[dict]
    provider: str
    model: str
    degraded: bool
    status: str
    duration_ms: int
    error_message: str | None = None
    usage: dict[str, int] | None = None


class AssetTags(BaseModel):
    """Suggested tags/keywords for a newly uploaded material."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tags: list[str] = Field(min_length=0, max_length=6)
    keywords: list[str] = Field(min_length=0, max_length=8)


@dataclass(frozen=True, slots=True)
class AssetTagSuggestion:
    tags: list[str]
    keywords: list[str]
    provider: str
    model: str
    status: str
    degraded: bool
    duration_ms: int
    error_message: str | None = None
    usage: dict[str, int] | None = None


def suggest_asset_tags_detailed(
    name: str, description: str, settings: Settings
) -> AssetTagSuggestion:
    """Suggest Chinese tags and keywords for an uploaded material via the LLM.

    The detailed result records provider/model/version outcome. When the model
    is disabled or fails, the caller can apply deterministic rule keywords and
    still persist an honest degraded trace. API keys are always redacted.
    """
    started = time.perf_counter()
    configured = settings.llm_provider
    if configured in {"", "rules", "off", "disabled"} or not settings.llm_api_key:
        return AssetTagSuggestion(
            [], [], "rules", "rule-keywords-v1", "degraded", True, 0,
            "LLM 未启用，使用规则关键词回退", {},
        )
    schema = AssetTags.model_json_schema()
    payload = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是中文素材标注助手。根据素材名称和描述，输出适合检索的中文主题标签(tags)"
                    "和画面关键词(keywords)。只输出符合 JSON Schema 的 JSON。Schema: "
                    + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                ),
            },
            {
                "role": "user",
                "content": f"名称：{name}\n描述：{description or name}",
            },
        ],
        "response_format": {"type": "json_object"},
    }
    endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    try:
        response = httpx.post(endpoint, headers=headers, json=payload, timeout=settings.llm_timeout)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        result = AssetTags.model_validate(json.loads(content))
        tags = [t.strip()[:20] for t in result.tags if t.strip()][:6]
        keywords = [k.strip()[:20] for k in result.keywords if k.strip()][:8]
        usage_payload = body.get("usage") if isinstance(body, dict) else None
        usage: dict[str, int] = {}
        if isinstance(usage_payload, dict):
            for canonical, provider_field in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = usage_payload.get(provider_field)
                if value is not None:
                    usage[canonical] = int(value)
        return AssetTagSuggestion(
            tags,
            keywords,
            configured,
            settings.llm_model,
            "succeeded",
            False,
            max(0, int((time.perf_counter() - started) * 1_000)),
            usage=usage,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if settings.llm_api_key:
            message = message.replace(settings.llm_api_key, "[REDACTED]")
        return AssetTagSuggestion(
            [],
            [],
            configured or "openai-compatible",
            settings.llm_model,
            "degraded",
            True,
            max(0, int((time.perf_counter() - started) * 1_000)),
            message[:500],
            {},
        )


def suggest_asset_tags(
    name: str, description: str, settings: Settings
) -> tuple[list[str], list[str]]:
    suggestion = suggest_asset_tags_detailed(name, description, settings)
    return suggestion.tags, suggestion.keywords


def rule_segments(transcript: str) -> list[dict]:
    result = []
    for text in segment_text(transcript):
        keywords = extract_keywords(text, top_k=5)
        result.append({"text": text, "topic": infer_topic(text, keywords), "keywords": keywords})
    return result


def enhance_semantic_segments(
    transcript: str,
    settings: Settings,
    provider: ChatProvider | None = None,
) -> SemanticEnhancement:
    """Use an optional chat model and always return deterministic rule output on failure."""
    started = time.perf_counter()
    fallback = rule_segments(transcript)
    configured = settings.llm_provider
    if configured in {"", "rules", "off", "disabled"}:
        return SemanticEnhancement(fallback, "rules", "rule-nlp-v1", False, "succeeded", 0, usage={})
    if configured not in {"openai", "openai-compatible", "deepseek"}:
        return SemanticEnhancement(
            fallback,
            configured,
            settings.llm_model,
            True,
            "degraded",
            0,
            f"不支持的 LLM_PROVIDER: {configured}",
        )
    if not settings.llm_api_key and provider is None:
        return SemanticEnhancement(
            fallback,
            "openai-compatible",
            settings.llm_model,
            True,
            "degraded",
            0,
            "LLM_API_KEY 未配置，已使用规则降级",
        )
    client = provider or OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "",
        model=settings.llm_model,
        timeout=settings.llm_timeout,
    )
    try:
        result = client.complete_json(transcript)
        segments = [item.model_dump() for item in result.segments]
        return SemanticEnhancement(
            segments,
            client.name,
            settings.llm_model,
            False,
            "succeeded",
            max(0, int((time.perf_counter() - started) * 1000)),
            usage=getattr(client, "usage", {}),
        )
    except Exception as exc:
        message = "LLM 请求超时，已使用规则降级" if isinstance(exc, httpx.TimeoutException) else str(exc)
        if settings.llm_api_key:
            message = message.replace(settings.llm_api_key, "[REDACTED]")
        return SemanticEnhancement(
            fallback,
            client.name,
            settings.llm_model,
            True,
            "degraded",
            max(0, int((time.perf_counter() - started) * 1000)),
            message[:500],
        )
