from __future__ import annotations

import sys
import threading
import time
import types

import httpx
import pytest

from app import asr
from app.asr import TranscriptionError, transcribe_file
from app.config import Settings
from app.nlp import (
    char_ngram_tfidf_cosines,
    extract_keywords,
    infer_topic,
    rank_assets,
    segment_text,
)


def test_rule_segmentation_handles_new_chinese_copy_and_long_sentences():
    text = (
        "过去我们依赖纸质报表。现在人工智能可以帮助团队分析数据、识别风险并提高工作效率，"
        "但真正重要的是建立清晰可靠的协作流程。\n最后，每个人都能把更多时间投入创造性工作。"
    )
    segments = segment_text(text, min_chars=12, max_chars=46)
    assert 2 <= len(segments) <= 5
    assert "".join(segments).replace("\n", "") == text.replace("\n", "")
    assert all(len(item) <= 46 for item in segments)


def test_keywords_and_topic_are_domain_aware_but_not_sample_hardcoded():
    text = "企业需要保护用户隐私，用网络安全策略降低云端数据泄露风险。"
    keywords = extract_keywords(text)
    assert len(keywords) >= 3
    assert any("安全" in value or "隐私" in value for value in keywords)
    assert infer_topic(text, keywords) == "数据安全"


def test_character_ngram_tfidf_and_hybrid_formula_are_transparent():
    assets = [
        {"id": "secure", "name": "数据安全", "tags": ["安全", "隐私"], "keywords": ["密码", "保护"]},
        {"id": "forest", "name": "绿色森林", "tags": ["自然", "环保"], "keywords": ["生态", "树木"]},
        {"id": "office", "name": "高效办公", "tags": ["办公", "效率"], "keywords": ["工作", "计划"]},
        {"id": "travel", "name": "旅行探索", "tags": ["旅行"], "keywords": ["地图", "远方"]},
    ]
    scores = char_ngram_tfidf_cosines("保护数据安全和用户隐私", ["数据安全 隐私 保护", "森林树木生态"])
    assert scores[0] > scores[1]
    ranked = rank_assets("保护数据安全和用户隐私", "数据安全", ["数据", "安全", "隐私"], assets)
    assert ranked[0].asset_id == "secure"
    assert len(ranked) >= 3
    assert len({item.asset_id for item in ranked}) == len(ranked)
    for item in ranked:
        assert item.total_score == pytest.approx(
            0.55 * item.tfidf_score + 0.30 * item.keyword_score + 0.15 * item.tag_score,
            abs=2e-6,
        )
        assert item.explanation


def test_real_local_asr_provider_is_lazy_and_mockable(tmp_path, monkeypatch):
    class FakeOpenCC:
        def __init__(self, config):
            assert config == "t2s"

        def convert(self, text):
            return text.replace("識別", "识别")

    class FakeSegment:
        text = " 本地識別成功 "

    class FakeModel:
        def __init__(self, model, device, compute_type, download_root):
            assert model == "tiny"
            assert device == "cpu"
            assert compute_type == "int8"
            assert download_root == str(tmp_path / "models" / "whisper")

        def transcribe(self, path, **kwargs):
            assert kwargs["language"] == "zh"
            assert "简体中文" in kwargs["initial_prompt"]
            assert kwargs["vad_filter"] is True
            return iter([FakeSegment()]), object()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setitem(sys.modules, "opencc", types.SimpleNamespace(OpenCC=FakeOpenCC))
    asr._LOCAL_MODELS.clear()
    media = tmp_path / "speech.mp4"
    media.write_bytes(b"fake-media-for-mocked-decoder")
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        asr_provider="local",
    )
    text, provider = transcribe_file(media, "video/mp4", settings)
    assert text == "本地识别成功"
    assert provider == "faster-whisper/tiny"


def test_invalid_asr_provider_is_structured(tmp_path):
    media = tmp_path / "speech.wav"
    media.write_bytes(b"not-used")
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        asr_provider="invalid",
    )
    with pytest.raises(TranscriptionError) as caught:
        transcribe_file(media, "audio/wav", settings)
    assert caught.value.code == "ASR_PROVIDER_INVALID"
    assert caught.value.retryable is True
    assert caught.value.category == "configuration"


@pytest.mark.parametrize(
    ("status", "payload", "code", "category", "retryable"),
    [
        (401, {"error": {"message": "invalid api key"}}, "ASR_PROVIDER_AUTH_ERROR", "configuration", True),
        (400, {"error": {"message": "model does not exist"}}, "ASR_PROVIDER_CONFIGURATION_ERROR", "configuration", True),
        (422, {"error": {"message": "invalid audio file"}}, "ASR_INPUT_REJECTED", "input", False),
        (429, {"error": {"message": "rate limit"}}, "ASR_PROVIDER_RATE_LIMITED", "transient", True),
        (503, {"error": {"message": "unavailable"}}, "ASR_PROVIDER_UNAVAILABLE", "transient", True),
    ],
)
def test_provider_errors_have_actionable_classification(
    status, payload, code, category, retryable
):
    request = httpx.Request("POST", "https://asr.example.test/audio/transcriptions")
    failure = asr._provider_http_error(httpx.Response(status, request=request, json=payload))
    assert failure.code == code
    assert failure.category == category
    assert failure.retryable is retryable


def test_local_asr_timeout_is_configurable(tmp_path, monkeypatch):
    class FakeModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, _path, **_kwargs):
            time.sleep(0.2)
            return [], object()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setitem(sys.modules, "opencc", types.SimpleNamespace(OpenCC=lambda _config: None))
    asr._LOCAL_MODELS.clear()
    media = tmp_path / "slow.wav"
    media.write_bytes(b"RIFF")
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        asr_provider="local",
        local_asr_timeout=0.05,
    )
    with pytest.raises(TranscriptionError) as caught:
        transcribe_file(media, "audio/wav", settings)
    assert caught.value.code == "ASR_LOCAL_TIMEOUT"
    assert caught.value.retryable is True


def test_local_asr_timeout_prevents_overlapping_retry(tmp_path, monkeypatch):
    release = threading.Event()

    class FakeModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, _path, **_kwargs):
            release.wait(2)
            return [], object()

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setitem(sys.modules, "opencc", types.SimpleNamespace(OpenCC=lambda _config: None))
    asr._LOCAL_MODELS.clear()
    media = tmp_path / "busy.wav"
    media.write_bytes(b"RIFF")
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        asr_provider="local",
        local_asr_timeout=0.05,
    )
    try:
        with pytest.raises(TranscriptionError) as timed_out:
            transcribe_file(media, "audio/wav", settings)
        assert timed_out.value.code == "ASR_LOCAL_TIMEOUT"
        with pytest.raises(TranscriptionError) as busy:
            transcribe_file(media, "audio/wav", settings)
        assert busy.value.code == "ASR_LOCAL_BUSY"
        assert busy.value.category == "transient"
    finally:
        release.set()


def test_invalid_provider_url_is_configuration_error(tmp_path):
    media = tmp_path / "speech.wav"
    media.write_bytes(b"RIFF")
    settings = Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}",
        asr_provider="openai",
        openai_api_key="test-key",
        openai_base_url="not-a-url",
    )
    with pytest.raises(TranscriptionError) as caught:
        transcribe_file(media, "audio/wav", settings)
    assert caught.value.code == "ASR_PROVIDER_CONFIGURATION_ERROR"
    assert caught.value.category == "configuration"
