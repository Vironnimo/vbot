"""Tests for the canonical data-directory path and initialization contract."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from core.storage.layout import (
    DATA_DIRECTORY_RELATIVE_PATHS,
    DataDirectoryLayout,
    initialize_data_directory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESOURCE_TEMPLATE = PROJECT_ROOT / "resources" / "data-dir" / ".env.example"
PRODUCTION_SOURCE_ROOTS = (
    PROJECT_ROOT / "core",
    PROJECT_ROOT / "server",
    PROJECT_ROOT / "cli",
    PROJECT_ROOT / "scripts",
)
LEGACY_DATA_ROOT_JOIN = re.compile(
    r"""
    (?:
        data_dir
        |data_root
        |self\._data_dir
        |storage\.data_dir
    )
    [^\n]{0,80}
    /
    \s*["']
    (?:\.tmp|temp|attachments|images|speech|models|debug|provider-usage)
    ["']
    """,
    re.VERBOSE,
)


def test_layout_exposes_every_canonical_named_path(tmp_path: Path) -> None:
    layout = DataDirectoryLayout(tmp_path)

    assert layout.attachments == tmp_path / "artifacts" / "attachments"
    assert layout.speech == tmp_path / "artifacts" / "speech"
    assert layout.models == tmp_path / "artifacts" / "models"
    assert layout.debug == tmp_path / "artifacts" / "debug"
    assert layout.atomic_temporary == tmp_path / "artifacts" / "temp" / "atomic"
    assert layout.bash_temporary == tmp_path / "artifacts" / "temp" / "bash"
    assert layout.subagent_temporary == tmp_path / "artifacts" / "temp" / "subagents"
    assert layout.terminal_temporary == tmp_path / "artifacts" / "temp" / "terminals"
    assert layout.provider_usage == tmp_path / "statistics" / "provider-usage"
    assert layout.bootstrap == tmp_path / "bootstrap"
    assert layout.processes == tmp_path / "processes"
    assert layout.terminals == tmp_path / "terminals"
    assert layout.environment_file == tmp_path / ".env"
    assert layout.settings_file == tmp_path / "settings.json"


def test_initialize_creates_exact_canonical_layout(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    result = initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")

    actual_directories = {
        path.relative_to(data_dir) for path in data_dir.rglob("*") if path.is_dir()
    }
    actual_files = {path.relative_to(data_dir) for path in data_dir.rglob("*") if path.is_file()}
    assert actual_directories == set(DATA_DIRECTORY_RELATIVE_PATHS)
    assert actual_files == {Path(".env"), Path("settings.json")}
    assert (data_dir / ".env").read_bytes() == RESOURCE_TEMPLATE.read_bytes()
    assert (data_dir / "settings.json").read_bytes() == b"{}\n"
    assert result.layout.root == data_dir


def test_initialize_preserves_existing_configuration_bytes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    environment_bytes = b"# existing\r\nTOKEN=value\r\n"
    settings_bytes = b'{"deliberately": "unformatted"}'
    (data_dir / ".env").write_bytes(environment_bytes)
    (data_dir / "settings.json").write_bytes(settings_bytes)

    first = initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")
    second = initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")

    assert (data_dir / ".env").read_bytes() == environment_bytes
    assert (data_dir / "settings.json").read_bytes() == settings_bytes
    assert first.created_files == ()
    assert second.created_directories == ()
    assert second.created_files == ()


def test_initialize_uses_empty_environment_when_template_is_unavailable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / "data"
    template_path = tmp_path / "missing-resources" / "data-dir" / ".env.example"

    with caplog.at_level("WARNING", logger="vbot.storage"):
        result = initialize_data_directory(
            data_dir,
            resources_dir=tmp_path / "missing-resources",
        )

    assert (data_dir / ".env").read_bytes() == b""
    assert (data_dir / "settings.json").read_bytes() == b"{}\n"
    assert result.created_files == (data_dir / ".env", data_dir / "settings.json")
    assert str(template_path) in caplog.text


def test_layout_cli_initializes_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "cli-data"

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "core" / "storage" / "layout.py"),
            str(data_dir),
            "--resources-dir",
            str(PROJECT_ROOT / "resources"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "created_directories=" in result.stdout
    assert all((data_dir / path).is_dir() for path in DATA_DIRECTORY_RELATIVE_PATHS)


def test_production_sources_do_not_join_legacy_data_root_paths() -> None:
    violations: list[str] = []

    for source_root in PRODUCTION_SOURCE_ROOTS:
        for source_file in source_root.rglob("*.py"):
            if source_file.parent == PROJECT_ROOT / "scripts" / "converters":
                continue
            if LEGACY_DATA_ROOT_JOIN.search(source_file.read_text(encoding="utf-8")):
                violations.append(str(source_file.relative_to(PROJECT_ROOT)))

    assert violations == []
