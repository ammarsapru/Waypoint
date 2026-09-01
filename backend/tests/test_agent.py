from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from backend.app.agent.retrieval import RepositoryIndexStore, RepositoryRetrievalIndex
from backend.app.agent.service import AgentExecutionError, RepositoryAgentService, TOOLS
from backend.app.agent.semantic import SemanticRepositoryTools
from backend.app.agent.onboarding import RepositoryOnboardingAgent
from backend.app.agent.issues import RepositoryIssueAgent, ai_findings
from backend.app.agent.provider import model_provider_router
from backend.app.agent.memory import ConversationStore
from backend.app.graph.analyzer import RepositoryAnalyzer
from backend.app.graph.store import AnalysisSessionStore, analysis_sessions
from backend.app.indexing import repository_snapshot
from backend.app.issues.models import GitHubIssue
from backend.app.onboarding.models import DeveloperRole, GroundedQuestion, TourRequest


class RepositoryAgentTests(unittest.TestCase):
    def test_chunk_identity_distinguishes_symbols_that_share_a_span(self) -> None:
        first = RepositoryRetrievalIndex._chunk_id(
            "src/app.ts", 10, 12, "function", "symbol-one"
        )
        second = RepositoryRetrievalIndex._chunk_id(
            "src/app.ts", 10, 12, "function", "symbol-two"
        )
        self.assertNotEqual(first, second)

    def test_repository_snapshot_includes_non_ast_text_context(self) -> None:
        (self.root / "setup.sh").write_text("#!/usr/bin/env bash\necho ready\n")
        (self.root / "rules.csv").write_text("rule,status\nA,active\n")
        snapshot = repository_snapshot(self.root)
        self.assertIn("setup.sh", snapshot.paths)
        self.assertIn("rules.csv", snapshot.paths)

    def test_semantic_tool_cache_is_shared_for_one_repository_revision(self) -> None:
        # A persistent SQLite connection (see PersistentCodeIndex._connect) must be
        # closed explicitly before tearDown deletes self.root - unittest runs
        # addCleanup callbacks *after* tearDown, too late to release the file handle
        # before the Windows rmtree.
        index = RepositoryRetrievalIndex(
            self.session, database_path=self.root / "semantic-cache.sqlite3"
        )
        try:
            first_service = SemanticRepositoryTools(self.session, index)
            second_service = SemanticRepositoryTools(self.session, index)

            first_result = first_service.repository_overview()
            second_result = second_service.repository_overview()

            self.assertIs(first_result, second_result)
            self.assertEqual(len(index.semantic_result_cache), 1)
        finally:
            index.close()

    def test_snapshot_crawls_agent_skills_and_collapses_verified_aliases(self) -> None:
        canonical = self.root / "skills" / "demo"
        agent_skills = self.root / ".agents" / "skills"
        toolkit = agent_skills / "toolkit"
        canonical.mkdir(parents=True)
        toolkit.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("# Shared skill\n", encoding="utf-8")
        (agent_skills / "demo").write_text("../../skills/demo\n", encoding="utf-8")
        (toolkit / "README.md").write_text("# Shared skill\n", encoding="utf-8")
        (toolkit / "runner.py").write_text("def run():\n    return True\n", encoding="utf-8")

        snapshot = repository_snapshot(self.root)
        aliases = snapshot.alias_map
        self.assertIn(".agents/skills/toolkit/runner.py", snapshot.paths)
        self.assertEqual(
            aliases[".agents/skills/demo"].canonical_path,
            "skills/demo",
        )
        self.assertEqual(aliases[".agents/skills/demo"].target_kind, "directory")
        self.assertEqual(
            aliases[".agents/skills/toolkit/README.md"].canonical_path,
            "skills/demo/SKILL.md",
        )
        self.assertEqual(
            aliases[".agents/skills/toolkit/README.md"].reason,
            "exact_content_duplicate",
        )

        report = RepositoryAnalyzer().analyze(self.root)
        self.assertTrue(any(
            node.span
            and node.span.path == ".agents/skills/toolkit/runner.py"
            for node in report.nodes
        ))
        stored = analysis_sessions.create(self.root, report)
        session = analysis_sessions.get(stored.analysis_id)
        # A persistent SQLite connection (see PersistentCodeIndex._connect) must be
        # closed explicitly before tearDown deletes self.root.
        index = RepositoryRetrievalIndex(
            session, database_path=self.root / "aliases.sqlite3"
        )
        try:
            tree = index.tree(limit=100)
            self.assertGreaterEqual(tree["alias_count"], 2)
            duplicate = index.read(".agents/skills/toolkit/README.md")
            self.assertEqual(duplicate["canonical_path"], "skills/demo/SKILL.md")
            self.assertIn("Shared skill", duplicate["content"])
        finally:
            index.close()

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "README.md").write_text(
            "# Greeting service\n\nA small API that returns greetings.\n",
            encoding="utf-8",
        )
        (self.root / "app.py").write_text(
            "def greet(name: str) -> str:\n"
            "    return f'Hello {name}'\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        stored = analysis_sessions.create(self.root, report)
        self.session = analysis_sessions.get(stored.analysis_id)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_retrieval_searches_symbols_and_reads_bounded_lines(self) -> None:
        index = RepositoryRetrievalIndex(self.session)
        results = index.search("greet", limit=5)
        self.assertIn("retrieval_confidence", results[0])
        self.assertIn("retrieval_requires_followup", results[0])
        self.assertIn("query_term_coverage", results[0])
        self.assertTrue(results)
        self.assertEqual(results[0]["path"], "app.py")
        source = index.read("app.py", 1, 500)
        self.assertEqual(source["end_line"], 2)
        self.assertIn("1 | def greet", source["content"])
        with self.assertRaises(ValueError):
            index.read("../outside.py")

    def test_chunk_build_skips_a_file_that_fails_to_decode_instead_of_aborting(self) -> None:
        # UTF-16 with a BOM: tokenize.open() reports this as SyntaxError (not
        # UnicodeError), since it decodes the first two lines as UTF-8 just to look
        # for a PEP 263 encoding cookie before it can even try the declared/real
        # encoding. _safe_lines() must still turn that into a ValueError so
        # _build_chunks() skips this one file instead of the whole index build
        # blowing up on it.
        (self.root / "bad_encoding.py").write_text(
            "def broken():\n    return 1\n", encoding="utf-16"
        )
        report = RepositoryAnalyzer().analyze(self.root)
        stored = analysis_sessions.create(self.root, report)
        session = analysis_sessions.get(stored.analysis_id)
        index = RepositoryRetrievalIndex(
            session, database_path=self.root / "bad-encoding.sqlite3"
        )
        try:
            results = index.search("greet", limit=5)
            self.assertTrue(any(item["path"] == "app.py" for item in results))
            self.assertFalse(
                any(chunk.path == "bad_encoding.py" for chunk in index.chunks)
            )
        finally:
            index.close()

    def test_retrieval_index_persists_revision_and_supports_filters(self) -> None:
        # Persistent SQLite connections (see PersistentCodeIndex._connect) must be
        # closed explicitly before tearDown deletes self.root.
        database = self.root / "index" / "code.sqlite3"
        first = RepositoryRetrievalIndex(self.session, database_path=database)
        try:
            status = first.status()
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["files"], 2)
            self.assertGreaterEqual(status["symbols"], 3)
            self.assertEqual(status["vectors"], status["chunks"])
            self.assertTrue(first.search("greeting service", languages=["documentation"]))
            cached_vectors = first.storage._vector_cache[first.revision_id]
            first.search("greet", limit=5)
            self.assertIs(first.storage._vector_cache[first.revision_id], cached_vectors)
            self.assertEqual(
                first.search("greet", path_prefixes=["missing/"], limit=5), []
            )

            with patch.object(
                RepositoryRetrievalIndex,
                "_build_chunks",
                side_effect=AssertionError("persistent chunks should be reused"),
            ):
                reopened = RepositoryRetrievalIndex(
                    self.session, database_path=database, snapshot=first.snapshot
                )
            try:
                self.assertEqual(reopened.revision_id, first.revision_id)
                self.assertEqual(len(reopened.chunks), len(first.chunks))
            finally:
                reopened.close()
        finally:
            first.close()

    def test_hybrid_search_handles_related_word_forms(self) -> None:
        # A persistent SQLite connection (see PersistentCodeIndex._connect) must be
        # closed explicitly before tearDown deletes self.root.
        index = RepositoryRetrievalIndex(
            self.session, database_path=self.root / "hybrid.sqlite3"
        )
        try:
            results = index.search("greetings", limit=5)
            self.assertTrue(any(item["path"] == "app.py" for item in results))
        finally:
            index.close()

    def test_generated_evaluation_artifacts_do_not_pollute_code_questions(self) -> None:
        generated = self.root / "verification" / "results"
        generated.mkdir(parents=True)
        (generated / "latency-report.md").write_text(
            "# Generated report\n\nquantumlatencytoken benchmark result\n",
            encoding="utf-8",
        )
        report = RepositoryAnalyzer().analyze(self.root)
        stored = analysis_sessions.create(self.root, report)
        session = analysis_sessions.get(stored.analysis_id)
        # A persistent SQLite connection (see PersistentCodeIndex._connect) must be
        # closed explicitly before tearDown deletes self.root.
        index = RepositoryRetrievalIndex(
            session, database_path=self.root / "generated-filter.sqlite3"
        )
        try:
            results = index.search("quantumlatencytoken implementation")
            self.assertTrue(
                all(item["path"] != "verification/results/latency-report.md" for item in results)
            )
            explicit = index.search("quantumlatencytoken benchmark report")
            self.assertTrue(
                any(item["path"].endswith("latency-report.md") for item in explicit)
            )
        finally:
            index.close()

    def test_index_store_refreshes_changed_repository_under_same_analysis(self) -> None:
        store = RepositoryIndexStore(max_indexes=2)
        self.addCleanup(store.close)
        first = store.get(self.session)
        (self.root / "app.py").write_text(
            "def greet(name: str) -> str:\n"
            "    return f'Hello {name}'\n\n"
            "def farewell(name: str) -> str:\n"
            "    return f'Goodbye {name}'\n",
            encoding="utf-8",
        )
        second = store.get(self.session)
        self.assertNotEqual(second.revision_id, first.revision_id)
        self.assertTrue(second.find_symbols("farewell")["results"])
        self.assertEqual(second.session.id, self.session.id)

    def test_agent_uses_tools_and_returns_only_validated_citations(self) -> None:
        search = SimpleNamespace(
            type="tool_use",
            id="tool-search",
            name="search_repository",
            input={"query": "greet", "limit": 5},
        )
        submit = SimpleNamespace(
            type="tool_use",
            id="tool-submit",
            name="submit_answer",
            input={
                "answer": "The service exposes a greet function.",
                "basis": "The function definition and return statement support this.",
                "refused": False,
                "citations": [
                    {
                        "path": "app.py",
                        "start_line": 1,
                        "end_line": 2,
                        "title": "Greeting implementation",
                        "relevance": "Defines and returns the greeting.",
                    }
                ],
                "suggested_questions": ["Who calls greet?"],
            },
        )
        responses = [
            SimpleNamespace(
                content=[search],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
            SimpleNamespace(
                content=[submit],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=20, output_tokens=10),
            ),
        ]
        client = SimpleNamespace(
            messages=SimpleNamespace(create=Mock(side_effect=responses))
        )
        fake_settings = SimpleNamespace(
            anthropic_api_key="configured",
            model_name="claude-sonnet-5",
            agent_max_output_tokens=4000,
            agent_max_tool_rounds=5,
        )
        agent = RepositoryAgentService(self.session)
        with patch("backend.app.agent.service.settings", fake_settings), patch.object(
            agent, "_client", return_value=client
        ):
            answer = agent.answer(GroundedQuestion(question="What does this service do?"))
        self.assertEqual(answer.provider, "claude-sonnet-5")
        self.assertEqual(
            [activity.tool for activity in answer.tool_trace],
            ["search_repository"],
        )
        self.assertIsNotNone(answer.conversation_id)
        self.assertEqual([item.span.path for item in answer.citations], ["app.py"])
        self.assertEqual(answer.inspected_file_count, 2)
        self.assertNotIn("README.md", [item.span.path for item in answer.citations])
        self.assertEqual(client.messages.create.call_count, 2)

    def _sample_github_issue(self, number: int = 7) -> GitHubIssue:
        return GitHubIssue(
            number=number,
            title="Greeting drops the name argument",
            body="Calling greet() with a name sometimes returns just 'Hello'.",
            state="open",
            url=f"https://github.com/example/project/issues/{number}",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )

    def test_investigate_issue_returns_verdict_linked_to_issue_number(self) -> None:
        search = SimpleNamespace(
            type="tool_use", id="tool-search", name="search_repository",
            input={"query": "greet", "limit": 5},
        )
        submit = SimpleNamespace(
            type="tool_use", id="tool-submit", name="submit_issue_investigation",
            input={
                "verdict": "confirmed",
                "severity": "medium",
                "category": "correctness",
                "title": "greet() does not always interpolate the name",
                "explanation": "The f-string substitution is conditional on input shape.",
                "confidence": 0.75,
                "evidence": [{"path": "app.py", "start_line": 1, "end_line": 2}],
                "suggested_approach": ["Add a regression test", "Fix the interpolation"],
                "recommended_fix": "Guard the f-string substitution with an explicit "
                "name check before formatting; add a regression test covering an "
                "empty/whitespace name input.",
            },
        )
        responses = [
            SimpleNamespace(
                content=[search], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
            SimpleNamespace(
                content=[submit], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=20, output_tokens=10),
            ),
        ]
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(side_effect=responses)))
        fake_settings = SimpleNamespace(
            anthropic_api_key="configured", model_name="claude-sonnet-5",
            agent_max_output_tokens=4000, agent_max_tool_rounds=5,
            investigation_rounds=3, synthesis_max_attempts=2,
        )
        issue_agent = RepositoryIssueAgent(self.session)
        with patch("backend.app.agent.issues.settings", fake_settings), patch.object(
            issue_agent.agent, "_client", return_value=client
        ):
            finding = issue_agent.investigate_issue(self._sample_github_issue(7))
        self.assertEqual(finding.github_issue_number, 7)
        self.assertEqual(finding.verdict, "confirmed")
        self.assertEqual(finding.source, "ai_finding")
        self.assertEqual([span.path for span in finding.evidence], ["app.py"])
        self.assertIn("regression test", finding.recommended_fix)

    def test_investigate_issue_recovers_a_real_but_unread_citation(self) -> None:
        # The model cites app.py:L1-L2 directly, without ever calling read_source or
        # search_repository first - `inspected` starts empty. Before bounded evidence
        # recovery existed, this would be rejected outright even though the citation
        # is genuinely real and in-bounds, because tool_choice is locked to the
        # submission tool once forced and the model has no way to go verify it. This
        # is the exact failure mode reported live: a real citation, just not yet
        # tracked as inspected.
        submit = SimpleNamespace(
            type="tool_use", id="tool-submit", name="submit_issue_investigation",
            input={
                "verdict": "confirmed",
                "severity": "medium",
                "category": "correctness",
                "title": "greet() drops the name in some cases",
                "explanation": "See the return statement.",
                "confidence": 0.7,
                "evidence": [{"path": "app.py", "start_line": 1, "end_line": 2}],
                "suggested_approach": ["Add a test", "Fix the bug"],
                "recommended_fix": "Guard against an empty name before formatting.",
            },
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=SimpleNamespace(
            content=[submit], stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        ))))
        fake_settings = SimpleNamespace(
            anthropic_api_key="configured", model_name="claude-sonnet-5",
            agent_max_output_tokens=4000, agent_max_tool_rounds=5,
            investigation_rounds=3, synthesis_max_attempts=2,
        )
        issue_agent = RepositoryIssueAgent(self.session)
        with patch("backend.app.agent.issues.settings", fake_settings), patch.object(
            issue_agent.agent, "_client", return_value=client
        ):
            finding = issue_agent.investigate_issue(self._sample_github_issue(7))
        self.assertEqual(finding.verdict, "confirmed")
        self.assertEqual([span.path for span in finding.evidence], ["app.py"])
        # Recovery happens server-side with no extra model round-trip - one call only.
        self.assertEqual(client.messages.create.call_count, 1)

    def test_investigate_issue_still_rejects_a_genuinely_out_of_range_citation(self) -> None:
        # app.py is 2 lines long in this fixture - citing L50-L60 is not recoverable
        # (the file simply doesn't have those lines), so recovery must not turn this
        # into a false accept.
        submit = SimpleNamespace(
            type="tool_use", id="tool-submit", name="submit_issue_investigation",
            input={
                "verdict": "refuted",
                "severity": "low",
                "category": "correctness",
                "title": "Fabricated citation",
                "explanation": "Cites a range the file does not have.",
                "confidence": 0.9,
                "evidence": [{"path": "app.py", "start_line": 50, "end_line": 60}],
                "suggested_approach": ["n/a", "n/a"],
                "recommended_fix": "n/a",
            },
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=SimpleNamespace(
            content=[submit], stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        ))))
        fake_settings = SimpleNamespace(
            anthropic_api_key="configured", model_name="claude-sonnet-5",
            agent_max_output_tokens=4000, agent_max_tool_rounds=1,
            investigation_rounds=3, synthesis_max_attempts=1,
        )
        issue_agent = RepositoryIssueAgent(self.session)
        with patch("backend.app.agent.issues.settings", fake_settings), patch.object(
            issue_agent.agent, "_client", return_value=client
        ):
            with self.assertRaises(AgentExecutionError) as raised:
                issue_agent.investigate_issue(self._sample_github_issue(7))
        self.assertIn("Finding evidence was not inspected", str(raised.exception))

    def test_investigate_issue_rejects_uninspected_evidence(self) -> None:
        submit = SimpleNamespace(
            type="tool_use", id="tool-submit", name="submit_issue_investigation",
            input={
                "verdict": "refuted",
                "severity": "low",
                "category": "correctness",
                "title": "Fabricated citation",
                "explanation": "Cites a range never actually read.",
                "confidence": 0.9,
                "evidence": [{"path": "app.py", "start_line": 50, "end_line": 60}],
                "suggested_approach": ["n/a", "n/a"],
                "recommended_fix": "n/a",
            },
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=SimpleNamespace(
            content=[submit], stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        ))))
        fake_settings = SimpleNamespace(
            anthropic_api_key="configured", model_name="claude-sonnet-5",
            agent_max_output_tokens=4000, agent_max_tool_rounds=1,
            investigation_rounds=3, synthesis_max_attempts=1,
        )
        issue_agent = RepositoryIssueAgent(self.session)
        with patch("backend.app.agent.issues.settings", fake_settings), patch.object(
            issue_agent.agent, "_client", return_value=client
        ):
            with self.assertRaises(AgentExecutionError):
                issue_agent.investigate_issue(self._sample_github_issue(7))

    def test_investigate_accumulates_new_findings_without_dropping_previous_ones(self) -> None:
        def submission(title: str) -> SimpleNamespace:
            return SimpleNamespace(
                type="tool_use", id=f"tool-submit-{title}", name="submit_issue_findings",
                input={"findings": [{
                    "severity": "low",
                    "category": "maintainability",
                    "title": title,
                    "explanation": "Detected via a fake round-trip.",
                    "confidence": 0.6,
                    "evidence": [{"path": "app.py", "start_line": 1, "end_line": 2}],
                    "suggested_approach": ["Look into it", "Confirm with a test"],
                    "recommended_fix": "Investigate further before changing anything.",
                }]},
            )

        fake_settings = SimpleNamespace(
            anthropic_api_key="configured", model_name="claude-sonnet-5",
            agent_max_output_tokens=4000, agent_max_tool_rounds=5,
            investigation_rounds=3, synthesis_max_attempts=2,
        )
        issue_agent = RepositoryIssueAgent(self.session)

        search = SimpleNamespace(
            type="tool_use", id="tool-search", name="search_repository",
            input={"query": "greet", "limit": 5},
        )
        first_responses = [
            SimpleNamespace(
                content=[search], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
            SimpleNamespace(
                content=[submission("First finding")], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=20, output_tokens=10),
            ),
        ]
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(side_effect=first_responses)))
        with patch("backend.app.agent.issues.settings", fake_settings), patch.object(
            issue_agent.agent, "_client", return_value=client
        ):
            first_batch = issue_agent.investigate([])
        self.assertEqual([item.title for item in first_batch], ["First finding"])
        self.assertEqual(
            [item.title for item in ai_findings.get(self.session.id)], ["First finding"]
        )

        second_responses = [
            SimpleNamespace(
                content=[search], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
            SimpleNamespace(
                content=[submission("Second finding")], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=20, output_tokens=10),
            ),
        ]
        client.messages.create = Mock(side_effect=second_responses)
        with patch("backend.app.agent.issues.settings", fake_settings), patch.object(
            issue_agent.agent, "_client", return_value=client
        ):
            second_batch = issue_agent.investigate([])
        self.assertEqual([item.title for item in second_batch], ["Second finding"])
        self.assertEqual(
            [item.title for item in ai_findings.get(self.session.id)],
            ["First finding", "Second finding"],
        )

    def test_focused_symbol_question_preloads_usage_evidence(self) -> None:
        symbol = next(
            node for node in self.session.report.nodes if node.name == "greet"
        )
        submit = SimpleNamespace(
            type="tool_use",
            id="tool-submit",
            name="submit_answer",
            input={
                "answer": "`greet` implements the repository greeting behavior.",
                "basis": "The selected symbol and its source define the behavior.",
                "refused": False,
                "citations": [{
                    "path": "app.py",
                    "start_line": 1,
                    "end_line": 2,
                    "title": "Selected greeting symbol",
                    "relevance": "Defines the selected function.",
                }],
                "suggested_questions": [],
            },
        )
        client = SimpleNamespace(
            messages=SimpleNamespace(
                create=Mock(return_value=SimpleNamespace(
                    content=[submit],
                    stop_reason="tool_use",
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                ))
            )
        )
        fake_settings = SimpleNamespace(
            anthropic_api_key="configured",
            model_name="claude-sonnet-5",
            agent_max_output_tokens=4000,
            agent_max_tool_rounds=5,
        )
        agent = RepositoryAgentService(self.session)
        with patch("backend.app.agent.service.settings", fake_settings), patch.object(
            agent, "_client", return_value=client
        ):
            answer = agent.answer(GroundedQuestion(
                question="How is this used?",
                focus_node_id=symbol.id,
                conversation_scope="inspector",
            ))
        dispatched = next(
            message["content"]
            for message in client.messages.create.call_args.kwargs["messages"]
            if message.get("role") == "user"
            and "SELECTED SYMBOL USAGE" in str(message.get("content"))
        )
        self.assertIn("SELECTED SYMBOL USAGE", dispatched)
        self.assertIn(symbol.qualified_name, dispatched)
        self.assertEqual(answer.tool_trace[0].tool, "get_symbol_relationships")
        self.assertEqual(answer.citations[0].span.path, "app.py")

    def test_precise_tools_return_bounded_source_evidence(self) -> None:
        tool_names = {tool["name"] for tool in TOOLS}
        self.assertTrue(
            {
                "get_repository_overview",
                "get_feature_evidence",
                "find_entry_points",
                "get_backend_architecture",
                "get_file_structure",
                "get_symbol_relationships",
                "find_related_tests",
                "get_dependency_impact",
                "get_project_configuration",
                "get_analysis_diagnostics",
                "find_symbols",
                "get_index_status",
            }.issubset(tool_names)
        )
        agent = RepositoryAgentService(self.session)
        overview = agent.semantic.repository_overview()
        self.assertEqual(overview["repository_name"], self.root.name)
        self.assertEqual(overview["languages"], {"python": 1})
        self.assertTrue(overview["evidence"])
        entry_points = agent.semantic.entry_points()
        self.assertTrue(
            any(item["path"] == "app.py" for item in entry_points["candidates"])
        )
        structure = agent.semantic.file_structure("app.py")
        self.assertTrue(
            any(item["name"] == "greet" for item in structure["symbols"])
        )

    def test_broad_question_runs_precise_tools_in_one_round_then_forces_answer(self) -> None:
        overview_tool = SimpleNamespace(
            type="tool_use",
            id="tool-overview",
            name="get_repository_overview",
            input={},
        )
        entry_tool = SimpleNamespace(
            type="tool_use",
            id="tool-entry",
            name="find_entry_points",
            input={"limit": 5},
        )
        submit = SimpleNamespace(
            type="tool_use",
            id="tool-submit",
            name="submit_answer",
            input={
                "answer": "This is a small greeting service with a documented API.",
                "basis": "The README and application entry point establish its purpose.",
                "refused": False,
                "citations": [
                    {
                        "path": "README.md",
                        "start_line": 1,
                        "end_line": 2,
                        "title": "Repository overview",
                        "relevance": "Names and describes the greeting service.",
                    }
                ],
                "suggested_questions": [],
            },
        )
        client = SimpleNamespace(
            messages=SimpleNamespace(
                create=Mock(
                    side_effect=[
                        SimpleNamespace(
                            content=[overview_tool, entry_tool],
                            stop_reason="tool_use",
                            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                        ),
                        SimpleNamespace(
                            content=[submit],
                            stop_reason="tool_use",
                            usage=SimpleNamespace(input_tokens=20, output_tokens=10),
                        ),
                    ]
                )
            )
        )
        fake_settings = SimpleNamespace(
            anthropic_api_key="configured",
            model_name="claude-sonnet-5",
            agent_max_output_tokens=4000,
            agent_max_tool_rounds=3,
        )
        agent = RepositoryAgentService(self.session)
        with patch("backend.app.agent.service.settings", fake_settings), patch.object(
            agent, "_client", return_value=client
        ):
            answer = agent.answer(
                GroundedQuestion(question="What is this repository and where does it start?")
            )
        self.assertEqual(answer.provider, "claude-sonnet-5")
        self.assertEqual(
            [activity.tool for activity in answer.tool_trace],
            ["get_repository_overview", "find_entry_points"],
        )
        self.assertEqual(client.messages.create.call_count, 2)
        second_call = client.messages.create.call_args_list[1].kwargs
        self.assertEqual(
            second_call["tool_choice"],
            {"type": "tool", "name": "submit_answer"},
        )
        self.assertEqual(
            [tool["name"] for tool in second_call["tools"]],
            ["submit_answer"],
        )
        tool_results = next(
            message["content"]
            for message in reversed(second_call["messages"])
            if message["role"] == "user" and isinstance(message["content"], list)
        )
        self.assertEqual(len(tool_results), 2)

    def test_answer_bounded_reads_valid_proposed_citation_before_accepting(self) -> None:
        submit = SimpleNamespace(
            type="tool_use",
            id="tool-submit-recovered",
            name="submit_answer",
            input={
                "answer": "The repository is a greeting service.",
                "basis": "The README explicitly describes the service.",
                "refused": False,
                "citations": [{
                    "path": "README.md",
                    "start_line": 1,
                    "end_line": 3,
                    "title": "Repository overview",
                    "relevance": "Describes the greeting service.",
                }],
                "suggested_questions": [],
            },
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(
            return_value=SimpleNamespace(
                content=[submit],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                waypoint_provider="test:model",
            )
        )))
        fake_settings = SimpleNamespace(
            model_name="test-model",
            agent_max_output_tokens=2000,
            agent_max_tool_rounds=1,
            investigation_rounds=0,
            synthesis_max_attempts=1,
        )
        agent = RepositoryAgentService(self.session)
        with (
            patch("backend.app.agent.service.settings", fake_settings),
            patch.object(
                type(model_provider_router),
                "dual_role_enabled",
                new_callable=PropertyMock,
                return_value=False,
            ),
            patch.object(agent, "_client", return_value=client),
        ):
            answer = agent.answer(GroundedQuestion(question="What is this repository?"))

        self.assertEqual(answer.provider, "test:model")
        self.assertEqual(answer.citations[0].span.path, "README.md")

    def test_mission_forces_structured_submission_before_round_limit(self) -> None:
        target = next(
            node
            for node in self.session.report.nodes
            if node.name == "greet" and node.span is not None
        )
        search = SimpleNamespace(
            type="tool_use",
            id="tool-search",
            name="search_repository",
            input={"query": "greet", "limit": 5},
        )
        submit = SimpleNamespace(
            type="tool_use",
            id="tool-submit",
            name="submit_contribution_mission",
            input={
                "title": "Add greeting coverage",
                "risk": "Low - the change is isolated to focused test coverage.",
                "target_node_id": target.id,
                "rationale": "A contained backend behavior with a clear seam.",
                "suggested_files": ["app.py"],
                "checklist": ["Review greet", "Add focused coverage"],
                "definition_of_done": ["Tests pass", "Behavior is documented"],
            },
        )
        client = SimpleNamespace(
            messages=SimpleNamespace(
                create=Mock(
                    side_effect=[
                        SimpleNamespace(content=[search]),
                        SimpleNamespace(content=[submit]),
                    ]
                )
            )
        )
        fake_settings = SimpleNamespace(
            model_name="claude-sonnet-5",
            agent_max_output_tokens=4000,
            agent_max_tool_rounds=2,
        )
        request = TourRequest(role=DeveloperRole.BACKEND, goal="")
        self.assertIn("backend architecture", request.goal)
        agent = RepositoryOnboardingAgent(self.session)
        with patch("backend.app.agent.onboarding.settings", fake_settings), patch.object(
            agent.repository_agent, "_client", return_value=client
        ):
            mission = agent.mission(request)
        self.assertEqual(mission.target_node.id, target.id)
        self.assertEqual(mission.risk, "low")
        second_call = client.messages.create.call_args_list[1].kwargs
        self.assertEqual(
            second_call["tool_choice"],
            {"type": "tool", "name": "submit_contribution_mission"},
        )

    def test_onboarding_bounded_reads_valid_proposed_evidence_before_accepting(self) -> None:
        submit = SimpleNamespace(
            type="tool_use",
            id="tool-submit-tour",
            name="submit_onboarding_tour",
            input={
                "steps": [
                    {
                        "title": "Read the overview",
                        "objective": "Understand the service purpose.",
                        "explanation": "The README introduces the behavior.",
                        "why_selected": "It provides the intended product context.",
                        "files": [{
                            "path": "README.md",
                            "start_line": 1,
                            "end_line": 3,
                            "reason": "Repository overview",
                        }],
                        "challenge_prompt": "What does the service return?",
                        "expected_concepts": ["greeting"],
                    },
                    {
                        "title": "Read the implementation",
                        "objective": "Trace the greeting implementation.",
                        "explanation": "The function returns the greeting.",
                        "why_selected": "It is the production implementation.",
                        "files": [{
                            "path": "app.py",
                            "start_line": 1,
                            "end_line": 2,
                            "reason": "Greeting function",
                        }],
                        "challenge_prompt": "Which function implements the behavior?",
                        "expected_concepts": ["greet"],
                    },
                ],
                "planning_basis": ["Repository documentation and source"],
            },
        )
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(
            return_value=SimpleNamespace(
                content=[submit],
                waypoint_provider="test:model",
            )
        )))
        fake_settings = SimpleNamespace(
            model_name="test-model",
            agent_max_output_tokens=4000,
            agent_max_tool_rounds=1,
            investigation_rounds=0,
            synthesis_max_attempts=1,
        )
        agent = RepositoryOnboardingAgent(self.session)
        with (
            patch("backend.app.agent.onboarding.settings", fake_settings),
            patch.object(
                type(model_provider_router),
                "dual_role_enabled",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch.object(agent.repository_agent, "_client", return_value=client),
        ):
            tour = agent.plan(TourRequest(role=DeveloperRole.BACKEND))

        self.assertEqual(len(tour.steps), 2)
        self.assertEqual(tour.provider, "test:model")
        self.assertEqual(tour.steps[0].files[0].path, "README.md")

    def test_conversation_history_persists_across_store_instances(self) -> None:
        database = self.root / "state" / "waypoint.sqlite3"
        # Stores keep a persistent SQLite connection open per thread now (see
        # ConversationStore._connect), so both must be closed explicitly before
        # tearDown deletes self.root - unittest runs addCleanup callbacks *after*
        # tearDown, too late to release the file handle before the Windows rmtree.
        first_store = ConversationStore(database)
        reopened_store = ConversationStore(database)
        try:
            conversation = first_store.get_or_create(self.session.id, None, "ask")
            first_store.append_turn(
                conversation,
                "Where is greet?",
                "It is in app.py.",
                '{"provider":"test","citations":[]}',
            )
            onboarding = first_store.get_or_create(
                self.session.id, None, "onboarding"
            )
            first_store.append_turn(
                onboarding,
                "Why start here?",
                "It is the entry point.",
            )

            reopened = reopened_store.get_or_create(
                self.session.id, conversation.id, "ask"
            )
            self.assertEqual(
                reopened_store.history(reopened),
                [
                    {"role": "user", "content": "Where is greet?"},
                    {"role": "assistant", "content": "It is in app.py."},
                ],
            )
            self.assertEqual(reopened_store.latest(self.session.id, "ask"), reopened)
            self.assertEqual(
                reopened_store.latest(self.session.id, "onboarding"), onboarding
            )
            transcript = reopened_store.transcript(reopened)
            self.assertEqual(transcript[1]["role"], "assistant")
            self.assertEqual(
                transcript[1]["answer_json"],
                '{"provider":"test","citations":[]}',
            )
            with self.assertRaises(ValueError):
                reopened_store.get_or_create(
                    self.session.id, conversation.id, "onboarding"
                )
        finally:
            first_store.close()
            reopened_store.close()

    def test_hybrid_memory_recovers_relevant_turn_outside_recent_window(self) -> None:
        database = self.root / "state" / "hybrid-memory.sqlite3"
        # A persistent SQLite connection (see ConversationStore._connect) must be
        # closed explicitly before tearDown deletes self.root - unittest runs
        # addCleanup callbacks *after* tearDown, too late to release the file handle
        # before the Windows rmtree.
        store = ConversationStore(database)
        try:
            conversation = store.get_or_create(self.session.id, None, "ask")
            memory_settings = SimpleNamespace(
                chat_history_turns=2,
                chat_semantic_memory_items=2,
                chat_memory_retention_turns=20,
                chat_memory_max_characters=2000,
                chat_memory_min_similarity=0.05,
            )
            with patch("backend.app.agent.memory.settings", memory_settings):
                store.append_turn(
                    conversation,
                    "Where is authentication token validation implemented?",
                    "Authentication tokens are validated in security/auth.py.",
                    revision_fingerprint="revision-a",
                )
                for index in range(4):
                    store.append_turn(
                        conversation,
                        f"What color is dashboard panel {index}?",
                        f"Dashboard panel {index} is green.",
                        revision_fingerprint="revision-a",
                    )
                recent = store.history(conversation)
                hybrid, diagnostics = store.hybrid_history(
                    conversation,
                    "Remind me how authentication tokens are validated",
                    "revision-a",
                )
        finally:
            store.close()

        self.assertNotIn("authentication", " ".join(item["content"] for item in recent))
        self.assertIn("security/auth.py", " ".join(item["content"] for item in hybrid))
        self.assertEqual(diagnostics.recent_turns, 2)
        self.assertGreaterEqual(diagnostics.semantic_turns, 1)
        self.assertTrue(diagnostics.selected_scores)

    def test_analysis_session_can_be_restored_from_local_state(self) -> None:
        database = self.root / "state" / "analysis.sqlite3"
        fake_settings = SimpleNamespace(
            state_path=database,
            allowed_root=self.root,
        )
        report = RepositoryAnalyzer().analyze(self.root)
        # Stores keep a persistent SQLite connection open per thread now (see
        # AnalysisSessionStore._connect), so both must be closed explicitly before
        # tearDown deletes self.root - unittest runs addCleanup callbacks *after*
        # tearDown, too late to release the file handle before the Windows rmtree.
        first_store = AnalysisSessionStore(max_sessions=5)
        reopened_store = AnalysisSessionStore(max_sessions=5)
        try:
            with patch("backend.app.graph.store.settings", fake_settings):
                stored = first_store.create(self.root, report)
                restored = reopened_store.get(stored.analysis_id)
        finally:
            first_store.close()
            reopened_store.close()
        self.assertEqual(restored.root, self.root.resolve())
        self.assertEqual(restored.report.analysis_id, stored.analysis_id)
        self.assertIn("app.py", restored.source_paths)

    def test_list_recent_summaries_avoids_parsing_full_report(self) -> None:
        database = self.root / "state" / "summaries.sqlite3"
        fake_settings = SimpleNamespace(
            state_path=database,
            allowed_root=self.root,
        )
        report = RepositoryAnalyzer().analyze(self.root)
        # Stores keep a persistent SQLite connection open per thread now (see
        # AnalysisSessionStore._connect), so both must be closed explicitly before
        # tearDown deletes self.root - unittest runs addCleanup callbacks *after*
        # tearDown, too late to release the file handle before the Windows rmtree.
        first_store = AnalysisSessionStore(max_sessions=5)
        reopened_store = AnalysisSessionStore(max_sessions=5)
        try:
            with patch("backend.app.graph.store.settings", fake_settings):
                stored = first_store.create(self.root, report)

                summaries = first_store.list_recent_summaries()
                self.assertEqual(len(summaries), 1)
                summary = summaries[0]
                self.assertEqual(summary.id, stored.analysis_id)
                self.assertEqual(summary.root, self.root.resolve())
                self.assertEqual(summary.repository_name, stored.repository_name)
                self.assertEqual(summary.files_parsed, stored.stats.files_parsed)
                self.assertEqual(summary.node_count, stored.stats.node_count)
                self.assertEqual(summary.edge_count, stored.stats.edge_count)

                # Restart-safe: a fresh store reads the same summary straight from
                # its own SQLite columns, without restoring the full report.
                with patch.object(
                    reopened_store, "_restore", side_effect=AssertionError(
                        "must not fully restore the report for a listing query"
                    )
                ):
                    reopened_summaries = reopened_store.list_recent_summaries()
        finally:
            first_store.close()
            reopened_store.close()
        self.assertEqual(len(reopened_summaries), 1)
        self.assertEqual(reopened_summaries[0].id, stored.analysis_id)


if __name__ == "__main__":
    unittest.main()
