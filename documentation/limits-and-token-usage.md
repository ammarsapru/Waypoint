# Limits and token usage

This merges what were two separate documents (`limits.md` and
`token-usage-and-metering.md`) — they covered the same subject (bounded resource
consumption) from two angles that belong together. For which provider/model actually
serves a request, see [`model-routing.md`](model-routing.md); the two topics used to be
mixed into `limits.md` and are now split cleanly.

## Agent-loop limits

| Limit | Current value | Purpose |
|---|---:|---|
| Outer Waypoint tool rounds | 8 by default (`WAYPOINT_AGENT_MAX_TOOL_ROUNDS`), range 1-20 | Hard ceiling for evidence and submission rounds |
| Claude Agent SDK turns per request | 8 by default (`WAYPOINT_CLAUDE_CODE_MAX_TURNS`), hard range 1-8 | Bounds internal Claude Code turns used to produce one action batch |
| Dual-mode investigation rounds | 3 by default (`WAYPOINT_INVESTIGATION_ROUNDS`), range 1-10 | Switches to synthesis after evidence has been gathered |
| Synthesis attempts | 2 by default (`WAYPOINT_SYNTHESIS_MAX_ATTEMPTS`), range 1-5 | Prevents repeated expensive final-model retries |
| Primary model maximum output | 6,000 tokens by default (`WAYPOINT_AGENT_MAX_OUTPUT_TOKENS`), range 512-32,000 | Bounds direct Messages API/Bedrock/OpenRouter output |
| Stored chat history sent to the model | 12 turns by default (`WAYPOINT_CHAT_HISTORY_TURNS`), range 1-50 | Bounds conversational context |

`WAYPOINT_CLAUDE_CODE_MAX_BUDGET_USD`, when configured above zero, limits one Claude
Agent SDK session. It is not a cumulative budget for the entire user question because an
outer Waypoint loop can create more than one SDK session.

The outer tool rounds and Claude Code SDK turns are different limits: one Waypoint agent
round makes one provider request, and a Claude Code-backed provider request may
internally consume multiple SDK turns. Neither loop is unlimited.

## Onboarding limits

Supported time budgets are 5, 15, 30, 60, 90, and 120 minutes. The backend accepts any
value from 5 through 120 minutes; the UI exposes only the intervals listed above.

| Field | Limit |
|---|---:|
| Tour steps | 2-10 |
| Files attached to one tour step | 1-8 |
| Expected concepts for one challenge | 1-8 |
| Contribution-mission suggested files | 1-12 |
| Mission checklist entries | 2-12 |
| Definition-of-done entries | 2-12 |

## Ask citation and answer limits

Waypoint does not restrict an answer to one citation per file — a final answer may cite
multiple distinct passages from the same file. The frontend groups those passages under
one file card and initially opens the first passage.

| Citation or answer field | Limit |
|---|---:|
| Total citations in one answer | 30 |
| Citation title | 200 characters |
| Citation relevance explanation | 500 characters |
| Full answer | 30,000 characters |
| Answer basis | 2,000 characters |
| Suggested follow-up questions | 5 |
| Displayed citation excerpt | First 8 lines and at most 1,600 characters |

There is no explicit schema-level maximum number of lines in a citation. Each citation
must have a positive start line, an end line at or after the start line, and its entire
range must be contained in evidence previously inspected by the agent. The direct
`read_source` tool returns at most 250 lines per call, which practically bounds
citations based on that tool to its inspected range; specialized semantic and graph
tools commonly return smaller evidence spans.

## Token usage and metering

### Hard model and loop limits

- `WAYPOINT_AGENT_MAX_OUTPUT_TOKENS`, default 6,000 and clamped to 512-32,000, limits
  the maximum generated tokens for primary agent calls.
- Assessment calls use a smaller fixed 1,800-token output limit.
- The API startup probe uses only eight maximum output tokens.
- The Claude Code health probe asks for at most 32 output tokens.
- Final rounds force the appropriate submission tool once evidence exists, reducing
  indefinite browsing.

### Input-context controls

- Chat memory retains the most recent `WAYPOINT_CHAT_HISTORY_TURNS` user/assistant
  turns, default 12, clamped to 1-50.
- `search_repository` defaults to 12 results and accepts up to 50 chunks per call
  (`SearchInput`, `backend/app/agent/tool_contracts.py:25-28`).
- `read_source` returns at most 250 lines per call.
- Tree listings, graph depth, feature candidates, entry points, related tests,
  diagnostics, and dependency impact all have explicit bounds.
- The agent receives selected chunks and tool results rather than the entire repository.
- Tool descriptions steer broad questions to high-yield semantic tools, reducing
  repeated generic search.
- Independent tools can run in one round, reducing round count even though their result
  contents still consume context.

