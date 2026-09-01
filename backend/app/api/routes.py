from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from backend.app.agent.issues import RepositoryIssueAgent, issue_investigations
from backend.app.agent.agentic_comparison import AgenticRetrievalComparisonService
from backend.app.agent.comparison import ModelComparisonService
from backend.app.agent.memory import conversations
from backend.app.agent.onboarding import RepositoryOnboardingAgent
from backend.app.agent.retrieval import repository_indexes
from backend.app.agent.service import (
    AgentExecutionError,
    AgentUnavailableError,
    RepositoryAgentService,
)
from backend.app.agent.usage_ledger import model_usage_ledger
from backend.app.config import settings
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.models import AnalysisReport, GraphNeighborhood, GraphSummary
from backend.app.graph.store import AnalysisSession, GraphQueryService, analysis_sessions
from backend.app.issues.models import IssueTimeline, IssueWorkspaceReport, ProposedIssue
from backend.app.issues.service import GitHubIssueError, GitHubIssueService
from backend.app.observability import log_event, traced
from backend.app.onboarding.models import (
    ArchitectureReport,
    AgenticComparisonReport,
    AgenticComparisonRequest,
    ChallengeAnswer,
    ChallengeResult,
    CodeJourney,
    ConversationTranscript,
    ConversationTurn,
    ContributionMission,
    GroundedAnswer,
    GroundedQuestion,
    ModelComparisonReport,
    ModelComparisonRequest,
    RevisionReport,
    TourPlan,
    TourRequest,
    TourSummary,
)
from backend.app.onboarding.service import OnboardingService, tour_states
from backend.app.repository_import import (
    GitCloneError,
    GitHubRepositoryCloner,
    GitUnavailableError,
    InvalidGitHubRepository,
    RepositoryImportError,
    parse_github_repository,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


class AnalysisRequest(BaseModel):
    repository_path: str = Field(min_length=1, max_length=2048)


class GitHubAnalysisRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=2048)

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        """Rejects a GitHubAnalysisRequest whose repository_url is not a parseable GitHub URL.

        Runs automatically during pydantic validation of POST /api/v1/analysis/github
        request bodies, before analyze_github_repository ever executes; delegates the
        actual parsing/format check to parse_github_repository in
        backend/app/repository_import.py. Exists so malformed URLs fail fast with a
        pydantic 422 validation error instead of reaching the git clone step.
        """
        parse_github_repository(value)
        return value


class HealthResponse(BaseModel):
    status: str
    app: str
    allowed_root: str
    clone_root: str
    trace_functions: bool
    max_trace: bool


class UsageSummaryRow(BaseModel):
    label: str
    calls: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    estimated_cost_usd: float | None
    provider_reported_cost_usd: float | None
    analysis_id: str | None = None


class UsageSummaryResponse(BaseModel):
    by_workflow: list[UsageSummaryRow]
    by_provider: list[UsageSummaryRow]
    by_analysis: list[UsageSummaryRow]


class SourceDocument(BaseModel):
    analysis_id: str
    path: str
    language: str
    content: str
    line_count: int
    size_bytes: int
    canonical_path: str | None = None


class AnalysisSessionSummary(BaseModel):
    analysis_id: str
    repository_name: str
    repository_root: str
    files_parsed: int
    node_count: int
    edge_count: int


class RepositoryTreeResponse(BaseModel):
    analysis_id: str
    paths: list[str]
    total: int
    parsed_files: int
    context_files: int
    canonical_files: int
    alias_count: int
    aliases: list[dict[str, str]]


_SOURCE_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".md": "markdown",
    ".mdx": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
}


github_cloner = GitHubRepositoryCloner(
    clone_root=settings.clone_root,
    allowed_root=settings.allowed_root,
    timeout_seconds=settings.clone_timeout_seconds,
    max_clone_bytes=settings.max_clone_bytes,
    max_clone_files=settings.max_clone_files,
    max_retained_clones=settings.max_retained_clones,
)


def _source_language(path: Path) -> str:
    """Maps a file path's suffix to a display language name for the source viewer.

    Input: a filesystem Path; only its lowercased suffix is inspected against the
    _SOURCE_LANGUAGES table above, defaulting to "text" for unrecognized extensions.
    Used by _read_source to populate SourceDocument.language, and imported directly
    in backend/tests/test_polyglot_analyzer.py; backs the syntax highlighting shown
    when a user opens a file in the code viewer.
    """
    return _SOURCE_LANGUAGES.get(path.suffix.lower(), "text")


