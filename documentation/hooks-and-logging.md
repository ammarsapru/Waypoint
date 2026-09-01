# Hooks and logs: definitions and call timing

## Logging initialization

`backend/app/main.py:25-26` calls `configure_logging()` and
`install_max_call_tracer()` when the backend module loads.

`configure_logging()` at `backend/app/observability.py:167-198` installs:

- a terminal stream handler using `DiagnosticFormatter`;
- an optional rotating JSONL trace handler using `JsonTraceFormatter`;
- trace/span correlation filters;
- the configured root log level and Python warning capture.

The JSONL handler rotates at 25 MiB and retains five backups. Its default location is
`.waypoint-data/traces/waypoint.jsonl`.

## Structured logs

`log_event()` at `backend/app/observability.py:227-243` is the shared structured logging
function. Callers supply severity, stable event name, human message, and fields. Fields
pass through `sanitize()`, which:

- redacts credential-like keys and bearer values;
- truncates large strings and collections;
- bounds recursive rendering;
- safely renders paths, bytes, Pydantic models, and dataclasses;
- never allows a rendering failure to break product execution.

Important event families include:

- `process.*`: startup, readiness, shutdown, logging, frontend mount;
- `http.*`: request, response, duration, and unhandled error;
- `repository.*`: URL validation, clone process, size, cleanup, retention;
- `parser.*` and `graph.*`: discovery, parsing, node/edge/unresolved creation;
- `index.*` and `retrieval.*`: revision changes, candidates, ranks, reads;
- `model.*`: provider selection, requests, visible response blocks, usage, failures;
- `model.tool_*`: tool arguments, results, bytes, duration, evidence, and errors;
- `mcp.*`: every exposed MCP tool/resource entry, return, and exception;
- `subprocess.*`: command lifecycle, output, exit status, and duration.

## Function hooks

`@traced("event.prefix")` is defined at
`backend/app/observability.py:279-424`. For decorated synchronous and asynchronous
functions, it emits:

1. `prefix.entry` and a `pre` hook before the function receives control;
2. `prefix.return` and a `post` hook after successful completion;
3. `prefix.exception`, an `error` hook, and full stack after failure;
4. duration, sanitized arguments/result, and parent/current span IDs.

The wrapper restores the prior span in `finally`, including when an exception occurs.

`register_function_hook()` at lines 201-213 adds a synchronous observer and returns an
unregister function. `_emit_function_hooks()` at lines 216-224 calls a snapshot of
observers with `pre`, `post`, or `error`. Hook failures are swallowed so instrumentation
cannot modify product behavior.

Production code currently emits these hook events but does not register a permanent
hook consumer. Registration is tested in `backend/tests/test_observability.py`. This
means logs are actively persisted, while the hook registry is presently an extension
point for future live streaming, metrics, or debugger consumers.

## Maximum tracing

`install_max_call_tracer()` at `backend/app/observability.py:427-489` is enabled only by
`ONBOARD_MAX_TRACE=1`. It uses Python tracing for almost every call, return, and exception
under `backend/app`, including arguments/results. A thread-local recursion guard prevents
the tracer from tracing its own logging.

This mode is intentionally immense, expensive, and normally disabled. Regular
`@traced` instrumentation is the practical default.

## Correlation

The HTTP middleware creates or accepts a request ID and binds it as the trace ID.
`ContextVar` values keep concurrent async requests separate. Nested decorated operations
receive span IDs, so terminal/JSONL events can be reconstructed into one request and its
sub-operations.

## Model “thought process” limitation

Logs capture visible model text blocks, requested tools, arguments, results, citations,
rounds, and validation failures. They do not and should not claim to capture private
chain-of-thought. The observable action/evidence trace is what can be reliably audited.

## Related implementation

- `backend/app/observability.py:25-164` — correlation, sanitization, and formatters.
- `backend/app/observability.py:167-243` — handlers, hook registration, and `log_event`.
- `backend/app/observability.py:246-489` — contexts, decorator, and maximum tracer.
- `backend/app/main.py:30-139` — lifecycle and HTTP correlation logs.
- `backend/app/agent/service.py:605-904` — model/tool/validation logs.
- `backend/app/processes.py:26-151` — subprocess logging.

