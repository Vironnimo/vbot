"""Locally installed native Extensions use the ordinary Desktop lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from desktop import extensions
from desktop.extensions import load_desktop_extensions
from tests.directory_links import link_directory


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
    tmp_path: Path, entry: str, monkeypatch: pytest.MonkeyPatch
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
    imported = []
    load = extensions._load_declarations

    def observed_load(*args: Any, **kwargs: Any) -> Any:
        imported.append(args[0])
        return load(*args, **kwargs)

    def decline(affected: frozenset[str]) -> None:
        assert affected == {"extension:printer:printer"}
        raise RuntimeError("test-owned-decline")

    monkeypatch.setattr(extensions, "_load_declarations", observed_load)
    # A second, earlier package would have been imported before discovering the
    # affected old owner without the complete metadata-only preflight.
    _extension(tmp_path, "added", "raise AssertionError('declined import')")
    with pytest.raises(RuntimeError, match="test-owned-decline"):
        load_desktop_extensions([tmp_path], previous=unchanged, before_load=decline)
    assert imported == []
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
) -> None:
    package = _extension(
        tmp_path,
        "alpha",
        "def register(api):\n"
        "    api.register_capability('printer', 'Printer', lambda emit: None)\n",
    )
    link_directory(tmp_path / "beta", package)
    initial = load_desktop_extensions([tmp_path])
    reloaded = load_desktop_extensions([tmp_path], previous=initial)
    assert set(reloaded.capabilities) == {"extension:alpha:printer", "extension:beta:printer"}
    assert all(record["status"] == "loaded" for record in reloaded.records)
    for capability_id, capability in reloaded.capabilities.items():
        assert capability is initial.capabilities[capability_id]


@pytest.mark.parametrize("asset", ["settings.json", "data.bin", "native.pyd", "ui/index.html"])
def test_native_reload_detects_added_changed_and_removed_package_assets(
    tmp_path: Path, asset: str
) -> None:
    package = _extension(
        tmp_path,
        "printer",
        "def register(api):\n"
        "    api.register_capability('printer', 'Printer', lambda emit: None)\n",
    )
    current = load_desktop_extensions([tmp_path])
    asset_path = package / asset
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    for content in (b"original", b"modified", None):
        if content is None:
            asset_path.unlink()
        else:
            asset_path.write_bytes(content)
        updated = load_desktop_extensions([tmp_path], previous=current)
        before = current.capabilities["extension:printer:printer"]
        after = updated.capabilities["extension:printer:printer"]
        assert updated.records[0]["change"] == "updated"
        assert after is not before
        assert after.source == before.source
        assert after.revision != before.revision
        current = updated


def test_native_reload_ignores_dependency_and_generated_cache_files(tmp_path: Path) -> None:
    package = _extension(
        tmp_path,
        "printer",
        "def register(api):\n"
        "    api.register_capability('printer', 'Printer', lambda emit: None)\n",
    )
    initial = load_desktop_extensions([tmp_path])
    for ignored in ("__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".cache"):
        directory = package / ignored
        directory.mkdir(exist_ok=True)
        (directory / "volatile.json").write_bytes(b"updated")
    (package / "desktop.pyc").write_bytes(b"updated bytecode")
    unchanged = load_desktop_extensions([tmp_path], previous=initial)
    assert unchanged.records[0]["change"] == "unchanged"
    assert (
        unchanged.capabilities["extension:printer:printer"]
        is initial.capabilities["extension:printer:printer"]
    )


@pytest.mark.parametrize(
    "failure",
    ["entries", "bytes", "depth", "scan", "read", "link", "directory_swap", "registration_change"],
)
def test_native_reload_refuses_incomplete_or_unbounded_package_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "extensions"
    package = _extension(
        root,
        "printer",
        "def register(api):\n"
        "    api.register_capability('printer', 'Printer', lambda emit: None)\n",
    )
    assets = package / "assets"
    assets.mkdir()
    asset = assets / "data.bin"
    asset.write_bytes(b"input")
    initial = load_desktop_extensions([root])
    assert initial.capabilities
    if failure == "entries":
        monkeypatch.setattr(extensions, "_PACKAGE_MAX_ENTRIES", 1)
    elif failure == "bytes":
        monkeypatch.setattr(extensions, "_PACKAGE_MAX_BYTES", 1)
    elif failure == "depth":
        monkeypatch.setattr(extensions, "_PACKAGE_MAX_DEPTH", 0)
    elif failure in {"scan", "directory_swap"}:
        real_scandir = os.scandir
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data.bin").write_bytes(b"not package content")

        def scan(path: Any) -> Any:
            if Path(path) == assets:
                if failure == "scan":
                    raise PermissionError("asset directory unavailable")
                assets.rename(tmp_path / "former-assets")
                link_directory(assets, outside)
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scan)
    elif failure == "read":
        real_open = Path.open

        def read(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == asset:
                raise PermissionError("asset unavailable")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", read)
    elif failure == "registration_change":
        asset.write_bytes(b"changed input")
        real_load = extensions._load_declarations

        def register(*args: Any, **kwargs: Any) -> Any:
            declarations = real_load(*args, **kwargs)
            asset.write_bytes(b"changed during registration")
            return declarations

        monkeypatch.setattr(extensions, "_load_declarations", register)
    else:
        link_directory(assets / "cycle", package)
    refused = load_desktop_extensions([root], previous=initial)
    assert refused.records[0]["status"] == "failed"
    assert refused.records[0]["error"]
    assert not refused.capabilities


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
