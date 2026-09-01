> **Archived, superseded.** This is a condensed restatement of
> [`../architecture/system-architecture.md`](../architecture/system-architecture.md)
> §2-8, with no code-line citations and nothing not already covered there (per a
> content audit on 2026-07-29). Kept here for history only.

# Waypoint RAG architecture

## 1. Repository acquisition and security boundary

The user selects a local repository under `ONBOARD_ALLOWED_ROOT` or supplies a public
HTTPS GitHub URL. GitHub repositories are shallow-cloned into the managed clone root
with credential prompting disabled, timeout/file/byte limits, symlink avoidance, and
retention cleanup.

The analyzer never treats an arbitrary requested path as the allowed root. It resolves
the requested repository and verifies containment under the separately configured root.

## 2. Multi-language static analysis

Waypoint discovers Python, JavaScript, TypeScript, JSX/TSX, and Java source while
skipping dependencies, VCS metadata, build output, caches, managed clones, symlinks, and
oversized files.

Python uses the standard AST. JavaScript, TypeScript, and Java use Tree-sitter. Analysis
creates repository, module, class, function, and method nodes plus contains/imports/
may-call/instantiates edges. Every resolved edge carries source evidence and confidence;
failures remain explicit unresolved references.

External imports are classified separately and feed the synthetic External packages UI
card rather than being turned into fake internal graph nodes.

## 3. Immutable repository revision

`repository_snapshot()` walks retrievable files deterministically and stores, per file:

- normalized relative path;
- SHA-256 content hash;
- byte size.

The aggregate path/content digest is the repository fingerprint. Repository ID,
fingerprint, and index schema version produce an immutable revision ID. A content change
therefore produces a different revision instead of silently mutating evidence under an
old answer.

## 4. Symbol-aware chunking

Parser source spans create meaningful chunks for modules, classes, functions, and
methods. Documentation and configuration files, or source without suitable symbol
spans, use bounded line chunks. Each chunk records path, lines, kind, content, symbol ID,
and qualified name.

This is more useful than arbitrary fixed token windows because retrieval can return a
complete function/method with its identity and graph relationships.

## 5. Persistent index

SQLite stores:

- repository revisions and indexing status;
- files and content identity;
- graph symbols and typed edges;
- source chunks;
- FTS5 search rows;
- local subword vectors;
- analysis sessions and conversation state in application storage.

Only complete revisions are published. Exact unchanged chunks can safely reuse vectors
across revisions. Recent revisions and in-memory index instances are bounded.

## 6. Hybrid candidate retrieval

For a query, Waypoint combines:

1. FTS5/BM25 lexical candidates.
2. Local subword-vector similarity candidates.
3. Direct token frequency and exact phrase matches.
4. Qualified-symbol and path matches.
5. Class/function/method relevance bonuses.
6. Test penalties unless the question requests tests.
7. Reciprocal-rank-style FTS/vector contributions.

The strongest symbol-backed results seed bounded graph expansion. Incoming/outgoing
neighbors add structurally relevant chunks even when their text does not closely match
the query.

## 7. Agentic tool planning

The model is not sent the entire codebase. It receives the question, bounded recent
conversation, system instructions, and 18 available tools. It can list, search, find,
read, inspect, expand, request semantic architecture summaries, find tests, and inspect
impact/configuration/diagnostics.

Each tool result is appended to the next round. Independent reads can run concurrently.
The outer loop is bounded by `WAYPOINT_AGENT_MAX_TOOL_ROUNDS` and forces final submission
near the limit once evidence exists.

## 8. Evidence registration and correction

Every source range returned by search, read, or semantic tools is registered as inspected
evidence. Tool errors are returned to the model for correction. Final citations must:

- reference an indexed path;
- contain valid line ordering;
- fall entirely inside an inspected source span;
- be re-readable through the active immutable index.

Pydantic validates the final answer schema. Invalid citations or fields are returned as
errors for another bounded attempt. An unvalidated answer never reaches the evidence
panel.

## 9. Answer presentation and memory

Only files cited in the validated final answer populate the Answer Evidence panel.
Conversation turns are persisted by analysis and scope, with only the configured recent
window sent back to the model. Symbol-inspector Ask preloads exact incoming/outgoing
usage evidence for the selected symbol.

## What the architecture is and is not

It is:

- agentic RAG;
- hybrid lexical/vector/graph retrieval;
- revision-aware and evidence-validating;
- local SQLite-first storage;
- static analysis with explicit uncertainty.

It is not currently:

- a dedicated graph database;
- an external vector database;
- a learned semantic-embedding system;
- runtime tracing of an analyzed application;
- proof that every `may_call` edge executed;
- full-repository dumping into every prompt.

## Related implementation

- `backend/app/repository_import.py` — secure GitHub acquisition.
- `backend/app/indexing.py` — deterministic snapshots and fingerprints.
- `backend/app/graph/analyzer.py` — repository analysis orchestration.
- `backend/app/graph/polyglot.py` — non-Python parsers and relationships.
- `backend/app/graph/store.py` — persisted analysis sessions and graph queries.
- `backend/app/agent/retrieval.py` — chunks, SQLite index, FTS, vectors, and ranking.
- `backend/app/agent/semantic.py` — high-level deterministic retrieval tools.
- `backend/app/agent/service.py` — agent loop, tool dispatch, and citation validation.
- `backend/app/agent/memory.py` — bounded persistent conversation memory.
