from __future__ import annotations

import argparse
import ctypes
import json
import logging
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.models import (
    EdgeKind,
    EvidenceStatus,
    GraphNode,
    NodeKind,
)
from backend.app.observability import configure_logging, log_event, trace_context
from backend.app.onboarding.models import DeveloperRole, TourRequest
from backend.app.onboarding.service import OnboardingService

logger = logging.getLogger("benchmarks")


@dataclass(frozen=True, slots=True)
class RepositoryExpectation:
    name: str
    directory: str
    size: str
    first_party_prefixes: tuple[str, ...]
    expected_modules: tuple[str, ...]
    expected_symbols: tuple[str, ...]
    forbidden_module_prefixes: tuple[str, ...] = ()
    allowed_unresolved_import_prefixes: tuple[str, ...] = ()
    minimum_python_files: int = 1
    maximum_parse_failure_ratio: float = 0.01


EXPECTATIONS = {
    "django": RepositoryExpectation(
        name="Django",
        directory="django",
        size="large",
        first_party_prefixes=("django",),
        expected_modules=(
            "django",
            "django.apps",
            "django.core.handlers.base",
            "django.db.models.base",
            "django.http.response",
        ),
        expected_symbols=(
            "django.core.handlers.base.BaseHandler",
            "django.db.models.base.Model",
            "django.http.response.HttpResponse",
        ),
        minimum_python_files=2500,
    ),
    "flask": RepositoryExpectation(
        name="Flask",
        directory="flask",
        size="medium",
        first_party_prefixes=("flask",),
        expected_modules=("flask", "flask.app", "flask.blueprints", "flask.ctx"),
        expected_symbols=(
            "flask.app.Flask",
            "flask.blueprints.Blueprint",
            "flask.ctx.AppContext",
        ),
        forbidden_module_prefixes=("src.flask",),
        minimum_python_files=70,
    ),
    "requests": RepositoryExpectation(
        name="Requests",
        directory="requests",
        size="small-medium",
        first_party_prefixes=("requests",),
        expected_modules=(
            "requests",
            "requests.api",
            "requests.models",
            "requests.sessions",
        ),
        expected_symbols=(
            "requests.api.get",
            "requests.models.Response",
            "requests.sessions.Session",
        ),
        forbidden_module_prefixes=("src.requests",),
        allowed_unresolved_import_prefixes=("requests.packages.",),
        minimum_python_files=30,
    ),
    "itsdangerous": RepositoryExpectation(
        name="ItsDangerous",
        directory="itsdangerous",
        size="small",
        first_party_prefixes=("itsdangerous",),
        expected_modules=(
            "itsdangerous",
            "itsdangerous.serializer",
            "itsdangerous.signer",
            "itsdangerous.timed",
        ),
        expected_symbols=(
            "itsdangerous.serializer.Serializer",
            "itsdangerous.signer.Signer",
            "itsdangerous.timed.TimestampSigner",
        ),
        forbidden_module_prefixes=("src.itsdangerous",),
        minimum_python_files=10,
    ),
}


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class BenchmarkResult:
    repository: str
    size: str
    commit: str
    generated_at: str
    elapsed_seconds: float
    peak_memory_mib: float
    stats: dict[str, Any]
    node_counts: dict[str, int]
    edge_counts: dict[str, int]
    evidence_counts: dict[str, int]
    architecture_seconds: float
    import_cycle_count: int
    hotspot_count: int
    tour_first_modules: dict[str, str]
    mission_target: str
    checks: list[Check] = field(default_factory=list)
    missing_modules: list[str] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)
    suspicious_first_party_imports: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _commit(repository: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", "--short", "HEAD"],
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _peak_memory_mib() -> float:
    """Read process peak working set without allocation-tracing overhead."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
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
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_process_memory_info.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = get_current_process()
        success = get_process_memory_info(
            process, ctypes.byref(counters), counters.cb
        )
        return (
            counters.PeakWorkingSetSize / 1024 / 1024 if success else 0.0
        )
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return peak / divisor
    except (ImportError, OSError):
        return 0.0


def _check(condition: bool, name: str, success: str, failure: str) -> Check:
    return Check(name=name, passed=condition, detail=success if condition else failure)


def _imported_module(reference: str) -> str:
    normalized = reference.strip()
    if normalized.startswith("from "):
        return normalized[5:].split(" import ", 1)[0].strip()
    if normalized.startswith("import "):
        return normalized[7:].split(",", 1)[0].strip().split(" as ", 1)[0]
    return normalized.split(" as ", 1)[0].strip()


def _is_first_party_import(reference: str, prefixes: tuple[str, ...]) -> bool:
    imported = _imported_module(reference)
    return any(
        imported == prefix or imported.startswith(f"{prefix}.")
        for prefix in prefixes
    )


def _node_lookup(nodes: list[GraphNode]) -> tuple[set[str], set[str]]:
    modules = {
        node.qualified_name for node in nodes if node.kind == NodeKind.MODULE
    }
    symbols = {
        node.qualified_name
        for node in nodes
        if node.kind in {
            NodeKind.CLASS,
            NodeKind.FUNCTION,
            NodeKind.METHOD,
        }
    }
    return modules, symbols


def benchmark(
    repository: Path, expectation: RepositoryExpectation
) -> BenchmarkResult:
    log_event(
        logger,
        logging.INFO,
        "benchmark.repository_started",
        "External repository benchmark started",
        repository=expectation.name,
        size=expectation.size,
        path=repository,
    )
    started = time.perf_counter()
    report = RepositoryAnalyzer(max_files=10_000).analyze(repository)
    elapsed = time.perf_counter() - started
    interactive_report = report.model_copy(
        update={"analysis_id": f"benchmark-{expectation.directory}"}
    )
    onboarding = OnboardingService(interactive_report)
    architecture_started = time.perf_counter()
    architecture = onboarding.architecture_report()
    architecture_seconds = time.perf_counter() - architecture_started
    tours = {
        role.value: onboarding.plan_tour(
            TourRequest(
                role=role,
                goal=f"Benchmark the {role.value} onboarding route",
                minutes=15,
            )
        )
        for role in DeveloperRole
    }
    mission = onboarding.contribution_mission(DeveloperRole.BACKEND)
    peak_memory_mib = _peak_memory_mib()

    nodes = report.nodes
    edges = report.edges
    node_ids = [node.id for node in nodes]
    edge_ids = [edge.id for edge in edges]
    node_id_set = set(node_ids)
    modules, symbols = _node_lookup(nodes)
    missing_modules = sorted(set(expectation.expected_modules) - modules)
    missing_symbols = sorted(set(expectation.expected_symbols) - symbols)
    forbidden_modules = sorted(
        module
        for module in modules
        if module.startswith(expectation.forbidden_module_prefixes)
    )
    invalid_endpoints = [
        edge.id
        for edge in edges
        if edge.source not in node_id_set or edge.target not in node_id_set
    ]
    invalid_spans = [
        node.id
        for node in nodes
        if node.span
        and (
            node.span.start_line < 1
            or node.span.end_line < node.span.start_line
            or not node.span.path
        )
    ]
    evidence_mismatches = [
        edge.id
        for edge in edges
        if (
            edge.kind in {EdgeKind.CONTAINS, EdgeKind.IMPORTS}
            and edge.evidence.status != EvidenceStatus.VERIFIED
        )
        or (
            edge.kind == EdgeKind.MAY_CALL
            and edge.evidence.status != EvidenceStatus.INFERRED
        )
    ]
    suspicious_imports = sorted(
        {
            reference.reference
            for reference in report.unresolved_references
            if reference.reference_kind == "import"
            and _is_first_party_import(
                reference.reference, expectation.first_party_prefixes
            )
            and not _imported_module(reference.reference).startswith(
                expectation.allowed_unresolved_import_prefixes
            )
        }
    )
    parse_ratio = (
        report.stats.parse_failures / report.stats.files_discovered
        if report.stats.files_discovered
        else 1.0
    )
    checks = [
        _check(
            report.stats.files_discovered >= expectation.minimum_python_files,
            "repository_size",
            f"Discovered {report.stats.files_discovered} Python files",
            (
                f"Expected at least {expectation.minimum_python_files} files, "
                f"found {report.stats.files_discovered}"
            ),
        ),
        _check(
            parse_ratio <= expectation.maximum_parse_failure_ratio,
            "parse_success",
            f"Parse failure ratio {parse_ratio:.3%}",
            f"Parse failure ratio {parse_ratio:.3%} exceeds threshold",
        ),
        _check(
            len(node_ids) == len(set(node_ids)),
            "unique_node_ids",
            "All node IDs are unique",
            f"Found {len(node_ids) - len(set(node_ids))} duplicate node IDs",
        ),
        _check(
            len(edge_ids) == len(set(edge_ids)),
            "unique_edge_ids",
            "All edge IDs are unique",
            f"Found {len(edge_ids) - len(set(edge_ids))} duplicate edge IDs",
        ),
        _check(
            not invalid_endpoints,
            "valid_edge_endpoints",
            "Every edge endpoint resolves to a node",
            f"{len(invalid_endpoints)} edges have missing endpoints",
        ),
        _check(
            not invalid_spans,
            "valid_source_spans",
            "Every source-backed node has a valid span",
            f"{len(invalid_spans)} nodes have invalid spans",
        ),
        _check(
            not evidence_mismatches,
            "evidence_invariants",
            "Edge kinds use the expected evidence status",
            f"{len(evidence_mismatches)} edges violate evidence invariants",
        ),
        _check(
            not missing_modules,
            "architectural_modules",
            "All expected architectural modules were found",
            f"Missing modules: {', '.join(missing_modules)}",
        ),
        _check(
            not missing_symbols,
            "architectural_symbols",
            "All expected architectural symbols were found",
            f"Missing symbols: {', '.join(missing_symbols)}",
        ),
        _check(
            not forbidden_modules,
            "source_layout",
            "Package names do not leak the source-directory prefix",
            f"Incorrect module prefixes found, e.g. {forbidden_modules[:5]}",
        ),
        _check(
            not suspicious_imports,
            "first_party_import_resolution",
            "No obvious first-party imports remain unresolved",
            (
                f"{len(suspicious_imports)} obvious first-party imports remain "
                f"unresolved, e.g. {suspicious_imports[:5]}"
            ),
        ),
        _check(
            all(tour.steps for tour in tours.values()),
            "adaptive_tours",
            "Every developer role produced a source-backed route",
            "At least one developer role produced an empty route",
        ),
        _check(
            len({tour.steps[0].node_id for tour in tours.values()}) >= 2,
            "role_route_diversity",
            "Role-specific tours do not all begin at the same module",
            "Every role begins at the same module",
        ),
        _check(
            mission.target_node.span is not None,
            "contribution_mission",
            "Contribution mission has a source-backed target",
            "Contribution mission target has no source evidence",
        ),
    ]
    result = BenchmarkResult(
        repository=expectation.name,
        size=expectation.size,
        commit=_commit(repository),
        generated_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=round(elapsed, 3),
        peak_memory_mib=round(peak_memory_mib, 2),
        stats=report.stats.model_dump(),
        node_counts=dict(Counter(node.kind.value for node in nodes)),
        edge_counts=dict(Counter(edge.kind.value for edge in edges)),
        evidence_counts=dict(
            Counter(edge.evidence.status.value for edge in edges)
        ),
        architecture_seconds=round(architecture_seconds, 3),
        import_cycle_count=architecture.import_cycle_count,
        hotspot_count=architecture.hotspot_count,
        tour_first_modules={
            role: tour.steps[0].evidence.path if tour.steps[0].evidence else ""
            for role, tour in tours.items()
        },
        mission_target=mission.target_node.qualified_name,
        checks=checks,
        missing_modules=missing_modules,
        missing_symbols=missing_symbols,
        suspicious_first_party_imports=suspicious_imports[:100],
    )
    log_event(
        logger,
        logging.INFO if result.passed else logging.ERROR,
        "benchmark.repository_completed",
        "External repository benchmark completed",
        repository=expectation.name,
        passed=result.passed,
        elapsed_seconds=result.elapsed_seconds,
        peak_memory_mib=result.peak_memory_mib,
        stats=result.stats,
        failed_checks=[
            check.name for check in result.checks if not check.passed
        ],
    )
    return result


def _markdown(results: list[BenchmarkResult]) -> str:
    lines = [
        "# Analyzer external-repository benchmark",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        "| Repository | Size | Commit | Files | Nodes | Edges | Time | Peak memory | Result |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    result.repository,
                    result.size,
                    result.commit,
                    str(result.stats["files_discovered"]),
                    str(result.stats["node_count"]),
                    str(result.stats["edge_count"]),
                    f"{result.elapsed_seconds:.2f}s",
                    f"{result.peak_memory_mib:.2f} MiB",
                    "PASS" if result.passed else "FAIL",
                ]
            )
            + " |"
        )
    for result in results:
        lines.extend(
            [
                "",
                f"## {result.repository}",
                "",
                *[
                    f"- {'PASS' if check.passed else 'FAIL'} — {check.name}: {check.detail}"
                    for check in result.checks
                ],
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixtures-root",
        type=Path,
        default=Path(r"C:\tmp\waypoint-benchmarks"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("benchmarks/results")
    )
    parser.add_argument(
        "--repository",
        choices=tuple(EXPECTATIONS),
        action="append",
        help="Run only selected repositories; may be repeated.",
    )
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Write baseline results without returning a failing exit code.",
    )
    arguments = parser.parse_args()
    configure_logging()
    selected = arguments.repository or list(EXPECTATIONS)
    missing = [
        name
        for name in selected
        if not (arguments.fixtures_root / EXPECTATIONS[name].directory).is_dir()
    ]
    if missing:
        parser.error(f"Missing fixture directories: {', '.join(missing)}")
    with trace_context("external-repository-benchmark"):
        results = [
            benchmark(
                arguments.fixtures_root / EXPECTATIONS[name].directory,
                EXPECTATIONS[name],
            )
            for name in selected
        ]
    arguments.output.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": all(result.passed for result in results),
        "results": [
            {
                **asdict(result),
                "passed": result.passed,
            }
            for result in results
        ],
    }
    (arguments.output / "latest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    (arguments.output / "latest.md").write_text(
        _markdown(results), encoding="utf-8"
    )
    print(_markdown(results))
    return 0 if payload["passed"] or arguments.allow_failures else 1


if __name__ == "__main__":
    sys.exit(main())
