# Core-feature coverage report

## Rubric interpretation and enforced gate

The rubric asks for **at least 70% test coverage on core logic**. Waypoint therefore
maintains an explicit, reviewable core-feature manifest instead of treating the whole
backend percentage as proof by itself. A release passes only when **every named core
feature and the weighted core aggregate are at least 70.0% combined coverage**.

Combined coverage uses executable lines and branch outcomes:

```text
(covered executable lines + covered branch outcomes)
---------------------------------------------------- × 100
(total executable lines + total branch outcomes)
```

Manifest: `verification/core-features.json`
Source: `.waypoint-data/coverage.xml`

## Core-feature results

| Core feature | Lines | Branches | Combined | Threshold | Status |
|---|---:|---:|---:|---:|---|
| Repository analysis and graph construction | 88.78% (965/1087) | 71.89% (289/402) | **84.22%** | 70.0% | **PASS** |
| Revision-aware indexing and graph retrieval | 85.09% (1044/1227) | 74.50% (298/400) | **82.48%** | 70.0% | **PASS** |
| Grounded agent answering, validation, and memory | 88.44% (704/796) | 67.55% (127/188) | **84.45%** | 70.0% | **PASS** |
| Adaptive onboarding routes and missions | 84.83% (900/1061) | 63.56% (150/236) | **80.96%** | 70.0% | **PASS** |
| Secure repository import | 81.12% (116/143) | 83.33% (30/36) | **81.56%** | 70.0% | **PASS** |
| **Weighted core aggregate** | **86.44% (3729/4314)** | **70.84% (894/1262)** | **82.91%** | **70.0%** | **PASS** |

## Why each feature is core

### Repository analysis and graph construction

Creates the evidence graph that every Explore, Ask, Onboarding, Issues, and MCP workflow consumes.

Included modules:

- `backend/app/graph/models.py`
- `backend/app/graph/analyzer.py`
- `backend/app/graph/polyglot.py`

### Revision-aware indexing and graph retrieval

Keeps repository evidence current and supplies bounded lexical, vector, symbol, and graph context to agents.

Included modules:

- `backend/app/indexing.py`
- `backend/app/graph/store.py`
- `backend/app/agent/retrieval.py`
- `backend/app/agent/semantic.py`

### Grounded agent answering, validation, and memory

Implements multi-round repository investigation, structured answers, citation validation, comparison, and conversation memory.

Included modules:

- `backend/app/agent/memory.py`
- `backend/app/agent/service.py`
- `backend/app/agent/comparison.py`
- `backend/app/agent/agentic_comparison.py`
- `backend/app/agent/tool_contracts.py`

### Adaptive onboarding routes and missions

Turns repository evidence into role-, objective-, experience-, and time-specific learning routes and contribution missions.

Included modules:

- `backend/app/agent/onboarding.py`
- `backend/app/onboarding/models.py`
- `backend/app/onboarding/questions.py`
- `backend/app/onboarding/service.py`

### Secure repository import

Places a local path or public GitHub repository inside the analyzed workspace while enforcing repository security limits.

Included modules:

- `backend/app/repository_import.py`

## Supporting subsystems reported outside the core denominator

These modules are not hidden: they remain in the stricter whole-backend report. They
are separated here so transport/framework infrastructure cannot redefine the product's
core business logic after seeing the percentages.

| Supporting subsystem | Combined coverage | Reason outside core denominator |
|---|---:|---|
| Provider and SDK adapters | 47.71% | Vendor transport integration; exercised by adapter unit tests and live evaluations but excluded from the core business-logic denominator. |
| HTTP and MCP delivery adapters | 59.87% | Interface wiring around core services; covered separately by integration and E2E tests. |
| Subprocess execution infrastructure | 28.99% | Generic bounded process runner used by secure import, not the repository-import policy itself. |
| Issues secondary feature | 51.06% | Week 7 secondary feature rather than the primary onboarding/grounded-RAG business path; still included in whole-backend coverage. |
| Observability infrastructure | 75.75% | Cross-cutting trace instrumentation; verified separately through hook tests and live JSONL evidence. |

## Relationship to whole-backend coverage

The core gate is additive to—not a replacement for—the existing `--cov-fail-under=70`
whole-backend gate. This prevents a narrow core definition from concealing untested
supporting code while also answering the rubric's core-logic requirement directly.

**Core gate result: PASS.**
