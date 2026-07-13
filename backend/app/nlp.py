from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Sequence


STOPWORDS = {
    "的", "了", "和", "是", "在", "也", "有", "与", "及", "或", "就", "都", "而", "让", "把",
    "被", "到", "从", "这", "那", "一个", "我们", "你们", "他们", "可以", "能够", "通过", "进行",
    "如果", "因为", "所以", "以及", "对于", "正在", "更加", "没有", "不是", "什么", "如何", "这个",
    "一种", "时候", "非常", "但是", "并且", "to", "the", "and", "of", "a", "an", "is", "are",
}

DOMAIN_TERMS = {
    "科技": ["人工智能", "AI", "算法", "科技", "数字化", "芯片", "智能", "创新", "模型", "机器人"],
    "办公": ["办公", "效率", "工作", "计划", "时间管理", "会议", "生产力", "电脑"],
    "城市交通": ["城市", "交通", "地铁", "汽车", "道路", "出行", "通勤"],
    "自然环境": ["自然", "环境", "森林", "生态", "环保", "绿色", "可持续", "气候"],
    "健康运动": ["健康", "运动", "跑步", "健身", "身体", "训练", "活力"],
    "美食生活": ["咖啡", "美食", "早餐", "饮品", "餐饮", "生活", "休息"],
    "教育阅读": ["教育", "阅读", "学习", "知识", "书籍", "课堂", "学生", "成长"],
    "金融商业": ["金融", "商业", "投资", "经济", "收益", "增长", "趋势", "市场"],
    "旅行探索": ["旅行", "探索", "风景", "远方", "地图", "假期", "目的地", "冒险"],
    "团队协作": ["团队", "协作", "沟通", "伙伴", "合作", "组织", "共同目标"],
    "数据安全": ["数据", "安全", "隐私", "网络安全", "保护", "密码", "云计算", "风险"],
    "创意设计": ["创意", "设计", "艺术", "灵感", "想法", "视觉", "创造力", "内容"],
}

_ALL_DOMAIN_TERMS = sorted(
    {term for terms in DOMAIN_TERMS.values() for term in terms}, key=len, reverse=True
)
_TIMECODE = re.compile(
    r"^\s*(?:\d+\s*)?(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{1,3}\s*--?>\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{1,3}\s*$",
    re.MULTILINE,
)


