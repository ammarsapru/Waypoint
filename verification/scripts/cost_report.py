"""Reconstruct historical model spend from existing hooks/logs/benchmark artifacts and
combine it with the live `model_usage` ledger into one detailed cost report.

Deliberately does NOT re-run anything against a live provider - every figure here is
either a value a past run already recorded, or a backfilled estimate computed from
token counts that run already recorded. Where neither exists (V1-V3 retrieval
benchmarks predate token tracking entirely; onboarding/mission/assessment workflows
have no benchmark coverage at all), the report says so rather than inventing a number.

Sources mined, in order:
1. `backend/app/agent/usage_ledger.py`'s SQLite `model_usage` table - real runtime
   calls recorded since 2026-07-30.
2. The rotated structured trace log (`ONBOARD_TRACE_PATH` + its `.1`-`.5` rotations) -
   whatever `model.response_received`/`model.issue_*_response` events survived
   rotation from before the ledger existed.
3. `benchmarks/results/agentic-v4-*.json` - the V4 independent-agent benchmark's
   per-task `runs[]`, which already carry real input/output tokens (and sometimes cost)
   per (provider, model).
4. `verification/results/*.json` files with a `tasks[].answers[]` shape (the
   comparison-benchmark and model-quality scripts) - same per-answer token/cost fields.

For any record with real input/output tokens but no cost (every Bedrock run, since
Bedrock's API has never returned one), this backfills `estimated_cost_usd` using
`backend/app/agent/provider.py:estimate_model_cost_usd` - the same pricing table added
2026-07-30, applied retroactively to data that was already captured, not invented.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.app.agent.provider import estimate_model_cost_usd
from backend.app.config import settings


def _model_from_provider_string(provider: str | None, model: str | None) -> str | None:
    """Resolve a bare model id for pricing lookup from whichever field is populated."""
    if model:
        return model
    if provider and ":" in provider:
        return provider.split(":", 1)[-1]
    return provider


def mine_ledger(db_path: Path) -> list[dict[str, Any]]:
    """Read every row from the live `model_usage` ledger table, if it exists yet."""
    if not db_path.exists():
        return []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT workflow, analysis_id, routed_provider, input_tokens, "
            "output_tokens, cache_read_input_tokens, cache_creation_input_tokens, "
            "provider_reported_cost_usd, estimated_cost_usd, recorded_at "
            "FROM model_usage"
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        return []  # table doesn't exist yet - no runtime calls recorded since 2026-07-30
    finally:
        connection.close()
    records = []
    for (workflow, analysis_id, routed_provider, input_tokens, output_tokens,
         cache_read, cache_write, provider_cost, estimated_cost, recorded_at) in rows:
        records.append({
            "source": "runtime_ledger",
            "category": workflow,
            "identity": analysis_id or "-",
            "provider": routed_provider,
            "model": _model_from_provider_string(routed_provider, None),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "original_cost_usd": provider_cost,
            "estimated_cost_usd": estimated_cost,
            "when": recorded_at,
        })
    return records


def mine_trace_logs(trace_path: Path) -> list[dict[str, Any]]:
    """Scan the current trace file and its rotated `.1`-`.5` backups for genuine
    `model.response_received` / `model.issue_*_response` events. Most log lines are
    unrelated (function traces, HTTP requests) and the vast majority of the retained
    window is typically from before this feature existed, so this is expected to find
    very little - that sparsity is itself a documented finding, not a script bug."""
    records: list[dict[str, Any]] = []
    candidates = [trace_path] + [
        trace_path.with_suffix(trace_path.suffix + f".{n}") for n in range(1, 6)
    ]
    target_events = {
        "model.response_received", "model.issue_investigation_response",
        "model.issue_sweep_response", "model.usage_recorded",
    }
    for path in candidates:
        if not path.exists():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not any(f'"event": "{event}"' in line for event in target_events):
                        continue  # cheap pre-filter before the expensive json.loads
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("event") not in target_events:
                        continue
                    fields = payload.get("fields", {})
                    input_tokens = fields.get("input_tokens")
                    output_tokens = fields.get("output_tokens")
                    if input_tokens in (None, "<redacted>") and output_tokens in (None, "<redacted>"):
                        continue  # nothing usable on this line
                    records.append({
                        "source": f"trace_log:{path.name}",
                        "category": fields.get("model_role") or payload.get("event"),
                        "identity": fields.get("analysis_id") or "-",
                        "provider": fields.get("routed_provider"),
                        "model": _model_from_provider_string(
                            fields.get("routed_provider"), fields.get("model")
                        ),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": fields.get("cache_read_input_tokens"),
                        "cache_creation_input_tokens": fields.get("cache_creation_input_tokens"),
                        "original_cost_usd": fields.get("estimated_cost_usd"),
                        "estimated_cost_usd": None,  # recomputed uniformly below
                        "when": payload.get("message"),
                    })
        except OSError:
            continue
    return records


def mine_benchmark_runs(pattern: str) -> list[dict[str, Any]]:
    """Mine `benchmarks/results/agentic-v4-*.json`'s `runs[]` list."""
    records: list[dict[str, Any]] = []
    for path_str in sorted(glob.glob(pattern)):
        path = Path(path_str)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for run in data.get("runs", []):
            input_tokens = run.get("input_tokens")
            output_tokens = run.get("output_tokens")
            if input_tokens is None and output_tokens is None:
                continue
            records.append({
                "source": f"benchmark:{path.name}",
                # Deliberately a fixed high-level bucket, not the benchmark's own
                # fine-grained per-task "category" (e.g. "source-ingestion") - this
                # report groups by *application workflow*, and every V4 run exercises
                # the same code path as a live Ask question
                # (agentic_comparison.py calls RepositoryAgentService.answer()
                # directly). The original per-task category is still visible in the
                # underlying benchmark JSON for anyone who wants to drill in further.
                "category": "benchmark_agentic_v4 (ask-equivalent)",
                "identity": run.get("case_id") or "-",
                "provider": run.get("provider"),
                "model": run.get("model"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": run.get("cache_read_input_tokens"),
                "cache_creation_input_tokens": run.get("cache_creation_input_tokens"),
                "original_cost_usd": run.get("estimated_cost_usd"),
                "estimated_cost_usd": None,
                "when": None,
            })
    return records


def mine_benchmark_summaries(pattern: str) -> list[dict[str, Any]]:
    """Mine the pre-aggregated `summary` dict each V4 benchmark file already computes
    per (provider, model) endpoint - `tokens_per_tool_call`, `output_tokens_per_second`,
    `mean_rounds`, `mean_tool_calls` are genuine per-case-normalized efficiency signals,
    richer than anything derivable by re-averaging the raw `runs[]` records blindly
    (which would over-weight whichever endpoint happened to have more cases in a given
    file). Kept as a separate mining function from `mine_benchmark_runs` because this
    reads `summary`, not `runs`, and the two are not meant to be combined into one
    record shape."""
    rows: list[dict[str, Any]] = []
    for path_str in sorted(glob.glob(pattern)):
        path = Path(path_str)
        if ".hooks." in path.name:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for endpoint, summary in (data.get("summary") or {}).items():
            rows.append({
                "source": path.name,
                "endpoint": endpoint,
                "cases": summary.get("cases"),
                "mean_total_tokens": summary.get("mean_total_tokens"),
                "tokens_per_tool_call": summary.get("tokens_per_tool_call"),
                "output_tokens_per_second": summary.get("output_tokens_per_second"),
                "mean_rounds": summary.get("mean_rounds"),
                "mean_tool_calls": summary.get("mean_tool_calls"),
            })
    return rows


def mine_verification_tasks(pattern: str) -> list[dict[str, Any]]:
    """Mine `verification/results/*.json` files shaped as `tasks[].answers[]`
    (the comparison-benchmark and model-quality scripts)."""
    records: list[dict[str, Any]] = []
    for path_str in sorted(glob.glob(pattern)):
        path = Path(path_str)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for task in data.get("tasks", []):
            for answer in task.get("answers", []):
                input_tokens = answer.get("input_tokens")
                output_tokens = answer.get("output_tokens")
                if input_tokens is None and output_tokens is None:
                    continue
                records.append({
                    "source": f"verification:{path.name}",
                    "category": "ask_comparison",
                    "identity": task.get("analysis_id") or "-",
                    "provider": answer.get("provider"),
                    "model": answer.get("model"),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": answer.get("cache_read_input_tokens"),
                    "cache_creation_input_tokens": answer.get("cache_creation_input_tokens"),
                    "original_cost_usd": answer.get("estimated_cost_usd"),
                    "estimated_cost_usd": None,
                    "when": None,
                })
    return records


def backfill_cost(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For every record, compute a uniform `estimated_cost_usd` from real token counts
    via the current pricing table - overwriting nothing that's a genuine
    provider-reported figure (`original_cost_usd` is preserved separately), just adding
    a comparable estimate wherever one wasn't captured at the time."""
    for record in records:
        if record.get("estimated_cost_usd") is not None:
            continue
        model = record.get("model")
        if not model:
            continue
        record["estimated_cost_usd"] = estimate_model_cost_usd(
            model,
            record.get("input_tokens"),
            record.get("output_tokens"),
            cache_read_input_tokens=record.get("cache_read_input_tokens"),
            cache_creation_input_tokens=record.get("cache_creation_input_tokens"),
        )
    return records


def render_report(
    records: list[dict[str, Any]], summaries: list[dict[str, Any]] | None = None
) -> str:
    lines = [
        "# Reconstructed cost report",
        "",
        "Generated by `verification/scripts/cost_report.py`. Every figure here is",
        "either a value a past run already recorded, or a value backfilled from real",
        "token counts that run already recorded via",
        "`backend/app/agent/provider.py:estimate_model_cost_usd`. Nothing was re-run",
        "against a live provider to produce this report. Bedrock has historically",
        "mirrored Anthropic's first-party per-token pricing for the same model",
        "generation, but that match is not independently confirmed against a live",
        "Bedrock pricing API - treat every `estimated_cost_usd` figure below as",
        "directional, not a substitute for AWS's or Anthropic's own billing console.",
        "",
    ]

    if not records:
        lines.append(
            "No records with real token counts were found across the ledger, trace "
            "logs, or benchmark artifacts."
        )
        return "\n".join(lines)

    total_input = sum(r.get("input_tokens") or 0 for r in records)
    total_output = sum(r.get("output_tokens") or 0 for r in records)
    total_cost = sum(r.get("estimated_cost_usd") or 0 for r in records)
    priced_records = [r for r in records if r.get("estimated_cost_usd") is not None]
    unpriced_count = len(records) - len(priced_records)

    lines += [
        "## Overall totals",
        "",
        f"- **{len(records)} records** mined across the runtime ledger, rotated trace",
        "  logs, and benchmark artifacts.",
        f"- **{total_input:,} input tokens**, **{total_output:,} output tokens**",
        "  summed across every record found.",
        f"- **${total_cost:,.4f} estimated total**, of which "
        f"{len(priced_records)} of {len(records)} records could be priced",
        f"  ({unpriced_count} used a model id not in the pricing table and are",
        "  excluded from the total).",
        "",
        "## By source",
        "",
        "| Source | Records | Input tokens | Output tokens | Estimated cost |",
        "|---|---:|---:|---:|---:|",
    ]

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_source[record["source"]].append(record)
    for source, group in sorted(by_source.items(), key=lambda item: -sum(
        r.get("estimated_cost_usd") or 0 for r in item[1]
    )):
        lines.append(
            f"| `{source}` | {len(group)} | "
            f"{sum(r.get('input_tokens') or 0 for r in group):,} | "
            f"{sum(r.get('output_tokens') or 0 for r in group):,} | "
            f"${sum(r.get('estimated_cost_usd') or 0 for r in group):,.4f} |"
        )

    lines += [
        "",
        "## By workflow/category",
        "",
        "| Category | Records | Input tokens | Output tokens | Estimated cost |",
        "|---|---:|---:|---:|---:|",
    ]
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_category[str(record.get("category"))].append(record)
    for category, group in sorted(by_category.items(), key=lambda item: -sum(
        r.get("estimated_cost_usd") or 0 for r in item[1]
    )):
        lines.append(
            f"| {category} | {len(group)} | "
            f"{sum(r.get('input_tokens') or 0 for r in group):,} | "
            f"{sum(r.get('output_tokens') or 0 for r in group):,} | "
            f"${sum(r.get('estimated_cost_usd') or 0 for r in group):,.4f} |"
        )

    lines += [
        "",
        "## By provider:model",
        "",
        "| Provider:model | Records | Input tokens | Output tokens | Estimated cost |",
        "|---|---:|---:|---:|---:|",
    ]
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = f"{record.get('provider') or 'unknown'}" + (
            f" ({record['model']})" if record.get("model") and record.get("model") not in str(record.get("provider")) else ""
        )
        by_model[key].append(record)
    for key, group in sorted(by_model.items(), key=lambda item: -sum(
        r.get("estimated_cost_usd") or 0 for r in item[1]
    )):
        lines.append(
            f"| {key} | {len(group)} | "
            f"{sum(r.get('input_tokens') or 0 for r in group):,} | "
            f"{sum(r.get('output_tokens') or 0 for r in group):,} | "
            f"${sum(r.get('estimated_cost_usd') or 0 for r in group):,.4f} |"
        )

    lines += [
        "",
        "## Token efficiency by provider:model (derived from raw records)",
        "",
        "Not a cost metric - two providers can use very different token counts to do",
        "the same job regardless of what either charges per token. `avg tokens/record`",
        "is the size of the job each record represents; `input:output ratio` is how",
        "much of that is accumulated context/tool-result reading versus actual",
        "generation - a high ratio usually means more repository exploration per",
        "answer, not necessarily a worse answer.",
        "",
        "| Provider:model | Records | Avg tokens/record | Input:output ratio |",
        "|---|---:|---:|---:|",
    ]
    for key, group in sorted(by_model.items(), key=lambda item: -sum(
        (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in item[1]
    )):
        total_in = sum(r.get("input_tokens") or 0 for r in group)
        total_out = sum(r.get("output_tokens") or 0 for r in group)
        avg_tokens = (total_in + total_out) / len(group) if group else 0
        ratio = f"{total_in / total_out:.1f}:1" if total_out else "n/a"
        lines.append(f"| {key} | {len(group)} | {avg_tokens:,.0f} | {ratio} |")

    if summaries:
        lines += [
            "",
            "## Token efficiency (pre-aggregated per benchmark endpoint)",
            "",
            "These come straight from each V4 benchmark's own `summary` block, computed",
            "per-case (not re-derived by this script), so they're normalized correctly",
            "even when one endpoint ran far more cases than another in the same file -",
            "unlike the table above, which pools every record together.",
            "",
            "| Benchmark file | Endpoint | Cases | Avg total tokens | Tokens/tool call | "
            "Output tok/sec | Avg rounds | Avg tool calls |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in sorted(
            summaries, key=lambda r: -(r.get("mean_total_tokens") or 0)
        ):
            lines.append(
                f"| `{row['source']}` | {row['endpoint']} | {row.get('cases')} | "
                f"{row.get('mean_total_tokens') or 0:,.0f} | "
                f"{row.get('tokens_per_tool_call') or 0:,.0f} | "
                f"{row.get('output_tokens_per_second') or 0:.1f} | "
                f"{row.get('mean_rounds') or 0:.1f} | "
                f"{row.get('mean_tool_calls') or 0:.1f} |"
            )

    lines += [
        "",
        "## Known gaps (not reconstructable from existing data)",
        "",
        "- **The direct Anthropic API cascade tier has zero surviving data, despite",
        "  being tested.** It's a fallback tier (only reached when Bedrock itself",
        "  fails), and every real exercise of it appears to have happened during live/",
        "  interactive sessions that were never saved to a benchmark artifact. The only",
        "  `anthropic-api` string matches anywhere in `benchmarks/`/`verification/` are",
        "  this codebase's own docstring text in `provider.py`, quoted back as evidence",
        "  by an agent answering a question about the Waypoint repository itself - not",
        "  real usage data. Bedrock, Claude Code, and OpenRouter all have real",
        "  reconstructed data; the direct API tier does not, for any workflow.",
        "- **Onboarding tours, missions, and assessments have zero benchmark",
        "  coverage.** No benchmark artifact contains a genuine onboarding/tour/",
        "  mission/assessment run - grepping for `submit_onboarding_tour`/",
        "  `ContributionMission`/`TourRequest` across `benchmarks/` only turns up",
        "  false positives (tool schemas and source-code text, not real runs). Cost",
        "  visibility for these three workflows starts only from 2026-07-30 forward,",
        "  via the runtime ledger - there is no historical data to reconstruct.",
        "- **V1-V3 retrieval benchmarks predate token tracking entirely.** They",
        "  measure hit@5/recall/MRR/latency/memory against the retrieval layer",
        "  directly, with zero model calls involved - there is nothing to",
        "  reconstruct because no model was ever called.",
        "- **The actual rendered frontend is never benchmarked for cost.** Playwright",
        "  E2E specs (`frontend/e2e/*.spec.ts`) verify UI behavior only; the V4",
        "  agentic benchmark calls `RepositoryAgentService.answer()` directly",
        "  in-process, never through a browser or even an HTTP request.",
        "- **Most of the historical trace log window is gone**, and what survived was",
        "  separately unusable until today. Two compounding issues: (1) the rotating",
        "  trace log (25MB x 5 backups) is shared with every other kind of log event,",
        "  so most real usage events from before 2026-07-30 were rotated out; (2) of",
        "  the 33 genuine `model.response_received`/`model.issue_*_response` events",
        "  that DID survive, every single one had `input_tokens`/`output_tokens`",
        "  already replaced with the literal string `\"<redacted>\"` at write time -",
        "  `backend/app/observability.py`'s credential-redaction regex matched",
        "  \"token\" as a substring of every token-*count* field name, not just actual",
        "  credential fields like `github_token`. Fixed 2026-07-30 (see",
        "  `_TOKEN_COUNT_KEY` in observability.py) so this stops happening going",
        "  forward - already-written historical log lines cannot be un-redacted, so",
        "  this is a real, permanent loss for everything logged before the fix, not",
        "  something this script can recover.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconstruct historical model spend from existing hooks/logs/"
        "benchmark artifacts and the live runtime ledger."
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("verification/results/cost-reconstruction.md"),
    )
    args = parser.parse_args()

    records: list[dict[str, Any]] = []
    records += mine_ledger(settings.state_path)
    records += mine_trace_logs(settings.trace_path)
    records += mine_benchmark_runs("benchmarks/results/agentic-v4-*.json")
    records += mine_verification_tasks("verification/results/*.json")
    records = backfill_cost(records)
    summaries = mine_benchmark_summaries("benchmarks/results/agentic-v4-*.json")

    report = render_report(records, summaries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
