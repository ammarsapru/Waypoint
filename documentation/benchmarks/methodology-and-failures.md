> **Methodology and root-cause analysis below are still the reference for how
> Waypoint's scorecard formulas work and why each category failed at the time.** The
> specific scores (retrieval 58.76%, etc.) are a 2026-07-26 snapshot predating the V2-V4
> retrieval improvements documented in [`history.md`](history.md); see there for how
> the retrieval score moved (58.76% → 67.48% → 73.87%) and
> [`test-suite-and-coverage.md`](test-suite-and-coverage.md) for current test counts.

# Waypoint Benchmark Methodology and Failure Analysis

## Release rule

The 70% threshold is a declared Waypoint release policy, not an industry-standard formula.
A run passes only when:

1. all required artifacts exist;
2. the equal-weight average of the five categories is at least 70%; and
3. every individual category passes its own 70% gate.

For model quality, every provider/model endpoint must additionally achieve at least 70%
task success. This is why the 83.16% model composite still fails: OpenRouter task success
was 50%.

The overall calculation was:

```text
(100.00 + 58.76 + 83.16 + 66.67 + 50.78) / 5 = 71.87%
```

The overall average clears 70%, but four category pass flags are false.

## Category formulas and pass criteria

### Parsing accuracy

```text
score = mean(parse success rate, node F1, relationship F1)
      = mean(1.0, 1.0, 1.0)
      = 100%
```

Pass criterion: category score at least 70%.

The benchmark creates a temporary, hand-audited nine-file fixture. It does not use an
external repository. Languages are Python, JavaScript, TypeScript, TSX, Java, HTML, and
CSS. The gold set contains 16 semantic nodes and nine cross-file relationships:

- four imports;
- two class instantiations;
- three inferred `may_call` relationships.

Result: all nine files parsed, all 16 expected nodes matched with no extra gold-scope
nodes, and all nine relationships matched. The 100% applies only to this controlled
fixture, not every possible real repository.

Gold fixture and expected sets are stored directly in
`benchmarks/run_parsing_accuracy.py`. Results are in
`benchmarks/results/parsing-accuracy.json`.

### Deterministic retrieval quality

```text
score = mean(Hit@5, Hit@10, MRR, nDCG@10)
      = mean(0.625, 0.875, 0.3774, 0.4730)
      = 58.76%
```

Pass criterion: category score at least 70%.

Definitions:

- Hit@5: at least one expected file occurs in the first five results.
- Hit@10: at least one expected file occurs in the first ten results.
- MRR: mean reciprocal rank of the first expected file; a miss contributes zero.
- nDCG@10: rewards retrieving all expected files and placing them near the top.

The eight questions were tested against **Waypoint's own repository**, rooted at the
current project directory. They are stored in `benchmarks/retrieval_cases.json`.

| # | Gold question | Expected file(s) | First correct rank | Outcome |
|---:|---|---|---:|---|
| 1 | Where is SQLite conversation memory stored and restored? | `backend/app/agent/memory.py` | 8 | Hit@10 only |
| 2 | How are GitHub repositories cloned into secure local storage? | `backend/app/repository_import.py` | 4 | Hit@5 and Hit@10 |
| 3 | How are TypeScript JavaScript and Java syntax trees parsed? | `backend/app/graph/polyglot.py` | 1 | Strong pass |
| 4 | Which API endpoint starts repository analysis? | `backend/app/api/routes.py` | 9 | Hit@10 only |
| 5 | How are code graph nodes imports and call edges generated? | `backend/app/graph/analyzer.py`, `backend/app/graph/polyglot.py` | 1 | Strong pass; second expected file rank 5 |
| 6 | Where does the repository agent execute precise tool calls? | `backend/app/agent/service.py` | 3 | Hit@5 and Hit@10 |
| 7 | How does the source index rank and retrieve code chunks? | `backend/app/agent/retrieval.py` | 5 | Barely Hit@5 |
| 8 | How are repository architecture and entry points inferred? | `backend/app/agent/semantic.py` | Not in top 10 | Complete miss |

Detailed failure causes:

- Broad files such as `frontend/src/App.tsx`, `AI_SYSTEM_ARCHITECTURE_REPORT.md`,
  `README.md`, and planning documents frequently outranked focused implementation files.
- Exact implementation questions were diluted by test files and generated explanatory
  documents containing the same vocabulary.
- `memory.py` was found only at rank 8 and `routes.py` at rank 9, so retrieval technically
  found them but would waste most of a small evidence budget.
- `semantic.py` did not appear in the first ten at all for the architecture-inference
  question. The retriever instead returned architecture reports, frontend code, parser
  code, tests, README content, onboarding, import, and retrieval modules.
- The benchmark is only eight questions from one repository, so its confidence interval
  is poor. It is a useful regression fixture, not a broad claim about all repositories.

### Model-answer quality

For each endpoint:

```text
endpoint task success = passed gold tasks / 4
```

One task passes only if it has:

- a non-empty answer;
- at least one citation;
- structured-output validation status `passed`;
- at least 60% required-concept recall; and
- at least one expected file cited.

The category score is:

```text
mean(
  mean endpoint task success,
  mean endpoint concept recall,
  mean endpoint expected-file recall
)
= mean(0.75, 0.9322, 0.8125)
= 83.16%
```

It then applies a reliability gate:

```text
minimum endpoint task success = min(Claude 1.0, OpenRouter 0.5) = 0.5
0.5 < 0.7, therefore FAIL
```

The four tasks were tested against a locally analyzed checkout of
**`pallets/flask`**, analysis ID `db40e1f66b094d829629a4e2fdf42d02`. The questions and
gold expectations are stored in `verification/model-quality-cases.json`.

