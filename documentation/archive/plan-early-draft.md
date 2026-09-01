> **Archived, superseded.** This is the earliest (2026-07-18) project planning
> conversation, kept for history. [`../product-plan.md`](../product-plan.md) (formerly
> `differentiator-plan.md`) is the refined, structured version with actual
> implementation status and is the current product-plan reference.

I'll build it — but one honest flag first, then the plan, because two pieces you named are the exact traps we already diagnosed and I don't want to bury that inside a nice-looking document.

The two flags, briefly: **"VS Code base for code to be tested" — an embedded VS Code / live code execution is a separate product** (Chromium/Electron or a WebContainer runtime) that would dwarf the actual project. I've scoped it below as a **read-only Monaco viewer + a sandboxed test-runner service**, which gives you "see the code" and "run its tests" without becoming a cloud IDE. And **"clicky" (guided tour) plus voice** is your real differentiator, so it's in — but sequenced as the *last, droppable* layer, because it's where time evaporates. With that said, here's the plan in the same shape as your other one.

---

# Codebase Visualizer + Guided Onboarding Agent — Project Plan

## 1. Goal

Point the tool at a Python repo; it parses the code into a verified dependency/architecture graph, renders it as an interactive node-graph, and an **agent plans and delivers a guided tour** of the codebase (clicky walkthrough + optional voice), answering "where is X handled?" with answers grounded in the real graph. A read-only code viewer shows actual files as the tour references them, and a sandboxed runner can execute the repo's tests.

**Differentiator vs repowise/CodeSee/CodeAtlas:** they're passive visualizers. This one *actively onboards a specific person* — the guided tour + grounded Q&A is the agentic layer none of them ship.

## 2. Architecture (parse once, render many)

```
Repo (GitHub URL or upload)
        │
        ▼
┌─────────────────────────────┐
│ Ingestion (Python AST)      │  ← the foundation; everything reads this
│ files, functions, classes,  │
│ imports, call edges         │
│ + import resolution         │
└──────────┬──────────────────┘
           ▼
┌─────────────────────────────┐
│ Graph store (nodes + edges) │  single source of truth
│ + AI semantic layer         │  file/module summaries, "this is the auth layer"
└──────────┬──────────────────┘
           │  (everything below is a consumer of the graph)
   ┌───────┼─────────────┬──────────────┬─────────────────┐
   ▼       ▼             ▼              ▼                 ▼
Graph    Monaco       Tour agent     Q&A agent        Test-runner
viewer   viewer       (clicky +      (grounded,       (sandboxed,
(nodes)  (read-only)  voice)         cited)           run repo tests)
```

## 3. Stage 1 — Ingestion (the spine; get this right first)

