"""Tests for the semantic scorer resolution and rank_assets embedding channel.

These never require torch/sentence-transformers to be installed: they exercise
the fallback contract (CharNgramScorer) and the injection plumbing with a fake
scorer, so CI stays hermetic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.embeddings import (
    CharNgramScorer,
    RemoteEmbeddingScorer,
    get_semantic_scorer,
    mark_remote_unavailable,
)
from app.nlp import char_ngram_tfidf_cosines, rank_assets, rank_assets_with_trace


ASSETS = [
    {"id": "a1", "name": "AI 芯片", "tags": ["科技"], "keywords": ["人工智能", "算力"]},
    {"id": "a2", "name": "城市夜景", "tags": ["城市"], "keywords": ["交通", "航拍"]},
]


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
    )
    base.update(overrides)
    return Settings(**base)


def test_char_ngram_scorer_matches_legacy_function():
    scorer = CharNgramScorer()
    docs = ["人工智能算力", "城市交通航拍"]
    assert scorer.cosine_scores("人工智能芯片", docs) == char_ngram_tfidf_cosines(
        "人工智能芯片", docs
    )


def test_get_semantic_scorer_none_when_disabled(tmp_path):
    settings = _settings(tmp_path, embedding_provider="none")
    assert get_semantic_scorer(settings) is None


def test_get_semantic_scorer_auto_without_deps_returns_char_or_none(tmp_path):
    # In the CI environment sentence-transformers is absent, so auto should not
    # crash; it returns None (caller uses CharNgramScorer) since no remote key.
    settings = _settings(tmp_path, embedding_provider="auto")
    # Either None (preferred) or a local scorer; both are acceptable fallbacks.
    scorer = get_semantic_scorer(settings)
    assert scorer is None or scorer.name in {"bge-small-zh", "char-ngram-tfidf"}


def test_get_semantic_scorer_remote_requires_key(tmp_path):
    settings = _settings(
        tmp_path,
        embedding_provider="openai",
        embedding_base_url="https://embed.example.com/v1",
        embedding_api_key=None,
    )
    assert get_semantic_scorer(settings) is None


def test_get_semantic_scorer_remote_returns_scorer_with_key(tmp_path):
    settings = _settings(
        tmp_path,
        embedding_provider="openai-compatible",
        embedding_base_url="https://embed.example.com/v1",
        embedding_api_key="sk-test-remote",
        embedding_model="text-embedding-3-small",
    )
    scorer = get_semantic_scorer(settings)
    assert isinstance(scorer, RemoteEmbeddingScorer)
    assert scorer.api_key == "sk-test-remote"


def test_remote_embedding_scorer_uses_validated_pure_python_cosine(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 2, "embedding": [1.0, 0.0]},
                ]
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.embeddings.httpx.Client", FakeClient)
    scorer = RemoteEmbeddingScorer(
        base_url="https://embed.example.com/v1",
        api_key="test-key",
        model="test-embedding",
        timeout=1,
    )
    assert scorer.cosine_scores("query", ["orthogonal", "same"]) == pytest.approx([0.0, 1.0])


def test_remote_embedding_scorer_rejects_duplicate_indices(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 0, "embedding": [0.0, 1.0]},
                ]
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.embeddings.httpx.Client", FakeClient)
    scorer = RemoteEmbeddingScorer(
        base_url="https://embed.example.com/v1",
        api_key="test-key",
        model="test-embedding",
        timeout=1,
    )
    with pytest.raises(ValueError, match="indices do not match inputs"):
        scorer.cosine_scores("query", ["document"])


def test_remote_embedding_scorer_rejects_non_finite_values(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 0, "embedding": [1.0, float("nan")]},
                    {"index": 1, "embedding": [0.0, 1.0]},
                ]
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.embeddings.httpx.Client", FakeClient)
    scorer = RemoteEmbeddingScorer(
        base_url="https://embed.example.com/v1",
        api_key="test-key",
        model="test-embedding",
        timeout=1,
    )
    with pytest.raises(ValueError, match="non-finite"):
        scorer.cosine_scores("query", ["document"])


class _FakeScorer:
    """Deterministic fake that reports a fixed similarity per asset id."""

    name = "fake-embedding"

    def __init__(self, scores_by_index):
        self._scores = scores_by_index

    def cosine_scores(self, query, documents):
        return [self._scores[i] for i in range(len(documents))]


def test_rank_assets_uses_injected_scorer():
    # Inject a scorer that overwhelmingly prefers asset a2 (city) over a1 (AI).
    # 0.55*1.0 = 0.55 for a2 beats a1's 0.55*0.0 + 0.30*1.0 + 0.15*1.0 = 0.45.
    fake = _FakeScorer([0.0, 1.0])
    ranked = rank_assets("人工智能", "科技", ["人工智能"], ASSETS, minimum=2, semantic_scorer=fake)
    ids = [item.asset_id for item in ranked]
    assert ids[0] == "a2"  # dominant embedding score wins despite zero keyword overlap


def test_rank_assets_falls_back_when_scorer_raises():
    class _Broken:
        name = "broken"

        def cosine_scores(self, query, documents):
            raise RuntimeError("provider down")

    ranked = rank_assets(
        "人工智能", "科技", ["人工智能"], ASSETS, minimum=2, semantic_scorer=_Broken()
    )
    # Must not raise; falls back to char-ngram and still returns candidates.
    assert len(ranked) == 2
    # The AI asset should win on char overlap with "人工智能".
    assert ranked[0].asset_id == "a1"


def test_rank_assets_falls_back_and_traces_vector_count_mismatch():
    class _Short:
        name = "short-vector-response"
        provider = "test"
        model = "test-embedding"

        def cosine_scores(self, query, documents):
            return [0.5]

    ranked, trace = rank_assets_with_trace(
        "人工智能", "科技", ["人工智能"], ASSETS, minimum=2, semantic_scorer=_Short()
    )
    assert len(ranked) == 2
    assert trace.degraded is True
    assert trace.source == "char-ngram"
    assert "count mismatch" in (trace.error_message or "")


def test_rank_assets_default_is_char_ngram():
    # No scorer injected -> char-ngram path, AI asset wins.
    ranked = rank_assets("人工智能", "科技", ["人工智能"], ASSETS, minimum=2)
    assert ranked[0].asset_id == "a1"
    assert "字符语义相似" in ranked[0].explanation or "向量语义相似" not in ranked[0].explanation


def test_remote_unavailable_caches_disabling(tmp_path):
    base = "https://embed.example.com/v1"
    mark_remote_unavailable(base)
    settings = _settings(
        tmp_path,
        embedding_provider="openai",
        embedding_base_url=base,
        embedding_api_key="sk-x",
    )
    assert get_semantic_scorer(settings) is None
