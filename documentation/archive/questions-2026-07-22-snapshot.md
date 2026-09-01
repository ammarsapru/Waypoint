> **Archived, superseded.** Pinned to line numbers from 2026-07-22; the codebase has
> changed substantially since (this file predates even the dual-role model routing
> work done on 2026-07-29). Its two "core question" sections duplicate
> [`../hooks-and-logging.md`](../hooks-and-logging.md) and the now-archived
> `graph-rag-and-vector-validity.md`. Kept for history only — do not trust the line
> numbers below.

# Waypoint source-code questions

Line numbers below refer to the repository state on July 22, 2026. If code is
inserted above a snippet later, the line numbers can move; the linked function or
class name remains the more stable reference.

## `config.py`

### Line 13: what do `.resolve()` and `.parents[2]` do?

```python
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
```

`__file__` is the path of the currently executing `config.py` file. `Path(__file__)`
turns that string into a `pathlib.Path` object.

`.resolve()` produces a canonical absolute path. It removes relative components such
as `.` and `..` and resolves symbolic links where the operating system can resolve
them. In this repository the result is conceptually:

```text
C:/.../final_project_alternative/backend/app/config.py
```

`.parents` is an indexable sequence of ancestor directories:

```text
parents[0] = .../backend/app
parents[1] = .../backend
parents[2] = .../final_project_alternative
```

Therefore `_PROJECT_ROOT` is the repository root. It is used on line 15 to find the
root `.env` file and later to create default `.waypoint-data` paths.

### Line 22: what does `in {"1", ...}` do?

```python
return value.strip().lower() in {"1", "true", "yes", "on"}
```

This converts an environment-variable string to a Boolean:

1. `strip()` removes surrounding whitespace.
2. `lower()` makes the comparison case-insensitive.
3. `in {...}` tests membership in the set of accepted true values.

Examples:

```text
" TRUE " -> "true" -> True
"yes"               -> True
"0"                 -> False
"banana"            -> False
```

It does not raise an error for an unknown string; any string outside the set becomes
`False`. If the environment variable does not exist at all, line 21 returns the
provided default instead.

### Line 35: `@dataclass`, `slots=True`, and `frozen=True`

```python
@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    log_level: str
    # ...
```

`@dataclass` asks Python to generate repetitive class functionality from the declared
fields. Most importantly, it generates an `__init__` accepting `app_name`,
`log_level`, and every other field. It also generates useful representations and
equality behavior.

`slots=True` gives instances a fixed set of attributes instead of a general-purpose
`__dict__`. Benefits include preventing accidental attributes such as
`settings.log_levle = ...` and usually using less memory. It does not perform type
validation at runtime by itself; the annotations primarily serve type checkers.

`frozen=True` prevents normal reassignment after construction:

```python
settings.log_level = "DEBUG"  # raises FrozenInstanceError
```

This is useful because configuration should be stable for the lifetime of a process.
Frozen is shallow rather than recursive: if a frozen dataclass contained a mutable
list, the list's contents could still be mutated. The `Settings` fields are mostly
strings, numbers, Booleans, and `Path` values, so this is not a practical issue here.

### Line 73 onward: what does `return cls(...)` construct, and what do these settings mean?

```python
return cls(
    app_name="adaptive-codebase-onboarding",
    log_level=os.getenv("ONBOARD_LOG_LEVEL", "INFO").upper(),
    log_value_limit=max(128, _env_int("ONBOARD_LOG_VALUE_LIMIT", 4000)),
    trace_functions=_env_bool("ONBOARD_TRACE_FUNCTIONS", True),
    max_trace=_env_bool("ONBOARD_MAX_TRACE"),
    trace_file_enabled=_env_bool("ONBOARD_TRACE_FILE", True),
    # ... limits and provider settings ...
)
```

`from_environment` is a class method, so `cls` means the class on which the method was
called. Normally it is `Settings`. `return cls(...)` constructs and returns one fully
populated `Settings` object. Using `cls` instead of spelling `Settings` also allows a
subclass to inherit the constructor correctly.

