# Operational evaluation: what successfully works

This suite measures application behavior, not merely whether Python statements ran.
The operational gate is 70% **for every category and overall**. A high average cannot
hide a failed category or a weak model endpoint.

## Current measured result

The authoritative generated scorecard is
[`../results/operational-scorecard.md`](../results/operational-scorecard.md). The latest
run scored 71.9% overall but **failed** the release gate:

| Category | Result | What is measured |
|---|---:|---|
| Parser accuracy | 100.0% | Parse success and gold node/edge precision, recall, and F1 |
| Deterministic retrieval | 58.8% | Hit@5, Hit@10, MRR, and nDCG@10 |
| Two-model answer quality | 83.2% average; failed endpoint gate | Valid answers, required concepts, and expected cited files |
| Service/browser performance | 66.7% | HTTP success, load SLO, process memory, cache timing, and Web Vitals |
| Frontend quality | 50.8% | Playwright scenario success and measured React/TypeScript line coverage |

The parser figure is from a controlled multilingual gold fixture. It does not claim
perfect accuracy on arbitrary repositories. The raw evidence must always accompany the
score.

## 1. Repository parsing accuracy

`benchmarks/run_parsing_accuracy.py` builds nine audited source files covering Python,
JavaScript, TypeScript, TSX, Java, HTML, and CSS. Expected semantic nodes and
relationships are compared with analyzer output using precision, recall, and F1. This
is more meaningful than checking only that parsing did not throw an exception.

```powershell
.\.venv\Scripts\python.exe benchmarks\run_parsing_accuracy.py
```

Raw result: `benchmarks/results/parsing-accuracy.json`.

## 2. Retrieval quality

`benchmarks/run_retrieval.py` uses eight gold questions and expected source files. It
measures:

- Hit@5 and Hit@10: whether any expected file appears in the first 5 or 10 results.
- MRR: how early the first correct file appears.
- nDCG@10: whether all relevant files are ranked near the top.
- cold/warm mean plus p50, p95, and p99 latency.
- index-build and restore time and process peak memory.

```powershell
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py `
  --repository . `
  --output benchmarks\results\retrieval-latest.json
```

This retriever is deterministic and model-independent. Model-specific retrieval quality
is measured separately as **expected cited-file recall and precision**: both models see
one frozen candidate set, and the benchmark scores which evidence each model actually
selects in its citations. Calling those two layers both “retrieval” would conceal whether
the index or the model caused a miss.

Latest local-index measurements were 47.94 seconds to build, 641.9 ms to restore,
191.62 MiB process peak memory, and roughly 3.07 seconds mean query latency. The poor
58.8% composite retrieval score is therefore both a relevance and a latency optimization
target, not a passing result.

## 3. Model-answer accuracy and two-model evidence selection

`verification/scripts/model_quality_benchmark.py` runs four Flask gold questions. Each
question performs retrieval once, hashes the immutable evidence, then sends the same
question and evidence concurrently to Claude Code Sonnet and OpenRouter. It records:

- validated-answer success and provider/schema failures;
- deterministic required-concept recall;
- expected cited-file recall and precision;
- input, cache-read, cache-creation, output, and effective input tokens when exposed;
- wall latency, output throughput, tool calls, citations, and output size;
- TTFT when the provider exposes a real streaming event.

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.model_quality_benchmark `
  --analysis-id <FLASK_ANALYSIS_ID> `
  --label claude-code-openrouter `
  --confirm-live-model-usage
```

Latest live result: Claude Code passed 4/4 tasks. OpenRouter's free router passed 2/4,
returned two validated responses, and had two validation failures. Claude averaged
96.4% required-concept recall and 87.5% expected-file recall. OpenRouter averaged 90.0%
concept recall and 75.0% expected-file recall across its successful responses.

The concept rubric is repeatable but is not a semantic judge. A human-reviewed sample or
an independently calibrated judge model should be added before calling this a production
factual-accuracy score.

## 4. TTFT, tokens, memory, latency, and caching

OpenRouter comparisons use SSE streaming. TTFT is dispatch-to-first meaningful content
or tool-call delta. Latest p50 was about 23.9 seconds over the two valid OpenRouter
responses. The Claude Agent SDK returns
assistant and final-result events, not raw token deltas, so Claude TTFT is marked `N/A`;
the first assistant-event duration remains in `ttft_status` as a diagnostic and is not
misrepresented as token TTFT.

`verification/scripts/service_performance.py` measures cold versus three warm calls per
endpoint, concurrency at 1/4/8 workers, request throughput, p50/p95/p99/max latency,
success rate, and backend RSS before/during/after load. The latest 90-request test had
100% request success. RSS rose from 256.965 MiB to a 264.180 MiB observed peak and ended
3.711 MiB above baseline. Concurrency 8 missed the 2-second p95 SLO at 3013.3 ms.

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.service_performance `
  --analysis-id <ANALYSIS_ID> `
  --server-pid <BACKEND_PID> `
  --requests-per-level 30
```

`npm run benchmark:frontend` records browser TTFB, DOM/load timing, transfer size, long
tasks, JavaScript heap, LCP, and CLS. Latest development-server results: 17.9 ms TTFB,
520 ms LCP, 0.0037 CLS, one 73 ms long task, and 10.68 MiB used JavaScript heap. INP is
left unavailable because no representative interaction is scripted; it is not fabricated.

## 5. Frontend tests and percentage coverage

Playwright validates the real browser flow over HTTP. Vitest/V8 instruments all frontend
`src/**/*.ts` and `src/**/*.tsx`, including untested files, so the baseline is honest:
1.55% lines, 1.67% statements, 1.97% branches, and 0.94% functions. This is measured but
does not yet satisfy the 70% target.

```powershell
cd frontend
npm run test:coverage
$env:WAYPOINT_E2E_BASE_URL='http://localhost:5173'
npm run test:e2e
npm run benchmark:frontend
```

## Build the unified scorecard

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.operational_scorecard
```

The JSON artifacts are the source of truth. The scorecard is a declared product rubric,
not a universal definition of software coverage, and standard pytest coverage remains a
separate maintainability signal.
