"""Tests for the canonical data-directory path and initialization contract."""

from __future__ import annotations

import re
import runpy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.database import marker as marker_module
from core.database import read_marker
from core.json_documents import render_json_document
from core.settings import SETTINGS_FORMAT_VERSION, validate_settings_file
from core.storage import layout as layout_module
from core.storage.layout import (
    DATA_DIRECTORY_RELATIVE_PATHS,
    INITIAL_SETTINGS_DOCUMENT,
    DataDirectoryLayout,
    initialize_data_directory,
)
from core.storage.storage import StorageManager

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
    assert layout.command_temporary == tmp_path / "artifacts" / "temp" / "commands"
    assert layout.scratch_temporary == tmp_path / "artifacts" / "temp" / "scratch"
    assert layout.scratch_state_temporary == tmp_path / "artifacts" / "temp" / "scratch-state"
    assert layout.subagent_temporary == tmp_path / "artifacts" / "temp" / "subagents"
    assert layout.terminal_temporary == tmp_path / "artifacts" / "temp" / "terminals"
    assert layout.bootstrap == tmp_path / "bootstrap"
    assert layout.processes == tmp_path / "processes"
    assert layout.terminals == tmp_path / "terminals"
    assert layout.environment_file == tmp_path / ".env"
    assert layout.settings_file == tmp_path / "settings.json"


@pytest.mark.parametrize("require_new", [False, True])
def test_initialize_creates_exact_canonical_layout(tmp_path: Path, require_new: bool) -> None:
    data_dir = tmp_path / "data"

    result = initialize_data_directory(
        data_dir, resources_dir=PROJECT_ROOT / "resources", require_new=require_new
    )

    actual_directories = {
        path.relative_to(data_dir) for path in data_dir.rglob("*") if path.is_dir()
    }
    actual_files = {path.relative_to(data_dir) for path in data_dir.rglob("*") if path.is_file()}
    assert actual_directories == set(DATA_DIRECTORY_RELATIVE_PATHS)
    assert actual_files == {Path(".env"), Path("settings.json"), Path("data-store.json")}
    assert (data_dir / ".env").read_bytes() == RESOURCE_TEMPLATE.read_bytes()
    assert (data_dir / "settings.json").read_text(encoding="utf-8") == INITIAL_SETTINGS_DOCUMENT
    assert render_json_document({}, version=SETTINGS_FORMAT_VERSION) == INITIAL_SETTINGS_DOCUMENT
    report = validate_settings_file(data_dir / "settings.json")
    assert report.exists
    assert report.diagnostics == ()
    assert result.layout.root == data_dir
    assert data_dir in result.created_directories
    assert data_dir / "data-store.json" in result.created_files


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


def _race_root_publication(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    original = layout_module._publish_root

    def publish(staging: Path, target: Path) -> None:
        assert target == root
        root.mkdir()
        original(staging, target)

    monkeypatch.setattr(layout_module, "_publish_root", publish)


def test_initialize_losing_root_creation_race_never_writes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    _race_root_publication(monkeypatch, data_dir)

    result = initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")

    # An unrelated creator's empty directory stays unmarked, including on Linux
    # where an ordinary rename could replace it with the staged directory.
    assert not (data_dir / "data-store.json").exists()
    assert data_dir / "data-store.json" not in result.created_files
    assert data_dir not in result.created_directories
    assert all((data_dir / path).is_dir() for path in DATA_DIRECTORY_RELATIVE_PATHS)
    assert list(tmp_path.iterdir()) == [data_dir]


def test_concurrent_initializers_never_observe_an_unmarked_new_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    writing, release = threading.Event(), threading.Event()
    original = marker_module.write_bootstrap_marker

    def pause_first_writer(root: Path) -> marker_module.DataStoreMarker:
        if not writing.is_set():
            writing.set()
            assert release.wait(5), "concurrent initializer did not finish"
        return original(root)

    monkeypatch.setattr(marker_module, "write_bootstrap_marker", pause_first_writer)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(initialize_data_directory, data_dir)
        try:
            assert writing.wait(5), "initializer did not reach its marker write"
            assert not data_dir.exists()
            second = initialize_data_directory(data_dir)
            assert read_marker(data_dir) is not None
        finally:
            release.set()
        first_result = first.result(timeout=5)

    assert data_dir in second.created_directories
    assert data_dir / "data-store.json" in second.created_files
    assert data_dir not in first_result.created_directories
    assert data_dir / "data-store.json" not in first_result.created_files
    assert list(tmp_path.iterdir()) == [data_dir]


def test_failed_marker_write_leaves_the_new_data_path_available_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    original = marker_module.write_bootstrap_marker

    def failed_write(root: Path) -> marker_module.DataStoreMarker:
        (root / "data-store.json").write_text("{", encoding="utf-8")
        raise OSError("interrupted marker write")

    monkeypatch.setattr(marker_module, "write_bootstrap_marker", failed_write)
    with pytest.raises(OSError):
        initialize_data_directory(data_dir)
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(marker_module, "write_bootstrap_marker", original)
    initialize_data_directory(data_dir)
    assert read_marker(data_dir) is not None


@pytest.mark.parametrize("concurrent", [False, True])
def test_initialize_require_new_leaves_existing_root_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent: bool
) -> None:
    data_dir = tmp_path / "data"
    if concurrent:
        _race_root_publication(monkeypatch, data_dir)
    else:
        data_dir.mkdir()

    with pytest.raises(FileExistsError):
        initialize_data_directory(
            data_dir, resources_dir=PROJECT_ROOT / "resources", require_new=True
        )

    assert list(data_dir.iterdir()) == []


def test_initialize_tolerates_concurrently_created_canonical_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    contested = data_dir / "artifacts" / "temp" / "commands"
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


class _Config:
    def __init__(self, values: dict[str, str], data_dir: Path | None = None) -> None:
        self.values = values
        if data_dir is not None:
            self.data_dir = data_dir

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


@pytest.mark.parametrize("source", ["attribute", "value"])
def test_storage_manager_resolves_data_dir_from_config(tmp_path: Path, source: str) -> None:
    data_dir = tmp_path / "configured"
    config = (
        _Config({}, data_dir) if source == "attribute" else _Config({"DATA_DIR": str(data_dir)})
    )

    assert StorageManager(config=config).data_dir == data_dir


def test_layout_cli_initializes_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The setup scripts run this file as a script; run it as ``__main__`` in-process.
    data_dir = tmp_path / "cli-data"
    script = PROJECT_ROOT / "core" / "storage" / "layout.py"
    monkeypatch.setattr(
        sys,
        "argv",
        [str(script), str(data_dir), "--resources-dir", str(PROJECT_ROOT / "resources")],
    )

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")

    assert exit_info.value.code == 0, capsys.readouterr().err
    assert "created_directories=" in capsys.readouterr().out
    assert all((data_dir / path).is_dir() for path in DATA_DIRECTORY_RELATIVE_PATHS)


def test_production_sources_do_not_join_legacy_data_root_paths() -> None:
    violations: list[str] = []

    for source_root in PRODUCTION_SOURCE_ROOTS:
        for source_file in source_root.rglob("*.py"):
            if LEGACY_DATA_ROOT_JOIN.search(source_file.read_text(encoding="utf-8")):
                violations.append(str(source_file.relative_to(PROJECT_ROOT)))

    assert violations == []
