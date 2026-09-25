"""Tests for the canonical data-directory path and initialization contract."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from core.json_documents import render_json_document
from core.settings import SETTINGS_FORMAT_VERSION, validate_settings_file
from core.storage.layout import (
    DATA_DIRECTORY_RELATIVE_PATHS,
    INITIAL_SETTINGS_DOCUMENT,
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
    assert layout.performance == tmp_path / "artifacts" / "performance"
    assert layout.atomic_temporary == tmp_path / "artifacts" / "temp" / "atomic"
    assert layout.bash_temporary == tmp_path / "artifacts" / "temp" / "bash"
    assert layout.subagent_temporary == tmp_path / "artifacts" / "temp" / "subagents"
    assert layout.terminal_temporary == tmp_path / "artifacts" / "temp" / "terminals"
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
    assert actual_files == {Path(".env"), Path("settings.json"), Path("data-store.json")}
    assert (data_dir / ".env").read_bytes() == RESOURCE_TEMPLATE.read_bytes()
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == INITIAL_SETTINGS_DOCUMENT
    assert result.layout.root == data_dir


def test_initial_settings_document_is_an_empty_current_settings_document(tmp_path: Path) -> None:
    assert render_json_document({}, version=SETTINGS_FORMAT_VERSION) == INITIAL_SETTINGS_DOCUMENT
    initialize_data_directory(tmp_path, resources_dir=PROJECT_ROOT / "resources")

    report = validate_settings_file(tmp_path / "settings.json")

    assert report.exists
    assert report.diagnostics == ()


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
    # A pre-created root is not a genuinely new root, so initialization does
    # not manufacture Session-store authorization for it.
    assert first.created_files == ()
    assert second.created_directories == ()
    assert second.created_files == ()


def _race_mkdir(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    *,
    concurrent_creator: str = "directory",
) -> None:
    """Let a simulated concurrent initializer create *target* first.

    The existence check still sees the path missing; this call's ``mkdir``
    then loses the race and raises ``FileExistsError`` like the OS does.
    """

    original_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == target:
            if concurrent_creator == "directory":
                original_mkdir(self, parents=True)
            else:
                self.write_bytes(b"not a directory")
            raise FileExistsError(str(self))
        original_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)


def test_initialize_losing_root_creation_race_never_writes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _race_mkdir(monkeypatch, data_dir)

    result = initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")

    # The concurrent creator owns the root and publishes its marker; the loser
    # treats the root as existing and never manufactures authorization.
    assert not (data_dir / "data-store.json").exists()
    assert data_dir / "data-store.json" not in result.created_files
    assert data_dir not in result.created_directories
    assert all((data_dir / path).is_dir() for path in DATA_DIRECTORY_RELATIVE_PATHS)


@pytest.mark.parametrize("concurrent", [False, True])
def test_initialize_require_new_leaves_existing_root_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent: bool
) -> None:
    data_dir = tmp_path / "data"
    if concurrent:
        _race_mkdir(monkeypatch, data_dir)
    else:
        data_dir.mkdir()

    with pytest.raises(FileExistsError):
        initialize_data_directory(
            data_dir, resources_dir=PROJECT_ROOT / "resources", require_new=True
        )

    assert list(data_dir.iterdir()) == []


def test_initialize_require_new_creates_authorized_canonical_layout(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    result = initialize_data_directory(
        data_dir, resources_dir=PROJECT_ROOT / "resources", require_new=True
    )

    assert data_dir in result.created_directories
    assert (data_dir / "data-store.json").is_file()
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == INITIAL_SETTINGS_DOCUMENT
    assert all((data_dir / path).is_dir() for path in DATA_DIRECTORY_RELATIVE_PATHS)


def test_initialize_tolerates_concurrently_created_canonical_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    contested = data_dir / "artifacts" / "temp" / "bash"
    _race_mkdir(monkeypatch, contested)

    result = initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")

    assert contested.is_dir()
    assert contested not in result.created_directories
    assert data_dir in result.created_directories
    assert data_dir / "data-store.json" in result.created_files


def test_initialize_rejects_concurrently_created_non_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    contested = data_dir / "logs"
    _race_mkdir(monkeypatch, contested, concurrent_creator="file")

    with pytest.raises(NotADirectoryError):
        initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")


def test_initialize_rejects_existing_non_directory_root(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.write_bytes(b"not a directory")

    with pytest.raises(NotADirectoryError):
        initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")


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
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == INITIAL_SETTINGS_DOCUMENT
    assert result.created_files == (
        data_dir / "data-store.json",
        data_dir / ".env",
        data_dir / "settings.json",
    )
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
            if LEGACY_DATA_ROOT_JOIN.search(source_file.read_text(encoding="utf-8")):
                violations.append(str(source_file.relative_to(PROJECT_ROOT)))

    assert violations == []
