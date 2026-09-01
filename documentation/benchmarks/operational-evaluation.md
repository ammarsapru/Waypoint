> **Numbers below are a 2026-07-26 snapshot, not current.** Test count and coverage
> figures here (79 backend tests, 74.65% coverage, 83.00% core aggregate) are
> superseded by [`test-suite-and-coverage.md`](test-suite-and-coverage.md)'s
> 2026-07-28 numbers (111 tests, 74.94%, 82.91%) and the even more recent count noted
> in [`../architecture/system-architecture.md`](../architecture/system-architecture.md)
> §12.3. The retrieval/model-quality/performance/frontend category scores here reflect
> the pre-V2 retrieval algorithm; see [`history.md`](history.md) for how those scores
> evolved through V2-V4. This document's methodology, remediation plan, and load/cache/
> Web Vitals detail remain the most complete write-up of that kind and are still
> valuable for that reason.

# Waypoint Operational Evaluation Report

Generated July 26, 2026. This report consolidates parser correctness, retrieval quality,
model-answer quality, frontend behavior, performance, caching, memory, Web Vitals, and
traditional test coverage.

## Executive result

Waypoint's operational score is **71.87%**, but the release result is **FAIL**. The policy
requires at least 70% overall **and in every category**. This prevents a perfect parser or
strong Claude result from hiding weak retrieval, frontend testing, or a failing second
model.

| Operational category | Score | 70% gate | Result |
|---|---:|---:|---|
| Parsing accuracy | 100.00% | 70% | PASS |
| Deterministic retrieval quality | 58.76% | 70% | FAIL |
| Two-model answer quality | 83.16% | 70% | FAIL: OpenRouter endpoint success is 50% |
| Service and browser performance | 66.67% | 70% | FAIL |
| Frontend quality | 50.78% | 70% | FAIL |

This operational score is a declared Waypoint product rubric. It is separate from
coverage.py/V8 code coverage, which measures whether instrumented code executed.

## 1. Parsing accuracy

### Method

`benchmarks/run_parsing_accuracy.py` creates nine controlled files covering Python,
JavaScript, TypeScript, TSX, Java, HTML, and CSS. Sixteen expected semantic nodes and nine
expected relationships are compared against analyzer output using precision, recall, and
F1. Relationship classes include imports, instantiations, and inferred calls.

### Results

| Metric | Result |
|---|---:|
| Files discovered and parsed | 9/9 |
| Parse failures | 0 |
| Parse success | 100% |
| Node precision / recall / F1 | 100% / 100% / 100% |
| Edge precision / recall / F1 | 100% / 100% / 100% |
| Duration | 128.986 ms |
| Peak Python allocation | 0.349 MiB |

This proves correctness only for the audited fixture. It does not claim perfect precision
or recall for arbitrary real-world syntax, frameworks, generated code, or ambiguous
dynamic calls.

## 2. Deterministic retrieval quality

### Method

Eight gold questions define expected source files. The benchmark scores the retrieval
index before a model sees the results:

- Hit@5 and Hit@10: whether an expected file appears within the cutoff.
- MRR: reciprocal rank of the first relevant result.
- nDCG@10: relevance ordering across the first ten results.
- Cold/warm query latency and p50/p95/p99 distributions.
- Index build, persisted-index restoration, and process peak memory.

### Results

| Metric | Result |
|---|---:|
| Hit@5 | 62.50% |
| Hit@10 | 87.50% |
| MRR | 0.3774 |
| nDCG@10 | 0.4730 |
| Composite | 58.76% — FAIL |
| Index build | 47,943.117 ms |
| Index restore | 641.937 ms |
| Mean query | 3,066.316 ms |
| Query p50 / p95 / p99 | 3,023.568 / 3,260.126 / 3,296.904 ms |
| Process peak memory | 191.621 MiB |

Notable misses include conversation memory appearing at rank 8, the analysis API route at
rank 9, and repository architecture inference failing to retrieve
`backend/app/agent/semantic.py` in the first ten results. Documentation and broad frontend
files frequently outrank precise implementation files.

## 3. Two-model answer and evidence-selection evaluation

### Fair-comparison design

Each Flask gold question is retrieved exactly once. Waypoint serializes and hashes that
evidence, then gives the identical frozen evidence and question to Claude Code Sonnet and
OpenRouter concurrently. Models cannot receive different repository material.