- `app_name` is the stable application identifier included in lifecycle logs.
- `log_level` controls the minimum emitted Python logging level, normally `INFO`.
- `log_value_limit` limits the size of an individual rendered value in diagnostic
  logs. `max(128, ...)` prevents a configuration so small that logs become useless.
- `trace_functions` enables the entry/return/error behavior of functions decorated
  with `@traced`.
- `max_trace` enables Python's extremely noisy global call tracer for application
  code. It is separate from ordinary `@traced` instrumentation and should normally
  remain off.
- `trace_file_enabled` determines whether structured JSONL traces are written in
  addition to terminal logs.
- `trace_path` determines where that rotating JSONL trace is written.
- Clone limits bound clone duration, bytes, file count, and retained repositories.
- Repository limits bound discovered files, individual source-file size, and retained
  unresolved-reference details. These protect CPU, memory, disk, and log volume.
- Agent limits bound recent chat turns, model tool rounds, response tokens, and Claude
  Code duration. They prevent an agent loop or response from growing indefinitely.

`settings = Settings.from_environment()` on line 139 constructs the singleton imported
by the rest of the backend. Environment changes made after this import do not
automatically mutate that already-created object; the backend must be restarted.

## `indexing.py`

### Lines 29-32: what is `IndexedFileState`?

```python
@dataclass(frozen=True, slots=True)
class IndexedFileState:
    path: str
    content_sha256: str
    size_bytes: int
```

One `IndexedFileState` describes the content state of one retrievable file at snapshot
time. It is not the progress state of an indexing job. Its fields mean:

- `path`: repository-relative, slash-normalized file path.
- `content_sha256`: a deterministic hash of the exact file bytes.
- `size_bytes`: exact byte length.

The snapshot code creates one of these records per accepted file. Retrieval storage
uses the resulting repository fingerprint to decide whether a persisted index revision
still describes the current checkout. Exact file hashes also allow unchanged vectors
to be reused safely across revisions.

### Lines 36-38: what is the repository snapshot fingerprint?

```python
@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    fingerprint: str
    files: tuple[IndexedFileState, ...]
```

The fingerprint is a SHA-256 digest representing the accepted repository paths and
their exact content hashes. If an indexed file is added, removed, renamed, or changed,
the fingerprint changes. Waypoint uses it as an immutable revision identity and as a
staleness check; it is not a Git commit hash and does not require the directory to be a
Git repository.

Metadata-only changes such as a timestamp normally do not affect it because the digest
uses paths and content hashes, not modification time.

### Lines 40-42: what is `frozenset[...]`, and why is it used?

```python
@property
def paths(self) -> frozenset[str]:
    return frozenset(item.path for item in self.files)
```

`frozenset[str]` is the return type annotation: an immutable set whose members are
strings. The expression constructs that set from every file-state path.

A set provides fast membership checks such as `path in snapshot.paths`, removes any
duplicates, and does not imply ordering. `frozenset` is used instead of `set` so a
caller cannot accidentally mutate the snapshot's path collection. `@property` lets the
caller write `snapshot.paths` rather than `snapshot.paths()`.

### Lines 50 and 55: why sort directories and filenames?

```python
directories[:] = sorted(
    name for name in directories
    if name not in SKIPPED_DIRECTORIES
    and not (Path(current_root) / name).is_symlink()
)

for filename in sorted(filenames):
```

Filesystem traversal order is not guaranteed and can differ between machines or runs.
Sorting makes file discovery deterministic. That matters because:

- the same repository should produce the same fingerprint;
- the `max_files` cutoff should select the same files each time;
- tests and diagnostic output become reproducible.

Assigning to `directories[:]` mutates the list supplied by `os.walk`. This both sorts
the directories and tells `os.walk` not to descend into the removed directories.

### Line 56: why divide `current_root` by `filename`?

```python
candidate = Path(current_root) / filename
```

This is not numeric division. `pathlib.Path` overloads the `/` operator to join path
components. If `current_root` is `C:/repo/backend` and `filename` is `main.py`, the
result is `C:/repo/backend/main.py`. This is clearer and more portable than manually
inserting `\` or `/` characters.

### Lines 53 and 57: what does `is_symlink()` check?

```python
and not (Path(current_root) / name).is_symlink()

