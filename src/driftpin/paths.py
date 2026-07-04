"""Locates repo-root config directories (`prompts/`, `agents/`) from any package module.

These directories live outside `src/driftpin` per the project layout. The repo
root is identified by its `pyproject.toml` marker rather than by walking up
looking for a directory of the target name — `src/driftpin/agents` (the
Python package) and `agents/` (the YAML config directory) share a name, so a
naive first-match search would find the wrong one. Editable-install
development is the only supported mode until Release 1's Docker packaging
stage bundles these directories explicitly.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT_MARKER = "pyproject.toml"


class RepoRootNotFoundError(Exception):
    def __init__(self, start: Path) -> None:
        super().__init__(f"Could not locate a '{_REPO_ROOT_MARKER}' above {start}")


class RepoDirectoryNotFoundError(Exception):
    def __init__(self, name: str, repo_root: Path) -> None:
        super().__init__(f"Could not locate '{repo_root / name}'")


def find_repo_root(start: Path) -> Path:
    for parent in [start.resolve(), *start.resolve().parents]:
        if (parent / _REPO_ROOT_MARKER).is_file():
            return parent
    raise RepoRootNotFoundError(start)


def find_repo_dir(name: str, start: Path) -> Path:
    repo_root = find_repo_root(start)
    candidate = repo_root / name
    if not candidate.is_dir():
        raise RepoDirectoryNotFoundError(name, repo_root)
    return candidate
