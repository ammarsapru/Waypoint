# Live run cost analysis — haxtheweb/praw vs open-notebook (2026-07-31)

Full write-up of the two dashboards built from the live test session run against
`haxtheweb/praw` and `lfnovo/open-notebook` on 2026-07-31, 05:22:41–05:51:49.
Everything here is reconstructed from the `model_usage` SQLite table (joined to
`analysis_sessions` for repository names) and the HTTP request-timing events in
`waypoint.jsonl` — no new instrumentation was added to produce it, and nothing
below required a code change. Pricing math matches
`backend/app/agent/provider.py`'s `estimate_model_cost_usd()` exactly:

| Model | Role | Input | Output | Cache write (1.25×) | Cache read (0.1×) |
|---|---|---|---|---|---|
| Sonnet 4.6 | investigation | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| Opus 4.6 | forced synthesis | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |

Two published artifacts carry the interactive/visual version of everything below:
- **Run comparison** (what actually happened): https://claude.ai/code/artifact/f591a73e-b57d-4cc3-8c1f-fcd4d409766f
- **Cost reduction projection** (what it would take to get cheaper): https://claude.ai/code/artifact/39199cd9-9479-4465-a6e3-8b2f19195da2

---

## Part 1 — Run comparison: what actually happened

### Headline numbers

| Metric | Value |
|---|---|
| Combined cost, both repos | **$4.0401** |
| Model calls | 37 (27 investigation, 10 synthesis) |
| True input tokens (raw + cache write + cache read) | 971,782 |
| Output tokens | 25,991 |
| True input : output ratio | **37.4 : 1** |
| Largest single output | 3,056 tokens (well under the 6,000/call cap) |

"True input" = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.
The raw `input_tokens` field alone is only the small uncached delta Bedrock reports
once a cache breakpoint is active — 1 to 25 tokens on every one of the 37 calls
tonight. Reading that field in isolation is what earlier made the ratio look
"flipped" (see the dedicated section below).

### Repo-by-repo comparison

| | haxtheweb/praw | open-notebook |
|---|---|---|
| Cost | $1.5204 | $2.5197 |
| Calls / requests | 16 / 4 | 21 / 5 |
| Total wall time | 219s | 347s |
| True input : output | 37.3 : 1 | 37.4 : 1 |
| Cache-read share of cached tokens | 30% | 35% |
| Workflows run | Ask ×2, Onboarding Tour, Onboarding Mission | Ask ×2, Onboarding Tour, Onboarding Mission, Issue investigation #1231 |

open-notebook cost 66% more than haxtheweb, mainly because its onboarding tour
needed a 5th round (an extra Opus round that alone cost $0.1286) and its cache
writes ran larger across the board (bigger repository, more evidence surfaced per
round).

### Every request, token composition round by round

Each request is investigation rounds (Sonnet, reduced tool list) followed by one
forced-synthesis round (Opus, expanded tool list including the submission tool).
Columns: **input** = raw uncached `input_tokens`; **write** = `cache_creation_input_tokens`
(billed 1.25×); **read** = `cache_read_input_tokens` (billed 0.1×); **output** =
output tokens for that round.

#### haxtheweb/praw

**Ask #1** — 4 rounds, 58.7s wall time, $0.4163
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 2,884 | 0 | 77 |
| 2 | investigation | 1 | 26,433 | 2,884 | 193 |
| 3 | investigation | 1 | 2,439 | 29,317 | 105 |
| 4 | synthesis | 21 | 37,753 | 0 | 1,836 |

**Ask #2** — 4 rounds, 47.7s wall time, $0.1419
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 948 | 2,884 | 81 |
| 2 | investigation | 1 | 785 | 3,832 | 97 |
| 3 | investigation | 1 | 4,443 | 4,617 | 1,224 |
| 4 | synthesis | 24 | 8,996 | 0 | 1,517 |

