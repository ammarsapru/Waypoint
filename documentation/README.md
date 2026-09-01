# Waypoint documentation index

This folder holds Waypoint's hand-maintained reference documentation, reorganized on
2026-07-29 out of ~20 markdown files that had accumulated at the project root since
2026-07-18 — several were literal duplicate snapshots of each other (explicit `V2`/`V3`/
`V4` benchmark reports), several described a "current" state that had since changed
(single-model routing claims after dual-role shipped; MCP tool counts that grew from 17
to 30), and a couple were early drafts fully superseded by a later, more refined
document. Nothing was deleted — superseded material moved to [`archive/`](archive/)
with a banner pointing at its replacement, so history is still recoverable.

Two logs stay at the project root rather than living here, because they're append-only
development journals, not descriptions of the application: [`../prompts.md`](../prompts.md)
(the AI-assisted engineering prompt log) and [`../issues.md`](../issues.md) (the bugs-found
log, with the incident history behind several corrections made in this reorganization).

## Architecture

- [`architecture/system-architecture.md`](architecture/system-architecture.md) — the
  primary architecture reference: parsing, chunking, storage, retrieval, the dual-role
  model split, validation/retry loops, observability, testing, and a full error/
  correction ledger. Start here for "how does Waypoint actually work."
- [`architecture/storage-and-retention-detail.md`](architecture/storage-and-retention-detail.md) —
  supplementary detail on the storage map, current limitations, and the agentic
  retrieval path, for anyone who wants more depth than the primary doc's summary.
- [`architecture/roadmap.md`](architecture/roadmap.md) — the target/recommended
  indexing and retrieval architecture, annotated with which recommendations are
  implemented versus still outstanding (most storage/indexing items are done; learned
  reranking, SQL-native graph traversal, and stable symbol IDs are not).

## Model routing

- [`model-routing.md`](model-routing.md) — single-model and dual-role architecture:
  how a provider/model is chosen for a request, the 2026-07-29 redesign that made
  investigation and synthesis share one resolved provider per conversation instead of
  routing independently, and a verified working `.env` example.

## Limits, tools, and operations

- [`limits-and-token-usage.md`](limits-and-token-usage.md) — every bounded limit in the
  system (agent rounds, onboarding, citations, token/output ceilings) plus what token
  usage is and isn't currently metered.
- [`agent-and-mcp-tools.md`](agent-and-mcp-tools.md) — the internal agent tool registry
  (18 tools shared across Ask/onboarding/mission/issues) and the full categorized
  30-tool MCP server inventory.
- [`mcp-connector.md`](mcp-connector.md) — how to start the MCP server (stdio or
  Streamable HTTP) and what it exposes.
- [`hooks-and-logging.md`](hooks-and-logging.md) — structured logging, the `@traced`
  decorator, correlation IDs, and the maximum-tracing mode.
- [`prompting-fundamentals.md`](prompting-fundamentals.md) — the prompting design
  principles Waypoint's workflows follow (role/objective framing, tool-oriented
  decomposition, evidence grounding, prompt-injection boundaries, structured outputs).

## Product

- [`product-plan.md`](product-plan.md) — product principles, the differentiator build
  plan (adaptive tours, mastery, first-contribution missions, architecture conformance,
  runtime overlays), implementation status, and delivery phases.

## Benchmarks and evaluation

This cluster previously had the most redundancy — the same test-count and coverage
figures were restated with different, increasingly stale numbers across several files.
Each doc below has a genuinely distinct purpose; where a document's own numbers are
known-superseded, it says so at the top and points at the fresher figure.

- [`benchmarks/test-suite-and-coverage.md`](benchmarks/test-suite-and-coverage.md) —
  the freshest test/coverage breakdown (per-file unit/integration composition, gate
  mechanics). Check here first for current counts, or just run `pytest` yourself.
- [`benchmarks/operational-evaluation.md`](benchmarks/operational-evaluation.md) — the
  fullest single write-up of the operational scorecard (parsing, retrieval, model
  quality, performance, frontend, load/cache/memory, Web Vitals, remediation plan).
- [`benchmarks/methodology-and-failures.md`](benchmarks/methodology-and-failures.md) —
  the scorecard formulas and root-cause failure analysis behind each category.
- [`benchmarks/sdk-ttft-measurement.md`](benchmarks/sdk-ttft-measurement.md) — why true
  token TTFT isn't measurable via the Claude Agent SDK, and what is measured instead.
- [`benchmarks/history.md`](benchmarks/history.md) — the canonical, append-only,
  cumulative benchmark/test/limits/evaluation report. Every dated run (baseline, V2,
  V3, V4) is preserved here rather than overwritten; this is the source of truth when
  a number here disagrees with a snapshot elsewhere.

## Archive

[`archive/`](archive/) holds superseded documents kept for history: point-in-time
benchmark snapshots that duplicated `benchmarks/history.md`, an old evaluation index,
two architecture write-ups fully covered by `architecture/system-architecture.md`, the
earliest product-planning draft, and a source-code Q&A pinned to 2026-07-22 line
numbers. Each file has a banner explaining what superseded it — don't treat their
content as current.
