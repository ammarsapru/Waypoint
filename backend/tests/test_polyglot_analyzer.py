from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.agent.retrieval import RepositoryRetrievalIndex
from backend.app.api.routes import _source_language
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.models import EdgeKind, EvidenceStatus, NodeKind
from backend.app.graph.store import AnalysisSession


class PolyglotRepositoryAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_typescript_javascript_and_tsx_emit_shared_graph_schema(self) -> None:
        source = self.root / "src"
        source.mkdir()
        (source / "math.js").write_text(
            "export function increment(value) { return value + 1; }\n",
            encoding="utf-8",
        )
        (source / "service.ts").write_text(
            "import { increment as addOne } from './math.js';\n"
            "export class CounterService {\n"
            "  run(value: number): number { return addOne(value); }\n"
            "}\n"
            "export const calculate = (value: number) => addOne(value);\n",
            encoding="utf-8",
        )
        (source / "Card.tsx").write_text(
            "export interface CardProps { name: string }\n"
            "export function Card({ name }: CardProps) {\n"
            "  const label = () => name.toUpperCase();\n"
            "  return <button>{label()}</button>;\n"
            "}\n",
            encoding="utf-8",
        )

        report = RepositoryAnalyzer().analyze(self.root)
        names = {node.qualified_name for node in report.nodes}
        self.assertIn("src.math.increment", names)
        self.assertIn("src.service.CounterService", names)
        self.assertIn("src.service.CounterService.run", names)
        self.assertIn("src.service.calculate", names)
        self.assertIn("src.Card.CardProps", names)
        self.assertIn("src.Card.Card", names)
        languages = {
            node.metadata.get("language")
            for node in report.nodes
            if node.kind == NodeKind.MODULE
        }
        self.assertEqual(languages, {"javascript", "typescript", "tsx"})
        imports = [edge for edge in report.edges if edge.kind == EdgeKind.IMPORTS]
        calls = [edge for edge in report.edges if edge.kind == EdgeKind.MAY_CALL]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0].evidence.status, EvidenceStatus.VERIFIED)
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(
            all(edge.evidence.status == EvidenceStatus.INFERRED for edge in calls)
        )
        self.assertEqual(report.stats.files_discovered, 3)
        self.assertEqual(report.stats.files_parsed, 3)

    def test_commonjs_require_resolves_destructured_alias_and_call(self) -> None:
        (self.root / "util.js").write_text(
            "function helper(value) { return value; }\nmodule.exports = { helper };\n",
            encoding="utf-8",
        )
        (self.root / "main.cjs").write_text(
            "const { helper: runHelper } = require('./util');\n"
            "function main() { return runHelper('ok'); }\n",
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
            if edge.kind != EdgeKind.CONTAINS
        }
        self.assertIn((EdgeKind.IMPORTS, "main", "util"), relationships)
        self.assertIn((EdgeKind.MAY_CALL, "main.main", "util.helper"), relationships)

    def test_external_javascript_packages_are_normalized_and_relative_misses_are_separate(self) -> None:
        (self.root / "app.ts").write_text(
            "import React from 'react/jsx-runtime';\n"
            "import { tool } from '@scope/package/subpath';\n"
            "import { missing } from './missing';\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        external = {
            str(reference.metadata.get("package"))
            for reference in report.unresolved_references
            if reference.reference_kind == "import"
            and reference.metadata.get("external") is True
        }
        self.assertEqual(external, {"react", "@scope/package"})
        relative = next(
            reference
            for reference in report.unresolved_references
            if reference.reference == "./missing"
        )
        self.assertFalse(relative.metadata["external"])

    def test_typescript_construction_resolves_instance_method_across_files(self) -> None:
        (self.root / "service.ts").write_text(
            "export class UserService { create(): string { return 'ok'; } }\n",
            encoding="utf-8",
        )
        (self.root / "handler.ts").write_text(
            "import { UserService } from './service';\n"
            "export function handle() {\n"
            "  const service = new UserService();\n"
            "  return service.create();\n"
            "}\n",
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
            if edge.kind != EdgeKind.CONTAINS
        }
        self.assertIn(
            (EdgeKind.INSTANTIATES, "handler.handle", "service.UserService"),
            relationships,
        )
        self.assertIn(
            (EdgeKind.MAY_CALL, "handler.handle", "service.UserService.create"),
            relationships,
        )

    def test_typescript_unknown_receiver_recovers_unique_imported_member(self) -> None:
        (self.root / "service.ts").write_text(
            "export class UserService { create(): string { return 'ok'; } }\n",
            encoding="utf-8",
        )
        (self.root / "handler.ts").write_text(
            "import { UserService } from './service';\n"
            "export function handle(service: unknown) {\n"
            "  return service.create();\n"
            "}\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        nodes = {node.id: node for node in report.nodes}
        recovered = [
            edge
            for edge in report.edges
            if edge.kind == EdgeKind.MAY_CALL
            and nodes[edge.source].qualified_name == "handler.handle"
            and nodes[edge.target].qualified_name == "service.UserService.create"
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].evidence.confidence, 0.55)
        self.assertIn("Conservatively inferred", recovered[0].evidence.resolution)

    def test_java_packages_classes_methods_imports_and_typed_receivers(self) -> None:
        service = self.root / "src" / "main" / "java" / "com" / "example" / "service"
        api = self.root / "src" / "main" / "java" / "com" / "example" / "api"
        service.mkdir(parents=True)
        api.mkdir(parents=True)
        (service / "UserService.java").write_text(
            "package com.example.service;\n"
            "public class UserService {\n"
            "  public String find(String id) { return id; }\n"
            "}\n",
            encoding="utf-8",
        )
        (api / "UserController.java").write_text(
            "package com.example.api;\n"
            "import com.example.service.UserService;\n"
            "public class UserController {\n"
            "  private final UserService service;\n"
            "  public UserController(UserService service) { this.service = service; }\n"
            "  public String get(String id) { return service.find(id); }\n"
            "}\n",
            encoding="utf-8",
        )

        report = RepositoryAnalyzer().analyze(self.root)
        nodes = {node.id: node for node in report.nodes}
        names = {node.qualified_name for node in report.nodes}
        self.assertIn(
            "com.example.api.UserController.UserController.get",
            names,
        )
        self.assertIn(
            "com.example.service.UserService.UserService.find",
            names,
        )
        imports = [edge for edge in report.edges if edge.kind == EdgeKind.IMPORTS]
        calls = [edge for edge in report.edges if edge.kind == EdgeKind.MAY_CALL]
        self.assertEqual(len(imports), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            nodes[calls[0].target].qualified_name,
            "com.example.service.UserService.UserService.find",
        )
        self.assertIn("declared static type", calls[0].evidence.resolution)

    def test_java_object_creation_emits_instantiation_edge(self) -> None:
        service = self.root / "src" / "main" / "java" / "com" / "example" / "service"
        api = self.root / "src" / "main" / "java" / "com" / "example" / "api"
        service.mkdir(parents=True)
        api.mkdir(parents=True)
        (service / "Worker.java").write_text(
            "package com.example.service;\n"
            "public class Worker { public void run() {} }\n",
            encoding="utf-8",
        )
        (api / "Main.java").write_text(
            "package com.example.api;\n"
            "import com.example.service.Worker;\n"
            "public class Main {\n"
            "  public void start() { Worker worker = new Worker(); worker.run(); }\n"
            "}\n",
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
                "com.example.api.Main.Main.start",
                "com.example.service.Worker.Worker",
            ),
            relationships,
        )
        self.assertIn(
            (
                EdgeKind.MAY_CALL,
                "com.example.api.Main.Main.start",
                "com.example.service.Worker.Worker.run",
            ),
            relationships,
        )

    def test_external_java_imports_are_grouped_by_package(self) -> None:
        (self.root / "Main.java").write_text(
            "import java.util.List;\n"
            "import org.springframework.web.bind.annotation.GetMapping;\n"
            "class Main {}\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        external = {
            str(reference.metadata.get("package"))
            for reference in report.unresolved_references
            if reference.reference_kind == "import"
            and reference.metadata.get("external") is True
        }
        self.assertEqual(
            external,
            {"java.util", "org.springframework.web.bind.annotation"},
        )

    def test_mixed_repository_counts_all_supported_sources_and_skips_dependencies(self) -> None:
        (self.root / "app.py").write_text("def python_entry():\n    return True\n", encoding="utf-8")
        (self.root / "web.ts").write_text("export function webEntry() { return true; }\n", encoding="utf-8")
        (self.root / "Main.java").write_text("class Main { static void run() {} }\n", encoding="utf-8")
        dependencies = self.root / "node_modules" / "ignored"
        dependencies.mkdir(parents=True)
        (dependencies / "vendor.js").write_text("export const vendor = true;\n", encoding="utf-8")

        report = RepositoryAnalyzer().analyze(self.root)
        self.assertEqual(report.stats.files_discovered, 3)
        self.assertEqual(report.stats.files_parsed, 3)
        module_paths = {
            node.span.path
            for node in report.nodes
            if node.kind == NodeKind.MODULE and node.span
        }
        self.assertEqual(module_paths, {"app.py", "web.ts", "Main.java"})

    def test_tree_sitter_recovery_is_visible_without_dropping_entire_file(self) -> None:
        (self.root / "broken.ts").write_text(
            "export class Broken { run(: string) { return true } }\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        self.assertEqual(report.stats.files_parsed, 1)
        self.assertEqual(report.stats.parse_failures, 0)
        self.assertTrue(
            any(item.code == "parse_recovered" for item in report.diagnostics)
        )

    def test_index_module_default_export_and_reexport_are_resolved(self) -> None:
        feature = self.root / "feature"
        feature.mkdir()
        (feature / "helper.ts").write_text(
            "export function helper() { return true; }\n",
            encoding="utf-8",
        )
        (feature / "index.ts").write_text(
            "import { helper } from './helper';\n"
            "export { helper } from './helper';\n"
            "export default class { run() { return helper(); } }\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        names = {node.qualified_name for node in report.nodes}
        self.assertIn("feature.default", names)
        self.assertIn("feature.default.run", names)
        imports = [edge for edge in report.edges if edge.kind == EdgeKind.IMPORTS]
        self.assertEqual(len(imports), 2)
        self.assertTrue(
            any(edge.kind == EdgeKind.MAY_CALL for edge in report.edges)
        )

    def test_typescript_source_is_symbol_chunked_and_uses_editor_language(self) -> None:
        source = self.root / "service.ts"
        source.write_text(
            "export class CounterService { run(): number { return 1; } }\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root).model_copy(
            update={"analysis_id": "polyglot-test"}
        )
        session = AnalysisSession(
            id="polyglot-test",
            root=self.root,
            report=report,
            source_paths=frozenset({"service.ts"}),
        )
        index = RepositoryRetrievalIndex(session)
        results = index.search("CounterService")
        self.assertTrue(results)
        self.assertTrue(any(item["kind"] == "class" for item in results))
        self.assertEqual(_source_language(source), "typescript")

    def test_html_and_css_emit_symbols_and_local_asset_edges(self) -> None:
        (self.root / "app.js").write_text(
            "document.querySelector('#app');\n", encoding="utf-8"
        )
        (self.root / "theme.css").write_text(
            ".shell, #app { color: red; }\n", encoding="utf-8"
        )
        (self.root / "index.html").write_text(
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="theme.css">'
            "</head><body>"
            '<main id="app" class="shell"><script src="app.js"></script></main>'
            "</body></html>\n",
            encoding="utf-8",
        )

        report = RepositoryAnalyzer().analyze(self.root)
        modules = {
            node.span.path: node
            for node in report.nodes
            if node.kind == NodeKind.MODULE and node.span
        }
        self.assertEqual(set(modules), {"app.js", "theme.css", "index.html"})
        self.assertEqual(modules["index.html"].metadata["language"], "html")
        self.assertEqual(modules["theme.css"].metadata["language"], "css")
        symbols = {node.name for node in report.nodes if node.kind == NodeKind.CLASS}
        self.assertIn("#app", symbols)
        self.assertIn(".shell", symbols)
        imports = {
            (edge.source, edge.target)
            for edge in report.edges
            if edge.kind == EdgeKind.IMPORTS
        }
        self.assertIn((modules["index.html"].id, modules["theme.css"].id), imports)
        self.assertIn((modules["index.html"].id, modules["app.js"].id), imports)
        self.assertEqual(_source_language(self.root / "index.html"), "html")
        self.assertEqual(_source_language(self.root / "theme.css"), "css")


if __name__ == "__main__":
    unittest.main()
