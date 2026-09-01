from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

from verification.scripts.http_flask_e2e import DEFAULT_REPOSITORY


def _post(client: httpx.Client, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.post(path, json=payload)
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    print(f"POST {path} -> {response.status_code} ({duration_ms} ms)")
    response.raise_for_status()
    return response.json()


def _get(client: httpx.Client, path: str, **kwargs: Any) -> dict[str, Any]:
    response = client.get(path, **kwargs)
    print(f"GET {path} -> {response.status_code}")
    response.raise_for_status()
    return response.json()


def _trace_evidence(
    trace_path: Path,
    output_path: Path,
    identifiers: set[str],
) -> int:
    if not trace_path.exists():
        return 0
    relevant_events = {
        "model.agent_started",
        "model.agent_round_started",
        "model.request_started",
        "model.response_received",
        "model.tool_called",
        "model.tool_completed",
        "model.tool_failed",
        "model.comparison_started",
        "model.comparison_routed",
        "model.comparison_completed",
    }
    selected: list[str] = []
    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        serialized = json.dumps(event, ensure_ascii=False)
        if event.get("event") in relevant_events and any(
            identifier in serialized for identifier in identifiers if identifier
        ):
            selected.append(json.dumps(event, ensure_ascii=False))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(selected) + ("\n" if selected else ""), encoding="utf-8")
    return len(selected)


def run(
    base_url: str,
    repository_url: str,
    repository_path: str | None,
    trace_path: Path,
    trace_output: Path,
    analysis_id: str | None = None,
    resume_existing_conversation: bool = False,
) -> dict[str, Any]:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=600) as client:
        analysis = (
            _get(client, f"/api/v1/analyses/{analysis_id}")
            if analysis_id
            else _post(
                client,
                "/api/v1/analysis" if repository_path else "/api/v1/analysis/github",
                (
                    {"repository_path": repository_path}
                    if repository_path
                    else {"repository_url": repository_url}
                ),
            )
        )
        analysis_id = analysis["analysis_id"]
        first_question = "How does Flask route an incoming HTTP request to a view function?"
        transcript = (
            _get(
                client,
                f"/api/v1/analyses/{analysis_id}/conversation/latest",
                params={"scope": "ask"},
            )
            if resume_existing_conversation
            else {"turns": []}
        )
        if len(transcript.get("turns", [])) >= 2:
            first = transcript["turns"][-2]["answer"]
            second = transcript["turns"][-1]["answer"]
        else:
            first = _post(
                client,
                f"/api/v1/analyses/{analysis_id}/answer",
                {"question": first_question, "conversation_scope": "ask"},
            )
            second = _post(
                client,
                f"/api/v1/analyses/{analysis_id}/answer",
                {
                    "question": "Which tests should I read next to verify that flow?",
                    "conversation_id": first["conversation_id"],
                    "conversation_scope": "ask",
                },
            )
        assert first["citations"], "The first answer did not contain validated citations"
        assert first["tool_trace"], "The agent did not expose any completed repository tools"
        assert second["conversation_id"] == first["conversation_id"]
        assert second["citations"], "The memory follow-up was not source grounded"

        tour = _post(
            client,
            f"/api/v1/analyses/{analysis_id}/tour",
            {
                "role": "backend",
                "goal": "Understand request routing and prepare a safe first contribution",
                "experience": "new",
                "minutes": 30,
            },
        )
        assert len(tour["steps"]) >= 2
        mission = _post(
            client,
            f"/api/v1/analyses/{analysis_id}/mission",
            {
                "role": "backend",
                "goal": "Make a safe first contribution around request routing",
                "experience": "new",
                "minutes": 30,
            },
        )
        assert mission["suggested_files"]

        comparison = _post(
            client,
            f"/api/v1/analyses/{analysis_id}/answer/compare",
            {"question": first_question, "evidence_limit": 8},
        )
        assert len(comparison["answers"]) == 2
        endpoints = {
            (answer["provider"], answer["model"])
            for answer in comparison["answers"]
        }
        assert len(endpoints) == 2
        assert all(answer["validation_status"] == "passed" for answer in comparison["answers"])

    identifiers = {analysis_id, first.get("conversation_id", "")}
    trace_count = _trace_evidence(trace_path, trace_output, identifiers)
    return {
        "reference_repository": repository_url,
        "analysis_id": analysis_id,
        "scenario": [
            "analyze Flask",
            "answer request-routing question with tools",
            "answer memory-aware follow-up",
            "generate role-specific onboarding route",
            "generate validated first-contribution mission",
            "compare the same question and frozen evidence across two models",
        ],
        "first_answer": first,
        "follow_up": second,
        "tour": tour,
        "mission": mission,
        "model_comparison": comparison,
        "captured_trace_events": trace_count,
        "trace_artifact": str(trace_output),
        "live_multistep_demo": "passed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Waypoint's opt-in, credit-consuming live Flask agent demo."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY)
    parser.add_argument("--repository-path")
    parser.add_argument(
        "--analysis-id",
        help="Resume a persisted analysis instead of analyzing the repository again.",
    )
    parser.add_argument(
        "--resume-existing-conversation",
        action="store_true",
        help="Reuse the latest two Ask turns for this analysis when present.",
    )
    parser.add_argument(
        "--trace-path",
        type=Path,
        default=Path(".waypoint-data/traces/waypoint.jsonl"),
    )
    parser.add_argument(
        "--trace-output",
        type=Path,
        default=Path("verification/results/live-multistep-trace.jsonl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("verification/results/live-multistep-report.json"),
    )
    parser.add_argument(
        "--confirm-live-model-usage",
        action="store_true",
        help="Required acknowledgement that this run may consume credits or subscription usage.",
    )
    args = parser.parse_args()
    if not args.confirm_live_model_usage:
        parser.error("--confirm-live-model-usage is required")
    result = run(
        args.base_url,
        args.repository_url,
        args.repository_path,
        args.trace_path,
        args.trace_output,
        args.analysis_id,
        args.resume_existing_conversation,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"PASS: live multi-step report written to {args.output}")


if __name__ == "__main__":
    main()