**Onboarding · Tour** — 4 rounds, 73.2s wall time, $0.5664
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 2,679 | 0 | 101 |
| 2 | investigation | 1 | 26,391 | 2,679 | 197 |
| 3 | investigation | 1 | 13,783 | 29,070 | 190 |
| 4 | synthesis | 25 | 52,976 | 0 | 2,306 |

**Onboarding · Mission** — 4 rounds, 39.2s wall time, $0.3957
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 5,282 | 0 | 119 |
| 2 | investigation | 1 | 19,679 | 5,282 | 213 |
| 3 | investigation | 1 | 5,991 | 24,961 | 235 |
| 4 | synthesis | 23 | 37,845 | 0 | 1,017 |

#### open-notebook

**Ask #1** — 4 rounds, 79.1s wall time, $0.5009
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 2,884 | 0 | 77 |
| 2 | investigation | 1 | 27,227 | 2,884 | 254 |
| 3 | investigation | 1 | 10,786 | 30,111 | 1,224 |
| 4 | synthesis | 24 | 40,833 | 0 | 2,358 |

**Ask #2** — 4 rounds, 78.7s wall time, $0.5023
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 1,208 | 2,884 | 152 |
| 2 | investigation | 1 | 17,961 | 4,092 | 147 |
| 3 | investigation | 1 | 12,455 | 22,053 | 185 |
| 4 | synthesis | 21 | 48,355 | 0 | 2,615 |

**Onboarding · Tour** — 5 rounds, 107.8s wall time, $0.8312
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 2,682 | 0 | 101 |
| 2 | investigation | 1 | 37,233 | 2,682 | 253 |
| 3 | investigation | 1 | 15,471 | 39,915 | 285 |
| 4 | synthesis | 25 | 64,295 | 0 | 2,820 |
| 5 | synthesis | 25 | 3,195 | 64,295 | 3,056 |

**Onboarding · Mission** — 4 rounds, 37.1s wall time, $0.3391
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 5,054 | 0 | 139 |
| 2 | investigation | 1 | 14,514 | 5,054 | 164 |
| 3 | investigation | 1 | 8,003 | 19,568 | 180 |
| 4 | synthesis | 23 | 31,988 | 0 | 840 |

**Issue investigation #1231** — 4 rounds, 44.1s wall time, $0.3463
| Round | Role | Input | Write | Read | Output |
|---|---|---|---|---|---|
| 1 | investigation | 3 | 3,612 | 0 | 135 |
| 2 | investigation | 1 | 12,802 | 3,612 | 121 |
| 3 | investigation | 1 | 9,869 | 16,414 | 175 |
| 4 | synthesis | 23 | 32,815 | 0 | 1,202 |

### Cost by workflow

| Workflow | haxtheweb/praw | open-notebook |
|---|---|---|
| Ask (both conversations) | $0.5582 | $1.0032 |
| Onboarding · Mission | $0.3957 | $0.3391 |
| Onboarding · Tour | $0.5664 | $0.8312 |
| Issue investigation | — (not run) | $0.3463 |

### Wall-clock latency per request

From the existing HTTP middleware (`http.response_sent` → `duration_ms`) — whole
request, all rounds included, not a per-model-call figure (the ledger has no
duration column; none was added to produce this).

| Repo | Endpoint | Rounds | Duration |
|---|---|---|---|
| haxtheweb | Ask #1 | 4 | 58.7s |
| haxtheweb | Ask #2 | 4 | 47.7s |
| haxtheweb | Onboarding Mission | 4 | 39.2s |
| haxtheweb | Onboarding Tour | 4 | 73.2s |
| open-notebook | Ask #1 | 4 | 79.1s |
| open-notebook | Ask #2 | 4 | 78.7s |
| open-notebook | Onboarding Mission | 4 | 37.1s |
| open-notebook | Onboarding Tour | 5 | 107.8s |
| open-notebook | Issue investigation | 4 | 44.1s |

### Cache warm-up within a conversation

