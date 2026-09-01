# Recommended indexing and retrieval architecture (roadmap)

This document describes the target storage and retrieval architecture for Waypoint. The
goal is to improve retrieval accuracy, latency, restart behavior, and observability
without introducing infrastructure that the current product does not yet need.

**Implementation status (checked against [`system-architecture.md`](system-architecture.md) and [`storage-and-retention-detail.md`](storage-and-retention-detail.md), 2026-07-29):** this is not a stale document — most of the storage/indexing recommendations below are implemented, but several retrieval-quality and scale items remain live roadmap, not done:

| Recommendation | Status |
|---|---|
| Persistent revision-aware SQLite hybrid index (repositories/revisions/files/symbols/chunks/edges/FTS5) | **Implemented** |
| Reciprocal-rank fusion of lexical + vector candidates | **Implemented** |
| Query intent classification routing to semantic tools | **Implemented** |
| One-hop graph expansion around strong results | **Implemented** |
| Durable product state for onboarding tours and issue findings | **Implemented** |
| Incremental reuse of unchanged chunk vectors across revisions | **Implemented** (full parse/file reuse is not — see below) |
| No dedicated external graph database | **Implemented as a deliberate decision**, not an oversight |
| `stable_key` for symbols independent of line numbers | **Outstanding** — chunk/symbol IDs still shift when line ranges change |
| SQL-indexed graph traversal over `code_edges` | **Outstanding** — `GraphQueryService` still traverses the in-memory deserialized report, not indexed SQL |
| `EmbeddingStore` / learned or hosted embeddings | **Outstanding** — only `LocalCodeVectorizer`'s deterministic sparse vectors exist; no learned/hosted provider |
| Markdown split by heading hierarchy | **Outstanding** — docs still use the 120-line/100-line-advance window fallback, not heading-aware splitting |
| Learned/calibrated reranker | **Outstanding** — deterministic MMR/diversification remains the fallback |
| Full incremental parse/file reuse (not just vectors) | **Outstanding** |
| Phase 5 scale review at large repositories | **Outstanding** — no dedicated profiling pass has been run |

The "roughly 8-12 evidence chunks" figure below refers to `search_repository`'s *default* result count; the tool's actual ceiling is 50 per call (`SearchInput`, `backend/app/agent/tool_contracts.py:25-28`) — both are true simultaneously, they describe the default versus the maximum, not a contradiction.

## Recommendation

Build a persistent, revision-aware hybrid index in SQLite first:

```text
Repository revision
        |
        v
Language parser and graph analyzer
        |
        +-------------------+
        |                   |
        v                   v
Syntax-aware chunks    Nodes and typed edges
        |                   |
        v                   |
SQLite FTS5/BM25            |
        |                   |
        +---- optional vector index
                    |
                    v
        Hybrid candidate generation
                    |
                    v
        One-hop graph expansion
                    |
                    v
              Final reranking
                    |
                    v
          Bounded source evidence
                    |
                    v
             Claude synthesis
                    |
                    v
          Citation-span validation
```

This keeps the existing agent and graph advantages while replacing the temporary
custom search index with a durable retrieval layer.

## Why this design

Code retrieval needs several different signals:

- Exact lexical search finds identifiers, imports, routes, configuration keys, and
  filenames.
- Semantic search connects a natural-language question to code using different
  terminology.
- Structural parsing keeps functions and classes intact.
- Graph traversal finds callers, callees, imported modules, implementations, and
  tests around an initially relevant symbol.
- A reranker removes candidates that matched individual words but do not answer the
  question.

None of those signals is sufficient alone. Vector-only retrieval can miss exact
identifiers, while graph-only retrieval needs a relevant starting node. The proposed
pipeline uses lexical and semantic retrieval to find seeds and the code graph to add
architectural context.

## Proposed SQLite schema

The schema should normalize data that must be searched or updated independently.
Exact migrations can evolve, but the conceptual entities should be stable.

### Repositories and revisions

```sql
repositories (
    id, canonical_root, display_name, remote_url, created_at, updated_at
)

revisions (
    id, repository_id, git_commit, content_fingerprint,
    analyzer_version, status, indexed_at
)
```

`content_fingerprint` supports directories without Git. `analyzer_version` forces a
safe rebuild when parser behavior or the graph schema changes.

### Files

```sql
files (
    id, revision_id, relative_path, language, content_sha256,
    byte_size, line_count, parse_status, indexed_at
)
```

The unique key should be `(revision_id, relative_path)`. File hashes make incremental
updates possible: unchanged files reuse their parsed and embedded data.

### Symbols

```sql
symbols (
    id, revision_id, file_id, stable_key, kind, name, qualified_name,
    parent_symbol_id, signature, start_line, end_line, metadata_json
)
```

`stable_key` should derive from language, normalized path, symbol kind, qualified
name, and parent—not solely from line numbers. That lets a symbol retain identity when
unrelated lines are inserted above it.

### Chunks

```sql
chunks (
    id, revision_id, file_id, symbol_id, kind,
    start_line, end_line, content_sha256,
    contextual_text, source_text, token_count, metadata_json
)
```

