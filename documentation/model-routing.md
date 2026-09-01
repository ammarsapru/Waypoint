# Model routing: single-model and dual-role architecture

Waypoint routes every model call through one shared entry point,
`model_provider_router.client().messages.create(...)` (`backend/app/agent/provider.py`).
Callers never choose a provider directly; they describe the request and, in dual-role
mode, which role (`investigation` or `synthesis`) it's for.

## Single-model mode (`WAYPOINT_MODEL_ARCHITECTURE=single`, the default)

One model handles an entire conversation. Each request runs an ordered cascade —
Bedrock (`settings.bedrock_primary_model`), the Anthropic API, Bedrock retried, then the
Claude Code subscription fallback — advancing to the next tier only on a recognized
billing/capacity error (`is_billing_or_capacity_error`); any other exception raises
immediately rather than being masked as a provider problem. Reaching Claude Code makes
the switch sticky for the rest of the process (`ModelProviderRouter.active_provider`),
so a single bad request doesn't permanently exile Bedrock/the API after one hiccup, but
a genuine full-cascade exhaustion does stay on Claude Code until the process restarts.

```env
WAYPOINT_MODEL_ARCHITECTURE=single
WAYPOINT_MODEL=claude-sonnet-5
```

## Dual-role mode (`WAYPOINT_MODEL_ARCHITECTURE=dual`)

Two roles do different jobs within the **same** conversation:

1. **Investigation** — selects repository tools, searches the index, reads bounded
   source ranges, and expands graph relationships. Cannot finalize the answer.
2. **Synthesis** — receives the accumulated inspected evidence and conversation
   context and must call the workflow's submission tool (`submit_answer`,
   `submit_onboarding_tour`, etc.), citing only evidence actually inspected.

### One shared provider per conversation, not independent per-role routing

Both roles run through **one shared cascade** — the same Bedrock → Anthropic API →
Bedrock retry → Claude Code order as single-model mode — and only the *model name*
requested at each tier differs by role (`_role_model_for_provider(role, provider)`, a
pure lookup table):

| Tier | Investigation model | Synthesis model |
|---|---|---|
| Bedrock | `WAYPOINT_INVESTIGATION_BEDROCK_MODEL` | `WAYPOINT_SYNTHESIS_BEDROCK_MODEL` |
| Anthropic API | `WAYPOINT_INVESTIGATION_MODEL` | `WAYPOINT_SYNTHESIS_MODEL` |
| Claude Code | `WAYPOINT_CLAUDE_CODE_MODEL` (shared — one setting, not per-role) | same |

This is a deliberate redesign (2026-07-29) that replaced an earlier version where each
role had its **own independent** provider cascade via `WAYPOINT_INVESTIGATION_PROVIDER`/
`WAYPOINT_SYNTHESIS_PROVIDER`. That design could land investigation and synthesis on two
different providers within one conversation — and since both roles share one `messages`
history, a later round served by a different provider than an earlier round crashed the
real Anthropic SDK trying to `json.dumps()` Bedrock-shaped `SimpleNamespace` response
objects left in history from an earlier round. Rather than normalize every adapter's
response shape to patch that crash, the routing itself was simplified so the crash is
impossible by construction: there is only ever one resolved provider per conversation.