Share of true input served from cache (`cache_read / (cache_read + cache_creation)`),
round by round, for both Ask conversations per repo:

| Round | haxtheweb #1 | haxtheweb #2 | open-notebook #1 | open-notebook #2 |
|---|---|---|---|---|
| 1 | 0% | 75% | 0% | 71% |
| 2 | 10% | 83% | 10% | 19% |
| 3 | 92% | 51% | 74% | 64% |
| 4 (synthesis) | 0% | 0% | 0% | 0% |

Round 1 always writes cold. By round 3 most of the repeated context is coming back
at 0.1× price. Round 4 always drops back to 0% because forced synthesis switches to
a different tool list (adds the submission tool), which changes the cache key and
forces a fresh write — this is the mechanism behind the whole of Part 2 below.

### Investigation (Sonnet) vs synthesis (Opus)

| | Investigation | Synthesis |
|---|---|---|
| Calls | 27 of 37 (73%) | 10 of 37 (27%) |
| Cost | $1.2735 | $2.7666 |
| Share of spend | 32% | **68%** |

Three cheap investigation rounds, one expensive forced-synthesis round that can't
read the investigation cache and has to write its own. Synthesis is a minority of
calls but a majority of spend.

### Resolving the "flipped ratio"

Reading `input_tokens` alone (the raw, per-call field) makes it look like output
overtook input:

| Repo | Raw `input_tokens` | Output | Naive ratio |
|---|---|---|---|
| haxtheweb/praw | 113 | 9,508 | **84 : 1**, output looks bigger |
| open-notebook | 166 | 16,483 | **99 : 1**, output looks bigger |

Folding in the two cache fields — which is where the real context actually went —
restores the expected input-heavy shape:

| Repo | True input (raw + write + read) | Output | True ratio |
|---|---|---|---|
| haxtheweb/praw | 354,946 | 9,508 | **37.3 : 1**, input dominates as expected |
| open-notebook | 616,972 | 16,483 | **37.4 : 1**, input dominates as expected |

The dashboard/report that looked flipped earlier was built from `input_tokens`
alone, without folding in `cache_creation_input_tokens` and
`cache_read_input_tokens`. That was a bug in the read, not in the app.

### Every call — raw data (37 rows)

