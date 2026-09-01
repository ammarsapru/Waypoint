# Waypoint: complete benchmark, test, limits, and evaluation report

Last updated: 2026-07-26  
Benchmark specification version: 2.0  
Core release threshold: 70% for every named core feature  
Operational quality threshold: 70% for every category and the overall score

## Executive conclusion

Waypoint has three different kinds of percentages. They must not be confused:

1. **Code coverage** asks which executable statements and branch outcomes the automated tests exercised. The latest backend run executed **82 tests** (44 unit, 38 integration) and reported **74.18% combined branch-aware coverage**, above the 70% gate.
2. **Core-feature coverage** applies that same instrumentation to an explicit list of core modules. Its most recently generated artifact reports an **83.00% weighted aggregate**, with every declared core feature above 70%. It proves test execution of those modules; it does not prove answer correctness.
3. **Operational quality** asks whether parsing, retrieval, model answers, performance, and the frontend behave well. This is the appropriate place for task success, precision, recall, latency, memory, and Web Vitals. The current strict-v2 scorecard is **68.69% overall and FAIL**: parsing passes, while retrieval, one model, performance, and frontend quality do not independently clear 70%.

The evaluation has now been made stricter. The specification contains **50 tasks**, the deterministic retrieval benchmark uses required evidence groups and distractors, and the live model benchmark contains **12 questions** rather than four. A test definition is not represented as a passing result until it has actually run.

## What is benchmarked

| Layer | What can fail | Principal metrics | Current executable artifact |
|---|---|---|---|
| Parser/graph | missed or invented files, symbols, imports, calls | precision, recall, F1, parse-success rate | `benchmarks/results/parsing-accuracy.json` |
| Retriever | relevant evidence absent, late, incomplete, or noisy | Hit@k, precision@k, required-group recall@k, MRR, MAP, nDCG, forbidden-hit rate | `benchmarks/results/retrieval-latest.json` |
| Answer model | incomplete, unsupported, malformed, or failed answer | concept recall, evidence-group recall, citation precision, schema pass, task pass | `verification/results/model-quality-*.json` |
| Agent behavior | bad tool selection/order, loops, policy violation | trajectory validity, tool-call count, rounds, bounded retries, outcome | traces plus 50-case manifest |
| Onboarding | generic route, invalid file, wrong order or budget | grounded-file rate, objective sensitivity, ordering, challenge correctness | HTTP/model cases in the manifest |
| Issues | fabricated or ungrounded finding, bad GitHub handling | evidence validity, schema validity, confidence bounds, provider failure handling | HTTP/model cases in the manifest |
| API/E2E | components work alone but not together | request success, assertions, persistent identity | `verification/results/http-flask-e2e.json` |
| Frontend | broken user workflow or untested React code | Playwright outcome, line/branch/function coverage, LCP, CLS, INP | `frontend/test-results`, `frontend/coverage`, frontend performance JSON |
| Runtime | slow, unreliable, memory-heavy | p50/p95/p99 latency, throughput, error rate, RSS, cache delta | `verification/results/service-performance.json` |
| Model comparison | provider trade-offs hidden by unequal context | same frozen evidence, task pass, tokens, TTFT, latency, tools, output | `verification/results/model-quality-claude-code-openrouter.json` |
| MCP | service capabilities unavailable to external agents | tool inventory and delegation tests | `backend/tests/test_mcp_server.py` |

## Research-backed agentic RAG evaluation design

Waypoint’s evaluation follows these principles:

- **Separate retrieval from generation.** RAGAS evaluates context relevance, faithfulness, and answer relevance separately; ARES similarly separates context relevance, answer faithfulness, and answer relevance. A strong final answer must not hide a weak retriever, and good retrieval must not excuse unsupported generation.
- **Use fine-grained diagnostics.** RAGChecker recommends claim-level and component-level checks because a single aggregate score cannot say whether the retriever omitted evidence or the generator ignored it.
- **Evaluate the trajectory as well as the final answer.** LangSmith’s agent-evaluation guidance distinguishes final-response, trajectory/tool-use, and individual-step evaluation. Waypoint therefore records tool names, arguments/results, rounds, timing, and validation attempts.
- **Require genuine multi-hop evidence.** HotpotQA demonstrates multi-document questions with supporting facts. The 8 multi-hop cases here require ordered links across multiple subsystems. The dataset-design literature warns that purported multi-hop tests can be solved from one shortcut passage, so a case must require independent evidence groups.
- **Test dynamic interaction and policy adherence.** Tau-bench evaluates agents interacting with tools and users over multiple turns. Waypoint includes memory follow-ups, tool-round limits, provider failures, and bounded retries rather than evaluating only one-shot prose.
- **Use heterogeneous repositories and adversarial negatives.** BEIR shows why a single homogeneous retrieval task overstates generalization. Waypoint uses its own repository, Flask, and PRAW, and adds forbidden/distractor paths.
- **Calibrate model judges with humans before treating them as authoritative.** The automated concept matcher is intentionally called a proxy. A human-reviewed sample should establish inter-rater agreement and judge thresholds before an LLM judge becomes a release gate.
- **Run offline regression and online monitoring.** The committed suite detects regressions; production traces should separately track latency, errors, retrieval drift, and user corrections without storing hidden chain-of-thought.

Primary references:

- RAGAS: <https://aclanthology.org/2024.eacl-demo.16/>
- RAGChecker: <https://arxiv.org/abs/2408.08067>
- ARES: <https://aclanthology.org/2024.naacl-long.20/>
- LangSmith evaluation concepts: <https://docs.langchain.com/langsmith/evaluation-concepts>
- LangSmith complex-agent evaluation: <https://docs.langchain.com/langsmith/evaluate-complex-agent>
- HotpotQA: <https://aclanthology.org/D18-1259/>
- Multi-hop dataset-design caveat: <https://aclanthology.org/N19-1405/>
- Tau-bench: <https://arxiv.org/abs/2406.12045>
- BEIR: <https://openreview.net/forum?id=wCu6T5xFjeJ>

## The 50-case suite

The machine-readable source is `verification/agentic-rag-evaluation-cases.json`. A unit test enforces exactly 50 unique, categorized, non-empty cases.

| Category | Cases | What it adds beyond the old eight questions |
|---|---:|---|
| Ask retrieval and grounding | 12 | Waypoint, Flask, and PRAW; overviews and focused implementation questions |
| Multi-hop reasoning | 8 | ordered chains spanning API, clone, parser, index, agent, storage, and UI |
| Onboarding | 8 | roles, objectives, 15/120-minute budgets, challenges, missions, and PRAW skills |
| Issues | 6 | open/closed history, timelines, grounded synthesis, approaches, and provider failures |
| Polyglot parsing | 6 | Python, JS, TS, Java, context formats, aliases, and duplicate handling |
| Memory, routing, and limits | 6 | follow-up context, isolation, Claude/OpenRouter, OpenRouter/OpenRouter, input and loop limits |
| Frontend E2E | 4 | analysis, restored sessions, Ask/evidence UX, onboarding/issues UX |
| **Total** | **50** | |

These cases are a specification with mixed execution modes. Deterministic cases may run on every commit. Live-provider and browser-provider cases should run in a controlled evaluation job because they spend quota and can be nondeterministic. “Defined” never means “passed.”

## Severe retrieval evaluation (v2)

### Why the old gold labels were insufficient

The old eight cases supplied one or two exact expected paths. That caused two problems:

