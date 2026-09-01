from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from collections import Counter
from pathlib import Path
from typing import Any

from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.models import EdgeKind, NodeKind


SOURCES = {
    "python/service.py": (
        "class UserService:\n"
        "    def find(self, user_id: str) -> str:\n"
        "        return user_id\n"
    ),
    "python/handler.py": (
        "from .service import UserService\n"
        "def handle() -> str:\n"
        "    service = UserService()\n"
        "    return service.find('42')\n"
    ),
    "web/math.js": "export function increment(value) { return value + 1; }\n",
    "web/counter.ts": (
        "import { increment } from './math.js';\n"
        "export class Counter { run(value: number): number { return increment(value); } }\n"
    ),
    "web/Card.tsx": (
        "export interface CardProps { label: string }\n"
        "export function Card({ label }: CardProps) { return <button>{label}</button>; }\n"
    ),
    "java/src/main/java/com/example/Worker.java": (
        "package com.example;\n"
        "public class Worker { public void run() {} }\n"
    ),
    "java/src/main/java/com/example/Main.java": (
        "package com.example;\n"
        "import com.example.Worker;\n"
        "public class Main { public void start() { Worker worker = new Worker(); worker.run(); } }\n"
    ),
    "site/theme.css": ".shell, #app { color: red; }\n",
    "site/index.html": (
        "<!doctype html><html><head><link rel='stylesheet' href='theme.css'></head>"
        "<body><main id='app' class='shell'></main></body></html>\n"
    ),
}

# The fixture is intentionally small enough to audit by hand. These are semantic
# entities and cross-file relationships a correct parser must recover.
EXPECTED_NODES = {
    "python.service.UserService",
    "python.service.UserService.find",
    "python.handler.handle",
    "web.math.increment",
    "web.counter.Counter",
    "web.counter.Counter.run",
    "web.Card.CardProps",
    "web.Card.Card",
    "com.example.Worker.Worker",
    "com.example.Worker.Worker.run",
    "com.example.Main.Main",
    "com.example.Main.Main.start",
    "site.#app",
    "site..shell",
    "site.theme.#app",
    "site.theme..shell",
}

EXPECTED_EDGES = {
    ("imports", "python.handler", "python.service"),
    ("instantiates", "python.handler.handle", "python.service.UserService"),
    ("may_call", "python.handler.handle", "python.service.UserService.find"),
    ("imports", "web.counter", "web.math"),
    ("may_call", "web.counter.Counter.run", "web.math.increment"),
    ("imports", "com.example.Main", "com.example.Worker"),
    ("instantiates", "com.example.Main.Main.start", "com.example.Worker.Worker"),
    ("may_call", "com.example.Main.Main.start", "com.example.Worker.Worker.run"),
    ("imports", "site", "site.theme"),
}


def _ratio(found: set[Any], expected: set[Any]) -> dict[str, Any]:
    true_positive = len(found & expected)
    false_positive = len(found - expected)
    false_negative = len(expected - found)
    precision = true_positive / (true_positive + false_positive) if found else 1.0
    recall = true_positive / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "unexpected": sorted(map(str, found - expected)),
        "missing": sorted(map(str, expected - found)),
    }


def evaluate() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for relative, content in SOURCES.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        tracemalloc.start()
        started = time.perf_counter()
        report = RepositoryAnalyzer().analyze(root)
        duration_ms = (time.perf_counter() - started) * 1000
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    nodes_by_id = {node.id: node for node in report.nodes}
    found_nodes = {
        node.qualified_name
        for node in report.nodes
        if node.kind not in {NodeKind.REPOSITORY, NodeKind.MODULE}
    }
    found_edges = {
        (
            edge.kind.value,
            nodes_by_id[edge.source].qualified_name,
            nodes_by_id[edge.target].qualified_name,
        )
        for edge in report.edges
        if edge.kind in {EdgeKind.IMPORTS, EdgeKind.INSTANTIATES, EdgeKind.MAY_CALL}
    }
    node_metrics = _ratio(found_nodes, EXPECTED_NODES)
    edge_metrics = _ratio(found_edges, EXPECTED_EDGES)
    return {
        "fixture_files": len(SOURCES),
        "languages": ["python", "javascript", "typescript", "tsx", "java", "html", "css"],
        "files_discovered": report.stats.files_discovered,
        "files_parsed": report.stats.files_parsed,
        "parse_failures": report.stats.parse_failures,
        "parse_success_rate": round(
            report.stats.files_parsed / max(1, report.stats.files_discovered), 4
        ),
        "duration_ms": round(duration_ms, 3),
        "peak_python_allocation_mib": round(peak_bytes / 1024 / 1024, 3),
        "node_metrics": node_metrics,
        "edge_metrics": edge_metrics,
        "edge_metrics_by_kind": {
            kind: _ratio(
                {edge for edge in found_edges if edge[0] == kind},
                {edge for edge in EXPECTED_EDGES if edge[0] == kind},
            )
            for kind in ("imports", "instantiates", "may_call")
        },
        "emitted_node_counts": dict(Counter(node.kind.value for node in report.nodes)),
        "emitted_edge_counts": dict(Counter(edge.kind.value for edge in report.edges)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure polyglot parser precision and recall")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/results/parsing-accuracy.json"),
    )
    arguments = parser.parse_args()
    result = evaluate()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
