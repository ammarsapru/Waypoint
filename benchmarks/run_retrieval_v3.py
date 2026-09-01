from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.run_retrieval import evaluate


METRICS = (
    "hit_at_1", "hit_at_3", "hit_at_5", "hit_at_10", "precision_at_5",
    "precision_at_10", "path_recall_at_5", "path_recall_at_10",
    "required_group_recall_at_5", "required_group_recall_at_10",
    "average_precision_at_10", "ndcg_at_10",
)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"cases": 0}
    means = {
        metric: statistics.mean(float(row[metric]) for row in rows)
        for metric in METRICS
    }
    mrr = statistics.mean(
        1 / int(row["first_relevant_rank"])
        if isinstance(row.get("first_relevant_rank"), int) else 0.0
        for row in rows
    )
    strict_pass = statistics.mean(bool(row["strict_pass"]) for row in rows)
    forbidden = statistics.mean(bool(row["forbidden_hits"]) for row in rows)
    strict_score = statistics.mean((
        strict_pass,
        means["required_group_recall_at_10"],
        means["average_precision_at_10"],
        mrr,
        means["ndcg_at_10"],
        1.0 - forbidden,
    ))
    return {
        "cases": len(rows),
        **{key: round(value, 4) for key, value in means.items()},
        "mean_reciprocal_rank": round(mrr, 4),
        "forbidden_path_hit_rate_at_10": round(forbidden, 4),
        "strict_case_pass_rate": round(strict_pass, 4),
        "strict_quality_score": round(strict_score, 4),
    }


def run(
    manifest_path: Path,
    repositories: dict[str, Path],
    repeats: int,
    allow_unadjudicated: bool,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    if len(cases) != 50:
        raise ValueError(f"V3 retrieval manifest must contain 50 cases, found {len(cases)}")
    pending = [
        case["id"] for case in cases
        if case.get("adjudication", {}).get("status") != "human_approved"
    ]
    if pending and not allow_unadjudicated:
        raise ValueError(
            f"{len(pending)} cases require human adjudication; pass "
            "--allow-unadjudicated only for development measurements"
        )
    missing = sorted(set(manifest["repositories"]) - set(repositories))
    if missing:
        raise ValueError("Missing repository paths for: " + ", ".join(missing))

    repository_results: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    case_metadata = {case["id"]: case for case in cases}
    for repository in manifest["repositories"]:
        root = repositories[repository]
        selected_cases = [case for case in cases if case["repository"] == repository]
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.json"
            cases_path.write_text(json.dumps(selected_cases), encoding="utf-8")
            result = evaluate(root, cases_path, repeats)
        for row in result["results"]:
            metadata = case_metadata[row["id"]]
            row.update({
                "repository": repository,
                "split": metadata["split"],
                "language": metadata["language"],
                "adjudication": metadata["adjudication"],
            })
            all_rows.append(row)
        repository_results[repository] = result

    slices: dict[str, dict[str, Any]] = {}
    for field in ("repository", "split", "language"):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in all_rows:
            grouped[str(row[field])].append(row)
        slices[field] = {key: _summary(rows) for key, rows in sorted(grouped.items())}
    return {
        "benchmark_version": manifest["version"],
        "release_gate_eligible": not pending,
        "pending_human_adjudication": pending,
        "case_count": len(all_rows),
        "query_repeats": repeats,
        "overall": _summary(all_rows),
        "slices": slices,
        "repository_performance": {
            key: {
                "index_ms": value["index_ms"],
                "index_restore_ms": value["index_restore_ms"],
                "process_peak_memory_mib": value["process_peak_memory_mib"],
                "mean_query_ms": value["mean_query_ms"],
                "query_p95_ms": value["query_p95_ms"],
            }
            for key, value in repository_results.items()
        },
        "results": all_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Waypoint's 50-case V3 retrieval evaluation")
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/retrieval_cases_v3.json"))
    parser.add_argument("--repository", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/retrieval-v3.json"))
    parser.add_argument("--allow-unadjudicated", action="store_true")
    arguments = parser.parse_args()
    repositories: dict[str, Path] = {}
    for item in arguments.repository:
        if "=" not in item:
            parser.error(f"invalid --repository value {item!r}; expected NAME=PATH")
        name, path = item.split("=", 1)
        if not name.strip() or not path.strip():
            parser.error(f"invalid --repository value {item!r}; expected NAME=PATH")
        if name in repositories:
            parser.error(f"duplicate repository name: {name}")
        repositories[name] = Path(path).resolve()
    result = run(
        arguments.manifest,
        repositories,
        max(1, arguments.repeats),
        arguments.allow_unadjudicated,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "case_count": result["case_count"],
        "release_gate_eligible": result["release_gate_eligible"],
        "overall": result["overall"],
        "slices": result["slices"],
        "repository_performance": result["repository_performance"],
    }, indent=2))


if __name__ == "__main__":
    main()
