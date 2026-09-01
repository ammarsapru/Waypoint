from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.api.routes import _read_source
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.store import analysis_sessions
from backend.app.onboarding.models import GroundedQuestion
from backend.app.onboarding.questions import RepositoryQuestionService


class RepositoryQuestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "app.py").write_text(
            "def run_server():\n    return 'ready'\n", encoding="utf-8"
        )
        feature_lines = "\n".join(
            f"- **Feature {index}** — Capability number {index}"
            for index in range(1, 12)
        )
        (self.root / "README.md").write_text(
            "# Sample\n\n"
            "## What is Sample?\n\n"
            "Sample is a local-first application for understanding complex "
            "repositories with source-backed evidence.\n\n"
            f"{feature_lines}\n",
            encoding="utf-8",
        )
        (self.root / "package.json").write_text(
            '{"name":"sample","scripts":{"dev":"vite"}}',
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        stored = analysis_sessions.create(self.root, report)
        self.session = analysis_sessions.get(stored.analysis_id)
        self.service = RepositoryQuestionService(self.session)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_repository_overview_returns_summary_ten_features_and_files(self) -> None:
        answer = self.service.answer(
            GroundedQuestion(
                question=(
                    "What is this repository about? Highlight its top 10 features."
                )
            )
        )
        self.assertFalse(answer.refused)
        self.assertEqual(answer.answer_type, "overview")
        self.assertIn("local-first application", answer.summary)
        self.assertEqual(len(answer.features), 10)
        self.assertEqual(answer.features[0].title, "Feature 1")
        cited_paths = {citation.span.path for citation in answer.citations}
        self.assertIn("README.md", cited_paths)
        self.assertIn("package.json", cited_paths)
        self.assertIn("app.py", cited_paths)
        self.assertFalse(
            any(".test" in citation.qualified_name for citation in answer.citations)
        )

    def test_documentation_can_be_loaded_as_indexed_evidence(self) -> None:
        document = _read_source(self.session, "README.md")
        self.assertEqual(document.language, "markdown")
        self.assertIn("What is Sample?", document.content)

    def test_specific_location_question_still_uses_symbol_evidence(self) -> None:
        answer = self.service.answer(
            GroundedQuestion(question="Where is run server implemented?")
        )
        self.assertFalse(answer.refused)
        self.assertEqual(answer.answer_type, "symbol")
        self.assertEqual(answer.citations[0].span.path, "app.py")


if __name__ == "__main__":
    unittest.main()
