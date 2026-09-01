> **Archived, superseded.** This "convenience" snapshot's content is already fully
> contained in [`../benchmarks/history.md`](../benchmarks/history.md)'s "V3 run
> history" section, which is the maintained cumulative record. Kept here only for
> anyone who has an old link to this exact file.

# Waypoint V3 retrieval and hybrid-memory report

This is the V3 convenience report. The cumulative, append-only history remains
`WAYPOINT_BENCHMARKS_AND_TESTS.md`; V1 and V2 results were not erased.

## Outcome

V3 implements multi-query retrieval, intent-aware evidence allocation, graph-aware
diversification, confidence metadata, bounded vector caching, and hybrid conversation
memory. The unchanged eight-question Waypoint benchmark improved from V2's **67.48%**
strict quality score to **73.87%**. The new 50-question, three-repository diagnostic
scored **72.57%**.

The 50-case result is **not release-gate eligible**. Its seed labels are marked
`pending_human`, and retrieval behavior was inspected during V3 development. It is useful
engineering evidence, not an independently adjudicated blind test.

## Production retrieval changes

1. Multi-hop questions are split into up to six informative clauses. Full-query and
   clause-level FTS candidates are fused, while one bounded vector search prevents query
   decomposition from multiplying the expensive vector scan.
2. Query intent identifies implementation, interface, configuration, storage, tests,
   frontend, and documentation evidence. File-level reranking rewards a requested layer
   that has not yet been represented.
3. Deterministic MMR-style selection rewards a new source layer, package neighborhood,
   and graph relationship while penalizing repeated package concentration.
4. Documentation intent now recognizes skills, rules, knowledge, worldview, tutorials,
   guides, and workflows. This corrected a real PRAW failure mode in which executable
   helper scripts outranked the requested skill documentation.
5. Generated V3 manifests are suppressed for ordinary product/code questions, like the
   existing benchmark-result and coverage-artifact suppression.
6. Every returned result includes query-term coverage, requested-layer coverage, a
   retrieval-confidence score, and `retrieval_requires_followup` below 0.62.
7. The Ask system prompt instructs the agent to perform a narrower retrieval when the
   confidence signal is low and to state that evidence is insufficient if the second
   retrieval remains weak.
8. Deserialized repository vectors are cached by immutable revision. The cache is bounded
   to three revisions and uses compact unsigned-short/float arrays instead of Python
   dictionaries to limit memory growth.

V3 does **not** add a learned cross-encoder reranker. Ranking remains deterministic and
offline. FTS5 supplies BM25, but several fusion/MMR weights remain hand-authored rather
than statistically calibrated. These limitations are intentionally visible.

## Hybrid conversation memory

Each completed Ask turn now produces three durable records in the same SQLite database:
the user message, assistant message, and one vector-memory row containing the turn's
question, answer, revision fingerprint, timestamp, and local vector.

At the next turn, prompt memory is assembled from:

- the most recent `WAYPOINT_CHAT_HISTORY_TURNS` turns in chronological order; and
- up to `WAYPOINT_CHAT_SEMANTIC_MEMORY_ITEMS` older turns selected for the current query.

Older-turn ranking is:

```text
0.85 * vector cosine similarity
+ 0.10 * logarithmic recency
+ 0.05 * exact repository-revision match
```

The defaults are 12 recent turns, 6 semantic turns, 200 retained turns, an 8,000-character
semantic-memory budget, and 0.12 minimum similarity. Selection is strictly scoped to the
same conversation, which is already scoped to one analysis and channel, preventing memory
from another repository or workspace from leaking into the prompt. Recent turn IDs are
excluded from semantic selection, and deleting old messages cascades to their vector rows.

The default `local-subword-hash-v1` vectorizer is a 768-dimension, deterministic sparse
subword embedding. It is private, offline, free, and robust to identifier morphology, but
it is **not a neural semantic embedding model**. It will not understand distant conceptual
paraphrases as well as a trained embedding model. The hybrid recent-window fallback keeps
the system useful when the sparse similarity signal is weak.

Memory diagnostics are logged for each agent start: recent turns, candidate older turns,
selected semantic turns, selected character count, similarity threshold, and scores.

## Fixed eight-case V2-to-V3 comparison

The questions, labels, forbidden paths, top-ten limit, and pass rules in
`benchmarks/retrieval_cases.json` were unchanged.

| Metric | V2 | V3 | Change |
|---|---:|---:|---:|
| Hit@1 | 75.00% | 75.00% | unchanged |
| Precision@5 | 47.50% | 57.50% | +10.00 points |
| Path recall@10 | 73.07% | 74.11% | +1.04 points |
| Required-group recall@10 | 87.50% | 92.71% | +5.21 points |
| MAP@10 | 51.93% | 56.22% | +4.29 points |
| MRR | 84.38% | 85.42% | +1.04 points |
| nDCG@10 | 68.61% | 71.35% | +2.74 points |
| Forbidden-path case rate | 25.00% | 12.50% | -12.50 points (better) |
| Strict case pass rate | 37.50% | 50.00% | +12.50 points |
| **Strict quality score** | **67.48%** | **73.87%** | **+6.39 points** |
| Mean query latency | 2,689.96 ms | 1,203.18 ms | 1,486.78 ms faster |
| Query p95 | 2,845.79 ms | 3,031.22 ms | 185.43 ms slower |
| Peak process memory | 193.66 MiB | 197.57 MiB | +3.91 MiB |