| id | Repo | Call | Time | Rd | Role | Input | Write | Read | Output | True input | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 9 | haxtheweb | Ask #1 | 05:22:41 | 1 | INVEST | 3 | 2,884 | 0 | 77 | 2,887 | $0.0120 |
| 10 | haxtheweb | Ask #1 | 05:22:47 | 2 | INVEST | 1 | 26,433 | 2,884 | 193 | 29,318 | $0.1029 |
| 11 | haxtheweb | Ask #1 | 05:22:50 | 3 | INVEST | 1 | 2,439 | 29,317 | 105 | 31,757 | $0.0195 |
| 12 | haxtheweb | Ask #1 | 05:23:35 | 4 | SYNTH | 21 | 37,753 | 0 | 1,836 | 37,774 | $0.2820 |
| 13 | haxtheweb | Ask #2 | 05:24:05 | 1 | INVEST | 3 | 948 | 2,884 | 81 | 3,835 | $0.0056 |
| 14 | haxtheweb | Ask #2 | 05:24:08 | 2 | INVEST | 1 | 785 | 3,832 | 97 | 4,618 | $0.0056 |
| 15 | haxtheweb | Ask #2 | 05:24:31 | 3 | INVEST | 1 | 4,443 | 4,617 | 1,224 | 9,061 | $0.0364 |
| 16 | haxtheweb | Ask #2 | 05:24:49 | 4 | SYNTH | 24 | 8,996 | 0 | 1,517 | 9,020 | $0.0943 |
| 17 | haxtheweb | Onboarding · Tour | 05:40:44 | 1 | INVEST | 3 | 2,679 | 0 | 101 | 2,682 | $0.0116 |
| 18 | haxtheweb | Onboarding · Mission | 05:40:45 | 1 | INVEST | 3 | 5,282 | 0 | 119 | 5,285 | $0.0216 |
| 19 | haxtheweb | Onboarding · Tour | 05:40:49 | 2 | INVEST | 1 | 26,391 | 2,679 | 197 | 29,071 | $0.1027 |
| 20 | haxtheweb | Onboarding · Mission | 05:40:51 | 2 | INVEST | 1 | 19,679 | 5,282 | 213 | 24,962 | $0.0786 |
| 21 | haxtheweb | Onboarding · Tour | 05:40:54 | 3 | INVEST | 1 | 13,783 | 29,070 | 190 | 42,854 | $0.0633 |
| 22 | haxtheweb | Onboarding · Mission | 05:40:56 | 3 | INVEST | 1 | 5,991 | 24,961 | 235 | 30,953 | $0.0335 |
| 23 | haxtheweb | Onboarding · Mission | 05:41:19 | 4 | SYNTH | 23 | 37,845 | 0 | 1,017 | 37,868 | $0.2621 |
| 24 | haxtheweb | Onboarding · Tour | 05:41:53 | 4 | SYNTH | 25 | 52,976 | 0 | 2,306 | 53,001 | $0.3889 |
| 25 | open-notebook | Ask #1 | 05:44:59 | 1 | INVEST | 3 | 2,884 | 0 | 77 | 2,887 | $0.0120 |
| 26 | open-notebook | Ask #1 | 05:45:04 | 2 | INVEST | 1 | 27,227 | 2,884 | 254 | 30,112 | $0.1068 |
| 27 | open-notebook | Ask #1 | 05:45:42 | 3 | INVEST | 1 | 10,786 | 30,111 | 1,224 | 40,898 | $0.0678 |
| 28 | open-notebook | Ask #1 | 05:46:15 | 4 | SYNTH | 24 | 40,833 | 0 | 2,358 | 40,857 | $0.3143 |
| 29 | open-notebook | Ask #2 | 05:46:39 | 1 | INVEST | 3 | 1,208 | 2,884 | 152 | 4,095 | $0.0077 |
| 30 | open-notebook | Ask #2 | 05:46:46 | 2 | INVEST | 1 | 17,961 | 4,092 | 147 | 22,054 | $0.0708 |
| 31 | open-notebook | Ask #2 | 05:46:52 | 3 | INVEST | 1 | 12,455 | 22,053 | 185 | 34,509 | $0.0561 |
| 32 | open-notebook | Ask #2 | 05:47:52 | 4 | SYNTH | 21 | 48,355 | 0 | 2,615 | 48,376 | $0.3677 |
| 33 | open-notebook | Onboarding · Tour | 05:48:55 | 1 | INVEST | 3 | 2,682 | 0 | 101 | 2,685 | $0.0116 |
| 34 | open-notebook | Onboarding · Mission | 05:48:56 | 1 | INVEST | 3 | 5,054 | 0 | 139 | 5,057 | $0.0210 |
| 35 | open-notebook | Onboarding · Mission | 05:49:01 | 2 | INVEST | 1 | 14,514 | 5,054 | 164 | 19,569 | $0.0584 |
| 36 | open-notebook | Onboarding · Tour | 05:49:02 | 2 | INVEST | 1 | 37,233 | 2,682 | 253 | 39,916 | $0.1442 |
| 37 | open-notebook | Onboarding · Mission | 05:49:06 | 3 | INVEST | 1 | 8,003 | 19,568 | 180 | 27,572 | $0.0386 |
| 38 | open-notebook | Onboarding · Tour | 05:49:09 | 3 | INVEST | 1 | 15,471 | 39,915 | 285 | 55,387 | $0.0743 |
| 39 | open-notebook | Onboarding · Mission | 05:49:27 | 4 | SYNTH | 23 | 31,988 | 0 | 840 | 32,011 | $0.2210 |
| 40 | open-notebook | Onboarding · Tour | 05:50:10 | 4 | SYNTH | 25 | 64,295 | 0 | 2,820 | 64,320 | $0.4725 |
| 41 | open-notebook | Onboarding · Tour | 05:50:37 | 5 | SYNTH | 25 | 3,195 | 64,295 | 3,056 | 67,515 | $0.1286 |
| 42 | open-notebook | Issue investigation | 05:51:11 | 1 | INVEST | 3 | 3,612 | 0 | 135 | 3,615 | $0.0156 |
| 43 | open-notebook | Issue investigation | 05:51:16 | 2 | INVEST | 1 | 12,802 | 3,612 | 121 | 16,415 | $0.0509 |
| 44 | open-notebook | Issue investigation | 05:51:21 | 3 | INVEST | 1 | 9,869 | 16,414 | 175 | 26,284 | $0.0446 |
| 45 | open-notebook | Issue investigation | 05:51:49 | 4 | SYNTH | 23 | 32,815 | 0 | 1,202 | 32,838 | $0.2353 |

