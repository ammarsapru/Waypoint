from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from backend.app.agent.comparison import ModelComparisonService
from backend.app.agent.provider import model_provider_router
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.store import analysis_sessions
from backend.app.onboarding.models import ModelComparisonRequest


class ModelComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "README.md").write_text(
            "# Greeting service\n\nThe service returns a greeting through greet.\n",
            encoding="utf-8",
        )
        (self.root / "app.py").write_text(
            "def greet(name: str) -> str:\n"
            "    return f'Hello {name}'\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        stored = analysis_sessions.create(self.root, report)
        self.session = analysis_sessions.get(stored.analysis_id or "")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _response(provider: str) -> SimpleNamespace:
        submission = SimpleNamespace(
            type="tool_use",
            name="submit_comparison_answer",
            input={
                "answer": f"{provider} says greet returns a personalized greeting.",
                "basis": "The frozen function source defines the return value.",
                "refused": False,
                "citations": [{
                    "path": "README.md",
                    "start_line": 1,
                    "end_line": 3,
                    "title": "Greeting service overview",
                    "relevance": "Describes the service and its greeting behavior.",
                }],
                "suggested_questions": [],
            },
        )
        return SimpleNamespace(
            content=[submission],
            usage=SimpleNamespace(input_tokens=50, output_tokens=20),
            waypoint_cost_usd=0.001,
        )

    def test_same_question_uses_identical_frozen_evidence_for_two_models(self) -> None:
        configured = SimpleNamespace(
            investigation_provider="openrouter",
            investigation_model="model-a",
            synthesis_provider="claude-code",
            synthesis_model="model-b",
            agent_max_output_tokens=2000,
        )
        calls: list[tuple[str, str, str]] = []

        def invoke(
            provider: str,
            model: str,
            role: str,
            request: dict[str, object],
        ) -> SimpleNamespace:
            messages = request["messages"]
            assert isinstance(messages, list)
            calls.append((provider, model, messages[0]["content"]))
            return self._response(provider)

        service = ModelComparisonService(self.session)
        with (
            patch("backend.app.agent.comparison.settings", configured),
            patch.object(
                type(model_provider_router),
                "dual_role_enabled",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch.object(
                model_provider_router,
                "create_for_endpoint",
                side_effect=invoke,
            ) as create,
        ):
            report = service.compare(ModelComparisonRequest(
                question="What does the greeting service do?",
                evidence_limit=4,
            ))

        self.assertEqual(create.call_count, 2)
        self.assertEqual(len(report.answers), 2)
        self.assertEqual(report.failures, [])
        self.assertEqual(
            {(answer.provider, answer.model) for answer in report.answers},
            {("openrouter", "model-a"), ("claude-code", "model-b")},
        )
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(report.answers[0].validation_status, "passed")
        self.assertEqual(report.answers[1].citations[0].span.path, "README.md")
        self.assertEqual(len(report.evidence_fingerprint), 64)
        self.assertEqual(report.repository_access, "server_retrieved_frozen_evidence")
        self.assertEqual(report.retrieval_operations, 1)
        self.assertGreater(report.evidence_passages, 0)
        for answer in report.answers:
            self.assertEqual(answer.total_tokens, 70)
            self.assertEqual(answer.tool_calls, 1)
            self.assertEqual(answer.repository_tool_calls, 0)
            self.assertEqual(answer.structured_output_tool_calls, 1)
            self.assertEqual(answer.requested_max_output_tokens, 2000)
            self.assertIsNone(answer.ttft_ms)
            self.assertIsNone(answer.first_observable_event_ms)
            self.assertEqual(answer.ttft_status, "unavailable_non_streaming")
            self.assertGreater(answer.output_characters, 0)

    def test_comparison_rejects_single_model_mode(self) -> None:
        service = ModelComparisonService(self.session)
        with patch.object(
            type(model_provider_router),
            "dual_role_enabled",
            new_callable=PropertyMock,
            return_value=False,
        ):
            with self.assertRaisesRegex(ValueError, "requires.*dual"):
                service.compare(ModelComparisonRequest(question="What does greet do?"))

    def test_comparison_preserves_one_provider_failure(self) -> None:
        configured = SimpleNamespace(
            investigation_provider="openrouter",
            investigation_model="model-a",
            synthesis_provider="claude-code",
            synthesis_model="model-b",
            agent_max_output_tokens=2000,
        )
        service = ModelComparisonService(self.session)

        def invoke(provider: str, *_args: object, **_kwargs: object) -> SimpleNamespace:
            if provider == "openrouter":
                raise RuntimeError("provider unavailable")
            return self._response(provider)

        with (
            patch("backend.app.agent.comparison.settings", configured),
            patch.object(
                type(model_provider_router),
                "dual_role_enabled",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch.object(model_provider_router, "create_for_endpoint", side_effect=invoke),
        ):
            report = service.compare(ModelComparisonRequest(question="What does greet do?"))

        self.assertEqual(len(report.answers), 1)
        self.assertEqual(report.answers[0].provider, "claude-code")
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(report.failures[0].provider, "openrouter")
        self.assertEqual(report.failures[0].validation_status, "failed")


if __name__ == "__main__":
    unittest.main()
