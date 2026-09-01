> **Archived, superseded.** This "convenience" snapshot's content is already fully
> contained in [`../benchmarks/history.md`](../benchmarks/history.md)'s "Run history:
> V2" section, which is the maintained cumulative record. Kept here only for anyone
> who has an old link to this exact file.

# Waypoint benchmarks and tests V2

Date: 2026-07-26  
Status: implemented and measured  
Retrieval benchmark repository: this Waypoint checkout  
Live answer benchmark repository: pinned `pallets/flask` checkout

## V2 outcome

V2 fixes two omissions in the previous evaluation:

1. model tasks now state which **citation paths and line ranges** are expected;
2. answer length, citation count, citation span, and output-token limits now affect task pass/fail.

V2 also changes the production retrieval ranker. Against the same eight strict queries, the strict retrieval score improved from **42.86% to 67.48%**. It remains below the 70% release threshold and is therefore still a failure, not a claimed success.

| Metric | Baseline | V2 | Change |
|---|---:|---:|---:|
| Hit@1 | 25.00% | 75.00% | +50.00 points |
| Hit@3 | 100.00% | 87.50% | -12.50 points |
| Hit@5 / Hit@10 | 100% / 100% | 100% / 100% | unchanged |
| Precision@5 | 35.00% | 47.50% | +12.50 points |
| Precision@10 | 26.25% | 37.50% | +11.25 points |
| Path recall@5 | 35.03% | 47.53% | +12.50 points |
| Path recall@10 | 51.70% | 73.07% | +21.37 points |
| Required-group recall@5 | 53.13% | 61.46% | +8.33 points |
| Required-group recall@10 | 67.71% | 87.50% | +19.79 points |
| MAP@10 | 25.26% | 51.93% | +26.67 points |
| MRR | 58.33% | 84.38% | +26.05 points |
| nDCG@10 | 43.33% | 68.61% | +25.28 points |
| Forbidden-path case rate | 37.50% | 25.00% | -12.50 points (better) |
| Strict case pass | 0/8 | 3/8 | +37.50 points |
| **Strict quality score** | **42.86%** | **67.48%** | **+24.62 points** |
| Mean query latency | 3,277 ms | 2,690 ms | -587 ms |

### Exactly what changed and why the result improved

The questions, expected path groups, forbidden paths, top-10 limit, and strict thresholds were unchanged. The gain came from changes to `RepositoryRetrievalIndex.search`, not easier labels.

| Retrieval change | Baseline problem | V2 behavior | Resulting improvement |
|---|---|---|---|
| Stopword removal | Common words such as `how`, `where`, `and`, and `through` gave large unrelated files lexical score. | Removes question noise from ranking while retaining raw terms for intent detection. | Reduced false matches and improved precision, MAP, and nDCG. |
| Morphology expansion | `conversations`, `stored`, and `restored` did not consistently match `conversation`, `store`, and `restore`. | Adds conservative singular and `-ed`/`-ing` variants. | Relevant implementation files entered the candidate set earlier. |
| Per-term frequency cap | Long files won by repeating one matching word. | Limits each informative term to three raw occurrences per chunk. | Reduced file-length bias and improved ranking order. |
| Distinct-concept coverage | Repeating one term could beat a file covering several independent parts of a multi-hop question. | Rewards the percentage of distinct query concepts found. | Required-group recall@10 rose from 67.71% to 87.50%. |
| Stronger path scoring | Meaningful filenames were underweighted. | Gives a stronger bounded bonus for query terms in repository paths. | Hit@1 rose from 25% to 75%; MRR rose from 58.33% to 84.38%. |
| Evaluation-artifact suppression | Benchmark manifests and generated reports repeated the benchmark wording and retrieved themselves. | Filters generated/evaluation artifacts unless the query asks about evaluation. | Forbidden-path case rate fell from 37.5% to 25%. |
| Implementation/documentation intent | Prose reports could outrank source code for “how is this implemented?” questions. | Excludes documentation for implementation intent unless the user asks for a README, overview, guide, tutorial, or documentation. | Path recall@10 rose from 51.70% to 73.07%; nDCG@10 rose from 43.33% to 68.61%. |
| Existing distinct-file diversity | Repeated chunks from one file could crowd out independent evidence. | Preserves the existing first-result-per-file diversification after the new scoring. | Helped the improved signals expose multiple subsystems required by multi-hop questions. |

The largest gain came from stronger path relevance and distinct-concept coverage combined with removal of common-word and file-length bias. Artifact/documentation filtering primarily reduced noise; morphology primarily improved natural-language-to-identifier recall.

Expected citation ranges and output limits made the separate model-answer benchmark stricter; they did not cause this retrieval gain. No question-to-file lookup map was added, expected paths were not injected into queries, and thresholds were not lowered.

