from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.processes import ProcessResult
from backend.app.repository_import import (
    GitHubRepositoryCloner,
    InvalidGitHubRepository,
    RepositoryImportError,
    parse_github_repository,
)


class GitHubRepositoryImportTests(unittest.TestCase):
    def test_accepts_only_canonical_public_github_repositories(self) -> None:
        parsed = parse_github_repository("github.com/pallets/flask.git")
        self.assertEqual(parsed.owner, "pallets")
        self.assertEqual(parsed.name, "flask")
        self.assertEqual(
            parsed.clone_url, "https://github.com/pallets/flask.git"
        )

        rejected = (
            "http://github.com/pallets/flask",
            "https://gitlab.com/pallets/flask",
            "https://token@github.com/pallets/flask",
            "https://github.com:443/pallets/flask",
            "https://github.com/pallets/flask/tree/main",
            "https://github.com/pallets/flask?tab=readme",
            "file:///C:/repositories/flask",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(InvalidGitHubRepository):
                    parse_github_repository(value)

    def test_clone_is_shallow_noninteractive_and_inside_secure_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            allowed_root = Path(temporary_directory)
            clone_root = allowed_root / ".waypoint-clones"

            def fake_process(command: list[str], **kwargs: object) -> ProcessResult:
                destination = Path(command[-1])
                destination.mkdir(parents=True)
                (destination / ".git").mkdir()
                (destination / "app.py").write_text(
                    "def hello():\n    return 'world'\n", encoding="utf-8"
                )
                self.assertIn("--depth", command)
                self.assertIn("--filter=blob:none", command)
                self.assertEqual(
                    kwargs["environment"],
                    {
                        "GIT_TERMINAL_PROMPT": "0",
                        "GCM_INTERACTIVE": "Never",
                    },
                )
                return ProcessResult(
                    command=tuple(command),
                    return_code=0,
                    stdout="",
                    stderr="",
                    duration_ms=12.5,
                )

            cloner = GitHubRepositoryCloner(
                clone_root=clone_root,
                allowed_root=allowed_root,
                timeout_seconds=30,
            )
            with (
                patch(
                    "backend.app.repository_import.shutil.which",
                    return_value="git",
                ),
                patch(
                    "backend.app.repository_import.run_logged_process",
                    side_effect=fake_process,
                ),
            ):
                destination = cloner.clone(
                    "https://github.com/pallets/flask"
                )
            destination.relative_to(clone_root)
            self.assertTrue((destination / "app.py").is_file())

    def test_clone_root_must_remain_inside_allowed_root(self) -> None:
        with tempfile.TemporaryDirectory() as allowed_directory:
            with tempfile.TemporaryDirectory() as outside_directory:
                cloner = GitHubRepositoryCloner(
                    clone_root=Path(outside_directory),
                    allowed_root=Path(allowed_directory),
                    timeout_seconds=30,
                )
                with self.assertRaises(RepositoryImportError):
                    cloner.clone("https://github.com/pallets/flask")

    def test_oversized_checkout_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            allowed_root = Path(temporary_directory)
            clone_root = allowed_root / ".waypoint-clones"

            def fake_process(command: list[str], **_: object) -> ProcessResult:
                destination = Path(command[-1])
                destination.mkdir(parents=True)
                (destination / ".git").mkdir()
                (destination / "large.py").write_bytes(b"x" * 256)
                return ProcessResult(
                    command=tuple(command),
                    return_code=0,
                    stdout="",
                    stderr="",
                    duration_ms=1,
                )

            cloner = GitHubRepositoryCloner(
                clone_root=clone_root,
                allowed_root=allowed_root,
                timeout_seconds=30,
                max_clone_bytes=128,
            )
            with (
                patch(
                    "backend.app.repository_import.shutil.which",
                    return_value="git",
                ),
                patch(
                    "backend.app.repository_import.run_logged_process",
                    side_effect=fake_process,
                ),
            ):
                with self.assertRaisesRegex(
                    RepositoryImportError, "storage limits"
                ):
                    cloner.clone("https://github.com/pallets/flask")
            self.assertEqual(list(clone_root.iterdir()), [])

    def test_managed_clone_retention_removes_old_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            allowed_root = Path(temporary_directory)
            clone_root = allowed_root / ".waypoint-clones"
            old_clone = clone_root / "owner--old--0000000000"
            old_clone.mkdir(parents=True)
            (old_clone / ".waypoint-managed").write_text(
                "https://github.com/owner/old.git", encoding="utf-8"
            )

            def fake_process(command: list[str], **_: object) -> ProcessResult:
                destination = Path(command[-1])
                destination.mkdir(parents=True)
                (destination / ".git").mkdir()
                (destination / "module.py").write_text(
                    "value = 1\n", encoding="utf-8"
                )
                return ProcessResult(
                    command=tuple(command),
                    return_code=0,
                    stdout="",
                    stderr="",
                    duration_ms=1,
                )

            cloner = GitHubRepositoryCloner(
                clone_root=clone_root,
                allowed_root=allowed_root,
                timeout_seconds=30,
                max_retained_clones=1,
            )
            with (
                patch(
                    "backend.app.repository_import.shutil.which",
                    return_value="git",
                ),
                patch(
                    "backend.app.repository_import.run_logged_process",
                    side_effect=fake_process,
                ),
            ):
                current = cloner.clone("https://github.com/owner/current")
            self.assertFalse(old_clone.exists())
            self.assertTrue(current.exists())
            self.assertEqual(
                [item.name for item in clone_root.iterdir()],
                [current.name],
            )


if __name__ == "__main__":
    unittest.main()
