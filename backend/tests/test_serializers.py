from __future__ import annotations

import json

from app.models import AIRun
from app.serializers import run_dict


def make_run(output_summary: dict | list | None) -> AIRun:
    return AIRun(
        id="run-1",
        operation="pipeline_segment_and_match",
        provider="rules",
        model="rule-nlp-v1",
        prompt_version="rules-v1",
        input_hash="hash",
        output_summary_json=json.dumps(output_summary),
    )


def test_run_dict_promotes_historical_token_usage_to_canonical_fields():
    run = make_run(
        {
            "segments": 3,
            "tokens": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
            },
        }
    )

    serialized = run_dict(run)

    assert serialized["input_tokens"] == 120
    assert serialized["output_tokens"] == 30
    assert serialized["total_tokens"] == 150
    assert serialized["output_summary"] == {"segments": 3}


def test_run_dict_maps_provider_aliases_and_derives_missing_total():
    run = make_run(
        {
            "tokens": {
                "prompt_tokens": "42",
                "completion_tokens": 8,
            }
        }
    )

    serialized = run_dict(run)

    assert serialized["input_tokens"] == 42
    assert serialized["output_tokens"] == 8
    assert serialized["total_tokens"] == 50
    assert "tokens" not in serialized["output_summary"]


def test_run_dict_uses_null_for_rule_runs_without_token_usage():
    run = make_run({"candidate_count": 4, "tokens": {}})

    serialized = run_dict(run)

    assert serialized["input_tokens"] is None
    assert serialized["output_tokens"] is None
    assert serialized["total_tokens"] is None
    assert serialized["output_summary"] == {"candidate_count": 4}


def test_run_dict_removes_internal_canonical_token_storage_fields():
    run = make_run(
        {
            "segments": 2,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }
    )

    serialized = run_dict(run)

    assert serialized["input_tokens"] == 10
    assert serialized["output_tokens"] == 5
    assert serialized["total_tokens"] == 15
    assert serialized["output_summary"] == {"segments": 2}