The mean improved because vectors are deserialized once. The single cold cache-fill query
still dominates p95 in this eight-sample, one-repeat run, so the p95 regression is retained
rather than hidden.

## Fifty-case diagnostic

Manifest: `benchmarks/retrieval_cases_v3.json`  
Raw result: `benchmarks/results/retrieval-v3.json`

- 50 unique questions: Waypoint 20, Flask 15, PRAW 15.
- 30 development and 20 nominal held-out questions.
- Dedicated Python, TypeScript, JavaScript, Java, HTML, shell, configuration, and
  documentation slices.
- Every case has required path groups, acceptable alternatives, forbidden distractors,
  and an adjudication state.
- The runner refuses pending labels unless `--allow-unadjudicated` is explicitly passed.

| Metric | Result |
|---|---:|
| Hit@1 / Hit@3 / Hit@5 / Hit@10 | 84.00% / 98.00% / 100.00% / 100.00% |
| Precision@5 / Precision@10 | 45.60% / 29.00% |
| Path recall@5 / Path recall@10 | 53.61% / 67.25% |
| Required-group recall@5 / @10 | 68.87% / 85.33% |
| MAP@10 | 51.63% |
| MRR | 90.07% |
| nDCG@10 | 66.41% |
| Forbidden-path case rate | 10.00% |
| Strict case pass rate | 52.00% |
| **Strict quality score** | **72.57%** |

The strict quality score is the equal mean of strict case pass rate, required-group
recall@10, MAP@10, MRR, nDCG@10, and one minus forbidden-hit rate. The diagnostic clears
70%, but low precision and only 52% strict case passes show that it is not equivalent to
“all retrieval requirements pass.”

| Slice | Cases | Strict quality | Strict case pass |
|---|---:|---:|---:|
| Waypoint | 20 | 71.23% | 45.00% |
| Flask | 15 | 75.78% | 60.00% |
| PRAW | 15 | 71.16% | 53.33% |
| Development | 30 | 74.21% | 60.00% |
| Nominal held-out | 20 | 70.12% | 40.00% |

The Java and configuration slices each contain only one case and must not be generalized
into broad language claims. The nominal held-out split is not a valid blind release set
after V3 development inspected aggregate results; a new independently adjudicated set is
required for a real release gate.

## Performance from the 50-case run

| Repository | Index build | Restore | Mean query | p95 query | Observed peak process memory |
|---|---:|---:|---:|---:|---:|
| Waypoint | 39.81 s | 503.75 ms | 852.27 ms | 1,093.10 ms | 196.39 MiB |
| Flask | 5.88 s | 77.98 ms | 199.93 ms | 348.43 ms | 258.94 MiB |
| PRAW | 15.73 s | 133.96 ms | 340.47 ms | 634.67 ms | 258.94 MiB |

Peak memory is process-wide and cumulative on Windows, not repository-exclusive. It must
not be summed across rows or interpreted as each repository's isolated allocation.

## Tests and coverage

| Check | Result |
|---|---:|
| Focused memory/retrieval/manifest tests | 27/27 passed |
| Complete backend suite | 90/90 passed |
| Combined branch-aware backend coverage | 74.80% — PASS |
| Weighted core-feature coverage | 83.40% — PASS |
| Revision-aware indexing/retrieval core | 83.93% — PASS |
| Grounded answer/validation/memory core | 81.09% — PASS |

New tests cover semantic recovery outside the recent window, no duplication of recent
turns, confidence metadata, query decomposition, intent layers, vector-cache reuse, the
exact 50-case manifest, repository/split/language aggregation, and rejection of
unadjudicated labels by default.

## Remaining work before a release claim

1. Have a person independently review every V3 required/acceptable/forbidden path.
2. Freeze a new blind held-out set because this run informed V3 development.
3. Increase independent Java, HTML/configuration, and documentation cases; one case is not
   a statistically meaningful language evaluation.
4. Improve precision@5 and required-group completeness rather than relying on Hit@10.
5. Evaluate a learned reranker against the deterministic fallback through an ablation.
6. Run the live Claude/OpenRouter and OpenRouter/OpenRouter answer suites separately;
   this retrieval run does not spend model quota and does not measure answer correctness.

## Reproduction

```powershell
# Unchanged eight-case comparison
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py `
  --repository . `
  --cases benchmarks\retrieval_cases.json `
  --output benchmarks\results\retrieval-v3-eight.json `
  --repeats 1

# Fifty-case development diagnostic. Pending labels require explicit opt-in.
.\.venv\Scripts\python.exe benchmarks\run_retrieval_v3.py `
  --repository waypoint=. `
  --repository flask=.waypoint-clones/pallets--flask--214023150b `
  --repository praw=.waypoint-clones/haxtheweb--praw--69db9269ab `
  --allow-unadjudicated `
  --repeats 1 `
  --output benchmarks\results\retrieval-v3.json

# Deterministic regression and readable coverage
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m verification.scripts.core_coverage_report
.\.venv\Scripts\python.exe -m verification.scripts.coverage_report
```
