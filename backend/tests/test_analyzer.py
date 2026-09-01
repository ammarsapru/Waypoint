from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.models import EdgeKind, EvidenceStatus, NodeKind


class RepositoryAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        package = self.root / "app"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "auth.py").write_text(
            "def verify(token: str) -> bool:\n"
            "    return bool(token)\n",
            encoding="utf-8",
        )
        (package / "routes.py").write_text(
            "from app.auth import verify\n\n"
            "def login(token: str) -> bool:\n"
            "    return verify(token)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_analysis_extracts_modules_definitions_imports_and_calls(self) -> None:
        report = RepositoryAnalyzer().analyze(self.root)
        qualified_names = {node.qualified_name for node in report.nodes}
        self.assertIn("app.auth", qualified_names)
        self.assertIn("app.auth.verify", qualified_names)
        self.assertIn("app.routes.login", qualified_names)

        import_edges = [
            edge for edge in report.edges if edge.kind == EdgeKind.IMPORTS
        ]
        call_edges = [
            edge for edge in report.edges if edge.kind == EdgeKind.MAY_CALL
        ]
        self.assertEqual(len(import_edges), 1)
        self.assertEqual(len(call_edges), 1)
        self.assertEqual(
            call_edges[0].evidence.status, EvidenceStatus.INFERRED
        )
        self.assertEqual(report.stats.files_parsed, 3)
        self.assertEqual(report.stats.parse_failures, 0)

    def test_dynamic_calls_remain_explicitly_unresolved(self) -> None:
        (self.root / "dynamic.py").write_text(
            "def dispatch(callback):\n"
            "    return callback()\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        unresolved_calls = [
            reference
            for reference in report.unresolved_references
            if reference.reference_kind == "call"
            and reference.reference == "callback"
        ]
        self.assertEqual(len(unresolved_calls), 1)
        self.assertEqual(
            unresolved_calls[0].evidence.status, EvidenceStatus.UNRESOLVED
        )

    def test_external_python_imports_include_package_metadata(self) -> None:
        (self.root / "external.py").write_text(
            "import httpx\nfrom pathlib import Path\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        external = {
            str(reference.metadata.get("package")): reference
            for reference in report.unresolved_references
            if reference.reference_kind == "import"
            and reference.metadata.get("external") is True
        }
        self.assertIn("httpx", external)
        self.assertIn("pathlib", external)
        self.assertEqual(external["httpx"].evidence.span.path, "external.py")

    def test_constructed_receiver_resolves_cross_file_class_and_method(self) -> None:
        (self.root / "app" / "service.py").write_text(
            "class UserService:\n"
            "    def create(self) -> str:\n"
            "        return 'created'\n",
            encoding="utf-8",
        )
        (self.root / "app" / "handler.py").write_text(
            "from app.service import UserService\n\n"
            "def handle() -> str:\n"
            "    service = UserService()\n"
            "    return service.create()\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        nodes = {node.id: node for node in report.nodes}
        relationships = {
            (
                edge.kind,
                nodes[edge.source].qualified_name,
                nodes[edge.target].qualified_name,
            )
            for edge in report.edges
        }
        self.assertIn(
            (
                EdgeKind.INSTANTIATES,
                "app.handler.handle",
                "app.service.UserService",
            ),
            relationships,
        )
        self.assertIn(
            (
                EdgeKind.MAY_CALL,
                "app.handler.handle",
                "app.service.UserService.create",
            ),
            relationships,
        )

    def test_unknown_receiver_recovers_one_unique_imported_member(self) -> None:
        (self.root / "app" / "service.py").write_text(
            "class UserService:\n"
            "    def create(self) -> str:\n"
            "        return 'created'\n",
            encoding="utf-8",
        )
        (self.root / "app" / "handler.py").write_text(
            "from app.service import UserService\n\n"
            "def handle(service):\n"
            "    return service.create()\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        nodes = {node.id: node for node in report.nodes}
        recovered = [
            edge
            for edge in report.edges
            if edge.kind == EdgeKind.MAY_CALL
            and nodes[edge.source].qualified_name == "app.handler.handle"
            and nodes[edge.target].qualified_name == "app.service.UserService.create"
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].evidence.confidence, 0.55)
        self.assertIn("Conservatively inferred", recovered[0].evidence.resolution)

    def test_syntax_errors_are_diagnostics_not_fatal_analysis_errors(self) -> None:
        (self.root / "broken.py").write_text(
            "def broken(:\n    pass\n", encoding="utf-8"
        )
        report = RepositoryAnalyzer().analyze(self.root)
        failures = [
            diagnostic
            for diagnostic in report.diagnostics
            if diagnostic.code == "parse_failed"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(report.stats.parse_failures, 1)

    def test_classes_and_methods_have_source_spans(self) -> None:
        (self.root / "service.py").write_text(
            "class Service:\n"
            "    def run(self) -> str:\n"
            "        return 'ok'\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        methods = [node for node in report.nodes if node.kind == NodeKind.METHOD]
        self.assertEqual(len(methods), 1)
        self.assertEqual(methods[0].qualified_name, "service.Service.run")
        self.assertEqual(methods[0].span.start_line, 2)

    def test_src_layout_uses_import_package_names(self) -> None:
        source_package = self.root / "src" / "sample"
        source_package.mkdir(parents=True)
        (source_package / "__init__.py").write_text("", encoding="utf-8")
        (source_package / "service.py").write_text(
            "def execute() -> str:\n"
            "    return 'ok'\n",
            encoding="utf-8",
        )
        (source_package / "api.py").write_text(
            "from sample.service import execute\n\n"
            "def run() -> str:\n"
            "    return execute()\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        qualified_names = {node.qualified_name for node in report.nodes}
        self.assertIn("sample.service", qualified_names)
        self.assertIn("sample.service.execute", qualified_names)
        self.assertNotIn("src.sample.service", qualified_names)
        internal_imports = [
            edge for edge in report.edges if edge.kind == EdgeKind.IMPORTS
        ]
        self.assertGreaterEqual(len(internal_imports), 2)

    def test_symbolic_python_file_outside_repository_is_not_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "secret.py"
            outside.write_text(
                "def outside_secret():\n    return 'private'\n",
                encoding="utf-8",
            )
            link = self.root / "linked.py"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"File symlinks are unavailable: {exc}")
            report = RepositoryAnalyzer().analyze(self.root)
        qualified_names = {node.qualified_name for node in report.nodes}
        self.assertNotIn("linked.outside_secret", qualified_names)
        self.assertEqual(report.stats.files_discovered, 3)

    def test_managed_clone_storage_is_excluded_from_parent_analysis(self) -> None:
        clone_root = self.root / ".waypoint-clones"
        cloned_repository = clone_root / "owner--repo--1234567890"
        cloned_repository.mkdir(parents=True)
        (cloned_repository / "foreign.py").write_text(
            "def should_not_be_indexed():\n    pass\n", encoding="utf-8"
        )
        analyzer = RepositoryAnalyzer()
        with patch(
            "backend.app.graph.analyzer.settings",
            SimpleNamespace(clone_root=clone_root),
        ):
            discovered = analyzer.discover_python_files(self.root)
        self.assertNotIn(cloned_repository / "foreign.py", discovered)
        self.assertEqual(len(discovered), 3)


if __name__ == "__main__":
    unittest.main()
