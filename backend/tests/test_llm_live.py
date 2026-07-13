"""Live integration tests against real external providers.

These are skipped by default (the ``-m 'not live'`` filter in pyproject.toml).
Run them locally after deploying or whenever a provider key/model changes:

    FRAMEFLOW_RUN_LIVE=1 python -m pytest -m live tests/test_llm_live.py -s

They never print API keys. Each test reads provider config from the real
environment (.env must already be loaded into the process environment).
"""
from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.llm import enhance_semantic_segments

pytestmark = pytest.mark.live

_TRANSCRIPT = (
    "人工智能正在改变我们的工作方式。未来的智能设备会在端侧完成推理。"
    "合理的计划能让办公效率显著提升。"
)


def _live_enabled() -> bool:
    return os.getenv("FRAMEFLOW_RUN_LIVE", "0") == "1"


@pytest.fixture(autouse=True)
def _require_live():
    if not _live_enabled():
        pytest.skip("FRAMEFLOW_RUN_LIVE 未设置为 1，跳过真实 provider 连通性测试")
    if not os.getenv("LLM_API_KEY"):
        pytest.skip("LLM_API_KEY 未配置，跳过 LLM 真实连通性测试")


def test_deepseek_returns_segments_without_degradation():
    """A configured DeepSeek-compatible provider must return real segments.

    Guards against silent degradation: if the model name, json_schema support,
    or connectivity is broken, ``enhance_semantic_segments`` currently swallows
    the error and returns degraded rules output. This test fails loudly in that
    case so the operator fixes the provider before demoing.
    """
    settings = Settings.from_env()
    assert settings.llm_provider in {"openai", "openai-compatible", "deepseek"}, (
        f"LLM_PROVIDER 当前为 {settings.llm_provider!r}，无法验证 LLM 真实路径"
    )
    result = enhance_semantic_segments(_TRANSCRIPT, settings)
    assert result.segments, "LLM 未返回任何片段"
    assert not result.degraded, (
        f"LLM 路径静默降级，演示时会误以为在用 LLM。错误：{result.error_message}"
    )
    assert result.provider != "rules", "provider 仍为 rules，说明 LLM 未真正生效"
    # Source text must be preserved byte-for-byte by the model segmentation.
    from app.nlp import clean_transcript

    expected = "".join(clean_transcript(_TRANSCRIPT).split())
    actual = "".join("".join(seg["text"] for seg in result.segments).split())
    assert actual == expected, "模型分段未完整保留原字幕"
    # Key must never leak into the persisted error message.
    if settings.llm_api_key:
        assert settings.llm_api_key not in (result.error_message or "")
