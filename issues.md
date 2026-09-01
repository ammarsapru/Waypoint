# Known Issues Log

Bugs found and fixed during development, kept here so the reasoning and evidence
aren't lost once the code around them changes. Newest first.

---

## Forced synthesis can submit a stub refusal with no quality gate (2026-07-31, open)

**Symptom:** during the live caching measurement, an Ask question against Flask
completed HTTP 200 but returned `answer: "test", basis: "test", citations: [],
refused: true` — the Opus synthesis round emitted an 84-output-token stub instead of
a real answer, after three perfectly good Sonnet investigation rounds (22 files
inspected, on-topic tool calls throughout).

**Root cause (partial):** `refused: true` legitimately bypasses citation validation
(a genuine refusal can't cite evidence), which makes it the shortest *legal* path out
of a forced `submit_answer` call — a lazy submission that would be rejected as an
answer sails through as a refusal. The immediately-following question in the same
conversation, through the identical code path (same caching, same forced tool_choice,
same models), produced a correct 11-citation answer — so this is intermittent model
behavior exploiting the escape hatch, not a deterministic code bug, and not caused by
the prompt-caching change (verified by the clean second run).

**Status: open, documented during code freeze rather than fixed.** A plausible fix is
a bounded retry when a refusal arrives with a near-empty answer/basis (mirroring the
existing rejection-retry machinery), but that changes loop behavior — post-freeze
work. If it recurs during demo rehearsal, treat as a bug fix then.

**Files:** `backend/app/agent/service.py` (observed), `/c/tmp/ask1.json` +
`model.response_received` trace for round 4 (evidence).

---

## Ask workspace: evidence panel showed the previous question's citations while a new one was loading (2026-07-30)

**Symptom:** asking a follow-up question in Ask kept the previous answer's evidence
files visible in the right-hand panel while the new question was still in flight, then
sometimes appeared to show nothing once the new answer arrived.

**Root cause:** `AskWorkspace.tsx` computed `activeAnswer` as "the most recent turn
*that has a non-null answer*" (`turns.slice().reverse().find((turn) => turn.answer)`).
`ask()` appends a new turn with `answer: null` immediately on submit, before the
request resolves — so while it was loading, that lookup skipped the pending turn and
fell back to the *previous* turn's answer, whose citations then rendered as if they
belonged to the new question.

**Fix:** `activeAnswer` now reads only the latest turn
(`turns[turns.length - 1]?.answer ?? null`), so it's `null` (not a stale answer) the
instant a new question is submitted. Added a distinct "Gathering evidence…" loading
state to the evidence panel for that `null`-during-`loading` case, rather than letting
it fall through to the same empty state used before any question has ever been asked.

**Files:** `frontend/src/components/AskWorkspace.tsx`

---

## Dual-role model architecture: unify to one shared provider cascade instead of independent per-role cascades (2026-07-29)

**Follow-up to the entry below.** That entry's `_create_for_role_cascade` gave
investigation and synthesis genuinely *independent* provider cascades - each role
could land on a different provider within the same conversation (e.g. investigation
on Bedrock, synthesis falling to the Anthropic API after a transient Bedrock
hiccup). That's exactly the precondition for that entry's Finding 3 latent
SimpleNamespace crash: both roles share one `messages` history, so if either role's
own cascade landed on a different provider than an earlier round used, a later
round's real Anthropic SDK client would crash trying to serialize Bedrock-shaped
blocks left over from an earlier round.

Rather than patch the serialization (normalizing every adapter's response shape
before it re-enters history), redesigned the routing so the crash is impossible by
construction: **investigation and synthesis now resolve ONE shared provider per
conversation**, and role selects only the model within whichever tier that shared
cascade lands on.

- Replaced `_role_bedrock_model`/`_role_endpoint`/`_create_for_role_cascade` with
  `_role_model_for_provider(role, provider)` - a pure lookup table (role,
  resolved-tier) -> model name - and `_create_dual_cascade(role, preferred_provider,
  request)`, which runs the exact same Bedrock -> Anthropic API -> Bedrock retry ->
  Claude Code shape as the single-model cascade, just asking
  `_role_model_for_provider` which model to request at each tier. Claude Code uses
  one shared model regardless of role (config.py has no per-role
  `claude_code_model`), unlike Bedrock and the Anthropic API.
- `investigation_provider`/`synthesis_provider` settings are no longer read by the
  live cascade at all (both roles always run the same cascade now) - kept in
  `config.py` only because `comparison.py`'s separate `/answer/compare`
  benchmarking feature (`create_for_endpoint`) still reads them directly for its own
  head-to-head comparison runs.
- Added conversation-scoped provider preference: the first round of a conversation
  resolves a provider normally; every later round in the SAME conversation
  (`agent/service.py`'s `answer()`, `onboarding.py`'s tour/mission loops,
  `issues.py`'s `investigate()`) passes that resolved tier back in as
  `_waypoint_conversation_provider`, which `_create_dual_cascade` moves to the front
  of its attempt order - but still falls through normally if that preferred tier
  itself fails this time, so a genuine outage doesn't get stuck retrying a dead
  tier.
- Deliberately did NOT implement this as a mutation of the process-wide
  `ModelProviderRouter.active_provider` singleton (unlike the existing, correct
  "Claude Code sticky" fallback, which reflects a genuine process-wide outage) -
  that would leak one user's transient Bedrock hiccup into every other concurrent
  user's conversation. The preference is passed in and read back per-request
  instead.
- `initialize()`'s dual-role branch and the `available` property no longer validate
  two independent role endpoints (the old `_role_endpoint` they called is gone);
  both now just check that at least one provider tier (Bedrock, Anthropic API, or
  Claude Code) is configured for the shared cascade.

**Verified end-to-end (2026-07-29):** a live `/answer` request against a small
analysis session completed with `HTTP 200` - all 3 investigation rounds resolved to
`bedrock:us.anthropic.claude-sonnet-4-6` and the forced synthesis round resolved to
`bedrock:us.anthropic.claude-opus-4-6-v1` - same provider tier throughout, a
different model per role, exactly as designed.

**Files:** `backend/app/agent/provider.py`, `backend/app/agent/service.py`,
`backend/app/agent/onboarding.py`, `backend/app/agent/issues.py`,
`backend/tests/test_provider.py`, `.env`

---

## Dual-role model architecture: cascade parity, error classification, and finding a working Opus (2026-07-29)

**Goal:** enable `WAYPOINT_MODEL_ARCHITECTURE=dual` (separate models for the agent's
`investigation` tool-use rounds vs. its final `synthesis` answer) while preserving
the same Bedrock -> Anthropic API -> Bedrock retry -> Claude Code resilience the
single-model path already has.

**Finding 1 - the dual-role path didn't have that resilience at all.**
`ModelProviderRouter.create_for_role`'s `"auto"` provider only tried the Anthropic
API then Claude Code - no Bedrock leg, at all, for either role. Flipping dual mode
on as-is would have silently dropped Bedrock from the path entirely. Fixed by
adding `_create_for_role_cascade` (mirrors the single-model cascade, scoped to each
role's own Bedrock model id vs. API model name - these are different strings for
the same model) and `_role_bedrock_model`; `_provider_available("auto")` was also
missing `bedrock_region` from its check. New required settings:
`investigation_bedrock_model`, `synthesis_bedrock_model`.

**Finding 2 - `is_billing_or_capacity_error` didn't recognize AWS's Marketplace
entitlement error.** Bedrock raises `AccessDeniedException` (not a billing error by
name) when a specific model needs an AWS Marketplace subscription the account
hasn't completed. That's exactly the kind of failure the cascade should advance
past, but the classifier didn't match it, so the whole request 502'd instead of
falling back. Fixed by adding `"marketplace"` and `"not authorized to perform"` to
the recognized signal phrases.

**Finding 3 - a separate, pre-existing cross-provider bug, surfaced (not caused) by
Finding 2's fix.** Once the classifier correctly let the cascade fall through from
a Marketplace-blocked Bedrock model to the Anthropic API, the Anthropic SDK crashed
locally with `TypeError: Object of type SimpleNamespace is not JSON serializable`
before a request was even sent. Bedrock's response adapter returns tool-use blocks
as `SimpleNamespace` objects; those get appended into conversation history during
investigation rounds, and a real Anthropic SDK client can't serialize them once a
conversation crosses providers mid-flight. This has always been latent in the
single-model cascade too - it just never gets exercised, because Bedrock's
confirmed-working model (`sonnet-4-6`) essentially never fails there. **Not fixed**
- avoided instead, by landing on a Bedrock model for synthesis that doesn't need to
fall back (see Finding 5). Fixing this properly means normalizing every provider
adapter's response shape before it re-enters message history.

**Finding 4 - AWS gives no list API for this account.** `bedrock:ListFoundationModels`
and `bedrock:ListInferenceProfiles` are both denied for the configured IAM user
(invoke-only credentials). The only reliable way to check model access is direct
invocation - and even that can be misleading with a single sample: `claude-opus-4-1`
and `claude-haiku-4-5` both returned a false "OK" on one attempt before failing
consistently on repeated tool-forced calls. AWS's Marketplace-entitlement
enforcement on this account is inconsistent, not absent. Confirmed empirically
(2026-07-29):
- `us.anthropic.claude-sonnet-4-6` - reliably works.
- `us.anthropic.claude-opus-4-1-20250805-v1:0`, `us.anthropic.claude-haiku-4-5-20251001-v1:0`
  - real models, blocked by an incomplete Marketplace subscription (self-service:
  AWS Console -> Bedrock -> Model access).
- `claude-sonnet-4-5`, `claude-opus-4-5`, `claude-sonnet-4-7`, `claude-sonnet-4-8`,
  `claude-haiku-5` - do not exist on Bedrock under any naming convention tried.
- `claude-sonnet-5`, `claude-opus-5`, `claude-fable-5` - real models, but this
  account isn't eligible (`AccessDeniedException: ... is not available for this
  account ... contact AWS Sales`) - an account-level gate, not self-service.
- `openai.gpt-oss-120b-1:0` - fully accessible, no entitlement issue at all, but
  benchmarked at 1/50 success on Waypoint's agentic tool-use suite (vs. 49/49 for
  Sonnet) - see `benchmarks/results/agentic-v4-bedrock-claude-vs-gptoss.md`. It
  repeatedly ignored a *forced, single-tool* `tool_choice` (calling
  `search_repository` when `submit_answer` was the only tool present in the
  request) and inconsistently invented parameter names (`max_results` instead of
  the schema's `limit`) - a real capability gap, not a fluke.

**Finding 5 - `us.anthropic.claude-opus-4-6-v1` works, reliably.** Found by
inspecting the AWS Bedrock model catalog directly (console), not by guessing a
naming pattern - confirmed with 3/3 tool-forced `converse()` calls. This is the
Opus counterpart to the already-working `sonnet-4-6`, on the same account, with no
Marketplace or Sales gate. `synthesis_bedrock_model` now points at this model
instead of the blocked Opus 4.1, so synthesis succeeds on the primary cascade tier
every time and never needs to exercise Finding 3's latent bug.

**Verified end-to-end (2026-07-29):** a live `/answer` request completed with
`HTTP 200`, 3 investigation rounds on `bedrock:us.anthropic.claude-sonnet-4-6`
(via the `claude-sonnet-5` role model) and synthesis on
`bedrock:us.anthropic.claude-opus-4-6-v1` - both tiers succeeding on the first
attempt, no fallback triggered.

**Files:** `backend/app/agent/provider.py`, `backend/app/config.py`,
`backend/tests/test_provider.py`, `backend/tests/conftest.py`, `.env`

---

## SQLite: persistent connections could leak an uncommitted transaction (2026-07-29)

**Symptom:** immediately after switching every store to a persistent per-thread
connection (see the entry below), the full backend test suite went from 115
passing to 39 failing, all with `sqlite3.OperationalError: database is locked`,
in methods that hadn't been touched (e.g. `AnalysisSessionStore._persist`).

**Root cause:** several write methods ended with an explicit `connection.commit()`
at the bottom, with no `try`/`except` around the statements above it. Under the
old "open a connection, do one thing, close it" pattern this was harmless -
closing a SQLite connection with a pending transaction implicitly rolls it back,
so an exception between the first write and the final `commit()` silently
discarded whatever had been written so far. Once connections started staying
open across calls, that safety net disappeared: an exception in that window now
left the persistent connection sitting on an uncommitted write lock forever,
which blocks every other store sharing the same SQLite file (they all point at
one `settings.state_path`) until the process restarts.

The concrete trigger: `PersistentCodeIndex.ensure_vectors()` ran one write
(`INSERT OR IGNORE ... SELECT`) to copy vectors forward from a prior identical
revision, then `return`ed early - without committing - whenever every chunk
already had a vector for that revision. Many tests reuse byte-identical
repository fixtures across test methods, so the second time any test vectorized
the same revision, `missing` was empty, the method returned early, and the
connection was left mid-transaction for the rest of the test session.

**Fix:** replaced every `connection.commit()` with `with connection:` (SQLite's
own transaction context manager - commits on clean exit, including on an early
`return`, and rolls back on any exception) around each store's write methods:
`TourStateStore._persist`, `AnalysisSessionStore._persist`,
`ConversationStore.get_or_create`/`append_turn`, `FindingStore.put`,
`PersistentCodeIndex.ensure_vectors`/`delete_revision`. `PersistentCodeIndex.publish`
already managed its transaction manually (`BEGIN IMMEDIATE` + explicit
`rollback()` on one deliberate early-return path); it now wraps its whole body in
`try`/`except: connection.rollback(); raise` so *any* exception rolls back, not
just the one path someone had already thought to handle.

**Files:** `backend/app/agent/retrieval.py`, `backend/app/onboarding/service.py`,
`backend/app/graph/store.py`, `backend/app/agent/memory.py`,
`backend/app/agent/issues.py`

---

## SQLite: every request paid a WAL checkpoint against the shared state file (2026-07-29)

**Symptom:** `GET /api/v1/analyses` (and effectively every endpoint that touches
`settings.state_path`) consistently took ~10-12 seconds, even though the
underlying query is a single indexed `SELECT ... LIMIT 50`.

**Root cause:** every store (`AnalysisSessionStore`, `TourStateStore`,
`ConversationStore`, `FindingStore`, `PersistentCodeIndex`) opened a brand new
`sqlite3.connect()` at the start of every method and closed it at the end. All
five point at one shared file (`settings.state_path`), which had grown past 1GB
over the course of this project's benchmarking sessions. SQLite tries to
checkpoint (fold the WAL sidecar back into the main file) whenever the last open
connection to a database closes - with no persistent connection anywhere, that
was effectively *every single call*, and checkpointing a ~1GB file costs real,
measurable time, especially on a disk with sync/filter-driver overhead (see the
OneDrive entry below).

**Fix:** every store now keeps one SQLite connection open per thread
(`threading.local()`), created lazily on first use and reused for the life of
the process, instead of reopening one per call. FastAPI dispatches these
synchronous store methods across a small worker-thread pool, so a thread-local
connection is never touched by more than one thread at a time - no new locking
was needed beyond wrapping connection creation itself in the store's existing
lock, to stop two threads from racing to run the first-time schema migration.
Checkpointing now only happens via SQLite's normal size-based
`wal_autocheckpoint` trigger, not on every call.

Each store also grew a `close()` method (closes and discards the current
thread's cached connection) purely for tests: a test that builds a store against
a temp directory and deletes that directory immediately afterward needs to
release the file handle first, or `shutil.rmtree` raises `PermissionError` on
Windows. `backend/tests/conftest.py` now closes every module-level singleton
store in a session-scoped teardown fixture, and the session-wide temp directory
is constructed with `ignore_cleanup_errors=True` as a backstop for any ad hoc
store a test builds directly against the default state path.

**Impact (measured):** the full backend test suite went from ~500s to ~32s
(115 tests, same suite, same machine). `GET /api/v1/analyses` against a live
1.05GB state file went from ~11.8s to ~0.4s once combined with the two entries
below.

**Files:** `backend/app/graph/store.py`, `backend/app/onboarding/service.py`,
`backend/app/agent/memory.py`, `backend/app/agent/issues.py`,
`backend/app/agent/retrieval.py`, `backend/tests/conftest.py`

---

## SQLite state file lived inside the OneDrive-synced project folder (2026-07-29)

**Symptom:** contributed to the same multi-second-per-request slowness above.

**Root cause:** the default `WAYPOINT_STATE_PATH`
(`.waypoint-data/waypoint.sqlite3`) resolves relative to the project root, which
sits inside a folder OneDrive actively syncs. Every write is intercepted by
OneDrive's cloud-files filter driver (checking hydration state, flagging the
file dirty for re-upload), adding overhead roughly proportional to file size on
top of whatever SQLite itself needs to do.

**Fix:** `WAYPOINT_STATE_PATH` and `ONBOARD_TRACE_PATH` are now set in `.env` to
`%LOCALAPPDATA%\Waypoint\`, outside any synced folder. No code change was
needed - both were already environment-configurable
(`backend/app/config.py`). The existing 1GB state file and trace log were
copied (not moved) to the new location so no history was lost; the original
copy under `.waypoint-data/` was left in place untouched.

**Files:** `.env`

---

## Legacy session rows forced a full JSON parse on every "recent analyses" list (2026-07-29)

**Symptom:** even after both fixes above, `GET /api/v1/analyses` was still slow
against the real (not a fresh test) state file.

**Root cause:** `AnalysisSessionStore.list_recent_summaries()` reads
`repository_name`/`files_parsed`/`node_count`/`edge_count` straight from their
own columns to avoid parsing each session's full `report_json` blob - the fix
for the *original* version of this same slow-dropdown problem (see below). But
those columns were added after many sessions already existed; rows persisted
before that migration have `repository_name IS NULL`, and the method's
documented fallback for that case is to fully restore and Pydantic-validate the
row's report anyway. Of the 47 accumulated sessions in this project's
development database, 42 (89%) predated the columns - some with `report_json`
upwards of 15MB - so nearly every listing request was doing 42 full JSON parses
regardless of how fast the SQL layer itself was.

**Fix:** one-off backfill: for every row with `repository_name IS NULL`, parse
`report_json` once and write `repository_name`/`files_parsed`/`node_count`/
`edge_count` into their columns. Purely additive (no existing column was
touched), took ~13s total, one time. Not a code change - if it recurs (e.g. a
state file restored from an old backup), the same backfill can be re-run; it's
idempotent since it only touches rows that are still `NULL`.

---

## Recent-analyses dropdown fully parsed every stored report just to list it

**Symptom:** the "recent analyses" picker took multiple seconds to open once a
few sessions had accumulated, worse for large repositories.

**Root cause:** `list_recent()` (and the dropdown route backing it) restored and
Pydantic-validated every stored session's full `AnalysisReport` - tens of
megabytes of node/edge JSON for a large repository - just to show four summary
numbers (repository name, files parsed, node count, edge count) per row.

**Fix:** added `AnalysisSessionStore.list_recent_summaries()`: a single SQL
`SELECT` restricted to dedicated summary columns, populated at write time in
`_persist()` since the values are already in memory there. Never touches
`report_json`. Rows without the columns yet (see the entry above) fall back to
one bounded `_restore()` call each. The same pattern was later applied to
`TourStateStore.list_tours()` for the onboarding-session picker, specifically to
avoid repeating this mistake.

**Files:** `backend/app/graph/store.py`, `backend/app/api/routes.py`,
`backend/app/mcp_server.py`

---

## Onboarding tour state used bare attributes instead of a dict keyed by tour_id

**Symptom:** `TourStateStore` (a process-lifetime singleton, correctly so - the
same pattern `analysis_sessions`/`conversations`/`ai_findings` all use) held a
tour's per-tour data - name, role, goal, plan - as bare scalar attributes
(`self.name`, `self.details`, etc.) rather than dicts keyed by `tour_id`. Since
the singleton is one shared object for the process's whole life, every new tour
overwrote the *same* attribute, so only the most recently touched tour's data
existed at any moment - not scoped per tour, per user, or per analysis session.

**Fix:** every per-tour value now lives in a `dict[tour_id, value]` on the
store (`_plan_json`, `_analysis_ids`, `_names`, alongside the pre-existing
`_challenges`/`_mastered`/`_attempts`), matching the pattern `_challenges`
already used correctly. A value with no key attached to it is the actual bug -
a dict key (in memory) or a primary key (in SQLite) is what turns a global
scalar into a value scoped to one entity; the singleton itself was never the
problem.

**Files:** `backend/app/onboarding/service.py`

---

## `ALTER TABLE ADD COLUMN` migration produced an invalid column name

**Symptom:** `sqlite3.OperationalError: no such column: analysis_id` when the
new onboarding-tours migration ran.

**Root cause:** `f"ALTER TABLE onboarding_tours ADD COLUMN {column}{ddl}"` was
missing a space between the column name and its type/default clause, producing
literal SQL like `ADD COLUMN analysis_idTEXT NOT NULL DEFAULT ''` - a single
(invalid) identifier, not a column name followed by a type.

**Fix:** added the missing space:
`f"ALTER TABLE onboarding_tours ADD COLUMN {column} {ddl}"`.

**Files:** `backend/app/onboarding/service.py`
