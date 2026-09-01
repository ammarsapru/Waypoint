from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any


STAGES = ("candidate_retrieval", "explicit_inspection", "cited_retrieval")


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def number(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}" if isinstance(value, float) else f"{value:,}"


def grouped(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[str(row.get(key, "unknown"))].append(row)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the V4 campaign as Markdown")
    parser.add_argument("result", type=Path)
    parser.add_argument("--hooks", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    hooks = (
        json.loads(args.hooks.read_text(encoding="utf-8"))
        if args.hooks and args.hooks.exists() else None
    )
    rows = result.get("runs", [])
    failures = result.get("failures", [])
    lines = [
        "# V4 independent-agent evaluation report",
        "",
        "## Run identity",
        "",
        f"- Protocol: `{result.get('protocol')}`",
        f"- Cases: {result.get('case_count', 0)}",
        f"- Expected model attempts: {result.get('expected_model_attempts', 0)}",
        f"- Successful attempts: {result.get('successful_model_runs', 0)}",
        f"- Failed attempts: {result.get('failed_model_runs', 0)}",
        f"- Release-gate eligible: {result.get('release_gate_eligible', False)}",
        "",
        "Each endpoint independently selected and called production Waypoint tools. "
        "No evidence bundle was frozen or shared between models.",
        "",
        "## Per-model retrieval and efficiency",
        "",
        "| Fixed endpoint | Successes | Candidate Hit@5 | Candidate recall | Candidate nDCG@10 | Cited recall | Concept recall | Mean tools | Mean rounds | Mean tokens | Mean seconds | p95 seconds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for endpoint, summary in result.get("summary", {}).items():
        candidate = summary["stages"]["candidate_retrieval"]
        cited = summary["stages"]["cited_retrieval"]
        lines.append(
            f"| `{endpoint}` | {summary['successful_cases']}/{summary['cases']} | "
            f"{percent(candidate['hit_at_5'])} | {percent(candidate['path_recall'])} | "
            f"{percent(candidate['ndcg_at_10'])} | {percent(cited['path_recall'])} | "
            f"{percent(summary.get('mean_expected_concept_recall'))} | "
            f"{number(summary['mean_tool_calls'])} | {number(summary['mean_rounds'])} | "
            f"{number(summary.get('mean_total_tokens'))} | "
            f"{number(summary['mean_duration_ms'] / 1000)} | "
            f"{number(summary['p95_duration_ms'] / 1000)} |"
        )

    lines += ["", "## Results by repository", "", "| Repository | Attempts | Candidate recall | Cited recall | Concept recall | Mean seconds |", "|---|---:|---:|---:|---:|---:|"]
    for repository, values in sorted(grouped(rows, "repository").items()):
        lines.append(
            f"| {repository} | {len(values)} | "
            f"{percent(mean(v['candidate_retrieval']['path_recall'] for v in values))} | "
            f"{percent(mean(v['cited_retrieval']['path_recall'] for v in values))} | "
            f"{percent(mean(v['answer_quality']['expected_concept_recall'] for v in values))} | "
            f"{number(mean(v['duration_ms'] for v in values) / 1000)} |"
        )

    lines += ["", "## Results by scenario category", "", "| Category | Attempts | Candidate Hit@5 | Group recall | Concept recall |", "|---|---:|---:|---:|---:|"]
    for category, values in sorted(grouped(rows, "category").items()):
        lines.append(
            f"| {category} | {len(values)} | "
            f"{percent(mean(v['candidate_retrieval']['hit_at_5'] for v in values))} | "
            f"{percent(mean(v['candidate_retrieval']['required_group_recall'] for v in values))} | "
            f"{percent(mean(v['answer_quality']['expected_concept_recall'] for v in values))} |"
        )

    lines += ["", "## Provider and validation failures", ""]
    if failures:
        lines += ["| Case | Endpoint | Trace | Type | Detail |", "|---|---|---|---|---|"]
        for failure in failures:
            detail = str(failure.get("detail", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {failure.get('case_id')} | `{failure.get('provider')}:{failure.get('model')}` | "
                f"`{failure.get('trace_id') or '-'}` | {failure.get('error_type')} | {detail[:500]} |"
            )
    else:
        lines.append("No model, tool, transport, or validation failures were recorded.")

    lines += ["", "## Hook and log quality", ""]
    if hooks:
        quality = hooks.get("quality", {})
        volume = hooks.get("volume", {})
        lines += [
            "| Measurement | Result |",
            "|---|---:|",
            f"| Hook events | {number(hooks.get('event_count', 0), 0)} |",
            f"| Correlated traces | {number(hooks.get('trace_count', 0), 0)} |",
            f"| Trace correlation | {percent(quality.get('trace_correlation_rate'))} |",
            f"| Pre-to-terminal pairing | {percent(quality.get('pre_terminal_pairing_rate'))} |",
            f"| Incomplete spans | {number(quality.get('incomplete_spans', 0), 0)} |",
            f"| Orphan terminal spans | {number(quality.get('orphan_terminal_spans', 0), 0)} |",
            f"| Possible secret leaks | {number(quality.get('possible_secret_leaks', 0), 0)} |",
            f"| Redaction markers | {number(quality.get('redaction_markers', 0), 0)} |",
            f"| Serialized hook bytes | {number(volume.get('serialized_bytes', 0), 0)} |",
            f"| Bytes per event | {number(volume.get('bytes_per_event'))} |",
            f"| Mean hook callback ms | {number(volume.get('hook_callback_mean_ms'), 6)} |",
        ]
    else:
        lines.append("Hook aggregate was not available.")

    lines += [
        "",
        "## Interpretation limits",
        "",
        "- Seed expected paths and concepts require independent human adjudication.",
        "- Concept recall is deterministic phrase coverage, not a substitute for human factual review.",
        "- Non-streaming provider calls report TTFT as unavailable rather than estimating it.",
        "- Logs capture observable calls, arguments, bounded results, corrections, and failures; they do not expose private chain-of-thought.",
        "",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