- a correct alternative implementation or documentation file could be incorrectly penalized;
- retrieving one expected file could pass even when a multi-file answer required three separate facts.

Each v2 case can therefore declare:

- `required_path_groups`: independent facts, with acceptable alternatives inside each group;
- `acceptable_paths`: relevant supporting files beyond the minimum groups;
- `forbidden_paths`: known distractors whose retrieval is penalized;
- `max_first_relevant_rank`: how soon useful evidence must appear;
- `min_required_group_recall_at_10`: completeness requirement;
- `min_precision_at_5`: noise requirement.

Results are deduplicated by file before scoring so ten chunks from one file do not appear to be ten independent pieces of evidence.

### Metric definitions

| Metric | Meaning | What success looks like |
|---|---|---|
| Hit@k | at least one acceptable file appears in the first k unique files | useful evidence is not absent |
| Precision@k | acceptable retrieved files / retrieved files in first k | low noise |
| Path recall@k | acceptable paths found / all labeled acceptable paths | broad supporting coverage; deliberately demanding |
| Required-group recall@k | evidence groups satisfied / required groups | every independent reasoning hop is available |
| MRR | mean of `1 / rank of first relevant file` | useful evidence ranks early |
| AP@10 / MAP@10 | precision accumulated at relevant ranks, averaged per query/across queries | multiple relevant files rank early |
| nDCG@10 | discounted ranking gain normalized by the ideal order | better ordering receives more credit |
| Forbidden-hit rate | fraction of cases with a known distractor in top 10 | lower is better |
| Strict case pass | rank, group-recall, precision, and forbidden checks all pass | the complete case contract is satisfied |

The v2 retrieval score is the equal-weight mean of strict-case pass rate, required-group recall@10, MAP@10, MRR, nDCG@10, and `1 - forbidden-hit-rate`. This score replaces the old mean of Hit@5, Hit@10, MRR, and nDCG in newly generated operational scorecards.

### Latest v2 measured result

The strict eight-query benchmark completed against this repository in 92.9 seconds (one query repetition). Index construction took 49.10 seconds, persisted-index restore took 628 ms, and observed process peak memory was 194.01 MiB.

| Metric | Result |
|---|---:|
| Hit@1 / Hit@3 / Hit@5 / Hit@10 | 25.00% / 100.00% / 100.00% / 100.00% |
| Precision@5 / Precision@10 | 35.00% / 26.25% |
| Path recall@5 / Path recall@10 | 35.03% / 51.70% |
| Required-group recall@5 / @10 | 53.13% / 67.71% |
| MAP@10 | 25.26% |
| MRR | 58.33% |
| nDCG@10 | 43.33% |
| Forbidden-path case rate | 37.50% |
| Strict case pass rate | 0/8 (0.00%) |
| **Strict retrieval score** | **42.86% — FAIL** |
| Mean / p95 query latency | 3,277 ms / 3,442 ms |

This is a materially more honest result than the superficially strong 100% Hit@10: every query found something relevant, but not enough independent evidence, with too much noise and three cases containing a known distractor. No case simultaneously met early-rank, 100% evidence-group recall, >=60% precision@5, and zero forbidden-path requirements. The immediate engineering priority is retrieval quality, not changing the threshold.

## Model answer evaluation and comparison

### Fair comparison protocol

`ModelComparisonService` performs retrieval once, freezes the evidence fingerprint, and passes the same question and evidence bundle to both configured endpoints. This prevents one provider from receiving easier evidence. Each result records:

- provider and model ID;
- schema/validation status and failures;
- wall-clock latency;
- OpenRouter streaming TTFT when available;
- Claude SDK first observable assistant event, which is not mislabeled as raw token TTFT;
- fresh input, cache-creation input, cache-read input, effective input, and output tokens when the provider reports them;
- estimated cost when known;
- output characters and output tokens/second;
- total, repository, and structured-output tool calls;
- citations and cited paths.

### Answer pass rule (v2)

A task passes only if all of these are true:

1. answer is non-empty;
2. structured output passes the Pydantic/schema validator;
3. at least one citation exists;
4. required-concept recall meets the case threshold (normally 0.8);
5. required-evidence-group recall meets the case threshold (0.67 or 1.0);
6. citation precision meets the case threshold (normally 0.6).

Citation precision is `supported cited file paths / all distinct cited file paths`. This directly addresses the earlier weakness where citing nine files could look successful merely because two expected files appeared. Alternative valid files are listed in `acceptable_paths`, so the metric does not insist there is only one correct evidence set.

### Historical measured Claude Code versus OpenRouter run

The existing four-task artifact was run against a pinned local Flask checkout and used the earlier v1 labels. It remains historical evidence and is not silently relabeled as a v2 run.

| Endpoint | Successful responses | Task pass | Mean concept recall | Mean expected-file recall | p50 / p95 latency | TTFT p50 | Effective input | Output | Tool calls | Citations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Claude Code `sonnet` | 4/4 | 100% | 96.43% | 87.50% | 27.54s / 47.08s | unavailable as token TTFT | 132,208 | 11,266 | 4 | 23 |
| OpenRouter `openrouter/free` | 2/4 | 50% | 90.00% on returned answers | 75.00% | 34.43s / 48.91s | 23.94s (2 samples) | 10,430 | 3,234 | 2 | 4 |

OpenRouter had two provider/validation failures; therefore its 50% success is the decisive failure even though returned answers had high concept recall. Claude’s `input_tokens=12` is not total context: the SDK separately reported 20,237 cache-creation and 111,959 cache-read tokens, producing 132,208 effective input tokens.

The v2 live corpus now contains 12 Flask questions: overview, sessions, request lifecycle, blueprints, contexts, errors, templating, CLI discovery, JSON provider, signals, test client, and configuration. It must be rerun to produce a current strict comparison. The run command intentionally requires `--confirm-live-model-usage`.

### Required two-model combinations

| Combination | Status | Required configuration/test |
|---|---|---|
| Claude Code + Claude/API | historical model-routing support exists; not the target cross-provider proof | distinct model IDs and same frozen evidence |
| Claude Code + OpenRouter | measured on four v1 Flask tasks; 12-task v2 rerun pending | one Claude endpoint and one OpenRouter endpoint |
| OpenRouter + OpenRouter | architecture supports two roles/models; live paired v2 result pending | two distinct OpenRouter model IDs, same question/evidence |

The report must name actual model IDs from every run. “OpenRouter” is a provider, not a model, and `openrouter/free` is a router that may select different free models; it is unsuitable for a controlled model-to-model conclusion unless the resolved model is recorded.

## Input, retrieval, output, and safety limits

Defaults can be overridden through environment variables where stated.

