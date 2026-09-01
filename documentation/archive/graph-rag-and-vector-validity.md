> **Archived, superseded.** This FAQ's core clarification ("is Waypoint RAG over
> graph nodes alone?") is now folded into
> [`../architecture/system-architecture.md`](../architecture/system-architecture.md)'s
> executive summary. Its edge-kind list (`contains`/`imports`/`may_call`/`instantiates`,
> citing `backend/app/graph/models.py` directly) was the evidence used to correct a
> factual error in the main architecture report on 2026-07-29 — this file was right,
> the report previously wasn't. Kept here for history only.

# Graph nodes and vector validity

## Is Waypoint agentic RAG over graph nodes?

Waypoint is agentic hybrid RAG over several evidence channels, not graph nodes alone.
The model can choose repository-search, bounded source-reading, symbol, graph, semantic,
test, configuration, and diagnostic tools over multiple rounds. Retrieved results are
added to its conversation before it synthesizes an answer.

The evidence channels are:

1. Parser-derived graph nodes and typed edges.
2. Symbol-aware source chunks.
3. SQLite FTS5/BM25 lexical retrieval.
4. Deterministic local subword-vector retrieval.
5. Exact path, qualified-name, symbol-kind, and test-aware ranking signals.
6. Bounded graph-neighbor expansion around strong retrieved symbols.

## What are the graph nodes?

`backend/app/graph/models.py:9-15` defines five node kinds:

| Node | Meaning |
|---|---|
| `repository` | The analyzed repository as the graph root. |
| `module` | An analyzed source file/module. |
| `class` | A parser-discovered class or interface-like type. |
| `function` | A module-level or nested function. |
| `method` | A callable owned by a class. |

Nodes carry a stable ID, kind, short name, qualified name, module, source span,
signature, and metadata. They are static source-code entities, not runtime objects.
A runtime-like construction such as `UserService()` is represented as an
`instantiates` edge to the class node rather than a separate object-instance node.

The typed edges are:

- `contains`: repository/module/class lexical ownership;
- `imports`: one internal module imports another;
- `may_call`: a statically established or conservatively inferred possible call;
- `instantiates`: a symbol constructs a class.

Every edge includes exact syntax, a source range, a resolution explanation, confidence,
and verified/inferred status. Unresolved references remain separate records rather than
being converted into invented edges.

## Why vector embeddings are not invalid

The premise needs correcting: vector retrieval is valid and useful. What is invalid is
treating vector similarity as proof of a code relationship.

A vector can find code whose wording resembles a question. It cannot by itself prove
that function A calls B, module A imports B, or an object is constructed on a particular
line. Typed graph evidence answers those structural questions.

Waypoint currently uses `LocalCodeVectorizer` at
`backend/app/agent/retrieval.py:29-73`. It is not a learned neural embedding model. It
creates a sparse 768-dimensional vector from normalized identifiers, suffix variants,
and hashed three-character subwords. This provides cheap, deterministic fuzzy matching
for related word forms, but only limited synonym/concept understanding.

Learned embeddings would be a valid future addition for queries such as “where is
identity verification handled?” when the code consistently says “authentication.” They
should augment rather than replace:

- exact lexical retrieval;
- parser-derived symbols and spans;
- typed graph relationships;
- final source citation validation.

## Related implementation

- `backend/app/graph/models.py:9-81` — node, edge, evidence, and unresolved models.
- `backend/app/graph/analyzer.py:769-989` — Python analysis and graph assembly.
- `backend/app/graph/polyglot.py:162-870` — JavaScript, TypeScript, and Java analysis.
- `backend/app/agent/retrieval.py:29-73` — local subword vectors.
- `backend/app/agent/retrieval.py:728-866` — hybrid ranking and structural expansion.
- `backend/app/agent/service.py:618-904` — agentic evidence loop and validation.