def _session(analysis_id: str) -> AnalysisSession:
    """Looks up a previously created AnalysisSession by id, or raises HTTP 404.

    Input: the analysis_id path parameter shared by nearly every route below.
    Delegates to analysis_sessions.get (backend/app/graph/store.py) and is called
    by almost every handler in this file to resolve that id before doing anything
    else. Centralizing the "session missing/expired" -> 404 translation here keeps
    each route handler free of repeated try/except boilerplate.
    """
    try:
        return analysis_sessions.get(analysis_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Analysis session was not found") from exc


def _read_source(
    session: AnalysisSession,
    source_path: str,
    canonical_path: str | None = None,
) -> SourceDocument:
    """Reads one indexed source file's text off disk, guarding against path traversal.

    Input: an AnalysisSession, the raw path the caller asked for, and an optional
    canonical/resolved path (as produced by the repository index) when it differs
    from the requested path. Rejects paths that are not in session.source_paths or
    that resolve outside session.root. Used by get_source (GET
    /api/v1/analyses/{analysis_id}/source) and directly unit-tested in
    backend/tests/test_questions.py; this is the guarded file read behind the
    "view this file" code viewer, so it must never escape the cloned/allowed repo root.
    """
    normalized = Path(source_path).as_posix().lstrip("/")
    canonical = Path(canonical_path or normalized).as_posix().lstrip("/")
    if canonical not in session.source_paths:
        raise HTTPException(status_code=404, detail="Source file was not found")
    candidate = (session.root / canonical).resolve()
    try:
        candidate.relative_to(session.root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Source file was not found") from exc
    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Source file could not be read") from exc
    return SourceDocument(
        analysis_id=session.id,
        path=normalized,
        language=_source_language(candidate),
        content=content,
        line_count=len(content.splitlines()),
        size_bytes=len(content.encode("utf-8")),
        canonical_path=canonical if canonical != normalized else None,
    )


def _analyze(root: Path, repository_name: str | None = None) -> AnalysisReport:
    """Runs static analysis on a repository, stores the result, and returns its overview.

    Input: a resolved filesystem path to a repository root (already validated or
    freshly cloned) and an optional display name override. Runs
    RepositoryAnalyzer().analyze, persists the report as a new AnalysisSession via
    analysis_sessions.create, warms the retrieval index via repository_indexes.get,
    then returns GraphQueryService(...).overview(). Executed inside a threadpool by
    analyze_repository and analyze_github_repository, since analysis is CPU/IO
    heavy; this is the core pipeline that turns a submitted path or GitHub URL into
    a usable analysis_id for every other endpoint in this file.
    """
    report = RepositoryAnalyzer().analyze(root)
    if repository_name:
        report = report.model_copy(update={"repository_name": repository_name})
    stored = analysis_sessions.create(root, report)
    session = analysis_sessions.get(stored.analysis_id or "")
    repository_indexes.get(session)
    return GraphQueryService(session.report).overview()


def _agent_error(exc: Exception) -> HTTPException:
    """Converts an agent-layer exception into the matching HTTPException.

    Input: any Exception raised by an AI-agent call, typically AgentUnavailableError
    or AgentExecutionError from backend/app/agent/service.py. Maps
    AgentUnavailableError (no ANTHROPIC_API_KEY configured) to 503, and everything
    else to 502 with the exception text as detail. Called by every route that
    invokes an LLM-backed agent (create_tour, answer_challenge, get_mission,
    answer_repository_question, compare_repository_answers, investigate_issues) so
    all AI-powered endpoints fail with a consistent status/response shape.
    """
    if isinstance(exc, AgentUnavailableError):
        return HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is required for AI-generated repository answers",
        )
    return HTTPException(status_code=502, detail=str(exc))


@router.get("/health", response_model=HealthResponse)
@traced("http.health")
async def health() -> HealthResponse:
    """Reports service liveness and the active configuration (roots, tracing flags).

    HTTP entry point: GET /api/v1/health, no request body or params. Reads directly
    from backend/app/config.py settings; used by verification/scripts/service_performance.py
    and ad-hoc readiness checks (e.g. deployment/CI probes) to confirm the backend
    process is up and to see which repository roots and tracing mode it is running with.
    """
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        allowed_root=str(settings.allowed_root),
        clone_root=str(settings.clone_root),
        trace_functions=settings.trace_functions,
        max_trace=settings.max_trace,
    )