if candidate.is_symlink():
    continue
```

A symbolic link is a filesystem entry that redirects to another path. `is_symlink()`
checks whether the entry itself is such a link. Waypoint skips symlinked directories
and files so snapshot traversal cannot:

- escape into content outside the requested repository;
- accidentally index the same content through multiple aliases;
- encounter link cycles;
- create a fingerprint dependent on an external target.

`os.walk(..., followlinks=False)` supplies an additional directory-link safeguard.

### Lines 65-85: complete explanation

```python
try:
    relative = candidate.resolve().relative_to(resolved_root).as_posix()
    content = candidate.read_bytes()
except (OSError, ValueError):
    continue
states.append(IndexedFileState(
    path=relative,
    content_sha256=hashlib.sha256(content).hexdigest(),
    size_bytes=len(content),
))
if len(states) >= max_files:
    break
if len(states) >= max_files:
    break
states.sort(key=lambda item: item.path)
digest = hashlib.sha256()
for item in states:
    digest.update(item.path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(item.content_sha256.encode("ascii"))
    digest.update(b"\0")
return RepositorySnapshot(digest.hexdigest(), tuple(states))
```

Step by step:

1. `candidate.resolve()` canonicalizes the candidate's absolute path.
2. `.relative_to(resolved_root)` both proves it is under the repository root and
   removes the root prefix. It raises `ValueError` if containment fails.
3. `.as_posix()` stores a stable `/`-separated path even on Windows.
4. `read_bytes()` reads the exact bytes; hashing bytes avoids text-encoding ambiguity.
5. `except (OSError, ValueError)` skips unreadable files and containment failures
   without failing the entire snapshot.
6. `hashlib.sha256(content).hexdigest()` creates the file's 64-character content hash.
7. `len(content)` records its byte size.
8. The first `break` exits the filename loop at `max_files`; the second exits the
   surrounding directory loop as well.
9. `states.sort(...)` establishes a final deterministic order regardless of traversal.
10. `digest = hashlib.sha256()` creates the aggregate repository hash builder.
11. Each relative path and file hash is encoded into bytes and fed into the digest.
12. `b"\0"` is an unambiguous separator. Without separators, different pairs of
    strings could theoretically form the same concatenated input.
13. `digest.hexdigest()` is the repository fingerprint.
14. `tuple(states)` freezes the ordered collection inside the returned snapshot.

## `main.py`

### Line 41: what is `max_python_file_bytes`, and why is it needed?

```python
max_python_file_bytes=settings.max_python_file_bytes,
```

At this exact line it is only being included in the startup log. The setting is created
in `config.py` with a default of 2,000,000 bytes and is enforced by the analyzer and
source-reading paths.

Despite the legacy name, it now limits individual supported source files across Python,
JavaScript, TypeScript, and Java parsing. Oversized source can consume excessive
memory, parser time, index space, model context, and logging volume. The limit is both a
reliability control and a denial-of-service boundary. It is not the total repository
size limit; clone and repository-file-count limits handle different dimensions.

### Line 45: what does `run_in_threadpool` do?

```python
provider = await run_in_threadpool(model_provider_router.initialize)
```

`model_provider_router.initialize` is synchronous and can perform blocking API/Claude
Code startup checks. Calling it directly inside FastAPI's asynchronous lifespan would
block the event-loop thread. `run_in_threadpool` runs the synchronous callable on a
worker thread and asynchronously waits for its return value.

This does not make the underlying function magically asynchronous or CPU-parallel. It
keeps blocking work off the event loop so other asynchronous work is not frozen.

## `observability.py`

### Lines 22-23: `ParamSpec` and `TypeVar`

```python
P = ParamSpec("P")
R = TypeVar("R")
```

`P` represents the complete parameter specification of an arbitrary callable: its
positional parameters, keyword parameters, and their types. `R` represents that
callable's return type.

They are used by the `traced` decorator:

```python
def traced(...) -> Callable[[Callable[P, R]], Callable[P, R]]:
    # ...
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
```

This tells a type checker that decorating `function(x: int, name: str) -> Result`
produces another callable with the same parameters and return type. Without `ParamSpec`,
the decorator would often degrade the callable type to an imprecise `Callable[..., Any]`.
Neither object changes runtime function arguments.

### Lines 25-29: trace and span context variables

```python
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "span_id", default="-"
)
```

A trace ID correlates the complete operation, such as one HTTP request. A span ID
identifies one nested operation or decorated function inside that trace.

`ContextVar` stores values per asynchronous/task context rather than as unsafe global
state. Two concurrent requests can therefore have different IDs. The default `"-"`
means no trace/span has been bound. `trace_context` binds and later resets these values;
`CorrelationFilter` copies them onto every log record.

### Lines 37-40

```python
_MAX_DEPTH = 4
_RESERVED_LOG_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__)
_profile_state = threading.local()
_hook_lock = threading.RLock()
```

- `_MAX_DEPTH = 4` prevents `sanitize()` from recursively expanding deeply nested or
  cyclic-looking values forever. Deeper content becomes a type/depth placeholder.
- `_RESERVED_LOG_RECORD_KEYS` captures standard `LogRecord` field names. It is
  currently not read elsewhere in this module, so at present it has no runtime effect;
  it is leftover/preparatory state and could be removed unless future formatting needs
  it.
- `_profile_state = threading.local()` stores the maximum tracer's `active` guard per
  thread. It prevents logging performed by the tracer from recursively tracing itself.
- `_hook_lock = threading.RLock()` protects the shared hook list during registration,
  removal, and copying. `RLock` allows the owning thread to acquire it recursively.

### `DiagnosticFormatter`

```python
class DiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        milliseconds = int(record.msecs)
        event = getattr(record, "event", "log.message")
        fields = getattr(record, "fields", {})
        prefix = (
            f"{timestamp}.{milliseconds:03d}Z "
            f"{record.levelname:<8} "
            f"event={event} "
            f"pid={record.process} process={record.processName} "
            f"thread={record.threadName}:{record.thread} "
            f"trace={getattr(record, 'trace_id', '-')} "
            f"span={getattr(record, 'span_id', '-')} "
            f"at={record.name}.{record.funcName}:{record.lineno}"
        )
        # render message, structured fields, and exception stack
