"""Locally installed native Extensions use the ordinary Desktop lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from desktop import extensions
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
    assert catalog.records[0]["status"] == "loaded"
    assert catalog.records[0]["capabilities"] == ["extension:printer:printer"]
    assert catalog.capabilities["extension:printer:printer"].describe() == {
        "id": "extension:printer:printer",
        "owner": "printer",
        "label": "Office printer",
        "description": "Print documents",
    }
    assert set(catalog.factories) == {"extension:printer:printer"}
    unchanged = load_desktop_extensions([tmp_path], previous=catalog)
    assert (
        unchanged.capabilities["extension:printer:printer"]
        is catalog.capabilities["extension:printer:printer"]
    )
    labels = native_directory / "labels.py"
    stamp = labels.stat()
    labels.write_text("LABEL = 'Edited printer'", encoding="utf-8")
    os.utime(labels, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    changed = load_desktop_extensions([tmp_path], previous=unchanged)
    assert changed.capabilities["extension:printer:printer"].label == "Edited printer"
    assert (
        changed.capabilities["extension:printer:printer"].source
        == catalog.capabilities["extension:printer:printer"].source
    )
    assert (
        changed.capabilities["extension:printer:printer"].revision
        != catalog.capabilities["extension:printer:printer"].revision
    )


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


def test_native_capabilities_are_owner_qualified_without_a_small_count_limit(
    tmp_path: Path,
) -> None:
    declaration = (
        "def register(api):\n"
        "    for number in range(40):\n"
        "        api.register_capability('printer_' + str(number), 'Printer', lambda emit: None)\n"
    )
    _extension(tmp_path, "first", declaration)
    _extension(tmp_path, "second", declaration)
    catalog = load_desktop_extensions([tmp_path])
    assert len(catalog.capabilities) == 80
    assert catalog.capabilities["extension:first:printer_0"].owner == "first"
    assert catalog.capabilities["extension:second:printer_0"].owner == "second"
    assert all(record["status"] == "loaded" for record in catalog.records)


def test_reload_cache_keeps_distinct_owners_when_packages_resolve_to_one_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for owner in ("alpha", "beta"):
        _extension(
            tmp_path,
            owner,
            "def register(api):\n"
            "    api.register_capability('printer', 'Printer', lambda emit: None)\n",
        )
    identify = extensions._source_identity

    def aliased_source(root: Path) -> tuple[str, str, bool]:
        _source, revision, legacy = identify(root)
        return "same-resolved-package", revision, legacy

    # Canonical source identity is equal for directory aliases/junctions, while
    # their registration owners are the distinct visible folder names.
    monkeypatch.setattr(extensions, "_source_identity", aliased_source)
    initial = load_desktop_extensions([tmp_path])
    reloaded = load_desktop_extensions([tmp_path], previous=initial)
    assert set(reloaded.capabilities) == {"extension:alpha:printer", "extension:beta:printer"}
    assert all(record["status"] == "loaded" for record in reloaded.records)
    for capability_id, capability in reloaded.capabilities.items():
        assert capability is initial.capabilities[capability_id]


def test_only_shipped_native_packages_keep_legacy_capability_ids(tmp_path: Path) -> None:
    _extension(
        tmp_path,
        "terminal",
        "def register(api):\n"
        "    api.register_capability('terminal', 'Other terminal', lambda emit: None)\n",
    )
    bundled = Path(__file__).resolve().parents[2] / "resources" / "extensions"
    shipped = load_desktop_extensions([bundled])
    assert {"terminal", "computer_use"} <= shipped.capabilities.keys()
    assert shipped.capabilities["terminal"].source == "builtin:terminal"
    local = load_desktop_extensions([tmp_path, bundled])
    assert "terminal" not in local.capabilities
    assert "extension:terminal:terminal" in local.capabilities


@pytest.mark.parametrize("entry", ["../outside.py", "/outside.py", "C:/outside.py", "native.js"])
def test_native_manifest_entry_cannot_escape_its_package(tmp_path: Path, entry: str) -> None:
    _extension(tmp_path, "invalid", "raise AssertionError('entry was imported')", entry=entry)
    catalog = load_desktop_extensions([tmp_path])
    assert catalog.records[0]["status"] == "failed"
    assert not catalog.capabilities