- **Parser:** Python `ast` (standard library). Per `.py` file extract: functions, classes, imports (`Import`/`ImportFrom`), and function→function call edges.
- **Import resolution** (the one genuinely fiddly part): map `from app.core import auth` → the real `app/core/auth.py`. Handle packages, relative imports (`..utils`), `__init__.py`; **skip external libraries** (they're not internal nodes). Rule: resolve what you can, drop what you can't rather than guessing — a wrong edge is worse than a missing one.
- **Output:** one graph structure — `{ files:[{path, functions, classes, imports}], edges:[{from, to, type: import|call}] }`. This is your single source of truth; everything else queries it.
- **v1 is Python-only.** Multi-language (via tree-sitter) is explicit future work — don't fight multiple parsers this weekend.

## 4. Stage 2 — Graph service + semantic layer

- **Deterministic query functions** over the graph (no LLM): `get_file_tree()`, `get_dependencies(file)`, `get_call_graph(fn)`, `find_entry_points()`, `find_definition(symbol)`, `get_dead_code()`, `find_cycles()`.
- **AST owns structure; AI owns meaning.** A cheap model annotates: per-file/per-module plain-English summaries, architectural role tagging ("data layer," "auth"), and entry-point reasoning. Edges are *facts from the AST*; labels are *AI*. Never let the LLM invent edges.
- Expose these functions as an **MCP server** — this is your headline agentic primitive (the agent calls these tools), and it's "software for agents."

## 5. Stage 3 — The graph visualization

- Render nodes = files (expandable to functions), edges = imports (solid) and calls (dashed).
- **Use a graph library** (React Flow or vis-network) — don't hand-roll layout; use the default force layout, make it functional not beautiful.
- Click a node → side panel shows that file's functions, its inbound/outbound dependencies, and its AI summary.
- Surface the things a reader wouldn't ask for: circular dependencies, god-files (high in-degree), dead modules, entry points highlighted.

## 6. Stage 4 — The agent (the graded core)

Two agentic loops over the graph, both real decisions, not scripts:

**Grounded Q&A agent** — "where is auth handled?" → agent calls graph tools (`find_definition`, `get_dependencies`), decides which to call based on results, answers with **file+function citations**. A **cite-or-refuse hook** blocks any answer not grounded in the graph.

**Tour-planner agent** — the differentiator. Given the repo graph, the agent *decides the route*: entry points → core modules → periphery. It's a multi-step plan, not a fixed order — it reasons about *this specific repo's* structure. Delivers the tour via the **clicky walkthrough**.

Primitives, mapped honestly: **MCP** (graph tools the agent calls), **memory** (what the newcomer has already been shown, so the tour and Q&A don't repeat), **hooks** (cite-or-refuse; a tour-step gate that won't advance until the current node is explained), **skills** (repo-convention summarizer, tour-planner as callable units).

## 7. Stage 5 — Monaco viewer + test runner (your "VS Code" ask, scoped safely)

- **Monaco Editor, read-only.** Click a graph node → the real file opens in a syntax-highlighted pane; the tour references line ranges. This is the *editor component* extracted from VS Code — an npm package, not the app. It gives "see the actual code" with none of the IDE weight. **No editing, no file-write, no git** — that's the scope line that keeps this a weekend feature.
- **Test runner = a separate sandboxed service**, not "run inside the editor." A backend endpoint clones the repo into a **Docker sandbox** (no network, resource-capped), runs its test suite (`pytest`), and streams pass/fail back to the UI. This is "code can be tested" done safely — the agent (or user) triggers it, results render in a panel. A **hook** gates it: never run untrusted code outside the sandbox.

## 8. Stage 6 — Clicky guided tour + voice (differentiator; build LAST, droppable)

- **Clicky walkthrough:** a UI tour library (Shepherd.js / Driver.js / Intro.js) highlights each graph region in turn, with a text bubble explaining it — the route comes from the tour-planner agent. **Build this fully with text bubbles first;** it's demoable and impressive on its own.
- **Voice (add last, only if time):** TTS narrates each tour step, synced to the highlight. This is the wow-finish and the thing no competitor has — but it's a second system (STT/TTS + sync), so it's the *first thing cut* if the clock runs out. The text tour must stand alone without it.

## 9. Model stack

- **Semantic layer (bulk file/module summaries):** cheap model — **Gemini Flash** or **Claude Haiku** (high volume, keep it cheap). This is also your **second provider** for the rubric.
- **Agent brain (tour planning, Q&A reasoning, route decisions):** **Claude** (Sonnet) — reliable at multi-step reasoning + structured output + tool orchestration.
- Both behind one model-client interface, model as config variable. Two providers ✓.

## 10. Tech stack

- **Backend:** Python + FastAPI (async; hosts the AST parser, graph service, MCP server, sandbox runner).
- **Frontend:** React — graph (React Flow), code pane (Monaco read-only), tour (Shepherd/Driver.js). Responsive; keep it ugly.
- **Sandbox:** Docker, no-network, resource-limited, for the test runner.
- **Store:** the graph in SQLite or in-memory JSON to start — no heavy DB needed for one repo.
- **Single-repo, single-user** for now — no auth, no multi-tenant.

## 11. The failure points to test in hour one (irreversible/expensive-late)

1. **Import resolution on a real repo** — run the AST parser on an actual mid-size Python repo (Flask, requests) and *look at the edges*. Are they right? This is the make-or-break of the whole graph; validate before building anything on top.
2. **Structured output from the agent** — does Claude reliably return the tool-call/JSON shapes your graph tools expect? Test before building the loop.
3. **Sandbox actually isolates** — confirm the Docker runner can't reach the network and is resource-capped *before* you run any untrusted repo in it.

## 12. Acceptance test for "the spine is right"

Before tour/voice/tests are even in scope, the core passes when:
1. Point at a real Python repo → a **correct** dependency graph (edges match reality on spot-check).
2. Graph renders; clicking a node shows its files/functions/summary.
3. Q&A agent answers "where is X?" with a **correct file+function citation**, and refuses when it can't ground the answer.
4. The tour-planner produces a sensible **route** for that specific repo (not a fixed order).

Hit those four and the graph+agent foundation is solid; Monaco, the sandbox runner, clicky, and voice are all additive layers over data that already exists.

## 13. Build order (fast-tool velocity, sequenced by risk not size)

1. AST parser + import resolution → **validate edges on a real repo** (do not proceed until right)
2. Graph service functions + MCP server
3. Graph visualization (React Flow)
4. Q&A agent with cite-or-refuse hook (the graded loop)
5. Tour-planner agent → clicky walkthrough (text bubbles)
6. Monaco read-only viewer
7. Sandboxed test runner
8. Voice narration ← last, droppable

## 14. Risks & mitigations

- **Import resolution is the whole graph** → validate on a real repo in hour one; drop unresolvable edges rather than guess.
- **"VS Code embedded" scope-creep** → Monaco read-only + separate sandbox runner, *not* an in-app IDE; no editing/git.
- **Untrusted code execution** → Docker sandbox, no network, resource caps, hook-gated.
- **Voice eats the clock** → text tour ships first and stands alone; voice is explicitly the first cut.
- **Prior art (repowise et al.)** → position on the *active guided-onboarding + voice* wedge, which none of them have; the graph is table-stakes substrate, not the pitch.

---

Two honest notes to close. First, the parts you asked for by name — the "VS Code" testing piece and clicky — are in, but scoped to the versions that ship (Monaco read-only + Docker runner; text-tour-then-voice) rather than the versions that sink the timeline (embedded IDE; voice-first). Second, this plan and your document-RAG plan are now both real and mutually exclusive for Monday — you can't build both. This one is more novel and more demo-flashy; the document one is more finishable and sits on your RAG track. You've been building toward the document one all weekend, so I'd treat this as the *alternative*, fully specced so you can compare — not a signal to switch now unless it genuinely excites you more.