`source_text` preserves exact evidence. `contextual_text` is used for retrieval and
embeddings and can include repository, path, enclosing symbols, signature, imports,
exports, and a short deterministic purpose description.

### Full-text index

Create an FTS5 virtual table over weighted fields such as:

```text
path
symbol_name
qualified_name
signature
contextual_text
source_text
```

FTS5/BM25 replaces raw occurrence counting with document-length and term-rarity-aware
ranking. Exact path and symbol matches should still receive explicit boosts because
those signals are unusually important in source code.

### Graph edges

```sql
edges (
    id, revision_id, source_symbol_id, target_symbol_id,
    kind, confidence, evidence_status,
    evidence_file_id, evidence_start_line, evidence_end_line,
    metadata_json
)
```

Indexes should cover `(revision_id, source_symbol_id, kind)` and
`(revision_id, target_symbol_id, kind)`. One- and two-hop traversals can then be served
efficiently without deserializing the complete analysis report.

### Embeddings

Keep embedding storage behind an interface:

```text
EmbeddingStore.upsert(chunks)
EmbeddingStore.delete(chunk_ids)
EmbeddingStore.search(revision_id, vector, limit)
```

For the local-first version, embeddings may be stored as SQLite blobs with a suitable
vector extension or in a sidecar local vector index. A hosted vector service should
not become mandatory until deployment scale requires it.

### Durable product state — implemented

Visible agent-product state is now stored in SQLite:

```text
issue_findings
onboarding_tours
onboarding challenge definitions and attempts (inside `onboarding_tours`)
```

This is separate from retrieval quality, but prevents visible user state from
disappearing after a backend restart. The stores retain bounded in-memory caches over
these durable records.

## Chunking strategy

### Source code

Use parser-derived chunks for:

- Module or file overview.
- Class, interface, enum, and type declaration.
- Function, method, constructor, and route handler.
- Important module-level initialization blocks.

Include a small amount of surrounding context only when required to understand the
symbol. Very large classes should have a class-summary chunk plus separate method
chunks rather than one enormous class chunk.

### Documentation and configuration

- Split Markdown by headings and preserve the heading hierarchy.
- Treat manifest sections and major configuration objects as chunks.
- Use overlapping line windows only as a fallback when no structural parser exists.

### Contextual headers

A retrieval representation might look like:

```text
Repository: waypoint
Path: backend/app/agent/service.py
Language: Python
Symbol: RepositoryAgentService.answer
Kind: method
Parent: RepositoryAgentService
Related concepts: tool loop, conversation memory, evidence validation

<exact source follows>
```

The header helps both lexical and semantic search understand an isolated symbol. The
exact source and line range remain separate so citations always point to repository
content rather than generated descriptions.

## Query and ranking pipeline

### 1. Query planning

Classify the question into one or more bounded intents, for example:

- Repository overview or features.
- Exact symbol or file lookup.
- Runtime flow or entry point.
- Dependency impact.
- Backend or frontend architecture.
- Configuration and deployment.
- Tests related to a component.

Known intents should invoke the existing deterministic semantic tools. Open-ended
questions should also run hybrid search. This routing is a planning aid, not a fixed
answer template; Claude still synthesizes the final response.

### 2. Candidate generation

Generate candidates independently:

1. Exact path, symbol, and qualified-name lookup.
2. FTS5/BM25 results, usually the best 30–50.
3. Embedding similarity results, usually the best 30–50.
4. Deterministic semantic-tool evidence for recognized intents.

Run independent searches concurrently.

### 3. Rank fusion

Merge candidate lists using Reciprocal Rank Fusion. RRF is a practical initial choice
because lexical and vector scores are not naturally calibrated to the same scale.
Apply explicit boosts for exact symbol/path matches and penalties for generated,
vendored, fixture, or test code unless the question asks for those files.

### 4. Graph expansion

Take the strongest symbol-backed candidates and retrieve a bounded set of related
nodes. Relationship-specific expansion should depend on intent:

| Intent | Useful expansion |
| --- | --- |
| “How does this work?” | callers, callees, imports, contained symbols |
| “What breaks if this changes?” | incoming calls, importers, related tests |
| “Where does the app start?” | entry module, configuration, initialized services |
| “How is this tested?” | test imports, matching symbol/path names, fixtures |
| Architecture | modules, imports, central services, transport-to-domain links |

Default to one hop. Two hops should require an impact or journey question and strict
result limits, otherwise graph expansion can swamp the useful seed evidence.

### 5. Reranking and evidence selection

Rerank the fused candidates against the complete question and recent conversation
context. Initially this can be a deterministic score combining:

- Fused rank.
- Exact identifier and path matches.
- Intent-compatible node and edge kinds.
- Production-versus-test relevance.
- Source diversity.
- Graph distance from strong seeds.

A learned or model-based reranker can be introduced after benchmarks show it improves
retrieval enough to justify its latency and cost. Pass roughly 8–12 diverse, bounded
evidence chunks to the answer model rather than dumping entire files into context.

## Incremental indexing lifecycle

