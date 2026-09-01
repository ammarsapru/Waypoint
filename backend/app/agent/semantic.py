from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from backend.app.agent.retrieval import RepositoryRetrievalIndex
from backend.app.graph.models import EdgeKind, GraphEdge, GraphNode, NodeKind
from backend.app.graph.store import AnalysisSession
from backend.app.observability import log_event

logger = logging.getLogger(__name__)

_MANIFEST_NAMES = {
    "build.gradle",
    "build.gradle.kts",
    "gradle.properties",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "settings.gradle",
    "settings.gradle.kts",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
}
_FRAMEWORK_SIGNALS = {
    "FastAPI": ("fastapi",),
    "Flask": ("flask",),
    "Django": ("django",),
    "React": ("react", "createroot"),
    "Vite": ("vite",),
    "Tailwind CSS": ("tailwind",),
    "Express": ("express",),
    "Spring Boot": ("spring-boot", "springapplication", "springbootapplication"),
    "Maven": ("maven", "pom.xml"),
    "Gradle": ("gradle",),
}
_ENTRY_PATH_MARKERS = (
    "/main.",
    "/app.",
    "/server.",
    "/index.tsx",
    "/index.jsx",
    "application.java",
    "manage.py",
    "wsgi.py",
    "asgi.py",
)
_BACKEND_LAYERS = {
    "entrypoints": ("main", "app", "server", "bootstrap", "application"),
    "transport": ("route", "router", "controller", "endpoint", "api", "http"),
    "services": ("service", "usecase", "use_case", "handler", "orchestrat"),
    "domain": ("domain", "model", "entity", "schema", "dto"),
    "persistence": ("repository", "store", "database", "db", "dao", "persistence"),
    "configuration": ("config", "setting", "environment", "container", "wiring"),
    "integrations": ("client", "gateway", "integration", "provider", "adapter"),
    "tests": ("test", "spec", "fixture", "e2e"),
}


