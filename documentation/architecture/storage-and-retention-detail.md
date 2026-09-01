# Storage and retention detail

> **Supplementary to [`system-architecture.md`](system-architecture.md)**, which is
> the primary architecture reference and now includes this document's retention
> limits, `GraphQueryService`, and onboarding/issue storage facts (§4.3 there). Read
> this document for the deeper walkthrough of the storage map, current limitations,
> and the agentic retrieval path; skip it if you only need the summary.

This document describes what Waypoint stores today, what exists only in memory, and
how a repository question reaches the language model. It reflects the current code,
not the intended future architecture.

## Executive summary

Waypoint currently uses a mixed persistence model:

- SQLite persists analysis reports, recent conversations, revisioned code chunks,
  normalized symbols and edges, file hashes, and an FTS5 search index.
- The analyzed repository remains the authoritative source for file contents.
- Parsed graph nodes and edges are serialized as one JSON analysis report in SQLite.
- Retrieval chunks are persisted per immutable content revision and also cached in
  memory while active.
- AI issue findings and onboarding challenge/mastery state persist in SQLite.
- There is an FTS5/BM25 index and a local deterministic subword-vector index, but no
  external vector database, learned embedding provider, or dedicated graph database.

The existing question-answering path is already a form of agentic, graph-augmented
RAG. Claude chooses bounded retrieval tools, receives source evidence, and writes an
answer whose citations must refer to inspected spans. The weak point is not the
agent loop. The current index provides durable lexical and local subword-vector
retrieval; learned semantic embeddings and incremental reuse of unchanged records
remain future work.

## Storage map

| Information | Current location | Survives restart? | Limits |
| --- | --- | --- | --- |
| Repository source | Original local directory or `.waypoint-clones` | Yes | Subject to configured file and clone limits |
| Analysis report | SQLite `analysis_sessions.report_json` | Yes | Five most recently updated analyses |
| Repository root and allowed paths | SQLite `analysis_sessions` | Yes | Restored only when still inside the allowed root |
| Graph nodes and edges | `report_json`, plus normalized `code_symbols`/`code_edges` | Yes | Active graph is still loaded as Pydantic objects |
| Conversations | SQLite `conversations` and `conversation_messages` | Yes | 200 conversations; recent configured turn window per conversation |
| Retrieval chunks | SQLite `code_chunks` plus Python cache | Yes | Three revisions per repository; ten active indexes in memory |
| Lexical search | SQLite `code_chunks_fts` | Yes | Revision-scoped FTS5/BM25 candidates |
| Local vectors | SQLite `code_chunk_vectors` | Yes | Versioned normalized subword vectors |
| File freshness | `code_files` content hashes and revision fingerprint | Yes | Checked when an agent service opens the analysis |
| Decoded source lines | Dictionary inside each retrieval index | No | Lifetime of the cached index |
| Semantic-tool results | Dictionary inside one `SemanticRepositoryTools` instance | No | Lifetime of its repository agent service |
| AI-proposed issue findings | SQLite `issue_findings` plus `FindingStore` cache | Yes | One current proposal set per analysis |
| Onboarding challenge progress | SQLite `onboarding_tours` plus `TourStateStore` cache | Yes | 100 tours in active memory cache |
| GitHub issues | Fetched from GitHub when requested | No local durable cache | GitHub API pagination and availability |

## SQLite state database

The database path is controlled by `WAYPOINT_STATE_PATH`. Its default is:

```text
.waypoint-data/waypoint.sqlite3
```

The same SQLite file is used for analysis sessions, conversations, the code index,
onboarding mastery, and AI-proposed issue findings.

### Analysis sessions

`AnalysisSessionStore` creates this table lazily:

```sql
analysis_sessions (
    id                TEXT PRIMARY KEY,
    root              TEXT NOT NULL,
    report_json       TEXT NOT NULL,
    source_paths_json TEXT NOT NULL,
    updated_at        TEXT NOT NULL
)
```

