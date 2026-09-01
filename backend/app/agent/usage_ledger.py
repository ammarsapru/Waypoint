from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from backend.app.config import settings
from backend.app.observability import log_event

logger = logging.getLogger(__name__)

# Every workflow that makes a model call is expected to pass one of these as
# `record_model_response`'s `workflow` argument. Kept as a plain tuple (not an enum)
# so a new workflow can start recording without a schema/type change here.
KNOWN_WORKFLOWS = (
    "ask",
    "onboarding_tour",
    "onboarding_mission",
    "onboarding_assessment",
    "issue_investigation",
    "issue_sweep",
    "comparison",
)


class ModelUsageLedger:
    """Persists one row per model response across every Waypoint workflow, backed by
    SQLite.

    Exists because prior to 2026-07-30 the only record of a model call's token usage
    and cost was a line in the general-purpose rotating trace log
    (`backend/app/observability.py`'s JSONL handler) - which is shared with every other
    kind of log event (function traces, HTTP requests, subprocess output) and rotates
    out at 25MB x 5 backups, so real usage events were routinely overwritten within
    hours of being written. This is a dedicated, compact table that only ever grows by
    one small row per model call, so it survives independent of how noisy the rest of
    the application's logging is. Not a substitute for a provider's own billing
    console - `estimated_cost_usd` is a best-effort figure (see
    `backend/app/agent/provider.py:estimate_model_cost_usd`), not an authoritative bill.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Set up the store's SQLite path; the table is created lazily on first
        connection, not here."""
        self.path = (path or settings.state_path).resolve()
        self._local = threading.local()
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        """Return this thread's cached SQLite connection, opening (and migrating) it on
        first use. See `FindingStore._connect` (backend/app/agent/issues.py) for why a
        persistent per-thread connection is used instead of reopening one per call."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_usage ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "workflow TEXT NOT NULL, "
            "analysis_id TEXT, "
            "conversation_id TEXT, "
            "round_number INTEGER, "
            "model_role TEXT, "
            "routed_provider TEXT, "
            "input_tokens INTEGER, "
            "output_tokens INTEGER, "
            "cache_read_input_tokens INTEGER, "
            "cache_creation_input_tokens INTEGER, "
            "provider_reported_cost_usd REAL, "
            "estimated_cost_usd REAL, "
            "stop_reason TEXT, "
            "success INTEGER NOT NULL DEFAULT 1, "
            "error_type TEXT"
            ")"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_usage_analysis "
            "ON model_usage(analysis_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_usage_workflow "
            "ON model_usage(workflow)"
        )
        self._local.connection = connection
        return connection

    def close(self) -> None:
        """Close and discard this thread's cached connection, if any. See
        `FindingStore.close` for why tests need this before deleting the backing file."""
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def record(self, **fields: Any) -> None:
        """Insert one usage row. Accepts the exact column names defined above as
        keyword arguments; any column not passed is stored NULL (or its SQL default).
        Called by `record_model_response` below - not normally called directly."""
        columns = [
            "workflow", "analysis_id", "conversation_id", "round_number",
            "model_role", "routed_provider", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "provider_reported_cost_usd", "estimated_cost_usd", "stop_reason",
            "success", "error_type",
        ]
        values = [fields.get(column) for column in columns]
        if fields.get("success") is None:
            values[columns.index("success")] = 1
        with self._lock:
            connection = self._connect()
            with connection:
                connection.execute(
                    f"INSERT INTO model_usage ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    values,
                )

    def summary_by_workflow(self) -> list[dict[str, Any]]:
        """Aggregate spend/tokens grouped by workflow, most total estimated cost first.
        Used by `verification/scripts/cost_report.py` and available for any future UI
        that wants a cost breakdown without re-deriving it from raw rows."""
        connection = self._connect()
        rows = connection.execute(
            "SELECT workflow, COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_input_tokens) AS cache_read_input_tokens, "
            "SUM(cache_creation_input_tokens) AS cache_creation_input_tokens, "
            "SUM(estimated_cost_usd) AS estimated_cost_usd, "
            "SUM(provider_reported_cost_usd) AS provider_reported_cost_usd "
            "FROM model_usage GROUP BY workflow ORDER BY estimated_cost_usd DESC"
        ).fetchall()
        keys = [
            "workflow", "calls", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "estimated_cost_usd", "provider_reported_cost_usd",
        ]
        return [dict(zip(keys, row)) for row in rows]

    def summary_by_provider(self) -> list[dict[str, Any]]:
        """Aggregate spend/tokens grouped by routed provider (e.g.
        "bedrock:us.anthropic.claude-sonnet-4-6"), most total estimated cost first -
        the provider-dimension counterpart to `summary_by_workflow`."""
        connection = self._connect()
        rows = connection.execute(
            "SELECT routed_provider, COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cache_read_input_tokens) AS cache_read_input_tokens, "
            "SUM(cache_creation_input_tokens) AS cache_creation_input_tokens, "
            "SUM(estimated_cost_usd) AS estimated_cost_usd, "
            "SUM(provider_reported_cost_usd) AS provider_reported_cost_usd "
            "FROM model_usage GROUP BY routed_provider ORDER BY estimated_cost_usd DESC"
        ).fetchall()
        keys = [
            "routed_provider", "calls", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "estimated_cost_usd", "provider_reported_cost_usd",
        ]
        return [dict(zip(keys, row)) for row in rows]

    def summary_by_analysis(self) -> list[dict[str, Any]]:
        """Aggregate spend/tokens grouped by analysis (repository session), most total
        estimated cost first - lets a caller see "this repo cost $X, that one cost $Y"
        rather than only a pooled total across every repository ever analyzed.

        Joins against `graph/store.py`'s `analysis_sessions` table (same SQLite file,
        no ATTACH needed) to resolve a human-readable `repository_name` - without it,
        rows would only be distinguishable by opaque analysis_id UUIDs. `LEFT JOIN`
        so a usage row survives even if its analysis was since deleted/pruned from
        `analysis_sessions` (shows as `repository_name IS NULL`, not dropped).
        """
        connection = self._connect()
        rows = connection.execute(
            "SELECT m.analysis_id, s.repository_name, COUNT(*) AS calls, "
            "SUM(m.input_tokens) AS input_tokens, SUM(m.output_tokens) AS output_tokens, "
            "SUM(m.cache_read_input_tokens) AS cache_read_input_tokens, "
            "SUM(m.cache_creation_input_tokens) AS cache_creation_input_tokens, "
            "SUM(m.estimated_cost_usd) AS estimated_cost_usd, "
            "SUM(m.provider_reported_cost_usd) AS provider_reported_cost_usd "
            "FROM model_usage m LEFT JOIN analysis_sessions s ON s.id = m.analysis_id "
            "GROUP BY m.analysis_id ORDER BY estimated_cost_usd DESC"
        ).fetchall()
        keys = [
            "analysis_id", "repository_name", "calls", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "estimated_cost_usd", "provider_reported_cost_usd",
        ]
        return [dict(zip(keys, row)) for row in rows]

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent `limit` raw rows, newest first - for a UI that wants
        to show individual calls rather than only aggregates."""
        connection = self._connect()
        rows = connection.execute(
            "SELECT recorded_at, workflow, analysis_id, round_number, model_role, "
            "routed_provider, input_tokens, output_tokens, cache_read_input_tokens, "
            "cache_creation_input_tokens, estimated_cost_usd, provider_reported_cost_usd, "
            "stop_reason, success FROM model_usage ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = [
            "recorded_at", "workflow", "analysis_id", "round_number", "model_role",
            "routed_provider", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "estimated_cost_usd", "provider_reported_cost_usd", "stop_reason", "success",
        ]
        return [dict(zip(keys, row)) for row in rows]


