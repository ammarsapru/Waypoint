from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from backend.app.observability import log_event, sanitize, traced

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    duration_ms: float


@traced("subprocess.run")
def run_logged_process(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float = 60.0,
    environment: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run a shell-free subprocess and stream every output line to diagnostics."""
    if not command:
        raise ValueError("A subprocess command cannot be empty")
    sanitized_command = sanitize(list(command))
    started = time.perf_counter()
    log_event(
        logger,
        logging.INFO,
        "subprocess.starting",
        "Starting subprocess",
        command=sanitized_command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        environment_keys=sorted(environment) if environment else [],
    )
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=process_environment,
    )
    log_event(
        logger,
        logging.INFO,
        "subprocess.started",
        "Subprocess created",
        child_pid=process.pid,
        command=sanitized_command,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def consume(stream: object, destination: list[str], channel: str) -> None:
        assert hasattr(stream, "__iter__")
        for line in stream:  # type: ignore[union-attr]
            clean = line.rstrip("\r\n")
            destination.append(clean)
            log_event(
                logger,
                logging.DEBUG,
                f"subprocess.{channel}",
                f"Subprocess {channel}",
                child_pid=process.pid,
                line=clean,
            )

    stdout_thread = threading.Thread(
        target=consume,
        args=(process.stdout, stdout_lines, "stdout"),
        name=f"stdout-{process.pid}",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=consume,
        args=(process.stderr, stderr_lines, "stderr"),
        name=f"stderr-{process.pid}",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        log_event(
            logger,
            logging.ERROR,
            "subprocess.timeout",
            "Subprocess exceeded its timeout and was killed",
            child_pid=process.pid,
            timeout_seconds=timeout_seconds,
        )
        raise
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    result = ProcessResult(
        command=tuple(command),
        return_code=return_code,
        stdout="\n".join(stdout_lines),
        stderr="\n".join(stderr_lines),
        duration_ms=duration_ms,
    )
    log_event(
        logger,
        logging.INFO if return_code == 0 else logging.ERROR,
        "subprocess.completed",
        "Subprocess completed",
        child_pid=process.pid,
        return_code=return_code,
        duration_ms=duration_ms,
        stdout_lines=len(stdout_lines),
        stderr_lines=len(stderr_lines),
    )
    return result
