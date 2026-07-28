"""Tests for the explicit canonical data-directory layout converter."""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

from core.storage.layout import (
    DATA_DIRECTORY_RELATIVE_PATHS,
    DataDirectoryLayout,
    initialize_data_directory,
)

_CONVERTER = import_module("scripts.converters.data_dir_artifacts_layout")
LEGACY_DIRECTORY_MAPPINGS = _CONVERTER.LEGACY_DIRECTORY_MAPPINGS
DataDirectoryConversionError = _CONVERTER.DataDirectoryConversionError
apply_data_directory_conversion = _CONVERTER.apply_data_directory_conversion
plan_data_directory_conversion = _CONVERTER.plan_data_directory_conversion

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _write_complete_legacy_tree(data_dir: Path) -> dict[Path, bytes]:
    expected: dict[Path, bytes] = {}
    for index, mapping in enumerate(LEGACY_DIRECTORY_MAPPINGS):
        source = data_dir / mapping.source_relative / "nested" / f"payload-{index}.bin"
        source.parent.mkdir(parents=True, exist_ok=True)
        payload = f"legacy-{index}".encode()
        source.write_bytes(payload)
        expected[source] = payload
    return expected


def test_dry_run_reports_complete_mapping_without_changes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    expected = _write_complete_legacy_tree(data_dir)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "converters" / "data_dir_artifacts_layout.py"),
            str(data_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dry-run only" in result.stdout
    assert result.stdout.count(" -> ") == len(LEGACY_DIRECTORY_MAPPINGS)
    assert all(path.read_bytes() == payload for path, payload in expected.items())
    assert not DataDirectoryLayout(data_dir).artifacts.exists()


def test_apply_moves_complete_tree_and_preserves_independent_roots(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    expected = _write_complete_legacy_tree(data_dir)
    independent = data_dir / "agents" / "main" / "agent.json"
    independent.parent.mkdir(parents=True)
    independent.write_bytes(b'{"id":"main"}')
    (data_dir / ".env").write_bytes(b"TOKEN=preserve\r\n")
    (data_dir / "settings.json").write_bytes(b'{"preserve":true}')
    layout = DataDirectoryLayout(data_dir)

    result = apply_data_directory_conversion(data_dir)

    assert result.moved_files == len(expected)
    assert independent.read_bytes() == b'{"id":"main"}'
    assert (data_dir / ".env").read_bytes() == b"TOKEN=preserve\r\n"
    assert (data_dir / "settings.json").read_bytes() == b'{"preserve":true}'
    for mapping in LEGACY_DIRECTORY_MAPPINGS:
        source_root = data_dir / mapping.source_relative
        destination_root = getattr(layout, mapping.destination_attribute)
        original = (
            source_root / "nested" / (f"payload-{LEGACY_DIRECTORY_MAPPINGS.index(mapping)}.bin")
        )
        destination = destination_root / original.relative_to(source_root)
        assert destination.read_bytes() == expected[original]
        assert not source_root.exists()
    assert all((data_dir / path).is_dir() for path in DATA_DIRECTORY_RELATIVE_PATHS)


def test_apply_is_noop_for_already_current_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    initialize_data_directory(data_dir, resources_dir=PROJECT_ROOT / "resources")

    plan = plan_data_directory_conversion(data_dir)
    result = apply_data_directory_conversion(data_dir)

    assert plan.moves == ()
    assert result.moved_files == 0


def test_apply_merges_noncolliding_current_target_tree(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    legacy = data_dir / "attachments" / "legacy.bin"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    current = DataDirectoryLayout(data_dir).attachments / "current.bin"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"current")

    apply_data_directory_conversion(data_dir)

    assert (current.parent / "legacy.bin").read_bytes() == b"legacy"
    assert current.read_bytes() == b"current"


def test_apply_preserves_retired_legacy_skill_drafts_in_place(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    draft = data_dir / "temp" / "skill-drafts" / "draft-1" / "SKILL.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("unfinished\n", encoding="utf-8")

    result = apply_data_directory_conversion(data_dir)

    assert result.moved_files == 0
    assert draft.read_text(encoding="utf-8") == "unfinished\n"
    assert not (data_dir / "artifacts" / "temp" / "skill-drafts").exists()


def test_preflight_rejects_destination_collision_before_any_move(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first = data_dir / "attachments" / "first.bin"
    second = data_dir / "images" / "second.bin"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    collision = DataDirectoryLayout(data_dir).images / "second.bin"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"collision")

    with pytest.raises(DataDirectoryConversionError, match="Destination collision"):
        apply_data_directory_conversion(data_dir)

    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_preflight_rejects_unknown_legacy_temp_category(tmp_path: Path) -> None:
    unsupported = tmp_path / "data" / "temp" / "other" / "payload.bin"
    unsupported.parent.mkdir(parents=True)
    unsupported.write_bytes(b"payload")

    with pytest.raises(DataDirectoryConversionError, match="Unsupported legacy temp"):
        plan_data_directory_conversion(tmp_path / "data")


def test_preflight_rejects_source_symlink(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    source = data_dir / "attachments"
    source.mkdir(parents=True)
    external = tmp_path / "external.bin"
    external.write_bytes(b"external")
    try:
        (source / "link.bin").symlink_to(external)
    except OSError as error:
        pytest.skip(f"Symlink creation is unavailable: {error}")

    with pytest.raises(DataDirectoryConversionError, match="symbolic link"):
        plan_data_directory_conversion(data_dir)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_preflight_rejects_special_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    source = data_dir / "attachments"
    source.mkdir(parents=True)
    fifo = source / "pipe"
    mkfifo = getattr(os, "mkfifo", None)
    assert mkfifo is not None
    mkfifo(fifo)

    with pytest.raises(DataDirectoryConversionError, match="special file"):
        plan_data_directory_conversion(data_dir)


def test_interrupted_apply_is_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    _write_complete_legacy_tree(data_dir)
    original_replace = Path.replace
    move_calls = 0

    def interrupt_second_move(source: Path, destination: Path) -> Path:
        nonlocal move_calls
        if source.is_relative_to(data_dir):
            move_calls += 1
            if move_calls == 2:
                raise OSError("injected interruption")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", interrupt_second_move)
    with pytest.raises(OSError, match="injected interruption"):
        apply_data_directory_conversion(data_dir)
    monkeypatch.setattr(Path, "replace", original_replace)

    remaining = plan_data_directory_conversion(data_dir)
    result = apply_data_directory_conversion(data_dir)

    assert len(remaining.moves) == len(LEGACY_DIRECTORY_MAPPINGS) - 1
    assert result.moved_files == len(remaining.moves)
    legacy_roots_remaining = [
        data_dir / mapping.source_relative
        for mapping in LEGACY_DIRECTORY_MAPPINGS
        if (data_dir / mapping.source_relative).exists()
    ]
    assert legacy_roots_remaining == []


def test_preflight_rejects_missing_root_and_user_home(tmp_path: Path) -> None:
    with pytest.raises(DataDirectoryConversionError, match="does not exist"):
        plan_data_directory_conversion(tmp_path / "missing")
    with pytest.raises(DataDirectoryConversionError, match="user home"):
        plan_data_directory_conversion(Path.home())