@router.get("/usage/summary", response_model=UsageSummaryResponse)
@traced("http.usage_summary")
async def usage_summary() -> UsageSummaryResponse:
    """Reports aggregate token usage and estimated cost recorded since 2026-07-30,
    grouped by workflow, by provider:model, and by repository/analysis.

    HTTP entry point: GET /api/v1/usage/summary, no params. Reads
    `backend/app/agent/usage_ledger.py`'s `model_usage` SQLite table, which every
    workflow (Ask, onboarding tours/missions/assessments, issue investigation/sweep)
    writes one row to per model call via `record_model_response`. `estimated_cost_usd`
    is a best-effort figure (see `provider.py:estimate_model_cost_usd`), not an
    authoritative bill - Bedrock's API returns no cost figure at all. `by_analysis`
    joins against `analysis_sessions` for a human-readable `repository_name` per row,
    so testing across multiple repositories shows cost broken out per repository
    rather than only a pooled total.
    """
    by_workflow = [
        UsageSummaryRow(label=str(row["workflow"] or "unknown"), **{
            key: row[key] for key in row if key != "workflow"
        })
        for row in model_usage_ledger.summary_by_workflow()
    ]
    by_provider = [
        UsageSummaryRow(label=str(row["routed_provider"] or "unknown"), **{
            key: row[key] for key in row if key != "routed_provider"
        })
        for row in model_usage_ledger.summary_by_provider()
    ]
    by_analysis = [
        UsageSummaryRow(
            label=str(row["repository_name"] or f"(unnamed) {row['analysis_id']}"),
            analysis_id=row["analysis_id"],
            **{
                key: row[key]
                for key in row
                if key not in ("repository_name", "analysis_id")
            },
        )
        for row in model_usage_ledger.summary_by_analysis()
    ]
    return UsageSummaryResponse(
        by_workflow=by_workflow, by_provider=by_provider, by_analysis=by_analysis
    )


