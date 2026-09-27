"""``ExtensionRegistry.load`` discovery, roots, records and fail-open loading.

Covers the accepted entry-point shapes, name order within a root and root order
across roots (data dir, extra roots, bundled copies), what each record reports
(manifest, config, disabled, overridden), and that every kind of load failure
fails only its own Extension. Loaded Extensions report through a ``run_start``
marker, so behavior is observed through real dispatch.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from core.extensions import API_VERSION, ExtensionRegistry, purge_extension_modules
from tests.core.extensions.extension_test_support import (
    fire_run_start,
    marker_lines,
    marker_source,
    record,
    write_extension,
    write_package,
)

_RAISES_ON_IMPORT = "raise RuntimeError('must never be imported')\n"


def _import_marker_source(name: str, marker: Path) -> str:
    """Module that records *name* at import time, before ``register`` runs."""
    return (
        "import pathlib\n"
        f"with pathlib.Path({str(marker)!r}).open('a', encoding='utf-8') as fh:\n"
        f"    fh.write({name!r} + '\\n')\n"
        "def register(api):\n"
        "    pass\n"
    )


def test_entry_point_shapes_load_in_name_order_with_extra_roots_last(tmp_path: Path) -> None:
    root, extra = tmp_path / "extensions", tmp_path / "extra"
    marker = tmp_path / "marker.txt"
    # An async register() completes before apply even without a running loop.
    write_extension(root, "zeta", marker_source(marker, "zeta", asynchronous=True))
    # Directory entry points resolve relative imports inside their package.
    for name, entry in (("alpha", "__init__.py"), ("mike", "extension.py")):
        package = write_package(root, name, "from .helper import register\n", entry=entry)
        (package / "helper.py").write_text(marker_source(marker, name), encoding="utf-8")
    # A module without register() loads but contributes nothing.
    write_extension(root, "no_register", "VALUE = 1\n")
    write_extension(extra, "extra_ext", marker_source(marker, "extra_ext"))

    registry = ExtensionRegistry.load(root, [extra], bundled_dir=tmp_path / "missing-bundled")
    fire_run_start(registry)

    assert marker_lines(marker) == ["alpha", "mike", "zeta", "extra_ext"]
    assert [(item.name, item.status) for item in registry.records()] == [
        ("alpha", "loaded"),
        ("mike", "loaded"),
        ("no_register", "loaded"),
        ("zeta", "loaded"),
        ("extra_ext", "loaded"),
    ]
    assert registry.diagnostics() == []


def test_missing_or_unreadable_roots_yield_no_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    missing = ExtensionRegistry.load(tmp_path / "does-not-exist")
    fire_run_start(missing)
    assert missing.records() == []

    root = tmp_path / "extensions"
    root.mkdir()
    original_iterdir = Path.iterdir

    def fail_target_iterdir(path: Path):
        if path == root:
            raise PermissionError("denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_target_iterdir)

    assert ExtensionRegistry.load(root).records() == []
    assert str(root) in caplog.text


def test_earlier_roots_shadow_bundled_copies_even_when_disabled(tmp_path: Path) -> None:
    data_dir, extra, bundled = tmp_path / "extensions", tmp_path / "extra", tmp_path / "bundled"
    marker = tmp_path / "marker.txt"
    write_extension(data_dir, "from_data", marker_source(marker, "from_data"))
    write_extension(data_dir, "gated", marker_source(marker, "gated"))
    write_extension(extra, "from_extra", marker_source(marker, "from_extra"))
    write_extension(bundled, "bundled_only", marker_source(marker, "bundled_only"))
    # Bundled copies of claimed names would raise if they were ever imported.
    for name in ("from_data", "gated", "from_extra"):
        write_extension(bundled, name, _RAISES_ON_IMPORT)

    registry = ExtensionRegistry.load(data_dir, [extra], disabled={"gated"}, bundled_dir=bundled)
    fire_run_start(registry)

    # Exactly one copy of each name runs; disabling a name never activates another copy.
    assert marker_lines(marker) == ["from_data", "from_extra", "bundled_only"]
    assert record(registry, "gated").status == "disabled"
    assert record(registry, "gated").entry_path == data_dir / "gated.py"
    assert record(registry, "from_extra").entry_path == extra / "from_extra.py"
    assert record(registry, "bundled_only").status == "loaded"
    overridden = {
        item.name: (item.entry_path, item.overridden_by)
        for item in registry.records()
        if item.status == "overridden"
    }
    assert overridden == {
        "from_data": (bundled / "from_data.py", str(data_dir / "from_data.py")),
        "gated": (bundled / "gated.py", str(data_dir / "gated.py")),
        "from_extra": (bundled / "from_extra.py", str(extra / "from_extra.py")),
    }


def test_loaded_records_carry_manifest_config_and_disabled_state(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    imported = tmp_path / "imported.txt"
    config_source = (
        "import json, pathlib\n"
        "def register(api):\n"
        f"    name = __name__.rsplit('.', 1)[-1]\n"
        f"    target = pathlib.Path({str(tmp_path)!r}) / (name + '.json')\n"
        "    target.write_text(json.dumps(api.config), encoding='utf-8')\n"
    )
    write_extension(root, "plain", "def register(api):\n    pass\n")
    write_package(
        root,
        "manifested",
        "def register(api):\n    pass\n",
        manifest={"version": "1.2.0", "description": "demo", "name": "Display Name"},
    )
    write_extension(root, "configured", config_source)
    write_extension(root, "configless", config_source)
    write_extension(root, "skipme", _import_marker_source("skipme", imported))

    registry = ExtensionRegistry.load(
        root, disabled={"skipme"}, config={"configured": {"token": "abc", "level": 3}}
    )

    plain = record(registry, "plain")
    assert (plain.status, plain.error, plain.manifest) == ("loaded", None, None)
    manifest = record(registry, "manifested").manifest
    assert manifest is not None
    assert (manifest.version, manifest.description, manifest.display_name) == (
        "1.2.0",
        "demo",
        "Display Name",
    )
    assert json.loads((tmp_path / "configured.json").read_text(encoding="utf-8")) == {
        "token": "abc",
        "level": 3,
    }
    assert json.loads((tmp_path / "configless.json").read_text(encoding="utf-8")) == {}
    # A disabled Extension is never imported.
    assert record(registry, "skipme").status == "disabled"
    assert marker_lines(imported) == []
    assert registry.diagnostics() == []


def test_each_load_failure_fails_only_its_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker, imported = tmp_path / "marker.txt", tmp_path / "imported.txt"
    noop = "def register(api):\n    pass\n"
    write_package(root, "bad_json", noop, manifest="{ not valid json")
    write_package(root, "bad_utf8", noop)
    (root / "bad_utf8" / "extension.json").write_bytes(b"\xff")
    write_package(root, "bad_version", noop, manifest={"version": 123})
    write_package(
        root,
        "future",
        _import_marker_source("future", imported),
        manifest={"api_version": API_VERSION + 1},
    )
    write_extension(root, "import_boom", "raise RuntimeError('import boom')\n")
    write_extension(root, "register_boom", "def register(api):\n    raise ValueError('nope')\n")
    write_extension(
        root,
        "register_cancelled",
        "import asyncio\ndef register(api):\n    raise asyncio.CancelledError()\n",
    )
    write_extension(
        root,
        "bad_settings",
        "def register(api):\n"
        "    api.register_settings([{'key': 'Bad', 'type': 'text', 'label': 'X'}])\n",
    )
    write_extension(
        root,
        "double_settings",
        "def register(api):\n"
        "    api.register_settings([{'key': 'a', 'type': 'text', 'label': 'A'}])\n"
        "    api.register_settings([{'key': 'b', 'type': 'text', 'label': 'B'}])\n",
    )
    write_extension(root, "healthy", marker_source(marker, "healthy"))

    registry = ExtensionRegistry.load(root)
    fire_run_start(registry)

    expected = {
        "bad_json": "invalid JSON",
        "bad_utf8": "not valid UTF-8",
        "bad_version": "version must be a string",
        "future": "api_version",
        "import_boom": "import failed: import boom",
        "register_boom": "register() raised: nope",
        "register_cancelled": "register() raised",
        "bad_settings": "Bad",
        "double_settings": "already declared",
    }
    errors = {item.name: item.error or "" for item in registry.diagnostics()}
    assert errors.keys() == expected.keys()
    assert {name: fragment in errors[name] for name, fragment in expected.items()} == dict.fromkeys(
        expected, True
    )
    assert record(registry, "healthy").status == "loaded"
    assert marker_lines(marker) == ["healthy"]
    # A newer api_version is refused before import: the module body never ran.
    assert marker_lines(imported) == []


def test_purge_removes_only_the_extension_namespace() -> None:
    for name in ("vbot_ext", "vbot_ext.pkg", "vbot_ext.pkg.sub"):
        sys.modules[name] = types.ModuleType(name)
    # Names that merely prefix the namespace without the dot boundary survive.
    survivors = {name: types.ModuleType(name) for name in ("vbot_extra", "vbot_extras_helper")}
    sys.modules.update(survivors)

    try:
        purge_extension_modules()

        assert not {"vbot_ext", "vbot_ext.pkg", "vbot_ext.pkg.sub"} & set(sys.modules)
        assert {name: sys.modules.get(name) for name in survivors} == survivors
    finally:
        for name in survivors:
            sys.modules.pop(name, None)
