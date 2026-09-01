from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


CODE_EXTENSIONS = {
    ".cjs", ".cts", ".java", ".js", ".jsx", ".mjs", ".mts",
    ".py", ".ts", ".tsx", ".html", ".htm", ".css",
}
CONTEXT_EXTENSIONS = {
    ".bash", ".bat", ".cfg", ".cmd", ".conf", ".csv", ".gradle",
    ".ini", ".json", ".kts", ".md", ".mdx", ".properties", ".ps1",
    ".sh", ".toml", ".txt", ".xml", ".xsd", ".yaml", ".yml", ".zsh",
}
CONTEXT_NAMES = {
    ".env.example", "dockerfile", "gradlew", "justfile", "makefile", "mvnw",
}
SKIPPED_DIRECTORIES = {
    ".codex", ".git", ".hg", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", ".waypoint-clones", ".waypoint-data",
    "__pycache__", "build", "dist", "node_modules",
    "site-packages", "venv",
}


@dataclass(frozen=True, slots=True)
class IndexedFileState:
    path: str
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class IndexedAliasState:
    path: str
    canonical_path: str
    reason: str
    target_kind: str = "file"


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    fingerprint: str
    files: tuple[IndexedFileState, ...]
    aliases: tuple[IndexedAliasState, ...] = ()

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.files)

    @property
    def alias_map(self) -> dict[str, IndexedAliasState]:
        return {item.path: item for item in self.aliases}


def _relative_alias_target(
    candidate: Path,
    root: Path,
    *,
    pointer_text: str | None = None,
) -> tuple[str, str] | None:
    """Resolve a symlink or one-line relative pointer without escaping the repository."""
    try:
        if pointer_text is None:
            target = candidate.resolve(strict=True)
        else:
            value = pointer_text.strip().replace("\\", "/")
            if not value.startswith(("./", "../")) or "\n" in value or "\r" in value:
                return None
            target = (candidate.parent / value).resolve(strict=True)
        relative = target.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    return relative, "directory" if target.is_dir() else "file"


def _pointer_alias(candidate: Path, root: Path) -> tuple[str, str] | None:
    try:
        if candidate.stat().st_size > 512:
            return None
        value = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(value.splitlines()) != 1:
        return None
    return _relative_alias_target(candidate, root, pointer_text=value)


def _canonical_rank(path: str) -> tuple[int, int, int, str]:
    """Prefer visible, project-owned locations when identical context files exist."""
    parts = Path(path).parts
    hidden_parts = sum(part.startswith(".") for part in parts)
    first = parts[0].lower() if parts else ""
    preferred = {"src": 0, "skills": 1, "docs": 2, "documentation": 3}
    return (hidden_parts, preferred.get(first, 10), len(parts), path.lower())


def repository_snapshot(root: Path, max_files: int = 5_000) -> RepositorySnapshot:
    """Return a deterministic content snapshot of files Waypoint can retrieve."""
    resolved_root = root.resolve()
    states: list[IndexedFileState] = []
    aliases: list[IndexedAliasState] = []
    for current_root, directories, filenames in os.walk(resolved_root, followlinks=False):
        retained: list[str] = []
        for name in sorted(directories):
            candidate_directory = Path(current_root) / name
            if name in SKIPPED_DIRECTORIES:
                continue
            if candidate_directory.is_symlink():
                target = _relative_alias_target(candidate_directory, resolved_root)
                if target is not None:
                    aliases.append(IndexedAliasState(
                        candidate_directory.relative_to(resolved_root).as_posix(),
                        target[0],
                        "symlink",
                        target[1],
                    ))
                continue
            retained.append(name)
        directories[:] = retained
        for filename in sorted(filenames):
            candidate = Path(current_root) / filename
            if candidate.is_symlink():
                target = _relative_alias_target(candidate, resolved_root)
                if target is not None:
                    aliases.append(IndexedAliasState(
                        candidate.relative_to(resolved_root).as_posix(),
                        target[0],
                        "symlink",
                        target[1],
                    ))
                continue
            pointer = _pointer_alias(candidate, resolved_root)
            if pointer is not None:
                aliases.append(IndexedAliasState(
                    candidate.relative_to(resolved_root).as_posix(),
                    pointer[0],
                    "relative_pointer",
                    pointer[1],
                ))
                continue
            if (
                candidate.suffix.lower() not in CODE_EXTENSIONS | CONTEXT_EXTENSIONS
                and candidate.name.lower() not in CONTEXT_NAMES
            ):
                continue
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
            if len(states) + len(aliases) >= max_files:
                break
        if len(states) + len(aliases) >= max_files:
            break

    # Collapse byte-identical contextual documents while retaining every code file as a
    # distinct graph identity. Equal code in two modules can still have different import
    # semantics, whereas duplicate documentation only wastes retrieval context.
    by_hash: dict[str, list[IndexedFileState]] = {}
    for state in states:
        if Path(state.path).suffix.lower() not in CODE_EXTENSIONS:
            by_hash.setdefault(state.content_sha256, []).append(state)
    duplicate_paths: set[str] = set()
    for group in by_hash.values():
        if len(group) < 2:
            continue
        canonical = min(group, key=lambda item: _canonical_rank(item.path))
        for duplicate in group:
            if duplicate.path == canonical.path:
                continue
            duplicate_paths.add(duplicate.path)
            aliases.append(IndexedAliasState(
                duplicate.path,
                canonical.path,
                "exact_content_duplicate",
                "file",
            ))
    states = [item for item in states if item.path not in duplicate_paths]
    states.sort(key=lambda item: item.path)
    aliases.sort(key=lambda item: (item.path, item.canonical_path, item.reason))
    digest = hashlib.sha256()
    for item in states:
        digest.update(item.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.content_sha256.encode("ascii"))
        digest.update(b"\0")
    for alias in aliases:
        digest.update(alias.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(alias.canonical_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(alias.reason.encode("ascii"))
        digest.update(b"\0")
    return RepositorySnapshot(digest.hexdigest(), tuple(states), tuple(aliases))
