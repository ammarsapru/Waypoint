# Waypoint MCP connector

Waypoint exposes its repository analyzer, persistent hybrid index, code graph, precise
architecture queries, and optional repository agent through the Model Context Protocol.

## Start locally over stdio

```powershell
.\.venv\Scripts\python.exe -m backend.app.mcp_server --transport stdio
```

Use `waypoint-mcp.example.json` as the base MCP client configuration. Some clients
require absolute values for `command` and `cwd`; replace the relative paths with this
repository's absolute path when required.

## Start over Streamable HTTP

```powershell
$env:WAYPOINT_MCP_HOST="127.0.0.1"
$env:WAYPOINT_MCP_PORT="8010"
.\.venv\Scripts\python.exe -m backend.app.mcp_server --transport streamable-http
```

The MCP endpoint is `http://127.0.0.1:8010/mcp`.

Inspect it with the official MCP Inspector:

```powershell
npx.cmd -y @modelcontextprotocol/inspector
```

## Exposed primitives

The server provides 30 tools (verified by counting `@mcp.tool()` decorators in
`backend/app/mcp_server.py` — this count grows over time, so re-verify rather than
trust it indefinitely), including local/GitHub analysis, hybrid search, bounded source
reads, symbol lookup, graph expansion, architecture queries, dependency impact, index
administration, onboarding/mission/challenge operations, GitHub issue history and
investigation, model/retrieval comparison, and optional model-backed repository
questions. See [`agent-and-mcp-tools.md`](agent-and-mcp-tools.md) for the full
categorized list.

It provides analysis-summary and index-status resource templates plus an
`explain_repository` prompt. Every operation reuses the application's allowed-root,
source allowlist, file-size, clone-size, graph-depth, and result-count protections.

## Tracing

Structured pre-call, post-call, and error events are enabled by default for functions
decorated with `@traced`. Every event goes to the terminal and to the rotating JSONL
trace file:

```text
.waypoint-data/traces/waypoint.jsonl
```

Set these values in `.env` to tune diagnostics:

```text
ONBOARD_LOG_LEVEL=INFO
ONBOARD_TRACE_FUNCTIONS=1
ONBOARD_TRACE_FILE=1
ONBOARD_TRACE_PATH=.waypoint-data/traces/waypoint.jsonl
ONBOARD_LOG_VALUE_LIMIT=4000
```

`ONBOARD_MAX_TRACE=1` additionally activates the Python interpreter call tracer for
every application function call, return, and exception. This is extremely noisy and
should be enabled only during focused debugging.

Trace logs contain visible model responses, tool arguments/results, retrieval ranks,
selected evidence, citation decisions, timing, token usage, and exception stacks.
Secrets are redacted and values are bounded. They do not claim to expose a model's
private hidden chain-of-thought.
