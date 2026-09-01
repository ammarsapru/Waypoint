from __future__ import annotations

import argparse
import ctypes
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

from backend.app.agent.retrieval import RepositoryRetrievalIndex
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.store import AnalysisSession
from backend.app.indexing import repository_snapshot


def _deduplicated_paths(retrieved: list[dict[str, object]]) -> list[str]:
    """Rank files, not repeated chunks from the same file."""
    paths: list[str] = []
    seen: set[str] = set()
    for item in retrieved:
        path = str(item["path"])
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _score_case(case: dict[str, object], paths: list[str]) -> dict[str, object]:
    raw_groups = case.get("required_path_groups")
    if raw_groups:
        groups = [set(map(str, group)) for group in raw_groups]  # type: ignore[arg-type]
    else:
        groups = [{str(path)} for path in case.get("expected_paths", [])]  # type: ignore[arg-type]
    relevant = set(map(str, case.get("acceptable_paths", [])))  # type: ignore[arg-type]
    for group in groups:
        relevant.update(group)
    forbidden = set(map(str, case.get("forbidden_paths", [])))  # type: ignore[arg-type]

    first_rank = next(
        (rank for rank, path in enumerate(paths, 1) if path in relevant), None
    )
    precision: dict[int, float] = {}
    recall: dict[int, float] = {}
    group_recall: dict[int, float] = {}
    hit: dict[int, float] = {}
    for cutoff in (1, 3, 5, 10):
        selected = paths[:cutoff]
        selected_set = set(selected)
        relevant_hits = sum(path in relevant for path in selected)
        precision[cutoff] = relevant_hits / max(1, len(selected))
        recall[cutoff] = relevant_hits / max(1, len(relevant))
        group_recall[cutoff] = (
            sum(bool(group & selected_set) for group in groups) / max(1, len(groups))
        )
        hit[cutoff] = 1.0 if relevant_hits else 0.0

    average_precision = 0.0
    running_hits = 0
    for rank, path in enumerate(paths[:10], 1):
        if path in relevant:
            running_hits += 1
            average_precision += running_hits / rank
    average_precision /= max(1, min(len(relevant), 10))
    ideal_hits = min(len(relevant), 10)
    ideal_dcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    dcg = sum(
        1 / math.log2(rank + 1)
        for rank, path in enumerate(paths[:10], 1)
        if path in relevant
    )
    ndcg = dcg / ideal_dcg if ideal_dcg else 1.0
    forbidden_hits = sorted(set(paths[:10]) & forbidden)

    max_first_rank = int(case.get("max_first_relevant_rank", 5))
    minimum_group_recall = float(case.get("min_required_group_recall_at_10", 1.0))
    minimum_precision = float(case.get("min_precision_at_5", 0.0))
    strict_pass = (
        first_rank is not None
        and first_rank <= max_first_rank
        and group_recall[10] >= minimum_group_recall
        and precision[5] >= minimum_precision
        and not forbidden_hits
    )
    return {
        "first_relevant_rank": first_rank,
        "hit_at_1": hit[1],
        "hit_at_3": hit[3],
        "hit_at_5": hit[5],
        "hit_at_10": hit[10],
        "precision_at_5": round(precision[5], 4),
        "precision_at_10": round(precision[10], 4),
        "path_recall_at_5": round(recall[5], 4),
        "path_recall_at_10": round(recall[10], 4),
        "required_group_recall_at_5": round(group_recall[5], 4),
        "required_group_recall_at_10": round(group_recall[10], 4),
        "average_precision_at_10": round(average_precision, 4),
        "ndcg_at_10": round(ndcg, 4),
        "forbidden_hits": forbidden_hits,
        "strict_pass": strict_pass,
        "strict_requirements": {
            "max_first_relevant_rank": max_first_rank,
            "min_required_group_recall_at_10": minimum_group_recall,
            "min_precision_at_5": minimum_precision,
            "no_forbidden_paths_at_10": True,
        },
        "required_path_groups": [sorted(group) for group in groups],
        "acceptable_paths": sorted(relevant),
    }


