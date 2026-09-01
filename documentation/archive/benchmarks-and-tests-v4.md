> **Archived, superseded.** This "convenience" snapshot's content is already fully
> contained in [`../benchmarks/history.md`](../benchmarks/history.md)'s "V4 run
> history" section, which is the maintained cumulative record. Kept here only for
> anyone who has an old link to this exact file.

# Waypoint V4 - validated agents and independent retrieval evaluation

Date: 2026-07-27

## Outcome

V4 changes the evaluation boundary. V3 measures Waypoint's deterministic retrieval
engine. The older comparison measures synthesis over one frozen evidence bundle. V4
instead measures which model retrieves better when each model must independently choose
and call the same production Waypoint tools.

The original Poolside/OpenRouter campaign never produced a scored result - both fixed
free-tier endpoints exhausted OpenRouter's daily free-model quota before a single case
completed (retained below as infrastructure evidence, not a retrieval score). A
subsequent live smoke test against `claude-code:sonnet`/`claude-code:opus` on the same
25-case subset also failed to complete: running the Claude Agent SDK from inside the
same interactive session driving the benchmark caused progressively worsening
contradictory success/error protocol failures (47 of 48 attempts), most plausibly
session-level contention rather than a genuine model limitation.

The **complete 50-case campaign has now run to completion** against AWS Bedrock, which
sidesteps both prior blockers: it is a plain signed HTTPS API (no OpenRouter daily quota,
no Claude Agent SDK session to contend with) and its Converse API can host both an
Anthropic model and a non-Anthropic model behind one uniform tool-calling contract. Fixed
endpoints: `bedrock:us.anthropic.claude-sonnet-4-6` and `bedrock:openai.gpt-oss-120b-1:0`.

**Result: Claude Sonnet succeeded on 49 of 50 cases (98%); GPT-OSS-120B succeeded on 1 of
50 (2%).** This is a decisive result, not a close comparison - see "Current execution
evidence" below for the full retrieval-quality and efficiency breakdown.

## Production changes exercised by V4

### Strict tool contracts

`backend/app/agent/tool_contracts.py` is the source of truth for all 17 repository tools.
Each contract has:

- a strict Pydantic input model with forbidden extra fields, primitive-type enforcement,
  bounds, enums, and cross-field validation;
- provider JSON Schema generated from the same input model;
- a Pydantic output model for the server-owned result;
- distinct failures for invalid model input and invalid Waypoint output.

Invalid model input returns a machine-readable correction envelope. Tool-input
corrections and final-submission corrections have independent default budgets of two.
Both operate inside the eight-round outer agent limit; they do not multiply it.

Ask validates `submit_answer`, verifies citations are in evidence exposed during that
run, performs at most one bounded evidence recovery read when safe, and revalidates.
Onboarding uses the same repository tool contracts and separately validates tour and
mission submissions.

### Provider-independent forced synthesis

The first Poolside diagnostic proved that a provider can ignore forced `tool_choice`
when retrieval tools remain available. Laguna M.1 continued calling `read_source` in
rounds that Waypoint had classified as synthesis, then exhausted the two-attempt guard.

The corrected runtime exposes **only** the Pydantic-backed `submit_answer` tool during a
forced Ask synthesis round. Investigation remains model-led; only the terminal contract
is constrained. A unit assertion verifies the forced round contains exactly that tool.

### Retry classification

Transient OpenRouter 429/5xx and transport errors receive bounded backoff with structured
retry events. Non-transient quota signals such as `free-models-per-day`, `purchase
credits`, and `insufficient credit` fail immediately because sleeping cannot repair
them. A provider unit test covers both retryable 429 recovery and non-retryable daily
quota exhaustion.

## Three evaluations that remain separate

| Evaluation | Repository access | What it isolates |
|---|---|---|
| Direct retrieval engine (V3) | Waypoint directly calls FTS/vector/graph retrieval | Index and ranking without an LLM |
| Frozen-evidence comparison | Server supplies one immutable evidence bundle | Synthesis and citation quality on equal context |
| Independent-agent retrieval (V4) | Each model selects and calls typed tools | Planning, query formulation, inspection, correction, and evidence selection |

Combining these scores would hide causes. A model can write well from poor retrieval or
retrieve relevant candidates and then fail to inspect, cite, or validate them.

## Standalone V4 corpus

`benchmarks/retrieval_cases_v4.json` defines the protocol and points to the standalone
`benchmarks/retrieval_cases_v4_cases.json`. These are 50 new V4 cases, not merged,
rewritten, or replayed V2/V3 prompts:

| Repository | Cases |
|---|---:|
| Waypoint | 20 |
| Flask | 10 |
| PRAW | 10 |
| Open Notebook | 10 |
| **Total** | **50** |

