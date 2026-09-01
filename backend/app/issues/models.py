from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from backend.app.graph.models import SourceSpan


class GitHubIssue(BaseModel):
    number: int
    title: str
    body: str | None = None
    state: Literal["open", "closed"]
    state_reason: str | None = None
    url: str
    author: str | None = None
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    comments: int = 0
    created_at: str
    updated_at: str
    closed_at: str | None = None


class IssueTimelineEvent(BaseModel):
    id: str
    event: str
    actor: str | None = None
    created_at: str | None = None
    description: str = ""


class IssueTimeline(BaseModel):
    repository: str
    issue_number: int
    events: list[IssueTimelineEvent]


class ProposedIssue(BaseModel):
    id: str
    source: Literal["static_finding", "ai_finding"]
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    severity: str
    category: str
    title: str
    explanation: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[SourceSpan] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    suggested_approach: list[str] = Field(default_factory=list)
    github_issue_number: int | None = None
    verdict: Literal["confirmed", "partially_confirmed", "refuted", "inconclusive"] | None = None
    recommended_fix: str = ""


class IssueWorkspaceReport(BaseModel):
    analysis_id: str
    repository: str | None = None
    github_connected: bool
    github_issues: list[GitHubIssue] = Field(default_factory=list)
    proposed_issues: list[ProposedIssue] = Field(default_factory=list)
    page: int = 1
    has_more: bool = False
    synchronization_warning: str | None = None

