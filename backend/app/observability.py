from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from backend.app.config import settings

P = ParamSpec("P")
R = TypeVar("R")

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "span_id", default="-"
)

_SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|password|passwd|secret|token|api[-_]?key|credential)",
    re.IGNORECASE,
)
# `_SENSITIVE_KEY`'s "token" branch exists for credential fields like `github_token`/
# `access_token` (always singular in this codebase), but "token" is also a substring
# of every token-*count* field (`input_tokens`, `output_tokens`, `max_tokens`,
# `token_count`, `cache_read_input_tokens`, ...) - which are plural or explicitly
# "_count", never a bare singular "token". Without this exemption, `sanitize()`
# blanket-redacted every token count in every structured log across the whole
# application (found 2026-07-30 while reconstructing historical cost data - the
# trace log's `model.response_received`/`model.issue_*_response` events had genuinely
# survived log rotation, but every one of their `input_tokens`/`output_tokens` fields
# had already been replaced with the literal string "<redacted>", making them
# unusable for cost reconstruction regardless of retention).
_TOKEN_COUNT_KEY = re.compile(r"(?:^|_)tokens(?:_|$)|token_count", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_MAX_DEPTH = 4
_RESERVED_LOG_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__)
_profile_state = threading.local()
_hook_lock = threading.RLock()
_function_hooks: list[Callable[[str, Mapping[str, Any]], None]] = []
_event_hooks: list[Callable[[Mapping[str, Any]], None]] = []


def _truncate(value: str, limit: int | None = None) -> str:
    active_limit = limit or settings.log_value_limit
    if len(value) <= active_limit:
        return value
    removed = len(value) - active_limit
    return f"{value[:active_limit]}…<{removed} chars truncated>"


def sanitize(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Return a bounded, credential-safe representation suitable for logs."""
    try:
        if key and _SENSITIVE_KEY.search(key) and not _TOKEN_COUNT_KEY.search(key):
            return "<redacted>"
        if depth > _MAX_DEPTH:
            return f"<{type(value).__name__}:max-depth>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str):
            return _truncate(_BEARER.sub("Bearer <redacted>", value))
        if isinstance(value, bytes):
            return f"<bytes length={len(value)}>"
        if isinstance(value, Mapping):
            items = list(value.items())
            rendered = {
                str(item_key): sanitize(
                    item_value, key=str(item_key), depth=depth + 1
                )
                for item_key, item_value in items[:50]
            }
            if len(items) > 50:
                rendered["<truncated>"] = f"{len(items) - 50} more keys"
            return rendered
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            rendered = [sanitize(item, depth=depth + 1) for item in items[:50]]
            if len(items) > 50:
                rendered.append(f"<{len(items) - 50} more items>")
            return rendered
        if hasattr(value, "model_dump"):
            return sanitize(value.model_dump(), depth=depth + 1)
        if hasattr(value, "__dataclass_fields__"):
            fields = {
                name: getattr(value, name)
                for name in value.__dataclass_fields__
                if hasattr(value, name)
            }
            return sanitize(fields, depth=depth + 1)
        return _truncate(repr(value))
    except Exception as exc:  # Logging must never break the product path.
        return f"<unrenderable {type(value).__name__}: {type(exc).__name__}>"


class DiagnosticFormatter(logging.Formatter):
    """Dense terminal formatter optimized for correlation and grepability."""

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
        message = record.getMessage()
        rendered_fields = ""
        if fields:
            rendered_fields = " fields=" + json.dumps(
                sanitize(fields), ensure_ascii=True, sort_keys=True, default=str
            )
        rendered = f"{prefix} message={json.dumps(message)}{rendered_fields}"
        if record.exc_info:
            rendered += "\n" + "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        return rendered


class JsonTraceFormatter(logging.Formatter):
    """One bounded JSON object per event for durable trace inspection."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "event": getattr(record, "event", "log.message"),
            "message": record.getMessage(),
            "logger": record.name,
            "function": record.funcName,
            "line": record.lineno,
            "process_id": record.process,
            "process_name": record.processName,
            "thread_id": record.thread,
            "thread_name": record.threadName,
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
            "fields": sanitize(getattr(record, "fields", {})),
        }
        if record.exc_info:
            payload["exception_stack"] = _truncate(
                "".join(traceback.format_exception(*record.exc_info)),
                max(settings.log_value_limit * 4, 4_000),
            )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        record.span_id = span_id_var.get()
        return True


