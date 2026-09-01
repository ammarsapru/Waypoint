export type NodeKind =
  | "repository"
  | "module"
  | "class"
  | "function"
  | "method";

export type EdgeKind = "contains" | "imports" | "may_call" | "instantiates";
export type EvidenceStatus = "verified" | "inferred" | "unresolved";

export interface SourceSpan {
  path: string;
  start_line: number;
  start_column: number;
  end_line: number;
  end_column: number;
}

export interface Evidence {
  status: EvidenceStatus;
  span: SourceSpan;
  syntax: string;
  resolution: string;
  confidence: number;
}

export interface GraphNode {
  id: string;
  kind: NodeKind;
  name: string;
  qualified_name: string;
  module: string | null;
  span: SourceSpan | null;
  signature: string | null;
  metadata: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  evidence: Evidence;
  metadata: Record<string, unknown>;
}

export interface UnresolvedReference {
  source: string;
  reference: string;
  reference_kind: string;
  evidence: Evidence;
  metadata: Record<string, unknown>;
}

export interface Diagnostic {
  severity: string;
  code: string;
  message: string;
  path: string | null;
  line: number | null;
}

export interface AnalysisStats {
  files_discovered: number;
  files_parsed: number;
  files_skipped: number;
  parse_failures: number;
  node_count: number;
  edge_count: number;
  unresolved_count: number;
  duration_ms: number;
}

export interface AnalysisReport {
  analysis_id: string;
  view: "full" | "overview";
  repository_root: string;
  repository_name: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  unresolved_references: UnresolvedReference[];
  diagnostics: Diagnostic[];
  stats: AnalysisStats;
}

export interface AnalysisSessionSummary {
  analysis_id: string;
  repository_name: string;
  repository_root: string;
  files_parsed: number;
  node_count: number;
  edge_count: number;
}

export interface IndexStatus {
  analysis_id: string;
  revision_id: string;
  fingerprint: string;
  status: "building" | "complete" | "failed" | "missing";
  indexed_at: string;
  files: number;
  symbols: number;
  chunks: number;
  edges: number;
  vectors: number;
  aliases: number;
}

export interface RepositoryTree {
  analysis_id: string;
  paths: string[];
  total: number;
  parsed_files: number;
  context_files: number;
  canonical_files: number;
  alias_count: number;
  aliases: RepositoryAlias[];
}

export interface RepositoryAlias {
  path: string;
  canonical_path: string;
  reason: "symlink" | "relative_pointer" | "exact_content_duplicate";
  target_kind: "file" | "directory";
}

export interface SourceDocument {
  analysis_id: string;
  path: string;
  language: string;
  content: string;
  line_count: number;
  size_bytes: number;
  canonical_path: string | null;
}