def _peak_memory_mib() -> float:
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        get_process_memory_info.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        handle = get_current_process()
        if get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 1024 / 1024
        return 0.0
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024 if sys.platform != "darwin" else value / 1024 / 1024


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def evaluate(repository: Path, cases_path: Path, repeats: int = 3) -> dict[str, object]:
    root = repository.resolve()
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    report = RepositoryAnalyzer().analyze(root).model_copy(
        update={"analysis_id": "retrieval-benchmark"}
    )
    snapshot = repository_snapshot(root)
    session = AnalysisSession(
        id="retrieval-benchmark",
        root=root,
        report=report,
        source_paths=snapshot.paths,
        revision_fingerprint=snapshot.fingerprint,
    )
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "retrieval.sqlite3"
        index_started = time.perf_counter()
        index = RepositoryRetrievalIndex(
            session, database_path=database,
            snapshot=snapshot,
        )
        index_ms = (time.perf_counter() - index_started) * 1000
        peak_memory_mib = _peak_memory_mib()
        restore_started = time.perf_counter()
        index = RepositoryRetrievalIndex(
            session, database_path=database, snapshot=snapshot
        )
        restore_ms = (time.perf_counter() - restore_started) * 1000
        results = []
        reciprocal_ranks: list[float] = []
        latencies: list[float] = []
        cold_latencies: list[float] = []
        warm_latencies: list[float] = []
        case_scores: list[dict[str, object]] = []
        for case in cases:
            retrieved = []
            case_latencies: list[float] = []
            for _ in range(max(1, repeats)):
                started = time.perf_counter()
                retrieved = index.search(case["question"], limit=10)
                latency = (time.perf_counter() - started) * 1000
                latencies.append(latency)
                case_latencies.append(latency)
            cold_latencies.append(case_latencies[0])
            warm_latencies.extend(case_latencies[1:])
            paths = _deduplicated_paths(retrieved)
            score = _score_case(case, paths)
            case_scores.append(score)
            first_rank = score["first_relevant_rank"]
            reciprocal_ranks.append(1 / first_rank if isinstance(first_rank, int) else 0.0)
            results.append({
                "id": case.get("id"),
                "question": case["question"],
                "retrieved_paths": paths,
                **score,
                "cold_latency_ms": round(case_latencies[0], 3),
                "warm_mean_latency_ms": round(
                    statistics.mean(case_latencies[1:] or case_latencies), 3
                ),
            })
    count = max(1, len(cases))
    means = {
        key: statistics.mean(float(score[key]) for score in case_scores)
        for key in (
            "hit_at_1", "hit_at_3", "hit_at_5", "hit_at_10",
            "precision_at_5", "precision_at_10", "path_recall_at_5",
            "path_recall_at_10", "required_group_recall_at_5",
            "required_group_recall_at_10", "average_precision_at_10",
            "ndcg_at_10",
        )
    }
    strict_pass_rate = statistics.mean(
        1.0 if score["strict_pass"] else 0.0 for score in case_scores
    ) if case_scores else 0.0
    forbidden_hit_rate = statistics.mean(
        1.0 if score["forbidden_hits"] else 0.0 for score in case_scores
    ) if case_scores else 0.0
    strict_quality_score = statistics.mean([
        strict_pass_rate,
        means.get("required_group_recall_at_10", 0.0),
        means.get("average_precision_at_10", 0.0),
        statistics.mean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        means.get("ndcg_at_10", 0.0),
        1.0 - forbidden_hit_rate,
    ])
    return {
        "repository": str(root),
        "case_count": len(cases),
        "index_ms": round(index_ms, 3),
        "index_restore_ms": round(restore_ms, 3),
        "process_peak_memory_mib": round(peak_memory_mib, 3),
        "hit_rate_at_1": round(means.get("hit_at_1", 0.0), 4),
        "hit_rate_at_3": round(means.get("hit_at_3", 0.0), 4),
        "recall_at_5": round(means.get("hit_at_5", 0.0), 4),
        "recall_at_10": round(means.get("hit_at_10", 0.0), 4),
        "hit_rate_at_5": round(means.get("hit_at_5", 0.0), 4),
        "hit_rate_at_10": round(means.get("hit_at_10", 0.0), 4),
        "precision_at_5": round(means.get("precision_at_5", 0.0), 4),
        "precision_at_10": round(means.get("precision_at_10", 0.0), 4),
        "path_recall_at_5": round(means.get("path_recall_at_5", 0.0), 4),
        "path_recall_at_10": round(means.get("path_recall_at_10", 0.0), 4),
        "required_group_recall_at_5": round(
            means.get("required_group_recall_at_5", 0.0), 4
        ),
        "required_group_recall_at_10": round(
            means.get("required_group_recall_at_10", 0.0), 4
        ),
        "mean_average_precision_at_10": round(
            means.get("average_precision_at_10", 0.0), 4
        ),
        "mean_reciprocal_rank": round(statistics.mean(reciprocal_ranks), 4),
        "ndcg_at_10": round(means.get("ndcg_at_10", 0.0), 4),
        "forbidden_path_hit_rate_at_10": round(forbidden_hit_rate, 4),
        "strict_case_pass_rate": round(strict_pass_rate, 4),
        "strict_quality_score": round(strict_quality_score, 4),
        "mean_query_ms": round(statistics.mean(latencies), 3),
        "query_p50_ms": round(_percentile(latencies, 0.50), 3),
        "query_p95_ms": round(_percentile(latencies, 0.95), 3),
        "query_p99_ms": round(_percentile(latencies, 0.99), 3),
        "cold_query_mean_ms": round(statistics.mean(cold_latencies), 3),
        "warm_query_mean_ms": round(
            statistics.mean(warm_latencies or cold_latencies), 3
        ),
        "query_repeats": max(1, repeats),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Waypoint code retrieval")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument(
        "--cases", type=Path,
        default=Path(__file__).with_name("retrieval_cases.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    arguments = parser.parse_args()
    result = evaluate(arguments.repository, arguments.cases, max(1, arguments.repeats))
    payload = json.dumps(result, indent=2)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