```

The formatter converts a `LogRecord` into the dense terminal line you see. It includes:

- UTC timestamp with milliseconds (`Z` means UTC);
- padded severity such as `INFO` or `ERROR`;
- stable event name;
- process and thread identity;
- trace and span correlation IDs;
- logger, function, and source line that emitted the record;
- JSON-quoted human message;
- sanitized, sorted JSON fields;
- a multiline traceback when `record.exc_info` exists.

`sanitize(fields)` redacts keys resembling tokens, passwords, credentials, cookies,
and API keys; replaces bearer tokens; truncates large values and collections; and
bounds recursion. `DiagnosticFormatter` is used by the terminal handler. The adjacent
`JsonTraceFormatter` emits one JSON object per line to the rotating trace file.

## Core question: where are hooks and logs defined, and when are they used?

### Answer

There are three related instrumentation levels:

1. **Explicit structured events.** `log_event(...)` adds an event name and sanitized
   fields to normal Python logging. It is used for meaningful domain events such as an
   HTTP request, repository clone, parser stage, retrieval result, model call, tool
   call, and provider fallback.
2. **Decorated function hooks.** `@traced("event.prefix")` wraps selected synchronous
   and asynchronous functions. It emits entry, successful return, duration, result,
   exception, and stack logs. It also calls registered observers with `pre`, `post`, or
   `error` phases.
3. **Maximum call tracing.** When `ONBOARD_MAX_TRACE=1`, `install_max_call_tracer()` uses
   Python's tracing API to observe nearly every call, return, and exception under
   `backend/app`. This is intentionally immense and substantially more expensive.

Hook registration is defined by `register_function_hook()` and returns an `unregister`
function. `_emit_function_hooks()` snapshots the hook list under a lock and calls each
observer with sanitized data. A broken observer is swallowed so diagnostics cannot
change application behavior.

At present, production code emits hook events but does not permanently register a
consumer through `register_function_hook`; registration is demonstrated in
`test_observability.py`. The logs themselves are active. Hooks are extension points for
future live trace streaming, metrics, debugger integrations, or test observers.

`configure_logging()` is called when `main.py` loads. It installs:

- a terminal `StreamHandler` using `DiagnosticFormatter`;
- optionally, a 25 MiB rotating JSONL handler with five backups;
- `CorrelationFilter` on both handlers;
- the configured root log level and warning capture.

### Related files and lines

- `backend/app/observability.py:98-157` — terminal and JSON trace formatters.
- `backend/app/observability.py:160-198` — correlation filter and handler setup.
- `backend/app/observability.py:201-224` — hook registration and emission.
- `backend/app/observability.py:227-243` — structured `log_event` helper.
- `backend/app/observability.py:246-276` — correlation context and argument binding.
- `backend/app/observability.py:279-424` — sync/async `@traced` wrappers and pre/post/error events.
- `backend/app/observability.py:427-489` — maximum Python call tracer.
- `backend/app/main.py:25-26` — logging and maximum tracer installation.
- `backend/app/main.py:83-139` — request/response/error middleware events.
- `backend/app/agent/service.py:605-904` — model rounds, model responses, tool calls,
  tool results, validation failures, and completion logs.
- `backend/app/processes.py:26` — traced subprocess execution.
- `backend/tests/test_observability.py:13-31` — current registered-hook behavior test.

## `repository_import.py`

### Line 49: how does `urlparse` work?

```python
parsed = urlparse(candidate)
```

`urllib.parse.urlparse` separates a URL string into components rather than establishing
that the URL is trustworthy. For:

```text
https://github.com/owner/project.git
```

important parsed fields are approximately:

```text
scheme   = "https"
hostname = "github.com"
path     = "/owner/project.git"
query    = ""
fragment = ""
```

Waypoint then performs its own allowlist validation: HTTPS only, exact GitHub hostname,
no username/password, no explicit port, query, fragment, or parameters, exactly two
path components, and restricted owner/repository characters. Finally it constructs its
own normalized clone URL instead of blindly reusing the supplied string.

### Line 117: what does `@traced` mean?

```python
@traced("repository.github_clone")
def clone(self, repository_url: str) -> Path:
```

This passes `clone` into the `traced` decorator and replaces it with a wrapper that
preserves its public metadata through `functools.wraps`. When function tracing is
enabled, the wrapper logs:

- `repository.github_clone.entry` with sanitized arguments;
- `repository.github_clone.return` with duration and result;
- or `repository.github_clone.exception` with duration, error type, message, and stack.

It also emits `pre`, `post`, or `error` hook events. It does not alter the cloning logic
or retry a failed clone.

### Lines 126-129: why `/`, and what does the code do?

```python
destination = clone_root / (
    f"{repository.owner}--{repository.name}--{uuid.uuid4().hex[:10]}"
)
destination.resolve().relative_to(clone_root)
```

Again, `/` is `Path` joining. The child directory name contains the already-validated
owner and repository plus ten hexadecimal characters from a random UUID. This avoids
collisions between repeated clones and prevents two users cloning the same repository
into the same directory.

`destination.resolve().relative_to(clone_root)` is a containment assertion. It returns
a relative path if the destination remains inside the secure clone root and raises
`ValueError` otherwise. The returned relative path is deliberately ignored; successful
completion of the check is what matters.

### Line 248: what is `_checkout_size`?

```python
def _checkout_size(self, destination: Path) -> tuple[int, int]:
    file_count = 0
    size_bytes = 0
    for current_root, directories, files in os.walk(
        destination, followlinks=False
    ):
        directories[:] = [
            directory
            for directory in directories
            if not (Path(current_root) / directory).is_symlink()
        ]
        for name in files:
            entry = Path(current_root) / name
            file_count += 1
            size_bytes += entry.stat(follow_symlinks=False).st_size
            if (
                file_count > self.max_clone_files
                or size_bytes > self.max_clone_bytes
            ):
                return file_count, size_bytes
    return file_count, size_bytes
