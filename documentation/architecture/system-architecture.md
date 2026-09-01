# Waypoint system architecture

This is Waypoint's primary architecture reference. It describes the architecture implemented in the current codebase, distinguishing what the system actually does today from what might be desirable later. Supplementary detail lives in [`storage-and-retention-detail.md`](storage-and-retention-detail.md) (retention limits and a few subsystems not repeated here) and [`roadmap.md`](roadmap.md) (target architecture not yet fully implemented); both cross-reference this document rather than duplicate it.

## 1. Executive summary

Waypoint is a graph-assisted, retrieval-augmented repository agent. It does not send an entire repository to an LLM. It first parses supported source files into a language-neutral graph, creates source-span-aware chunks, stores a revisioned index in SQLite, and exposes bounded retrieval and graph operations as tools. The model investigates the repository through those tools and must submit a structured, source-cited result.

Waypoint is agentic hybrid RAG over several evidence channels — repository search, bounded source-reading, symbol, graph, semantic, test, configuration, and diagnostic tools — not "RAG over graph nodes alone." Graph nodes are one evidence channel among several the model can choose across multiple rounds; retrieved results (including graph facts) are added to its conversation before it synthesizes an answer. Local sparse vectors (§4.4) are also a real, load-bearing part of retrieval, not a decoration — they contribute fuzzy identifier matching that lexical search alone misses.

```text
Local path or secured GitHub clone
              |
              v
     File discovery and safety checks
              |
              v
 Python AST + Tree-sitter language parsers
              |
              v
 Language-neutral nodes, typed edges, source spans
              |
              +-----------------------+
              |                       |
              v                       v
  Symbol-aware source chunks    Analysis graph/session
              |                       |
              +-----------+-----------+
                          v
       SQLite + FTS5 + local sparse vectors
                          |
                          v
       Bounded search/read/graph/semantic tools
                          |
                          v
        Investigation model -> synthesis model
                          |
                          v
       Pydantic schema and citation validation
                          |
                          v
       Answer + evidence + tool trace + memory
```

The important architectural boundary is that repository text is treated as untrusted evidence. The model gets selected excerpts and graph facts through tools; it is not allowed to execute repository code or freely browse the host filesystem.

## 2. How codebases are parsed

### 2.1 Discovery and safety

`RepositoryAnalyzer.discover_source_files()` walks the requested repository without following directory links. It:

- accepts Python plus the extensions supported by the polyglot parser;
- skips generated, dependency, VCS, virtual-environment, cache, and Waypoint data directories;
- rejects symlinks and resolved paths that escape the repository root;
- sorts paths for deterministic output;
- stops with an error if the configured file-count limit is exceeded.

The default limits are 5,000 source files and 2,000,000 bytes per parsed file. They are configurable through `ONBOARD_MAX_REPOSITORY_FILES` and `ONBOARD_MAX_PYTHON_FILE_BYTES`. Despite its historical name, `max_python_file_bytes` is also passed to the non-Python parser.

Relevant implementation:

- `backend/app/graph/analyzer.py:620-703` — discovery, extension filtering, symlink/path checks, and limits.
- `backend/app/config.py:126-133` — parsing limits.
- `backend/app/indexing.py:9-25` — retrievable code/context extensions and skipped directories.

### 2.2 Python parsing

Python files are decoded using `tokenize.open()`, which respects Python source encoding declarations, and parsed with the standard-library `ast.parse()`. The analyzer creates a module node with an exact source span, language metadata, size, and a SHA-256 content digest. AST visitors then extract definitions, imports, containment, inheritance, and call/reference relationships.

Relevant implementation:

- `backend/app/graph/analyzer.py:713-818` — Python decode, AST parse, diagnostics, and module creation.
- `backend/app/graph/analyzer.py:820+` — repository analysis and relationship resolution.

### 2.3 JavaScript, TypeScript, Java, HTML, and CSS parsing

The polyglot path uses Tree-sitter grammars. The supported source extensions are:

- JavaScript: `.js`, `.jsx`, `.mjs`, `.cjs`
- TypeScript: `.ts`, `.tsx`, `.mts`, `.cts`
- Java: `.java`
- markup/styles: `.html`, `.htm`, `.css`

Tree-sitter produces concrete syntax trees even for many partially invalid files. `PolyglotGraphBuilder` converts these language-specific trees into the same graph schema used by Python. It recognizes language-appropriate declarations such as classes, interfaces, enums, records, functions, methods, and Java constructors, then resolves imports and cross-file relationships where static evidence permits.

Relevant implementation:

- `backend/app/graph/polyglot.py:11-16` — Tree-sitter grammar dependencies.
- `backend/app/graph/polyglot.py:33-60` — extensions and declaration node types.
- `backend/app/graph/polyglot.py:116-145` — language-to-parser selection.
- `backend/app/graph/polyglot.py:164-223` — bounded file parsing and diagnostics.

### 2.4 Language-neutral graph output

Both parser paths emit common `GraphNode`, `GraphEdge`, `SourceSpan`, `Evidence`, and `UnresolvedReference` models. Typical node kinds include repository, module/file, class, function, and method. `EdgeKind` (`backend/app/graph/models.py:27-40`) defines exactly four typed edges: `contains` (lexical containment — module contains class contains method), `imports`, `may_call`, and `instantiates`. There is no separate "inheritance" or plain "calls" edge kind; a base-class relationship and a call reference are both represented through these four kinds plus each edge's own metadata. Each resolved relationship carries source evidence, a confidence value, and a resolution/evidence status.

This graph is static evidence, not a runtime trace. A `may_call` relationship means that the call target is plausible from the syntax and known symbols; it does not prove that the call happened at runtime. Relationships that cannot be safely resolved are retained as unresolved references or diagnostics rather than fabricated as definite edges.

## 3. How codebases are chunked

Chunking occurs after graph construction in `RepositoryRetrievalIndex._build_chunks()`.

### 3.1 Symbol-aware chunks

