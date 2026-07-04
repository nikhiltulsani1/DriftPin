from __future__ import annotations

from pathlib import Path

import pytest

from driftpin.paths import (
    RepoDirectoryNotFoundError,
    RepoRootNotFoundError,
    find_repo_dir,
    find_repo_root,
)


def test_find_repo_root_locates_pyproject_toml() -> None:
    root = find_repo_root(Path(__file__))
    assert (root / "pyproject.toml").is_file()


def test_find_repo_dir_distinguishes_same_named_package_and_config_dir() -> None:
    agents_config_dir = find_repo_dir("agents", Path(__file__))
    assert (agents_config_dir / "test-architect.yaml").is_file()
    assert "src" not in agents_config_dir.parts


def test_find_repo_dir_raises_for_nonexistent_directory() -> None:
    with pytest.raises(RepoDirectoryNotFoundError):
        find_repo_dir("this-directory-does-not-exist", Path(__file__))


def test_find_repo_root_raises_when_no_marker_found(tmp_path: Path) -> None:
    isolated = tmp_path / "a" / "b" / "c"
    isolated.mkdir(parents=True)
    with pytest.raises(RepoRootNotFoundError):
        find_repo_root(isolated)
