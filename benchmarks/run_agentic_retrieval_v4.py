from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import threading
from collections import Counter
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.agent.agentic_comparison import AgenticRetrievalComparisonService
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.store import analysis_sessions
from backend.app.onboarding.models import AgenticComparisonRequest, RetrievalLabels
from backend.app.observability import register_event_hook, register_function_hook


STAGES = ("candidate_retrieval", "explicit_inspection", "cited_retrieval")
METRICS = (
    "hit_at_5", "hit_at_10", "precision_at_10", "path_recall",
    "required_group_recall", "mrr", "ndcg_at_10", "forbidden_hit_rate",
)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


class HookRecorder:
    """Durably record sanitized pre/post/error hooks while a long benchmark runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.phase_counts: Counter[str] = Counter()
        self.function_counts: Counter[str] = Counter()
        self.error_counts: Counter[str] = Counter()
        self.durations: dict[str, list[float]] = defaultdict(list)
        self.trace_ids: set[str] = set()
        self.trace_event_counts: Counter[str] = Counter()
        self.span_phases: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.event_count = 0
        self.correlated_events = 0
        self.serialized_bytes = 0
        self.redaction_markers = 0
        self.possible_secret_leaks = 0
        self.callback_duration_ms = 0.0
        self.structured_event_counts: Counter[str] = Counter()
        self.structured_events = 0
        self.path.write_text("", encoding="utf-8")

    def _write(self, event: dict[str, Any], started: float) -> None:
        serialized = json.dumps(event, ensure_ascii=False)
        trace_id = str(event.get("trace_id", "-"))
        with self.lock:
            self.serialized_bytes += len((serialized + "\n").encode("utf-8"))
            self.redaction_markers += serialized.count("<redacted>")
            self.possible_secret_leaks += int(bool(re.search(
                r"(?:sk-(?!<redacted>)[A-Za-z0-9_-]{12,}|Bearer\s+(?!<redacted>)[A-Za-z0-9._-]{12,})",
                serialized,
            )))
            if trace_id != "-":
                self.trace_ids.add(trace_id)
                self.correlated_events += 1
                self.trace_event_counts[trace_id] += 1
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
            self.callback_duration_ms += (
                __import__("time").perf_counter() - started
            ) * 1000

    def __call__(self, phase: str, payload: Any) -> None:
        started = __import__("time").perf_counter()
        if str(payload.get("trace_id", "-")) == "-":
            return
        event = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "phase": phase,
            **dict(payload),
        }
        function = str(event.get("function", "unknown"))
        trace_id = str(event.get("trace_id", "-"))
        duration = event.get("duration_ms")
        span_id = str(event.get("span_id", "-"))
        with self.lock:
            self.event_count += 1
            self.phase_counts[phase] += 1
            self.function_counts[function] += 1
            if span_id != "-":
                self.span_phases[(trace_id, span_id)].add(phase)
            if isinstance(duration, (int, float)):
                self.durations[function].append(float(duration))
            if phase == "error":
                self.error_counts[str(event.get("exception_type", "unknown"))] += 1
        self._write({"record_type": "function_hook", **event}, started)

    def record_event(self, payload: Any) -> None:
        started = __import__("time").perf_counter()
        if str(payload.get("trace_id", "-")) == "-":
            return
        event = {
            "record_type": "structured_log_event",
            "recorded_at": datetime.now(UTC).isoformat(),
            **dict(payload),
        }
        with self.lock:
            self.event_count += 1
            self.structured_events += 1
            self.structured_event_counts[str(event.get("event", "unknown"))] += 1
        self._write(event, started)

    def report(self) -> dict[str, Any]:
        paired = sum(
            "pre" in phases and bool({"post", "error"}.intersection(phases))
            for phases in self.span_phases.values()
        )
        started = sum("pre" in phases for phases in self.span_phases.values())
        orphan_terminals = sum(
            "pre" not in phases and bool({"post", "error"}.intersection(phases))
            for phases in self.span_phases.values()
        )
        incomplete = sum(
            "pre" in phases and not {"post", "error"}.intersection(phases)
            for phases in self.span_phases.values()
        )
        return {
            "event_count": self.event_count,
            "trace_count": len(self.trace_ids),
            "phase_counts": dict(self.phase_counts),
            "structured_log_event_count": self.structured_events,
            "structured_event_counts": dict(self.structured_event_counts),
            "error_counts": dict(self.error_counts),
            "quality": {
                "trace_correlation_rate": round(
                    self.correlated_events / self.event_count, 4
                ) if self.event_count else 0.0,
                "pre_terminal_pairing_rate": round(paired / started, 4) if started else 0.0,
                "started_spans": started,
                "paired_spans": paired,
                "orphan_terminal_spans": orphan_terminals,
                "incomplete_spans": incomplete,
                "possible_secret_leaks": self.possible_secret_leaks,
                "redaction_markers": self.redaction_markers,
            },
            "volume": {
                "serialized_bytes": self.serialized_bytes,
                "bytes_per_event": round(self.serialized_bytes / self.event_count, 3)
                if self.event_count else 0.0,
                "events_per_trace": round(self.event_count / len(self.trace_ids), 3)
                if self.trace_ids else 0.0,
                "hook_callback_total_ms": round(self.callback_duration_ms, 3),
                "hook_callback_mean_ms": round(self.callback_duration_ms / self.event_count, 6)
                if self.event_count else 0.0,
            },
            "events_by_trace": dict(self.trace_event_counts),
            "functions": {
                function: {
                    "events": count,
                    "completed_durations": len(self.durations.get(function, [])),
                    "mean_duration_ms": round(statistics.mean(self.durations[function]), 3)
                    if self.durations.get(function) else None,
                    "p50_duration_ms": _percentile(self.durations.get(function, []), 0.50),
                    "p95_duration_ms": _percentile(self.durations.get(function, []), 0.95),
                }
                for function, count in self.function_counts.most_common()
            },
        }


def _parse_pair(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("endpoint must be PROVIDER:MODEL")
    provider, model = value.split(":", 1)
    if not provider or not model:
        raise argparse.ArgumentTypeError("endpoint must be PROVIDER:MODEL")
    return provider, model


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[f"{row['provider']}:{row['model']}"].append(row)
    result: dict[str, Any] = {}
    for endpoint, values in grouped.items():
        durations = [float(item["duration_ms"]) for item in values]
        tokens = [int(item["total_tokens"]) for item in values if item["total_tokens"] is not None]
        outputs = [int(item["output_tokens"]) for item in values if item["output_tokens"] is not None]
        stages: dict[str, Any] = {}
        for stage in STAGES:
            stages[stage] = {
                metric: round(statistics.mean(
                    float(item[stage][metric]) for item in values
                ), 4)
                for metric in METRICS
            }
        result[endpoint] = {
            "cases": len(values),
            "successful_cases": sum(item["validation_status"] == "passed" for item in values),
            "mean_duration_ms": round(statistics.mean(item["duration_ms"] for item in values), 3),
            "p50_duration_ms": _percentile(durations, 0.50),
            "p95_duration_ms": _percentile(durations, 0.95),
            "total_duration_ms": round(sum(durations), 3),
            "mean_tool_calls": round(statistics.mean(item["repository_tool_calls"] for item in values), 3),
            "mean_rounds": round(statistics.mean(item["total_model_rounds"] for item in values), 3),
            "mean_invalid_tool_calls": round(statistics.mean(item["invalid_tool_calls"] for item in values), 3),
            "mean_total_tokens": round(statistics.mean(
                item["total_tokens"] for item in values if item["total_tokens"] is not None
            ), 3) if any(item["total_tokens"] is not None for item in values) else None,
            "total_tokens": sum(tokens) if tokens else None,
            "tokens_per_tool_call": round(
                sum(tokens) / max(1, sum(item["repository_tool_calls"] for item in values)), 3
            ) if tokens else None,
            "output_tokens_per_second": round(
                sum(outputs) / max(0.001, sum(durations) / 1000), 3
            ) if outputs else None,
            "mean_expected_concept_recall": round(statistics.mean(
                item["answer_quality"]["expected_concept_recall"] for item in values
            ), 4),
            "mean_citations": round(statistics.mean(
                item["answer_quality"]["citation_count"] for item in values
            ), 3),
            "stages": stages,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the live V4 independent-agent retrieval comparison"
    )
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/retrieval_cases_v4.json"))
    parser.add_argument(
        "--cases-manifest", type=Path,
        help="Override the protocol base cases (supports the V2 list or V3 cases object).",
    )
    parser.add_argument("--repository", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--analysis", action="append", default=[], metavar="NAME=ANALYSIS_ID")
    parser.add_argument("--endpoint", action="append", type=_parse_pair, required=True)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--max-cases", type=int, default=100)
    parser.add_argument("--allow-unadjudicated", action="store_true")
    parser.add_argument("--confirm-live-provider-usage", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/agentic-retrieval-v4.json"))
    parser.add_argument("--hooks-output", type=Path)
    parser.add_argument("--hooks-report", type=Path)
    args = parser.parse_args()
    if not args.confirm_live_provider_usage:
        parser.error("live model calls require --confirm-live-provider-usage")
    if not 2 <= len(args.endpoint) <= 4 or len(set(args.endpoint)) != len(args.endpoint):
        parser.error("provide two to four distinct --endpoint values")
    protocol = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest_names = protocol.get("base_manifests") or [protocol["base_manifest"]]
    base_paths = (
        [args.cases_manifest]
        if args.cases_manifest
        else [args.manifest.parent / item for item in manifest_names]
    )
    cases = []
    for base_path in base_paths:
        base = json.loads(base_path.read_text(encoding="utf-8"))
        cases.extend(base["cases"] if isinstance(base, dict) else base)
    cases = [
        {
            "repository": "waypoint",
            "split": "development",
            "language": "mixed",
            "adjudication": {"status": "pending_human"},
            **case,
        }
        for case in cases
    ]
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case["id"] in selected]
    cases = cases[:max(1, args.max_cases)]
    pending = [
        case["id"] for case in cases
        if case.get("adjudication", {}).get("status") != "human_approved"
    ]
    if pending and not args.allow_unadjudicated:
        parser.error("labels are pending human review; pass --allow-unadjudicated for development")

    sessions: dict[str, Any] = {}
    for item in args.analysis:
        name, analysis_id = item.split("=", 1)
        sessions[name] = analysis_sessions.get(analysis_id)
    for item in args.repository:
        name, raw_path = item.split("=", 1)
        root = Path(raw_path).resolve()
        report = RepositoryAnalyzer().analyze(root)
        stored = analysis_sessions.create(root, report)
        sessions[name] = analysis_sessions.get(stored.analysis_id or "")
    missing = sorted({case["repository"] for case in cases} - set(sessions))
    if missing:
        parser.error("missing --repository or --analysis for: " + ", ".join(missing))

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    hook_path = args.hooks_output or args.output.with_suffix(".hooks.jsonl")
    hook_report_path = args.hooks_report or args.output.with_suffix(".hooks.json")
    hooks = HookRecorder(hook_path)
    unregister = register_function_hook(hooks)
    unregister_events = register_event_hook(hooks.record_event)
    comparison_services = {
        name: AgenticRetrievalComparisonService(session)
        for name, session in sessions.items()
    }

    def output_payload() -> dict[str, Any]:
        attempted = len(rows) + len(failures)
        return {
        "benchmark_version": protocol["version"],
        "protocol": protocol["protocol"],
        "cases_manifests": [str(path) for path in base_paths],
        "release_gate_eligible": not pending,
        "pending_human_adjudication": pending,
        "endpoints": [{"provider": p, "model": m} for p, m in args.endpoint],
        "case_count": len(cases),
        "successful_model_runs": len(rows),
        "failed_model_runs": len(failures),
        "completed_model_attempts": attempted,
        "expected_model_attempts": len(cases) * len(args.endpoint),
        "success_rate": round(len(rows) / attempted, 4) if attempted else 0.0,
        "summary": _summary(rows),
        "runs": rows,
        "failures": failures,
        "hook_artifacts": {
            "events_jsonl": str(hook_path),
            "aggregate_json": str(hook_report_path),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for case in cases:
            request = AgenticComparisonRequest(
                question=case["question"],
                labels=RetrievalLabels(
                    required_path_groups=case["required_path_groups"],
                    acceptable_paths=case.get("acceptable_paths", []),
                    forbidden_paths=case.get("forbidden_paths", []),
                ),
            )
            report = comparison_services[case["repository"]].compare_endpoints(
                request, args.endpoint, parallel=False
            )
            for run in report.runs:
                answer_text = run.answer.casefold()
                concepts = [str(item) for item in case.get("expected_concepts", [])]
                matched = [item for item in concepts if item.casefold() in answer_text]
                rows.append({
                    "case_id": case["id"], "repository": case["repository"],
                    "split": case["split"], "language": case["language"],
                    "category": case.get("category", "repository_question"),
                    "question": case["question"],
                    "expected_concepts": concepts,
                    "answer_quality": {
                        "matched_expected_concepts": matched,
                        "expected_concept_recall": len(matched) / len(concepts)
                        if concepts else 0.0,
                        "citation_count": len(run.citations),
                    },
                    **run.model_dump(mode="json"),
                })
            failures.extend({
                "case_id": case["id"], "repository": case["repository"],
                **failure.model_dump(mode="json"),
            } for failure in report.failures)
            args.output.write_text(
                json.dumps(output_payload(), indent=2) + "\n", encoding="utf-8"
            )
            hook_report_path.write_text(
                json.dumps(hooks.report(), indent=2) + "\n", encoding="utf-8"
            )
            print(f"{case['id']}: {len(report.runs)} passed, {len(report.failures)} failed", flush=True)
    finally:
        unregister()
        unregister_events()

    output = output_payload()
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    hook_report_path.write_text(
        json.dumps(hooks.report(), indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: output[key] for key in (
        "benchmark_version", "case_count", "successful_model_runs",
        "failed_model_runs", "summary",
    )}, indent=2))


if __name__ == "__main__":
    main()