| Limit | Default / enforced range | Purpose |
|---|---:|---|
| repository path or GitHub URL | 1-2,048 characters | request validation |
| allowed local root | `ONBOARD_ALLOWED_ROOT`, default process working directory | prevents arbitrary filesystem traversal |
| clone destination | `ONBOARD_CLONE_ROOT`, default `.waypoint-clones` | isolates imports |
| clone timeout | 180 seconds, minimum 10 | bounds subprocess time |
| clone checkout size | 1,000,000,000 bytes, minimum 1,000,000 | disk/resource protection |
| clone files | 100,000, minimum 100 | checkout explosion protection |
| retained clones | 10, minimum 1 | disk retention bound |
| analyzed repository files | 5,000, minimum 1 | analysis/index bound |
| source file size | 2,000,000 bytes, minimum 1,024 | decoding/model-input bound |
| unresolved-call details | 20,000 | response size bound |
| symbol/module chunk | up to about 160 lines | stops giant modules dominating retrieval |
| non-symbol/context chunk | 120 lines, stride 100 (20-line overlap) | preserves boundary context |
| direct source read | at most 250 lines per call | bounded tool output |
| tree listing | implementation cap 5,000 paths | bounded discovery output |
| search result limit | internal retrieval/tool-specific caps; model comparison 2-15 evidence items | controls prompt evidence volume |
| Ask/onboarding question | 2-500 characters | bounded prompt input |
| supplied request history | at most 10 messages | caller-provided memory bound |
| stored chat context | 12 turns by default, configurable 1-50 | persistent recent-memory window |
| agent tool rounds | 8 by default, configurable 1-20 | stops unbounded agent loops |
| Claude Code turns | 8 by default, hard range 1-8 | SDK execution bound |
| investigation rounds | 3 by default, hard range 1-10 | task-role bound |
| synthesis attempts | 2 by default, hard range 1-5 | validation retry bound |
| generated output request | 6,000 tokens by default, range 512-32,000 | provider output budget |
| final Ask answer | 1-30,000 characters | schema output guard |
| answer basis | 1-2,000 characters | concise provenance |
| answer citations | at most 30 | response/UX bound |
| suggested follow-ups | at most 5 | response bound |
| citation relevance | at most 500 characters | response bound |
| onboarding duration | 5-120 minutes | product route bound |
| onboarding steps | 2-10 | route-size guard |
| files per onboarding step | 1-8 | cognitive/input bound |
| submitted challenge answer | at most 4,000 characters | validation/model bound |
| issue findings | at most 20 | response bound |
| evidence per issue | 1-12 spans | grounding bound |
| issue approach | 2-12 steps | output structure bound |
| OpenRouter timeout | 180 seconds, minimum 15 | request bound |
| Claude Code timeout | 180 seconds, minimum 15 | SDK bound |
| trace value | 4,000 characters by default, minimum 128 | prevents unbounded logs |

The application does not send an entire repository to a model. It retrieves bounded chunks/tools from the immutable revision. Provider context-window limits still apply after system prompts, history, tool schemas, retrieved evidence, and generated output are combined. A future token-budget allocator should enforce a single explicit prompt budget before provider dispatch; today the strongest hard controls are evidence count, read lines, history, rounds, and output tokens.

## Parsing and indexing accuracy

The parser test must compare produced nodes and edges with hand-authored fixture truth, not merely check that parsing did not crash.

- Node precision = correct emitted nodes / all emitted nodes.
- Node recall = correct emitted nodes / all expected nodes.
- Edge precision = correct emitted relationships / all emitted relationships.
- Edge recall = correct emitted relationships / all expected relationships.
- F1 = harmonic mean of precision and recall.
- Parse success = files parsed without parser failure / files intended for parsing.

The current stored parsing artifact reports 100% on its fixtures. That is strong evidence for those fixtures only; it is not a claim of perfect Python, JavaScript, TypeScript, or Java support in arbitrary repositories. The 50-case suite adds explicit per-language and non-AST context-file cases.

Markdown, JSON, XML, text, shell, PowerShell, CSV, HTML, and CSS can be visible, read, and line-window chunked if accepted by repository discovery. HTML and CSS also have parser support. A format being retrievable does not imply it emits class/function/call graph nodes.

## Unit, integration, HTTP, and browser tests

Latest backend command: `python -m pytest -q`

| Suite | Count | Meaning |
|---|---:|---|
| Unit | 44 | deterministic isolated algorithm/service behavior |
| Integration | 38 | boundaries such as API-store-agent, index-graph, MCP-service |
| Backend total | 82 | latest run: all passed in 26.33 seconds |

The suite enforces classification in `backend/tests/conftest.py`; an unclassified test module fails collection. The latest coverage result is **74.18% combined branch-aware**, above the 70% gate. The newly added tests validate strict retrieval scoring, file-level deduplication, the exact 50-case manifest, and the 29-tool MCP inventory.

The pinned Flask HTTP E2E artifact analyzed 83/83 parser-targeted files with zero parse failures, produced 1,704 nodes and 2,535 edges, persisted 1,734 chunks/vectors, and checked source, symbols, architecture, and index endpoints. This tests real component composition without spending model quota.

The latest stored Playwright run has one executed passing workflow and one skipped live-model workflow. That is insufficient breadth. The stored frontend instrumentation reports only **1.55% line coverage** because the major React workspaces were not executed under coverage. Passing one browser path must not be presented as adequate frontend coverage.

## Performance, percentiles, cache, and memory

`p50` is the median. `p95` is the latency under which 95% of observations fall; it describes a slow-tail user experience. `p99` describes the slowest 1% tail. These values are meaningful only with the sample count and concurrency recorded.

The stored service load run made 30 requests at each concurrency level:

| Concurrency | Success | Throughput | p50 | p95 | p99 |
|---:|---:|---:|---:|---:|---:|
| 1 | 30/30 | 1.802 req/s | 546 ms | 799 ms | 914 ms |
| 4 | 30/30 | 4.578 req/s | 802 ms | 1,404 ms | 1,459 ms |
| 8 | 30/30 | 5.124 req/s | 1,097 ms | 3,013 ms | 3,045 ms |

The service SLO requires >=99% success and p95 <=2,000 ms. Success was 100%, but concurrency-8 p95 was 3,013 ms, so the SLO failed. RSS moved from 256.965 MiB to a 264.180 MiB observed peak and ended at 260.676 MiB, a 3.711 MiB net increase. RSS includes Python, native libraries, SQLite pages, and allocator behavior; it is not a precise per-request allocation measure.

Cold/warm endpoint measurements exist, but some warm means were slower than the one cold sample. This means the results do not prove a cache improvement. A credible cache benchmark requires repeated alternating cold/warm trials, explicit cache reset, confidence intervals, and separate index-restore, retrieval, prompt-cache, and HTTP response-cache measurements.

Frontend measurements: LCP 520 ms and CLS 0.0037 met the recorded 2,500 ms and 0.1 thresholds. INP was not measured because the run did not script a representative interaction. LCP measures when the largest visible content element renders; CLS measures unexpected layout movement; INP measures interaction responsiveness.

## Hooks, logs, and safe agent observability

`backend/app/observability.py` defines structured events, `@traced` pre/post/error wrappers, argument/result truncation, correlation context, and optional JSONL trace persistence. Calls are placed across HTTP routes, repository import, analyzers, indexing, retrieval, provider dispatch, agent rounds, tool execution, validation, subprocesses, and MCP functions.

Useful events include:

- function received/completed/failed;
- subprocess dispatched/output/exit;
- provider request start/completion/failure;
- each agent round and tool request/result;
- retrieval query candidates and selected evidence;
- validation rejection and retry;
- memory load/store;
- MCP request lifecycle.

Logs may capture observable decisions, tools, arguments, evidence, structured outputs, timing, token usage, and errors. They should **not claim to expose private chain-of-thought**. Precise debugging should rely on reproducible inputs, external actions, outputs, and validation reasons. Secrets and full sensitive source payloads should be redacted or bounded.

## MCP parity and why MCP is useful

The MCP server now exposes **29 tools**, two resources, and one prompt. The inventory test obtains the registry from FastMCP rather than trusting documentation.