For every graph node that has a source span, Waypoint reads exactly that span and creates a `SourceChunk`. A chunk contains:

- stable chunk ID;
- repository-relative path;
- start and end lines;
- node kind;
- source text;
- graph symbol ID;
- qualified symbol name.

Module-level chunks are capped at 160 lines. Functions, methods, and classes normally use their parser-derived source span. Stable IDs include the graph symbol ID so two declarations sharing a span cannot overwrite one another.

### 3.2 File fallback chunks

Not every retrievable file has parsed symbols. Documentation, manifests, configuration, and source files without extracted definitions are chunked by line window:

- a window contains at most 120 lines;
- the next window begins every 100 lines;
- therefore adjacent windows overlap by up to 20 lines.

Parsed Python, JavaScript, TypeScript, and Java files with symbol chunks do not also receive generic file chunks. HTML and CSS currently fall through to line-window chunks because the skip set in `_build_chunks()` only names Python/JS/TS/Java extensions. Their syntax may still contribute graph nodes, but retrieval retains a file-window fallback.

This is not a fixed-token embedding pipeline. Its first preference is syntax-aware symbol boundaries, with bounded line windows as a fallback.

Relevant implementation:

- `backend/app/agent/retrieval.py:578-662` — chunk IDs, symbol chunks, module cap, and overlapping file chunks.

### 3.3 Keeping chunks up to date

`repository_snapshot()` walks all supported source and contextual files and records each relative path, byte size, and content SHA-256. It then hashes the ordered path/hash sequence into one repository fingerprint.

The repository ID is derived from the canonical root. The revision ID is derived from repository ID, fingerprint, and index schema version. Therefore:

- unchanged content restores the existing immutable revision;
- a changed, added, removed, or renamed indexed file changes the fingerprint and produces a new revision;
- changing the index schema version also invalidates old revisions;
- the previous complete revision is not mutated while a new one is being built.

Relevant implementation:

- `backend/app/indexing.py:28-85` — file state and deterministic repository fingerprint.
- `backend/app/agent/retrieval.py:193-201` — repository and revision IDs.
- `backend/app/agent/retrieval.py:502-551` — restore-or-build behavior.

## 4. How code and indexes are stored

The default state database is `.waypoint-data/waypoint.sqlite3`, configurable with `WAYPOINT_STATE_PATH` (`backend/app/config.py:193-198`). SQLite uses foreign keys and WAL journal mode.

### 4.1 Persistent index tables

`PersistentCodeIndex` creates these records:

| Table | Purpose |
|---|---|
| `code_repositories` | Canonical repository identity and display name |
| `code_revisions` | Repository fingerprint, analysis ID, build state, and timestamp |
| `code_files` | Relative path, content hash, and byte size for a revision |
| `code_symbols` | Graph symbol identity, kind, name, signature, span, and metadata |
| `code_chunks` | Exact source chunk content and its symbol/span context |
| `code_edges` | Typed graph relationships, confidence, status, and evidence location |
| `code_chunk_vectors` | Persisted local sparse vector per chunk |
| `code_chunks_fts` | SQLite FTS5 index over paths, names, contextual text, and content |

Relevant implementation: `backend/app/agent/retrieval.py:76-190`.

### 4.2 Analysis sessions

The complete analysis report and set of allowed source paths are also stored in the `analysis_sessions` table. The store maintains a bounded in-memory cache for speed and falls back to SQLite after a backend restart. The persisted revision fingerprint connects the session to repository freshness.

Relevant implementation: `backend/app/graph/store.py:107-155` and `backend/app/graph/store.py:224-305`.

### 4.3 Other durable product state and retention

Onboarding and issue-workspace state persist in the same SQLite database, not only the analysis/index/conversation tables above: `onboarding_tours` backs `TourStateStore` (tour plans and per-step mastery), and `issue_findings` backs `FindingStore` (Waypoint-generated issue proposals, kept separate from live GitHub issue history, which is fetched on demand with no local durable cache).

Bounded in-memory caches sit in front of SQLite for speed: five most-recently-updated analyses, 200 conversations, three revisions per repository with ten active repository indexes, and 100 tours — each falls back to a SQLite read on a cache miss (e.g. after a restart), so nothing here is a durability boundary, only a performance one. `GraphQueryService` (`backend/app/graph/store.py`) answers graph-neighborhood questions (BFS traversal for `expand_graph`/`get_symbol_relationships`) over the in-memory deserialized report rather than querying `code_edges` directly with SQL — see [`roadmap.md`](roadmap.md) for the still-outstanding recommendation to move this to indexed SQL traversal.

### 4.4 The “vector” layer

Waypoint does have a vector retrieval component, but it is local and deterministic rather than a hosted neural embedding model. `LocalCodeVectorizer` builds a 768-dimensional sparse vector from identifiers, case/underscore-separated words, simple suffix variants, and hashed character trigrams. Similarity is a sparse dot product.

Consequences:

- no code is sent to an external embedding provider;
- indexing has no embedding API cost;
- revisions can reuse persisted vectors;
- fuzzy identifier matching is better than exact keyword search alone;
- semantic understanding is weaker than a high-quality learned code embedding model.

The current architecture therefore does not reject vectors. It combines lightweight local vectors with lexical and graph evidence. A learned embedding service could later replace or complement `LocalCodeVectorizer` without changing the agent/tool boundary.

Relevant implementation: `backend/app/agent/retrieval.py:29-73` and the `code_chunk_vectors` table at lines 166-173.

## 5. How the AI accesses the repository

The AI does not receive the SQLite database, unrestricted filesystem access, or the entire repository. It receives JSON-schema tool definitions. The application executes a selected tool, returns bounded JSON evidence, and records the exact returned spans as inspected evidence.

### 5.1 Retrieval fusion

`RepositoryRetrievalIndex.search()` combines:

