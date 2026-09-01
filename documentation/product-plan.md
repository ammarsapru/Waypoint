# Adaptive Codebase Onboarding — Differentiator Build Plan

## Implementation status (July 2026)

The evidence graph, source viewer, symbol exploration, file-centric graph,
repository retrieval index, agent-backed Ask workspace, adaptive onboarding,
mastery checks, contribution missions, and Issues workspace are implemented.
Ask conversations, analysis sessions, repository indexes, onboarding mastery,
and issue proposals persist locally in SQLite. The same bounded repository
capabilities are exposed through a 30-tool MCP connector (see
[`agent-and-mcp-tools.md`](agent-and-mcp-tools.md)), and correlated
pre/post/error hooks write terminal and rotating JSONL traces.

When `ANTHROPIC_API_KEY` is configured, Claude investigates the repository with
bounded search, source-reading, symbol, and graph tools. The backend accepts only
citations to source ranges the model actually inspected. Without a key, Ask and
other model-required operations return an explicit configuration error rather
than pretending a template was synthesized by a model; deterministic graph
views and baseline onboarding remain available.

The Explore workspace now has two complementary projections:

- **Symbols:** the original repository/module/class/function/method nodes and
  filters remain available.
- **File cards:** each source file is one card containing its analyzed module,
  classes, functions, and methods. Cross-file imports and possible calls are
  aggregated into labeled connections, and each card lists the external symbols
  it uses.

GitHub issue history and Waypoint-generated issue proposals are deliberately
separate. Python, JavaScript/JSX, TypeScript/TSX, and Java now share the same
evidence graph. Runtime overlays, additional languages, collaborative tenancy,
and arbitrary repository execution remain future work behind the scope gates below.

### Retrieval decision

Use a hybrid evidence pipeline rather than allowing the model to crawl files
blindly:

1. Build a symbol-aware lexical index once per analysis.
2. Retrieve candidates using exact text, symbol names, paths, and graph
   neighborhoods.
3. Let the model request bounded source ranges through tools.
4. Validate every final citation against inspected ranges.
5. Add embeddings and a reranker behind the same retrieval interface only after
   evaluation repositories demonstrate a recall gap.

This keeps exact identifier lookup strong, limits cost and context size, and
prevents an embedding result from being treated as source evidence by itself.

## Product position

This product does not compete by being another repository chat box or dependency
diagram. It builds, tests, and continuously updates a developer's mental model of
a codebase, then guides that developer toward a safe first contribution.

Core promise:

> Understand this codebase for my role, prove that I understand it, and show me
> the safest useful thing I can do next.

## Product principles

1. **Evidence before confidence.** Structural facts come from source evidence.
   Inferences are labeled with confidence and can be inspected.
2. **Personalized, not generic.** Tours vary by role, objective, experience, and
   demonstrated understanding.
3. **Active learning, not passive narration.** The product checks comprehension
   and adapts the route.
4. **A contribution is the finish line.** Onboarding ends in a grounded,
   achievable first-contribution mission.
5. **Static and runtime truth stay distinct.** Possible execution paths must not
   be presented as observed runtime paths.
6. **Every operation is diagnosable.** Requests, functions, model calls, parsing
   decisions, and subprocesses share correlation IDs and emit structured logs.

## Differentiator tracks

### D1 — Adaptive role- and goal-based tours

Inputs:

- Role: backend, frontend, QA, security, product, incident response
- Goal: learn the system, investigate a symptom, change a feature, review risk
- Experience: language/framework familiarity and desired depth
- Time budget: five-minute overview through deep onboarding

Output is a structured route whose steps cite graph nodes and source spans.
Changing the persona must produce a materially different route.

Acceptance:

- At least three personas produce visibly different routes for one repository.
- Every step has a learning objective, evidence, explanation, and completion rule.
- The route can be regenerated after a mastery update without repeating mastered
  material unnecessarily.

### D2 — Comprehension loop and mastery map

Each tour section may end with one of:

- Select the responsible node.
- Put an execution path in order.
- Predict which dependency is touched next.
- Explain a boundary using cited source.
- Identify an unsupported inference.

The system stores mastery per concept, not just a completed-tour boolean.

Acceptance:

- Answers are evaluated against deterministic graph evidence where possible.
- Incorrect answers change the next route.
- The UI distinguishes unvisited, introduced, practiced, and mastered concepts.

### D3 — Personalized first-contribution mission

The planner proposes a low-risk task using graph centrality, test proximity,
change surface, and the user's mastered concepts.

Mission output:

- Goal and user value
- Why it is suitable for this developer
- Expected files and symbols
- Evidence-backed blast radius
- Existing test or implementation pattern to follow
- Risks, non-goals, and definition of done

The MVP proposes and explains a task; it does not modify or execute untrusted
code.

### D4 — Evidence and uncertainty mode

Relationships are visually and structurally classified:

- `verified`: directly supported by syntax or runtime observation
- `inferred`: static resolution with explicit assumptions
- `unresolved`: evidence exists but the target cannot be established

Clicking a node or edge exposes its exact file and source span. AI annotations
remain separate from parser facts.

