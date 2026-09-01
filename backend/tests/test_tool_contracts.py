from __future__ import annotations

import json

import pytest

from backend.app.agent.tool_contracts import (
    TOOL_CONTRACTS,
    ToolInputValidationError,
    ToolOutputValidationError,
    correction_envelope,
)


def test_search_contract_rejects_coercion_and_unknown_fields() -> None:
    contract = TOOL_CONTRACTS["search_repository"]
    with pytest.raises(ToolInputValidationError):
        contract.validate_input({"query": "agent", "limit": "12"})
    with pytest.raises(ToolInputValidationError):
        contract.validate_input({"query": "agent", "surprise": True})


def test_read_contract_rejects_inverted_ranges() -> None:
    with pytest.raises(ToolInputValidationError):
        TOOL_CONTRACTS["read_source"].validate_input({
            "path": "app.py", "start_line": 20, "end_line": 10,
        })


def test_server_output_contract_detects_waypoint_defect() -> None:
    with pytest.raises(ToolOutputValidationError):
        TOOL_CONTRACTS["search_repository"].validate_output({
            "results": [{"path": "app.py"}], "count": 2,
        })


def test_correction_envelope_is_machine_readable() -> None:
    payload = json.loads(correction_envelope(
        code="invalid_tool_input",
        tool="search_repository",
        message="bad input",
        errors=[{"loc": ["limit"], "type": "int_type"}],
    ))
    assert payload["ok"] is False
    assert payload["error"]["retryable"] is True
    assert payload["error"]["violations"][0]["loc"] == ["limit"]
