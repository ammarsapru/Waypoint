from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _frontend(playwright: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    suites = playwright.get("suites", [])
    tests: list[dict[str, Any]] = []

    def visit(items: list[dict[str, Any]]) -> None:
        for item in items:
            tests.extend(item.get("specs", []))
            visit(item.get("suites", []))

    visit(suites)
    executed = [spec for spec in tests if spec.get("tests", [{}])[0].get("status") != "skipped"]
    passed = sum(
        all(test.get("status") == "expected" for test in spec.get("tests", []))
        for spec in executed
    )
    e2e_success = passed / max(1, len(executed))
    line_percent = float(coverage.get("total", {}).get("lines", {}).get("pct", 0)) / 100
    return {
        "score": round((e2e_success + line_percent) / 2, 4),
        "e2e_success_rate": round(e2e_success, 4),
        "frontend_line_coverage": round(line_percent, 4),
        "executed_tests": len(executed),
        "skipped_tests": len(tests) - len(executed),
    }


def build(paths: dict[str, Path], threshold: float) -> dict[str, Any]:
    parsing = _load(paths["parsing"])
    retrieval = _load(paths["retrieval"])
    performance = _load(paths["performance"])
    model = _load(paths["model"])
    playwright = _load(paths["playwright"])
    frontend_coverage = _load(paths["frontend_coverage"])
    frontend_performance = _load(paths["frontend_performance"])
    categories: dict[str, Any] = {}
    missing = []

    if parsing:
        values = [
            parsing["parse_success_rate"],
            parsing["node_metrics"]["f1"],
            parsing["edge_metrics"]["f1"],
        ]
        categories["parsing_accuracy"] = {
            "score": round(statistics.mean(values), 4),
            "parse_success_rate": values[0],
            "node_f1": values[1],
            "edge_f1": values[2],
        }
    else:
        missing.append("parsing_accuracy")

    if retrieval:
        strict_score = retrieval.get("strict_quality_score")
        values = [
            retrieval["hit_rate_at_5"],
            retrieval["hit_rate_at_10"],
            retrieval["mean_reciprocal_rank"],
            retrieval["ndcg_at_10"],
        ]
        categories["retrieval_quality"] = {
            "score": round(strict_score if strict_score is not None else statistics.mean(values), 4),
            "hit_rate_at_5": values[0],
            "hit_rate_at_10": values[1],
            "mrr": values[2],
            "ndcg_at_10": values[3],
            "map_at_10": retrieval.get("mean_average_precision_at_10"),
            "required_group_recall_at_10": retrieval.get("required_group_recall_at_10"),
            "strict_case_pass_rate": retrieval.get("strict_case_pass_rate"),
            "forbidden_hit_rate_at_10": retrieval.get("forbidden_path_hit_rate_at_10"),
            "scoring_version": "strict-v2" if strict_score is not None else "legacy-v1",
        }
    else:
        missing.append("retrieval_quality")

    if model:
        summaries = list(model.get("provider_summaries", {}).values())
        values = [
            statistics.mean(item[metric] for item in summaries)
            for metric in (
                "task_success_rate",
                "mean_concept_recall",
                "mean_expected_path_recall",
            )
        ] if summaries else [0.0, 0.0, 0.0]
        categories["model_answer_quality"] = {
            "score": round(statistics.mean(values), 4),
            "mean_task_success": round(values[0], 4),
            "mean_concept_recall": round(values[1], 4),
            "mean_expected_path_recall": round(values[2], 4),
            "models": len(summaries),
            "minimum_model_task_success": round(
                min((item["task_success_rate"] for item in summaries), default=0.0), 4
            ),
        }
    else:
        missing.append("model_answer_quality")

    if performance:
        slo_score = 1.0 if performance["slo"]["passed"] else 0.0
        web_vitals_score = (
            1.0 if frontend_performance and frontend_performance["thresholds_passed"]
            else 0.0
        )
        categories["performance"] = {
            "score": round(
                (performance["service_success_rate"] + slo_score + web_vitals_score) / 3,
                4,
            ),
            "service_success_rate": performance["service_success_rate"],
            "slo_passed": performance["slo"]["passed"],
            "frontend_web_vitals_passed": bool(web_vitals_score),
            "peak_observed_rss_mib": performance["memory"]["peak_observed_rss_mib"],
        }
    else:
        missing.append("performance")

    if playwright and frontend_coverage:
        categories["frontend_quality"] = _frontend(playwright, frontend_coverage)
    else:
        missing.append("frontend_quality")

    for item in categories.values():
        item["threshold"] = threshold
        item["passed"] = item["score"] >= threshold
    if "model_answer_quality" in categories:
        categories["model_answer_quality"]["passed"] = (
            categories["model_answer_quality"]["passed"]
            and categories["model_answer_quality"]["minimum_model_task_success"]
            >= threshold
        )
    complete = not missing
    overall = (
        round(statistics.mean(item["score"] for item in categories.values()), 4)
        if complete
        else None
    )
    return {
        "name": "Waypoint operational service evaluation",
        "definition": "Equal-weight mean of parsing accuracy, retrieval quality, two-model answer quality, frontend quality, and performance. This is separate from code coverage.",
        "threshold": threshold,
        "complete": complete,
        "missing_categories": missing,
        "overall_score": overall,
        "passed": complete and overall is not None and overall >= threshold and all(
            item["passed"] for item in categories.values()
        ),
        "categories": categories,
        "artifacts": {key: str(value) for key, value in paths.items()},
    }


def _markdown(result: dict[str, Any]) -> str:
    overall_text = (
        f"{result['overall_score']:.1%}"
        if result["overall_score"] is not None
        else "not calculated"
    )
    lines = [
        "# Operational service evaluation",
        "",
        result["definition"],
        "",
        f"Status: **{'PASS' if result['passed'] else 'INCOMPLETE' if not result['complete'] else 'FAIL'}**  ",
        f"Overall: **{overall_text}**  ",
        f"Threshold: **{result['threshold']:.1%} per category and overall**",
        "",
        "| Category | Score | Threshold | Status |",
        "|---|---:|---:|---|",
    ]
    for name, item in result["categories"].items():
        lines.append(
            f"| {name.replace('_', ' ').title()} | {item['score']:.1%} | "
            f"{item['threshold']:.1%} | {'PASS' if item['passed'] else 'FAIL'} |"
        )
    if result["missing_categories"]:
        lines += ["", "Missing artifacts: " + ", ".join(result["missing_categories"])]
    lines += [
        "",
        "This score is an explicitly chosen product rubric, not a standard mathematical definition of coverage. Raw metrics remain authoritative and are retained in the linked JSON artifacts.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Waypoint's operational service scorecard")
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--model-result", type=Path, default=Path("verification/results/model-quality-claude-code-openrouter.json"))
    parser.add_argument("--output", type=Path, default=Path("verification/results/operational-scorecard.json"))
    arguments = parser.parse_args()
    paths = {
        "parsing": Path("benchmarks/results/parsing-accuracy.json"),
        "retrieval": Path("benchmarks/results/retrieval-v2.json"),
        "performance": Path("verification/results/service-performance.json"),
        "model": arguments.model_result,
        "playwright": Path("frontend/test-results/results.json"),
        "frontend_coverage": Path("frontend/coverage/coverage-summary.json"),
        "frontend_performance": Path("verification/results/frontend-performance.json"),
    }
    result = build(paths, arguments.threshold)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    markdown = _markdown(result)
    arguments.output.with_suffix(".md").write_text(markdown + "\n", encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
