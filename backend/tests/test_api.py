from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from fastapi.testclient import TestClient

from backend.app.agent.service import AgentUnavailableError
from backend.app.api.routes import router
from backend.app.issues.models import GitHubIssue
from backend.app.main import app
from backend.app.onboarding.models import AgenticComparisonReport, RetrievalLabels


class ApiTests(unittest.TestCase):
    def test_ui_api_contract_is_registered(self) -> None:
        registered = {
            getattr(route, "path", "")
            for route in router.routes
            if getattr(route, "path", "").startswith("/api/v1")
        }
        expected = {
            "/api/v1/health",
            "/api/v1/usage/summary",
            "/api/v1/analysis",
            "/api/v1/analysis/github",
            "/api/v1/analyses",
            "/api/v1/analyses/{analysis_id}",
            "/api/v1/analyses/{analysis_id}/summary",
            "/api/v1/analyses/{analysis_id}/index",
            "/api/v1/analyses/{analysis_id}/tree",
            "/api/v1/analyses/{analysis_id}/index/rebuild",
            "/api/v1/analyses/{analysis_id}/conversation/latest",
            "/api/v1/analyses/{analysis_id}/source",
            "/api/v1/analyses/{analysis_id}/nodes/{node_id}/neighborhood",
            "/api/v1/analyses/{analysis_id}/nodes/{node_id}/usage",
            "/api/v1/analyses/{analysis_id}/tour",
            "/api/v1/analyses/{analysis_id}/tours/{tour_id}/answers",
            "/api/v1/analyses/{analysis_id}/architecture",
            "/api/v1/analyses/{analysis_id}/mission",
            "/api/v1/analyses/{analysis_id}/answer",
            "/api/v1/analyses/{analysis_id}/answer/compare",
            "/api/v1/analyses/{analysis_id}/answer/compare-agentic",
            "/api/v1/analyses/{analysis_id}/journey/{node_id}",
            "/api/v1/analyses/{analysis_id}/compare/{base_analysis_id}",
            "/api/v1/analyses/{analysis_id}/issues",
            "/api/v1/analyses/{analysis_id}/issues/investigate",
            "/api/v1/analyses/{analysis_id}/issues/{issue_number}/investigate",
            "/api/v1/analyses/{analysis_id}/issues/{issue_number}/investigation",
            "/api/v1/analyses/{analysis_id}/issues/{issue_number}/timeline",
        }
        self.assertTrue(expected.issubset(registered), expected - registered)

    def test_usage_summary_reflects_ledger_rows(self) -> None:
        from backend.app.agent.usage_ledger import model_usage_ledger

        model_usage_ledger.record(
            workflow="ask",
            routed_provider="bedrock:us.anthropic.claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=100,
            estimated_cost_usd=0.0045,
        )
        with TestClient(app) as client:
            response = client.get("/api/v1/usage/summary")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("by_workflow", payload)
        self.assertIn("by_provider", payload)
        self.assertIn("by_analysis", payload)
        workflow_labels = {row["label"] for row in payload["by_workflow"]}
        self.assertIn("ask", workflow_labels)
        provider_labels = {row["label"] for row in payload["by_provider"]}
        self.assertIn("bedrock:us.anthropic.claude-sonnet-4-6", provider_labels)

    def test_usage_summary_by_analysis_resolves_repository_name_across_repos(
        self,
    ) -> None:
        from backend.app.agent.usage_ledger import model_usage_ledger

        with TestClient(app) as client:
            first = client.post(
                "/api/v1/analysis", json={"repository_path": "backend/tests"}
            ).json()
            second = client.post(
                "/api/v1/analysis", json={"repository_path": "backend/app"}
            ).json()

            model_usage_ledger.record(
                workflow="ask", analysis_id=first["analysis_id"],
                routed_provider="bedrock:us.anthropic.claude-sonnet-4-6",
                input_tokens=100, output_tokens=10, estimated_cost_usd=0.0006,
            )
            model_usage_ledger.record(
                workflow="issue_sweep", analysis_id=second["analysis_id"],
                routed_provider="bedrock:us.anthropic.claude-opus-4-6-v1",
                input_tokens=200, output_tokens=20, estimated_cost_usd=0.0022,
            )

            response = client.get("/api/v1/usage/summary")

        self.assertEqual(response.status_code, 200)
        by_analysis = {
            row["analysis_id"]: row for row in response.json()["by_analysis"]
        }
        self.assertEqual(
            by_analysis[first["analysis_id"]]["label"], first["repository_name"]
        )
        self.assertEqual(
            by_analysis[second["analysis_id"]]["label"], second["repository_name"]
        )
        self.assertNotEqual(
            by_analysis[first["analysis_id"]]["label"],
            by_analysis[second["analysis_id"]]["label"],
        )

    def test_health_exposes_diagnostic_mode(self) -> None:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/health", headers={"x-request-id": "health-test"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-request-id"], "health-test")
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("trace_functions", payload)
        self.assertIn("max_trace", payload)
        self.assertIn("clone_root", payload)

    def test_agentic_comparison_http_contract(self) -> None:
        with TestClient(app) as client:
            analyzed = client.post(
                "/api/v1/analysis", json={"repository_path": "backend/tests"}
            )
            analysis_id = analyzed.json()["analysis_id"]
            expected = AgenticComparisonReport(
                analysis_id=analysis_id,
                question="Where is analysis implemented?",
                labels=RetrievalLabels(acceptable_paths=["test_analyzer.py"]),
            )
            with patch(
                "backend.app.api.routes.AgenticRetrievalComparisonService.compare",
                return_value=expected,
            ) as compare:
                response = client.post(
                    f"/api/v1/analyses/{analysis_id}/answer/compare-agentic",
                    json={
                        "question": "Where is analysis implemented?",
                        "labels": {
                            "required_path_groups": [],
                            "acceptable_paths": ["test_analyzer.py"],
                            "forbidden_paths": [],
                        },
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "independent_agent_retrieval")
        compare.assert_called_once()

    def test_repository_path_cannot_escape_allowed_root(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/analysis",
                json={"repository_path": ".."},
                headers={"x-request-id": "escape-test"},
            )
        self.assertEqual(response.status_code, 403)

    def test_github_analysis_validates_then_analyzes_cloned_repository(self) -> None:
        with (
            patch(
                "backend.app.api.routes.github_cloner.clone",
                return_value=Path("backend/tests").resolve(),
            ) as clone,
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/v1/analysis/github",
                json={"repository_url": "https://github.com/example/project"},
                headers={"x-request-id": "github-analysis-test"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-request-id"], "github-analysis-test")
        self.assertEqual(response.json()["repository_name"], "project")
        self.assertGreater(response.json()["stats"]["files_parsed"], 0)
        clone.assert_called_once_with("https://github.com/example/project")

        with TestClient(app) as client:
            rejected = client.post(
                "/api/v1/analysis/github",
                json={"repository_url": "file:///C:/private/repository"},
            )
        self.assertEqual(rejected.status_code, 422)

    def test_analysis_session_supports_source_summary_and_neighborhood(self) -> None:
        with TestClient(app) as client:
            analysis_response = client.post(
                "/api/v1/analysis",
                json={"repository_path": "backend/tests"},
                headers={"x-request-id": "phase-one-analysis"},
            )
            self.assertEqual(analysis_response.status_code, 200)
            report = analysis_response.json()
            analysis_id = report["analysis_id"]
            self.assertTrue(analysis_id)

            sessions_response = client.get("/api/v1/analyses")
            self.assertEqual(sessions_response.status_code, 200)
            session = next(
                item for item in sessions_response.json()
                if item["analysis_id"] == analysis_id
            )
            self.assertEqual(session["repository_name"], "tests")
            self.assertGreater(session["files_parsed"], 0)

            summary_response = client.get(
                f"/api/v1/analyses/{analysis_id}/summary"
            )
            self.assertEqual(summary_response.status_code, 200)
            self.assertEqual(
                summary_response.json()["analysis_id"], analysis_id
            )

            index_response = client.get(
                f"/api/v1/analyses/{analysis_id}/index"
            )
            self.assertEqual(index_response.status_code, 200)
            self.assertEqual(index_response.json()["status"], "complete")
            self.assertEqual(
                index_response.json()["vectors"],
                index_response.json()["chunks"],
            )

            tree_response = client.get(
                f"/api/v1/analyses/{analysis_id}/tree"
            )
            self.assertEqual(tree_response.status_code, 200)
            tree = tree_response.json()
            self.assertEqual(tree["analysis_id"], analysis_id)
            self.assertIn("test_analyzer.py", tree["paths"])
            self.assertEqual(tree["total"], len(tree["paths"]))
            self.assertGreaterEqual(tree["parsed_files"], 1)

            module = next(
                node
                for node in report["nodes"]
                if node["kind"] == "module"
                and node["span"]["path"] == "test_analyzer.py"
            )
            source_response = client.get(
                f"/api/v1/analyses/{analysis_id}/source",
                params={"path": "test_analyzer.py"},
            )
            self.assertEqual(source_response.status_code, 200)
            self.assertIn(
                "class RepositoryAnalyzerTests",
                source_response.json()["content"],
            )

            neighborhood_response = client.get(
                (
                    f"/api/v1/analyses/{analysis_id}/nodes/"
                    f"{module['id']}/neighborhood"
                ),
                params={"depth": 1},
            )
            self.assertEqual(neighborhood_response.status_code, 200)
            self.assertEqual(
                neighborhood_response.json()["center_node_id"], module["id"]
            )

            usage_response = client.get(
                f"/api/v1/analyses/{analysis_id}/nodes/{module['id']}/usage"
            )
            self.assertEqual(usage_response.status_code, 200)
            usage = usage_response.json()
            self.assertEqual(usage["symbol"]["node_id"], module["id"])
            self.assertIn("incoming", usage)
            self.assertIn("outgoing", usage)
            self.assertIn("related_files", usage)

            with patch(
                "backend.app.api.routes.RepositoryAgentService.available",
                new_callable=PropertyMock,
                return_value=False,
            ):
                answer_response = client.post(
                    f"/api/v1/analyses/{analysis_id}/answer",
                    json={"question": "Where is repository analysis tested?"},
                )
            self.assertEqual(answer_response.status_code, 503)
            self.assertIn("ANTHROPIC_API_KEY", answer_response.json()["detail"])

            journey_response = client.get(
                f"/api/v1/analyses/{analysis_id}/journey/{module['id']}",
                params={"max_steps": 5},
            )
            self.assertEqual(journey_response.status_code, 200)
            self.assertLessEqual(len(journey_response.json()["steps"]), 5)

            rejected_source = client.get(
                f"/api/v1/analyses/{analysis_id}/source",
                params={"path": "../../plan.md"},
            )
            self.assertEqual(rejected_source.status_code, 404)

    def test_investigate_issue_without_github_origin_returns_502(self) -> None:
        with TestClient(app) as client:
            analysis_response = client.post(
                "/api/v1/analysis", json={"repository_path": "backend/tests"}
            )
            analysis_id = analysis_response.json()["analysis_id"]

            # backend/tests has no .waypoint-managed marker and isn't its own git
            # checkout, so GitHubIssueService.repository is None and _request() raises
            # before any real network call - this exercises the real code path, no
            # mocking needed.
            response = client.post(
                f"/api/v1/analyses/{analysis_id}/issues/7/investigate"
            )
            self.assertEqual(response.status_code, 502)
            self.assertIn("GitHub origin", response.json()["detail"])

    def test_get_issue_investigation_returns_404_before_any_investigation(self) -> None:
        with TestClient(app) as client:
            analysis_response = client.post(
                "/api/v1/analysis", json={"repository_path": "backend/tests"}
            )
            analysis_id = analysis_response.json()["analysis_id"]

            response = client.get(
                f"/api/v1/analyses/{analysis_id}/issues/7/investigation"
            )
            self.assertEqual(response.status_code, 404)

    def test_investigate_issue_returns_503_when_agent_unavailable(self) -> None:
        # Unlike answer_repository_question, investigate_issue has no `.available`
        # pre-check (matching the pre-existing investigate_issues route) - it relies on
        # RepositoryAgentService._client() itself raising AgentUnavailableError. So the
        # only correct way to simulate "no provider configured" here is to make
        # _client() raise directly; patching the inert `.available` property (as the
        # /answer test does) would NOT stop this route from calling a real, live,
        # credit-spending model provider.
        fake_issue = GitHubIssue(
            number=7,
            title="Sample issue",
            body="Sample body.",
            state="open",
            url="https://github.com/example/project/issues/7",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        with TestClient(app) as client:
            analysis_response = client.post(
                "/api/v1/analysis", json={"repository_path": "backend/tests"}
            )
            analysis_id = analysis_response.json()["analysis_id"]

            with patch(
                "backend.app.api.routes.GitHubIssueService.issue",
                return_value=fake_issue,
            ), patch(
                "backend.app.api.routes.RepositoryAgentService._client",
                side_effect=AgentUnavailableError("No model provider is available"),
            ):
                response = client.post(
                    f"/api/v1/analyses/{analysis_id}/issues/7/investigate"
                )
            self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