Tool families:

- analysis/import: local analysis and secure GitHub clone+analysis;
- repository discovery: recent analyses, tree, index status, rebuild;
- retrieval/source: search, read source, find/inspect symbols;
- graph/architecture: expansion, relationships, dependency impact, tests, entry points, backend architecture, full architecture, code journey, revision comparison;
- conversation/AI: latest conversation, Ask, frozen two-model comparison;
- onboarding: route, challenge, contribution mission;
- issues: list, timeline, investigation.

An LLM that can read files still benefits from this MCP because Waypoint provides stable revision identity, safe root enforcement, deduplicated context files, parsed cross-language symbols, static relationships with evidence status, hybrid retrieval, bounded reads, conversation persistence, issue history, onboarding workflows, and the same validated services used by the UI. MCP reduces repeated repository crawling and makes provenance and tool behavior consistent across clients. It does not make the LLM omniscient and it does not replace direct source verification.

## Output validation and retry behavior

Model outputs are requested through structured tools/schemas and parsed with Pydantic. Validation checks field lengths, enum/range constraints, file membership, node identity, line spans, citation existence, and feature-specific invariants. A malformed or ungrounded result is not displayed as a success.

The flow is:

1. retrieve or inspect revision-bound evidence;
2. request structured model output;
3. validate schema and repository provenance;
4. return if valid;
5. otherwise append concise correction feedback and retry up to `synthesis_max_attempts` (default 2);
6. return a typed failure after the bound is exhausted.

Provider selection and fallback are separately bounded. Tool rounds default to eight and Claude Code turns have a hard maximum of eight. A timeout, billing error, maximum-turn result, schema error, or unavailable provider must remain distinguishable in the logs and API response.

## Release gates and interpretation

The 70% number is a project policy, not an industry law. A release should pass only when:

- every declared core feature has >=70% combined code coverage;
- all deterministic unit/integration tests pass;
- operational parsing, retrieval, answer, frontend, and performance categories each pass their thresholds;
- both required live model combinations have a recent artifact with resolved model IDs;
- all critical 50-case scenarios pass, while quota-dependent cases may be explicitly marked not run in ordinary local CI;
- no severe security, data-loss, or fabricated-citation failure is hidden by averaging.

A high average cannot compensate for a failing critical category. Results must always show numerator, denominator, sample size, repository revision, model ID, configuration, and timestamp.

## Known failures and remediation priorities

1. **Retrieval v1 failed (58.76%).** Important implementation files ranked at positions 8-9 or were absent in the top 10. Remedy: stricter evidence groups reveal the problem; improve intent routing, symbol/file priors, query decomposition, and graph expansion, then rerun rather than loosening labels.
2. **OpenRouter v1 returned only 2/4 valid answers.** Remedy: record resolved model, distinguish provider from validator failures, choose a stable explicit model, apply bounded retry, and rerun all 12 strict cases.
3. **Service p95 failed at concurrency 8.** Remedy: profile SQLite/index serialization and large response construction; add endpoint-level concurrency data before optimizing.
4. **Frontend line coverage was 1.55%.** Remedy: component tests for Ask, Onboarding, Issues, Graph, source viewer, memory restoration, loading/error states; collect browser coverage for representative flows.
5. **Only one non-live browser flow executed.** Remedy: run the four committed E2E scenarios, with live cases in an explicit quota-enabled job.
6. **Model correctness remains partly proxy-scored.** Remedy: human-label a stratified answer set for claim correctness, completeness, citation entailment, and unsupported claims; calibrate any LLM judge against it.
7. **Historical scorecards use v1 scoring.** Remedy: never overwrite provenance; regenerate v2 artifacts and label their scorer version.
8. **Cache benefit is not demonstrated.** Remedy: controlled repeated cold/warm trials with cache reset and confidence intervals.

## Reproduction commands

```powershell
# Backend unit + integration suite and branch-aware coverage
.\.venv\Scripts\python.exe -m pytest -q

# Strict deterministic retrieval benchmark
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py `
  --repository . `
  --cases benchmarks\retrieval_cases.json `
  --output benchmarks\results\retrieval-latest.json `
  --repeats 3

# Live 12-task Claude/OpenRouter comparison (spends quota)
.\.venv\Scripts\python.exe verification\scripts\model_quality_benchmark.py `
  --analysis-id <PINNED_FLASK_ANALYSIS_ID> `
  --confirm-live-model-usage `
  --label claude-code-openrouter-v2

# Rebuild operational scorecard from current artifacts
.\.venv\Scripts\python.exe verification\scripts\operational_scorecard.py

# MCP inventory/parity tests
.\.venv\Scripts\python.exe -m pytest backend\tests\test_mcp_server.py -q --no-cov