def configure_logging(stream: Any | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    target_stream = stream or sys.stdout
    if stream is None and hasattr(target_stream, "reconfigure"):
        try:
            target_stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass
    handler = logging.StreamHandler(target_stream)
    handler.setFormatter(DiagnosticFormatter())
    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)
    if settings.trace_file_enabled:
        settings.trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_handler = RotatingFileHandler(
            settings.trace_path,
            maxBytes=25 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        trace_handler.setFormatter(JsonTraceFormatter())
        trace_handler.addFilter(CorrelationFilter())
        root.addHandler(trace_handler)
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    logging.captureWarnings(True)
    log_event(
        logging.getLogger("backend"),
        logging.INFO,
        "process.logging_configured",
        "Diagnostic terminal logging configured",
        log_level=settings.log_level,
        trace_functions=settings.trace_functions,
        max_trace=settings.max_trace,
        value_limit=settings.log_value_limit,
        trace_file_enabled=settings.trace_file_enabled,
        trace_path=settings.trace_path if settings.trace_file_enabled else None,
    )


def register_function_hook(
    hook: Callable[[str, Mapping[str, Any]], None]
) -> Callable[[], None]:
    """Register a synchronous observer for pre/post/error function events."""
    with _hook_lock:
        _function_hooks.append(hook)

    def unregister() -> None:
        with _hook_lock:
            if hook in _function_hooks:
                _function_hooks.remove(hook)

    return unregister


def register_event_hook(
    hook: Callable[[Mapping[str, Any]], None]
) -> Callable[[], None]:
    """Observe sanitized structured log events without parsing terminal text."""
    with _hook_lock:
        _event_hooks.append(hook)

    def unregister() -> None:
        with _hook_lock:
            if hook in _event_hooks:
                _event_hooks.remove(hook)

    return unregister


def _emit_function_hooks(phase: str, payload: Mapping[str, Any]) -> None:
    with _hook_lock:
        hooks = tuple(_function_hooks)
    enriched = {
        "trace_id": trace_id_var.get(),
        "phase": phase,
        **payload,
    }
    for hook in hooks:
        try:
            hook(phase, sanitize(enriched))
        except Exception:
            # Instrumentation must never alter the application result.
            continue


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    try:
        safe_fields = sanitize(fields)
        logger.log(
            level,
            message,
            extra={"event": event, "fields": safe_fields},
            stacklevel=2,
        )
        with _hook_lock:
            hooks = tuple(_event_hooks)
        payload = {
            "trace_id": trace_id_var.get(),
            "span_id": span_id_var.get(),
            "event": event,
            "level": logging.getLevelName(level),
            "message": message,
            "fields": safe_fields,
        }
        for hook in hooks:
            try:
                hook(payload)
            except Exception:
                continue
    except Exception:
        # Never recursively log a logging failure.
        pass


class trace_context:
    """Bind trace/span IDs to the current async and thread context."""

    def __init__(self, trace_id: str | None = None, span_id: str | None = None):
        self.trace_id = trace_id or uuid.uuid4().hex
        self.span_id = span_id or uuid.uuid4().hex[:16]
        self._trace_token: contextvars.Token[str] | None = None
        self._span_token: contextvars.Token[str] | None = None

    def __enter__(self) -> "trace_context":
        self._trace_token = trace_id_var.set(self.trace_id)
        self._span_token = span_id_var.set(self.span_id)
        return self

    def __exit__(self, *_: Any) -> None:
        if self._span_token is not None:
            span_id_var.reset(self._span_token)
        if self._trace_token is not None:
            trace_id_var.reset(self._trace_token)


def _bound_arguments(function: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
    try:
        signature = inspect.signature(function)
        bound = signature.bind_partial(*args, **kwargs)
        return sanitize(bound.arguments)
    except Exception:
        return {
            "args": sanitize(args),
            "kwargs": sanitize(kwargs),
        }


def traced(
    event_prefix: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Log entry, return, duration, and exceptions for sync or async functions."""

    def decorator(function: Callable[P, R]) -> Callable[P, R]:
        prefix = event_prefix or f"{function.__module__}.{function.__qualname__}"
        logger = logging.getLogger(function.__module__)

        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                if not settings.trace_functions:
                    return await function(*args, **kwargs)
                started = time.perf_counter()
                parent_span = span_id_var.get()
                token = span_id_var.set(uuid.uuid4().hex[:16])
                log_event(
                    logger,
                    logging.INFO,
                    f"{prefix}.entry",
                    f"Entering async function {function.__qualname__}",
                    arguments=_bound_arguments(function, args, kwargs),
                    parent_span=parent_span,
                )
                _emit_function_hooks("pre", {
                    "function": f"{function.__module__}.{function.__qualname__}",
                    "event_prefix": prefix,
                    "arguments": _bound_arguments(function, args, kwargs),
                    "parent_span": parent_span,
                    "span_id": span_id_var.get(),
                })
                try:
                    result = await function(*args, **kwargs)
                    duration_ms = round((time.perf_counter() - started) * 1000, 3)
                    log_event(
                        logger,
                        logging.INFO,
                        f"{prefix}.return",
                        f"Async function {function.__qualname__} completed",
                        duration_ms=duration_ms,
                        result=sanitize(result),
                    )
                    _emit_function_hooks("post", {
                        "function": f"{function.__module__}.{function.__qualname__}",
                        "event_prefix": prefix,
                        "duration_ms": duration_ms,
                        "result": sanitize(result),
                        "span_id": span_id_var.get(),
                    })
                    return result
                except Exception as exc:
                    duration_ms = round((time.perf_counter() - started) * 1000, 3)
                    log_event(
                        logger,
                        logging.ERROR,
                        f"{prefix}.exception",
                        f"Async function {function.__qualname__} failed",
                        duration_ms=duration_ms,
                        exception_type=type(exc).__name__,
                        exception_message=str(exc),
                    )
                    _emit_function_hooks("error", {
                        "function": f"{function.__module__}.{function.__qualname__}",
                        "event_prefix": prefix,
                        "duration_ms": duration_ms,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "span_id": span_id_var.get(),
                    })
                    logger.exception("Full async function failure stack")
                    raise
                finally:
                    span_id_var.reset(token)

            return async_wrapper

        @functools.wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if not settings.trace_functions:
                return function(*args, **kwargs)
            started = time.perf_counter()
            parent_span = span_id_var.get()
            token = span_id_var.set(uuid.uuid4().hex[:16])
            log_event(
                logger,
                logging.INFO,
                f"{prefix}.entry",
                f"Entering function {function.__qualname__}",
                arguments=_bound_arguments(function, args, kwargs),
                parent_span=parent_span,
            )
            _emit_function_hooks("pre", {
                "function": f"{function.__module__}.{function.__qualname__}",
                "event_prefix": prefix,
                "arguments": _bound_arguments(function, args, kwargs),
                "parent_span": parent_span,
                "span_id": span_id_var.get(),
            })
            try:
                result = function(*args, **kwargs)
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                log_event(
                    logger,
                    logging.INFO,
                    f"{prefix}.return",
                    f"Function {function.__qualname__} completed",
                    duration_ms=duration_ms,
                    result=sanitize(result),
                )
                _emit_function_hooks("post", {
                    "function": f"{function.__module__}.{function.__qualname__}",
                    "event_prefix": prefix,
                    "duration_ms": duration_ms,
                    "result": sanitize(result),
                    "span_id": span_id_var.get(),
                })
                return result
            except Exception as exc:
                duration_ms = round((time.perf_counter() - started) * 1000, 3)
                log_event(
                    logger,
                    logging.ERROR,
                    f"{prefix}.exception",
                    f"Function {function.__qualname__} failed",
                    duration_ms=duration_ms,
                    exception_type=type(exc).__name__,
                    exception_message=str(exc),
                )
                _emit_function_hooks("error", {
                    "function": f"{function.__module__}.{function.__qualname__}",
                    "event_prefix": prefix,
                    "duration_ms": duration_ms,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "span_id": span_id_var.get(),
                })
                logger.exception("Full function failure stack")
                raise
            finally:
                span_id_var.reset(token)

        return wrapper

    return decorator


def install_max_call_tracer(package_root: Path | None = None) -> None:
    """Trace every Python call in this application. Intentionally very noisy."""
    if not settings.max_trace:
        return
    logger = logging.getLogger("backend.max_trace")
    root = (package_root or Path(__file__).parent).resolve()

    def profiler(frame: Any, event: str, arg: Any) -> Callable[..., Any]:
        if event not in {"call", "return", "exception"}:
            return profiler
        if getattr(_profile_state, "active", False):
            return profiler
        filename = Path(frame.f_code.co_filename)
        try:
            filename.resolve().relative_to(root)
        except (OSError, ValueError):
            return profiler
        # Avoid tracing the tracer and formatter themselves.
        if filename.name == Path(__file__).name:
            return profiler
        _profile_state.active = True
        try:
            fields: dict[str, Any] = {
                "function": frame.f_code.co_qualname,
                "file": str(filename),
                "line": frame.f_lineno,
            }
            if event == "call":
                argument_info = inspect.getargvalues(frame)
                fields["arguments"] = {
                    name: sanitize(argument_info.locals.get(name), key=name)
                    for name in argument_info.args
                }
            elif event == "return":
                fields["result"] = sanitize(arg)
            else:
                exception_type, exception, _ = arg
                fields["exception_type"] = exception_type.__name__
                fields["exception_message"] = str(exception)
            log_event(
                logger,
                logging.DEBUG,
                f"function.max_trace.{event}",
                f"Python function {event}: {frame.f_code.co_qualname}",
                **fields,
            )
            _emit_function_hooks(
                {"call": "pre", "return": "post", "exception": "error"}[event],
                fields,
            )
        finally:
            _profile_state.active = False
        return profiler

    sys.settrace(profiler)
    threading.settrace(profiler)
    log_event(
        logger,
        logging.WARNING,
        "process.max_trace_enabled",
        "Maximum Python call tracing enabled; terminal output will be immense",
        package_root=root,
    )


def process_metadata() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "python": sys.version,
        "executable": sys.executable,
        "platform": sys.platform,
        "cwd": str(Path.cwd()),
        "argv": sanitize(sys.argv),
    }