Source: `%LOCALAPPDATA%\Waypoint\waypoint.sqlite3` → `model_usage` table, joined to
`analysis_sessions` for repository names; latency from
`%LOCALAPPDATA%\Waypoint\traces\waypoint.jsonl`.

---

## Part 2 — Cost reduction projection: what it would take to get cheaper

Nothing in this part is a code change. Every number is either **recomputed
exactly** from the logged tokens above using the app's real pricing formula, or
explicitly labeled as an **illustrative estimate** where the size of the fix isn't
measured yet.

### Headline numbers

| | Value |
|---|---|
| Actual spend tonight | $4.0401 |
| If the cache-key fix below were applied | **$1.9571 (−52%)** |
| + illustrative volume-reduction levers (15–30%, unquantified) | **$1.37 – $1.66 (−66% to −59%)** |
| Synthesis share of spend | 68% (10 of 37 calls, 27%) |

### The one big fix: stop forced-synthesis from re-writing the cache it should be reading

Investigation runs 1–3 rounds on Sonnet with a reduced tool list, then a forced
synthesis round switches to Opus with an **expanded** tool list (it adds the
submission tool). Anthropic/Bedrock prompt caching is prefix-based — changing the
tool list changes the cache key, so synthesis can't read anything investigation
just paid to cache. It re-writes the same context from scratch, at the 1.25× write
price instead of the 0.1× read price it would get if the tool list hadn't changed
across the handoff.

If the cache key survived the role switch (i.e. synthesis is handed the same tool
list as investigation, submission tool included from round 1 but simply unused
until the model is ready) — same token volume, priced as a read instead of a
write: a **92% price cut** on that specific block (0.1 ÷ 1.25).

Applied to the exact `cache_creation_input_tokens` logged on all 10 synthesis
rounds tonight:

| Repo | Current (billed as write, 1.25×) | If billed as read, 0.1× | Saved |
|---|---|---|---|
| haxtheweb/praw | $0.8598 | $0.0688 | **$0.7910** |
| open-notebook | $1.3843 | $0.1107 | **$1.2735** |
| **Combined** | **$2.2441** | **$0.1796** | **$2.0645** |

A second, much smaller instance of the same class of fix: `onboarding_mission`'s
round 1 starts one second after `onboarding_tour`'s round 1 on the same repo
(haxtheweb: 05:40:44 → 05:40:45; open-notebook: 05:48:55 → 05:48:56). Both write a
repository-overview-shaped prefix from cold. If that shared prefix were ordered
identically across both workflows, the second one to start could read instead of
write it — worth $0.0092 (haxtheweb) and $0.0093 (open-notebook), folded into the
totals below. Small in dollar terms, but the same underlying mechanism as the fix
above: **the cache key, not the token volume, is the problem.**

### Combined projection, per repo