export interface GraphNeighborhood {
  center_node_id: string;
  depth: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface SymbolUsageNode {
  node_id: string;
  kind: NodeKind;
  name: string;
  qualified_name: string;
  path: string | null;
  start_line: number | null;
  end_line: number | null;
  language: string | null;
}

export interface SymbolUsageRelationship {
  edge_id: string;
  direction: "incoming" | "outgoing";
  relationship: EdgeKind;
  symbol: SymbolUsageNode;
  status: EvidenceStatus;
  confidence: number;
  resolution: string;
  evidence: Evidence;
}

export interface SymbolUsage {
  symbol: SymbolUsageNode;
  incoming: SymbolUsageRelationship[];
  outgoing: SymbolUsageRelationship[];
  related_files: string[];
  evidence: Array<{
    path: string;
    start_line: number;
    end_line: number;
    node_id?: string;
    qualified_name?: string;
    kind?: string;
    excerpt: string;
  }>;
}

export type Selection =
  | { type: "node"; value: GraphNode }
  | { type: "edge"; value: GraphEdge }
  | null;

export type DeveloperRole = "general" | "backend" | "security" | "qa";
export type ExperienceLevel = "new" | "familiar" | "expert";

export interface TourRequest {
  role: DeveloperRole;
  goal: string;
  experience: ExperienceLevel;
  minutes: number;
  name?: string;
}

export interface TourSummary {
  tour_id: string;
  analysis_id: string;
  name: string;
  role: DeveloperRole;
  goal: string;
  experience: ExperienceLevel;
  estimated_minutes: number;
  step_count: number;
  updated_at: string;
}

export interface ChallengeOption {
  node_id: string;
  label: string;
  kind: string;
}

export interface TourChallenge {
  id: string;
  prompt: string;
  options: ChallengeOption[];
  question_type: "multiple_choice" | "free_text";
}

export interface TourStep {
  index: number;
  title: string;
  node_id: string;
  node_kind: string;
  objective: string;
  explanation: string;
  why_selected: string;
  evidence: SourceSpan | null;
  challenge: TourChallenge | null;
  files: TourEvidenceFile[];
}

export interface TourEvidenceFile {
  path: string;
  start_line: number;
  end_line: number;
  node_id: string | null;
  reason: string;
}

export interface TourPlan {
  id: string;
  analysis_id: string;
  role: DeveloperRole;
  goal: string;
  experience: ExperienceLevel;
  estimated_minutes: number;
  steps: TourStep[];
  planning_basis: string[];
  provider: string;
}

export interface ChallengeResult {
  correct: boolean;
  explanation: string;
  mastered_node_ids: string[];
  mastered_concept_ids: string[];
  remediation: string | null;
  points_earned: number;
  points_possible: number;
  correct_node_id: string | null;
  expected_concepts: string[];
  tour_points_earned: number;
  tour_points_possible: number;
  tour_mastery: boolean;
}

export interface ArchitectureInsight {
  id: string;
  severity: string;
  category: string;
  title: string;
  explanation: string;
  node_ids: string[];
  evidence: SourceSpan[];
}

export interface ArchitectureReport {
  analysis_id: string;
  insights: ArchitectureInsight[];
  import_cycle_count: number;
  hotspot_count: number;
}

export interface ContributionMission {
  analysis_id: string;
  title: string;
  risk: string;
  target_node: GraphNode;
  rationale: string;
  suggested_files: string[];
  blast_radius_node_ids: string[];
  checklist: string[];
  definition_of_done: string[];
  provider: string;
  confidence: number;
  validation_checks: string[];
  status: string;
}

export interface GroundedCitation {
  node_id: string | null;
  qualified_name: string;
  kind: string;
  span: SourceSpan;
  title: string;
  excerpt: string;
  relevance: string;
}

export interface GroundedFeature {
  title: string;
  description: string;
  source_path: string;
  source_line: number;
}

export interface AgentToolActivity {
  round: number;
  tool: string;
  status: string;
  duration_ms: number;
  result_bytes: number;
  evidence_files: string[];
  candidate_files: string[];
  explicitly_inspected_files: string[];
}

export interface RetrievalStageMetrics {
  ranked_paths: string[];
  hit_at_5: number;
  hit_at_10: number;
  precision_at_10: number;
  path_recall: number;
  required_group_recall: number;
  mrr: number;
  ndcg_at_10: number;
  forbidden_hit_rate: number;
}

export interface AgenticModelRun {
  provider: string;
  model: string;
  actual_models: string[];
  answer: string;
  citations: GroundedCitation[];
  candidate_retrieval: RetrievalStageMetrics;
  explicit_inspection: RetrievalStageMetrics;
  cited_retrieval: RetrievalStageMetrics;
  tool_trace: AgentToolActivity[];
  repository_tool_calls: number;
  invalid_tool_calls: number;
  total_model_rounds: number;
  duration_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  estimated_cost_usd: number | null;
  ttft_ms: number | null;
  ttft_status: string;
  validation_status: string;
}

export interface AgenticComparisonReport {
  analysis_id: string;
  question: string;
  mode: string;
  repository_access: string;
  labels: {
    required_path_groups: string[][];
    acceptable_paths: string[];
    forbidden_paths: string[];
  };
  runs: AgenticModelRun[];
  failures: Array<{
    provider: string;
    model: string;
    error_type: string;
    detail: string;
    duration_ms: number;
  }>;
}

export interface GroundedAnswer {
  question: string;
  answer: string;
  citations: GroundedCitation[];
  refused: boolean;
  basis: string;
  answer_type: string;
  summary: string | null;
  features: GroundedFeature[];
  suggested_questions: string[];
  provider: string;
  conversation_id: string | null;
  inspected_file_count: number;
  tool_trace: AgentToolActivity[];
}

export interface ConversationTurn {
  question: string;
  answer: GroundedAnswer;
}

export interface ConversationTranscript {
  analysis_id: string;
  conversation_id: string | null;
  turns: ConversationTurn[];
}

export interface JourneyStep {
  index: number;
  node: GraphNode;
  from_node_id: string | null;
  relationship: EdgeKind | null;
  evidence: SourceSpan | null;
}

export interface CodeJourney {
  analysis_id: string;
  start_node_id: string;
  steps: JourneyStep[];
  truncated: boolean;
}

export interface RevisionChange {
  change: "added" | "modified" | "removed";
  qualified_name: string;
  path: string;
  node_id: string | null;
}

export interface RevisionReport {
  analysis_id: string;
  base_analysis_id: string;
  added: RevisionChange[];
  modified: RevisionChange[];
  removed: RevisionChange[];
  unchanged_count: number;
  refresher: string[];
}

export interface GitHubIssue {
  number: number;
  title: string;
  body: string | null;
  state: "open" | "closed";
  state_reason: string | null;
  url: string;
  author: string | null;
  labels: string[];
  assignees: string[];
  comments: number;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

export interface IssueTimelineEvent {
  id: string;
  event: string;
  actor: string | null;
  created_at: string | null;
  description: string;
}

export interface IssueTimeline {
  repository: string;
  issue_number: number;
  events: IssueTimelineEvent[];
}

export interface ProposedIssue {
  id: string;
  source: "static_finding" | "ai_finding";
  status: "proposed" | "accepted" | "rejected";
  severity: string;
  category: string;
  title: string;
  explanation: string;
  confidence: number;
  evidence: SourceSpan[];
  node_ids: string[];
  suggested_approach: string[];
  github_issue_number: number | null;
  verdict: "confirmed" | "partially_confirmed" | "refuted" | "inconclusive" | null;
  recommended_fix: string;
}

export interface IssueWorkspaceReport {
  analysis_id: string;
  repository: string | null;
  github_connected: boolean;
  github_issues: GitHubIssue[];
  proposed_issues: ProposedIssue[];
  page: number;
  has_more: boolean;
  synchronization_warning: string | null;
}