The deterministic index score above measures candidate retrieval. Expected cited-file
recall and precision measure model-specific evidence selection. Keeping these separate
identifies whether a miss came from the index or the model.

### Aggregate results

| Metric | Claude Code Sonnet | OpenRouter free router |
|---|---:|---:|
| Gold tasks | 4 | 4 |
| Valid responses | 4 | 2 |
| Task success | 100% | 50% |
| Mean required-concept recall | 96.43% | 90.00% on valid responses |
| Mean expected-file recall | 87.50% | 75.00% on valid responses |
| Latency p50 | 27,540.568 ms | 34,434.041 ms |
| Latency p95 | 47,076.934 ms | 48,910.665 ms |
| True TTFT p50 | Unavailable through SDK | 23,944.391 ms, two samples |
| Effective input tokens | 132,208 | 10,430 |
| Output tokens | 11,266 | 3,234 |
| Structured output calls | 4 | 2 |
| Validated citations | 23 | 4 |

OpenRouter failures were not discarded:

1. Repository overview returned nine suggested questions where the schema permits five.
2. Request-lifecycle citations contained paths/ranges outside the frozen evidence.

The category's arithmetic score is 83.16%, but it fails because the weakest endpoint's
task-success rate is 50%. Claude is not allowed to compensate for OpenRouter reliability.

### Accuracy limitation

Required-concept matching is deterministic and reviewable, but it is not a complete
semantic correctness judge. Production evaluation should add human labels for factual
correctness, unsupported claims, completeness, and citation entailment, ideally with
inter-reviewer agreement. A judge model can assist but should first be calibrated against
those labels.

## 4. TTFT and model latency

OpenRouter uses SSE; its TTFT stops at the first meaningful text or tool-call delta.
Claude Agent SDK does not expose raw token deltas. Waypoint measures its first observable
`AssistantMessage` instead and stores it independently as
`first_observable_event_ms`.

Claude's observed assistant-event values were 19,946.597, 15,616.342, 14,142.486, and
6,293.164 ms: p50 **14,879.414 ms**, p95 **19,297.059 ms**. These include CLI/SDK startup,
transport, queueing, inference, and SDK buffering. See `SDK_TTFT_MEASUREMENT_REPORT.md`
for the exact definition and why it must not be labeled token TTFT.

## 5. Backend performance, load, cache, and memory

### Load results

The HTTP benchmark exercised health, analysis, summary, index, tree, and conversation
restoration endpoints with 30 requests each at concurrency 1, 4, and 8.

| Concurrency | Success | Throughput | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|---:|
| 1 | 30/30 | 1.802 req/s | 546.160 ms | 799.424 ms | 913.716 ms |
| 4 | 30/30 | 4.578 req/s | 801.639 ms | 1,403.841 ms | 1,459.243 ms |
| 8 | 30/30 | 5.124 req/s | 1,096.513 ms | 3,013.291 ms | 3,045.428 ms |

All 90 requests succeeded. The SLO still fails because concurrency-8 p95 exceeded the
2,000 ms limit.

### Backend memory

| Measurement | Result |
|---|---:|
| Baseline RSS | 256.965 MiB |
| Peak observed RSS | 264.180 MiB |
| Ending RSS | 260.676 MiB |
| Ending growth | 3.711 MiB |

This is observed process RSS during one load run, not a leak proof. A long-duration soak
test with forced revision churn is needed to distinguish retained caches from leaks.

### Cold versus warm HTTP calls

| Endpoint | First call | Warm mean | Observation |
|---|---:|---:|---|
| Health | 1,099.287 ms | 352.221 ms | Warm improvement |
| Analysis | 560.022 ms | 598.953 ms | No improvement |
| Summary | 546.356 ms | 452.820 ms | Warm improvement |
| Index | 724.878 ms | 780.379 ms | Warm regression |
| Tree | 773.688 ms | 762.655 ms | Essentially flat |
| Latest conversation | 409.608 ms | 510.417 ms | Warm regression |

These timings indicate that “warm” does not universally mean cached. The next benchmark
should record explicit cache hit/miss counters instead of inferring cache use solely from
latency.

## 6. Frontend performance and Web Vitals

| Metric | Result |
|---|---:|
| HTTP status | 200 |
| Browser TTFB | 17.9 ms |
| DOMContentLoaded | 200.1 ms |
| Load event | 206.2 ms |
| Largest Contentful Paint | 520 ms — PASS |
| Cumulative Layout Shift | 0.0037 — PASS |
| Resource requests | 29 |
| Resource transfer | 3,559,982 bytes |
| Long tasks | 1, lasting 73 ms |
| JavaScript heap used / total | 10.681 / 12.779 MiB |