Acceptance:

- No inferred Python call is represented as a verified runtime call.
- Every verified edge has inspectable evidence.
- Unresolved references remain visible for diagnostics instead of disappearing.

### D5 — Request-journey explanations

Given an entry point, endpoint, command, or test, produce a stepwise narrative
through validation, orchestration, state access, and response construction.

Acceptance:

- The graph, code viewer, and explanation panel advance together.
- Static paths are labeled "possible"; runtime traces are labeled "observed."
- Each step links to a source span.

### D6 — Architecture expectation versus reality

Compare documented or user-provided boundaries with observed dependencies.

Examples:

- Presentation code directly reaches persistence.
- A documented service layer is bypassed.
- A utility module is a high-centrality architectural dependency.
- A cycle crosses an intended boundary.

Acceptance:

- Every reported violation names the rule and its source evidence.
- Users can dismiss or codify findings as accepted architecture rules.

### D7 — Change-aware refresher tours

On a new revision, calculate which previously learned concepts changed and
generate a focused refresher.

Acceptance:

- Persist mastery against stable concept identities.
- Mark concepts invalidated, modified, or unaffected by a diff.
- Explain why each refresher step was selected.

### D8 — Runtime trace overlay

For trusted fixtures first, capture an observed execution path from a test or
example request and overlay it on the static graph.

Acceptance:

- Runtime execution is isolated from static inference in the data model and UI.
- A user can toggle possible, observed, and unobserved paths.
- Process limits and security controls are verified before arbitrary execution is
  considered.

## Cross-cutting diagnostic logging

Development mode should be deliberately verbose while remaining safe.

Every log record should include:

- UTC timestamp and severity
- Event name and human-readable message
- Process ID/name and thread ID/name
- Request correlation ID, trace ID, and span ID
- Module, function, file, and line
- Duration for completed operations
- Sanitized arguments and summarized results
- Full exception type, message, and stack trace

Required event families:

- `process.*`: application startup, configuration, readiness, shutdown
- `http.*`: request received, response sent, duration, status, failures
- `function.*`: entry, return, exception, duration
- `parser.*`: discovery, parse result, definition, resolution, unresolved evidence
- `graph.*`: node/edge creation, query inputs, query results
- `model.*`: provider/model, request size, tool calls, latency, token usage
- `subprocess.*`: sanitized command, cwd, PID, stdout/stderr, exit and timeout
- `tour.*`, `mastery.*`, `mission.*`: decisions and evidence used

Safety rules:

- Redact credentials, tokens, cookies, authorization headers, and secret-like
  environment variables.
- Summarize source and model payloads by default; full payload logging requires a
  separate explicit switch.
- Cap individual values and log lines.
- Use rotation in file logging and allow terminal verbosity to be configured.
- Never let logging failures break the product path.

Modes:

- `normal`: lifecycle, request summaries, warnings, failures
- `debug`: parser decisions, graph operations, model and subprocess lifecycle
- `trace`: decorated function calls with sanitized arguments and results
- `max`: Python call profiler for project code; extremely noisy and intended only
  for short diagnostic runs

## Delivery phases

### Phase 0 — Evidence spine and observability

- Repository constraints and safe path handling
- Stable graph schema with source spans and confidence
- Python AST plus Tree-sitter JavaScript/TypeScript/Java file, definition, and
  relationship extraction
- Candidate call references with unresolved evidence
- Structured terminal logging, correlation IDs, trace decorators, max profiler
- Initial API and tests

### Phase 1 — Navigable understanding

- React graph using a deterministic layout
- Monaco read-only source viewer
- Node/edge evidence inspector
- File tree, search, cycles, centrality, and entry-point candidates
- Source retrieval with exact citations

### Phase 2 — Adaptive onboarding

- Persona/goal intake
- Structured tour planner
- Request-journey view
- Text-based synchronized walkthrough
- Grounded Q&A with citation validation

### Phase 3 — Learning and action

- Comprehension challenges
- Concept-level mastery model
- Adaptive replanning
- First-contribution mission generator
- Mission evidence and blast-radius view

### Phase 4 — Evolution and architecture

- Git revision identity and stable concept matching
- Change-aware refresher tours
- Architecture policies and conformance findings
- Git history and rationale archaeology

### Phase 5 — Trusted execution

- Runtime instrumentation for bundled/trusted fixtures
- Static-versus-observed overlays
- Hardened isolated runner only after a dedicated threat model
- Optional narration after the complete text experience works

## Scope gates

The first demo is complete when it can:

1. Analyze curated Python, JavaScript/TypeScript, and Java repositories into an
   evidence-aware graph.
2. Display source-backed nodes and edges.
3. Produce distinct backend, security, and QA tours.
4. Ask at least one deterministic comprehension question and adapt afterward.
5. Propose one source-backed first-contribution mission.
6. Correlate all terminal logs for one analysis request from HTTP entry through
   individual parsing and graph-building operations.

Voice, arbitrary repository execution, languages beyond Python/JavaScript/
TypeScript/Java, multi-provider models, and collaborative tenancy are explicitly
outside the first demo.