1. SQLite FTS5 lexical candidates;
2. local sparse-vector candidates;
3. direct token, exact phrase, path, and qualified-name scoring;
4. symbol-kind preferences;
5. test-file penalties unless tests were requested;
6. one-hop graph context around the strongest symbol-backed results;
7. path diversity for longer queries.

`search_repository`'s `limit` field defaults to 12 results and accepts up to 50 per call (`SearchInput`, `backend/app/agent/tool_contracts.py:25-28`); the model can request more evidence in one call when it needs to, but the practical default keeps prompt evidence volume small. Results contain exact repository-relative paths and line spans. Source reads independently check that the path belongs to the analyzed source set, remains under the repository root, is a regular non-symlink file, and remains under the file-size limit.

Relevant implementation:

- `backend/app/agent/retrieval.py:553-576` — safe source access.
- `backend/app/agent/retrieval.py:700-881` — filtered hybrid ranking and graph expansion.

### 5.2 Internal agent tools

The Ask agent currently defines 18 tools in Python dictionaries in `backend/app/agent/service.py`:

- low-level retrieval/graph tools: `list_repository_tree`, `search_repository`, `find_symbols`, `get_index_status`, `read_source`, `inspect_symbol`, and `expand_graph`;
- high-level semantic tools: `get_repository_overview`, `get_feature_evidence`, `find_entry_points`, `get_backend_architecture`, `get_file_structure`, `get_symbol_relationships`, `find_related_tests`, `get_dependency_impact`, `get_project_configuration`, and `get_analysis_diagnostics`;
- final structured-output tool: `submit_answer`.

Every tool has a name, description, JSON input schema, bounds, and `additionalProperties: false`. `RepositoryAgentService._execute_tool()` dispatches the approved name to application code. The model cannot invent a new executable operation merely by returning another name.

Relevant implementation:

- `backend/app/agent/service.py:82-345` — schemas and the final assembled tool list.
- `backend/app/agent/service.py:395+` — tool dispatch.
- `backend/app/agent/semantic.py` — implementations of higher-level repository operations.

### 5.3 Specialized workflow tool sets

Onboarding, contribution missions, and issue investigation reuse the repository tools but replace the generic final submission operation with a workflow-specific structured tool:

- `TOUR_TOOLS` and `submit_onboarding_tour` — `backend/app/agent/onboarding.py:135-152`;
- `MISSION_TOOLS` and `submit_contribution_mission` — `backend/app/agent/onboarding.py:156-232`;
- `FINDING_TOOLS` and `submit_issue_findings` — `backend/app/agent/issues.py:95-106`.

### 5.4 MCP server tools

The MCP interface is separately defined with `@mcp.tool()` decorators in `backend/app/mcp_server.py`. It currently exposes **30 tools**, 2 resources, and 1 prompt (verified directly by counting `@mcp.tool()`/`@mcp.resource()`/`@mcp.prompt()` decorators — this count moves as the connector grows, so treat it as a snapshot, not a permanent fact). It covers secured local/GitHub analysis, tree/search/read, symbol and graph inspection, semantic architecture operations, index administration, onboarding/mission/challenge operations, issue history and investigation, model/retrieval comparison, and the complete repository Ask agent.

The MCP server is a connector over the same analysis, index, and semantic service layer. It does not duplicate the parser or maintain a different repository representation.

### 5.5 Skills versus tools: current truth

There is no runtime `SKILL.md` file that teaches the repository agent its capabilities. In this application:

- tools are Python-defined JSON schemas plus Python dispatch functions;
- prompts describe how the model should choose those tools;
- onboarding, missions, Ask, and issue investigation are specialized agent workflows/tool bundles.

Those workflows are “skill-like” in a general agentic-design sense, but they are not literal portable skill packages. If a project rubric specifically requires a formal Skill primitive rather than specialized tools and prompts, this remains a documentation/implementation gap and should not be claimed as complete merely because the tool bundles exist.

## 6. Memory layer

Waypoint integrates persistent, bounded conversation memory using the same SQLite state database.

`ConversationStore` maintains:

- `conversations`: conversation ID, analysis ID, channel, and timestamps;
- `conversation_messages`: ordered `user` and `assistant` messages plus serialized answer JSON.

Conversations are scoped by analysis and channel, so Ask and other experiences can retain separate histories for the same repository. Before a model request, `history()` loads the most recent `chat_history_turns * 2` messages and returns them in chronological order. After a successful response, `append_turn()` stores both sides of the turn and prunes older messages.

The default memory window is 12 user/assistant turns, configurable from 1 to 50 with `WAYPOINT_CHAT_HISTORY_TURNS`. Because the messages are persisted in SQLite, history survives a backend restart as long as the same state database and analysis session remain available.

Relevant implementation:

- `backend/app/agent/memory.py:20-52` — schema and connection.
- `backend/app/agent/memory.py:128-176` — latest conversation and transcript/history reads.
- `backend/app/agent/memory.py:178-208` — append and prune.
- `backend/app/config.py:183-185` — bounded history configuration.
- `backend/app/agent/service.py:595-603` — conversation retrieval before an Ask run.

This is short-term conversational memory, not autonomous long-term learning. It does not alter model weights, build a user profile, or preserve unlimited chat. Repository analysis sessions and indexes are persistent state, but they are repository memory rather than conversational memory.

## 7. Primary and secondary AI split

There are two useful meanings of “primary/secondary split” in this system.

### 7.1 Model-role split: investigation and synthesis

The configurable dual-model architecture separates two responsibilities:

| Role | Responsibility | Tool behavior |
|---|---|---|
| Investigation | Locate relevant files, inspect source, traverse graph relationships, and gather evidence | Receives repository investigation tools; in dual mode it cannot finalize the answer |
| Synthesis | Turn inspected evidence and conversation context into the final structured response | Is forced to use the workflow's submission tool and must cite inspected evidence |

