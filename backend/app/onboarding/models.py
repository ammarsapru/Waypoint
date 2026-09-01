from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.graph.models import GraphNode, SourceSpan


class DeveloperRole(str, Enum):
    GENERAL = "general"
    BACKEND = "backend"
    SECURITY = "security"
    QA = "qa"


class ExperienceLevel(str, Enum):
    NEW = "new"
    FAMILIAR = "familiar"
    EXPERT = "expert"


class TourRequest(BaseModel):
    role: DeveloperRole = DeveloperRole.GENERAL
    goal: str = Field(default="Understand the repository", max_length=300)
    experience: ExperienceLevel = ExperienceLevel.NEW
    minutes: int = Field(default=15, ge=5, le=120)
    name: str| None = None 

    @model_validator(mode="after")
    def normalize_goal(self) -> "TourRequest":
        normalized = self.goal.strip()
        if not normalized:
            defaults = {
                DeveloperRole.BACKEND: (
                    "Understand the backend architecture, request flow, data boundaries, "
                    "and testing strategy"
                ),
                DeveloperRole.SECURITY: (
                    "Understand trust boundaries, validation, authentication, and risk"
                ),
                DeveloperRole.QA: (
                    "Understand behavior, test coverage, fixtures, and failure paths"
                ),
                DeveloperRole.GENERAL: "Understand how the repository fits together",
            }
            self.goal = defaults[self.role]
        else:
            self.goal = normalized
        return self

class TourSummary(BaseModel):
    tour_id: str 
    analysis_id: str
    name: str
    role: DeveloperRole
    goal: str
    experience: ExperienceLevel
    estimated_minutes: int
    step_count: int
    updated_at: str


class ChallengeOption(BaseModel):
    node_id: str
    label: str
    kind: str


class TourChallenge(BaseModel):
    id: str
    prompt: str
    options: list[ChallengeOption] = Field(default_factory=list)
    question_type: str = "multiple_choice"


class TourEvidenceFile(BaseModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    node_id: str | None = None
    reason: str


class TourStep(BaseModel):
    index: int
    title: str
    node_id: str
    node_kind: str
    objective: str
    explanation: str
    why_selected: str
    evidence: SourceSpan | None
    challenge: TourChallenge | None = None
    files: list[TourEvidenceFile] = Field(default_factory=list)


class TourPlan(BaseModel):
    id: str
    analysis_id: str
    role: DeveloperRole
    goal: str
    experience: ExperienceLevel
    estimated_minutes: int
    steps: list[TourStep]
    planning_basis: list[str]
    provider: str = "grounded-static"


class ChallengeAnswer(BaseModel):
    challenge_id: str
    selected_node_id: str | None = None
    response: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_response(self) -> "ChallengeAnswer":
        if not self.selected_node_id and not (self.response or "").strip():
            raise ValueError("A selected node or written response is required")
        return self


class ChallengeResult(BaseModel):
    correct: bool
    explanation: str
    mastered_node_ids: list[str]
    mastered_concept_ids: list[str] = Field(default_factory=list)
    remediation: str | None = None
    # Per-question score, out of 5: multiple choice is binary (0 or 5); free-text is
    # the model's 0.0-1.0 closeness score scaled and rounded to the nearest point.
    points_earned: int = Field(ge=0, le=5)
    points_possible: int = Field(default=5, ge=0)
    # The correct option's node_id, for multiple choice only - lets the frontend
    # reveal which option was correct without a second round trip (it already has
    # every option's label from the TourStep). None for free-text, where there's no
    # single fixed correct answer; `expected_concepts` fills that role instead.
    correct_node_id: str | None = None
    expected_concepts: list[str] = Field(default_factory=list)
    # Running totals across every challenge answered so far in this tour, so the
    # frontend can show live progress without re-summing every stored result itself.
    tour_points_earned: int = Field(ge=0)
    tour_points_possible: int = Field(ge=0)
    tour_mastery: bool


class ArchitectureInsight(BaseModel):
    id: str
    severity: str
    category: str
    title: str
    explanation: str
    node_ids: list[str]
    evidence: list[SourceSpan]


class ArchitectureReport(BaseModel):
    analysis_id: str
    insights: list[ArchitectureInsight]
    import_cycle_count: int
    hotspot_count: int


class ContributionMission(BaseModel):
    analysis_id: str
    title: str
    risk: str
    target_node: GraphNode
    rationale: str
    suggested_files: list[str]
    blast_radius_node_ids: list[str]
    checklist: list[str]
    definition_of_done: list[str]
    provider: str = "grounded-static"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    validation_checks: list[str] = Field(default_factory=list)
    status: str = "proposed"


class SymbolSearchResult(BaseModel):
    node: GraphNode
    score: float


class SymbolSearchResponse(BaseModel):
    query: str
    results: list[SymbolSearchResult]


class QuestionHistoryMessage(BaseModel):
    role: str
    content: str = Field(min_length=1, max_length=4000)


class GroundedQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    focus_node_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, max_length=64)
    conversation_scope: str = Field(
        default="ask", pattern="^(ask|onboarding|inspector)$"
    )
    history: list[QuestionHistoryMessage] = Field(
        default_factory=list, max_length=10
    )


class GroundedCitation(BaseModel):
    node_id: str | None = None
    qualified_name: str
    kind: str
    span: SourceSpan
    title: str = ""
    excerpt: str = ""
    relevance: str = ""


class GroundedFeature(BaseModel):
    title: str
    description: str
    source_path: str
    source_line: int


