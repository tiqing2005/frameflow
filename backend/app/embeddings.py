"""Semantic similarity providers for the hybrid ranker.

The ranker's first weight (0.55) is fed by a :class:`SemanticScorer`. The
default scorer is :class:`CharNgramScorer` (character n-gram TF-IDF cosine),
which needs no external dependency. When an embedding provider is configured
and its dependency is available, :func:`get_semantic_scorer` returns a richer
scorer (local sentence-transformers BGE or a remote OpenAI-compatible
``/embeddings`` endpoint) that captures synonym/metaphor similarity the
character scorer cannot.

Any failure (missing optional dependency, model download error, network
timeout, missing key) returns ``None`` so the caller falls back to
:class:`CharNgramScorer`. This keeps the demo runnable with zero keys and
zero network, while making true semantic matching available when configured.

No API key is ever logged or serialized into responses.
"""
from __future__ import annotations

import logging
import math
from typing import Protocol, Sequence

import httpx

from .config import Settings
from .nlp import char_ngram_tfidf_cosines

logger = logging.getLogger("frameflow.embeddings")


class SemanticScorer(Protocol):
    """Produces cosine similarity scores in [0, 1] for one query vs N docs."""

    name: str

    def cosine_scores(self, query: str, documents: Sequence[str]) -> list[float]: ...


class CharNgramScorer:
    """Wraps the existing deterministic character n-gram TF-IDF cosine."""

    name = "char-ngram-tfidf"

    def cosine_scores(self, query: str, documents: Sequence[str]) -> list[float]:
        return char_ngram_tfidf_cosines(query, documents)


class LocalBgeScorer:
    """Local sentence-transformers embedding scorer (lazy-loaded singleton).

    The heavy model is loaded once and cached at module level. If torch /
    sentence-transformers is not installed, construction raises and the caller
    treats it as "embedding unavailable" -> char-ngram fallback.
    """

    name = "bge-small-zh"
    provider = "sentence-transformers"

    def __init__(self, model_name: str, device: str, cache_folder: str | None):
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._model = SentenceTransformer(model_name, device=device, cache_folder=cache_folder)
        self.model = model_name

    def cosine_scores(self, query: str, documents: Sequence[str]) -> list[float]:
        import numpy as np  # type: ignore

        texts = [query, *documents]
        vectors = self._model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        query_vec = vectors[0]
        doc_vecs = vectors[1:]
        scores: list[float] = []
        for doc_vec in doc_vecs:
            similarity = float(np.dot(query_vec, doc_vec))
            scores.append(max(0.0, min(1.0, similarity)))
        return scores


class RemoteEmbeddingScorer:
    """OpenAI-compatible /embeddings endpoint scorer."""

    name = "remote-embedding"
    provider = "openai-compatible"

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def cosine_scores(self, query: str, documents: Sequence[str]) -> list[float]:
        payload = {"model": self.model, "input": [query, *documents]}
        endpoint = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(documents) + 1:
            raise ValueError("embedding response vector count does not match inputs")
        if any(not isinstance(item, dict) for item in data):
            raise ValueError("embedding response contains an invalid item")
        try:
            indices = [int(item["index"]) for item in data]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("embedding response contains an invalid index") from exc
        expected_indices = list(range(len(documents) + 1))
        if sorted(indices) != expected_indices:
            raise ValueError("embedding response indices do not match inputs")
        ordered = [item for _, item in sorted(zip(indices, data), key=lambda pair: pair[0])]
        vectors = [item.get("embedding") for item in ordered]
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise ValueError("embedding response contains an invalid vector")
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise ValueError("embedding response contains inconsistent dimensions")

        def normalize(vector) -> list[float]:
            values = [float(value) for value in vector]
            if any(not math.isfinite(value) for value in values):
                raise ValueError("embedding response contains a non-finite value")
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            return [value / norm for value in values]

        normalized = [normalize(vector) for vector in vectors]
        query_vec = normalized[0]
        return [
            max(0.0, min(1.0, sum(a * b for a, b in zip(query_vec, document))))
            for document in normalized[1:]
        ]


# Module-level caches so we do not repeatedly probe an unavailable provider.
_LOCAL_SCORER: LocalBgeScorer | None = None
_LOCAL_DISABLED = False
_REMOTE_DISABLED: dict[str, bool] = {}


def get_semantic_scorer(settings: Settings) -> SemanticScorer | None:
    """Resolve the configured semantic scorer, or None to use char-ngram fallback.

    Resolution rules for ``EMBEDDING_PROVIDER``:
      - ``none``  -> always None (char-ngram).
      - ``local`` -> local BGE; if unavailable, None.
      - ``openai``/``openai-compatible`` -> remote endpoint; if no key/unavailable, None.
      - ``auto`` (default) -> local BGE if available, else remote if a key is
        configured, else None.
    """
    provider = settings.embedding_provider
    if provider in {"", "none", "off", "disabled"}:
        return None
    if provider == "local":
        return _resolve_local(settings)
    if provider in {"openai", "openai-compatible"}:
        return _resolve_remote(settings)
    # auto
    scorer = _resolve_local(settings) if not _LOCAL_DISABLED else None
    if scorer is not None:
        return scorer
    return _resolve_remote(settings)


def _resolve_local(settings: Settings) -> SemanticScorer | None:
    global _LOCAL_SCORER, _LOCAL_DISABLED
    if _LOCAL_DISABLED:
        return None
    if _LOCAL_SCORER is not None:
        return _LOCAL_SCORER
    try:
        cache_folder = str(settings.hf_home) if settings.hf_home else None
        _LOCAL_SCORER = LocalBgeScorer(
            model_name=settings.embedding_model,
            device=settings.embedding_device,
            cache_folder=cache_folder,
        )
        logger.info("embedding local provider ready: %s", settings.embedding_model)
        return _LOCAL_SCORER
    except Exception as exc:  # pragma: no cover - depends on optional dep + network
        _LOCAL_DISABLED = True
        logger.warning("本地 Embedding 不可用，回退字符 n-gram：%s", exc)
        return None


def _resolve_remote(settings: Settings) -> SemanticScorer | None:
    if not settings.embedding_api_key:
        return None
    base = settings.embedding_base_url
    if _REMOTE_DISABLED.get(base):
        return None
    # Return a fresh scorer; failures surface lazily at call time and are
    # handled by the caller's fallback. We do not pre-warm remote endpoints.
    return RemoteEmbeddingScorer(
        base_url=base,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        timeout=settings.embedding_timeout,
    )


def mark_remote_unavailable(base_url: str) -> None:
    """Record that a remote endpoint failed, to avoid retrying every match."""
    _REMOTE_DISABLED[base_url.rstrip("/")] = True