The scenarios cover architecture, multi-hop runtime tracing, onboarding, issues,
conversation memory, provider routing, MCP, frontend, polyglot parsing, configuration,
and deployment. Every case contains required path groups, acceptable paths, forbidden
paths, and expected concepts.

The labels remain `pending_human`. Development runs require
`--allow-unadjudicated`; no such run is release-gate eligible until a reviewer approves
the labels without looking at model outputs.

## Models fixed for this campaign

- `bedrock:us.anthropic.claude-sonnet-4-6` - Claude Sonnet, addressed by its AWS
  cross-region inference profile ID (Bedrock rejects the plain model ID for on-demand
  invocation of this model and requires the `us.`-prefixed profile instead).
- `bedrock:openai.gpt-oss-120b-1:0` - OpenAI's open-weight GPT-OSS 120B model, hosted on
  Bedrock and invoked through the same Converse API and tool-calling contract as the
  Anthropic model, so both endpoints are driven identically.

Both endpoints are called through a single `BedrockConverseMessages` adapter
(`backend/app/agent/provider.py`) built specifically for this comparison: Bedrock's
Converse API is the one request/response format shared across every model family it
hosts, so it is the only path that can drive an Anthropic model and a non-Anthropic model
through the identical tool-calling contract. Earlier attempts historically fixed
OpenRouter or Claude Code endpoints instead; those are retained below as infrastructure
evidence, not replaced.

Endpoints execute sequentially to avoid cross-model capacity contention and to make
per-model wall-clock timings interpretable.

## Retrieval and efficiency metrics

Every successful endpoint is scored at three stages:

1. **Candidate retrieval** - first-seen file order returned by repository tools.
2. **Explicit inspection** - files deliberately opened with `read_source`.
3. **Cited retrieval** - unique file order in the validated final citations.

Each stage reports Hit@5, Hit@10, Precision@10, path recall, required-group recall, MRR,
nDCG@10, and forbidden-hit rate. Each model run additionally reports:

- repository tool calls, invalid calls, and outer model rounds;
- wall-clock duration plus p50/p95 aggregates;
- input, output, and total tokens when reported;
- tokens per tool call and output tokens per second;
- cost when reported;
- TTFT for streaming OpenRouter calls, otherwise an explicit unavailable status;
- expected-concept phrase recall and validated citation count.

Candidate files are derived from each model's own tool results. Explicit files derive
from that model's own `read_source` calls. Cited files derive from its validated terminal
submission. No evidence bundle is frozen or shared.

## Hooks and logs measured

`benchmarks/run_agentic_retrieval_v4.py` registers both function and structured-event
observers. The JSONL artifact retains only events with a benchmark trace ID, while the
normal application terminal/rotating-file logs remain exhaustive. The aggregate reports:

- pre/post/error phase counts and exception types;
- model rounds, requests, responses, tool calls, completions, retries, corrections, and
  terminal results;
- trace correlation and pre-to-terminal span pairing;
- incomplete and orphan spans;
- serialized bytes, bytes/event, events/trace, and callback time;
- redaction markers and conservative possible-secret patterns.

The logs expose observable inputs, bounded outputs, calls, corrections, and failures.
They do not and cannot expose a model's private chain-of-thought.

The separate `benchmarks/run_observability_overhead.py` microbenchmark measured about
94.89 microseconds per pre/post hook event for sanitization, trace correlation, dispatch,
and in-memory JSON serialization. Its relative percentage is not an application gate:
the uninstrumented baseline is a nearly free no-op. Real agent artifacts report durable
JSONL callback cost separately.

## Exact campaign command

```powershell
.\.venv\Scripts\python.exe benchmarks\run_agentic_retrieval_v4.py `
  --repository waypoint=. `
  --repository flask=.waypoint-clones\pallets--flask--214023150b `
  --repository praw=.waypoint-clones\haxtheweb--praw--69db9269ab `
  --repository open-notebook=.waypoint-clones\lfnovo--open-notebook--5d0a28a4b0 `
  --endpoint bedrock:us.anthropic.claude-sonnet-4-6 `
  --endpoint bedrock:openai.gpt-oss-120b-1:0 `
  --max-cases 50 `
  --allow-unadjudicated `
  --confirm-live-provider-usage `
  --output benchmarks\results\agentic-v4-bedrock-claude-vs-gptoss.json `
  --hooks-output benchmarks\results\agentic-v4-bedrock-claude-vs-gptoss.hooks.jsonl `
  --hooks-report benchmarks\results\agentic-v4-bedrock-claude-vs-gptoss.hooks.json
```

The runner checkpoints after each case and reuses a repository/endpoint service across
cases so its persistent index and semantic caches benefit later questions. The prior
Poolside/OpenRouter command is preserved in version history for reproducing that
diagnostic specifically.

## Current execution evidence

### Product and acceptance checks