class SemanticRepositoryTools:
    """Bounded deterministic architecture queries over one analyzed repository.

    Wraps an ``AnalysisSession`` graph report plus its ``RepositoryRetrievalIndex``
    to answer higher-level architecture questions (overview, entry points, layers,
    file structure, dependency impact, ...) as plain dict payloads with bounded
    source evidence, all backed by a per-revision result cache. Instantiated by
    backend/app/agent/service.py:RepositoryAgentService.__init__ (as ``self.semantic``)
    and by backend/app/mcp_server.py:_semantic for MCP tool handlers; its public
    methods are also exposed as agent tools (get_repository_overview,
    find_entry_points, etc.) so the LLM agent and the MCP surface share one
    implementation of these deterministic, non-LLM repository queries.
    """

    def __init__(
        self,
        session: AnalysisSession,
        index: RepositoryRetrievalIndex,
    ) -> None:
        """Indexes graph nodes/edges once and builds the outgoing/incoming edge and degree lookups.

        Input: `session` is the AnalysisSession for one analyzed repository revision;
        `index` is the RepositoryRetrievalIndex used to read source text and to share
        the semantic result cache. Called wherever a SemanticRepositoryTools instance
        is created (RepositoryAgentService.__init__, backend/app/mcp_server.py:_semantic,
        and tests); precomputing adjacency/degree here avoids re-scanning the full
        edge list on every subsequent query method call.
        """
        self.session = session
        self.index = index
        self.report = session.report
        self.nodes = {node.id: node for node in self.report.nodes}
        self.modules = [node for node in self.report.nodes if node.kind == NodeKind.MODULE]
        self.edges = self.report.edges
        self.outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        self.incoming: dict[str, list[GraphEdge]] = defaultdict(list)
        self.degree: Counter[str] = Counter()
        for edge in self.edges:
            self.outgoing[edge.source].append(edge)
            self.incoming[edge.target].append(edge)
            self.degree[edge.source] += 1
            self.degree[edge.target] += 1
        self._cache = index.semantic_result_cache
        self._cache_lock = index.semantic_result_cache_lock

    def _cached(self, name: str, arguments: dict[str, Any], build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Returns a memoized result for (tool name, arguments), building it once via `build` on a miss.

        Input: `name` is the semantic tool's identifier, `arguments` is its bounded
        (already-clamped) argument dict used to form the cache key, and `build` is a
        zero-arg callable that computes the actual dict result. Called by every public
        query method below (repository_overview, feature_evidence, entry_points, ...)
        as their final `return self._cached(...)` line; the cache is shared across
        SemanticRepositoryTools instances for the same repository revision (see
        RepositoryRetrievalIndex.semantic_result_cache), so repeated agent tool calls
        with identical arguments avoid recomputation within one immutable analysis.
        """
        key = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            log_event(
                logger,
                logging.INFO,
                "agent.semantic_cache_hit",
                "Reused a deterministic repository tool result",
                analysis_id=self.session.id,
                tool=name,
                arguments=arguments,
            )
            return cached
        created = build()
        with self._cache_lock:
            result = self._cache.setdefault(key, created)
        log_event(
            logger,
            logging.INFO,
            "agent.semantic_cache_stored",
            "Stored a deterministic repository tool result for this revision",
            analysis_id=self.session.id,
            tool=name,
            arguments=arguments,
        )
        return result

    @staticmethod
    def _is_test_path(path: str) -> bool:
        """Heuristically decides whether a repository-relative path is test code rather than production code.

        Input: `path` is a repository-relative file path string (any OS separator).
        Used throughout this class (repository_overview, feature_evidence, entry_points,
        related_tests, ...) to separate "production" candidates from test files by
        directory/filename convention, since the graph itself has no test/production
        flag; this keeps overview and entry-point results focused on shipped code
        while still letting related_tests specifically select test files.
        """
        lowered = path.lower().replace("\\", "/")
        name = Path(path).name.lower()
        return (
            "/test/" in f"/{lowered}/"
            or "/tests/" in f"/{lowered}/"
            or "/__tests__/" in f"/{lowered}/"
            or name.startswith("test_")
            or ".test." in name
            or ".spec." in name
        )

    def _evidence(self, node: GraphNode, max_lines: int = 80) -> dict[str, Any] | None:
        """Reads a bounded source excerpt for one graph node's span, for use as citation-backing evidence.

        Input: `node` is a GraphNode that may or may not have a source `span`;
        `max_lines` caps how much of the span is read (excerpt text is further
        capped at 4,000 characters). Returns None when the node has no span or the
        index cannot read it. Called by nearly every query method in this class
        (repository_overview, entry_points, symbol_relationships, dependency_impact,
        related_tests, ...) to attach real source text to each returned candidate so
        the agent's answers stay grounded in actual repository content.
        """
        if node.span is None:
            return None
        end = min(node.span.end_line, node.span.start_line + max_lines - 1)
        try:
            source = self.index.read(node.span.path, node.span.start_line, end)
        except ValueError:
            return None
        return {
            "path": source["path"],
            "start_line": source["start_line"],
            "end_line": source["end_line"],
            "node_id": node.id,
            "qualified_name": node.qualified_name,
            "kind": node.kind.value,
            "excerpt": source["content"][:4_000],
        }

    def _path_evidence(self, path: str, max_lines: int = 120) -> dict[str, Any] | None:
        """Reads a bounded excerpt from the start of a whole file (not tied to a graph node), for evidence.

        Input: `path` is a repository-relative file path (e.g. a README or manifest);
        `max_lines` bounds the read and the excerpt is further capped at 5,000
        characters. Returns None if the index cannot read the path. Called by
        repository_overview and project_configuration to surface documentation and
        manifest file contents (which are files, not graph symbols) as supporting
        evidence alongside node-based evidence from `_evidence`.
        """
        try:
            source = self.index.read(path, 1, max_lines)
        except ValueError:
            return None
        return {
            "path": source["path"],
            "start_line": source["start_line"],
            "end_line": source["end_line"],
            "excerpt": source["content"][:5_000],
        }

    def _edge_evidence(self, edge: GraphEdge) -> dict[str, Any] | None:
        """Reads a small source excerpt around the line where a graph edge (call/import/etc.) was detected.

        Input: `edge` is a GraphEdge whose `evidence.span` marks the source location
        the edge was inferred from; reads a narrow window (one line before to up to
        eight lines after) capped at 2,000 characters, since edges need only enough
        context to show the reference, not the whole containing function. Called by
        symbol_relationships to back each incoming/outgoing relationship with the
        actual line of code that produced it.
        """
        span = edge.evidence.span
        try:
            source = self.index.read(
                span.path,
                max(1, span.start_line - 1),
                min(span.end_line + 1, span.start_line + 8),
            )
        except ValueError:
            return None
        return {
            "path": source["path"],
            "start_line": source["start_line"],
            "end_line": source["end_line"],
            "edge_id": edge.id,
            "relationship": edge.kind.value,
            "excerpt": source["content"][:2_000],
        }

    @staticmethod
    def _public_node(node: GraphNode) -> dict[str, Any]:
        """Projects an internal GraphNode into the small, stable dict shape exposed to agent tool callers.

        Input: `node` is any GraphNode from the analysis report. Used throughout this
        class (repository_overview, entry_points, backend_architecture,
        symbol_relationships, dependency_impact, related_tests, ...) every time a node
        needs to appear in a tool result, so the JSON shape returned to the LLM/MCP
        client stays consistent and does not leak internal-only GraphNode fields.
        """
        return {
            "node_id": node.id,
            "kind": node.kind.value,
            "name": node.name,
            "qualified_name": node.qualified_name,
            "path": node.span.path if node.span else None,
            "start_line": node.span.start_line if node.span else None,
            "end_line": node.span.end_line if node.span else None,
            "language": node.metadata.get("language"),
        }

    def repository_overview(self) -> dict[str, Any]:
        """Summarizes the whole repository: languages, node/edge counts, detected frameworks, docs, manifests, central modules, and evidence.

        Input: none (operates on the full analysis report already loaded in
        `self.report`/`self.modules`). Exposed as the `get_repository_overview` agent
        tool (see TOOLS/SEMANTIC_TOOLS in backend/app/agent/service.py, dispatched
        from `RepositoryAgentService._execute_tool`) and called directly in
        backend/tests/test_semantic_tools.py; it is the recommended first call for
        "what is this repository / what does it do" questions since it front-loads
        framework and central-module evidence in one bounded call.
        """
        def build() -> dict[str, Any]:
            """Builds the (uncached) repository overview payload; invoked once by `_cached` on a cache miss."""
            node_counts = Counter(node.kind.value for node in self.report.nodes)
            edge_counts = Counter(edge.kind.value for edge in self.edges)
            language_counts = Counter(
                str(node.metadata.get("language", "unknown")) for node in self.modules
            )
            readmes = sorted(
                path
                for path in self.session.source_paths
                if Path(path).name.lower() in {"readme.md", "readme.mdx", "readme.txt"}
            )
            manifests = sorted(
                path
                for path in self.session.source_paths
                if Path(path).name.lower() in _MANIFEST_NAMES
            )
            ranked = sorted(
                (node for node in self.modules if node.span and not self._is_test_path(node.span.path)),
                key=lambda node: (
                    -sum(marker in node.qualified_name.lower() for marker in ("main", "app", "api", "service", "server")),
                    -self.degree[node.id],
                    node.qualified_name,
                ),
            )[:8]
            evidence = [
                item
                for item in (
                    *[self._path_evidence(path, 140) for path in readmes[:2]],
                    *[self._path_evidence(path, 120) for path in manifests[:5]],
                    *[self._evidence(node, 60) for node in ranked[:5]],
                )
                if item is not None
            ][:12]
            evidence_text = "\n".join(str(item.get("excerpt", "")).lower() for item in evidence)
            frameworks = sorted(
                framework
                for framework, signals in _FRAMEWORK_SIGNALS.items()
                if any(signal in evidence_text for signal in signals)
            )
            return {
                "repository_name": self.report.repository_name,
                "files_analyzed": self.report.stats.files_parsed,
                "languages": dict(language_counts),
                "node_counts": dict(node_counts),
                "edge_counts": dict(edge_counts),
                "framework_signals": frameworks,
                "documentation": readmes[:10],
                "manifests": manifests[:20],
                "central_modules": [self._public_node(node) for node in ranked],
                "evidence": evidence,
            }

        return self._cached("repository_overview", {}, build)

    def feature_evidence(self, limit: int = 10) -> dict[str, Any]:
        """Selects diverse, keyword-ranked production symbols plus documentation as candidate evidence for "what features exist" questions.

        Input: `limit` is the requested candidate count, clamped to [1, 20]. Exposed
        as the `get_feature_evidence` agent tool (dispatched from
        `RepositoryAgentService._execute_tool`); the docstring on the returned
        payload explicitly warns the model that candidates are evidence, not
        confirmed product claims, since ranking here is a heuristic keyword score,
        not a semantic understanding of what the code actually does.
        """
        bounded = max(1, min(limit, 20))

        def build() -> dict[str, Any]:
            """Builds the (uncached) feature-evidence payload; invoked once by `_cached` on a cache miss."""
            overview = self.repository_overview()
            production = [
                node
                for node in self.report.nodes
                if node.span
                and node.kind in {NodeKind.MODULE, NodeKind.CLASS, NodeKind.FUNCTION}
                and not self._is_test_path(node.span.path)
            ]
            ranked = sorted(
                production,
                key=lambda node: (
                    -sum(
                        marker in f"{node.qualified_name} {node.span.path}".lower()
                        for marker in (
                            "api", "route", "agent", "graph", "onboard", "issue",
                            "auth", "search", "clone", "source", "service", "ui",
                        )
                    ),
                    -self.degree[node.id],
                    node.qualified_name,
                ),
            )
            selected: list[GraphNode] = []
            seen_paths: set[str] = set()
            for node in ranked:
                if node.span.path in seen_paths and len(selected) < bounded // 2:
                    continue
                selected.append(node)
                seen_paths.add(node.span.path)
                if len(selected) >= bounded:
                    break
            evidence = [item for item in (self._evidence(node, 70) for node in selected) if item]
            documentation_evidence = overview["evidence"][:4]
            return {
                "candidate_count": len(selected),
                "candidates": [self._public_node(node) for node in selected],
                "instruction": (
                    "Treat these as source-backed feature candidates, not final product claims. "
                    "Use documentation evidence to name user-facing capabilities."
                ),
                "evidence": [*documentation_evidence, *evidence][:16],
            }

        return self._cached("feature_evidence", {"limit": bounded}, build)

    def entry_points(self, limit: int = 15) -> dict[str, Any]:
        """Ranks likely application entry points (main/startup symbols) by path-marker and naming-convention scoring plus graph connectivity.

        Input: `limit` is the requested candidate count, clamped to [1, 30]. Exposed
        as the `find_entry_points` agent tool (dispatched from
        `RepositoryAgentService._execute_tool`) and as the `find_entry_points` MCP
        tool in backend/app/mcp_server.py; used for "how do I run/start this" and
        "where does execution begin" questions since the graph alone has no notion
        of "entry point".
        """
        bounded = max(1, min(limit, 30))

        def build() -> dict[str, Any]:
            """Builds the (uncached) entry-point ranking; invoked once by `_cached` on a cache miss."""
            ranked: list[tuple[float, GraphNode, list[str]]] = []
            for node in self.report.nodes:
                if node.span is None or self._is_test_path(node.span.path):
                    continue
                haystack = f"/{node.span.path} {node.name} {node.qualified_name}".lower()
                reasons: list[str] = []
                score = 0.0
                for marker in _ENTRY_PATH_MARKERS:
                    if marker in haystack:
                        score += 10
                        reasons.append(f"path/name matches {marker}")
                if node.name.lower() in {"main", "create_app", "app", "lifespan", "run", "start", "bootstrap"}:
                    score += 14
                    reasons.append("conventional startup symbol")
                if node.kind == NodeKind.MODULE:
                    score += min(self.degree[node.id], 20) * 0.3
                if score > 0:
                    ranked.append((score, node, reasons))
            ranked.sort(key=lambda item: (-item[0], item[1].qualified_name))
            selected = ranked[:bounded]
            return {
                "candidates": [
                    {**self._public_node(node), "score": round(score, 2), "reasons": reasons}
                    for score, node, reasons in selected
                ],
                "evidence": [
                    item for item in (self._evidence(node, 100) for _, node, _ in selected) if item
                ][:bounded],
            }

        return self._cached("entry_points", {"limit": bounded}, build)

    def backend_architecture(self, limit_per_layer: int = 5) -> dict[str, Any]:
        """Buckets modules into architectural layers (entrypoints, transport, services, domain, persistence, configuration, integrations, tests) by path/name keyword matching against `_BACKEND_LAYERS`.

        Input: `limit_per_layer` caps how many modules are kept per layer, clamped to
        [1, 10]. Exposed as the `get_backend_architecture` agent tool (dispatched
        from `RepositoryAgentService._execute_tool`) and the `get_backend_architecture`
        MCP tool; answers "how is the backend structured / layered" questions. The
        classification is a name/path convention heuristic, not a semantic analysis,
        which is why the result explicitly carries a `classification_basis` caveat.
        """
        bounded = max(1, min(limit_per_layer, 10))

        def build() -> dict[str, Any]:
            """Builds the (uncached) layer classification; invoked once by `_cached` on a cache miss."""
            layers: dict[str, list[GraphNode]] = {}
            evidence: list[dict[str, Any]] = []
            for layer, markers in _BACKEND_LAYERS.items():
                candidates = [
                    node
                    for node in self.modules
                    if node.span
                    and any(marker in f"{node.qualified_name} {node.span.path}".lower() for marker in markers)
                ]
                candidates.sort(key=lambda node: (-self.degree[node.id], node.qualified_name))
                layers[layer] = candidates[:bounded]
                evidence.extend(
                    item
                    for item in (self._evidence(node, 70) for node in layers[layer][:3])
                    if item
                )
            return {
                "layers": {
                    layer: [self._public_node(node) for node in nodes]
                    for layer, nodes in layers.items()
                },
                "classification_basis": "Path/name conventions plus graph connectivity; verify ambiguous layers from evidence.",
                "evidence": evidence[:12],
            }

        return self._cached("backend_architecture", {"limit_per_layer": bounded}, build)

    def file_structure(self, path: str) -> dict[str, Any]:
        """Lists every analyzed symbol defined in one file plus its cross-file (incoming/outgoing) relationships and a source excerpt.

        Input: `path` is a repository-relative file path; it is normalized to
        POSIX form and validated against `self.session.source_paths`, raising
        ValueError if the file was not part of this analysis. Exposed as the
        `get_file_structure` agent tool (dispatched from
        `RepositoryAgentService._execute_tool`) and called directly in
        backend/tests/test_semantic_tools.py and backend/tests/test_agent.py; used
        for "what's in this file / what does it depend on" questions.
        """
        normalized = Path(path).as_posix()
        if normalized not in self.session.source_paths:
            raise ValueError("File is not part of this analysis")

        def build() -> dict[str, Any]:
            """Builds the (uncached) file-structure payload; invoked once by `_cached` on a cache miss."""
            members = sorted(
                (node for node in self.report.nodes if node.span and node.span.path == normalized),
                key=lambda node: (node.span.start_line, node.kind.value, node.qualified_name),
            )
            ids = {node.id for node in members}
            relationships = [
                edge for edge in self.edges if edge.source in ids or edge.target in ids
            ]
            external = []
            for edge in relationships:
                other_id = edge.target if edge.source in ids else edge.source
                other = self.nodes.get(other_id)
                if other and (not other.span or other.span.path != normalized):
                    external.append(
                        {
                            "direction": "outgoing" if edge.source in ids else "incoming",
                            "kind": edge.kind.value,
                            "symbol": self._public_node(other),
                            "confidence": edge.evidence.confidence,
                        }
                    )
            source = self._path_evidence(normalized, 250)
            return {
                "path": normalized,
                "language": next((node.metadata.get("language") for node in members if node.kind == NodeKind.MODULE), None),
                "symbols": [self._public_node(node) for node in members],
                "external_relationships": external[:100],
                "evidence": [source] if source else [],
            }

        return self._cached("file_structure", {"path": normalized}, build)

    def symbol_relationships(self, node_id: str) -> dict[str, Any]:
        """Returns one symbol's full immediate relationship set: outgoing/incoming edges, related files, and source evidence for both.

        Input: `node_id` is a graph node id (as returned by search/find tools);
        raises ValueError if it does not exist in `self.nodes`. Exposed as the
        `get_symbol_relationships` agent tool and MCP tool, called directly by
        backend/app/api/routes.py:get_symbol_usage (the `/nodes/{node_id}/usage`
        endpoint) and by backend/app/agent/service.py:RepositoryAgentService.answer
        when a request carries `focus_node_id`, to seed the model with mandatory
        starting evidence about a user-selected symbol before it investigates further.
        """
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError("Graph node was not found")

        def describe(edge: GraphEdge, direction: str) -> dict[str, Any]:
            """Projects one graph edge plus its resolution/evidence into the dict shape used for outgoing/incoming relationship entries."""
            other = self.nodes[edge.target if direction == "outgoing" else edge.source]
            return {
                "edge_id": edge.id,
                "direction": direction,
                "relationship": edge.kind.value,
                "symbol": self._public_node(other),
                "status": edge.evidence.status.value,
                "confidence": edge.evidence.confidence,
                "resolution": edge.evidence.resolution,
                "evidence": edge.evidence.model_dump(mode="json"),
            }

        def build() -> dict[str, Any]:
            """Builds the (uncached) symbol-relationships payload; invoked once by `_cached` on a cache miss."""
            outgoing_edges = self.outgoing[node_id]
            incoming_edges = self.incoming[node_id]
            outgoing = [describe(edge, "outgoing") for edge in outgoing_edges]
            incoming = [describe(edge, "incoming") for edge in incoming_edges]
            related_nodes = [
                self.nodes[edge.target] for edge in self.outgoing[node_id]
            ] + [self.nodes[edge.source] for edge in self.incoming[node_id]]
            related_files = sorted(
                {
                    candidate.span.path
                    for candidate in [node, *related_nodes]
                    if candidate.span is not None
                }
            )
            return {
                "symbol": self._public_node(node),
                "outgoing": outgoing[:100],
                "incoming": incoming[:100],
                "related_files": related_files,
                "evidence": [
                    item
                    for item in (
                        self._evidence(candidate, 50)
                        for candidate in [node, *related_nodes[:9]]
                    )
                    if item
                ] + [
                    item
                    for item in (
                        self._edge_evidence(edge)
                        for edge in [*incoming_edges, *outgoing_edges][:30]
                    )
                    if item
                ],
            }

        return self._cached("symbol_relationships", {"node_id": node_id}, build)

    def related_tests(self, query: str = "", node_id: str | None = None, limit: int = 15) -> dict[str, Any]:
        """Finds test files/symbols related to a free-text query and/or a specific graph node, ranked by keyword overlap and direct call/reference edges.

        Input: `query` is free text (may be empty), `node_id` is an optional graph
        node id whose name/path/edges boost matching tests, `limit` is clamped to
        [1, 30]. Exposed as the `find_related_tests` agent tool and MCP tool
        (`find_related_tests` in backend/app/mcp_server.py); used to answer "what
        tests cover this" questions, since the graph does not explicitly link
        production symbols to the tests that exercise them.
        """
        bounded = max(1, min(limit, 30))
        normalized = query.strip().lower()
        target = self.nodes.get(node_id) if node_id else None
        terms = {
            term
            for term in (
                normalized,
                target.name.lower() if target else "",
                Path(target.span.path).stem.lower() if target and target.span else "",
            )
            if term
        }

        def build() -> dict[str, Any]:
            """Builds the (uncached) related-tests ranking; invoked once by `_cached` on a cache miss."""
            tests = [
                node for node in self.report.nodes if node.span and self._is_test_path(node.span.path)
            ]
            scored: list[tuple[int, GraphNode]] = []
            for node in tests:
                haystack = f"{node.qualified_name} {node.span.path}".lower()
                score = sum(10 for term in terms if term in haystack)
                if target and any(
                    edge.source == node.id and edge.target == target.id for edge in self.edges
                ):
                    score += 30
                if score or not terms:
                    scored.append((score, node))
            scored.sort(key=lambda item: (-item[0], item[1].qualified_name))
            selected = [node for _, node in scored[:bounded]]
            return {
                "query": normalized,
                "target_node_id": node_id,
                "tests": [self._public_node(node) for node in selected],
                "evidence": [item for item in (self._evidence(node, 80) for node in selected) if item],
            }

        return self._cached(
            "related_tests",
            {"query": normalized, "node_id": node_id, "limit": bounded},
            build,
        )

    def dependency_impact(self, node_id: str, depth: int = 2) -> dict[str, Any]:
        """Breadth-first-walks reverse import/call/instantiate edges from a symbol to estimate which other symbols would be statically affected by changing it.

        Input: `node_id` must exist in `self.nodes` (raises ValueError otherwise);
        `depth` bounds the BFS hop count, clamped to [1, 4]. Exposed as the
        `get_dependency_impact` agent tool and MCP tool; the walk is capped at 100
        collected impacts as a safety bound on fan-out. This is static reverse-edge
        reachability only (not runtime proof of impact), which the tool description
        in TOOLS/SEMANTIC_TOOLS explicitly warns the model about.
        """
        if node_id not in self.nodes:
            raise ValueError("Graph node was not found")
        bounded_depth = max(1, min(depth, 4))

        def build() -> dict[str, Any]:
            """Builds the (uncached) dependency-impact BFS result; invoked once by `_cached` on a cache miss."""
            visited = {node_id}
            queue: deque[tuple[str, int]] = deque([(node_id, 0)])
            impacts: list[dict[str, Any]] = []
            while queue and len(impacts) < 100:
                current, current_depth = queue.popleft()
                if current_depth >= bounded_depth:
                    continue
                for edge in self.incoming[current]:
                    if edge.kind not in {
                        EdgeKind.IMPORTS,
                        EdgeKind.MAY_CALL,
                        EdgeKind.INSTANTIATES,
                    }:
                        continue
                    dependent = edge.source
                    if dependent in visited:
                        continue
                    visited.add(dependent)
                    queue.append((dependent, current_depth + 1))
                    impacts.append(
                        {
                            "depth": current_depth + 1,
                            "relationship": edge.kind.value,
                            "status": edge.evidence.status.value,
                            "dependent": self._public_node(self.nodes[dependent]),
                        }
                    )
            impacted_nodes = [self.nodes[item["dependent"]["node_id"]] for item in impacts[:12]]
            return {
                "target": self._public_node(self.nodes[node_id]),
                "depth": bounded_depth,
                "dependents": impacts,
                "evidence": [
                    item
                    for item in (
                        self._evidence(candidate, 50)
                        for candidate in [self.nodes[node_id], *impacted_nodes]
                    )
                    if item
                ],
            }

        return self._cached("dependency_impact", {"node_id": node_id, "depth": bounded_depth}, build)

    def project_configuration(self) -> dict[str, Any]:
        """Collects manifest, build, env-template, and container configuration files and detects framework signals from their contents.

        Input: none (scans `self.session.source_paths` for names matching
        `_MANIFEST_NAMES`, `.env*`/`dockerfile*` prefixes, or a `.gradle` suffix,
        capped at 30 files). Exposed as the `get_project_configuration` agent tool
        (dispatched from `RepositoryAgentService._execute_tool`); used for "how is
        this project configured / built / containerized" questions, complementing
        `repository_overview`'s narrower manifest sampling with a fuller configuration
        sweep.
        """
        def build() -> dict[str, Any]:
            """Builds the (uncached) project-configuration payload; invoked once by `_cached` on a cache miss."""
            paths = sorted(
                path
                for path in self.session.source_paths
                if Path(path).name.lower() in _MANIFEST_NAMES
                or Path(path).name.lower().startswith((".env", "dockerfile"))
                or Path(path).suffix.lower() in {".gradle"}
            )[:30]
            evidence = [item for item in (self._path_evidence(path, 180) for path in paths) if item]
            combined = "\n".join(str(item["excerpt"]).lower() for item in evidence)
            frameworks = sorted(
                framework
                for framework, signals in _FRAMEWORK_SIGNALS.items()
                if any(signal in combined for signal in signals)
            )
            return {"files": paths, "framework_signals": frameworks, "evidence": evidence}

        return self._cached("project_configuration", {}, build)

    def diagnostics(self, limit: int = 20) -> dict[str, Any]:
        """Reports analysis-time parse diagnostics and unresolved import/call references, with source evidence for a sample of the unresolved ones.

        Input: `limit` bounds how many diagnostics/unresolved-reference samples are
        returned, clamped to [1, 50]. Exposed as the `get_analysis_diagnostics` agent
        tool (dispatched from `RepositoryAgentService._execute_tool`); lets the agent
        explain analysis gaps (e.g. "this reference could not be resolved") honestly
        instead of guessing, and surfaces `self.report.stats` for overall analysis
        health.
        """
        bounded = max(1, min(limit, 50))

        def build() -> dict[str, Any]:
            """Builds the (uncached) diagnostics payload; invoked once by `_cached` on a cache miss."""
            diagnostic_counts = Counter(item.code for item in self.report.diagnostics)
            unresolved_counts = Counter(
                item.reference_kind for item in self.report.unresolved_references
            )
            unresolved = self.report.unresolved_references[:bounded]
            evidence: list[dict[str, Any]] = []
            for reference in unresolved[:10]:
                try:
                    source = self.index.read(
                        reference.evidence.span.path,
                        reference.evidence.span.start_line,
                        min(reference.evidence.span.end_line, reference.evidence.span.start_line + 20),
                    )
                except ValueError:
                    continue
                evidence.append(
                    {
                        "path": source["path"],
                        "start_line": source["start_line"],
                        "end_line": source["end_line"],
                        "excerpt": source["content"],
                    }
                )
            return {
                "analysis_stats": self.report.stats.model_dump(mode="json"),
                "diagnostic_counts": dict(diagnostic_counts),
                "diagnostics": [item.model_dump(mode="json") for item in self.report.diagnostics[:bounded]],
                "unresolved_counts": dict(unresolved_counts),
                "unresolved_samples": [item.model_dump(mode="json") for item in unresolved],
                "evidence": evidence,
            }

        return self._cached("diagnostics", {"limit": bounded}, build)
