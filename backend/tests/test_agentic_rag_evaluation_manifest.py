from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_agentic_rag_manifest_is_complete_and_well_formed() -> None:
    payload = json.loads(
        (ROOT / "verification" / "agentic-rag-evaluation-cases.json").read_text(
            encoding="utf-8"
        )
    )
    cases = payload["cases"]
    ids = [case["id"] for case in cases]
    counts = Counter(case["category"] for case in cases)

    assert len(cases) == 50
    assert len(ids) == len(set(ids))
    assert counts == Counter(payload["categories"])
    assert all(case["repository"] in payload["repositories"] for case in cases)
    assert all(case["prompt"].strip() for case in cases)
    assert all(len(case["assertions"]) >= 2 for case in cases)


def test_live_model_cases_define_citations_and_output_enforcement() -> None:
    payload = json.loads(
        (ROOT / "verification" / "model-quality-cases.json").read_text(
            encoding="utf-8"
        )
    )
    constraints = payload["default_output_constraints"]
    assert payload["version"] == "2.0"
    assert constraints == {
        "min_answer_characters": 200,
        "max_answer_characters": 8000,
        "min_citations": 2,
        "max_citations": 8,
        "max_citation_span_lines": 160,
        "max_output_tokens": 6000,
    }
    assert len(payload["cases"]) == 12
    assert sum(len(case["expected_citation_groups"]) for case in payload["cases"]) == 27
    for case in payload["cases"]:
        assert case["expected_citation_groups"]
        assert all(
            item["start_line"] <= item["end_line"]
            for group in case["expected_citation_groups"]
            for item in group
        )