1. Resolve the repository and identify its Git commit when available.
2. Discover eligible files and calculate content hashes.
3. Compare hashes and analyzer version with the latest complete revision.
4. Parse and chunk only added or changed files.
5. Reuse unchanged file, symbol, chunk, and embedding records where safe.
6. Delete records for removed files and stale relationships.
7. Recompute cross-file edges affected by changed imports or exports.
8. Commit the new revision atomically and mark it complete.
9. Keep the previous complete revision until the new one succeeds.

Queries must never see a partially built revision. A failed index operation should
leave the last complete revision available.

## Why SQLite before a graph database

Waypoint already has graph semantics, but its expected queries are bounded one- or
two-hop traversals inside one repository. Indexed edge tables handle those efficiently
while also allowing analysis, conversations, FTS, and product state to share one
transactional local database.

A dedicated graph database would add deployment, synchronization, authentication,
backup, and migration complexity without fixing the first-stage retrieval problem.
The immediate problem is locating the correct seed symbols for a question; BM25 and
semantic retrieval improve that directly.

Adopt a dedicated graph database only when measurements show requirements such as:

- Millions of symbols and relationships in a routinely queried graph.
- Frequent deep, variable-length multi-hop queries.
- Cross-repository or organization-wide dependency analysis.
- High concurrent graph query and update volume.
- Latency demonstrably dominated by indexed SQL edge traversal.

The repository and service interfaces should avoid SQLite-specific graph assumptions,
so a future Neo4j, Memgraph, or other graph adapter can be added without changing the
agent tools.

## Observability requirements

Every indexing and retrieval request should have a correlation ID and log:

- Repository, revision, analyzer version, and changed-file counts.
- Per-file parse/chunk duration and failure reason.
- Inserted, reused, updated, and deleted symbol/chunk/edge counts.
- Embedding batch size, provider duration, retries, and failures without logging keys.
- Query plan and normalized intent.
- Candidate counts and durations for exact, BM25, vector, and semantic retrieval.
- Rank-fusion, graph-expansion, and reranking durations.
- Final evidence IDs, paths, spans, scores, and why each was selected.
- Model tool rounds, evidence token count, answer citations, and rejected citations.

Source contents and secrets should not be emitted indiscriminately. “Immense logs”
should mean complete structured provenance and timing, with configurable truncated
values, rather than leaking entire repositories or environment variables.

## Evaluation plan

Build a benchmark containing repositories of different sizes and languages. Each
question should specify expected files, symbols, and acceptable alternative evidence.
Measure retrieval separately from response fluency:

- File Recall@5 and Recall@10.
- Symbol Recall@5 and Recall@10.
- Mean reciprocal rank.
- Citation correctness and source-span validity.
- Evidence diversity and irrelevant-context rate.
- Retrieval latency at p50 and p95.
- Index time, incremental update time, index size, and embedding cost.
- Final answer completeness scored against repository-grounded criteria.

Compare lexical-only, lexical-plus-graph, lexical-plus-vector, and full hybrid modes.
Embeddings and reranking should remain only if they produce measurable improvements.

## Implementation phases

### Phase 1: persistent lexical and graph index — implemented

- Added normalized revision, file, symbol, chunk, and edge tables.
- Added FTS5/BM25 search with code-specific scoring and filters.
- Retained the existing retrieval interface and added precise symbol/status tools.
- Added immutable revision publication and retrieval diagnostics.

Stable symbol identity independent of parser node IDs remains an improvement for the
next schema revision.

This phase offers the highest immediate value: restart persistence, better lexical
ranking, SQL-filterable graph data, and lower repeated analysis cost.

### Phase 2: incremental revisions — partially implemented

- Content fingerprints, change detection, atomic publication, graph refresh, and an
  agent-visible index-status tool are implemented.
- Reusing unchanged parsed files/chunks, targeted edge invalidation, and UI indexing
  progress remain to be implemented.

### Phase 3: hybrid retrieval — implemented locally

- Added contextual chunk text and versioned local subword vectors.
- Added automatic vector backfill and exact-content reuse across revisions.
- Added vector candidate generation and reciprocal-rank fusion with BM25.
- Preserved exact lexical retrieval as a first-class signal.

The local vectors are deliberately dependency-free. A learned embedding-provider
adapter remains optional and should be adopted only when benchmarks demonstrate a
material retrieval improvement.

### Phase 4: reranking and benchmark tuning — implemented baseline

- Added a known-evidence retrieval dataset and executable benchmark.
- Added deterministic code/test/path scoring, result diversity, and one-hop graph
  expansion.
- The current local benchmark reaches Recall@10 of 1.0 across its initial eight cases.
- An optional learned reranker and larger external-repository datasets remain
  evidence-driven enhancements, not correctness requirements.

### Phase 5: scale review

- Profile SQLite FTS and edge traversals on the largest supported repositories.
- Consider separating vector or graph storage only where benchmark evidence demands it.

## Final decision

Implement the persistent SQLite hybrid index. Keep and normalize the existing code
graph, but do not introduce a dedicated graph database yet. The target architecture is
not “RAG instead of a graph”; it is lexical and semantic RAG that uses the graph for
structural expansion and explanation.
