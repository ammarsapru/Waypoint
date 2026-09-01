from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3
    )


def _score_answer(case: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    answer = str(report.get("answer", ""))
    lowered = answer.lower()
    groups = case["required_concepts"]
    matched = [group for group in groups if any(term.lower() in lowered for term in group)]
    citations = report.get("citations", [])
    cited_paths = {item["span"]["path"] for item in citations}
    raw_groups = case.get("required_evidence_groups")
    if raw_groups:
        evidence_groups = [set(group) for group in raw_groups]
    else:
        evidence_groups = [{path} for path in case.get("expected_paths", [])]
    acceptable_paths = set(case.get("acceptable_paths", []))
    for group in evidence_groups:
        acceptable_paths.update(group)
    concept_recall = len(matched) / max(1, len(groups))
    evidence_group_recall = sum(
        bool(group & cited_paths) for group in evidence_groups
    ) / max(1, len(evidence_groups))
    path_precision = (
        len(cited_paths & acceptable_paths) / len(cited_paths) if cited_paths else 0.0
    )
    minimum_concept_recall = float(case.get("min_concept_recall", 0.6))
    minimum_group_recall = float(case.get("min_evidence_group_recall", 0.6))
    minimum_path_precision = float(case.get("min_citation_precision", 0.5))
    expected_citation_groups = case.get("expected_citation_groups", [])

    def citation_matches(expected: dict[str, Any], citation: dict[str, Any]) -> bool:
        span = citation.get("span", {})
        if span.get("path") != expected.get("path"):
            return False
        return int(span.get("start_line", 0)) <= int(expected["end_line"]) and int(
            span.get("end_line", 0)
        ) >= int(expected["start_line"])

    citation_group_recall = (
        sum(
            any(
                citation_matches(expected, citation)
                for expected in group
                for citation in citations
            )
            for group in expected_citation_groups
        )
        / max(1, len(expected_citation_groups))
        if expected_citation_groups
        else evidence_group_recall
    )
    constraints = {
        "min_answer_characters": 200,
        "max_answer_characters": 8_000,
        "min_citations": 2,
        "max_citations": 8,
        "max_citation_span_lines": 160,
        "max_output_tokens": 6_000,
        **case.get("output_constraints", {}),
    }
    observed_output_tokens = report.get("output_tokens")
    output_checks = {
        "minimum_answer_characters": len(answer) >= constraints["min_answer_characters"],
        "maximum_answer_characters": len(answer) <= constraints["max_answer_characters"],
        "minimum_citations": len(citations) >= constraints["min_citations"],
        "maximum_citations": len(citations) <= constraints["max_citations"],
        "citation_span_limit": all(
            int(item["span"]["end_line"]) - int(item["span"]["start_line"]) + 1
            <= constraints["max_citation_span_lines"]
            for item in citations
        ),
        "output_token_limit": (
            observed_output_tokens is None
            or int(observed_output_tokens) <= constraints["max_output_tokens"]
        ),
    }
    output_constraints_passed = all(output_checks.values())
    minimum_citation_group_recall = float(
        case.get("min_expected_citation_group_recall", 0.67)
    )
    passed = (
        bool(answer.strip())
        and bool(cited_paths)
        and concept_recall >= minimum_concept_recall
        and evidence_group_recall >= minimum_group_recall
        and path_precision >= minimum_path_precision
        and citation_group_recall >= minimum_citation_group_recall
        and output_constraints_passed
        and report.get("validation_status") == "passed"
    )
    return {
        "concept_recall": round(concept_recall, 4),
        "expected_path_recall": round(evidence_group_recall, 4),
        "evidence_group_recall": round(evidence_group_recall, 4),
        "expected_path_precision": round(path_precision, 4),
        "citation_precision": round(path_precision, 4),
        "expected_citation_group_recall": round(citation_group_recall, 4),
        "unsupported_cited_paths": sorted(cited_paths - acceptable_paths),
        "matched_concepts": matched,
        "missing_concepts": [group for group in groups if group not in matched],
        "cited_paths": sorted(cited_paths),
        "thresholds": {
            "concept_recall": minimum_concept_recall,
            "evidence_group_recall": minimum_group_recall,
            "citation_precision": minimum_path_precision,
            "expected_citation_group_recall": minimum_citation_group_recall,
        },
        "output_constraints": constraints,
        "output_checks": output_checks,
        "output_constraints_passed": output_constraints_passed,
        "passed": passed,
    }


def run(
    base_url: str,
    analysis_id: str,
    cases_path: Path,
    evidence_limit: int,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        defaults = payload.get("default_output_constraints", {})
        cases = [
            {"output_constraints": defaults, **case}
            for case in payload.get("cases", [])
        ]
    else:
        cases = payload
    if case_ids:
        cases = [case for case in cases if case["id"] in case_ids]
    tasks: list[dict[str, Any]] = []
    provider_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    provider_failures: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with httpx.Client(base_url=base_url, timeout=300) as client:
        for case in cases:
            started = time.perf_counter()
            response: httpx.Response | None = None
            try:
                response = client.post(
                    f"/api/v1/analyses/{analysis_id}/answer/compare",
                    json={"question": case["question"], "evidence_limit": evidence_limit},
                )
                response.raise_for_status()
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                error_body = ""
                if response is not None:
                    error_body = response.text[:2000]
                tasks.append({
                    "id": case["id"],
                    "question": case["question"],
                    "expected_paths": case.get("expected_paths", []),
                    "required_evidence_groups": case.get("required_evidence_groups", []),
                    "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
                    "answers": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "response_body": error_body,
                })
                continue
            comparison = response.json()
            answers = []
            for answer in comparison["answers"]:
                score = _score_answer(case, answer)
                row = {**answer, "score": score}
                answers.append(row)
                provider_rows[f"{answer['provider']}:{answer['model']}"].append(row)
            failures = comparison.get("failures", [])
            for failure in failures:
                provider_failures[
                    f"{failure['provider']}:{failure['model']}"
                ].append(failure)
            tasks.append({
                "id": case["id"],
                "question": case["question"],
                "expected_paths": case.get("expected_paths", []),
                "candidate_evidence_files": comparison["evidence_files"],
                "candidate_evidence_recall": round(
                    sum(
                        bool(set(group) & set(comparison["evidence_files"]))
                        for group in case.get(
                            "required_evidence_groups",
                            [[path] for path in case.get("expected_paths", [])],
                        )
                    )
                    / max(
                        1,
                        len(case.get(
                            "required_evidence_groups",
                            [[path] for path in case.get("expected_paths", [])],
                        )),
                    ),
                    4,
                ),
                "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
                "answers": answers,
                "failures": failures,
            })
    summaries = {}
    for endpoint in sorted(set(provider_rows) | set(provider_failures)):
        rows = provider_rows[endpoint]
        failures = provider_failures[endpoint]
        durations = [float(row["duration_ms"]) for row in rows]
        ttfts = [float(row["ttft_ms"]) for row in rows if row.get("ttft_ms") is not None]
        observable_events = [
            float(row["first_observable_event_ms"])
            for row in rows
            if row.get("first_observable_event_ms") is not None
        ]
        summaries[endpoint] = {
            "tasks": len(rows) + len(failures),
            "successful_responses": len(rows),
            "provider_or_validation_failures": len(failures),
            "task_success_rate": round(
                sum(bool(row["score"]["passed"]) for row in rows)
                / max(1, len(rows) + len(failures)),
                4,
            ),
            "mean_concept_recall": round(
                statistics.mean(row["score"]["concept_recall"] for row in rows)
                if rows else 0.0,
                4,
            ),
            "mean_expected_path_recall": round(
                statistics.mean(row["score"]["expected_path_recall"] for row in rows)
                if rows else 0.0,
                4,
            ),
            "mean_citation_precision": round(
                statistics.mean(row["score"]["citation_precision"] for row in rows)
                if rows else 0.0,
                4,
            ),
            "latency_p50_ms": _percentile(durations, 0.5),
            "latency_p95_ms": _percentile(durations, 0.95),
            "ttft_p50_ms": _percentile(ttfts, 0.5),
            "ttft_measured_tasks": len(ttfts),
            "first_observable_event_p50_ms": _percentile(observable_events, 0.5),
            "first_observable_event_p95_ms": _percentile(observable_events, 0.95),
            "first_observable_event_samples": len(observable_events),
            "input_tokens": sum(row.get("input_tokens") or 0 for row in rows),
            "cache_creation_input_tokens": sum(
                row.get("cache_creation_input_tokens") or 0 for row in rows
            ),
            "cache_read_input_tokens": sum(
                row.get("cache_read_input_tokens") or 0 for row in rows
            ),
            "effective_input_tokens": sum(
                row.get("effective_input_tokens") or 0 for row in rows
            ),
            "output_tokens": sum(row.get("output_tokens") or 0 for row in rows),
            "tool_calls": sum(row.get("tool_calls") or 0 for row in rows),
            "citations": sum(len(row.get("citations") or []) for row in rows),
        }
    return {
        "analysis_id": analysis_id,
        "case_count": len(cases),
        "scoring": {
            "task_pass": "non-empty schema-valid answer with citations meeting each case's minimum concept recall, required-evidence-group recall, and citation precision",
            "warning": "Deterministic concept/evidence scoring detects omissions and unsupported citations but does not replace calibrated human factual review.",
        },
        "failed_tasks": sum(bool(task.get("error")) for task in tasks),
        "provider_summaries": summaries,
        "tasks": tasks,
    }


def _markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Claude Code versus OpenRouter quality benchmark",
        "",
        f"Analysis: `{result['analysis_id']}` · Tasks: {result['case_count']}",
        "",
        "| Endpoint | Success | Concept recall | Evidence-group recall | Citation precision | p50 latency | p95 latency | TTFT p50 | TTFT samples | Uncached input | Cache create | Cache read | Effective input | Output tokens | Tool calls | Citations |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for endpoint, item in result["provider_summaries"].items():
        lines.append(
            f"| {endpoint} | {item['task_success_rate']:.1%} | "
            f"{item['mean_concept_recall']:.1%} | {item['mean_expected_path_recall']:.1%} | "
            f"{item['mean_citation_precision']:.1%} | "
            f"{item['latency_p50_ms']} ms | {item['latency_p95_ms']} ms | "
            f"{item['ttft_p50_ms'] or 'N/A'} | {item['ttft_measured_tasks']} | "
            f"{item['input_tokens']} | {item['cache_creation_input_tokens']} | "
            f"{item['cache_read_input_tokens']} | {item['effective_input_tokens']} | "
            f"{item['output_tokens']} | {item['tool_calls']} | {item['citations']} |"
        )
    lines += [
        "",
        "OpenRouter TTFT is measured from request dispatch to the first meaningful streaming content or tool-call delta. Claude Agent SDK does not expose raw token deltas, so its TTFT remains N/A and the first assistant-event timing is retained only in the status field.",
        "",
        f"Scoring rule: {result['scoring']['task_pass']}.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Claude Code and OpenRouter on gold repository questions")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--analysis-id", required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("verification/model-quality-cases.json"),
    )
    parser.add_argument("--evidence-limit", type=int, default=10)
    parser.add_argument("--label", default="claude-code-openrouter")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--confirm-live-model-usage", action="store_true")
    arguments = parser.parse_args()
    if not arguments.confirm_live_model_usage:
        raise SystemExit("Refusing to spend model quota without --confirm-live-model-usage")
    result = run(
        arguments.base_url,
        arguments.analysis_id,
        arguments.cases,
        max(2, min(15, arguments.evidence_limit)),
        set(arguments.case_id) or None,
    )
    output = Path("verification/results")
    output.mkdir(parents=True, exist_ok=True)
    stem = f"model-quality-{arguments.label}"
    (output / f"{stem}.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    markdown = _markdown(result)
    (output / f"{stem}.md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
