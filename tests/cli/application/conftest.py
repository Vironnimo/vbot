"""Fixtures for packaged application tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.cli.application.git_repositories import (
    SourceRepositories,
    Templates,
    build_templates,
    copy_plain_repository,
    copy_source_repositories,
)


@pytest.fixture(scope="session")
def repository_templates(tmp_path_factory: pytest.TempPathFactory) -> Templates:
    return build_templates(tmp_path_factory.mktemp("git-templates"))


@pytest.fixture
def source_repositories(repository_templates: Templates, tmp_path: Path) -> SourceRepositories:
    """A checkout tracking a local remote, plus a publisher clone of that remote."""

    return copy_source_repositories(repository_templates, tmp_path / "repositories")


@pytest.fixture
def plain_repository(repository_templates: Templates) -> Callable[[Path], str]:
    """Create a committed repository without a remote at a path; return its HEAD."""

    return lambda destination: copy_plain_repository(repository_templates, destination)