Each multi-round conversation loop (`agent/service.py`'s `answer()`, `onboarding.py`'s
tour and mission loops, `issues.py`'s `investigate()`) tracks which tier its first round
resolved to (parsed from the response's `waypoint_provider` field, e.g. `"bedrock"` out
of `"bedrock:us.anthropic.claude-sonnet-4-6"`) and passes it back in on every later round
as `_waypoint_conversation_provider`. `_create_dual_cascade` moves that tier to the front
of its attempt order — but still falls through to the remaining tiers normally if the
preferred one itself fails on a later call, so a mid-conversation outage doesn't get
stuck retrying a dead tier.

`investigation_provider`/`synthesis_provider` still exist as settings in `config.py`,
but the live Ask/onboarding/issues cascade no longer reads them at all. They're kept only
because `comparison.py`'s separate `/answer/compare` benchmarking feature
(`create_for_endpoint`) uses them to force two *specific*, independently named
endpoints for a head-to-head comparison — a different use case from routing a live
conversation.

### Current, verified working example (Bedrock, both roles)

```env
WAYPOINT_MODEL_ARCHITECTURE=dual
WAYPOINT_INVESTIGATION_MODEL=claude-sonnet-5
WAYPOINT_INVESTIGATION_BEDROCK_MODEL=us.anthropic.claude-sonnet-4-6
WAYPOINT_SYNTHESIS_MODEL=claude-opus-5
WAYPOINT_SYNTHESIS_BEDROCK_MODEL=us.anthropic.claude-opus-4-6-v1
```

Verified end-to-end on 2026-07-29: a live `/answer` request completed with `HTTP 200`,
all investigation rounds resolved to `bedrock:us.anthropic.claude-sonnet-4-6`, and the
forced synthesis round resolved to `bedrock:us.anthropic.claude-opus-4-6-v1` — same
provider tier throughout, a different model per role. Bedrock and Anthropic-API model
identifiers are different strings for the same logical model family (e.g.
`us.anthropic.claude-sonnet-4-6` vs. `claude-sonnet-5`), hence separate
`*_BEDROCK_MODEL` settings per role. AWS gives no `ListFoundationModels`/
`ListInferenceProfiles` permission for this account's IAM user, so direct trial
invocation was the only way to confirm which Bedrock model IDs actually work; see
`issues.md` for the full empirical record (including several Bedrock model IDs that
exist but are Marketplace- or account-gated, and IDs that don't exist at all).

## Limits and behavior (both modes)

- `WAYPOINT_INVESTIGATION_ROUNDS` (default 3, range 1-10) controls when the Ask agent
  switches to forced synthesis once it has inspected evidence.
- `WAYPOINT_AGENT_MAX_TOOL_ROUNDS` (default 8, range 1-20) remains the hard outer-loop
  ceiling regardless of architecture.
- `WAYPOINT_SYNTHESIS_MAX_ATTEMPTS` (default 2, range 1-5) prevents repeated expensive
  final-model retries.
- `WAYPOINT_CLAUDE_CODE_MAX_TURNS` (default 8, hard range 1-8) limits internal Claude
  Agent SDK turns for one model request — a different limit from the outer tool-round
  ceiling; one Waypoint round is one provider request, which may itself consume several
  internal SDK turns.
- `WAYPOINT_CLAUDE_CODE_MAX_BUDGET_USD` limits one Claude Agent SDK session; it is not a
  cumulative per-question ledger because each outer round is a separate SDK session.
- The final answer still passes Pydantic validation and must cite source spans actually
  returned by retrieval tools, regardless of which provider produced it.
- Structured logs include `model.role_routed`, `model.dual_architecture_initialized`,
  the resolved `routed_provider` per round, token usage, and provider-reported estimated
  cost when available.

Restart the backend after changing any provider/model environment variable — settings
are loaded once at process import time (`backend/app/config.py`).

## Related implementation

- `backend/app/agent/provider.py` — `ModelProviderRouter`, `_role_model_for_provider`,
  `create_for_role`, `_create_dual_cascade`, `is_billing_or_capacity_error`.
- `backend/app/agent/service.py` — the Ask `answer()` loop's conversation-provider
  tracking.
- `backend/app/agent/onboarding.py`, `backend/app/agent/issues.py` — the equivalent
  tracking in the tour/mission/issue-investigation loops.
- `backend/app/agent/comparison.py` — the separate `create_for_endpoint` path used only
  by `/answer/compare`, which still reads `investigation_provider`/`synthesis_provider`.
- `backend/app/config.py` — all settings referenced above.
- `backend/tests/test_provider.py` — dual-role cascade tests, including conversation-
  scoped provider-preference behavior.