### Work reuse that reduces repeated retrieval work

- Immutable repository revisions reuse persisted chunks and exact-content local vectors.
- SQLite FTS/vector indexes avoid rebuilding search state for every question.
- Semantic tool results are cached in process by tool name and arguments.
- Conversation history is persisted so follow-ups do not start without context.

These caches reduce compute and repeated retrieval — separately, as of 2026-07-30,
Waypoint also sets real model prompt-caching breakpoints (see below), so this is no
longer the only cache layer in the system.

### Model prompt caching (added 2026-07-30)

`backend/app/agent/provider.py` marks the system prompt and the tail of the growing
message history as cache breakpoints before dispatching to either cascade tier that
supports it, so a later round of the same multi-round loop (Ask, onboarding, issue
investigation) gets a cache read for everything already sent in an earlier round
instead of being rebilled at full input price for the whole accumulated conversation:

- **Bedrock Converse** (`BedrockConverseMessages.create`, `_bedrock_supports_prompt_caching`) —
  appends a `cachePoint` block to the system prompt and to the last message's content,
  gated to Claude model ids only (`"claude"`/`"anthropic."` in the model string). The
  same adapter also drives non-Anthropic comparison models (`comparison.py`), and
  cachePoint support isn't confirmed for every family Converse hosts, so it's opt-in by
  model rather than applied to every Bedrock request.
- **Direct Anthropic API** (`_anthropic_cache_shape`, `_apply_anthropic_cache_shape`) —
  rewrites a plain-string `system` and the last message's content into
  `cache_control: {"type": "ephemeral"}`-tagged blocks.
- Both providers surface `cache_read_input_tokens`/`cache_creation_input_tokens` on the
  response's `usage` object when a request actually hit a breakpoint.
- Not yet covered: OpenRouter, Gemini, and the Claude Code fallback tier keep their
  existing (uninstrumented) request shape — caching was scoped to the two tiers that are
  actually load-bearing for this deployment (Bedrock primary, Anthropic API fallback).

**Live-measured 2026-07-31** (two real Ask questions against the Flask analysis,
dual mode, Bedrock): AWS accepted the `cachePoint` shape and within-question caching
works as designed — by round 3 of an investigation, 18,649–25,750 tokens were served
as cache reads with only a single token billed at full input price, making the
investigation phase ~28.5% cheaper than its uncached equivalent (round 3 alone:
66–73% cheaper). Two measured limitations: (1) **dual-mode synthesis gets zero cache
benefit and adds pure waste** — prompt caches are model-scoped, so the Opus synthesis
round can never read the Sonnet investigation cache; it instead *wrote* a
26,773–31,477-token cache at the 1.25x write premium that nothing ever reads
(conversations end at synthesis), cutting the measured net whole-question saving to
~3%. Skipping cache markers on terminal synthesis rounds (a small post-freeze change)
would raise the net saving to roughly 12–15% per question at these context sizes.
(2) **Cross-question reads require activity within the ~5-minute cache TTL** — a
14-minute gap between the two measured questions expired the cache, and conversation
memory replays only Q&A turns (not tool traffic), so the follow-up question's fresh
prefix was small anyway.

### What Waypoint currently meters

The Ask loop records the provider-reported `input_tokens`/`output_tokens` (and, since
2026-07-30, `cache_read_input_tokens`/`cache_creation_input_tokens`) for each model
response in structured logs (`backend/app/agent/service.py`). The issues agent
(`backend/app/agent/issues.py`, `_log_model_response`) gained the equivalent per-round
logging on 2026-07-30 — previously only Ask had this. Claude Code usage is translated
into the same response shape by `backend/app/agent/provider.py`. Logs also record
provider/model and round, message and tool counts, tool duration and serialized result
bytes, inspected spans/files, answer citation counts, and request failures/provider
fallback. This is operational telemetry, not complete billing metering.

### Cost estimation (added 2026-07-30)

`provider.py:estimate_model_cost_usd` multiplies a call's real token counts (including
cache reads/writes, at the standard ~0.1x/~1.25x multipliers of the input rate) by a
small hardcoded per-model price table, and the issues agent logs the result as
`estimated_cost_usd` alongside every round. This is explicitly **not authoritative** —
Bedrock's Converse API returns no dollar figure at all, so the table is a best-effort
estimate for observability (Bedrock has historically mirrored Anthropic's first-party
per-token pricing for the same model generation, but that's not independently confirmed
against a live Bedrock pricing API). AWS's own billing console remains the source of
truth. The Ask loop (`service.py`) does not yet call this helper — it still only logs
raw token counts, no estimated cost.

### Persisted usage ledger (added 2026-07-30)

