"""Locally installed native Extensions use the ordinary Desktop lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop.extensions import load_desktop_extensions


def _extension(root: Path, name: str, source: str, *, entry: str = "desktop.py") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "extension.json").write_text(
        json.dumps({"desktop": {"entry": entry, "api_version": 1}}), encoding="utf-8"
    )
    (package / "desktop.py").write_text(source, encoding="utf-8")
    return package


@pytest.mark.parametrize("entry", ["desktop.py", "native/desktop.py"])
def test_native_discovery_never_imports_server_code_or_constructs_handlers(
    tmp_path: Path, entry: str
) -> None:
    package = _extension(
        tmp_path,
        "printer",
        "from .labels import LABEL\n"
        "def factory(emit):\n    raise AssertionError('not shared')\n"
        "def register(api):\n"
        "    api.register_capability('printer', LABEL, factory, description='Print documents')\n",
        entry=entry,
    )
    native_directory = (package / entry).parent
    native_directory.mkdir(exist_ok=True)
    if native_directory != package:
        (package / "desktop.py").rename(package / entry)
        (native_directory / "__init__.py").write_text(
            "raise AssertionError('initializer imported')", encoding="utf-8"
        )
    (package / "__init__.py").write_text(
        "raise AssertionError('server imported')", encoding="utf-8"
    )
    (native_directory / "labels.py").write_text("LABEL = 'Office printer'", encoding="utf-8")
    catalog = load_desktop_extensions([tmp_path])
    assert catalog.records == [{"name": "printer", "status": "loaded", "capability_errors": []}]
    assert catalog.capabilities["printer"].describe() == {
        "id": "printer",
        "owner": "printer",
        "label": "Office printer",
        "description": "Print documents",
    }
    assert set(catalog.factories) == {"printer"}


@pytest.mark.parametrize("state", ["disabled", "failed", "server_only"])
def test_local_identity_prevents_later_native_code_from_loading(tmp_path: Path, state: str) -> None:
    local, bundled = tmp_path / "local", tmp_path / "bundled"
    package = _extension(local, "same", "raise RuntimeError('local broken')")
    if state == "server_only":
        (package / "extension.json").write_text("{}", encoding="utf-8")
    _extension(bundled, "same", "raise AssertionError('must not import the later copy')")
    catalog = load_desktop_extensions(
        [local, bundled], disabled=["same"] if state == "disabled" else []
    )
    assert [record["status"] for record in catalog.records] == [state, "overridden"]
    assert not catalog.capabilities


@pytest.mark.parametrize("disabled", ["printer", {"printer": True}, [False], 1])
def test_invalid_disabled_settings_prevent_native_loading(tmp_path: Path, disabled: object) -> None:
    _extension(tmp_path, "printer", "raise AssertionError('must not import native code')")
    catalog = load_desktop_extensions([tmp_path], disabled=disabled)  # type: ignore[arg-type]
    assert catalog.records == [
        {"name": "extensions.disabled", "status": "failed", "error": "invalid_disabled_list"}
    ]
    assert not catalog.capabilities


def test_native_capability_collisions_preserve_the_first_owner_and_diagnose_the_second(
    tmp_path: Path,
) -> None:
    declaration = (
        "def register(api):\n    api.register_capability('printer', 'Printer', lambda emit: None)\n"
    )
    _extension(tmp_path, "first", declaration)
    _extension(tmp_path, "second", declaration)
    catalog = load_desktop_extensions([tmp_path])
    assert catalog.capabilities["printer"].owner == "first"
    assert catalog.records[1]["capability_errors"] == ["printer"]


@pytest.mark.parametrize("entry", ["../outside.py", "/outside.py", "C:/outside.py", "native.js"])
def test_native_manifest_entry_cannot_escape_its_package(tmp_path: Path, entry: str) -> None:
    _extension(tmp_path, "invalid", "raise AssertionError('entry was imported')", entry=entry)
    catalog = load_desktop_extensions([tmp_path])
    assert catalog.records[0]["status"] == "failed"
    assert not catalog.capabilities