INP is not reported because the benchmark does not yet perform a representative user
interaction. Reporting zero would be fabricated. A future interaction script should open
a large repository, manipulate the graph, select a card, open source, and send an Ask
message before measuring INP.

## 7. Frontend behavior and code coverage

- Playwright: one executed application workflow passed; one Voicebox fixture test skipped
  because `WAYPOINT_VOICEBOX_PATH` was not configured.
- Vitest: three unit tests passed.
- V8 coverage: 1.66% statements, 1.96% branches, 0.93% functions, 1.55% lines.
- Production TypeScript/Vite build: passed.

The browser success rate is 100% for the one executed workflow, but the frontend does not
have enough unit/component coverage to claim reliability. This is the largest test-suite
gap.

## 8. Traditional backend test coverage

This is retained as a separate maintainability metric:

- 79 backend tests passed.
- Whole-backend branch-aware coverage.py result: 74.65% — PASS.
- Line coverage: 78.2%.
- Branch coverage: 61.6%.
- Explicit weighted core-feature coverage: 83.00% — PASS.

Code coverage answers “which implementation paths executed?” Operational evaluation
answers “did the service produce correct, relevant, reliable, and timely outcomes?” Both
are useful; neither substitutes for the other.

## 9. Priority remediation plan

1. **Retrieval ranking:** prioritize exact implementation paths/symbols, reduce broad
   documentation and frontend dominance for backend questions, and rerun the eight gold
   cases plus larger external datasets.
2. **Frontend tests:** add component tests for `AskWorkspace`, `GraphView`, `FileTree`,
   `OnboardingWorkspace`, `IssuesWorkspace`, API errors, transcript restoration, and
   loading/error states. Enforce incremental thresholds rather than pretending 1.55% is
   acceptable.
3. **OpenRouter reliability:** retain portable schemas, add one bounded repair attempt for
   invalid cardinality/path ranges, and score both first-attempt and repaired success.
4. **Concurrency:** profile the analysis/index/tree serialization paths and eliminate
   repeated large-object construction before increasing the 2-second SLO.
5. **Cache observability:** emit cache-hit, miss, load, eviction, size, and saved-time
   counters. Add warm/cold semantic-cache and revision-cache comparisons.
6. **Accuracy labels:** expand from four Flask questions to multiple repositories and
   languages with human-reviewed answers and citation-entailment labels.
7. **Performance depth:** add a soak test, memory growth slope, CPU utilization, database
   timing, representative INP, and production-build Web Vitals.

## 10. Reproduction commands

```powershell
# Backend tests and traditional coverage
.\.venv\Scripts\python.exe -m pytest backend\tests -q
.\.venv\Scripts\python.exe -m verification.scripts.coverage_report

# Gold parser and deterministic retrieval
.\.venv\Scripts\python.exe benchmarks\run_parsing_accuracy.py
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py --repository . `
  --output benchmarks\results\retrieval-latest.json

# Backend load/cache/RSS
.\.venv\Scripts\python.exe -m verification.scripts.service_performance `
  --analysis-id <ANALYSIS_ID> --server-pid <BACKEND_PID> --requests-per-level 30

# Live same-evidence model comparison; spends model quota
.\.venv\Scripts\python.exe -m verification.scripts.model_quality_benchmark `
  --analysis-id <FLASK_ANALYSIS_ID> --label claude-code-openrouter `
  --confirm-live-model-usage

# Frontend
cd frontend
npm run test:coverage
$env:WAYPOINT_E2E_BASE_URL='http://localhost:5173'
npm run test:e2e
npm run benchmark:frontend
npm run build

# Unified operational gate
cd ..
.\.venv\Scripts\python.exe -m verification.scripts.operational_scorecard
```

## 11. Evidence files

- `verification/results/operational-scorecard.md`
- `verification/results/operational-scorecard.json`
- `verification/results/model-quality-claude-code-openrouter.md`
- `verification/results/model-quality-claude-code-openrouter.json`
- `verification/results/service-performance.json`
- `verification/results/frontend-performance.json`
- `verification/results/coverage-report.md`
- `benchmarks/results/parsing-accuracy.json`
- `benchmarks/results/retrieval-latest.json`
- `frontend/coverage/coverage-summary.json`
- `frontend/test-results/results.json`