`backend/app/agent/usage_ledger.py` closes the gap the section above describes.
`ModelUsageLedger` is a dedicated SQLite table (`model_usage`, in `settings.state_path`
alongside every other store) that gets one row per model call, via the shared
`record_model_response(workflow, response, ...)` helper — called from **every**
workflow's call site: `service.py`'s `answer()` (`workflow="ask"`), `onboarding.py`'s
tour/mission/assessment loops (`"onboarding_tour"`/`"onboarding_mission"`/
`"onboarding_assessment"`), and `issues.py`'s `investigate_issue()`/`investigate()`
(`"issue_investigation"`/`"issue_sweep"`). This exists specifically because the
general-purpose rotating trace log (see "What Waypoint currently meters" above) is
shared with every other kind of log event and rotates out real usage data within
hours — a dedicated table that only ever grows by one small row per model call doesn't
compete with that noise for retention. Not a substitute for a provider's billing
console: `estimated_cost_usd` is still the same best-effort figure described above.

A related, separate bug (also found and fixed 2026-07-30): `observability.py`'s
credential-redaction regex matched "token" as a substring of every token-*count* field
name (`input_tokens`, `max_tokens`, `cache_read_input_tokens`, ...), not just genuine
credential fields like `github_token` — so even where a usage event survived trace-log
rotation, its token counts had already been overwritten with the literal string
`"<redacted>"` at write time. Fixed via `_TOKEN_COUNT_KEY`, an exemption for field names
ending in plural `_tokens`/`token_count` (credentials in this codebase are always
singular — `github_token`, `access_token` — so the exemption cannot mask a real secret).
Already-written historical log lines cannot be un-redacted; this only prevents the loss
going forward.

`verification/scripts/cost_report.py` (`python -m verification.scripts.cost_report`)
reconstructs historical spend from whatever real token data still exists — the ledger,
surviving trace-log events, and every `benchmarks/results/agentic-v4-*.json` /
`verification/results/*.json` artifact with per-run token counts — and backfills
`estimated_cost_usd` for any of them missing one, via the same pricing table. Output:
`verification/results/cost-reconstruction.md`. It documents its own known gaps
explicitly (no onboarding benchmark coverage at all, V1-V3 retrieval benchmarks predate
token tracking, the rendered frontend is never benchmarked for cost) rather than
implying completeness it doesn't have.

### What is not implemented yet

Waypoint does not currently provide:

- cumulative token totals in the UI (the data now exists in the `model_usage` ledger;
  nothing in the frontend surfaces it yet);
- an *authoritative* dollar-cost figure by provider/model (an estimate exists — see
  above; it is not a substitute for AWS's/Anthropic's own billing console);
- a hard daily/monthly monetary budget;
- per-workflow quotas or user billing;
- alerts at configured token/cost thresholds;
- persistence of Claude Code's `total_cost_usd` field (captured as
  `provider_reported_cost_usd` in the ledger when present, but not distinguished from
  the computed estimate in any UI);
- prompt caching on OpenRouter, Gemini, or the Claude Code fallback tier;
- any benchmark coverage at all for onboarding tours, missions, or assessments.

It would therefore still be inaccurate to claim Waypoint has complete cost metering,
but as of 2026-07-30 it has a real persisted usage record for every workflow, not just
Ask and issues.

## Ask composer behavior

When a question is submitted: the composer is cleared immediately, the user message is
inserted into the conversation immediately, a loading state is shown while the request
runs, the assistant response fills the pending conversation turn when it arrives, and if
the request fails the submitted user message remains visible with an error.

## Related implementation

- `backend/app/config.py` — model, history, round, turn, and output limits.
- `backend/app/agent/memory.py` — bounded persistent conversation history.
- `backend/app/agent/service.py` — outer rounds, forced submission, and usage logs.
- `backend/app/agent/provider.py` — cascade/turn limits, usage mapping,
  `_anthropic_cache_shape`/`_apply_anthropic_cache_shape` (Anthropic API prompt
  caching), `_bedrock_supports_prompt_caching` (Bedrock Converse `cachePoint` gating),
  and `estimate_model_cost_usd` (best-effort per-call dollar estimate).
- `backend/app/agent/issues.py` — `_log_model_response`, the per-round token/cache/
  estimated-cost logging for issue investigations.
- `backend/app/agent/usage_ledger.py` — `ModelUsageLedger`/`record_model_response`,
  the cross-workflow persisted SQLite usage table.
- `verification/scripts/cost_report.py` — reconstructs historical spend from the
  ledger, trace logs, and benchmark artifacts into one report.
- `backend/app/observability.py` — `sanitize`/`_TOKEN_COUNT_KEY`, the credential-
  redaction exemption for token-count field names.
- `backend/app/agent/retrieval.py` — bounded hybrid retrieval.
- `backend/app/agent/semantic.py` — semantic result cache.
