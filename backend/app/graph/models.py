from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeKind(str, Enum):
    """Enumerates the kinds of entity a GraphNode can represent.

    Inheriting from both `str` and `Enum` means members compare and serialize as
    plain strings (e.g. `NodeKind.CLASS == "class"`), so Pydantic, SQLite JSON
    storage, and the frontend can all treat a node's kind as an ordinary string.
    Used everywhere a GraphNode is built in analyzer.py/polyglot.py, and by
    store.py's GraphQueryService when grouping/filtering nodes by kind; keeping
    this a closed enum stops typos from silently creating unrecognized kinds.
    """

    REPOSITORY = "repository"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class EdgeKind(str, Enum):
    """Enumerates the kinds of relationship a GraphEdge can represent.

    CONTAINS is lexical containment (module contains class contains method),
    IMPORTS is a module-level import, MAY_CALL is an inferred call edge, and
    INSTANTIATES is class construction. Built wherever a GraphEdge is created in
    analyzer.py/polyglot.py and read back by store.py's GraphQueryService to
    bucket edges by kind for the graph summary and neighborhood views.
    """

    CONTAINS = "contains"
    IMPORTS = "imports"
    MAY_CALL = "may_call"
    INSTANTIATES = "instantiates"


class EvidenceStatus(str, Enum):
    """Enumerates how confidently a graph relationship was established.

    VERIFIED means the fact is syntactic (e.g. AST/tree-sitter lexical
    containment), INFERRED means it was resolved by heuristic (e.g. a call
    target guessed from imports/types), and UNRESOLVED means no target could be
    established without guessing. Attached to every Evidence record so
    GraphQueryService.summary() and the frontend can report a confidence
    breakdown instead of treating all edges as equally certain.
    """

    VERIFIED = "verified"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


class SourceSpan(BaseModel):
    """An immutable, 1-indexed line/column range locating something in a source file.

    Built by analyzer.py's `_span` (from Python AST node positions) and
    polyglot.py's `_span`/`_offset_span` (from tree-sitter node positions or raw
    byte offsets), then attached to nearly every GraphNode and Evidence record.
    `model_config = ConfigDict(frozen=True)` makes instances immutable and
    hashable; to change a value you must derive a new instance, e.g.
    `updated = span.model_copy(update={"start_line": 5})`, rather than mutating
    in place.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    start_line: int = Field(ge=1)
    start_column: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=0)


class Evidence(BaseModel):
    """An immutable provenance record explaining why a graph relationship exists.

    Bundles the resolution `status`, the `span` it was found at, the raw
    `syntax` text, a human-readable `resolution` explanation, and a `confidence`
    score. Constructed throughout analyzer.py/polyglot.py every time a
    GraphEdge or UnresolvedReference is created; the frontend and
    GraphQueryService use `status`/`confidence` to visually distinguish
    verified, inferred, and unresolved relationships. Frozen like SourceSpan, so
    use `model_copy(update={...})` to derive a modified copy.
    """

    model_config = ConfigDict(frozen=True)

    status: EvidenceStatus
    span: SourceSpan
    syntax: str
    resolution: str
    confidence: float = Field(ge=0.0, le=1.0)  # ge/le bound confidence to the inclusive range 0.0-1.0


class GraphNode(BaseModel):
    """One entity in the code graph: a repository, module, class, function, or method.

    `id` is a stable, content-derived identifier (see `_stable_id` in
    analyzer.py/polyglot.py) so the same definition produces the same id across
    re-analysis runs; `metadata` is a free-form dict (e.g.
    `{"language": "python"}`). Constructed by DefinitionVisitor/RelationshipVisitor
    in analyzer.py and PolyglotGraphBuilder in polyglot.py, then consumed by
    store.py's GraphQueryService and serialized to the frontend graph view.
    Frozen so nodes are safe to share/cache across analysis sessions.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: NodeKind
    name: str
    qualified_name: str
    module: str | None = None
    span: SourceSpan | None = None
    signature: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """One directed relationship between two GraphNode ids, with its supporting Evidence.

    `source`/`target` are GraphNode ids and `kind` is an EdgeKind (contains,
    imports, may_call, instantiates). Built by both analyzer.py and polyglot.py
    whenever a relationship is discovered, and traversed by store.py's
    GraphQueryService.neighborhood()/summary() to answer graph queries served to
    the API, MCP server, and agent retrieval tools.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    source: str
    target: str
    kind: EdgeKind
    evidence: Evidence
    metadata: dict[str, Any] = Field(default_factory=dict)


class UnresolvedReference(BaseModel):
    """An import/call/instantiation that could not be matched to a specific internal GraphNode.

    `source` is the originating node's id, `reference` is the raw name/text
    referenced, and `reference_kind` is one of "import"/"call"/"instantiation".
    Appended to GraphAccumulator.unresolved (bounded) throughout
    analyzer.py/polyglot.py and surfaced via AnalysisReport.unresolved_references
    so callers can see external dependencies or references the analyzer could
    not statically resolve without guessing.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    reference: str
    reference_kind: str
    evidence: Evidence
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnalysisDiagnostic(BaseModel):
    """A single warning/error/info message produced while analyzing a repository.

    Examples include a file exceeding the size limit, a syntax error, or a
    duplicate module name. Collected into GraphAccumulator.diagnostics
    throughout analyzer.py/polyglot.py and returned as part of
    AnalysisReport.diagnostics so callers can see what was skipped or degraded
    during a scan instead of failing the whole analysis silently.
    """

    model_config = ConfigDict(frozen=True)

    severity: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None