`report_json` contains the entire `AnalysisReport`, including repository metadata,
nodes, edges, unresolved references, diagnostics, and statistics. Nodes and edges
are therefore durable, but they are not stored as independently queryable database
rows for UI graph queries. The retrieval index now also normalizes symbols and edges
into revision-scoped tables. Existing graph tools still traverse Pydantic objects;
the normalized tables establish durable queryable storage for future SQL traversal.

`source_paths_json` is the allowlist of files that retrieval may read. It contains
supported source, documentation, and configuration paths. The associated
`revision_fingerprint` is derived from relative paths and SHA-256 content hashes. On restore,
Waypoint verifies that the repository root:

1. Is still under `ONBOARD_ALLOWED_ROOT`.
2. Still exists as a directory.
3. Contains a valid serialized report and path list.

Only five analysis sessions are retained. The store also keeps up to five sessions
in memory for quick access.

### Conversations

`ConversationStore` creates:

```sql
conversations (
    id          TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL,
    channel     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
)

conversation_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    answer_json     TEXT,
    created_at      REAL NOT NULL
)
```

Messages are ordered by their integer ID. `answer_json` preserves the structured
answer, including evidence and agent activity, while `content` preserves the visible
text. Conversations are scoped to an analysis and UI channel.

The model receives only the last `WAYPOINT_CHAT_HISTORY_TURNS` turns, which defaults
to 12. Older messages are deleted when a new turn is appended, so this is bounded
conversation memory rather than an unlimited transcript archive.

## Repository source storage

Waypoint does not copy a local repository into the database. Source is read from the
analyzed directory. A GitHub URL is cloned into the configured secure clone root,
which defaults to:

```text
<allowed-root>/.waypoint-clones
```

Before reading a source path, retrieval checks that it was included in the analysis,
resolves inside the repository root, is a regular non-symlink file, is below the
configured size limit, and can be decoded. These checks prevent a model tool call
from using the source reader as an arbitrary filesystem reader.

## Graph representation and traversal

The analyzer produces typed nodes such as repositories, modules, classes, functions,
and methods. It also produces typed relationships such as containment, imports, and
possible calls, with evidence metadata.

At query time `GraphQueryService` constructs dictionaries and adjacency sets from the
deserialized report. Neighborhood traversal is breadth-first and supports a bounded
depth. Graph summary calculations count node types, edge types, evidence statuses,
and node degree.

This means Waypoint already has a code graph, but not a graph database. The active
graph is an in-memory projection of the report, while normalized revisioned edge rows
are also stored in SQLite.

## Current retrieval index

`RepositoryRetrievalIndex` loads a complete revision from SQLite when available. It
builds and atomically publishes `SourceChunk` objects when that revision has not been
indexed before. Each chunk records:

- A deterministic hash based on path, line range, and kind.
- File path and source line range.
- Node kind.
- Source content.
- Optional symbol ID and qualified name.

Parsed code is primarily chunked using analyzer source spans. Functions, methods,
classes, and modules therefore become structurally meaningful chunks. Module chunks
are capped to roughly 160 lines. Documentation, configuration, and unparsed files
are divided into overlapping line windows of up to 120 lines, advancing 100 lines at
a time.

The index is cached by analysis ID in `RepositoryIndexStore`. At most ten indexes are
held. Eviction or restart discards only the memory cache; the next request restores
the matching complete revision from SQLite.

On every new repository-agent service, Waypoint snapshots eligible files. If their
combined path/content fingerprint differs from the analysis fingerprint, it re-runs
the analyzer, refreshes the same analysis ID, and publishes a new revision. Queries
never read a revision marked `building`.

### Lexical ranking

Search extracts identifier-like query tokens and asks revision-scoped FTS5 for BM25
candidates. A local subword-vector search supplies fuzzy word-form candidates. The two
rank lists are fused using reciprocal-rank scores and combined with code-specific
scoring based on:

- Token occurrence count.
- Exact normalized query occurrence.
- Matches in the qualified symbol name.
- Matches in the path.
- A small bonus for class, function, and method chunks.