# Frontend unit tests, coverage, build, and browser tests
Set-Location frontend
npm run test:coverage
npm run build
npm run test:e2e
```

## Artifact authority

- Test specifications: `verification/agentic-rag-evaluation-cases.json`, `verification/model-quality-cases.json`, `benchmarks/retrieval_cases.json`
- Scorers: `benchmarks/run_retrieval.py`, `verification/scripts/model_quality_benchmark.py`, `verification/scripts/operational_scorecard.py`
- Raw results: JSON files under `benchmarks/results` and `verification/results`
- Human-readable code coverage: `verification/results/coverage-report.md` and `.waypoint-data/coverage-html/index.html`
- MCP implementation: `backend/app/mcp_server.py`
- Current configuration limits: `backend/app/config.py` and Pydantic models

When prose and raw JSON disagree, the raw result plus the exact scorer revision is authoritative. Generated reports must state whether a result is historical, current, pending, skipped, or failed.

---

# Run history: V2 citation enforcement and retrieval improvement

Date: 2026-07-26  
Git commit: `0df646b`  
Status: implemented, measured, tested, and pushed  
Relationship to earlier sections: **append-only update; all baseline results above remain historical evidence**

## Why this run was added

The preceding run established strict retrieval groups and a 50-case capability manifest, but two evaluation gaps remained:

1. model-answer cases named acceptable files without declaring the expected citation line ranges;
2. the model benchmark did not fail answers for excessive length, excessive citations, over-wide citation spans, or reported output tokens above the evaluation budget.

The preceding strict retrieval run also failed on MAP@10, precision, path recall, required-group recall, strict case pass rate, and forbidden-path rate. V2 changed both the benchmark and the production retrieval algorithm, then reran the same eight strict queries so the before/after values remain comparable.

## Files changed in V2

| File | Change |
|---|---|
| `backend/app/agent/retrieval.py` | stopword removal, morphology expansion, capped term frequency, concept coverage, stronger path scoring, artifact suppression, implementation/documentation intent filtering |
| `verification/model-quality-cases.json` | versioned V2 format, 12 cases, 27 expected citation groups, explicit default output constraints |
| `verification/scripts/model_quality_benchmark.py` | citation path/range overlap scoring and enforceable output checks |
| `verification/scripts/operational_scorecard.py` | operational retrieval category now reads the V2 artifact |
| `backend/tests/test_benchmarks.py` | tests for query normalization, citation spans, output limits, strict retrieval, and file deduplication |
| `backend/tests/test_agentic_rag_evaluation_manifest.py` | validates the V2 case count, citation-group count, ranges, and complete output contract |
| `benchmarks/results/retrieval-v2.json` | raw measured V2 retrieval output |
| `verification/results/operational-scorecard.json` and `.md` | regenerated operational result using V2 retrieval |
| `verification/results/coverage-report.md` | regenerated whole-backend readable coverage |
| `verification/results/core-feature-coverage.md` | regenerated core-feature coverage |
| `BENCHMARKS_AND_TESTS_V2.md` | standalone view retained for convenience; this cumulative section is authoritative with the earlier runs |

No runtime prompt was moved or deleted during this run. No earlier raw result was overwritten: `retrieval-latest.json` remains the strict baseline and `retrieval-v2.json` contains the new run.

## Expected citation contract added

The live quality source `verification/model-quality-cases.json` now contains **12 Flask tasks and 27 expected citation groups**. Each expected citation alternative declares a repository-relative path and an expected source interval:

```json
{
  "path": "src/flask/app.py",
  "start_line": 966,
  "end_line": 1060
}
```

A model citation satisfies an expected item only if its path matches and its submitted line interval overlaps the expected interval. A group may contain alternatives where more than one source location legitimately proves the same fact. This avoids assuming that only one file can be correct while still rejecting a citation to an irrelevant part of a correct file.

The expected citation groups cover:

1. repository overview;
2. session interfaces and open/save lifecycle;
3. WSGI request lifecycle;
4. blueprint definition and registration;
5. application/request context behavior;
6. exception handling and response finalization;
7. Jinja environment and template rendering;
8. CLI application discovery;
9. JSON provider customization;
10. request/template signals;
11. test-client context and session behavior;
12. configuration loading and instance-relative configuration.

These line ranges correspond to the pinned Flask checkout used by the benchmark. A different Flask revision must receive a new gold-set revision rather than silently reusing shifted line numbers.

### V2 citation metrics

The model scorer now records:

- `evidence_group_recall`: whether each required file/fact group is represented;
- `citation_precision`: supported cited paths divided by all distinct cited paths;
- `expected_citation_group_recall`: expected path/range groups satisfied;
- `unsupported_cited_paths`: cited paths outside all accepted alternatives;
- citation-span limit result;
- the complete output-check map.

The default minimum expected citation-group recall is 67%, and cases may override it. A correct filename with a non-overlapping line range does not receive expected-citation credit.

## Output enforcement added

The V2 evaluation defaults are explicitly stored at the top of `verification/model-quality-cases.json`:

| Constraint | V2 benchmark value | Enforcement |
|---|---:|---|
| Minimum answer length | 200 characters | answer fails if shorter |
| Maximum answer length | 8,000 characters | answer fails if longer |
| Minimum citation count | 2 | answer fails with fewer |
| Maximum citation count | 8 | answer fails with more |
| Maximum citation width | 160 source lines | any wider citation fails the task |
| Maximum reported output | 6,000 tokens | answer fails when provider-reported output exceeds it |

All checks participate in the final task result. They are not informational fields.

The application also retains broader runtime safety ceilings: 30,000 answer characters, 2,000 basis characters, 30 citations, five suggested questions, inspected-evidence containment, and a default provider request budget of 6,000 output tokens. The benchmark is intentionally stricter than the server ceiling: runtime limits prevent pathological responses, while benchmark limits evaluate usable answers.

### V2 model task pass rule

A live model task now passes only when all of the following hold:

1. provider returns an answer;
2. structured output passes schema validation;
3. answer is non-empty;
4. required-concept recall meets the per-case threshold;
5. required-evidence-group recall meets the per-case threshold;
6. citation precision meets the per-case threshold;
7. expected citation path/range group recall meets the per-case threshold;
8. every output constraint passes;
9. application validation status is `passed`.

This prevents passing through keyword repetition, a citation to the wrong portion of the right file, a whole-module citation spanning more than a thousand lines, or an oversized response.

## Production retrieval changes

### 1. Query stopword removal

High-frequency question words such as `how`, `where`, `what`, `and`, `through`, and `with` are removed from lexical scoring. Raw terms are retained separately for intent detection. Previously, a large unrelated file could receive a high score simply because it repeated common prose words.

### 2. Conservative morphology expansion

The retriever adds singular and conservative `-ed`/`-ing` variants. For example, `conversations` can match `conversation`, and `stored` can match `store`. This improves matches between natural-language questions and identifier/source vocabulary without introducing a model dependency.

### 3. Capped term frequency

Each informative term contributes at most three raw occurrences per chunk. This stops long files from winning solely through repetition.

### 4. Distinct-concept coverage

Scoring now rewards the fraction of distinct query concepts represented by a chunk. A chunk matching several parts of the question outranks one repeating a single term many times.

### 5. Stronger path relevance

Matches in file paths receive a stronger per-term bonus. Paths and filenames carry unusually strong meaning in code repositories—for example, `indexing.py`, `sessions.py`, and `repository_import.py`.

### 6. Evaluation-artifact suppression

Evaluation manifests and generated results are excluded unless the user is asking about benchmarks, coverage, evaluation, or reports. The previous run sometimes retrieved a benchmark definition because it repeated the exact benchmark question.

### 7. Implementation/documentation intent

Implementation queries prefer source/configuration evidence and exclude prose documentation unless the question explicitly requests documentation, a guide, README, tutorial, or overview. Documentation remains indexed and available; it is not globally removed.

### 8. Existing behavior preserved

V2 retains hybrid FTS/local-vector reciprocal-rank fusion, graph-neighborhood expansion, path/language/kind filters, test awareness, source safety checks, and distinct-file diversity. No benchmark-question-to-file lookup table was added.

## Retrieval results: preserved baseline versus V2

Baseline artifact: `benchmarks/results/retrieval-latest.json`  
V2 artifact: `benchmarks/results/retrieval-v2.json`

| Metric | Strict baseline | V2 | Difference |
|---|---:|---:|---:|
| Hit@1 | 25.00% | 75.00% | +50.00 points |
| Hit@3 | 100.00% | 87.50% | -12.50 points |
| Hit@5 | 100.00% | 100.00% | unchanged |
| Hit@10 | 100.00% | 100.00% | unchanged |
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
| **Strict retrieval score** | **42.86%** | **67.48%** | **+24.62 points** |
| Mean query latency | 3,277 ms | 2,690 ms | 587 ms faster |
| p95 query latency | 3,442 ms | 2,846 ms | 596 ms faster |
| Index construction | 49.10 s | 39.41 s | environment-sensitive |
| Persisted-index restore | 628 ms | 543 ms | environment-sensitive |
| Observed peak process memory | 194.01 MiB | 193.66 MiB | effectively unchanged |

### Exactly what changed and why the result improved

The benchmark questions, expected path groups, forbidden paths, top-10 result limit, and strict thresholds were kept unchanged between these two artifacts. The improvement therefore came from changes to `RepositoryRetrievalIndex.search` in `backend/app/agent/retrieval.py`, not from making the test easier.

| Retrieval change | Baseline problem | V2 behavior | Why it improved the measured result |
|---|---|---|---|
| Query stopword removal | Words such as `how`, `where`, `and`, `through`, and `with` occurred frequently in long unrelated files and added raw lexical score. | Common question words are removed from ranking terms while retained separately for intent detection. | Reduced false-positive lexical matches, helping precision, MAP, and nDCG. |
| Morphological query expansion | Natural-language forms such as `conversations`, `stored`, and `restored` did not always align with source identifiers such as `conversation`, `store`, and `restore`. | The query adds conservative singular and `-ed`/`-ing` variants. | Brought implementation files into the candidate set earlier, contributing to higher Hit@1, MRR, and required-group recall. |
| Per-term frequency cap | A large file could outrank a focused file by repeating one matching term many times. | Each informative term contributes at most three raw occurrences per chunk. | Prevented file length and repetition from dominating relevance, improving top-rank ordering. |
| Distinct-concept coverage bonus | A chunk repeating one word could beat a chunk that covered several independent parts of a multi-hop question. | Ranking rewards the proportion of distinct informative query terms present in the chunk. | Increased evidence breadth, reflected most clearly in required-group recall@10 rising from 67.71% to 87.50%. |
| Stronger path-name scoring | Filename matches were underweighted even though repository paths such as `indexing.py`, `sessions.py`, and `repository_import.py` are strong semantic evidence. | Every informative query term found in the repository-relative path receives a stronger bounded bonus. | Moved focused implementation files closer to rank one, raising Hit@1 from 25% to 75% and MRR from 58.33% to 84.38%. |
| Evaluation-artifact suppression | Benchmark manifests and generated reports repeated the exact benchmark language and could retrieve themselves. | Evaluation definitions and generated result directories are filtered unless the query explicitly asks about benchmarks, reports, coverage, or evaluation. | Reduced self-retrieval and known distractors, lowering the forbidden-path case rate from 37.5% to 25%. |
| Implementation-versus-documentation intent | Architecture reports and prose documentation could outrank source code for questions asking how a function, request, parser, index, clone, or validation process works. | Implementation-intent queries exclude documentation unless the question explicitly requests a README, overview, guide, tutorial, or documentation. | Increased source-code concentration in the first ten results, improving path recall@10 from 51.70% to 73.07% and nDCG@10 from 43.33% to 68.61%. |
| Existing distinct-file diversity retained | Multiple chunks from one highly relevant file could otherwise occupy most result slots. | The existing one-first-result-per-file diversification continues after V2 scoring. | Allowed the improved ranking signals to surface more independent files needed for multi-hop questions. This behavior was preserved rather than newly introduced. |

The largest causal improvement came from combining path relevance, distinct-concept coverage, and removal of common-word/file-length bias. Artifact and documentation filtering primarily reduced noise. Morphology primarily improved recall for natural-language-to-identifier mismatches.

The following changes made evaluation stricter but did **not** cause the retrieval score improvement:

- adding expected citation paths and source ranges;
- enforcing answer character limits;
- enforcing citation count and citation-width limits;
- enforcing the 6,000-token model-output limit;
- adding deterministic tests for those rules.

Those changes apply to the live model-answer benchmark, whereas the 42.86%-to-67.48% comparison measures repository retrieval only.

No benchmark-specific map from question to answer file was added, no expected paths were inserted into queries, and no threshold was lowered. V2 still fails because the remaining 47.5% precision@5, 87.5% required-group recall@10, 25% forbidden-path rate, and 3/8 strict pass rate do not meet the declared contract.

The V2 benchmark used one query repetition for prompt comparison. The release performance run should use at least three repetitions and confidence intervals.

Hit@3 decreased even while the more meaningful aggregate improved. This is retained rather than hidden: one or more queries moved their first useful file later within the first three, while Hit@1, evidence completeness, ranking gain, precision, strict passes, and latency improved.

## V2 failure analysis

V2 remains a **retrieval FAIL** because 67.48% is below the 70% category gate.

- Precision@5 is 47.5%, below the 60% per-case goal. Top-five bundles still contain plausible but nonessential files.
- Required-group recall@10 is 87.5%, below the strict 100% contract. Some multi-hop questions still omit one independent subsystem.
- Strict pass rate is 3/8. Five cases fail at least one of early rank, precision, complete group recall, or forbidden-path exclusion.
- Forbidden-path case rate is 25%. Two cases still surface a labeled distractor.
- nDCG@10 is 68.61%, showing that relevant evidence is not consistently ordered close enough to the top.
- Hit@10 remains 100%, but this proves only that every query found at least one relevant file—not that the bundle was complete or clean.

The thresholds were not lowered to manufacture a passing result.

## V2 tests and coverage

New deterministic tests verify:

- stopwords are absent from informative query terms;
- singular and stored/restored morphology variants exist;
- strict required-group, precision, and forbidden-path scoring;
- repeated chunks are deduplicated to unique files;
- expected citation paths and overlapping source ranges;
- non-overlapping expected citations fail;
- citations wider than the configured span fail;
- answer character, citation-count, citation-span, and reported-token constraints affect pass/fail;
- the live model manifest contains exactly 12 tasks and 27 expected citation groups;
- every expected range has `start_line <= end_line`;
- all six default output constraints exist with the intended values.

Measured test results after all V2 changes:

| Test/coverage result | Value |
|---|---:|
| Targeted retrieval + agent tests | 22/22 passed |
| Complete backend regression | 85/85 passed |
| Combined branch-aware backend coverage | 74.3% — PASS |
| Line coverage | 77.8% |
| Branch coverage | 61.2% |
| Core-feature weighted aggregate | 83.07% — PASS |
| Repository analysis/graph core | 84.22% |
| Revision-aware index/retrieval core | 83.28% |
| Grounded answer/validation/memory core | 79.66% |
| Adaptive onboarding core | 83.14% |
| Secure repository import core | 81.56% |

## Updated operational result

The operational scorecard now reads `benchmarks/results/retrieval-v2.json`.

| Category | V2 score | 70% gate | Status |
|---|---:|---:|---|
| Parsing accuracy | 100.0% | 70% | PASS |
| Retrieval quality | 67.5% | 70% | FAIL |
| Historical model-answer quality | 83.2% | 70% plus minimum-model gate | FAIL |
| Performance | 66.7% | 70% | FAIL |
| Frontend quality | 50.8% | 70% | FAIL |
| **Equal-weight aggregate** | **73.6%** | **70%** | **FAIL** |

The aggregate exceeds 70%, but the release remains failed because every category and the weakest compared model must independently pass. A strong parser cannot compensate for weak retrieval, frontend coverage, performance, or provider reliability.

## Live model execution status

The 12-task V2 model suite was structurally validated but was **not rerun against paid/live providers during this change**. It would spend configured provider quota. Therefore:

- expected citations: implemented and deterministically tested;
- output enforcement: implemented and deterministically tested;
- historical four-task Claude/OpenRouter results: preserved above;
- live 12-task V2 Claude/OpenRouter measurements: pending explicit quota-enabled execution;
- OpenRouter/OpenRouter paired V2 measurements: pending.

No V2 model-quality percentage should be inferred until the live run exists.

## Prompt-location inventory at V2

Waypoint still does not have one centralized prompt registry.

| Prompt or prompt-like contract | Runtime location |
|---|---|
| Ask repository-agent system instructions | `backend/app/agent/service.py`, `SYSTEM_PROMPT` |
| Ask tool descriptions/input schemas | `backend/app/agent/service.py`, `TOOLS` |
| Issue investigation instructions | `backend/app/agent/issues.py`, `FINDING_PROMPT` |
| Onboarding route planner | `backend/app/agent/onboarding.py`, `TOUR_SYSTEM_PROMPT` |
| Contribution mission planner | `backend/app/agent/onboarding.py`, `MISSION_SYSTEM_PROMPT` |
| Free-text onboarding challenge grader | inline prompt in `backend/app/agent/onboarding.py` near the assessment call |
| Claude Agent SDK action-selection wrapper | `backend/app/agent/provider.py` |
| Frozen comparison output contract | `backend/app/agent/comparison.py`, `SUBMIT_COMPARISON_TOOL` and comparison request construction |
| MCP explanation prompt | `backend/app/mcp_server.py`, `explain_repository` |
| Deterministic onboarding challenge text | `backend/app/onboarding/service.py` |
| Ask UI starter questions | `frontend/src/components/AskWorkspace.tsx` |
| Human interaction/audit log | root `prompts.md` |
| Strict retrieval questions | `benchmarks/retrieval_cases.json` |
| Live model questions/citations/output contract | `verification/model-quality-cases.json` |
| Full 50-case capability specification | `verification/agentic-rag-evaluation-cases.json` |

`prompts.md` is an audit log, not a runtime prompt source. Editing it does not alter agent behavior.

## Recommended next retrieval run

V3 should preserve both existing artifacts and add a new result rather than overwriting them.

1. Decompose multi-hop questions into independent subqueries and fuse their candidates.
2. Rerank the top 30 hybrid candidates with a calibrated reranker and retain a deterministic fallback.
3. Allocate evidence slots by intent layer: implementation, configuration, tests, documentation, frontend, and history.
4. Diversify by graph neighborhood and subsystem, not only by unique file path.
5. Calibrate BM25/IDF and fixed bonuses on a development split, never the held-out release set.
6. Expand deterministic retrieval to at least 50 queries across Waypoint, Flask, and PRAW.
7. Separate development labels from a held-out release set.
8. Human-adjudicate disputed acceptable/forbidden paths without seeing model identity or rank.
9. Report Python, JavaScript/TypeScript, Java, markup/configuration, and documentation slices independently.
10. Add retrieval confidence and another retrieval round—or an explicit insufficiency statement—when required evidence is incomplete.

## V2 reproduction commands

```powershell
# Reproduce strict V2 retrieval without overwriting the baseline
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py `
  --repository . `
  --cases benchmarks\retrieval_cases.json `
  --output benchmarks\results\retrieval-v2.json `
  --repeats 3

