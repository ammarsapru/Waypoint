from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.agent.usage_ledger import ModelUsageLedger, record_model_response


class ModelUsageLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "usage.sqlite3"
        self.ledger = ModelUsageLedger(self.database)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary_directory.cleanup()

    def _rows(self) -> list[tuple]:
        connection = self.ledger._connect()
        return connection.execute(
            "SELECT workflow, analysis_id, round_number, model_role, routed_provider, "
            "input_tokens, output_tokens, cache_read_input_tokens, "
            "cache_creation_input_tokens, provider_reported_cost_usd, "
            "estimated_cost_usd, stop_reason, success FROM model_usage ORDER BY id"
        ).fetchall()

    def test_record_inserts_a_retrievable_row(self) -> None:
        self.ledger.record(
            workflow="ask",
            analysis_id="a1",
            conversation_id="c1",
            round_number=2,
            model_role="synthesis",
            routed_provider="bedrock:us.anthropic.claude-opus-4-6-v1",
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=0,
            estimated_cost_usd=0.0105,
            stop_reason="tool_use",
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[0], "ask")
        self.assertEqual(row[1], "a1")
        self.assertEqual(row[2], 2)
        self.assertEqual(row[3], "synthesis")
        self.assertEqual(row[5], 1000)
        self.assertEqual(row[6], 200)
        self.assertEqual(row[12], 1)  # success defaults to 1

    def test_record_defaults_missing_fields_to_null_not_a_crash(self) -> None:
        self.ledger.record(workflow="onboarding_assessment")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "onboarding_assessment")
        self.assertIsNone(rows[0][1])  # analysis_id
        self.assertIsNone(rows[0][5])  # input_tokens

    def test_survives_reopening_against_the_same_path(self) -> None:
        self.ledger.record(workflow="issue_investigation", analysis_id="a1")
        self.ledger.close()
        reopened = ModelUsageLedger(self.database)
        try:
            rows = reopened._connect().execute(
                "SELECT workflow FROM model_usage"
            ).fetchall()
            self.assertEqual(rows, [("issue_investigation",)])
        finally:
            reopened.close()

    def test_summary_by_workflow_aggregates_across_rows(self) -> None:
        self.ledger.record(
            workflow="ask", input_tokens=100, output_tokens=10,
            estimated_cost_usd=0.01,
        )
        self.ledger.record(
            workflow="ask", input_tokens=200, output_tokens=20,
            estimated_cost_usd=0.02,
        )
        self.ledger.record(
            workflow="issue_investigation", input_tokens=50, output_tokens=5,
            estimated_cost_usd=0.005,
        )
        summary = {row["workflow"]: row for row in self.ledger.summary_by_workflow()}
        self.assertEqual(summary["ask"]["calls"], 2)
        self.assertEqual(summary["ask"]["input_tokens"], 300)
        self.assertAlmostEqual(summary["ask"]["estimated_cost_usd"], 0.03)
        self.assertEqual(summary["issue_investigation"]["calls"], 1)


class RecordModelResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "usage.sqlite3"
        self.ledger = ModelUsageLedger(self.database)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary_directory.cleanup()

    def test_extracts_usage_and_computes_cost_from_a_bedrock_response(self) -> None:
        with patch(
            "backend.app.agent.usage_ledger.model_usage_ledger", self.ledger
        ):
            response = SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=1_000_000, output_tokens=1_000_000,
                    cache_read_input_tokens=0, cache_creation_input_tokens=0,
                ),
                waypoint_provider="bedrock:us.anthropic.claude-sonnet-4-6",
                waypoint_cost_usd=None,
                stop_reason="tool_use",
            )
            record_model_response(
                "ask", response, analysis_id="a1", conversation_id="c1",
                round_number=1, model_role="investigation",
            )
        rows = self.ledger._connect().execute(
            "SELECT workflow, routed_provider, input_tokens, output_tokens, "
            "estimated_cost_usd FROM model_usage"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        workflow, routed_provider, input_tokens, output_tokens, estimated_cost_usd = rows[0]
        self.assertEqual(workflow, "ask")
        self.assertEqual(routed_provider, "bedrock:us.anthropic.claude-sonnet-4-6")
        self.assertEqual(input_tokens, 1_000_000)
        self.assertEqual(output_tokens, 1_000_000)
        # $3 input + $15 output per 1M tokens for claude-sonnet-4-6.
        self.assertEqual(estimated_cost_usd, 18.0)

    def test_leaves_estimated_cost_null_for_an_unrecognized_model(self) -> None:
        with patch(
            "backend.app.agent.usage_ledger.model_usage_ledger", self.ledger
        ):
            response = SimpleNamespace(
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                waypoint_provider="openrouter:some/unlisted-model",
                waypoint_cost_usd=0.0002,
                stop_reason="end_turn",
            )
            record_model_response("ask", response, analysis_id="a1")
        row = self.ledger._connect().execute(
            "SELECT estimated_cost_usd, provider_reported_cost_usd FROM model_usage"
        ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], 0.0002)

    def test_handles_a_response_with_no_usage_attribute_at_all(self) -> None:
        with patch(
            "backend.app.agent.usage_ledger.model_usage_ledger", self.ledger
        ):
            response = SimpleNamespace(stop_reason="end_turn")
            record_model_response("onboarding_tour", response, analysis_id="a1")
        row = self.ledger._connect().execute(
            "SELECT input_tokens, output_tokens, estimated_cost_usd FROM model_usage"
        ).fetchone()
        self.assertEqual(row, (None, None, None))


if __name__ == "__main__":
    unittest.main()