The strongest symbol-backed seeds receive bounded one-hop graph expansion, and broad
queries are diversified across files. Results can be filtered by path prefix, symbol
kind, language, and whether tests are
allowed. They are sorted by score, path, and starting line. BM25 accounts for term
rarity and document length. The local subword vectors handle related word forms, but
synonyms and broader conceptual similarity remain limited without learned embeddings.

## Agentic retrieval path

For an Ask request, the current high-level flow is:

```text
Question and recent conversation turns
                 |
                 v
       RepositoryAgentService
                 |
       Claude selects one or more tools
                 |
       +---------+------------------+
       |                            |
Low-level source tools       Semantic repository tools
search/read/tree/symbol      overview/features/entry points/
graph neighborhood           architecture/impact/tests/etc.
       |                            |
       +-------------+--------------+
                     v
             Inspected evidence
                     |
                     v
        Claude submits synthesized answer
                     |
                     v
       Citation and source-span validation
```

The semantic tools are deterministic queries over the report, graph, manifests, and
source index. They help Claude ask more precise questions than generic text search.
Independent tool calls may execute concurrently, and every source excerpt returned by
a tool is registered as inspected evidence. The final answer is rejected or corrected
if it cites source outside those inspected spans.

Symbol-focused Inspector questions additionally carry a `focus_node_id`. Waypoint
preloads that symbol's callers, callees, importers, instantiation sites, related files,
and exact edge evidence before the first model round. The deterministic Usage tab uses
the same response without requiring a model call.

## What works well

- Source reads are bounded and constrained to analyzed files.
- Symbol chunks use parser-derived spans rather than only arbitrary token windows.
- Exact identifiers, paths, and qualified names receive strong lexical signals.
- Search is persistent and revision-scoped through FTS5/BM25.
- Content changes invalidate the active revision and refresh its graph and chunks.
- Claude can filter searches and use exact symbol lookup in a single tool call.
- The model can combine source retrieval with graph relationships.
- Deterministic semantic tools make common architecture questions more reliable.
- Answers have evidence provenance and citation validation.
- SQLite keeps the local-first deployment simple.

## Current limitations

1. A changed revision currently rebuilds its complete graph and chunk set; unchanged
   file rows and embeddings are not yet copied incrementally.
2. Active graph tools still traverse the in-memory report instead of querying the
   normalized edge tables.
3. There is no external learned-embedding provider for strongly synonymous concepts.
4. The local subword vectorizer is deterministic and dependency-free, not a learned semantic
   embedding model; a learned provider may improve synonym-heavy questions.
5. Chunk identity includes line ranges, so edits above a symbol can change its ID.
6. Content hashing reads eligible files whenever an agent service opens an analysis;
   a stat-based fast path is not implemented yet.
7. Memory consumption grows with the number and size of active cached indexes.
8. Full parser reuse for unchanged files is not implemented, although unchanged exact
   chunk vectors are reused safely across revisions.

## Relevant implementation files

- `backend/app/config.py` — paths, retention limits, model, and agent settings.
- `backend/app/indexing.py` — eligible-file discovery and repository fingerprints.
- `backend/app/graph/store.py` — analysis persistence and in-memory graph queries.
- `backend/app/agent/memory.py` — durable bounded conversation history.
- `backend/app/agent/retrieval.py` — persistent revisions, FTS5, chunks, safe reads, and cache.
- `backend/app/agent/semantic.py` — deterministic repository-level retrieval tools.
- `backend/app/agent/service.py` — Claude tool loop and evidence validation.
- `backend/app/agent/issues.py` — SQLite-backed AI finding storage with an in-memory cache.
- `backend/app/onboarding/service.py` — SQLite-backed tour progress and mastery storage.
- `backend/app/observability.py` — correlated pre/post/error hooks and rotating JSONL traces.
- `backend/app/mcp_server.py` — MCP tools, resources, and prompt adapters over the same services.