class AnalysisStats(BaseModel):
    """Aggregate counters summarizing one repository analysis run.

    Covers files discovered/parsed/skipped, parse failures, node/edge/unresolved
    counts, and wall-clock duration. Assembled once at the end of
    RepositoryAnalyzer.analyze (backend/app/graph/analyzer.py) and returned as
    AnalysisReport.stats; used by the API and UI to show scan health/progress
    without walking the full graph.
    """

    files_discovered: int
    files_parsed: int
    files_skipped: int
    parse_failures: int
    node_count: int
    edge_count: int
    unresolved_count: int
    duration_ms: float


class AnalysisReport(BaseModel):
    """The top-level result of analyzing a repository: its full code graph plus metadata.

    Holds all `nodes`, `edges`, `unresolved_references`, `diagnostics`, and
    `stats`, along with a `view` marker ("full" vs "overview") and an optional
    `analysis_id` once persisted. Returned by RepositoryAnalyzer.analyze,
    stored/restored by store.py's AnalysisSessionStore, and serialized to the
    frontend, HTTP API (backend/app/api/routes.py), and MCP server as the single
    object describing a repository's code graph.
    """

    analysis_id: str | None = None
    view: str = "full"
    repository_root: str
    repository_name: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    unresolved_references: list[UnresolvedReference]
    diagnostics: list[AnalysisDiagnostic]
    stats: AnalysisStats


class GraphNeighborhood(BaseModel):
    """The subgraph of nodes and edges within a given hop `depth` of `center_node_id`.

    Built and returned by store.py's GraphQueryService.neighborhood(), and used
    by the API's neighborhood endpoint and the agent's retrieval tools to fetch a
    bounded, relevant slice of a potentially very large graph instead of the
    entire AnalysisReport.
    """

    center_node_id: str
    depth: int = Field(ge=0)
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class GraphSummary(BaseModel):
    """Aggregate graph statistics for one analysis: counts by kind plus the most-connected nodes.

    Includes counts of nodes by NodeKind, edges by EdgeKind, references by
    EvidenceStatus, and the top-10 highest-degree nodes. Built by store.py's
    GraphQueryService.summary() and returned by the API's `/summary` endpoint and
    the MCP server for a quick, high-level view of a repository's graph without
    transferring the full node/edge lists.
    """

    analysis_id: str
    node_counts: dict[str, int]
    edge_counts: dict[str, int]
    evidence_counts: dict[str, int]
    top_connected_nodes: list[dict[str, str | int]]