V2 still fails: precision@5 is 47.5%, required-group recall@10 is 87.5%, forbidden-path rate is 25%, strict pass is 3/8, and the strict aggregate is 67.48% against a 70% gate.

Hit@3 fell because three queries now put a relevant file at ranks 2–3 rather than all eight doing so, while Hit@1, multi-file completeness, ranking quality, and noise all improved substantially. The strict aggregate remains the release authority.

## Production retrieval changes

Implemented in `backend/app/agent/retrieval.py`:

- removes high-frequency question words such as `how`, `where`, `and`, and `through` from lexical ranking;
- retains raw terms separately so request intent filters still work;
- adds conservative singular and `-ed`/`-ing` morphology variants;
- caps each term to three occurrences per chunk so giant files cannot win by repetition;
- rewards coverage of distinct query concepts rather than raw frequency alone;
- increases matches against file paths, which are unusually meaningful in code retrieval;
- excludes evaluation manifests and generated results unless the question is about evaluation;
- for implementation queries, removes prose documentation unless the user explicitly requests an overview, guide, README, or documentation;
- keeps one-result-per-file diversity so repeated chunks do not crowd out other evidence files;
- preserves graph-neighborhood expansion and hybrid FTS/local-vector reciprocal-rank fusion.

These are general retrieval rules. No benchmark question-to-file lookup table was added.

## Expected citations

The source of truth is `verification/model-quality-cases.json`. It is now a versioned object with 12 cases and **27 expected citation groups**.

An expected citation is not merely a filename. Each alternative contains:

```json
{
  "path": "src/flask/app.py",
  "start_line": 966,
  "end_line": 1060
}
```

A submitted citation satisfies the expected item only when:

- the path matches; and
- the submitted line interval overlaps the expected interval.

Alternatives may appear in one group when the same fact is legitimately evidenced in two locations. A task normally needs at least 67% of expected citation groups. Required evidence groups still separately test whether the correct files were cited at all.

The 12 tasks cover:

1. repository overview;
2. sessions;
3. request lifecycle;
4. blueprints;
5. application/request contexts;
6. exception handling;
7. templating;
8. CLI discovery;
9. JSON providers;
10. signals;
11. testing client;
12. configuration loading.

The expected spans were checked against the pinned Flask source used by the benchmark. A different Flask revision requires a new gold-set revision because line numbers can move.

## Output enforcement

The following V2 defaults are visible at the top of `verification/model-quality-cases.json`:

| Constraint | Enforced value |
|---|---:|
| Minimum answer length | 200 characters |
| Maximum answer length | 8,000 characters |
| Minimum citations | 2 |
| Maximum citations | 8 |
| Maximum lines in one citation | 160 |
| Maximum reported output tokens | 6,000 |

`verification/scripts/model_quality_benchmark.py` applies every check and records each Boolean result. A task fails if any output check fails, even when its concepts and files are correct.

Application runtime validation remains an additional layer:

- Pydantic limits an Ask answer to 30,000 characters;
- basis text is limited to 2,000 characters;
- citations are limited to 30;
- follow-up questions are limited to five;
- citation paths and ranges must be inside evidence the agent actually inspected;
- the provider request defaults to 6,000 maximum output tokens.

The 8,000-character and 8-citation benchmark limits are deliberately stricter than the runtime safety ceiling. Runtime limits protect the server; benchmark limits measure answer usability.

## V2 answer pass contract

A live model task passes only if all are true:

1. provider returned an answer;
2. structured output passed its schema;
3. answer is non-empty;
4. required-concept recall reached the per-case threshold;
5. required-evidence-group recall reached the per-case threshold;
6. citation precision reached the per-case threshold;
7. expected citation path/range group recall reached the per-case threshold;
8. all output limits passed;
9. application validation status is `passed`.

This prevents a model from passing by mentioning keywords, citing the correct file at an irrelevant location, citing an entire 1,500-line module, or returning an excessively long response.

## Tests added

`backend/tests/test_benchmarks.py` now verifies:

- strict required-group, precision, and forbidden-path scoring;
- repeated chunks are deduplicated to unique files;
- stopwords are removed while morphology variants are retained;
- expected citation path and line overlap are required;
- over-wide citation spans fail;
- output character, citation-count, citation-span, and token constraints participate in pass/fail.

The targeted retrieval/agent run executed **22 tests and all passed**. The complete backend regression then executed **85 tests and all passed**, with **74.3% combined branch-aware coverage**. The core-feature aggregate is **83.07%**, with every declared core feature above 70%.

## Why V2 still fails retrieval

The remaining failures are useful diagnostics:

