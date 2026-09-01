from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from backend.app.observability import log_event, traced
from backend.app.processes import run_logged_process

logger = logging.getLogger(__name__)

_GITHUB_COMPONENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$")


class RepositoryImportError(ValueError):
    """Base error for a repository that cannot be imported safely."""


class InvalidGitHubRepository(RepositoryImportError):
    pass


class GitUnavailableError(RepositoryImportError):
    pass


class GitCloneError(RepositoryImportError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    owner: str
    name: str
    clone_url: str


@traced("repository.github_url.validate")
def parse_github_repository(value: str) -> GitHubRepository:
    candidate = value.strip()
    if candidate.lower().startswith("github.com/"):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme.lower() != "https" or parsed.hostname != "github.com":
        raise InvalidGitHubRepository(
            "Enter a public HTTPS GitHub URL such as "
            "https://github.com/owner/repository"
        )
    if parsed.username or parsed.password:
        raise InvalidGitHubRepository(
            "GitHub URLs containing credentials are not accepted"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidGitHubRepository("The GitHub URL has an invalid port") from exc
    if port is not None or parsed.query or parsed.fragment or parsed.params:
        raise InvalidGitHubRepository(
            "GitHub URL ports, query strings, and fragments are not accepted"
        )
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise InvalidGitHubRepository(
            "The URL must identify one repository: github.com/owner/repository"
        )
    owner, name = parts
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not owner or not name or not all(
        _GITHUB_COMPONENT.fullmatch(part) for part in (owner, name)
    ):
        raise InvalidGitHubRepository(
            "The GitHub owner or repository name contains unsupported characters"
        )
    return GitHubRepository(
        owner=owner,
        name=name,
        clone_url=f"https://github.com/{owner}/{name}.git",
    )


class GitHubRepositoryCloner:
    def __init__(
        self,
        clone_root: Path,
        allowed_root: Path,
        timeout_seconds: int,
        max_clone_bytes: int = 1_000_000_000,
        max_clone_files: int = 100_000,
        max_retained_clones: int = 10,
    ) -> None:
        self.clone_root = clone_root.resolve()
        self.allowed_root = allowed_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.max_clone_bytes = max_clone_bytes
        self.max_clone_files = max_clone_files
        self.max_retained_clones = max_retained_clones

    def _validated_clone_root(self) -> Path:
        try:
            self.clone_root.relative_to(self.allowed_root)
        except ValueError as exc:
            raise RepositoryImportError(
                "ONBOARD_CLONE_ROOT must be inside ONBOARD_ALLOWED_ROOT"
            ) from exc
        self.clone_root.mkdir(parents=True, exist_ok=True)
        if not self.clone_root.is_dir():
            raise RepositoryImportError("The secure clone directory is unavailable")
        return self.clone_root

    @traced("repository.github_clone")
    def clone(self, repository_url: str) -> Path:
        repository = parse_github_repository(repository_url)
        clone_root = self._validated_clone_root()
        git = shutil.which("git")
        if git is None:
            raise GitUnavailableError(
                "Git is not installed or is not available on the server PATH"
            )
        destination = clone_root / (
            f"{repository.owner}--{repository.name}--{uuid.uuid4().hex[:10]}"
        )
        destination.resolve().relative_to(clone_root)
        log_event(
            logger,
            logging.INFO,
            "repository.clone_requested",
            "Validated GitHub repository clone requested",
            owner=repository.owner,
            repository=repository.name,
            clone_url=repository.clone_url,
            destination=destination,
            shallow=True,
        )
        command = [
            git,
            "-c",
            "credential.helper=",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--no-tags",
            "--single-branch",
            "--",
            repository.clone_url,
            str(destination),
        ]
        try:
            result = run_logged_process(
                command,
                cwd=clone_root,
                timeout_seconds=self.timeout_seconds,
                environment={
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "Never",
                },
            )
        except subprocess.TimeoutExpired as exc:
            self._cleanup(destination)
            raise GitCloneError(
                f"GitHub clone exceeded the {self.timeout_seconds}-second limit"
            ) from exc
        except OSError as exc:
            self._cleanup(destination)
            raise GitCloneError(f"Git could not be started: {exc}") from exc
        if result.return_code != 0:
            self._cleanup(destination)
            detail = result.stderr.strip().splitlines()
            safe_detail = detail[-1][:500] if detail else "Git returned an error"
            raise GitCloneError(f"GitHub clone failed: {safe_detail}")
        if not destination.is_dir() or not (destination / ".git").exists():
            self._cleanup(destination)
            raise GitCloneError("Git completed without creating a valid repository")
        try:
            file_count, size_bytes = self._checkout_size(destination)
        except OSError as exc:
            self._cleanup(destination)
            raise GitCloneError(
                f"Cloned repository could not be inspected safely: {exc}"
            ) from exc
        if (
            file_count > self.max_clone_files
            or size_bytes > self.max_clone_bytes
        ):
            self._cleanup(destination)
            raise GitCloneError(
                "Cloned repository exceeds the configured storage limits "
                f"({file_count} files, {size_bytes} bytes)"
            )
        try:
            (destination / ".waypoint-managed").write_text(
                repository.clone_url,
                encoding="utf-8",
            )
        except OSError as exc:
            self._cleanup(destination)
            raise GitCloneError(
                f"Cloned repository could not be registered safely: {exc}"
            ) from exc
        self._prune_old_clones(destination)
        log_event(
            logger,
            logging.INFO,
            "repository.clone_completed",
            "GitHub repository cloned into secure application storage",
            owner=repository.owner,
            repository=repository.name,
            destination=destination,
            duration_ms=result.duration_ms,
            checkout_files=file_count,
            checkout_bytes=size_bytes,
        )
        return destination.resolve()

    def _prune_old_clones(self, current: Path) -> None:
        managed = sorted(
            (
                candidate
                for candidate in self.clone_root.iterdir()
                if candidate.is_dir()
                and (candidate / ".waypoint-managed").is_file()
                and candidate.resolve() != current.resolve()
            ),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        removed: list[str] = []
        for stale in managed[max(0, self.max_retained_clones - 1) :]:
            self._cleanup(stale)
            removed.append(stale.name)
        if removed:
            log_event(
                logger,
                logging.INFO,
                "repository.clone_retention_applied",
                "Old managed repository clones were removed",
                retained_limit=self.max_retained_clones,
                removed=removed,
            )

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

    def discard(self, destination: Path) -> None:
        self._cleanup(destination)

    def _cleanup(self, destination: Path) -> None:
        resolved = destination.resolve()
        try:
            resolved.relative_to(self.clone_root)
        except ValueError:
            log_event(
                logger,
                logging.ERROR,
                "security.clone_cleanup_rejected",
                "Refused clone cleanup outside the configured clone root",
                destination=resolved,
                clone_root=self.clone_root,
            )
            return
        if resolved.exists():
            shutil.rmtree(resolved)
            log_event(
                logger,
                logging.INFO,
                "repository.clone_cleaned",
                "Incomplete cloned repository was removed",
                destination=resolved,
            )
