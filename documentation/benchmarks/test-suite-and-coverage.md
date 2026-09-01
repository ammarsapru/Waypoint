# Waypoint test suite and coverage methodology

Date: 2026-07-28

This document describes what Waypoint's test suite actually verifies today — broken
down as unit, integration, and end-to-end tests — and how test coverage is measured and
gated. Numbers below come from a live run of the full suite on this date (111 backend
tests, 74.94% whole-backend coverage, 82.91% weighted core-feature coverage), not from
memory or an earlier report.

## Suite totals

| Layer | Count | Tooling |
|---|---:|---|
| Backend unit tests | 69 | `pytest` (`unittest.TestCase` style) |
| Backend integration tests | 42 | `pytest` |
| Frontend unit tests | 3 | Vitest |
| Frontend end-to-end tests | 2 | Playwright |
| **Total** | **116** | |

Backend unit/integration classification is not a loose convention — it is enforced by
`backend/tests/conftest.py`'s `pytest_collection_modifyitems` hook, which maps every test
*module* (file) to `unit` or `integration` from two explicit sets and **raises
`pytest.UsageError` for any test file not in either set**. A new test file that isn't
classified fails the whole run immediately rather than silently landing in neither
bucket.

## Backend unit tests (69) — isolated, deterministic, one service or algorithm

| File | Tests | What it verifies |
|---|---:|---|
| `test_analyzer.py` | 10 | Python `ast`-based graph construction: modules/classes/functions/imports/calls, dynamic-call handling, cross-file receiver resolution, syntax-error diagnostics (non-fatal), source spans, src-layout import naming. |
| `test_polyglot_analyzer.py` | 13 | Tree-sitter extraction for JS/TS/TSX/Java: shared graph schema across languages, CommonJS `require` resolution, external package normalization, cross-file method resolution, Java package/class/method/import/instantiation edges. |
| `test_provider.py` | 19 | The model-provider abstraction: OpenRouter/Bedrock/Gemini request-response translation and tool-schema portability, billing/capacity error classification, the Claude Code runtime fallback, retry/backoff behavior, dual-role routing. |
| `test_tool_contracts.py` | 4 | Strict Pydantic input/output contracts for repository tools: rejecting type coercion and unknown fields, rejecting inverted line ranges, detecting a malformed server-side result, and the machine-readable correction envelope shape. |
| `test_observability.py` | 2 | The structured logging/tracing hook system: correlated sanitized fields on emitted events, and pre/post/error hook emission around a traced function. |
| `test_issues.py` | 3 | GitHub issue workspace mechanics: repository identity via the managed-clone marker, filtering pull requests out of issue results, restoring AI-generated findings from SQLite. |
| `test_questions.py` | 3 | Deterministic (non-agentic) repository question answering: overview/feature summaries, indexed documentation as evidence, symbol-grounded location questions. |
| `test_repository_import.py` | 5 | Secure GitHub import policy: only canonical public repository URLs accepted, shallow non-interactive clones stay inside the secure clone root, oversized checkouts are removed, old managed clones are retained/evicted correctly. |
| `test_benchmarks.py` | 8 | The V3 deterministic retrieval-benchmark harness itself: manifest versioning/balance/release-approval gating, unadjudicated-label rejection, query decomposition and morphology expansion, per-case scoring (required groups, precision, forbidden paths), file-ranking deduplication. |
| `test_agentic_rag_evaluation_manifest.py` | 2 | The V4 case manifest's structural integrity: well-formed and complete, and that live-model cases define citation/output-enforcement fields. |

## Backend integration tests (42) — deterministic, crossing component boundaries

| File | Tests | What it verifies |
|---|---:|---|
| `test_agent.py` | 20 | The largest integration suite: chunk identity across the retrieval index, repository snapshotting including non-AST text context, semantic-tool caching per revision, hybrid lexical+vector search and its morphology handling, hybrid conversation memory recall outside the recent-turn window, and restart-safe analysis-session persistence (including the new lightweight summary-listing path). |
| `test_api.py` | 6 | The FastAPI HTTP layer: the UI/API contract stays registered, health endpoint diagnostics, the agentic-comparison HTTP contract, path-traversal rejection outside the allowed root, GitHub analysis validating before cloning, and session summary/neighborhood endpoints. |
| `test_onboarding.py` | 8 | The full onboarding agent: 2-hour time budgets, role-specific tours actually differing and staying source-grounded, mastery scoring on challenges, SQLite-restored tour state, architecture/mission/search grounding, citation-and-refusal behavior on grounded answers, bounded source-grounded code journeys. |
| `test_semantic_tools.py` | 2 | The deterministic semantic-tool layer (`SemanticRepositoryTools`) end to end: overview/features/configuration/entry-points, and architecture/file/symbol/test-impact/diagnostics tools together. |
| `test_mcp_server.py` | 2 | The MCP server surface: it exposes only safe, intended repository capabilities, and the GitHub tool passes a validated repository name through to analysis. |
| `test_model_comparison.py` | 3 | Frozen-evidence model comparison: both models see identical evidence for the same question, single-model-mode requests are rejected, one provider's failure doesn't take down the whole comparison. |
| `test_agentic_comparison.py` | 1 | The V4 independent-agent harness itself: two models retrieving independently and being scored per stage (candidate/inspection/cited), not from one shared evidence bundle. |