def clean_transcript(text: str) -> str:
    """Remove SRT/VTT scaffolding while preserving spoken text."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^WEBVTT.*$", "", text, flags=re.MULTILINE | re.IGNORECASE)
    text = _TIMECODE.sub("", text)
    text = re.sub(r"(?m)^\s*\d+\s*$", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _split_long(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]
    clauses = [part.strip() for part in re.split(r"(?<=[，、,:：])", sentence) if part.strip()]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if current and len(current) + len(clause) > max_chars:
            chunks.append(current.strip())
            current = clause
        elif len(clause) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(clause[i : i + max_chars] for i in range(0, len(clause), max_chars))
        else:
            current += clause
    if current:
        chunks.append(current.strip())
    return chunks


def segment_text(text: str, min_chars: int = 14, max_chars: int = 86) -> list[str]:
    """Deterministic Chinese-first semantic sentence grouping.

    Sentence boundaries are respected first, very short utterances are merged,
    and overly long utterances are split on clause punctuation.
    """
    cleaned = clean_transcript(text)
    if not cleaned:
        return []
    raw = [part.strip() for part in re.split(r"(?<=[。！？!?；;\.])|\n+", cleaned) if part.strip()]
    expanded: list[str] = []
    for sentence in raw:
        expanded.extend(_split_long(sentence, max_chars))

    merged: list[str] = []
    pending = ""
    for sentence in expanded:
        if not pending:
            pending = sentence
        elif len(pending) < min_chars and len(pending) + len(sentence) <= max_chars:
            pending += sentence
        else:
            merged.append(pending.strip())
            pending = sentence
    if pending:
        if merged and len(pending) < min_chars and len(merged[-1]) + len(pending) <= max_chars:
            merged[-1] += pending
        else:
            merged.append(pending.strip())
    return [item for item in merged if item]


def _fallback_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    lowered = text.lower()
    for term in _ALL_DOMAIN_TERMS:
        if term.lower() in lowered:
            tokens.append(term)
    tokens.extend(re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{1,24}", text))
    for block in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        if len(block) <= 5:
            tokens.append(block)
        else:
            tokens.extend(block[i : i + 2] for i in range(0, len(block) - 1, 2))
    return tokens


def extract_keywords(text: str, top_k: int = 5) -> list[str]:
    """Extract stable keywords; jieba TF-IDF is enhanced with domain phrases."""
    candidates: list[str] = []
    lowered = text.lower()
    for term in _ALL_DOMAIN_TERMS:
        if term.lower() in lowered:
            candidates.append(term)
    try:
        import jieba.analyse  # type: ignore

        candidates.extend(jieba.analyse.extract_tags(text, topK=max(top_k * 2, 8)))
    except Exception:
        candidates.extend(_fallback_tokens(text))
    candidates.extend(_fallback_tokens(text))

    result: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        token = raw.strip(" ，。！？、；;:：\t\n").lower()
        if len(token) < 2 or token in STOPWORDS or token in seen:
            continue
        seen.add(token)
        result.append(raw.strip())
        if len(result) >= top_k:
            break
    return result


def infer_topic(text: str, keywords: Sequence[str] | None = None) -> str:
    haystack = (text + " " + " ".join(keywords or [])).lower()
    scored = []
    for topic, terms in DOMAIN_TERMS.items():
        score = sum(2 if len(term) >= 4 else 1 for term in terms if term.lower() in haystack)
        scored.append((score, topic))
    score, topic = max(scored)
    return topic if score else "综合主题"


def normalize_terms(values: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        token = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value).lower())
        if token:
            result.add(token)
    return result


def _normalized_chars(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())


def _ngrams(text: str, low: int = 2, high: int = 4) -> Counter[str]:
    normalized = _normalized_chars(text)
    grams: Counter[str] = Counter()
    for n in range(low, high + 1):
        grams.update(normalized[i : i + n] for i in range(max(0, len(normalized) - n + 1)))
    if not grams and normalized:
        grams[normalized] = 1
    return grams


def char_ngram_tfidf_cosines(query: str, documents: Sequence[str]) -> list[float]:
    """Compute cosine similarity with character 2-4 gram TF-IDF vectors."""
    counters = [_ngrams(query), *(_ngrams(doc) for doc in documents)]
    document_count = len(counters)
    document_frequency: Counter[str] = Counter()
    for counter in counters:
        document_frequency.update(counter.keys())
    idf = {
        term: math.log((1 + document_count) / (1 + frequency)) + 1
        for term, frequency in document_frequency.items()
    }

    vectors: list[dict[str, float]] = []
    norms: list[float] = []
    for counter in counters:
        total = sum(counter.values()) or 1
        vector = {term: (count / total) * idf[term] for term, count in counter.items()}
        vectors.append(vector)
        norms.append(math.sqrt(sum(value * value for value in vector.values())))
    query_vector, query_norm = vectors[0], norms[0]
    scores: list[float] = []
    for vector, norm in zip(vectors[1:], norms[1:]):
        if not query_norm or not norm:
            scores.append(0.0)
            continue
        common = query_vector.keys() & vector.keys()
        dot = sum(query_vector[term] * vector[term] for term in common)
        scores.append(max(0.0, min(1.0, dot / (query_norm * norm))))
    return scores


@dataclass(frozen=True, slots=True)
class RankedAsset:
    asset_id: str
    rank: int
    total_score: float
    tfidf_score: float
    keyword_score: float
    tag_score: float
    matched_terms: list[str]
    explanation: str
    is_diversity_filler: bool


@dataclass(frozen=True, slots=True)
class RankingTrace:
    provider: str
    model: str
    source: str
    degraded: bool = False
    error_message: str | None = None


def rank_assets_with_trace(
    text: str,
    topic: str,
    keywords: Sequence[str],
    assets: Sequence[dict],
    minimum: int = 3,
    semantic_scorer: "object | None" = None,
) -> tuple[list[RankedAsset], RankingTrace]:
    if not assets:
        return [], RankingTrace("rules", "char-ngram-tfidf", "char-ngram")
    asset_docs = [
        " ".join([str(asset.get("name", "")), *asset.get("tags", []), *asset.get("keywords", [])])
        for asset in assets
    ]
    query_text = " ".join([text, topic, *keywords])
    # The 0.55 weight feeds the "tfidf_score" column/display slot. With an
    # embedding scorer it carries true semantic cosine; otherwise it falls back
    # to the deterministic character n-gram TF-IDF cosine.
    if semantic_scorer is not None:
        try:
            tfidf_scores = semantic_scorer.cosine_scores(query_text, asset_docs)
            if len(tfidf_scores) != len(assets):
                raise ValueError(
                    f"embedding vector count mismatch: expected {len(assets)}, got {len(tfidf_scores)}"
                )
            if any(not math.isfinite(float(value)) for value in tfidf_scores):
                raise ValueError("embedding scorer returned a non-finite similarity")
            similarity_label = "向量语义相似"
            trace = RankingTrace(
                provider=str(getattr(semantic_scorer, "provider", "embedding")),
                model=str(
                    getattr(
                        semantic_scorer,
                        "model",
                        getattr(semantic_scorer, "name", "embedding"),
                    )
                ),
                source="embedding",
            )
        except Exception as exc:
            tfidf_scores = char_ngram_tfidf_cosines(query_text, asset_docs)
            similarity_label = "字符语义相似"
            trace = RankingTrace(
                provider="rules",
                model="char-ngram-tfidf",
                source="char-ngram",
                degraded=True,
                error_message=f"{type(exc).__name__}: {exc}"[:500],
            )
    else:
        tfidf_scores = char_ngram_tfidf_cosines(query_text, asset_docs)
        similarity_label = "字符语义相似"
        trace = RankingTrace("rules", "char-ngram-tfidf", "char-ngram")
    query_keywords = normalize_terms(keywords or extract_keywords(text))
    query_topic_terms = normalize_terms([topic, *extract_keywords(topic, top_k=3)])
    scored: list[dict] = []
    for asset, tfidf in zip(assets, tfidf_scores):
        asset_keywords = normalize_terms(asset.get("keywords", []))
        asset_tags = normalize_terms(asset.get("tags", []))
        keyword_hits = query_keywords & (asset_keywords | asset_tags)
        tag_hits = query_topic_terms & (asset_tags | asset_keywords)
        # Also count containment so phrases like "数据安全" match "安全" honestly.
        for query_term in query_keywords:
            for target in asset_keywords | asset_tags:
                if len(query_term) >= 2 and (query_term in target or target in query_term):
                    keyword_hits.add(query_term)
        for query_term in query_topic_terms:
            for target in asset_tags | asset_keywords:
                if len(query_term) >= 2 and (query_term in target or target in query_term):
                    tag_hits.add(query_term)
        keyword_score = min(1.0, len(keyword_hits) / max(1, len(query_keywords)))
        tag_score = min(1.0, len(tag_hits) / max(1, len(query_topic_terms)))
        total = 0.55 * tfidf + 0.30 * keyword_score + 0.15 * tag_score
        matched = sorted(keyword_hits | tag_hits, key=lambda item: (-len(item), item))[:6]
        scored.append(
            {
                "asset_id": str(asset["id"]),
                "total": total,
                "tfidf": tfidf,
                "keyword": keyword_score,
                "tag": tag_score,
                "matched": matched,
            }
        )
    scored.sort(key=lambda item: (-item["total"], item["asset_id"]))
    limit = max(minimum, min(6, len(scored)))
    results: list[RankedAsset] = []
    for index, item in enumerate(scored[:limit], start=1):
        filler = item["total"] < 0.035 and not item["matched"]
        if filler:
            explanation = "相关性信号较弱，作为跨主题多样性兜底候选，建议人工确认。"
        else:
            parts = []
            if item["matched"]:
                parts.append(f"命中关键词「{'、'.join(item['matched'])}」")
            if item["tfidf"] >= 0.02:
                parts.append(f"字幕与素材描述存在{similarity_label}")
            if item["tag"] > 0:
                parts.append("主题与素材标签一致")
            explanation = "；".join(parts) + "。"
            if not parts:
                explanation = "综合语义与素材覆盖度进入候选。"
        results.append(
            RankedAsset(
                asset_id=item["asset_id"],
                rank=index,
                total_score=round(item["total"], 6),
                tfidf_score=round(item["tfidf"], 6),
                keyword_score=round(item["keyword"], 6),
                tag_score=round(item["tag"], 6),
                matched_terms=item["matched"],
                explanation=explanation,
                is_diversity_filler=filler,
            )
        )
    return results, trace


def rank_assets(
    text: str,
    topic: str,
    keywords: Sequence[str],
    assets: Sequence[dict],
    minimum: int = 3,
    semantic_scorer: "object | None" = None,
) -> list[RankedAsset]:
    results, _trace = rank_assets_with_trace(
        text,
        topic,
        keywords,
        assets,
        minimum=minimum,
        semantic_scorer=semantic_scorer,
    )
    return results
