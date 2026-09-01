from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3
    )


def _rss_mib(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
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

            handle = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0400, False, pid)
            if not handle:
                return None
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            ctypes.windll.kernel32.CloseHandle(handle)
            return round(counters.WorkingSetSize / 1024 / 1024, 3) if ok else None
        statm = Path(f"/proc/{pid}/statm").read_text().split()
        return round(int(statm[1]) * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024, 3)
    except (OSError, ValueError):
        return None


def _request(base_url: str, path: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = httpx.get(f"{base_url}{path}", timeout=60)
        latency = (time.perf_counter() - started) * 1000
        return {
            "path": path,
            "status": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "latency_ms": latency,
            "bytes": len(response.content),
        }
    except httpx.HTTPError as exc:
        return {
            "path": path,
            "status": None,
            "ok": False,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "bytes": 0,
            "error": str(exc),
        }


def run(
    base_url: str,
    analysis_id: str,
    requests_per_level: int,
    concurrency_levels: list[int],
    server_pid: int | None,
) -> dict[str, Any]:
    paths = [
        "/api/v1/health",
        f"/api/v1/analyses/{analysis_id}",
        f"/api/v1/analyses/{analysis_id}/summary",
        f"/api/v1/analyses/{analysis_id}/index",
        f"/api/v1/analyses/{analysis_id}/tree?limit=500",
        f"/api/v1/analyses/{analysis_id}/conversation/latest?scope=ask",
    ]
    baseline_memory = _rss_mib(server_pid)
    cache_timings = []
    for path in paths:
        samples = [_request(base_url, path) for _ in range(4)]
        cache_timings.append({
            "path": path,
            "cold_ms": round(samples[0]["latency_ms"], 3),
            "warm_mean_ms": round(
                statistics.mean(sample["latency_ms"] for sample in samples[1:]), 3
            ),
            "all_successful": all(sample["ok"] for sample in samples),
        })

    levels = []
    observed_memory = [value for value in [baseline_memory] if value is not None]
    for concurrency in concurrency_levels:
        started = time.perf_counter()
        results = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(_request, base_url, paths[index % len(paths)])
                for index in range(requests_per_level)
            ]
            for future in as_completed(futures):
                results.append(future.result())
                memory = _rss_mib(server_pid)
                if memory is not None:
                    observed_memory.append(memory)
        elapsed = time.perf_counter() - started
        latencies = [float(item["latency_ms"]) for item in results]
        succeeded = sum(bool(item["ok"]) for item in results)
        levels.append({
            "concurrency": concurrency,
            "requests": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "success_rate": round(succeeded / max(1, len(results)), 4),
            "throughput_requests_per_second": round(len(results) / max(elapsed, 0.001), 3),
            "latency_p50_ms": _percentile(latencies, 0.50),
            "latency_p95_ms": _percentile(latencies, 0.95),
            "latency_p99_ms": _percentile(latencies, 0.99),
            "max_latency_ms": round(max(latencies, default=0), 3),
        })
    ending_memory = _rss_mib(server_pid)
    if ending_memory is not None:
        observed_memory.append(ending_memory)
    total_requests = sum(level["requests"] for level in levels)
    total_succeeded = sum(level["succeeded"] for level in levels)
    return {
        "base_url": base_url,
        "analysis_id": analysis_id,
        "paths": paths,
        "requests_per_level": requests_per_level,
        "service_success_rate": round(total_succeeded / max(1, total_requests), 4),
        "cache_timings": cache_timings,
        "load_levels": levels,
        "memory": {
            "server_pid": server_pid,
            "baseline_rss_mib": baseline_memory,
            "peak_observed_rss_mib": max(observed_memory) if observed_memory else None,
            "ending_rss_mib": ending_memory,
            "growth_mib": (
                round(ending_memory - baseline_memory, 3)
                if ending_memory is not None and baseline_memory is not None
                else None
            ),
        },
        "slo": {
            "success_rate": 0.99,
            "p95_latency_ms": 2000,
            "passed": all(
                level["success_rate"] >= 0.99 and level["latency_p95_ms"] <= 2000
                for level in levels
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure Waypoint HTTP latency, concurrency, cache timing, and memory")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--analysis-id", required=True)
    parser.add_argument("--requests-per-level", type=int, default=60)
    parser.add_argument("--concurrency", type=int, action="append", default=[])
    parser.add_argument("--server-pid", type=int)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("verification/results/service-performance.json"),
    )
    arguments = parser.parse_args()
    result = run(
        arguments.base_url.rstrip("/"),
        arguments.analysis_id,
        max(6, arguments.requests_per_level),
        arguments.concurrency or [1, 4, 8],
        arguments.server_pid,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
