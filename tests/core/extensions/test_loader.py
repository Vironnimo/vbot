"""Tests for ``ExtensionRegistry.load`` discovery and fail-open loading.

Covers the three accepted entry-point shapes (single-file module, package
``__init__.py``, directory ``extension.py`` fallback), sorted load order across
roots, fail-open behavior on import/``register`` failures, and async
``register`` via the no-running-loop path. Loaded extensions write their name to
a marker file from a ``run_start`` handler so behavior is observed through real
dispatch rather than registry internals.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.extensions import ExtensionRegistry, HookContext


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _extension_source(name: str, marker: Path, *, is_async: bool = False) -> str:
    register_def = "async def register(api):" if is_async else "def register(api):"
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "\n"
        f"{register_def}\n"
        "    def handler(ctx, **payload):\n"
        "        with _MARKER.open('a', encoding='utf-8') as fh:\n"
        f"            fh.write({name!r} + '\\n')\n"
        "    api.on('run_start', handler)\n"
    )


def _write_single_file(root: Path, name: str, marker: Path, *, is_async: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(
        _extension_source(name, marker, is_async=is_async), encoding="utf-8"
    )


def _write_package(root: Path, name: str, marker: Path) -> None:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(_extension_source(name, marker), encoding="utf-8")


def _write_directory_fallback(root: Path, name: str, marker: Path) -> None:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "extension.py").write_text(_extension_source(name, marker), encoding="utf-8")


def _fire_run_start(registry: ExtensionRegistry) -> None:
    ctx = HookContext(session_id="s", agent_id="a", run_id="r")
    asyncio.run(registry.dispatch_run_start(ctx, session_id="s", agent_id="a"))


def _marker_names(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return marker.read_text(encoding="utf-8").split()


def test_loads_single_file_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_single_file(root, "single_mod", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["single_mod"]


def test_loads_package_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_package(root, "package_ext", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["package_ext"]


def test_non_utf8_manifest_fails_only_its_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_package(root, "broken_manifest", marker)
    (root / "broken_manifest" / "extension.json").write_bytes(b"\xff")
    _write_single_file(root, "healthy", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["healthy"]
    broken = _record_by_name(registry, "broken_manifest")
    assert broken.status == "failed"
    assert broken.error is not None
    assert "not valid UTF-8" in broken.error


def test_unreadable_extension_root_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    original_iterdir = Path.iterdir

    def fail_target_iterdir(path: Path):
        if path == root:
            raise PermissionError("denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_target_iterdir)

    registry = ExtensionRegistry.load(root)

    assert registry.records() == []
    assert str(root) in caplog.text
    assert "Skipping unreadable Extension directory" in caplog.text


def test_loads_directory_fallback_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_directory_fallback(root, "fallback_ext", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["fallback_ext"]


def test_load_order_is_sorted_by_name(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_single_file(root, "zeta", marker)
    _write_single_file(root, "alpha", marker)
    _write_single_file(root, "mike", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["alpha", "mike", "zeta"]


def test_extra_dirs_loaded_after_primary(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    marker = tmp_path / "marker.txt"
    _write_single_file(primary, "primary_ext", marker)
    _write_single_file(extra, "extra_ext", marker)

    registry = ExtensionRegistry.load(primary, [extra])
    _fire_run_start(registry)

    assert _marker_names(marker) == ["primary_ext", "extra_ext"]


def test_broken_extension_does_not_block_others(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    root.mkdir(parents=True, exist_ok=True)
    (root / "broken.py").write_text("raise RuntimeError('import boom')\n", encoding="utf-8")
    _write_single_file(root, "healthy", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["healthy"]


def test_register_failure_does_not_block_others(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    root.mkdir(parents=True, exist_ok=True)
    (root / "bad_register.py").write_text(
        "def register(api):\n    raise ValueError('register boom')\n", encoding="utf-8"
    )
    _write_single_file(root, "healthy", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["healthy"]


def test_missing_directory_yields_empty_registry(tmp_path: Path) -> None:
    registry = ExtensionRegistry.load(tmp_path / "does-not-exist")
    # No handlers registered; dispatch is a safe no-op.
    _fire_run_start(registry)


def test_module_without_register_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    root.mkdir(parents=True, exist_ok=True)
    (root / "no_register.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_single_file(root, "healthy", marker)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["healthy"]


def test_async_register_runs_without_running_loop(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_single_file(root, "async_ext", marker, is_async=True)

    registry = ExtensionRegistry.load(root)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["async_ext"]


def _write_raising_single_file(root: Path, name: str) -> Path:
    """Write a single-file extension whose import raises; returns its entry path."""
    root.mkdir(parents=True, exist_ok=True)
    entry = root / f"{name}.py"
    entry.write_text("raise RuntimeError('must never be imported')\n", encoding="utf-8")
    return entry


def _record_by_name(registry: ExtensionRegistry, name: str):
    return next(record for record in registry.records() if record.name == name)


def test_bundled_dir_is_scanned_and_loads(tmp_path: Path) -> None:
    data_dir = tmp_path / "extensions"
    bundled = tmp_path / "bundled"
    marker = tmp_path / "marker.txt"
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_single_file(bundled, "bundled_ext", marker)

    registry = ExtensionRegistry.load(data_dir, bundled_dir=bundled)
    _fire_run_start(registry)

    assert _marker_names(marker) == ["bundled_ext"]
    assert _record_by_name(registry, "bundled_ext").status == "loaded"


def test_data_dir_copy_shadows_bundled_same_name(tmp_path: Path) -> None:
    data_dir = tmp_path / "extensions"
    bundled = tmp_path / "bundled"
    marker = tmp_path / "marker.txt"
    _write_single_file(data_dir, "shared", marker)
    _write_raising_single_file(bundled, "shared")

    registry = ExtensionRegistry.load(data_dir, bundled_dir=bundled)
    _fire_run_start(registry)

    # Only the data-dir copy loaded and ran its handler; the bundled copy would
    # have raised on import, proving it was never imported.
    assert _marker_names(marker) == ["shared"]
    data_record = _record_by_name(registry, "shared")
    assert data_record.status == "loaded"
    assert data_record.entry_path == data_dir / "shared.py"

    overridden = [record for record in registry.records() if record.status == "overridden"]
    assert len(overridden) == 1
    assert overridden[0].name == "shared"
    assert overridden[0].entry_path == bundled / "shared.py"
    assert overridden[0].overridden_by == str(data_dir / "shared.py")


def test_extra_root_copy_shadows_bundled_same_name(tmp_path: Path) -> None:
    data_dir = tmp_path / "extensions"
    extra = tmp_path / "extra"
    bundled = tmp_path / "bundled"
    marker = tmp_path / "marker.txt"
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_single_file(extra, "shared", marker)
    _write_raising_single_file(bundled, "shared")

    registry = ExtensionRegistry.load(data_dir, [extra], bundled_dir=bundled)
    _fire_run_start(registry)

    # Root order is data dir → extras → bundled, so the extra-root copy wins.
    assert _marker_names(marker) == ["shared"]
    extra_record = _record_by_name(registry, "shared")
    assert extra_record.status == "loaded"
    assert extra_record.entry_path == extra / "shared.py"

    overridden = [record for record in registry.records() if record.status == "overridden"]
    assert len(overridden) == 1
    assert overridden[0].entry_path == bundled / "shared.py"
    assert overridden[0].overridden_by == str(extra / "shared.py")


def test_disabled_name_still_claims_identity_over_bundled_copy(tmp_path: Path) -> None:
    data_dir = tmp_path / "extensions"
    bundled = tmp_path / "bundled"
    marker = tmp_path / "marker.txt"
    _write_single_file(data_dir, "shared", marker)
    _write_raising_single_file(bundled, "shared")

    registry = ExtensionRegistry.load(data_dir, disabled={"shared"}, bundled_dir=bundled)
    _fire_run_start(registry)

    # Disabling a name must never silently activate a different copy of it: the
    # data-dir copy is disabled (never imported) and still shadows the bundled one.
    assert _marker_names(marker) == []
    data_record = _record_by_name(registry, "shared")
    assert data_record.status == "disabled"
    assert data_record.entry_path == data_dir / "shared.py"

    overridden = [record for record in registry.records() if record.status == "overridden"]
    assert len(overridden) == 1
    assert overridden[0].entry_path == bundled / "shared.py"
    assert overridden[0].overridden_by == str(data_dir / "shared.py")


def test_bundled_dir_none_and_missing_behave_like_today(tmp_path: Path) -> None:
    data_dir = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_single_file(data_dir, "only_ext", marker)

    without_bundled = ExtensionRegistry.load(data_dir)
    _fire_run_start(without_bundled)
    assert _marker_names(marker) == ["only_ext"]
    assert [record.name for record in without_bundled.records()] == ["only_ext"]

    marker.unlink()
    with_missing_bundled = ExtensionRegistry.load(data_dir, bundled_dir=tmp_path / "does-not-exist")
    _fire_run_start(with_missing_bundled)
    assert _marker_names(marker) == ["only_ext"]
    assert [record.name for record in with_missing_bundled.records()] == ["only_ext"]


def test_same_name_across_roots_no_longer_both_load(tmp_path: Path) -> None:
    data_dir = tmp_path / "extensions"
    bundled = tmp_path / "bundled"
    marker = tmp_path / "marker.txt"
    _write_single_file(data_dir, "shared", marker)
    _write_single_file(bundled, "shared", marker)

    registry = ExtensionRegistry.load(data_dir, bundled_dir=bundled)
    _fire_run_start(registry)

    # Exactly one copy loads and fires (no double-firing across roots); the other
    # is an ``overridden`` record.
    assert _marker_names(marker) == ["shared"]
    statuses = sorted(record.status for record in registry.records())
    assert statuses == ["loaded", "overridden"]
