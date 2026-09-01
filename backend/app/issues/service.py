from __future__ import annotations

import configparser
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.app.graph.store import AnalysisSession
from backend.app.issues.models import (
    GitHubIssue,
    IssueTimeline,
    IssueTimelineEvent,
    IssueWorkspaceReport,
    ProposedIssue,
)
from backend.app.observability import log_event, traced
from backend.app.onboarding.service import OnboardingService

logger = logging.getLogger(__name__)

_GITHUB_REMOTE = re.compile(
    r"(?:https?://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


class GitHubIssueError(RuntimeError):
    pass


# Generic pseudocode-level guidance per static-finding category (see
# OnboardingService.architecture_report()'s ArchitectureInsight.category values). These
# categories are fixed and deterministic, so a per-instance model call isn't needed -
# the same template applies to every instance of that category.
_STATIC_FINDING_RECOMMENDED_FIX: dict[str, str] = {
    "import_cycle": (
        "Identify the contract the two sides of the cycle actually share (types, "
        "interfaces, or constants both need) and extract just that into a new, "
        "lower-level module - e.g. `shared_types.py` or `contracts.py` - that neither "
        "original module imports back from. Update both modules to depend on the new "
        "module instead of each other:\n\n"
        "    module_a -> shared_contract <- module_b\n\n"
        "instead of `module_a <-> module_b`. Avoid a quick fix that just moves the "
        "import inside a function (defers the cycle rather than removing it)."
    ),
    "high_connectivity": (
        "Before extracting anything, introduce a narrower interface/facade in front of "
        "this module describing only what each caller group actually needs (e.g. "
        "reads vs. writes, or grouped by consuming feature). Once callers depend on "
        "that facade instead of the concrete module directly, split the "
        "implementation behind it by responsibility:\n\n"
        "    callers -> Facade -> { ResponsibilityA, ResponsibilityB, ... }\n\n"
        "Splitting the file first, without the facade step, usually just moves the "
        "same coupling to more places."
    ),
}


def _repository_from_url(value: str) -> str | None:
    match = _GITHUB_REMOTE.match(value.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def repository_identity(root: Path) -> str | None:
    marker = root / ".waypoint-managed"
    if marker.is_file():
        try:
            identity = _repository_from_url(marker.read_text(encoding="utf-8"))
            if identity:
                return identity
        except (OSError, UnicodeError):
            pass
    config_path = root / ".git" / "config"
    if not config_path.is_file():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    preferred = ['remote "origin"']
    sections = preferred + [section for section in parser.sections() if section.startswith("remote ")]
    for section in dict.fromkeys(sections):
        if parser.has_option(section, "url"):
            identity = _repository_from_url(parser.get(section, "url"))
            if identity:
                return identity
    return None


class GitHubIssueService:
    def __init__(self, session: AnalysisSession) -> None:
        self.session = session
        self.repository = repository_identity(session.root)

    def _request(self, path: str, query: dict[str, Any] | None = None) -> Any:
        if not self.repository:
            raise GitHubIssueError("The analyzed repository has no GitHub origin")
        encoded_query = urllib.parse.urlencode(query or {})
        url = f"https://api.github.com/repos/{self.repository}/{path.lstrip('/')}"
        if encoded_query:
            url += "?" + encoded_query
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "waypoint-codebase-onboarding",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read().decode("utf-8")
                remaining = response.headers.get("x-ratelimit-remaining")
                log_event(
                    logger,
                    logging.INFO,
                    "github.issues_response",
                    "GitHub issue data received",
                    repository=self.repository,
                    endpoint=path,
                    status=response.status,
                    rate_limit_remaining=remaining,
                    response_bytes=len(payload),
                )
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise GitHubIssueError(
                f"GitHub returned HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GitHubIssueError(f"GitHub issue synchronization failed: {exc}") from exc

    @staticmethod
    def _issue(item: dict[str, Any]) -> GitHubIssue:
        return GitHubIssue(
            number=int(item["number"]),
            title=str(item.get("title", "Untitled issue")),
            body=item.get("body"),
            state=item.get("state", "open"),
            state_reason=item.get("state_reason"),
            url=str(item.get("html_url", "")),
            author=(item.get("user") or {}).get("login"),
            labels=[
                str(label.get("name", ""))
                for label in item.get("labels", [])
                if label.get("name")
            ],
            assignees=[
                str(user.get("login", ""))
                for user in item.get("assignees", [])
                if user.get("login")
            ],
            comments=int(item.get("comments", 0)),
            created_at=str(item.get("created_at", "")),
            updated_at=str(item.get("updated_at", "")),
            closed_at=item.get("closed_at"),
        )

    def _proposals(self) -> list[ProposedIssue]:
        architecture = OnboardingService(self.session.report).architecture_report()
        static_findings = [
            ProposedIssue(
                id=insight.id,
                source="static_finding",
                severity=insight.severity,
                category=insight.category,
                title=insight.title,
                explanation=insight.explanation,
                confidence=1.0 if insight.category == "import_cycle" else 0.75,
                evidence=insight.evidence,
                node_ids=insight.node_ids,
                suggested_approach=[
                    "Inspect the cited dependency relationships.",
                    "Confirm whether the relationship violates an intended boundary.",
                    "Add a focused regression test before changing the dependency.",
                ],
                recommended_fix=_STATIC_FINDING_RECOMMENDED_FIX.get(insight.category, ""),
            )
            for insight in architecture.insights
        ]
        from backend.app.agent.issues import ai_findings

        return static_findings + ai_findings.get(self.session.id)

    @traced("issues.workspace")
    def workspace(self, page: int = 1, per_page: int = 50) -> IssueWorkspaceReport:
        proposals = self._proposals()
        if not self.repository:
            return IssueWorkspaceReport(
                analysis_id=self.session.id,
                github_connected=False,
                proposed_issues=proposals,
                synchronization_warning=(
                    "No GitHub origin was found. GitHub issue history is unavailable for "
                    "this local repository."
                ),
            )
        try:
            payload = self._request(
                "issues",
                {
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "page": max(1, page),
                    "per_page": max(1, min(per_page, 100)),
                },
            )
            actual_issues = [item for item in payload if "pull_request" not in item]
            return IssueWorkspaceReport(
                analysis_id=self.session.id,
                repository=self.repository,
                github_connected=True,
                github_issues=[self._issue(item) for item in actual_issues],
                proposed_issues=proposals,
                page=max(1, page),
                has_more=len(payload) >= max(1, min(per_page, 100)),
            )
        except GitHubIssueError as exc:
            return IssueWorkspaceReport(
                analysis_id=self.session.id,
                repository=self.repository,
                github_connected=True,
                proposed_issues=proposals,
                page=max(1, page),
                synchronization_warning=str(exc),
            )

    @traced("issues.timeline")
    def timeline(self, issue_number: int) -> IssueTimeline:
        payload = self._request(
            f"issues/{issue_number}/timeline", {"per_page": 100}
        )
        events = []
        for index, item in enumerate(payload):
            event = str(item.get("event", "event"))
            actor = (item.get("actor") or item.get("user") or {}).get("login")
            label = (item.get("label") or {}).get("name")
            commit_id = item.get("commit_id")
            description = event.replace("_", " ")
            if label:
                description += f": {label}"
            if commit_id:
                description += f" ({str(commit_id)[:10]})"
            events.append(
                IssueTimelineEvent(
                    id=str(item.get("id", f"{issue_number}:{index}")),
                    event=event,
                    actor=actor,
                    created_at=item.get("created_at") or item.get("submitted_at"),
                    description=description,
                )
            )
        return IssueTimeline(
            repository=self.repository or "",
            issue_number=issue_number,
            events=events,
        )

    @traced("issues.issue")
    def issue(self, issue_number: int) -> GitHubIssue:
        """Fetch one GitHub issue by number, for per-issue investigation.

        Raises GitHubIssueError if the number belongs to a pull request rather than an
        issue - GitHub's REST API returns both through the same endpoint, distinguished
        only by the presence of a "pull_request" key. Called by
        backend/app/api/routes.py:investigate_issue.
        """
        payload = self._request(f"issues/{issue_number}")
        if "pull_request" in payload:
            raise GitHubIssueError(f"#{issue_number} is a pull request, not an issue")
        return self._issue(payload)