| Task | Question | Expected paths | Required concepts |
|---|---|---|---|
| Repository overview | What is this repository about? Highlight its top 10 features. | `src/flask/app.py`, `src/flask/sansio/app.py` | web framework, WSGI, routing, templates/Jinja, sessions, blueprints, request/application context |
| Sessions | How does Flask's session architecture work and where can it be replaced? | `src/flask/sessions.py` | SessionInterface, open_session, save_session, secure/signed cookie, replacement/customization |
| Request lifecycle | Trace the request lifecycle from the WSGI entry point to a view response. | `src/flask/app.py`, `src/flask/ctx.py` | wsgi_app, request context, dispatch_request, finalize_request, response |
| Blueprints | What are blueprints, and how are their routes connected to an application? | `src/flask/sansio/blueprints.py`, `src/flask/blueprints.py` | collection of routes, registration, deferred/recorded work, application |

The candidate retriever found all expected files for the first three questions. For the
blueprint question it found only `src/flask/sansio/blueprints.py`, giving 50% candidate
evidence recall before either model answered.

#### Claude Code Sonnet results

| Task | Passed? | Concept recall | Expected-path recall | Expected-path precision | Explanation |
|---|---|---:|---:|---:|---|
| Overview | Yes | 85.71% | 100% | 20% | Found both expected files, but cited ten files; one concept group was missed |
| Sessions | Yes | 100% | 100% | 50% | Cited the expected sessions file plus `ctx.py` |
| Request lifecycle | Yes | 100% | 100% | 25% | Cited both expected files, but eight files in total |
| Blueprints | Yes | 100% | 50% | 33.33% | The retriever did not provide `src/flask/blueprints.py`; cited three files including the one available expected file |

Claude passed all four tasks, but citation precision was low. The scoring currently
reports expected-path precision but does not include it in the pass formula. This should
be reconsidered because citing every available file can obtain perfect recall without
being selective.

#### OpenRouter free-router results

| Task | Passed? | Detail |
|---|---|---|
| Overview | No | Produced nine suggested questions; schema permits at most five. The answer was rejected rather than silently truncated. |
| Sessions | Yes | 80% concept recall, 100% expected-path recall, 33.33% expected-path precision. |
| Request lifecycle | No | Cited `src/flask/ctx.py:L260-L698` and `src/flask/views.py:L1-L367`, which were outside the exact frozen evidence spans. The answer failed grounding validation. |
| Blueprints | Yes | 100% concept recall, 50% expected-path recall, 100% precision; only the available expected blueprint file was cited. |

OpenRouter therefore passed 2/4, or 50%. The free router can select different underlying
models between runs, so this endpoint is not a reproducible fixed-model benchmark. A
production comparison should pin a model and provider route.

### Performance

```text
score = mean(
  HTTP request success rate,
  backend load-SLO pass as 0 or 1,
  frontend Web-Vitals pass as 0 or 1
)
= mean(1.0, 0.0, 1.0)
= 66.67%
```

Pass criterion: score at least 70%.

The HTTP/load portion used the saved **`haxtheweb/praw`** analysis session,
ID `21507751dd9a4933b6c209a3da334085`, plus the global health route. Six endpoints were
requested at concurrency 1, 4, and 8, with 30 requests per level.

The backend SLO passes only if every load level has:

- at least 99% request success; and
- p95 latency no greater than 2,000 ms.

All 90 requests succeeded. Concurrency 8 failed the latency requirement with p95
3,013.291 ms. That turns the entire SLO component into zero.

The Web-Vitals component used the Waypoint frontend development-server landing page. It
passes if LCP is at most 2,500 ms and CLS is at most 0.1. LCP was 520 ms and CLS 0.0037,
so it passed. This is not a heavy loaded-repository Web-Vitals test, and INP was not
measured.

The performance score is intentionally simple but coarse: a p95 miss of one millisecond
and a miss of several seconds both produce zero for the SLO component. Raw latency,
throughput, cache, RSS, browser heap, and long-task metrics remain the authoritative
diagnostics.

### Frontend quality

```text
score = mean(executed Playwright success rate, V8 line coverage)
      = mean(1.0, 0.0155)
      = 50.78%
```

Pass criterion: score at least 70%.

One executed Playwright workflow passed. It analyzes `backend/tests`, exercises Explore,
file cards, inspector usage, collapsible panels, source resizing, fullscreen, Ask,
Onboarding, Issues, theme persistence, and console-error checks. However:

- the GitHub Flask request is intercepted and fulfilled with the already obtained local
  analysis response;
- Ask, onboarding tour, mission, and challenge responses are mocked;
- the Voicebox acceptance test was skipped because `WAYPOINT_VOICEBOX_PATH` was absent;
- only three frontend unit tests exist; and
- instrumented line coverage is 1.55%.

Consequently, the 100% executed-E2E figure represents one successful UI contract flow,
not comprehensive live frontend-to-model reliability.

## Why each failing category failed

| Category | Immediate reason | Underlying weakness |
|---|---|---|
| Retrieval | Composite 58.76% | Correct implementation files rank too low; one total top-10 miss; tiny self-repository dataset |
| Model quality | OpenRouter endpoint success 50% | One cardinality/schema violation and one citation-grounding violation; free router is not model-pinned |
| Performance | Load SLO component zero | Concurrency-8 p95 was 3.013 seconds versus a 2-second limit |
| Frontend | 1.55% line coverage | Only three unit tests and one executed E2E flow; major components uninstrumented by tests |

## Important interpretation

The current scorecard is useful because its formulas are explicit and reproducible, but
it is not yet statistically strong enough for a production claim. The retrieval dataset
needs multiple repositories and substantially more questions; parser gold fixtures need
real-world corpora; model accuracy needs human factual labels; frontend E2E needs live
backend/model paths; and performance needs production builds, repeated runs, and soak
testing.

