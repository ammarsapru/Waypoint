from __future__ import annotations

import logging
import os
import json
import sqlite3
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path

from backend.app.config import settings
from backend.app.indexing import repository_snapshot

from backend.app.graph.models import (
    AnalysisReport,
    GraphEdge,
    GraphNeighborhood,
    GraphNode,
    GraphSummary,
)
from backend.app.observability import log_event, traced

logger = logging.getLogger(__name__)

_CONTEXT_EXTENSIONS = {
    ".bash",
    ".bat",
    ".cfg",
    ".cmd",
    ".conf",
    ".csv",
    ".css",
    ".gradle",
    ".json",
    ".ini",
    ".kts",
    ".md",
    ".mdx",
    ".properties",
    ".ps1",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".xsd",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
    ".zsh",
}
_CONTEXT_NAMES = {
    ".env.example",
    "dockerfile",
    "gradlew",
    "justfile",
    "makefile",
    "mvnw",
}
_SKIPPED_CONTEXT_DIRECTORIES = {
    ".codex",
    ".git",
    ".hg",
    ".venv",
    ".waypoint-clones",
    ".waypoint-data",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}


def _discover_context_paths(root: Path, limit: int = 500) -> set[str]:
    """Collects relative paths of non-code "context" files (docs, config, etc.) under a repository root.

    Input: an absolute repository root Path and a cap on how many paths to
    return (default 500); walks the tree while skipping VCS/build/venv
    directories and symlinks, keeping only files whose extension is in
    _CONTEXT_EXTENSIONS or whose lowercased name is in _CONTEXT_NAMES.
    Not currently called elsewhere in the codebase — context-path discovery for
    AnalysisSessionStore is instead delegated to
    backend.app.indexing.repository_snapshot (see `_make_session` below), so
    this helper is retained as a narrower, self-contained utility rather than
    wired into the active session pipeline.
    """
    discovered: set[str] = set()
    resolved_root = root.resolve()
    for current_root, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in _SKIPPED_CONTEXT_DIRECTORIES
            and not (Path(current_root) / directory).is_symlink()
        )
        for filename in sorted(files):
            if len(discovered) >= limit:
                return discovered
            candidate = Path(current_root) / filename
            if candidate.is_symlink():
                continue
            if (
                candidate.suffix.lower() not in _CONTEXT_EXTENSIONS
                and candidate.name.lower() not in _CONTEXT_NAMES
            ):
                continue
            try:
                candidate.resolve().relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            discovered.add(candidate.relative_to(root).as_posix())
    return discovered


