from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.app.observability import register_function_hook, trace_context, traced


@traced("benchmark.observability.noop")
def _instrumented_noop(value: int) -> int:
    return value + 1


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return round(ordered[index], 6)


def _run_sample(
    iterations: int, *, hooks_enabled: bool, measure_memory: bool = False
) -> dict[str, Any]:
    events: list[str] = []

    def recorder(phase: str, payload: Any) -> None:
        events.append(json.dumps({"phase": phase, **dict(payload)}, sort_keys=True))

    unregister = register_function_hook(recorder) if hooks_enabled else None
    object.__setattr__(settings, "trace_functions", hooks_enabled)
    if measure_memory:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        with trace_context("observability-overhead"):
            checksum = sum(_instrumented_noop(index) for index in range(iterations))
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        peak_bytes = 0
        if measure_memory:
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        if unregister is not None:
            unregister()
    return {
        "elapsed_ms": elapsed_ms,
        "peak_bytes": peak_bytes,
        "event_count": len(events),
        "serialized_bytes": sum(len(event.encode("utf-8")) + 1 for event in events),
        "checksum": checksum,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure the incremental cost of Waypoint function hooks."
    )
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/results/observability-overhead-v4.json"),
    )
    args = parser.parse_args()
    if args.iterations < 1 or args.samples < 3:
        parser.error("iterations must be positive and samples must be at least 3")

    original_trace = settings.trace_functions
    root = logging.getLogger()
    previous_disabled = root.disabled
    root.disabled = True
    try:
        # Alternate order to reduce warm-up and CPU-frequency bias.
        disabled: list[dict[str, Any]] = []
        enabled: list[dict[str, Any]] = []
        for sample in range(args.samples + 1):
            first_enabled = sample % 2 == 1
            first = _run_sample(args.iterations, hooks_enabled=first_enabled)
            second = _run_sample(args.iterations, hooks_enabled=not first_enabled)
            if sample == 0:  # Unreported warm-up pair.
                continue
            (enabled if first_enabled else disabled).append(first)
            (disabled if first_enabled else enabled).append(second)
        memory_iterations = min(args.iterations, 250)
        baseline_memory = _run_sample(
            memory_iterations, hooks_enabled=False, measure_memory=True
        )
        enabled_memory = _run_sample(
            memory_iterations, hooks_enabled=True, measure_memory=True
        )
    finally:
        object.__setattr__(settings, "trace_functions", original_trace)
        root.disabled = previous_disabled

    disabled_times = [row["elapsed_ms"] for row in disabled]
    enabled_times = [row["elapsed_ms"] for row in enabled]
    baseline = statistics.median(disabled_times)
    instrumented = statistics.median(enabled_times)
    incremental = instrumented - baseline
    overhead_percent = (incremental / baseline * 100) if baseline else None
    event_count = int(statistics.median(row["event_count"] for row in enabled))
    report = {
        "schema_version": "v4",
        "generated_at": datetime.now(UTC).isoformat(),
        "benchmark_type": "isolated function-hook microbenchmark",
        "scope_note": (
            "Measures @traced argument/result sanitization, trace correlation, hook dispatch, "
            "and in-memory JSON serialization. Terminal and rotating-file logging are disabled; "
            "this is not end-to-end request latency."
        ),
        "iterations_per_sample": args.iterations,
        "reported_samples": args.samples,
        "baseline": {
            "median_ms": round(baseline, 6),
            "p95_ms": _percentile(disabled_times, 0.95),
            "peak_bytes": baseline_memory["peak_bytes"],
        },
        "hooks_enabled": {
            "median_ms": round(instrumented, 6),
            "p95_ms": _percentile(enabled_times, 0.95),
            "peak_bytes": enabled_memory["peak_bytes"],
            "events_per_sample": event_count,
            "bytes_per_sample": int(statistics.median(row["serialized_bytes"] for row in enabled)),
        },
        "incremental": {
            "median_ms": round(incremental, 6),
            "overhead_percent": round(overhead_percent, 3) if overhead_percent is not None else None,
            "microseconds_per_hook_event": round(
                incremental * 1000 / event_count, 6
            ) if event_count else None,
        },
        "memory_probe_iterations": memory_iterations,
        "raw_samples": {"baseline": disabled, "hooks_enabled": enabled},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["incremental"], indent=2), flush=True)


if __name__ == "__main__":
    main()