| Check | Current result |
|---|---:|
| Backend unit/integration suite | 111/111 passed |
| Whole-backend branch-aware code coverage | 74.94% - PASS against enforced 70% |
| Weighted core aggregate | 82.91% - PASS (all 5 core features individually PASS) |
| Frontend Vitest unit suite | 3/3 passed |
| Frontend TypeScript/Vite production build | passed |
| Playwright HTTP/UI E2E workflow | passed |
| Frontend code coverage | prior result 1.62% - FAIL; major workspaces remain under-tested |

### Completed Bedrock campaign: Claude Sonnet vs GPT-OSS-120B

The retained files are:

- `benchmarks/results/agentic-v4-bedrock-claude-vs-gptoss.json`
- `benchmarks/results/agentic-v4-bedrock-claude-vs-gptoss.md`
- `benchmarks/results/agentic-v4-bedrock-claude-vs-gptoss.hooks.json`
- `benchmarks/results/agentic-v4-bedrock-claude-vs-gptoss.hooks.jsonl`

All 50 cases ran to completion across both endpoints (100 attempted model runs).

| Measurement | Claude Sonnet | GPT-OSS-120B |
|---|---:|---:|
| Successful cases | 49/50 (98%) | 1/50 (2%) |
| Mean duration | 100.6 s | 62.3 s (n=1) |
| Mean tool calls | 11.63 | 2 (n=1) |
| Mean rounds | 5.96 | 6 (n=1) |
| Mean invalid tool calls | 0.061 | 0 (n=1) |
| Mean total tokens | 218,179 | 42,651 (n=1) |
| Mean expected-concept recall | 72.9% | 0.0% (n=1) |
| Mean citations | 13.0 | 2 (n=1) |

Claude Sonnet's per-stage retrieval scores (49 successful cases):

| Stage | Hit@5 | Hit@10 | Path recall | Group recall | MRR | nDCG@10 | Forbidden-hit rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| Candidate retrieval | 10.2% | 61.2% | 77.8% | 90.9% | 0.148 | 0.095 | 0.46% |
| Explicit inspection | 98.0% | 98.0% | 56.3% | 70.9% | 0.935 | 0.646 | 0.70% |
| Cited retrieval | 98.0% | 100% | 62.9% | 78.0% | 0.918 | 0.685 | 0.48% |

Notable pattern: candidate-retrieval ranking quality (raw tool-result order) is modest,
but Claude's agentic behavior compensates heavily by the explicit-inspection and cited
stages - it consistently finds and cites the right evidence even when the first-seen
ranking wasn't sharp, rather than anchoring on a mediocre top-5.

**GPT-OSS-120B's near-total failure is not primarily a retrieval problem.** Of 49 failed
attempts, 46 failed with `"Repository synthesis exceeded 2 attempts"` and 3 with
`"Repository agent exceeded the tool-schema correction limit (2)"` - it repeatedly found
*some* evidence but could not reliably close out a schema-valid, citation-backed final
answer within the bounded correction budget. Its one successful case scored perfectly on
Hit@5/Hit@10/MRR for candidate and cited retrieval, suggesting the underlying retrieval
tools work fine for it when it does complete - the bottleneck is output discipline on the
terminal `submit_answer` contract, not finding files.

One Claude failure (`v4-on-10`) was infrastructure, not a model or retrieval failure: a
Bedrock `ThrottlingException` - "Too many tokens per day" - after 4 retries, indicating
the AWS account's daily Bedrock token quota was reached near the end of the run. This is
excluded from the retrieval-quality analysis above (Claude's reported stage metrics are
over the 49 cases that completed).

| Hook/log measurement | Result |
|---|---:|
| Hook events | 6,443 |
| Correlated traces | 100 |
| Trace correlation | 100% |
| Pre/terminal pairing | 100% |
| Incomplete/orphan spans | 0 / 0 |
| Possible secret leaks flagged | 26 (unreviewed - see note below) |
| Redaction markers | 1,660 |
| Mean durable hook callback | 1.731 ms/event |

The 26 flagged possible-secret-leak patterns have not yet been manually reviewed to
confirm true/false positive status; treat as an open follow-up before treating this run
as fully clean from a log-hygiene standpoint.

This is a completed, scored V4 campaign - not a diagnostic. It is not yet release-gate
eligible only because the 50-case label set remains `pending_human` adjudication (see
"Standalone V4 corpus" above), independent of these results.

## UI, HTTP, and MCP entry points

The Ask workspace exposes **Compare agents**. The corresponding HTTP endpoint is:

```text
POST /api/v1/analyses/{analysis_id}/answer/compare-agentic
```

The MCP server exposes `compare_agentic_retrieval`. Both invoke the production comparison
service and return independent runs and failures. Expected paths may be supplied for
objective scores; without labels the UI must describe the result as trajectory-only.