@dataclass(frozen=True, slots=True)
class AnalysisSession:
    """An immutable, in-memory record of one stored repository analysis.

    Bundles the analysis `id`, the repository `root` it was run against, the
    full `report` (AnalysisReport graph), the `source_paths` snapshot used to
    detect later repository changes, and a `revision_fingerprint` for that
    snapshot. Created by AnalysisSessionStore._make_session and handed back by
    `.create()`/`.get()`/`.refresh()`; consumed throughout backend/app/api,
    backend/app/agent, and backend/app/mcp_server.py wherever code needs to
    query or re-check a previously analyzed repository.
    """

    id: str
    root: Path
    report: AnalysisReport
    source_paths: frozenset[str]
    revision_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Lightweight, listing-only view of one stored AnalysisSession.

    Carries just the fields a "recent analyses" picker needs (id, root,
    repository name, and the three headline stats) without the full
    AnalysisReport - which can be tens of megabytes of node/edge JSON for a
    large repository. Returned by AnalysisSessionStore.list_recent_summaries
    instead of a full AnalysisSession specifically so that listing never has
    to fully Pydantic-validate every stored report just to show five small
    numbers. Consumed by backend/app/api/routes.py:list_analyses and
    backend/app/mcp_server.py:list_recent_analyses.
    """

    id: str
    root: Path
    repository_name: str
    files_parsed: int
    node_count: int
    edge_count: int
    revision_fingerprint: str = ""


class AnalysisSessionStore:
    """Thread-safe bounded cache backed by restart-safe SQLite sessions."""

    def __init__(self, max_sessions: int = 50) -> None:
        """Initializes an empty in-memory LRU session cache with a `max_sessions` cap and a reentrant lock for thread safety."""
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, AnalysisSession] = OrderedDict()
        self._lock = threading.RLock()
        self._local = threading.local()

    def _connect(self) -> sqlite3.Connection:
        """Return this thread's cached SQLite connection, opening (and migrating) it on
        first use.

        Kept per-thread (via `threading.local`) instead of reopened on every call: FastAPI
        dispatches these synchronous store methods across a small pool of worker threads,
        so a thread-local connection is never touched by more than one thread at a time,
        while still being reused across many requests handled by that thread - unlike a
        fresh open-then-close per call, which forces SQLite to checkpoint its WAL (fold
        pending writes back into the main file) on every single call, since that
        connection was the last one open. Against `settings.state_path` - a file shared by
        every store in the app and, over a long dev session, potentially large - that
        checkpoint was costing seconds per request. The whole method runs under
        `self._lock` (not just the in-memory dicts) so two threads racing to open the
        first-ever connection can't both run the `ALTER TABLE` migration below at once.
        Called at the start of every method that touches SQLite; see `close()` for the
        matching teardown a test needs before deleting the underlying file.
        """
        with self._lock:
            connection = getattr(self._local, "connection", None)
            if connection is not None:
                return connection
            settings.state_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(settings.state_path, timeout=10)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_sessions (
                    id TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    source_paths_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    revision_fingerprint TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(analysis_sessions)"
                ).fetchall()
            }
            if "revision_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE analysis_sessions ADD COLUMN "
                    "revision_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            for column, ddl_type in (
                ("repository_name", "TEXT"),
                ("files_parsed", "INTEGER"),
                ("node_count", "INTEGER"),
                ("edge_count", "INTEGER"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE analysis_sessions ADD COLUMN {column} {ddl_type}"
                    )
            self._local.connection = connection
            return connection

    def close(self) -> None:
        """Close and discard this thread's cached connection, if any.

        The process never needs to call this - a persistent connection is meant to
        outlive requests. It exists for tests that construct a store against a
        temporary directory and then delete that directory immediately afterward: on
        Windows, deleting a file while this store still holds it open raises
        PermissionError, so those tests must close the store first.
        """
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def _persist(self, session: AnalysisSession) -> None:
        """Writes (or upserts) a session's report/state to the SQLite state file and trims the table to `max_sessions` rows.

        Input: an AnalysisSession. Creates the `analysis_sessions` table (and
        adds the `revision_fingerprint` and summary columns via ALTER TABLE if
        missing, for backward compatibility with older state files) if needed,
        then inserts/replaces the row and deletes any rows beyond the
        `max_sessions` most-recently-updated ones. Called by `create()` and
        `refresh()` so that sessions survive process restarts, not just the
        in-memory OrderedDict. Also writes `repository_name`/`files_parsed`/
        `node_count`/`edge_count` as their own columns (cheap - the values are
        already in memory here) so `list_recent_summaries()` never has to
        parse `report_json` just to answer a listing query.
        """
        connection = self._connect()
        # `with connection:` (not `connection.commit()`) so a mid-write exception rolls
        # back instead of leaving this persistent connection stuck holding a write lock
        # that would block every other store sharing this SQLite file.
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO analysis_sessions("
                "id, root, report_json, source_paths_json, updated_at, "
                "revision_fingerprint, repository_name, files_parsed, "
                "node_count, edge_count"
                ") VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    str(session.root),
                    session.report.model_dump_json(),
                    json.dumps(sorted(session.source_paths)),
                    session.revision_fingerprint,
                    session.report.repository_name,
                    session.report.stats.files_parsed,
                    session.report.stats.node_count,
                    session.report.stats.edge_count,
                ),
            )
            rows = connection.execute(
                "SELECT id FROM analysis_sessions ORDER BY updated_at DESC"
            ).fetchall()
            for (identifier,) in rows[self.max_sessions :]:
                connection.execute(
                    "DELETE FROM analysis_sessions WHERE id = ?", (identifier,)
                )

    def _restore(self, analysis_id: str) -> AnalysisSession | None:
        """Reloads a persisted session from the SQLite state file by id, or returns None if it can't be found/validated.

        Input: the session's `analysis_id` string. Falls back to a legacy
        SELECT (without `revision_fingerprint`) if that column doesn't exist yet
        in an older state file, and returns None if the row is missing, its
        stored `root` now falls outside `settings.allowed_root` or no longer
        exists, or the stored JSON fails to validate. Called by `get()` on a
        cache miss and by `list_recent()`, so an out-of-process/restarted
        server can still serve analyses that were computed before it restarted.
        """
        if not settings.state_path.is_file():
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT root, report_json, source_paths_json, "
                "revision_fingerprint "
                "FROM analysis_sessions WHERE id = ?",
                (analysis_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            try:
                legacy = connection.execute(
                    "SELECT root, report_json, source_paths_json "
                    "FROM analysis_sessions WHERE id = ?",
                    (analysis_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
            row = (*legacy, "") if legacy is not None else None
        if row is None:
            return None
        root = Path(row[0]).resolve()
        try:
            root.relative_to(settings.allowed_root)
        except ValueError:
            return None
        if not root.is_dir():
            return None
        try:
            report = AnalysisReport.model_validate_json(row[1])
            source_paths = frozenset(json.loads(row[2]))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        return AnalysisSession(
            id=analysis_id,
            root=root,
            report=report,
            source_paths=source_paths,
            revision_fingerprint=str(row[3] or ""),
        )

    def _make_session(
        self,
        analysis_id: str,
        root: Path,
        report: AnalysisReport,
    ) -> AnalysisSession:
        """Builds an AnalysisSession by pairing a freshly analyzed report with a repository_snapshot of its current files.

        Input: the `analysis_id` to assign, the repository `root`, and the
        AnalysisReport just produced by RepositoryAnalyzer.analyze. Stamps the
        report's `analysis_id` field via `model_copy` and takes a
        backend.app.indexing.repository_snapshot (bounded by
        `settings.max_repository_files`) to capture the file set/fingerprint
        used later to detect repository changes. Called by `create()` and
        `refresh()`; centralizes session construction so both paths stay
        consistent.
        """
        snapshot = repository_snapshot(
            root, int(getattr(settings, "max_repository_files", 5_000))
        )
        return AnalysisSession(
            id=analysis_id,
            root=root.resolve(),
            report=report.model_copy(update={"analysis_id": analysis_id}),
            source_paths=snapshot.paths,
            revision_fingerprint=snapshot.fingerprint,
        )

    @traced("graph.session.create")
    def create(self, root: Path, report: AnalysisReport) -> AnalysisReport:
        """Stores a new analysis under a fresh id, evicting the oldest session if the cache is full.

        Input: the repository `root` and the AnalysisReport produced by
        RepositoryAnalyzer.analyze. Generates a new uuid4-based analysis id,
        builds and caches (in-memory + SQLite via `_persist`) the corresponding
        AnalysisSession, and logs how many source files were parsed vs. left as
        unparsed "context" files. Called by backend/app/api/routes.py:_analyze
        and backend/app/mcp_server.py whenever a new repository scan completes;
        this is what turns a one-off analysis into a durable, queryable session
        with a stable id clients can reference later.
        """
        analysis_id = uuid.uuid4().hex
        session = self._make_session(analysis_id, root, report)
        stored_report = session.report
        parsed_source_paths = {
            node.span.path
            for node in stored_report.nodes
            if node.kind.value == "module" and node.span is not None
        }
        context_paths = set(session.source_paths) - parsed_source_paths
        evicted: list[str] = []
        with self._lock:
            self._sessions[analysis_id] = session
            self._sessions.move_to_end(analysis_id)
            while len(self._sessions) > self.max_sessions:
                removed_id, _ = self._sessions.popitem(last=False)
                evicted.append(removed_id)
        self._persist(session)
        log_event(
            logger,
            logging.INFO,
            "graph.session_created",
            "Analysis session stored for interactive navigation",
            analysis_id=analysis_id,
            repository_root=root,
            node_count=stored_report.stats.node_count,
            edge_count=stored_report.stats.edge_count,
            source_file_count=len(session.source_paths),
            parsed_source_file_count=len(parsed_source_paths),
            context_file_count=len(context_paths),
            evicted_sessions=evicted,
        )
        return stored_report

    @traced("graph.session.refresh")
    def refresh(
        self,
        analysis_id: str,
        root: Path,
        report: AnalysisReport,
    ) -> AnalysisSession:
        """Replace an analysis atomically while preserving its public identifier.

        Input: an existing `analysis_id`, the repository `root`, and a newly
        computed AnalysisReport. Rebuilds the AnalysisSession (new report +
        fresh repository_snapshot) and overwrites the cached/persisted entry for
        that same id, so any client still holding the id transparently sees the
        updated graph. Called by backend/app/agent/retrieval.py's
        RepositoryIndexCache.get/.rebuild after detecting that a repository's
        on-disk content changed since it was last analyzed, keeping the agent's
        graph and retrieval index in sync with the working tree without forcing
        the caller to mint a brand-new analysis id.
        """
        session = self._make_session(analysis_id, root, report)
        with self._lock:
            self._sessions[analysis_id] = session
            self._sessions.move_to_end(analysis_id)
        self._persist(session)
        log_event(
            logger,
            logging.INFO,
            "graph.session_refreshed",
            "Analysis session refreshed after repository content changed",
            analysis_id=analysis_id,
            revision_fingerprint=session.revision_fingerprint,
            node_count=session.report.stats.node_count,
            edge_count=session.report.stats.edge_count,
            source_file_count=len(session.source_paths),
        )
        return session

    @traced("graph.session.get")
    def get(self, analysis_id: str) -> AnalysisSession:
        """Fetches a session by id from the in-memory cache, falling back to SQLite restore, or raises KeyError if unknown.

        Input: an `analysis_id` string. On a cache hit, marks the session as
        most-recently-used; on a miss, calls `_restore()` and re-populates the
        cache (evicting the least-recently-used entry if now over
        `max_sessions`), raising `KeyError(analysis_id)` if it cannot be
        restored either. Called throughout backend/app/api/routes.py (e.g.
        `_session`), backend/app/mcp_server.py, and backend/app/agent/retrieval.py
        wherever an endpoint or tool needs the AnalysisSession behind an id the
        client supplied.
        """
        with self._lock:
            session = self._sessions.get(analysis_id)
            if session is None:
                session = self._restore(analysis_id)
                if session is None:
                    raise KeyError(analysis_id)
                self._sessions[analysis_id] = session
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
            self._sessions.move_to_end(analysis_id)
            return session

    @traced("graph.session.list")
    def list_recent(self) -> list[AnalysisSession]:
        """Return restart-safe analysis sessions, newest first.

        No inputs beyond `self`. If no SQLite state file exists yet, falls back
        to the in-memory cache reversed (most-recently-used first); otherwise
        reads up to `max_sessions` ids ordered by `updated_at DESC` from SQLite
        and restores each via `_restore()`, skipping any that no longer
        validate. Called by backend/app/api/routes.py and
        backend/app/mcp_server.py to power a "recent analyses" listing so users
        can resume a previous session without re-scanning the repository.
        """
        if not settings.state_path.is_file():
            with self._lock:
                return list(reversed(self._sessions.values()))
        connection = self._connect()
        try:
            identifiers = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM analysis_sessions ORDER BY updated_at DESC LIMIT ?",
                    (self.max_sessions,),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            return []
        sessions: list[AnalysisSession] = []
        for identifier in identifiers:
            restored = self._restore(identifier)
            if restored is not None:
                sessions.append(restored)
        return sessions

    @traced("graph.session.list_summaries")
    def list_recent_summaries(self) -> list[SessionSummary]:
        """Return lightweight recent-session summaries, newest first, without parsing full reports.

        No inputs beyond `self`. Same ordering/bounding as `list_recent()`, but
        reads `repository_name`/`files_parsed`/`node_count`/`edge_count`
        straight from their own SQLite columns instead of restoring and
        Pydantic-validating every stored `report_json` - the fix for
        `list_recent()`/`_restore()` being unusably slow once sessions
        accumulate (each report can be tens of megabytes for a large
        repository). Rows persisted before these columns existed (NULL values)
        fall back to one bounded `_restore()` call each, so old state files
        still work correctly, just without the speedup for those specific
        stragglers. Called by backend/app/api/routes.py:list_analyses and
        backend/app/mcp_server.py:list_recent_analyses to power the "recent
        analyses" picker.
        """
        if not settings.state_path.is_file():
            with self._lock:
                return [
                    SessionSummary(
                        id=session.id,
                        root=session.root,
                        repository_name=session.report.repository_name,
                        files_parsed=session.report.stats.files_parsed,
                        node_count=session.report.stats.node_count,
                        edge_count=session.report.stats.edge_count,
                        revision_fingerprint=session.revision_fingerprint,
                    )
                    for session in reversed(self._sessions.values())
                ]
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, root, revision_fingerprint, repository_name, "
                "files_parsed, node_count, edge_count FROM analysis_sessions "
                "ORDER BY updated_at DESC LIMIT ?",
                (self.max_sessions,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        summaries: list[SessionSummary] = []
        for identifier, root_text, fingerprint, name, files_parsed, node_count, edge_count in rows:
            root = Path(str(root_text)).resolve()
            try:
                root.relative_to(settings.allowed_root)
            except ValueError:
                continue
            if not root.is_dir():
                continue
            if name is None:
                restored = self._restore(str(identifier))
                if restored is None:
                    continue
                summaries.append(SessionSummary(
                    id=str(identifier),
                    root=restored.root,
                    repository_name=restored.report.repository_name,
                    files_parsed=restored.report.stats.files_parsed,
                    node_count=restored.report.stats.node_count,
                    edge_count=restored.report.stats.edge_count,
                    revision_fingerprint=restored.revision_fingerprint,
                ))
                continue
            summaries.append(SessionSummary(
                id=str(identifier),
                root=root,
                repository_name=str(name),
                files_parsed=int(files_parsed or 0),
                node_count=int(node_count or 0),
                edge_count=int(edge_count or 0),
                revision_fingerprint=str(fingerprint or ""),
            ))
        return summaries


class GraphQueryService:
    """Read-only query layer over one AnalysisReport's nodes/edges (neighborhoods, summaries, overviews).

    Wraps a single AnalysisReport with id-indexed node lookups so callers don't
    need to re-scan the full node/edge lists for every query. Instantiated
    per-request in backend/app/api/routes.py and backend/app/agent/retrieval.py
    (e.g. `GraphQueryService(session.report)`) to answer graph API/tool calls
    without mutating or persisting anything.
    """

    def __init__(self, report: AnalysisReport) -> None:
        """Indexes a report's nodes by id for fast lookup and keeps its edge list for traversal."""
        self.report = report
        self.nodes = {node.id: node for node in report.nodes}
        self.edges = report.edges

    @traced("graph.query.neighborhood")
    def neighborhood(self, node_id: str, depth: int) -> GraphNeighborhood:
        """Breadth-first-searches the undirected graph out to `depth` hops from `node_id` and returns that induced subgraph.

        Input: a `node_id` that must exist in `self.nodes` (raises `KeyError`
        otherwise) and a non-negative `depth`. Builds an undirected adjacency
        map from all edges, then BFS-expands the visited set hop by hop, and
        returns every node reached plus every edge whose both endpoints were
        reached. Called by backend/app/api/routes.py's neighborhood endpoint and
        backend/app/agent/retrieval.py wherever the agent needs a bounded local
        view around a specific node instead of the entire (possibly huge) graph.
        """
        if node_id not in self.nodes:
            raise KeyError(node_id)
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.source, set()).add(edge.target)
            adjacency.setdefault(edge.target, set()).add(edge.source)
        visited = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        while queue:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, current_depth + 1))
        selected_edges = [
            edge
            for edge in self.edges
            if edge.source in visited and edge.target in visited
        ]
        return GraphNeighborhood(
            center_node_id=node_id,
            depth=depth,
            nodes=[self.nodes[item] for item in visited],
            edges=selected_edges,
        )

    @traced("graph.query.summary")
    def summary(self) -> GraphSummary:
        """Computes aggregate node/edge/evidence counts and the top-10 highest-degree nodes for the whole report.

        No inputs beyond `self`. Tallies node counts by NodeKind, edge counts by
        EdgeKind, and reference counts by EvidenceStatus in a single pass, plus
        each node's degree (in+out edge count), then ranks nodes by degree
        descending (id ascending as a tiebreak) and keeps the top 10. Called by
        backend/app/api/routes.py's `/summary` endpoint and
        backend/app/mcp_server.py to give a quick, high-level view of a
        repository's graph without shipping the full node/edge lists.
        """
        node_counts: dict[str, int] = {}
        edge_counts: dict[str, int] = {}
        evidence_counts: dict[str, int] = {}
        degree: dict[str, int] = {node_id: 0 for node_id in self.nodes}
        for node in self.nodes.values():
            node_counts[node.kind.value] = node_counts.get(node.kind.value, 0) + 1
        for edge in self.edges:
            edge_counts[edge.kind.value] = edge_counts.get(edge.kind.value, 0) + 1
            status = edge.evidence.status.value
            evidence_counts[status] = evidence_counts.get(status, 0) + 1
            degree[edge.source] = degree.get(edge.source, 0) + 1
            degree[edge.target] = degree.get(edge.target, 0) + 1
        ranked = sorted(degree.items(), key=lambda item: (-item[1], item[0]))[:10]
        return GraphSummary(
            analysis_id=self.report.analysis_id or "",
            node_counts=node_counts,
            edge_counts=edge_counts,
            evidence_counts=evidence_counts,
            top_connected_nodes=[
                {
                    "node_id": node_id,
                    "qualified_name": self.nodes[node_id].qualified_name,
                    "kind": self.nodes[node_id].kind.value,
                    "connections": connections,
                }
                for node_id, connections in ranked
            ],
        )

    @traced("graph.query.overview")
    def overview(self, full_graph_threshold: int = 4_000) -> AnalysisReport:
        """Return a browser-safe initial graph while retaining the full session.

        Input: an optional `full_graph_threshold` node-count cutoff (default
        4,000). Below the threshold, returns the full report unchanged (view
        "full"); above it, projects down to just repository/module nodes and
        their CONTAINS/IMPORTS edges plus import-kind unresolved references
        (view "overview"), leaving the underlying stored AnalysisSession intact
        for later drill-down. Called by backend/app/api/routes.py:_analyze and
        the `/analysis/{id}/overview` endpoint so the frontend graph view never
        has to render an enormous graph on first load.
        """
        if len(self.report.nodes) <= full_graph_threshold:
            return self.report.model_copy(update={"view": "full"})
        selected_ids = {
            node.id
            for node in self.report.nodes
            if node.kind.value in {"repository", "module"}
        }
        overview_edges = [
            edge
            for edge in self.edges
            if edge.source in selected_ids and edge.target in selected_ids
        ]
        overview = self.report.model_copy(
            update={
                "view": "overview",
                "nodes": [
                    node for node in self.report.nodes if node.id in selected_ids
                ],
                "edges": overview_edges,
                "unresolved_references": [
                    reference
                    for reference in self.report.unresolved_references
                    if reference.reference_kind == "import"
                ],
            }
        )
        log_event(
            logger,
            logging.INFO,
            "graph.overview_created",
            "Large graph projected to module-level browser overview",
            analysis_id=self.report.analysis_id,
            full_node_count=len(self.report.nodes),
            returned_node_count=len(overview.nodes),
            full_edge_count=len(self.report.edges),
            returned_edge_count=len(overview.edges),
            threshold=full_graph_threshold,
        )
        return overview


analysis_sessions = AnalysisSessionStore()
