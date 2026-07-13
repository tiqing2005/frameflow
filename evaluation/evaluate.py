"""Small, reproducible retrieval evaluation for the interview evidence pack."""

from __future__ import annotations

import json
import math
import sys
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.nlp import (  # noqa: E402
    char_ngram_tfidf_cosines,
    extract_keywords,
    infer_topic,
    normalize_terms,
    rank_assets,
)
def asset_rows() -> list[dict]:
    # Read the literal seed catalogue without importing app.seed, so this
    # lightweight evaluation can run even before SQLAlchemy is installed.
    seed_file = ROOT / "backend" / "app" / "seed.py"
    tree = ast.parse(seed_file.read_text(encoding="utf-8"))
    seed_assets = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SEED_ASSETS"
            for target in node.targets
        ):
            seed_assets = ast.literal_eval(node.value)
            break
    if seed_assets is None:
        raise RuntimeError("无法读取 SEED_ASSETS")
    return [
        {"id": slug, "name": name, "tags": tags, "keywords": keywords}
        for slug, name, tags, keywords, _primary, _accent in seed_assets
    ]


def keyword_ranking(text: str, assets: list[dict]) -> list[str]:
    terms = normalize_terms(extract_keywords(text, top_k=8))
    scored: list[tuple[float, str]] = []
    for asset in assets:
        targets = normalize_terms([asset["name"], *asset["tags"], *asset["keywords"]])
        hits = set()
        for query in terms:
            for target in targets:
                if query == target or (len(query) >= 2 and (query in target or target in query)):
                    hits.add(query)
        scored.append((len(hits) / max(1, len(terms)), asset["id"]))
    return [item[1] for item in sorted(scored, key=lambda item: (-item[0], item[1]))]


def tfidf_ranking(text: str, assets: list[dict]) -> list[str]:
    documents = [" ".join([a["name"], *a["tags"], *a["keywords"]]) for a in assets]
    scores = char_ngram_tfidf_cosines(text, documents)
    pairs = zip(scores, [asset["id"] for asset in assets])
    return [item[1] for item in sorted(pairs, key=lambda item: (-item[0], item[1]))]


def hybrid_ranking(text: str, assets: list[dict]) -> list[str]:
    keywords = extract_keywords(text, top_k=6)
    topic = infer_topic(text, keywords)
    return [item.asset_id for item in rank_assets(text, topic, keywords, assets, minimum=3)]


def metrics(rankings: list[tuple[str, list[str]]], k: int = 3) -> dict[str, float]:
    hit = reciprocal = ndcg = 0.0
    for expected, ranking in rankings:
        if expected not in ranking:
            continue
        rank = ranking.index(expected) + 1
        reciprocal += 1.0 / rank
        if rank <= k:
            hit += 1.0
            ndcg += 1.0 / math.log2(rank + 1)
    count = max(1, len(rankings))
    return {
        f"Hit@{k}": hit / count,
        "MRR": reciprocal / count,
        f"nDCG@{k}": ndcg / count,
    }


def main() -> None:
    cases = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))
    assets = asset_rows()
    strategies = {
        "关键词基线": keyword_ranking,
        "字符 TF-IDF": tfidf_ranking,
        "混合排序": hybrid_ranking,
    }
    report: dict[str, dict[str, float]] = {}
    failures: dict[str, list[str]] = {}
    for name, strategy in strategies.items():
        results = [(case["expected"], strategy(case["text"], assets)) for case in cases]
        report[name] = metrics(results)
        failures[name] = [
            case["id"]
            for case, (_expected, ranking) in zip(cases, results)
            if case["expected"] not in ranking[:3]
        ]

    lines = [
        "# 检索策略离线评测",
        "",
        f"固定评测集：{len(cases)} 条未作为应用演示样例写入的中文字幕；相关项为对应主题素材。",
        "",
        "| 策略 | Hit@3 | MRR | nDCG@3 | Top 3 未命中 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for name, values in report.items():
        missing = "、".join(failures[name]) or "无"
        lines.append(
            f"| {name} | {values['Hit@3']:.3f} | {values['MRR']:.3f} | "
            f"{values['nDCG@3']:.3f} | {missing} |"
        )
    lines += [
        "",
        "> 这是一组小规模、可复现的工程决策证据，不代表线上泛化指标。混合排序仍保留三项分数与命中词，便于人工判断。",
        "",
        "运行：`python evaluation/evaluate.py`。脚本会覆盖本文件。",
        "",
    ]
    output = Path(__file__).parent / "RESULTS.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
