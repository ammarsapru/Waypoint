# AI development interaction log

This log records significant AI-assisted engineering work, the output reviewed by the
developer, corrections made, and validation performed. It does not include routine
explanatory questions or the messages explicitly excluded by the developer.


## Repository graph and observability foundation

- **Objective:** Scaffold a FastAPI repository analyzer with unusually detailed diagnostic
  output.
- **AI-assisted work:** Python AST graph extraction, typed graph models, API routes,
  structured terminal events, pre/post/error function hooks, subprocess streaming, and
  trace-file output.
- **Developer review/corrections:** Clarified allowed-root security, inferred versus verified
  edges, and the required verbosity of diagnostic events.
- **Validation:** Analyzer, API, observability, path-boundary, and subprocess tests.

## Interactive graph and onboarding UI

- **Objective:** Build a React, TypeScript, and Tailwind interface for Explore, Ask,
  Onboarding, and Issues.
- **AI-assisted work:** React Flow graphs, file cards, Monaco source evidence, collapsible
  sidebars, draggable source panel, fullscreen graph, themes, guided file routes, and
  contribution missions.
- **Developer review/corrections:** Reworked graph cards to group symbols by file, fixed
  fullscreen/sidebar behavior, and made answer evidence inspectable beside chat.
- **Validation:** TypeScript production builds and Playwright browser scenarios.

## Secure GitHub repository import

- **Objective:** Accept either a local repository or a public GitHub URL.
- **AI-assisted work:** URL validation, shallow non-interactive clone, managed clone storage,
  checkout size/file limits, retention, subprocess diagnostics, and allowed-root checks.
- **Developer review/corrections:** Required cloned repositories to remain inside an
  application-controlled directory and prohibited credentialed or local transports.
- **Validation:** Repository-import unit tests and HTTP API tests.

## Polyglot graph analysis

- **Objective:** Extend structural analysis beyond Python.
- **AI-assisted work:** Tree-sitter JavaScript, JSX, TypeScript, TSX, and Java extraction;
  imports, classes, functions, methods, instantiations, and conservative call resolution.
- **Developer review/corrections:** Preserved unresolved dynamic calls instead of presenting
  guesses as verified relationships and enabled inferred `may_call` visualization.
- **Validation:** Dedicated polyglot analyzer tests and repository benchmark invariants.

## Agentic repository Q&A and persistent RAG

- **Objective:** Replace predetermined answers with source-grounded model synthesis for
  arbitrary repository questions.
- **AI-assisted work:** Agent tool registry, multi-round tool loop, persistent SQLite chunks,
  FTS5/BM25, local subword vectors, graph expansion, citation validation, conversation
  memory, symbol-scoped chat, onboarding planning, and issue investigation.
- **Developer review/corrections:** Forced final structured submissions, bounded tool rounds,
  readable Markdown, evidence-only file panels, and durable conversation restoration.
- **Validation:** Agent, retrieval, semantic-tool, onboarding, issue, memory, and API tests;
  external repository benchmarks.

## MCP connector and trace hooks

- **Objective:** Expose Waypoint capabilities to external agents and make execution paths
  auditable.
- **AI-assisted work:** A 17-tool MCP server over stdio and Streamable HTTP, resource
  templates, prompts, correlated request/function/model/tool logs, token and cost fields,
  and redaction.
- **Developer review/corrections:** Kept MCP tools bounded by the same source, path, graph,
  and clone limits as the HTTP application; explicitly avoided claiming hidden
  chain-of-thought access.
- **Validation:** MCP and observability hook tests plus a recorded MCP smoke run.

## Dual-provider model routing

- **Objective:** Support OpenRouter plus Claude, or two OpenRouter models, divided by agent
  role.
- **AI-assisted work:** OpenRouter adapter, Claude Agent SDK adapter, investigation/synthesis
  routing, startup probing, billing fallback, turn/budget limits, and provider metadata.
- **Developer review/corrections:** Added the `OPEN_ROUTER_KEY` compatibility alias, replaced
  unavailable model identifiers, and kept Claude Code subscription authentication separate
  from Anthropic API credentials.
- **Validation:** Provider unit tests, forced-tool probes, and live grounded Ask smoke tests
  for OpenRouter→OpenRouter and OpenRouter→Claude.

## Phase 3 verification and model comparison — July 24, 2026

- **Objective:** Make coverage, test evidence, live agent behavior, and two-model comparison
  independently auditable.
- **AI-assisted work:** Enforced 70% branch-aware coverage, Flask HTTP verification scripts,
  opt-in live multi-step demo, frozen-evidence comparison endpoint, documentation package,
  and README architecture diagram.
- **Developer review required:** Validate the live Flask scenario, inspect trace artifacts,
  choose the deployed provider pair, and approve the final GitHub history.
- **Validation:** Full pytest coverage run, comparison unit tests, frontend build, HTTP E2E,
  and opt-in live provider run.