class AgentToolActivity(BaseModel):
    round: int = Field(ge=1)
    tool: str
    status: str
    duration_ms: float = Field(ge=0)
    result_bytes: int = Field(ge=0)
    evidence_files: list[str] = Field(default_factory=list)
    candidate_files: list[str] = Field(default_factory=list)
    explicitly_inspected_files: list[str] = Field(default_factory=list)


class GroundedAnswer(BaseModel):
    question: str
    answer: str
    citations: list[GroundedCitation]
    refused: bool
    basis: str
    answer_type: str = "symbol"
    summary: str | None = None
    features: list[GroundedFeature] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)
    provider: str = "grounded-static"
    conversation_id: str | None = None
    inspected_file_count: int = 0
    tool_trace: list[AgentToolActivity] = Field(default_factory=list)


class ModelComparisonRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    evidence_limit: int = Field(default=8, ge=2, le=15)


class ModelComparisonAnswer(BaseModel):
    provider: str
    model: str
    answer: str
    basis: str
    citations: list[GroundedCitation]
    duration_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    effective_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    output_characters: int = Field(default=0, ge=0)
    output_tokens_per_second: float | None = Field(default=None, ge=0)
    ttft_ms: float | None = Field(default=None, ge=0)
    ttft_status: str = "unavailable_non_streaming"
    first_observable_event_ms: float | None = Field(default=None, ge=0)
    tool_calls: int = Field(default=1, ge=0)
    repository_tool_calls: int = Field(default=0, ge=0)
    structured_output_tool_calls: int = Field(default=1, ge=0)
    requested_max_output_tokens: int = Field(default=0, ge=0)
    validation_status: str = "passed"


class ModelComparisonFailure(BaseModel):
    provider: str
    model: str
    trace_id: str | None = None
    error_type: str
    detail: str
    duration_ms: float = Field(ge=0.0)
    validation_status: str = "failed"


class ModelComparisonReport(BaseModel):
    analysis_id: str
    question: str
    evidence_fingerprint: str
    evidence_files: list[str]
    evidence_passages: int = Field(default=0, ge=0)
    retrieval_operations: int = Field(default=1, ge=0)
    repository_access: str = "server_retrieved_frozen_evidence"
    question_character_limit: int = Field(default=500, ge=1)
    evidence_item_limit: int = Field(default=8, ge=1)
    answers: list[ModelComparisonAnswer] = Field(default_factory=list, max_length=2)
    failures: list[ModelComparisonFailure] = Field(default_factory=list, max_length=2)


class RetrievalLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_path_groups: list[list[str]] = Field(default_factory=list, max_length=20)
    acceptable_paths: list[str] = Field(default_factory=list, max_length=100)
    forbidden_paths: list[str] = Field(default_factory=list, max_length=100)


class AgenticComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=500)
    labels: RetrievalLabels = Field(default_factory=RetrievalLabels)


class RetrievalStageMetrics(BaseModel):
    ranked_paths: list[str] = Field(default_factory=list)
    hit_at_5: float = Field(ge=0, le=1)
    hit_at_10: float = Field(ge=0, le=1)
    precision_at_10: float = Field(ge=0, le=1)
    path_recall: float = Field(ge=0, le=1)
    required_group_recall: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    ndcg_at_10: float = Field(ge=0, le=1)
    forbidden_hit_rate: float = Field(ge=0, le=1)


class AgenticModelRun(BaseModel):
    provider: str
    model: str
    trace_id: str
    actual_models: list[str] = Field(default_factory=list)
    answer: str
    citations: list[GroundedCitation]
    candidate_retrieval: RetrievalStageMetrics
    explicit_inspection: RetrievalStageMetrics
    cited_retrieval: RetrievalStageMetrics
    tool_trace: list[AgentToolActivity]
    repository_tool_calls: int = Field(ge=0)
    invalid_tool_calls: int = Field(ge=0)
    total_model_rounds: int = Field(ge=1)
    duration_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    ttft_ms: float | None = Field(default=None, ge=0)
    ttft_status: str
    validation_status: str = "passed"


class AgenticComparisonReport(BaseModel):
    analysis_id: str
    question: str
    mode: str = "independent_agent_retrieval"
    repository_access: str = "independent_model_selected_waypoint_tools"
    labels: RetrievalLabels
    runs: list[AgenticModelRun] = Field(default_factory=list, max_length=4)
    failures: list[ModelComparisonFailure] = Field(default_factory=list, max_length=4)


class ConversationTurn(BaseModel):
    question: str
    answer: GroundedAnswer


class ConversationTranscript(BaseModel):
    analysis_id: str
    conversation_id: str | None
    turns: list[ConversationTurn] = Field(default_factory=list)


class JourneyStep(BaseModel):
    index: int
    node: GraphNode
    from_node_id: str | None
    relationship: str | None
    evidence: SourceSpan | None


class CodeJourney(BaseModel):
    analysis_id: str
    start_node_id: str
    steps: list[JourneyStep]
    truncated: bool


class RevisionChange(BaseModel):
    change: str
    qualified_name: str
    path: str
    node_id: str | None


class RevisionReport(BaseModel):
    analysis_id: str
    base_analysis_id: str
    added: list[RevisionChange]
    modified: list[RevisionChange]
    removed: list[RevisionChange]
    unchanged_count: int
    refresher: list[str]


class StoredChallenge(BaseModel):
    model_config = ConfigDict(frozen=True)

    correct_node_id: str | None = None
    explanation: str
    question_type: str = "multiple_choice"
    expected_concepts: list[str] = Field(default_factory=list)
    node_id: str | None = None
    evidence: list[SourceSpan] = Field(default_factory=list)
