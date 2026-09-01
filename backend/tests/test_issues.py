from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.agent.issues import FindingStore, IssueInvestigationStore
from backend.app.graph.models import SourceSpan
from backend.app.graph.store import analysis_sessions
from backend.app.issues.models import ProposedIssue
from backend.app.issues.service import (
    GitHubIssueError,
    GitHubIssueService,
    repository_identity,
)
from backend.app.onboarding.models import ArchitectureInsight, ArchitectureReport


class GitHubIssueServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / ".waypoint-managed").write_text(
            "https://github.com/example/project.git", encoding="utf-8"
        )
        (self.root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        report = RepositoryAnalyzer().analyze(self.root)
        stored = analysis_sessions.create(self.root, report)
        self.session = analysis_sessions.get(stored.analysis_id)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_repository_identity_uses_managed_clone_marker(self) -> None:
        self.assertEqual(repository_identity(self.root), "example/project")

    def test_workspace_filters_pull_requests_from_issue_results(self) -> None:
        issue = {
            "number": 7,
            "title": "A real issue",
            "state": "open",
            "html_url": "https://github.com/example/project/issues/7",
            "user": {"login": "maintainer"},
            "labels": [{"name": "bug"}],
            "assignees": [],
            "comments": 2,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "closed_at": None,
        }
        pull_request = dict(issue, number=8, pull_request={"url": "example"})
        service = GitHubIssueService(self.session)
        with patch.object(service, "_request", return_value=[issue, pull_request]):
            report = service.workspace()
        self.assertTrue(report.github_connected)
        self.assertEqual(report.repository, "example/project")
        self.assertEqual([item.number for item in report.github_issues], [7])

    def test_static_findings_get_a_templated_recommended_fix_per_category(self) -> None:
        synthetic_report = ArchitectureReport(
            analysis_id=self.session.id,
            insights=[
                ArchitectureInsight(
                    id="cycle-1", severity="warning", category="import_cycle",
                    title="Import cycle across 2 modules", explanation="a -> b -> a",
                    node_ids=[], evidence=[],
                ),
                ArchitectureInsight(
                    id="hotspot-1", severity="info", category="high_connectivity",
                    title="app is an architectural hotspot", explanation="...",
                    node_ids=[], evidence=[],
                ),
            ],
            import_cycle_count=1,
            hotspot_count=1,
        )
        service = GitHubIssueService(self.session)
        with patch(
            "backend.app.issues.service.OnboardingService.architecture_report",
            return_value=synthetic_report,
        ):
            proposals = service._proposals()
        by_category = {item.category: item for item in proposals}
        self.assertTrue(by_category["import_cycle"].recommended_fix.strip())
        self.assertTrue(by_category["high_connectivity"].recommended_fix.strip())
        self.assertNotEqual(
            by_category["import_cycle"].recommended_fix,
            by_category["high_connectivity"].recommended_fix,
        )

    def test_issue_fetches_single_github_issue_by_number(self) -> None:
        issue = {
            "number": 7,
            "title": "A real issue",
            "state": "open",
            "html_url": "https://github.com/example/project/issues/7",
            "user": {"login": "maintainer"},
            "labels": [],
            "assignees": [],
            "comments": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "closed_at": None,
        }
        service = GitHubIssueService(self.session)
        with patch.object(service, "_request", return_value=issue) as request:
            result = service.issue(7)
        request.assert_called_once_with("issues/7")
        self.assertEqual(result.number, 7)
        self.assertEqual(result.title, "A real issue")

    def test_issue_rejects_pull_request_number(self) -> None:
        pull_request = {
            "number": 8,
            "title": "Not really an issue",
            "state": "open",
            "html_url": "https://github.com/example/project/pull/8",
            "pull_request": {"url": "example"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
        service = GitHubIssueService(self.session)
        with patch.object(service, "_request", return_value=pull_request):
            with self.assertRaises(GitHubIssueError):
                service.issue(8)

    def test_ai_findings_restore_from_sqlite(self) -> None:
        database = self.root / "findings.sqlite3"
        finding = ProposedIssue(
            id="finding-1",
            source="ai_finding",
            severity="medium",
            category="testing",
            title="Missing boundary coverage",
            explanation="The boundary has no focused test.",
            confidence=0.8,
            evidence=[SourceSpan(
                path="app.py", start_line=1, start_column=0,
                end_line=2, end_column=0,
            )],
            suggested_approach=["Add a focused test", "Run the suite"],
        )
        # Stores keep a persistent SQLite connection open per thread now (see
        # FindingStore._connect), so both must be closed explicitly before tearDown
        # deletes self.root - unittest runs addCleanup callbacks *after* tearDown,
        # too late to release the file handle before the Windows rmtree.
        first_store = FindingStore(database)
        try:
            first_store.put(self.session.id, [finding])
        finally:
            first_store.close()

        second_store = FindingStore(database)
        try:
            self.assertEqual(second_store.get(self.session.id), [finding])
        finally:
            second_store.close()

    def test_issue_investigation_store_restores_from_sqlite(self) -> None:
        database = self.root / "issue-investigations.sqlite3"
        finding = ProposedIssue(
            id="finding-issue-7",
            source="ai_finding",
            severity="medium",
            category="correctness",
            title="Off-by-one in pagination",
            explanation="The last page is dropped.",
            confidence=0.7,
            evidence=[SourceSpan(
                path="app.py", start_line=1, start_column=0,
                end_line=2, end_column=0,
            )],
            suggested_approach=["Fix the boundary", "Add a regression test"],
            github_issue_number=7,
            verdict="confirmed",
        )
        first_store = IssueInvestigationStore(database)
        try:
            first_store.put(self.session.id, 7, finding)
        finally:
            first_store.close()

        second_store = IssueInvestigationStore(database)
        try:
            self.assertEqual(second_store.get(self.session.id, 7), finding)
            self.assertIsNone(second_store.get(self.session.id, 999))
        finally:
            second_store.close()


if __name__ == "__main__":
    unittest.main()