| Repo | Actual | + cache-key fix | Δ | + illustrative volume cut (15–30%) | Δ range |
|---|---|---|---|---|---|
| haxtheweb/praw | $1.52 | $0.72 | **−53%** | $0.50 – $0.61 | −67% to −60% |
| open-notebook | $2.52 | $1.24 | **−51%** | $0.87 – $1.05 | −66% to −58% |
| **Combined** | **$4.04** | **$1.96** | **−52%** | **$1.37 – $1.66** | **−66% to −59%** |

The illustrative volume cut is explicitly a range, not a measurement: 15–30%
applied to whatever true-input volume remains after the cache-key fix. Nothing in
tonight's data says the real number is 15%, 30%, or something else — it needs the
levers below actually built and re-measured against a run like this one.

### Further levers — directionally real, not sized yet

These attack token **volume** (fewer tokens ever enter a call) rather than token
**price** (the fix above) — they're why the projection shows a range instead of a
third exact number.

| Lever | Mechanism | What it would do here |
|---|---|---|
| Per-request input-token budget | volume | Hard-cap total tokens a single Ask/Onboarding/Issue call may accumulate across all rounds, forcing earlier synthesis instead of a 4th–5th expansion round. |
| Per-round context budget | volume | Cap how much each individual round is allowed to add, so one large `search_repository` result can't single-handedly double a round's cache write. |
| Max tool-result bytes/tokens | volume | Truncate or paginate oversized tool outputs before they enter the conversation — currently a large `get_repository_overview` or search result goes in unabridged. |
| Evidence dedup by chunk identity | volume | If the same file/line range gets re-fetched across rounds (common once a search and a direct read both surface it), store it once and reference it instead of re-sending the text. |
| Context compaction between rounds | volume | Replace a completed round's raw tool call + result with a short summary before the next round is sent, shrinking what gets cached at every subsequent write. |
| Summaries replacing completed tool results | volume | Once a tool result has been read and acted on, its role in later context is just "this was already checked" — a one-line summary usually suffices. |
| Relevant tool subset per phase | repricing | Directly fixes the synthesis cache-miss above: if synthesis is handed the same tool list as investigation (submission tool included from round 1, just unused), the cache key never changes. |
| Cache-aware accounting | repricing | Already shipped — surfacing `cache_read` vs `cache_creation` is what made every other row in this table possible to reason about. |
| Early synthesis when evidence suffices | volume | A stop rule that ends investigation the moment marginal new evidence drops, rather than always running to the round cap — fewer rounds means less accumulated volume before the expensive synthesis write. |
| Max files/passages per run | volume | Cap the number of distinct files a single analysis may pull into evidence, independent of how many rounds it takes to get there. |
| Stop rule on marginal retrieval gain | volume | Same mechanism as early synthesis, generalized: detect when a new tool call is mostly re-covering ground already in context and skip it. |
| Separate memory / evidence budgets | volume | Split the context budget so long-lived repo memory and this-turn evidence don't compete for the same cap, avoiding one crowding out the other mid-conversation. |

### How to read this

- **"+ cache-key fix"** is not a guess: it takes the exact
  `cache_creation_input_tokens` logged on every synthesis round tonight and
  reprices that same token count at the cache-*read* rate instead of the
  cache-*write* rate, using the same `estimate_model_cost_usd()` formula the app
  already bills with. Same tokens, cheaper price tier — no volume assumption.
- The shared-prefix adjustment (mission reading tour's round-1 prefix) is real and
  measured the same way, but small — under $0.02 combined.
- **"+ illustrative volume cut"** is explicitly a range: 15–30% applied to
  whatever true-input volume remains after the cache-key fix. It needs the levers
  above actually built and re-measured to become a real number.
- No code was changed to produce either part of this document. Source: the same 37
  `model_usage` rows as Part 1, plus `_MODEL_PRICING_USD_PER_MTOK` /
  `_CACHE_WRITE_PRICE_MULTIPLIER` / `_CACHE_READ_PRICE_MULTIPLIER` read directly
  from `backend/app/agent/provider.py`.
