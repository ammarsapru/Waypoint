from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from backend.app.agent.agentic_comparison import AgenticRetrievalComparisonService
from backend.app.agent.comparison import ModelComparisonService
from backend.app.agent.provider import model_provider_router
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.store import analysis_sessions
from backend.app.onboarding.models import AgenticComparisonRequest, RetrievalLabels


def test_models_retrieve_independently_and_are_scored_by_stage() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "README.md").write_text("# Greeting\nThe app greets users.\n", encoding="utf-8")
        (root / "app.py").write_text(
            "def greet(name: str) -> str:\n    return f'Hello {name}'\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(root)
        stored = analysis_sessions.create(root, report)
        session = analysis_sessions.get(stored.analysis_id or "")
        calls: dict[str, int] = defaultdict(int)

        def invoke(provider: str, model: str, role: str, request: dict):
            calls[provider] += 1
            if calls[provider] == 1:
                tool = SimpleNamespace(
                    type="tool_use", id=f"{provider}-search",
                    name="search_repository", input={"query": "greet", "limit": 5},
                )
            else:
                tool = SimpleNamespace(
                    type="tool_use", id=f"{provider}-submit", name="submit_answer",
                    input={
                        "answer": "The repository defines a greeting function.",
                        "basis": "The source contains greet.",
                        "refused": False,
                        "citations": [{
                            "path": "app.py", "start_line": 1, "end_line": 2,
                            "title": "Greeting function", "relevance": "Defines greet.",
                        }],
                        "suggested_questions": [],
                    },
                )
            return SimpleNamespace(
                content=[tool], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=20, output_tokens=8),
                waypoint_provider=f"{provider}:{model}",
                waypoint_cost_usd=0.001,
                waypoint_ttft_ms=10.0,
                waypoint_ttft_status="measured",
            )

        configured = SimpleNamespace(
            investigation_provider="openrouter", investigation_model="model-a",
            synthesis_provider="claude-code", synthesis_model="model-b",
        )
        with (
            patch("backend.app.agent.comparison.settings", configured),
            patch.object(model_provider_router, "create_for_endpoint", side_effect=invoke),
            patch.object(
                type(model_provider_router), "dual_role_enabled",
                new_callable=PropertyMock, return_value=False,
            ),
            patch.object(
                ModelComparisonService,
                "_endpoints",
                return_value=[("openrouter", "model-a"), ("claude-code", "model-b")],
            ),
        ):
            result = AgenticRetrievalComparisonService(session).compare(
                AgenticComparisonRequest(
                    question="Where is greeting implemented?",
                    labels=RetrievalLabels(
                        required_path_groups=[["app.py"]], acceptable_paths=["app.py"]
                    ),
                )
            )
        assert len(result.runs) == 2
        assert result.failures == []
        assert all(run.candidate_retrieval.hit_at_5 == 1 for run in result.runs)
        assert all(run.cited_retrieval.path_recall == 1 for run in result.runs)
        assert all(run.repository_tool_calls == 1 for run in result.runs)
        assert calls == {"openrouter": 2, "claude-code": 2}
