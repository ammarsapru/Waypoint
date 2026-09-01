from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# Configure isolation before test modules import backend.app.config. Without this,
# temporary test sessions can evict real development analyses from the bounded store.
_TEST_STATE = tempfile.TemporaryDirectory(
    prefix="waypoint-pytest-", ignore_cleanup_errors=True
)
_TEST_ROOT = Path(_TEST_STATE.name)
os.environ["WAYPOINT_STATE_PATH"] = str(_TEST_ROOT / "waypoint-test.sqlite3")
os.environ["ONBOARD_TRACE_PATH"] = str(_TEST_ROOT / "waypoint-test.jsonl")
os.environ["ONBOARD_TRACE_FILE"] = "0"
os.environ["WAYPOINT_PROVIDER_STARTUP_PROBE"] = "0"
os.environ["WAYPOINT_CLAUDE_CODE_FALLBACK"] = "0"
# Force single-model mode regardless of the developer's own .env: tests that patch
# a partial fake `settings` (e.g. test_agent.py's fake_settings SimpleNamespaces)
# only define single-mode fields, and model_provider_router.dual_role_enabled reads
# the real, unpatched settings singleton - so a developer's real dual-role .env
# config would otherwise leak into unrelated tests and crash on a missing
# investigation_rounds/etc. attribute. Dual-role behavior itself is covered
# directly in test_provider.py with its own complete fake settings.
os.environ["WAYPOINT_MODEL_ARCHITECTURE"] = "single"


@pytest.fixture(scope="session", autouse=True)
def _close_persistent_state_stores():
    """Close every module-level store's persistent SQLite connection before the
    session-wide state directory is removed.

    Stores now keep a connection open per thread for as long as the process runs
    (see e.g. TourStateStore._connect) instead of reopening one per call, so that a
    single request doesn't pay for a WAL checkpoint against `settings.state_path`
    every time. That means nothing closes `_TEST_STATE` (the temp dir backing
    WAYPOINT_STATE_PATH for this whole pytest session) on its own anymore - without
    this fixture, `_TEST_STATE`'s finalizer tries to delete a database file these
    stores still hold open, which raises PermissionError on Windows during
    interpreter shutdown. Imports happen inside the fixture body, not at module
    level, since conftest.py must set WAYPOINT_STATE_PATH (above) before any of
    these modules - which read it at import time - get imported.
    """
    yield
    from backend.app.agent.issues import ai_findings, issue_investigations
    from backend.app.agent.memory import conversations
    from backend.app.agent.retrieval import repository_indexes
    from backend.app.agent.usage_ledger import model_usage_ledger
    from backend.app.graph.store import analysis_sessions
    from backend.app.onboarding.service import tour_states

    for store in (
        ai_findings, issue_investigations, conversations, repository_indexes,
        analysis_sessions, tour_states, model_usage_ledger,
    ):
        store.close()
    # ignore_errors=True is a deliberate safety net, not a substitute for the loop
    # above: any test that opens its own store against the *default* state path
    # (rather than a per-test temp path) leaves one more connection this fixture
    # doesn't know about. Silently leaking a stray temp file on interpreter
    # shutdown is an acceptable trade for not crashing the whole test run over it.
    _TEST_STATE.cleanup()


_UNIT_TEST_MODULES = {
    "test_analyzer.py",
    "test_agentic_rag_evaluation_manifest.py",
    "test_benchmarks.py",
    "test_issues.py",
    "test_observability.py",
    "test_polyglot_analyzer.py",
    "test_provider.py",
    "test_tool_contracts.py",
    "test_questions.py",
    "test_repository_import.py",
    "test_usage_ledger.py",
}
_INTEGRATION_TEST_MODULES = {
    "test_agent.py",
    "test_agentic_comparison.py",
    "test_api.py",
    "test_mcp_server.py",
    "test_model_comparison.py",
    "test_onboarding.py",
    "test_semantic_tools.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Make suite boundaries executable and reject silently unclassified tests."""
    for item in items:
        filename = Path(str(item.path)).name
        if filename in _UNIT_TEST_MODULES:
            item.add_marker(pytest.mark.unit)
        elif filename in _INTEGRATION_TEST_MODULES:
            item.add_marker(pytest.mark.integration)
        else:
            raise pytest.UsageError(
                f"Test module {filename!r} is not classified as unit or integration"
            )