```

It measures the complete cloned checkout, including Git metadata, and returns
`(file_count, total_bytes)`. It does not follow symbolic-link directories and asks
`stat` not to follow file links. It returns immediately once either configured limit is
exceeded because an exact final total is unnecessary—the clone is already known to be
invalid. `clone()` then deletes an oversized checkout and returns a controlled error.

## Core question: is the system agentic RAG over graph nodes, what are the nodes, and are vector embeddings invalid?

### Answer

Waypoint is best described as **agentic hybrid RAG over source chunks, a typed static
code graph, lexical search, and local fuzzy vectors**.

It is agentic because the model chooses bounded retrieval and graph tools over multiple
rounds. It is RAG because retrieved repository evidence is added to the model context
before synthesis. It is hybrid because graph traversal is only one retrieval channel.

The graph's node kinds are:

```python
class NodeKind(str, Enum):
    REPOSITORY = "repository"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
```

- A repository node represents the analyzed repository.
- A module node normally represents one analyzed source file/module.
- Class, function, and method nodes represent parser-discovered static symbols and
  carry a qualified name, source span, and optional signature.
- Runtime object instances are not separate graph nodes. A statically detected class
  construction is represented by an `instantiates` edge from the calling symbol to the
  class node.

Edges are `contains`, `imports`, `may_call`, and `instantiates`. Every edge carries
evidence with a source range, original syntax, resolution explanation, confidence, and
one of `verified`, `inferred`, or `unresolved`. Therefore the graph expresses explicit
code relationships rather than mere textual similarity.

Vector retrieval is not invalid. A vector is useful for finding chunks whose wording
resembles a question, especially when exact keyword matching misses a related word
form. But vector similarity alone cannot prove that function A calls function B, that a
module imports another module, or that a class is instantiated at a particular line.
Similarity is a candidate-generation signal, not relationship evidence.

Waypoint currently does **not** use a learned embedding model. `LocalCodeVectorizer`
creates a deterministic 768-dimensional sparse vector from normalized words, suffix
variants, and hashed three-character subwords. It is cheap, local, reproducible, and
helps with forms such as `greet` versus `greetings`; it has limited understanding of
synonyms and broad concepts compared with a learned semantic embedding model.

Current retrieval combines:

1. SQLite FTS5/BM25 lexical candidates.
2. Local subword-vector candidates.
3. Exact token, path, qualified-name, and symbol-kind bonuses.
4. Test-file penalties unless the question asks about tests.
5. One-hop graph expansion around the strongest symbol-backed chunks.
6. Bounded source reads and final citation validation.

The right design is therefore not “graph or vectors.” Graphs answer structural
questions and supply relationships; lexical/vector retrieval finds candidate evidence;
the agent decides which tools to use; deterministic citation checks keep the final
answer grounded. Learned embeddings remain a valid future improvement for synonym-heavy
queries, but they should augment rather than replace graph and lexical evidence.

### Related files and lines

- `backend/app/graph/models.py:9-27` — node kinds, edge kinds, and evidence status.
- `backend/app/graph/models.py:30-72` — source spans, evidence, nodes, and edges.
- `backend/app/graph/analyzer.py:769-913` — repository parsing and graph construction.
- `backend/app/graph/polyglot.py:162-827` — JavaScript, TypeScript, and Java parsing and relationships.
- `backend/app/agent/retrieval.py:29-73` — deterministic local subword vectorizer and similarity.
- `backend/app/agent/retrieval.py:135-187` — persisted chunks, vectors, edges, and FTS tables.
- `backend/app/agent/retrieval.py:592-662` — symbol-aware and file-level chunk creation.
- `backend/app/agent/retrieval.py:728-852` — hybrid scoring and graph-neighbor expansion.
- `backend/app/agent/service.py:82-345` — tools made available to the repository agent.
- `backend/app/agent/service.py:395-479` — execution of retrieval and graph tools.
- `backend/app/agent/service.py:618-904` — bounded agent loop and evidence correction.
- `current-storage-and-retrieval-architecture.md:150-204` — current chunking and retrieval explanation.