## Frontend tests

**Unit (Vitest, 3 tests, `src/api.test.ts`):** covers `formatApiErrorDetail` — keeping
plain server errors human-readable, formatting FastAPI validation error arrays instead of
naively coercing them to a string, and serializing structured error details with a status
fallback. Run via `npm run test:unit`; `npm run test:coverage` adds V8 instrumentation
(`vitest.config.mjs`) over `src/**/*.{ts,tsx}`, excluding test files themselves.

**End-to-end (Playwright, 2 specs, `frontend/e2e/`):**
- `application.spec.ts` — analyzes a repository, navigates the resulting evidence, and
  confirms both light/dark themes persist across the session.
- `voicebox.acceptance.spec.ts` — a live external-environment acceptance check that the
  Voicebox overview returns ten features with inspectable evidence; skipped when that
  external environment isn't available.

Run via `npm run test:e2e` (Playwright), configured in `frontend/playwright.config.ts`.

## How coverage is measured and gated

Coverage is enforced at **two independent levels** for the backend, plus a tracked (not
yet gated) level for the frontend.

### 1. Whole-backend gate — `pytest-cov` / `coverage.py`

`pyproject.toml`'s `[tool.pytest.ini_options]` runs every test with:

```text
--cov=backend.app --cov-report=term-missing
--cov-report=xml:.waypoint-data/coverage.xml
--cov-report=html:.waypoint-data/coverage-html
--cov-fail-under=70
```

`[tool.coverage.run]` sets `branch = true` (branch outcomes count, not just line hits) and
excludes `backend/tests/*` from the measured source. The whole suite must fail if
combined line+branch coverage across all of `backend.app` drops under 70% — this is a
blanket floor across every module, framework glue included.

### 2. Weighted core-feature gate — `verification/core-features.json` + `core_coverage_report.py`

A blanket backend percentage can hide a well-tested-on-average codebase with a badly
undertested core. Waypoint additionally maintains an explicit, reviewable manifest of
five **core features**, each mapped to its actual modules, each independently required to
hit 70% combined line+branch coverage — not just the aggregate:

| Core feature | Modules | Combined coverage (current) |
|---|---|---:|
| Repository analysis and graph construction | `graph/models.py`, `graph/analyzer.py`, `graph/polyglot.py` | 84.22% |
| Revision-aware indexing and graph retrieval | `indexing.py`, `graph/store.py`, `agent/retrieval.py`, `agent/semantic.py` | 82.48% |
| Grounded agent answering, validation, and memory | `agent/memory.py`, `agent/service.py`, `agent/comparison.py`, `agent/agentic_comparison.py`, `agent/tool_contracts.py` | 84.45% |
| Adaptive onboarding routes and missions | `agent/onboarding.py`, `onboarding/models.py`, `onboarding/questions.py`, `onboarding/service.py` | 80.96% |
| Secure repository import | `repository_import.py` | 81.56% |
| **Weighted core aggregate** | all of the above, summed | **82.91%** |

Combined coverage is `(covered lines + covered branch outcomes) / (total lines + total
branch outcomes) × 100`, computed straight from the same Cobertura XML the pytest run
already produced (`verification/scripts/core_coverage_report.py --xml
.waypoint-data/coverage.xml --manifest verification/core-features.json`).

Five **supporting subsystems** (provider/SDK adapters, HTTP/MCP delivery adapters,
subprocess execution infrastructure, the Issues secondary feature, observability) are
explicitly named and reported but deliberately **excluded from the core denominator** —
each with a stated reason (vendor transport, framework wiring, generic infrastructure,
secondary feature, cross-cutting instrumentation). They are not hidden: they still count
toward the whole-backend gate above, so moving code into "supporting" can't be used to
dodge the 70% floor — it only changes which of the two gates is doing the enforcing.

The core gate is additive to, not a replacement for, the whole-backend gate. A release
is only considered passing when **both** are satisfied.

### 3. Frontend coverage — tracked, not yet gated

`vitest.config.mjs` instruments `src/**/*.{ts,tsx}` with the V8 provider, but its
`thresholds` are explicitly set to `0` for lines/functions/branches/statements — so
`npm run test:coverage` reports a number but does not fail the build on it. Current
result is 1.62%, a known, openly tracked gap (only `api.ts`'s error-formatting helper has
unit coverage; the React component workspaces are exercised by Playwright E2E, not
Vitest unit tests, and are not reflected in this percentage).

## Reproduction commands

```powershell
# Full backend suite + both coverage gates
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe verification/scripts/core_coverage_report.py `
  --xml .waypoint-data/coverage.xml `
  --manifest verification/core-features.json `
  --output verification/results/core-feature-coverage.md

# Frontend unit tests + coverage report (unenforced)
npm --prefix frontend run test:unit
npm --prefix frontend run test:coverage

# Frontend end-to-end (Playwright)
npm --prefix frontend run test:e2e
```