model_usage_ledger = ModelUsageLedger()


def record_model_response(
    workflow: str,
    response: Any,
    *,
    analysis_id: str | None = None,
    conversation_id: str | None = None,
    round_number: int | None = None,
    model_role: str | None = None,
) -> None:
    """Extract usage/cost fields from a model response and persist one ledger row,
    also emitting the same fields as a structured log event for continuity with
    existing trace-log tooling.

    Input: workflow - one of `KNOWN_WORKFLOWS` (not enforced - a new workflow name
    still records, just isn't one of the recognized ones yet); response - the raw
    provider response object (its `.usage` carries token/cache counts, and
    `.waypoint_provider`/`.waypoint_cost_usd`/`.stop_reason` carry routing and any
    provider-reported cost). `estimated_cost_usd` is computed here via
    `provider.py:estimate_model_cost_usd` against the resolved model id parsed out of
    `waypoint_provider` (e.g. "bedrock:us.anthropic.claude-sonnet-4-6" ->
    "us.anthropic.claude-sonnet-4-6"). Called after every successful model call in
    service.py's answer(), onboarding.py's tour/mission/assessment loops, and
    issues.py's investigate_issue()/investigate() - the single shared recording path
    for all of them, so a new workflow only needs one call site to start appearing in
    the ledger.
    """
    from backend.app.agent.provider import estimate_model_cost_usd  # local import: avoids a
    # module-level cycle (provider.py does not import this module, but importing it at
    # module scope here would still make provider.py's own import graph brittle to
    # future changes; this function is called far less often than it would cost to
    # resolve a top-level circular import bug).

    usage = getattr(response, "usage", None)
    routed_provider = getattr(response, "waypoint_provider", None)
    model = str(routed_provider or "").split(":", 1)[-1] or None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    cache_read_input_tokens = getattr(usage, "cache_read_input_tokens", None)
    cache_creation_input_tokens = getattr(usage, "cache_creation_input_tokens", None)
    provider_reported_cost_usd = getattr(response, "waypoint_cost_usd", None)
    estimated_cost_usd = (
        estimate_model_cost_usd(
            model, input_tokens, output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        )
        if model
        else None
    )

    model_usage_ledger.record(
        workflow=workflow,
        analysis_id=analysis_id,
        conversation_id=conversation_id,
        round_number=round_number,
        model_role=model_role,
        routed_provider=routed_provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        provider_reported_cost_usd=provider_reported_cost_usd,
        estimated_cost_usd=estimated_cost_usd,
        stop_reason=getattr(response, "stop_reason", None),
        success=1,
    )
    log_event(
        logger, logging.INFO, "model.usage_recorded",
        "Model response usage recorded to the ledger",
        workflow=workflow, analysis_id=analysis_id, conversation_id=conversation_id,
        round=round_number, model_role=model_role, routed_provider=routed_provider,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        provider_reported_cost_usd=provider_reported_cost_usd,
        estimated_cost_usd=estimated_cost_usd,
    )