As of the 2026-07-29 redesign, investigation and synthesis are **not** independently routed to potentially different providers. Both roles share one provider cascade per conversation (`ModelProviderRouter._create_dual_cascade`): Bedrock, then the Anthropic API, then Bedrock retried, then the Claude Code subscription fallback — the same tier order as single-model mode. `_role_model_for_provider(role, provider)` is a pure lookup of which *model name* to request for a role once a tier is already chosen (e.g. `us.anthropic.claude-sonnet-4-6` for investigation vs. `us.anthropic.claude-opus-4-6-v1` for synthesis on the Bedrock tier); it never influences which tier is picked.

This replaced an earlier design where each role ran its own independent cascade via separate `WAYPOINT_INVESTIGATION_PROVIDER`/`WAYPOINT_SYNTHESIS_PROVIDER` settings. That design allowed investigation and synthesis to land on genuinely different providers within one conversation — since both roles share one `messages` history, a later round served by a different provider than an earlier round crashed the real Anthropic SDK trying to serialize Bedrock's `SimpleNamespace`-shaped response objects left over from an earlier round (Bedrock's adapter fabricates these because Bedrock returns raw dicts, not SDK objects). The new design makes that crash impossible by construction rather than patching the serialization: there is only ever one provider per conversation, resolved once and reused.