- **Precision@5 is 47.5%, below 60%.** The top five often contain two correct files and three plausible but nonessential files.
- **Required-group recall@10 is 87.5%, below the strict 100% contract.** Some queries still miss one independent subsystem such as configuration, transport, or indexing.
- **Strict pass is only 3/8.** Five cases fail at least one of early rank, precision, complete evidence groups, or forbidden paths.
- **Forbidden-path rate is 25%.** Two cases still surface a specifically labeled distractor.
- **nDCG@10 is 68.61%.** Relevant evidence exists but is not consistently ordered close enough to the top.

Hit@10 is 100%, but that only means every query found at least one useful file. It does not prove the evidence bundle is complete or clean.

## Recommended V3 retrieval work

The next improvements should be evaluated one at a time through ablation runs:

1. **Query decomposition.** Split multi-hop questions into independent subqueries, retrieve per hop, and fuse results. This directly targets missing evidence groups.
2. **Learned or calibrated reranker.** Rerank the top 30 hybrid candidates using query/file/symbol features or a small cross-encoder. Preserve a deterministic fallback.
3. **Intent-aware source layers.** Classify whether a query needs implementation, configuration, tests, documentation, frontend, or history, then allocate slots per layer instead of using one global ranking.
4. **Graph-aware diversity.** Select candidates that cover different modules and relationship neighborhoods rather than only distinct paths.
5. **IDF/BM25 calibration.** Replace remaining fixed bonuses with corpus-frequency-aware features and tune them on a training split, never the held-out release set.
6. **Larger gold set.** Expand strict deterministic retrieval from eight to at least 50 queries across Waypoint, Flask, and PRAW, with separate development and held-out sets.
7. **Human adjudication.** Review disputed “acceptable” and “forbidden” paths without changing labels after seeing model identity or rank.
8. **Per-language slices.** Report Python, JS/TS, Java, markup/configuration, and documentation retrieval independently.
9. **Confidence and abstention.** If required evidence coverage is weak, have the agent retrieve again or state that evidence is incomplete rather than synthesizing confidently.

Do not lower the 60% precision or 100% required-group thresholds simply to turn the current result green.

## Where prompts are stored

Waypoint does not yet have one centralized prompt registry. Current prompt locations are:

| Prompt type | Location |
|---|---|
| Ask/repository-agent system prompt | `backend/app/agent/service.py`, `SYSTEM_PROMPT` |
| Ask tool descriptions and JSON schemas | `backend/app/agent/service.py`, `TOOLS` |
| Issue investigation prompt | `backend/app/agent/issues.py`, `FINDING_PROMPT` |
| Onboarding route prompt | `backend/app/agent/onboarding.py`, `TOUR_SYSTEM_PROMPT` |
| Contribution mission prompt | `backend/app/agent/onboarding.py`, `MISSION_SYSTEM_PROMPT` |
| Free-text challenge grader | inline in `backend/app/agent/onboarding.py` near the assessment call |
| Claude Agent SDK action-selection wrapper | `backend/app/agent/provider.py` |
| Frozen model-comparison submit contract | `backend/app/agent/comparison.py`, `SUBMIT_COMPARISON_TOOL` and comparison request construction |
| MCP reusable explanation prompt | `backend/app/mcp_server.py`, `explain_repository` |
| Deterministic onboarding challenge wording | `backend/app/onboarding/service.py` |
| Ask starter questions | `frontend/src/components/AskWorkspace.tsx` |
| Human AI-interaction log | root `prompts.md` |
| Retrieval evaluation questions | `benchmarks/retrieval_cases.json` |
| Live model questions, expected citations, output contract | `verification/model-quality-cases.json` |
| Full 50-case capability suite | `verification/agentic-rag-evaluation-cases.json` |

`prompts.md` is an audit log, not a runtime prompt source. Editing it does not change agent behavior.

## Reproduction

```powershell
# Strict V2 retrieval
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py `
  --repository . `
  --cases benchmarks\retrieval_cases.json `
  --output benchmarks\results\retrieval-v2.json `
  --repeats 3

# Deterministic benchmark and agent tests
.\.venv\Scripts\python.exe -m pytest `
  backend\tests\test_benchmarks.py `
  backend\tests\test_agent.py `
  -q --no-cov

# Live citation/output evaluation; spends configured provider quota
.\.venv\Scripts\python.exe verification\scripts\model_quality_benchmark.py `
  --analysis-id <PINNED_FLASK_ANALYSIS_ID> `
  --cases verification\model-quality-cases.json `
  --confirm-live-model-usage `
  --label strict-v2
```

The saved `retrieval-v2.json` used one repetition to compare ranking changes promptly. The release performance run should use at least three repetitions and report confidence intervals. The live 12-task model suite was defined and deterministically validated, but was not rerun here because it consumes external model quota.