@router.post("/analysis", response_model=AnalysisReport)
@traced("http.analysis")
async def analyze_repository(request: AnalysisRequest) -> AnalysisReport:
    """Analyzes a local repository already present on disk and creates an analysis session.

    HTTP entry point: POST /api/v1/analysis with an AnalysisRequest body
    ({"repository_path": str}). Resolves the path (rejecting it with 403 if it
    escapes settings.allowed_root) and delegates to _analyze in a threadpool,
    which itself uses RepositoryAnalyzer (backend/app/graph/analyzer.py). Called
    by frontend/src/api.ts analyzeRepository(); this is the entry point for the
    "analyze a local repo" flow that seeds the analysis_id every other endpoint uses.
    """
    requested = Path(request.repository_path).expanduser()
    resolved = requested.resolve() if requested.is_absolute() else (settings.allowed_root / requested).resolve()
    try:
        # relative_to raises ValueError if resolved is not inside allowed_root, catching ../ escapes
        resolved.relative_to(settings.allowed_root)
    except ValueError as exc:
        log_event(logger, logging.WARNING, "security.repository_path_rejected", "Repository path rejected because it escapes the configured root", requested_path=request.repository_path, resolved_path=resolved, allowed_root=settings.allowed_root)
        raise HTTPException(status_code=403, detail="Repository path is outside the configured allowed root") from exc
    try:
        return await run_in_threadpool(_analyze, resolved)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (NotADirectoryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/analysis/github", response_model=AnalysisReport)
@traced("http.analysis_github")
async def analyze_github_repository(request: GitHubAnalysisRequest) -> AnalysisReport:
    """Shallow-clones a public GitHub repository and analyzes it.

    HTTP entry point: POST /api/v1/analysis/github with a GitHubAnalysisRequest
    body ({"repository_url": str}, pre-validated by validate_repository_url).
    Delegates cloning to github_cloner.clone (GitHubRepositoryCloner in
    backend/app/repository_import.py) and analysis to _analyze, both in a
    threadpool; maps invalid URLs/unavailable git/clone failures to 422/503/400.
    Called by frontend/src/api.ts analyzeGitHubRepository(); this is the entry
    point for the "analyze a GitHub repo" onboarding flow.
    """
    identity = parse_github_repository(request.repository_url)
    try:
        root = await run_in_threadpool(github_cloner.clone, request.repository_url)
        return await run_in_threadpool(_analyze, root, identity.name)
    except InvalidGitHubRepository as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GitUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (GitCloneError, RepositoryImportError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analyses/{analysis_id}", response_model=AnalysisReport)
async def get_analysis(analysis_id: str) -> AnalysisReport:
    """Re-fetches the full analysis report/graph overview for an existing session.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}, path param analysis_id
    only. Delegates to _session then GraphQueryService(...).overview()
    (backend/app/graph/store.py). Called by frontend/src/api.ts fetchAnalysis();
    lets the UI restore a previously created analysis (e.g. after a page reload
    or when returning via a bookmarked link) without re-running analysis.
    """
    return GraphQueryService(_session(analysis_id).report).overview()


@router.get("/analyses", response_model=list[AnalysisSessionSummary])
async def list_analyses() -> list[AnalysisSessionSummary]:
    """Lists recently created analysis sessions with lightweight summary stats.

    HTTP entry point: GET /api/v1/analyses, no params. Delegates to
    analysis_sessions.list_recent_summaries (backend/app/graph/store.py) in a
    threadpool - reads summary fields straight from their own SQLite columns
    rather than restoring and fully parsing every stored report, since that
    became unusably slow once several large repositories' sessions
    accumulated. Called by frontend/src/api.ts fetchAnalyses(); powers a
    "recent repositories"/history picker so users can jump back into past
    analyses.
    """
    summaries = await run_in_threadpool(analysis_sessions.list_recent_summaries)
    return [
        AnalysisSessionSummary(
            analysis_id=summary.id,
            repository_name=summary.repository_name,
            repository_root=str(summary.root),
            files_parsed=summary.files_parsed,
            node_count=summary.node_count,
            edge_count=summary.edge_count,
        )
        for summary in summaries
    ]


@router.get("/analyses/{analysis_id}/summary", response_model=GraphSummary)
async def get_summary(analysis_id: str) -> GraphSummary:
    """Returns aggregate graph statistics (node/edge counts by kind) for an analysis.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/summary, path param
    analysis_id only. Delegates to _session then
    GraphQueryService(...).summary(). Exercised by
    verification/scripts/service_performance.py and backend/tests/test_api.py;
    not currently wired into the main frontend UI, but mirrored conceptually by
    the "waypoint://analyses/{analysis_id}/summary" MCP resource in
    backend/app/mcp_server.py for programmatic/agent access to the same stats.
    """
    return GraphQueryService(_session(analysis_id).report).summary()


@router.get("/analyses/{analysis_id}/index")
async def get_index(analysis_id: str) -> dict[str, object]:
    """Returns the repository index's persisted status (revision id, record counts).

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/index, path param
    analysis_id only. Delegates to repository_indexes.get(...).status()
    (backend/app/agent/retrieval.py) in a threadpool. Called by
    frontend/src/api.ts fetchIndexStatus(); lets the UI show indexing
    progress/readiness before enabling search, ask, and tour features that
    depend on the index being built.
    """
    index = await run_in_threadpool(repository_indexes.get, _session(analysis_id))
    return index.status()


@router.get(
    "/analyses/{analysis_id}/tree",
    response_model=RepositoryTreeResponse,
)
async def get_repository_tree(analysis_id: str) -> RepositoryTreeResponse:
    """Returns every indexed source/context path, not only parser module nodes.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/tree, path param
    analysis_id only (bounded by settings.max_repository_files). Delegates to
    repository_indexes.get(...).tree() and cross-references parsed module nodes
    from session.report.nodes to split totals into parsed vs. context-only
    files. Called by frontend/src/api.ts fetchRepositoryTree(); powers the file
    explorer/tree view shown alongside the code viewer and graph.
    """
    session = _session(analysis_id)
    index = await run_in_threadpool(repository_indexes.get, session)
    tree = index.tree(limit=settings.max_repository_files)
    parsed_paths = {
        node.span.path
        for node in session.report.nodes
        if node.kind.value == "module" and node.span is not None
    }
    return RepositoryTreeResponse(
        analysis_id=analysis_id,
        paths=tree["paths"],
        total=tree["total"],
        parsed_files=len(parsed_paths),
        context_files=len(set(session.source_paths) - parsed_paths),
        canonical_files=tree["canonical_files"],
        alias_count=tree["alias_count"],
        aliases=tree["aliases"],
    )


@router.post("/analyses/{analysis_id}/index/rebuild")
async def rebuild_index(analysis_id: str) -> dict[str, object]:
    """Forces a full re-analysis and atomic rebuild of an existing session's index.

    HTTP entry point: POST /api/v1/analyses/{analysis_id}/index/rebuild, path
    param analysis_id only. Delegates to repository_indexes.rebuild
    (backend/app/agent/retrieval.py) in a threadpool. Called by
    frontend/src/api.ts rebuildIndex(); lets users refresh search/retrieval
    results after the underlying repository on disk has changed, without
    re-submitting a brand new analysis request.
    """
    index = await run_in_threadpool(repository_indexes.rebuild, _session(analysis_id))
    return index.status()


@router.get("/analyses/{analysis_id}/conversation/latest", response_model=ConversationTranscript)
async def latest_conversation(analysis_id: str, scope: str = Query("ask", pattern="^(ask|onboarding|inspector)$")) -> ConversationTranscript:  # regex: matches only the exact strings "ask", "onboarding", or "inspector"
    """Restores the most recent chat transcript for an analysis, scoped by feature area.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/conversation/latest,
    path param analysis_id plus query param scope (defaults to "ask", must be
    one of ask/onboarding/inspector). Delegates to conversations.latest/
    transcript (backend/app/agent/memory.py), rebuilding GroundedAnswer objects
    from stored answer_json with a plain-text fallback for legacy rows. Called
    by frontend/src/api.ts fetchLatestConversation(); lets the chat UI resume a
    conversation after a page reload instead of starting over.
    """
    _session(analysis_id)
    conversation = conversations.latest(analysis_id, scope)
    if conversation is None:
        return ConversationTranscript(analysis_id=analysis_id, conversation_id=None, turns=[])
    turns: list[ConversationTurn] = []
    pending_question: str | None = None
    for item in conversations.transcript(conversation):
        if item["role"] == "user":
            pending_question = item["content"]
        elif pending_question is not None:
            raw = item.get("answer_json")
            try:
                answer = GroundedAnswer.model_validate_json(raw) if raw else GroundedAnswer(question=pending_question, answer=item["content"], citations=[], refused=False, basis="restored conversation", conversation_id=conversation.id)
            except ValueError:
                answer = GroundedAnswer(question=pending_question, answer=item["content"], citations=[], refused=False, basis="restored conversation", conversation_id=conversation.id)
            turns.append(ConversationTurn(question=pending_question, answer=answer))
            pending_question = None
    return ConversationTranscript(analysis_id=analysis_id, conversation_id=conversation.id, turns=turns)


@router.get("/analyses/{analysis_id}/source", response_model=SourceDocument)
async def get_source(analysis_id: str, path: str = Query(min_length=1, max_length=2048)) -> SourceDocument:
    """Returns one indexed source file's content for the code viewer.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/source, path param
    analysis_id plus required query param path. Resolves a possibly-aliased
    request path to its canonical index path via
    repository_indexes.get(...).resolve_path (400 on an unresolvable path), then
    reads it through _read_source. Called by frontend/src/api.ts fetchSource();
    powers "click a file/symbol -> view its source" throughout the UI.
    """
    session = _session(analysis_id)
    index = await run_in_threadpool(repository_indexes.get, session)
    try:
        canonical = index.resolve_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await run_in_threadpool(_read_source, session, path, canonical)


@router.get("/analyses/{analysis_id}/nodes/{node_id}/neighborhood", response_model=GraphNeighborhood)
async def get_neighborhood(analysis_id: str, node_id: str, depth: int = Query(1, ge=1, le=3)) -> GraphNeighborhood:
    """Returns the graph neighborhood (nodes/edges) within a bounded hop count of a node.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/nodes/{node_id}/neighborhood,
    path params analysis_id/node_id plus query param depth (clamped 1-3).
    Delegates to GraphQueryService(...).neighborhood(); raises 404 if node_id
    does not exist in the graph. Called by frontend/src/api.ts
    fetchNeighborhood(); powers the interactive dependency-graph visualization
    when a user expands a node.
    """
    try:
        return GraphQueryService(_session(analysis_id).report).neighborhood(node_id, depth)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Graph node was not found") from exc


@router.get("/analyses/{analysis_id}/nodes/{node_id}/usage")
async def get_symbol_usage(analysis_id: str, node_id: str) -> dict[str, object]:
    """Returns detailed relationship data (callers, callees, imports, etc.) for one symbol.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/nodes/{node_id}/usage,
    path params analysis_id/node_id only. Delegates to
    RepositoryAgentService(session).semantic.symbol_relationships
    (backend/app/agent/semantic.py) in a threadpool; raises 404 if node_id is
    unresolvable. Called by frontend/src/api.ts fetchSymbolUsage(); powers the
    "where is this used" panel when inspecting a symbol in the graph/code viewer.
    """
    session = _session(analysis_id)
    try:
        service = RepositoryAgentService(session)
        return await run_in_threadpool(service.semantic.symbol_relationships, node_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Graph node was not found") from exc


@router.post("/analyses/{analysis_id}/tour", response_model=TourPlan)
async def create_tour(analysis_id: str, request: TourRequest) -> TourPlan:
    """Generates a guided onboarding tour plan for the repository, tailored to a role.

    HTTP entry point: POST /api/v1/analyses/{analysis_id}/tour, path param
    analysis_id plus a TourRequest body (e.g. role). Uses the LLM-backed
    RepositoryOnboardingAgent(session).plan when RepositoryAgentService is
    available, otherwise falls back to the deterministic
    OnboardingService(...).plan_tour heuristic; agent failures are translated by
    _agent_error. Called by frontend/src/api.ts createTour(); this is the core
    of the "AI-guided onboarding tour" feature new contributors see first.
    """
    session = _session(analysis_id)
    try:
        if RepositoryAgentService(session).available:
            return await run_in_threadpool(RepositoryOnboardingAgent(session).plan, request)
        return await run_in_threadpool(OnboardingService(session.report).plan_tour, request)
    except (AgentUnavailableError, AgentExecutionError) as exc:
        raise _agent_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analyses/{analysis_id}/tours", response_model=list[TourSummary])
async def list_tours(analysis_id: str) -> list[TourSummary]:
    """Lists every onboarding tour previously generated for this repository analysis.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/tours, path param
    analysis_id. Delegates to tour_states.list_tours
    (backend/app/onboarding/service.py), which reads only summary columns -
    never the stored plan JSON - so this stays fast regardless of how many
    tours exist. 404s via _session if the analysis itself doesn't exist.
    Powers a "previous onboarding sessions" picker so a user can resume a
    tour instead of only ever starting a new one.
    """
    _session(analysis_id)
    return tour_states.list_tours(analysis_id)


@router.get("/analyses/{analysis_id}/tours/{tour_id}", response_model=TourPlan)
async def get_tour(analysis_id: str, tour_id: str) -> TourPlan:
    """Reopens one previously generated onboarding tour in full.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/tours/{tour_id}, path
    params analysis_id/tour_id. Delegates to tour_states.get_plan
    (backend/app/onboarding/service.py) to fully reconstruct the stored
    TourPlan, then verifies plan.analysis_id matches the analysis_id in the
    URL - rejecting a tour_id that belongs to a different repository, even if
    the id itself is valid - before returning it. 404s if the tour doesn't
    exist or was created before this feature (plan_json unset).
    """
    plan = tour_states.get_plan(tour_id)
    if plan is None or plan.analysis_id != analysis_id:
        raise HTTPException(status_code=404, detail="Tour was not found")
    return plan


@router.post("/analyses/{analysis_id}/tours/{tour_id}/answers", response_model=ChallengeResult)
async def answer_challenge(analysis_id: str, tour_id: str, answer: ChallengeAnswer) -> ChallengeResult:
    """Grades a learner's answer to one mastery-check challenge in a tour.

    HTTP entry point: POST /api/v1/analyses/{analysis_id}/tours/{tour_id}/answers,
    path params analysis_id/tour_id plus a ChallengeAnswer body. Looks up the
    challenge via tour_states.challenge (backend/app/onboarding/service.py);
    free-text answers are graded by the LLM via
    RepositoryOnboardingAgent(session).evaluate, other answer types by
    tour_states.answer deterministically. Called by frontend/src/api.ts
    answerChallenge(); powers the interactive quiz portion of onboarding tours.
    """
    session = _session(analysis_id)
    try:
        challenge = tour_states.challenge(tour_id, answer.challenge_id)
        if challenge.question_type == "free_text":
            return await run_in_threadpool(RepositoryOnboardingAgent(session).evaluate, tour_id, challenge, answer)
        return tour_states.answer(tour_id, answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Tour challenge was not found") from exc
    except (AgentUnavailableError, AgentExecutionError) as exc:
        raise _agent_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analyses/{analysis_id}/architecture", response_model=ArchitectureReport)
async def get_architecture(analysis_id: str) -> ArchitectureReport:
    """Returns a structured architecture report (layers, key modules) inferred from the graph.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/architecture, path
    param analysis_id only. Purely deterministic — delegates to
    OnboardingService(...).architecture_report() with no LLM call. Called by
    frontend/src/api.ts fetchArchitecture(); powers the architecture overview
    page shown as part of repository onboarding.
    """
    return OnboardingService(_session(analysis_id).report).architecture_report()


@router.post("/analyses/{analysis_id}/mission", response_model=ContributionMission)
async def get_mission(analysis_id: str, request: TourRequest) -> ContributionMission:
    """Suggests a first-contribution mission for the repository, tailored to a role.

    HTTP entry point: POST /api/v1/analyses/{analysis_id}/mission, path param
    analysis_id plus a TourRequest body (role). Uses the LLM-backed
    RepositoryOnboardingAgent(session).mission when available, otherwise falls
    back to OnboardingService(...).contribution_mission; agent failures are
    translated by _agent_error. Called by frontend/src/api.ts fetchMission();
    powers the "suggest something for me to work on" onboarding feature.
    """
    session = _session(analysis_id)
    try:
        if RepositoryAgentService(session).available:
            return await run_in_threadpool(RepositoryOnboardingAgent(session).mission, request)
        return OnboardingService(session.report).contribution_mission(request.role)
    except (AgentUnavailableError, AgentExecutionError) as exc:
        raise _agent_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/analyses/{analysis_id}/answer", response_model=GroundedAnswer)
async def answer_repository_question(analysis_id: str, request: GroundedQuestion) -> GroundedAnswer:
    """Answers a free-form natural-language question about the repository with cited evidence.

    HTTP entry point: POST /api/v1/analyses/{analysis_id}/answer, path param
    analysis_id plus a GroundedQuestion body (question, history,
    conversation_id, etc.). Requires ANTHROPIC_API_KEY to be configured (503 if
    not); delegates to RepositoryAgentService(session).answer
    (backend/app/agent/service.py). Called by frontend/src/api.ts
    askRepository(); this is the main "ask the codebase" chat/Q&A endpoint.
    """
    service = RepositoryAgentService(_session(analysis_id))
    if not service.available:
        # 503 (not 500) signals a missing/optional dependency (ANTHROPIC_API_KEY) rather than a server error
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is required for AI-generated repository answers",
        )
    try:
        return await run_in_threadpool(service.answer, request)
    except (AgentUnavailableError, AgentExecutionError) as exc:
        raise _agent_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/analyses/{analysis_id}/answer/compare",
    response_model=ModelComparisonReport,
)
async def compare_repository_answers(
    analysis_id: str,
    request: ModelComparisonRequest,
) -> ModelComparisonReport:
    """Sends one question and one frozen evidence bundle to both configured models.

    HTTP entry point: POST /api/v1/analyses/{analysis_id}/answer/compare, path
    param analysis_id plus a ModelComparisonRequest body. Delegates to
    ModelComparisonService(session).compare (backend/app/agent/comparison.py)
    in a threadpool. Not currently called by the main frontend UI; used by
    evaluation tooling in verification/scripts/model_comparison_benchmark.py
    and model_quality_benchmark.py to compare answer quality across model
    providers on identical evidence.
    """
    try:
        return await run_in_threadpool(
            ModelComparisonService(_session(analysis_id)).compare,
            request,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _agent_error(exc) from exc


@router.post(
    "/analyses/{analysis_id}/answer/compare-agentic",
    response_model=AgenticComparisonReport,
)
async def compare_agentic_retrieval(
    analysis_id: str,
    request: AgenticComparisonRequest,
) -> AgenticComparisonReport:
    """Run the same task through two independent tool-calling model trajectories."""
    try:
        return await run_in_threadpool(
            AgenticRetrievalComparisonService(_session(analysis_id)).compare,
            request,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _agent_error(exc) from exc


@router.get("/analyses/{analysis_id}/journey/{node_id}", response_model=CodeJourney)
async def get_journey(analysis_id: str, node_id: str, max_steps: int = Query(20, ge=1, le=100)) -> CodeJourney:
    """Computes a bounded, step-by-step trace ("code journey") starting from a node.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/journey/{node_id}, path
    params analysis_id/node_id plus query param max_steps (clamped 1-100).
    Delegates to OnboardingService(...).journey(); raises 404 if node_id is
    unresolvable. Called by frontend/src/api.ts fetchJourney(); powers a
    guided "walk me through this code" trace feature in the onboarding UI.
    """
    try:
        return OnboardingService(_session(analysis_id).report).journey(node_id, max_steps)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Graph node was not found") from exc


@router.get("/analyses/{analysis_id}/compare/{base_analysis_id}", response_model=RevisionReport)
async def compare_analyses(analysis_id: str, base_analysis_id: str) -> RevisionReport:
    """Diffs two analysis sessions to report what changed between them.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/compare/{base_analysis_id},
    both path params are analysis session ids. Resolves both via _session, then
    delegates to OnboardingService(current.report).compare(previous.report).
    Called by frontend/src/api.ts compareAnalyses(); powers a "what changed
    since I last analyzed this repository" revision-review feature.
    """
    current = _session(analysis_id)
    previous = _session(base_analysis_id)
    return OnboardingService(current.report).compare(previous.report)


@router.get("/analyses/{analysis_id}/issues", response_model=IssueWorkspaceReport)
async def get_issues(analysis_id: str, page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100)) -> IssueWorkspaceReport:
    """Returns a paginated workspace of the repository's GitHub issues.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/issues, path param
    analysis_id plus query params page (>=1) and per_page (1-100, default 50).
    Delegates to GitHubIssueService(session).workspace
    (backend/app/issues/service.py) in a threadpool. Called by
    frontend/src/api.ts fetchIssues(); powers the issues/backlog panel used to
    triage and pick work.
    """
    return await run_in_threadpool(GitHubIssueService(_session(analysis_id)).workspace, page, per_page)


@router.post("/analyses/{analysis_id}/issues/investigate", response_model=list[ProposedIssue])
async def investigate_issues(analysis_id: str) -> list[ProposedIssue]:
    """Runs the LLM issue-investigation agent to sweep the whole repository for new,
    evidence-backed issue proposals - not scoped to any single GitHub issue.

    HTTP entry point: POST /api/v1/analyses/{analysis_id}/issues/investigate,
    path param analysis_id only. Fetches the current issue workspace via
    GitHubIssueService(session).workspace, then delegates to
    RepositoryIssueAgent(session).investigate, which accumulates new findings onto any
    already stored for this analysis rather than replacing them; agent failures are
    translated by _agent_error. Called by frontend/src/api.ts investigateIssues();
    powers the "Investigate with model"/"Generate more" action in the Waypoint
    proposals tab. For investigating one specific GitHub issue and getting a reply
    scoped to it, see investigate_issue() below instead.
    """
    session = _session(analysis_id)
    issue_service = GitHubIssueService(session)
    workspace = await run_in_threadpool(issue_service.workspace)
    try:
        return await run_in_threadpool(RepositoryIssueAgent(session).investigate, workspace.github_issues)
    except (AgentUnavailableError, AgentExecutionError) as exc:
        raise _agent_error(exc) from exc


@router.post("/analyses/{analysis_id}/issues/{issue_number}/investigate", response_model=ProposedIssue)
async def investigate_issue(analysis_id: str, issue_number: int) -> ProposedIssue:
    """Runs the LLM issue-investigation agent against one specific GitHub issue and
    returns a single evidence-backed verdict on it, cached for later retrieval.

    HTTP entry point: POST
    /api/v1/analyses/{analysis_id}/issues/{issue_number}/investigate, path params
    analysis_id and issue_number. Fetches that one issue via
    GitHubIssueService(session).issue (mapping GitHubIssueError to HTTP 502, e.g. the
    number belongs to a pull request or GitHub sync fails), then delegates to
    RepositoryIssueAgent(session).investigate_issue; agent failures are translated by
    _agent_error. Called by frontend/src/api.ts investigateIssue(); powers the
    "Investigate this issue" action rendered inline in the issue-detail pane, so a user
    never has to leave the GitHub issue they're viewing to see the result.
    """
    session = _session(analysis_id)
    try:
        issue = await run_in_threadpool(GitHubIssueService(session).issue, issue_number)
    except GitHubIssueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        return await run_in_threadpool(RepositoryIssueAgent(session).investigate_issue, issue)
    except (AgentUnavailableError, AgentExecutionError) as exc:
        raise _agent_error(exc) from exc


@router.get("/analyses/{analysis_id}/issues/{issue_number}/investigation", response_model=ProposedIssue)
async def get_issue_investigation(analysis_id: str, issue_number: int) -> ProposedIssue:
    """Returns a previously generated per-issue investigation reply without re-running
    the agent, or 404 if that issue has never been investigated.

    HTTP entry point: GET
    /api/v1/analyses/{analysis_id}/issues/{issue_number}/investigation, path params
    analysis_id and issue_number. Reads from the module-level `issue_investigations`
    store (backend/app/agent/issues.py). Called by frontend/src/api.ts
    fetchIssueInvestigation() when an issue is selected, so a cached reply from an
    earlier investigate_issue() call reappears without spending model quota again.
    """
    session = _session(analysis_id)
    cached = await run_in_threadpool(issue_investigations.get, session.id, issue_number)
    if cached is None:
        raise HTTPException(status_code=404, detail="No cached investigation for this issue yet")
    return cached


@router.get("/analyses/{analysis_id}/issues/{issue_number}/timeline", response_model=IssueTimeline)
async def get_issue_timeline(analysis_id: str, issue_number: int) -> IssueTimeline:
    """Returns the full comment/event timeline for one GitHub issue.

    HTTP entry point: GET /api/v1/analyses/{analysis_id}/issues/{issue_number}/timeline,
    path params analysis_id and issue_number. Delegates to
    GitHubIssueService(session).timeline in a threadpool; maps GitHubIssueError
    (e.g. GitHub API failure) to HTTP 502. Called by frontend/src/api.ts
    fetchIssueTimeline(); powers the issue detail view in the issues panel.
    """
    try:
        return await run_in_threadpool(GitHubIssueService(_session(analysis_id)).timeline, issue_number)
    except GitHubIssueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