# Complete deterministic regression and coverage
.\.venv\Scripts\python.exe -m pytest -q

# Regenerate the operational scorecard from V2 retrieval
.\.venv\Scripts\python.exe verification\scripts\operational_scorecard.py

# Live strict V2 model run; spends configured provider quota
.\.venv\Scripts\python.exe verification\scripts\model_quality_benchmark.py `
  --analysis-id <PINNED_FLASK_ANALYSIS_ID> `
  --cases verification\model-quality-cases.json `
  --confirm-live-model-usage `
  --label strict-v2
```

## Cumulative artifact authority after V2

- Cumulative human report: `WAYPOINT_BENCHMARKS_AND_TESTS.md` (this file)
- Convenience V2-only report: `BENCHMARKS_AND_TESTS_V2.md`
- Strict baseline raw retrieval: `benchmarks/results/retrieval-latest.json`
- Strict V2 raw retrieval: `benchmarks/results/retrieval-v2.json`
- Live V2 specification: `verification/model-quality-cases.json`
- V2 scorer: `verification/scripts/model_quality_benchmark.py`
- Operational V2 score: `verification/results/operational-scorecard.json`
- Coverage: `.waypoint-data/coverage.xml` plus the two readable coverage reports

Future runs must append another dated run-history section here and retain earlier raw artifacts under unique versioned filenames.

---

# V3 run history — hybrid memory, multi-hop retrieval, and 50-case diagnostic

V3 preserves the baseline and V2 artifacts above. Detailed convenience report:
`BENCHMARKS_AND_TESTS_V3.md`. Raw artifacts:
`benchmarks/results/retrieval-v3-eight.json` and
`benchmarks/results/retrieval-v3.json`.

## What V3 changed

The production retriever now decomposes multi-hop questions into at most six lexical
clauses, fuses full-query and clause FTS ranks, classifies requested evidence layers,
performs deterministic file-level MMR across source layers/packages/graph neighborhoods,
and attaches confidence, term-coverage, layer-coverage, and follow-up fields to results.
Documentation intent includes skills, rules, knowledge, worldview, tutorials, guides, and
workflows. Generated V3 manifests are suppressed unless the question is about evaluation.

Repository vectors are deserialized once per immutable revision and cached in compact
unsigned-short/float arrays. The cache is bounded to three revisions. This reduced the
unchanged eight-case mean query latency from V2's 2,689.96 ms to 1,203.18 ms while adding
only 3.91 MiB to that run's observed peak. The eight-sample p95 rose to 3,031.22 ms because
the first query pays cache construction; this regression is not hidden.

The agent now uses hybrid conversation memory. Each question/answer turn is retained in
SQLite with a local 768-dimension sparse subword vector and repository revision. The next
prompt combines the most recent 12 turns with up to six relevant older turns. Older memory
score is 85% cosine similarity, 10% logarithmic recency, and 5% exact-revision bonus,
subject to an 8,000-character semantic budget, 0.12 similarity floor, 200-turn retention,
and strict conversation/analysis/channel isolation. These limits are environment
configurable and memory-selection diagnostics are logged.

The local vectorizer is deterministic, offline, private, and free. It is not a trained
neural embedding model, so “semantic” memory here means fuzzy subword-vector similarity,
not deep paraphrase understanding.

## Preserved V2 versus V3 fixed benchmark

The eight questions and their labels were unchanged.

| Metric | V2 | V3 | Difference |
|---|---:|---:|---:|
| Precision@5 | 47.50% | 57.50% | +10.00 points |
| Path recall@10 | 73.07% | 74.11% | +1.04 points |
| Required-group recall@10 | 87.50% | 92.71% | +5.21 points |
| MAP@10 | 51.93% | 56.22% | +4.29 points |
| MRR | 84.38% | 85.42% | +1.04 points |
| nDCG@10 | 68.61% | 71.35% | +2.74 points |
| Forbidden-path case rate | 25.00% | 12.50% | -12.50 points (better) |
| Strict case pass | 37.50% | 50.00% | +12.50 points |
| **Strict retrieval score** | **67.48%** | **73.87%** | **+6.39 points** |
| Mean query latency | 2,689.96 ms | 1,203.18 ms | 1,486.78 ms faster |

The fixed V3 score passes the 70% composite retrieval gate, but only four of eight cases
pass every strict per-case requirement. The aggregate does not erase those failures.

## New 50-case, three-repository diagnostic

`benchmarks/retrieval_cases_v3.json` contains 50 unique questions: 20 Waypoint, 15 Flask,
and 15 PRAW; 30 are development cases and 20 are nominal held-out cases. It includes
Python, JavaScript, TypeScript, Java, HTML, shell, configuration, and documentation
slices. Each case records required path groups, acceptable alternatives, forbidden paths,
and adjudication status.

| Metric | V3 diagnostic |
|---|---:|
| Hit@1 / Hit@3 / Hit@10 | 84.00% / 98.00% / 100.00% |
| Precision@5 | 45.60% |
| Required-group recall@10 | 85.33% |
| MAP@10 | 51.63% |
| MRR | 90.07% |
| nDCG@10 | 66.41% |
| Forbidden-path case rate | 10.00% |
| Strict case pass | 52.00% |
| **Strict retrieval score** | **72.57%** |

| Slice | Cases | Strict score | Strict cases passing |
|---|---:|---:|---:|
| Waypoint | 20 | 71.23% | 45.00% |
| Flask | 15 | 75.78% | 60.00% |
| PRAW | 15 | 71.16% | 53.33% |
| Development | 30 | 74.21% | 60.00% |
| Nominal held-out | 20 | 70.12% | 40.00% |

This result is explicitly **not release-gate eligible**. All seed labels are
`pending_human`, the runner rejects them without `--allow-unadjudicated`, and aggregate
results were inspected while V3 was developed. The nominal held-out cases are therefore
not a blind final test. Java and configuration each have one case, which is useful as a
smoke slice but insufficient for a language-wide claim.

## V3 test and coverage evidence

| Evidence | Result |
|---|---:|
| Focused memory/retrieval/manifest suite | 27/27 passed |
| Complete backend regression | 90/90 passed |
| Combined branch-aware backend coverage | 74.80% — PASS |
| Weighted core aggregate | 83.40% — PASS |
| Revision-aware retrieval core | 83.93% — PASS |
| Grounded answer/validation/memory core | 81.09% — PASS |

Tests cover older-turn vector recovery, exclusion of recent-turn duplicates, retrieval
confidence fields, decomposition/intent helpers, vector-cache reuse, 50-case manifest
shape, repository/split/language aggregation, and rejection of unadjudicated release runs.

## V3 recommendation status

| Prior recommendation | V3 status |
|---|---|
| Multi-hop query decomposition | Implemented |
| Intent-aware evidence layers | Implemented |
| Graph/subsystem diversity | Implemented |
| At least 50 questions over Waypoint/Flask/PRAW | Implemented |
| Development/held-out reporting | Implemented, but blind status consumed during development |
| Per-language reporting | Implemented; Java/HTML/config sample sizes remain too small |
| Confidence plus follow-up/abstention | Implemented in metadata and agent instructions |
| Human label adjudication | Pending; cannot be self-certified by the implementation agent |
| Learned/calibrated reranker | Not implemented; deterministic MMR remains the fallback |
| Fully calibrated fusion weights | Not implemented; FTS uses BM25 but several fusion weights remain fixed |

These outstanding items are governance/modeling work, not silently reported as complete.
The live Claude/OpenRouter and OpenRouter/OpenRouter answer benchmarks were not run during
V3; the retrieval benchmark is model-free and spends no provider quota.

## V3 reproduction commands

```powershell
.\.venv\Scripts\python.exe benchmarks\run_retrieval.py `
  --repository . `
  --cases benchmarks\retrieval_cases.json `
  --output benchmarks\results\retrieval-v3-eight.json `
  --repeats 1