Each multi-round conversation (`agent/service.py`'s `answer()`, `onboarding.py`'s tour and mission loops, `issues.py`'s `investigate()`) tracks which tier its first round resolved to and passes it back in as `_waypoint_conversation_provider` on every later round, so the cascade prefers that same tier — while still falling through normally if that preferred tier itself fails on a later call, so a genuine mid-conversation outage doesn't get stuck retrying a dead tier. `investigation_provider`/`synthesis_provider` still exist as settings, but only `comparison.py`'s separate `/answer/compare` benchmarking feature (`create_for_endpoint`) reads them now; they no longer affect live Ask/onboarding/issue routing.

Dual-role routing is enabled only when `WAYPOINT_MODEL_ARCHITECTURE` is `dual`, `two-model`, or `two_model`; the default configuration is `single`. See [`model-routing.md`](../model-routing.md) for the full current picture (settings, worked example, and the reasoning behind this design); the root `issues.md` log has the incident history behind why it evolved this way.

In the Ask loop, the investigation phase lasts for the configured `WAYPOINT_INVESTIGATION_ROUNDS` (default 3) before a forced synthesis phase, subject to the global tool-round bound. In single mode one selected provider handles the whole loop, although the service still transitions from free investigation to forced submission near the end.

Relevant implementation:

- `backend/app/config.py:68-80` and `backend/app/config.py:147-182` — architecture, endpoints, and defaults.
- `backend/app/agent/provider.py` — `_role_model_for_provider`, `create_for_role`, `_create_dual_cascade`.
- `backend/app/agent/service.py:646-711` — role transition and tool restrictions.

### 7.2 Feature split

At the product level, the primary AI capability is grounded repository understanding: Ask plus role/objective-specific onboarding routes. Secondary AI capabilities build on the same evidence layer:

- safe first-contribution missions;
- repository issue synthesis;
- assessments of onboarding comprehension;
- cross-model comparison/benchmarking.

These are not separate parsers or separate RAG systems. They reuse the same graph, chunks, retrieval index, memory conventions, and source validation while changing the prompt, final schema, and workflow-specific checks.

## 8. Output validation and retry loops

Waypoint uses several validation layers rather than trusting model prose directly.

### 8.1 Tool input constraints

Tool schemas bound lists, strings, result counts, graph depth, line ranges, and enum values, and generally reject unknown properties. These constraints reduce malformed calls before application dispatch.

### 8.2 Pydantic final schemas

Ask output must validate as `SubmittedAnswer`. It requires non-empty bounded answer and basis fields, permits at most 30 citations and five suggested questions, and requires positive citation line numbers. Onboarding tours, missions, assessments, and issue findings have their own Pydantic models and field validators.

Relevant implementation:

- `backend/app/agent/service.py:52-65` — Ask submission schema.
- `backend/app/agent/onboarding.py` — tour, mission, and assessment schemas.
- `backend/app/agent/issues.py` — issue-finding schema.

### 8.3 Evidence/citation validation

Each retrieval result or source read with a path and line range is registered as an `InspectedSpan`. At final submission, every citation must:

- have `end_line >= start_line`;
- refer to a real indexed source path;
- be fully contained inside evidence the agent inspected.

The backend rereads the accepted range itself and constructs the client-facing citation. Thus, a model cannot validate a citation simply by mentioning a plausible filename.

Relevant implementation: `backend/app/agent/service.py:485-564`.

### 8.4 Bounded evidence recovery

If a proposed citation is real but was not registered in the inspected-span set, `_recover_answer_evidence()` performs bounded source reads for at most 20 proposed ranges and then runs the full deterministic validator once more. The safe reader still rejects unknown, escaping, symlinked, missing, oversized, or invalid ranges. This is recovery of verifiable evidence, not automatic acceptance of model claims.

Relevant implementation: `backend/app/agent/service.py:566-592` and `backend/app/agent/service.py:808-850`.

### 8.5 Agent loop and retry limits

The Ask agent runs a bounded model/tool loop:

1. Load recent conversation memory.
2. Ask the investigation model to select one or more tools.
3. Execute only known tools and add their results to the message history.
4. Record inspected spans and tool activity.
5. At the configured cutoff, switch to synthesis and force the final submission tool.
6. Validate the submission.
7. If validation fails, return a structured tool error to the model so it can correct the output within the remaining rounds.
8. Fail explicitly if the round or synthesis-attempt limit is exhausted.

Defaults and hard configuration bounds:

| Control | Default | Allowed configuration range | Purpose |
|---|---:|---:|---|
| `WAYPOINT_AGENT_MAX_TOOL_ROUNDS` | 8 | 1-20 | Maximum outer model/tool iterations |
| `WAYPOINT_INVESTIGATION_ROUNDS` | 3 | 1-10 | Evidence-gathering rounds before synthesis in dual mode |
| `WAYPOINT_SYNTHESIS_MAX_ATTEMPTS` | 2 | 1-5 | Maximum final-answer attempts |
| `WAYPOINT_AGENT_MAX_OUTPUT_TOKENS` | 6,000 | 512-32,000 | Per-model-response output ceiling |
| `WAYPOINT_CLAUDE_CODE_MAX_TURNS` | 8 | 1-8 | Internal Claude Code SDK turn ceiling per provider call |

The outer tool rounds and Claude Code SDK turns are different limits. One Waypoint agent round makes one provider request; a Claude Code-backed provider request may internally consume multiple SDK turns. Neither loop is unlimited.

Relevant implementation:

- `backend/app/config.py:163-190` — all limits.
- `backend/app/agent/service.py:646-769` — round control, forced submission, and missing-tool correction.
- `backend/app/agent/service.py:771-850` — submission validation feedback and retry.
- `backend/app/agent/service.py:991` — explicit round-exhaustion failure.

### 8.6 Provider failure behavior

A provider exception is logged and converted to an `AgentExecutionError`; it is not silently treated as an answer. The `auto` provider route has a narrow fallback from Anthropic API to Claude Code for recognized billing/capacity failures. It is not a general retry of arbitrary errors. OpenRouter and explicitly selected provider routes fail through their own client errors unless a higher workflow deliberately retries them.

## 9. Observability of this pipeline

Structured logs exist around repository discovery, per-file parsing, index restoration/publication, retrieval candidate generation, every agent round, provider routing, model responses, tool calls/results, validation failures, and completion. The `@traced(...)` decorator supplies pre-call, return, duration, and exception hooks for decorated functions. JSONL trace output defaults to `.waypoint-data/traces/waypoint.jsonl`.

Relevant implementation:

- `backend/app/observability.py:98+` — diagnostic formatting.
- `backend/app/observability.py:233+` — structured event logging.
- `backend/app/observability.py:285+` — function tracing decorator.
- `backend/app/config.py:98-108` — trace controls and default path.

These logs expose model requests/responses, tool choices, arguments, results, timing, token usage when supplied by the provider, and validation outcomes. They do not and should not claim to expose private hidden chain-of-thought. The traceable reasoning artifact is the observable tool/evidence trajectory and final rationale.

## 10. Current limitations and honest boundaries

- Static parsing cannot prove runtime dispatch, reflection, dependency injection, dynamic imports, generated code, or data-dependent calls. Such relationships should remain possible/unresolved rather than definite.
- The local sparse vectorizer supports fuzzy lexical similarity, but it is not equivalent to a learned neural code embedding model.
- Conversation memory is a bounded transcript, not semantic long-term memory or user-model personalization.
- HTML and CSS are parsed and indexed, but their relationship extraction is necessarily shallower than Python/Java/JS/TS application semantics.
- The runtime agents use tools and specialized workflows, not a literal `SKILL.md` skill package.
- Dual-model capability is configurable, but it only operates as a two-role system when `WAYPOINT_MODEL_ARCHITECTURE` is explicitly set to a dual value and at least one provider tier in the shared cascade is usable; both roles always share whichever single tier the cascade resolves to (§7.1).
- Retry loops are intentionally bounded. Exhaustion produces an error instead of silently accepting invalid or unsupported output.

## 11. Primary implementation map

| Concern | Main files |
|---|---|
| Settings and limits | `backend/app/config.py` |
| Snapshot/fingerprint | `backend/app/indexing.py` |
| Python parser and graph construction | `backend/app/graph/analyzer.py` |
| JS/TS/Java/HTML/CSS parsing | `backend/app/graph/polyglot.py` |
| Graph models | `backend/app/graph/models.py` |
| Analysis-session persistence/querying | `backend/app/graph/store.py` |
| Chunking, SQLite index, FTS/vector/graph retrieval | `backend/app/agent/retrieval.py` |
| High-level semantic repository operations | `backend/app/agent/semantic.py` |
| Ask tools, loop, validation, citations | `backend/app/agent/service.py` |
| Conversation memory | `backend/app/agent/memory.py` |
| Provider and model-role routing | `backend/app/agent/provider.py` |
| Onboarding and mission workflows | `backend/app/agent/onboarding.py` |
| Issue synthesis workflow | `backend/app/agent/issues.py` |
| MCP connector | `backend/app/mcp_server.py` |
| Hooks and structured logs | `backend/app/observability.py` |

## 12. How unit and integration tests are performed

Waypoint's deterministic backend suite uses `pytest`. Test classification is executable rather than merely descriptive: `backend/tests/conftest.py` assigns every test module to exactly one of two marker groups and raises a collection error if a new module has not been classified.

The test environment is isolated before application modules are imported. The configuration fixture points `WAYPOINT_STATE_PATH` and the trace path at a temporary directory, disables persistent trace output, and disables the live provider startup probe. Therefore ordinary unit/integration runs do not overwrite development sessions or spend model credits.

### 12.1 Unit tests

Unit tests isolate an algorithm, parser, policy, formatter, or provider adapter. They use temporary repositories and mocks instead of a running HTTP server, live GitHub access, or a live model.

Current unit modules and responsibilities:

| Test module | Isolated behavior |
|---|---|
| `backend/tests/test_analyzer.py` | Python AST definitions, imports, calls, evidence, limits, and path safety |
| `backend/tests/test_polyglot_analyzer.py` | Java/JS/TS/TSX/HTML/CSS extraction and relationship resolution |
| `backend/tests/test_provider.py` | Provider payload translation, response/usage mapping, routing, and error handling |
| `backend/tests/test_questions.py` | Deterministic onboarding question construction and scoring |
| `backend/tests/test_repository_import.py` | GitHub URL policy, checkout accounting, clone limits, and failure paths |
| `backend/tests/test_observability.py` | Pre/post/error hooks, structured formatting, redaction, and value bounding |
| `backend/tests/test_issues.py` | Issue transformation and deterministic issue-service behavior |

Run unit tests only:

```powershell
.\.venv\Scripts\python.exe -m pytest -m unit --no-cov
```

The documented verified result is **40 passed and 33 deselected**. `--no-cov` is acceptable for this focused debugging command, but not for the release coverage run.

### 12.2 Integration tests

Integration tests cross real application boundaries while substituting deterministic typed model fixtures for paid/network model calls.

| Test module | Boundaries exercised |
|---|---|
| `backend/tests/test_agent.py` | analysis -> index -> tools -> model fixture -> citation validation -> conversation memory |
| `backend/tests/test_api.py` | FastAPI route -> analysis session -> graph/source/index service |
| `backend/tests/test_mcp_server.py` | MCP operation -> shared parser/index/semantic service |
| `backend/tests/test_model_comparison.py` | frozen evidence -> two model fixtures -> independent validation and metering |
| `backend/tests/test_onboarding.py` | graph/index -> route, mission, challenge, and model workflows |
| `backend/tests/test_semantic_tools.py` | graph/index -> architecture, entry-point, relationship, and test tools |

Run integration tests only:

```powershell
.\.venv\Scripts\python.exe -m pytest -m integration --no-cov
```

The documented verified result is **35 passed and 40 deselected**.

### 12.3 Full deterministic backend suite

The release command runs both classifications and enforces coverage:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

This count grows steadily; treat any specific number here as a snapshot, not a permanent fact. The most recently verified count (2026-07-29, after the dual-role provider redesign in `provider.py` added new cascade tests) was **128 passed**. See [`benchmarks/test-suite-and-coverage.md`](../benchmarks/test-suite-and-coverage.md) for the fuller per-module breakdown, or run the command yourself for the current number. Live-provider evaluations are intentionally outside this command because quota, latency, and model behavior would make the coverage gate nondeterministic.

Relevant files:

- `pyproject.toml` — pytest markers, default command options, coverage configuration, and threshold.
- `backend/tests/conftest.py` — state isolation and mandatory module classification.
- `verification/tested-evidence/README.md` — test inventory and boundary definitions.

## 13. How end-to-end testing is performed

Waypoint has two different E2E surfaces because an HTTP service and a browser UI can fail independently.

### 13.1 Real HTTP E2E

`verification/scripts/http_flask_e2e.py` drives a separately running Waypoint backend with real `httpx` requests. Its reference repository is Pallets Flask: a known, moderate-sized Python project with a `src/` layout, application classes, CLI behavior, decorators, routing, and tests.

The workflow verifies:

1. a real GitHub import or existing local Flask checkout;
2. at least 60 parsed files and no more than three parse failures;
3. known Flask modules such as `src/flask/app.py`;
4. persistent analysis identity and summary retrieval;
5. a complete index whose chunk and vector counts agree;
6. bounded source reading;
7. deterministic architecture results;
8. lookup of the known `flask.app.Flask` class;
9. symbol-usage and graph-neighborhood endpoints.

Start the backend first, then run:

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.http_flask_e2e
```

To avoid another GitHub clone, supply a Flask checkout that is already under `ONBOARD_ALLOWED_ROOT`:

```powershell
.\.venv\Scripts\python.exe -m verification.scripts.http_flask_e2e `
  --repository-path ".waypoint-clones\pallets--flask--YOUR_ID"
```

The script prints the status and duration of every HTTP call and writes `verification/results/http-flask-e2e.json`. It does not call an LLM; it proves the process-level repository pipeline independently of provider availability.

### 13.2 Browser E2E

Playwright tests live in `frontend/e2e`. The current scenarios verify repository analysis/navigation/theme persistence and a repository-overview answer with ten inspectable evidence-backed features. Model endpoints are mocked for deterministic UI behavior, while the browser still exercises the frontend's actual rendering and API interaction logic.

Run the browser suite with:

```powershell
npm.cmd --prefix frontend run test:e2e
```

The Playwright configuration uses Chrome, one worker, a 60-second test timeout, a default base URL of `http://127.0.0.1:8766`, retained traces on failure, failure screenshots, and an HTML report. One worker avoids collisions in shared analysis/session state.

Relevant files:

- `frontend/playwright.config.ts`
- `frontend/e2e/application.spec.ts`
- `frontend/e2e/voicebox.acceptance.spec.ts`
- `verification/http-e2e/README.md`

### 13.3 Live model E2E is a separate evaluation

`verification/scripts/live_multistep_demo.py` runs the agent against a live provider, asks a follow-up in the same conversation, and extracts the relevant tool/model events from the JSONL trace. It requires explicit confirmation because it can spend credits. This is behavioral evaluation rather than coverage input.

## 14. Coverage measurement and baseline

### 14.1 Whole-backend coverage gate

The default pytest command instruments `backend.app` with `pytest-cov` and coverage.py. Branch measurement is enabled. It produces:

- terminal missing-line output;
- Cobertura XML at `.waypoint-data/coverage.xml`;
- annotated HTML at `.waypoint-data/coverage-html/index.html`;
- a failing process exit when combined coverage is below 70%.

The gate is configured in `pyproject.toml`:

```toml
addopts = "-ra --strict-markers --cov=backend.app --cov-report=term-missing --cov-report=xml:.waypoint-data/coverage.xml --cov-report=html:.waypoint-data/coverage-html --cov-fail-under=70"
```

The enforcement value is coverage.py's combined executable-statement and branch result. The XML also exposes line and branch rates independently; neither independent percentage replaces the command's combined gate.

### 14.2 Core-feature coverage gate

The project requirement concerns coverage of core business logic, so the whole-backend number is not the only proof. `verification/core-features.json` declares five reviewable core areas before reporting their results:

1. repository analysis and graph construction;
2. revision-aware indexing and graph retrieval;
3. grounded answering, validation, comparison, and memory;
4. adaptive onboarding routes and missions;
5. secure repository import.

`verification/scripts/core_coverage_report.py` reads the coverage XML and fails unless **every core feature** and the weighted core aggregate are at least 70%. This prevents one highly covered module from hiding a failing core feature.

Run and regenerate the two readable reports with:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m verification.scripts.core_coverage_report
.\.venv\Scripts\python.exe -m verification.scripts.coverage_report
```

### 14.3 Established baseline

This is a historical snapshot, preserved rather than overwritten (consistent with how [`benchmarks/history.md`](../benchmarks/history.md) treats every run as append-only evidence). For the most recently regenerated numbers, see [`benchmarks/test-suite-and-coverage.md`](../benchmarks/test-suite-and-coverage.md) (2026-07-28: 111 backend tests, 74.94% combined coverage, 82.91% weighted core aggregate) or regenerate both reports yourself with the commands below. The documented July 24, 2026 baseline is:

| Measurement | Baseline | Gate | Status |
|---|---:|---:|---|
| Backend deterministic tests | 75 passed | All must pass | PASS |
| Whole-backend combined branch-aware coverage | 75.51% | 70% | PASS |
| Independent line coverage | 79.0% | Reported diagnostic | Measured |
| Independent branch coverage | 62.5% | Included in combined calculation | Measured |
| Weighted core-feature aggregate | 82.84% | 70% | PASS |
| Lowest individual declared core feature | 79.14% | 70% | PASS |

The exact baseline belongs to a specific XML artifact and source revision. It is not permanent: every release must regenerate it. The authoritative readable result is `verification/results/coverage-report.md`; the core definition is `verification/core-features.json`.

The baseline was established by:

1. identifying the product's core features and their owned modules;
2. setting the required threshold to the rubric's 70%;
3. running the complete deterministic unit/integration suite with branch coverage;
4. recording the generated XML rather than estimating from test counts;
5. calculating both the whole-backend and per-core-feature results;
6. failing the build if either gate falls below its threshold.

## 15. Encountered error and correction ledger

This is the complete error/correction set recoverable from the maintained prompt history, verification artifacts, and current implementation. It should not be represented as a perfect historical incident log: early work was not recorded contemporaneously, and some entries in `prompts.md` were explicitly reconstructed. Operational failures that remain unresolved are marked as such.

| Encountered behavior or error | Cause | Correction | Current status / evidence |
|---|---|---|---|
| Ask returned a one-line, repository-count-style response instead of synthesizing an answer | The early Ask path relied on deterministic/static summary behavior rather than a general repository agent | Replaced it with a multi-round tool-using agent, structured `submit_answer`, high-level semantic tools, and citation validation | Resolved in `backend/app/agent/service.py` and `backend/app/agent/semantic.py`; integration coverage in `test_agent.py` and `test_semantic_tools.py` |
| “Top features” retrieval selected irrelevant test symbols | Generic lexical/graph ranking could elevate strongly connected test code without product context | Added repository overview and feature-evidence tools that prioritize documentation, manifests, entry points, and diverse production symbols; test paths are penalized unless requested | Corrected, with a browser acceptance scenario in `frontend/e2e/voicebox.acceptance.spec.ts` |
| Code Journey/answer evidence could be empty or disconnected from the answer | Evidence display was not derived strictly from the final citations | Ask became a split view and opens only files cited by the final answer | Corrected in `frontend/src/components/AskWorkspace.tsx` and source/evidence components |
| Chat disappeared after refresh | Conversation state existed only in frontend component memory | Added SQLite `conversations`/`conversation_messages`, serialized answer JSON, latest-transcript API, bounded history restoration, and frontend rehydration | Resolved in `backend/app/agent/memory.py`, `backend/app/api/routes.py:271+`, and `AskWorkspace.tsx` |
| Previously analyzed repositories could not be reopened easily | Analysis identity was held mainly in an in-memory session cache/UI state | Persisted analysis reports and fingerprints in SQLite and added a recent-session endpoint and selector | Resolved in `backend/app/graph/store.py`, `backend/app/api/routes.py:240+`, and `frontend/src/App.tsx` |
| UI accepted only local paths | No secured remote-import workflow existed | Added public GitHub URL validation, shallow non-interactive cloning, size/file/time limits, retained-clone cleanup, and a UI source selector | Resolved in `backend/app/repository_import.py` and analysis GitHub route tests |
| Analyzer originally covered only Python | Parsing was based solely on Python's AST | Added Tree-sitter JavaScript, TypeScript/TSX, Java, HTML, and CSS grammars mapped into the common graph schema | Resolved in `backend/app/graph/polyglot.py`; tested by `test_polyglot_analyzer.py` |
| Cross-file call/class usage appeared missing or overly definite | Dynamic receivers and aliases cannot always be resolved safely with basic lexical matching | Added import aliases, receiver-type tracking, instantiation edges, cross-file member recovery, and explicit unresolved/`may_call` evidence rather than fabricating certainty | Partially resolved by design; static limits remain documented and tested |
| `may_call` relationships were not visible by default | Frontend edge-filter defaults excluded inferred call edges | Added `may_call` to the default selected edge kinds | Resolved in `frontend/src/App.tsx:136` |
| External package imports had no meaningful file-card representation | External targets are not repository files and therefore had no ordinary source card | Added a synthetic External Packages card connected to all importing files while retaining unresolved dependency meaning | Resolved in `frontend/src/components/GraphView.tsx` |
| Some graph/file cards had no lines | A file can genuinely have no statically resolved cross-file import/call/instantiation edge; early layouts also made some edges visually unclear | Preserved isolated cards when evidence is absent, expanded relationship recovery, and repaired the landing demonstration so all five example cards have visible connections | Evidence behavior is intentional; landing visualization corrected in frontend graph/layout code |
| Graph fullscreen, collapsible panels, and source resizing did not work as expected | Missing/incomplete UI state and pointer/keyboard resize handling | Added graph fullscreen state/Escape handling, collapsible controls, and an accessible row resizer with pointer and keyboard support | Resolved in `frontend/src/App.tsx:385+`, `636+`, `701+`, and `755+` plus styles |
| Ask/onboarding reported “Reached maximum number of turns (1)” and later `(4)` | The Claude Agent SDK's internal `max_turns` was lower than the outer Waypoint agent tool-round allowance; these are separate nested limits | Made the SDK limit configurable, raised its default/cap to 8, kept the Waypoint outer default at 8, and separated investigation from forced synthesis so finalization has reserved attempts | Corrected in `backend/app/config.py:163-187`, `provider.py:145`, and the Ask/onboarding loops. A provider can still exhaust a deliberately lower override |
| Mission/onboarding could exhaust tool rounds before submission | Generic retrieval took too many turns and no finalization phase was reserved | Added precise semantic tools, a forced-submission cutoff, and bounded synthesis attempts; onboarding offers a 120-minute time-budget option | Corrected in `backend/app/agent/onboarding.py`; round exhaustion still fails explicitly rather than returning invalid output |
| Claude API calls failed after credits were exhausted | External billing/quota, not repository logic | Added provider routing across Anthropic API, OpenRouter, and locally authenticated Claude Code; `auto` can fall back to Claude Code for recognized billing/capacity failures | Fallback implemented in `backend/app/agent/provider.py`; availability still depends on subscription/login/quota |
| Initial two-model evidence compared two Claude/Claude iterations rather than all required provider combinations | The first live benchmark exercised only the endpoints then available | Added explicit comparison endpoints and a matrix runner for Claude+Claude, Claude+OpenRouter, and OpenRouter+OpenRouter using frozen identical evidence and per-run metrics | Framework resolved in `verification/scripts/model_comparison_benchmark.py`; live OpenRouter rows remain dependent on OpenRouter quota |
| OpenRouter live runs could not be fully verified when free-model quota/availability failed | Provider-side quota and free-model capacity | Preserved deterministic adapter/comparison tests and made live runs opt-in; the system reports the provider failure instead of inventing benchmark results | **Operationally unresolved until usable OpenRouter quota/model capacity is available** |
| Coverage evidence initially emphasized an overall percentage without proving core-feature coverage | Whole-backend coverage alone did not directly answer the rubric's core-logic requirement | Added `verification/core-features.json`, a per-feature combined line/branch gate, weighted aggregate, and readable Markdown generation | Resolved; every core feature is currently above 70% and the aggregate is 82.84% |
| Coverage XML was difficult to interpret | Cobertura XML is machine-oriented | Added XML-to-Markdown and core-feature report generators plus an annotated HTML report | Resolved in `verification/scripts/coverage_report.py` and `core_coverage_report.py` |
| `prompts.md` was initially too terse and incomplete | Significant development requests and developer corrections were not recorded as an auditable engineering log | Reconstructed major prompts with constraints, corrections, implementation, and validation evidence while excluding prompts the developer explicitly asked not to record | Improved, but reconstructed entries are transparently labeled; this report update does not modify `prompts.md` |

## 16. What kinds of tools the AI has used

“AI tools” can refer to three different layers, which should not be mixed together.

### 16.1 Runtime repository-agent tools available to the model

The Ask agent is offered 18 bounded tools. Their types are:

| Tool type | Examples | Purpose |
|---|---|---|
| Repository discovery | `list_repository_tree`, `get_index_status` | Establish available files and index provenance |
| Hybrid retrieval | `search_repository`, `find_symbols` | Find relevant chunks/symbols using lexical, local-vector, and graph ranking |
| Exact evidence access | `read_source` | Read a bounded source range from an indexed file |
| Graph navigation | `inspect_symbol`, `expand_graph`, `get_symbol_relationships`, `get_dependency_impact` | Follow typed relationships and estimate static impact |
| High-level semantic repository tools | `get_repository_overview`, `get_feature_evidence`, `find_entry_points`, `get_backend_architecture`, `get_file_structure`, `find_related_tests`, `get_project_configuration`, `get_analysis_diagnostics` | Answer common repository questions with fewer, more precise calls |
| Structured completion | `submit_answer` | Return a schema-validated, cited final answer |

Onboarding, missions, and issue investigation inherit the investigation tools and replace `submit_answer` with their own schema-specific submission operation.

### 16.2 Tools actually observed in the stored live multi-step trace

The trace at `verification/results/live-multistep-trace.jsonl` contains 13 observable agent tool calls:

| Observed tool | Calls | Why it was used |
|---|---:|---|
| `read_source` | 4 | Inspect exact source windows selected during investigation |
| `submit_answer` | 4 | Submit candidate/final structured answers for validation |
| `get_repository_overview` | 2 | Establish repository-wide product and architecture context |
| `search_repository` | 1 | Retrieve query-relevant chunks |
| `find_related_tests` | 1 | Locate tests relevant to the investigated behavior |
| `get_backend_architecture` | 1 | Obtain a bounded architecture-layer view |

This table is evidence of actual use for that recorded demo, not a claim that every available tool has been exercised by a live model.

### 16.3 MCP tools

The MCP connector offers 30 server-side tools plus 2 resources and 1 prompt (§5.4). These include repository import/analysis, search/read, symbol/graph operations, architecture operations, index administration, onboarding/mission/issue operations, model/retrieval comparison, and an `ask_repository` convenience operation. MCP clients invoke the same constrained service layer as the web application.

### 16.4 AI-assisted development tools

The maintained engineering log identifies AI coding assistance for scaffolding, implementation, refactoring, test authoring, verification, and documentation. The concrete development operations used in this workspace include:

- repository/file discovery and text search;
- read-only source inspection;
- patch-based file editing;
- PowerShell command execution;
- pytest unit/integration/coverage runs;
- TypeScript compilation and Vite production builds;
- Playwright browser tests;
- HTTP verification scripts;
- Docker Compose configuration validation;
- trace and benchmark artifact inspection.

These development tools are separate from the tools the deployed Waypoint model can call. The deployed model cannot run arbitrary shell commands, patch an analyzed repository, or execute its code through the Ask tool registry.
