from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.agent.retrieval import RepositoryRetrievalIndex
from backend.app.agent.semantic import SemanticRepositoryTools
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.models import NodeKind
from backend.app.graph.store import AnalysisSession


class SemanticRepositoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        backend = self.root / "backend"
        tests = self.root / "tests"
        backend.mkdir()
        tests.mkdir()
        (self.root / "README.md").write_text(
            "# Sample platform\n\nA repository analysis and onboarding service.\n",
            encoding="utf-8",
        )
        (self.root / "package.json").write_text(
            '{"scripts":{"dev":"vite"},"dependencies":{"react":"latest"}}',
            encoding="utf-8",
        )
        (backend / "repository.py").write_text(
            "def save(value: str) -> str:\n    return value\n",
            encoding="utf-8",
        )
        (backend / "service.py").write_text(
            "from backend.repository import save\n\n"
            "def run(value: str) -> str:\n    return save(value)\n",
            encoding="utf-8",
        )
        (backend / "main.py").write_text(
            "from backend.service import run\n\n"
            "def main() -> str:\n    return run('ready')\n",
            encoding="utf-8",
        )
        (tests / "test_service.py").write_text(
            "from backend.service import run\n\n"
            "def test_run() -> None:\n    assert run('ok') == 'ok'\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root).model_copy(
            update={"analysis_id": "semantic-tools"}
        )
        module_paths = {
            node.span.path
            for node in report.nodes
            if node.kind == NodeKind.MODULE and node.span
        }
        session = AnalysisSession(
            id="semantic-tools",
            root=self.root,
            report=report,
            source_paths=frozenset(
                module_paths | {"README.md", "package.json"}
            ),
        )
        self.index = RepositoryRetrievalIndex(session)
        self.tools = SemanticRepositoryTools(session, self.index)
        self.nodes = {node.qualified_name: node for node in report.nodes}

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_overview_features_configuration_and_entry_points(self) -> None:
        overview = self.tools.repository_overview()
        self.assertEqual(overview["files_analyzed"], 4)
        self.assertIn("README.md", overview["documentation"])
        self.assertIn("React", overview["framework_signals"])
        self.assertIn("Vite", overview["framework_signals"])
        self.assertTrue(overview["evidence"])

        features = self.tools.feature_evidence(5)
        self.assertGreaterEqual(features["candidate_count"], 3)
        self.assertTrue(features["evidence"])

        configuration = self.tools.project_configuration()
        self.assertIn("package.json", configuration["files"])
        self.assertIn("React", configuration["framework_signals"])

        entry_points = self.tools.entry_points()
        self.assertTrue(
            any(item["qualified_name"] == "backend.main.main" for item in entry_points["candidates"])
        )

    def test_architecture_file_symbol_tests_impact_and_diagnostics(self) -> None:
        architecture = self.tools.backend_architecture()
        self.assertTrue(architecture["layers"]["entrypoints"])
        self.assertTrue(architecture["layers"]["services"])
        self.assertTrue(architecture["layers"]["persistence"])

        structure = self.tools.file_structure("backend/service.py")
        self.assertTrue(any(item["name"] == "run" for item in structure["symbols"]))
        self.assertTrue(structure["external_relationships"])

        run = self.nodes["backend.service.run"]
        relationships = self.tools.symbol_relationships(run.id)
        self.assertTrue(relationships["incoming"])
        self.assertTrue(relationships["outgoing"])

        related_tests = self.tools.related_tests(node_id=run.id)
        self.assertTrue(
            any(item["path"] == "tests/test_service.py" for item in related_tests["tests"])
        )

        save = self.nodes["backend.repository.save"]
        impact = self.tools.dependency_impact(save.id, 3)
        impacted_names = {
            item["dependent"]["qualified_name"] for item in impact["dependents"]
        }
        self.assertIn("backend.service.run", impacted_names)
        self.assertIn("backend.main.main", impacted_names)

        diagnostics = self.tools.diagnostics()
        self.assertEqual(diagnostics["analysis_stats"]["parse_failures"], 0)


if __name__ == "__main__":
    unittest.main()