.\.venv\Scripts\python.exe benchmarks\run_retrieval_v3.py `
  --repository waypoint=. `
  --repository flask=.waypoint-clones/pallets--flask--214023150b `
  --repository praw=.waypoint-clones/haxtheweb--praw--69db9269ab `
  --allow-unadjudicated `
  --repeats 1 `
  --output benchmarks\results\retrieval-v3.json

.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m verification.scripts.core_coverage_report
.\.venv\Scripts\python.exe -m verification.scripts.coverage_report
```

# V4 run history — typed tool contracts and independent model retrieval

V4 preserves every baseline, V2, and V3 section above. The complete protocol and
reproduction guide is in `BENCHMARKS_AND_TESTS_V4.md`; its protocol manifest is
`benchmarks/retrieval_cases_v4.json` and its live runner is
`benchmarks/run_agentic_retrieval_v4.py`.

## What V4 changed

- All 17 internal repository tools now derive provider schemas from strict Pydantic
  input contracts and validate server results through Pydantic output contracts.
- Ask, onboarding tours, and contribution missions return structured correction
  envelopes with independent, bounded schema/submission correction budgets.
- The Claude Agent SDK adapter normalizes contradictory success/error terminal results.
- A second comparison mode lets both configured models independently select and call
  repository tools. Frozen-evidence comparison remains available for synthesis isolation.
- Candidate results, explicit `read_source` inspection, and final cited files are scored
  separately. Tool calls, invalid calls, rounds, tokens, cost, TTFT status, and duration
  are retained per endpoint.
- The comparison is available through HTTP, the React Ask UI, and the MCP server.

## V4 verification recorded so far

| Evidence | Result |
|---|---:|
| Complete backend suite | 111/111 passed |
| Whole-backend branch-aware coverage | 74.94% — PASS |
| Weighted core aggregate including V4 | 82.91% — PASS (all 5 core features individually PASS) |
| Frontend production build | PASS |
| Frontend Vitest unit suite | 3/3 passed |
| Frontend instrumented code coverage | 1.62% overall — FAIL; major React workspaces remain unexercised |
| Frontend Playwright application E2E | executed workflow passed; live Voicebox acceptance workflow skipped without its external environment |
| Fixed Poolside independent-agent comparison | BLOCKED (superseded) — both selected endpoints returned non-transient OpenRouter `free-models-per-day` 429; retained as infrastructure evidence only |
| Claude Code (`claude-code:sonnet`/`opus`) independent-agent smoke test | BLOCKED (superseded) — 47/48 attempts failed with contradictory Claude Agent SDK success/error protocol messages, most plausibly from running the SDK inside the same interactive session driving the benchmark |
| **Bedrock independent-agent campaign (Claude Sonnet vs GPT-OSS-120B)** | **COMPLETE — 50/50 cases run to completion, 100 attempted model runs** |
| Release-gate label status | Not eligible — 50 standalone V4 cases await human review, independent of the completed Bedrock results below |

### Bedrock campaign result: Claude Sonnet vs GPT-OSS-120B

Fixed endpoints `bedrock:us.anthropic.claude-sonnet-4-6` and
`bedrock:openai.gpt-oss-120b-1:0`, driven through one `BedrockConverseMessages` adapter
using AWS Bedrock's Converse API — the one request/response format shared across every
model family Bedrock hosts, chosen specifically because it can drive both an Anthropic
model and a non-Anthropic model through the identical tool-calling contract. Full detail,
per-stage retrieval tables, and failure analysis are in `BENCHMARKS_AND_TESTS_V4.md`;
summary:

| Measurement | Claude Sonnet | GPT-OSS-120B |
|---|---:|---:|
| Successful cases | 49/50 (98%) | 1/50 (2%) |
| Mean tokens/case | 218,179 | 42,651 (n=1) |
| Mean expected-concept recall | 72.9% | 0.0% (n=1) |
| Cited-retrieval Hit@10 | 100% | 100% (n=1) |

Claude Sonnet's one failure was AWS infrastructure (`ThrottlingException`, daily Bedrock
token quota reached), not a model or retrieval defect. GPT-OSS-120B's 49 failures were
overwhelmingly `"Repository synthesis exceeded 2 attempts"` — its one success scored
perfectly on retrieval metrics, indicating the bottleneck is output/citation discipline
on the terminal submission contract, not its ability to find relevant files. This is a
decisive, completed result, not a diagnostic: retained in
`benchmarks/results/agentic-v4-bedrock-claude-vs-gptoss.{json,md}` and hook companions.

The two earlier blocked attempts (OpenRouter Poolside quota exhaustion; Claude Code
session-nesting protocol failures) remain retained as infrastructure evidence in
`benchmarks/results/agentic-v4-poolside-diagnostic-4.{json,md}` and are described in
`BENCHMARKS_AND_TESTS_V4.md`, which also records the standalone corpus, fixed endpoints,
synthesis enforcement fix, retry policy, observability measurements, and exact rerun
commands for all three attempts.
