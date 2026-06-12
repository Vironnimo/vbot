"""Tests for two-phase registration: records, manifest, disable, lifecycle.

Covers the packaging layer added on top of dispatch/loader discovery: per
-extension :class:`ExtensionRecord` status, optional ``extension.json`` manifest
(happy / invalid / ``api_version`` mismatch), disabled extensions never being
imported, ``api.config`` delivery, deterministic awaiting of async ``register()``
before hook declarations apply, failure diagnostics, and startup/shutdown
lifecycle firing (order + fail-open).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.extensions import API_VERSION, ExtensionRegistry, HookContext


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _marker_lines(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return marker.read_text(encoding="utf-8").split()


def _write_single_file(root: Path, name: str, source: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(source, encoding="utf-8")


def _write_package(root: Path, name: str, source: str, manifest: dict | str | None = None) -> Path:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(source, encoding="utf-8")
    if manifest is not None:
        content = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (package / "extension.json").write_text(content, encoding="utf-8")
    return package


def _import_marker_source(name: str, marker: Path) -> str:
    """Module that records its name at *import* time (module exec side effect)."""
    return (
        "import pathlib\n"
        f"with pathlib.Path({str(marker)!r}).open('a', encoding='utf-8') as fh:\n"
        f"    fh.write({name!r} + '\\n')\n"
        "\n"
        "def register(api):\n"
        "    pass\n"
    )


def _config_marker_source(marker: Path) -> str:
    """Module that writes ``api.config`` to a marker during ``register``."""
    return (
        "import json, pathlib\n"
        "def register(api):\n"
        f"    pathlib.Path({str(marker)!r}).write_text(json.dumps(api.config), encoding='utf-8')\n"
    )


def _lifecycle_source(name: str, marker: Path, *, startup_boom: bool = False) -> str:
    boom = "        raise RuntimeError('startup boom')\n" if startup_boom else ""
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "\n"
        "def register(api):\n"
        "    def _startup():\n"
        f"        _write({name!r} + ':startup')\n"
        f"{boom}"
        "    def _shutdown():\n"
        f"        _write({name!r} + ':shutdown')\n"
        "    api.on_startup(_startup)\n"
        "    api.on_shutdown(_shutdown)\n"
    )


def _record(registry: ExtensionRegistry, name: str):
    return next(record for record in registry.records() if record.name == name)


def test_loaded_extension_produces_record(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "plain", "def register(api):\n    pass\n")

    registry = ExtensionRegistry.load(root)

    record = _record(registry, "plain")
    assert record.status == "loaded"
    assert record.error is None
    assert record.manifest is None
    assert registry.diagnostics() == []


def test_disabled_extension_is_never_imported(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "imported.txt"
    _write_single_file(root, "skipme", _import_marker_source("skipme", marker))

    registry = ExtensionRegistry.load(root, disabled={"skipme"})

    # module body never executed: no import-time marker, status disabled
    assert _marker_lines(marker) == []
    assert _record(registry, "skipme").status == "disabled"


def test_config_reaches_register(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "config.json"
    _write_single_file(root, "configured", _config_marker_source(marker))

    ExtensionRegistry.load(root, config={"configured": {"token": "abc", "level": 3}})

    assert json.loads(marker.read_text(encoding="utf-8")) == {"token": "abc", "level": 3}


def test_config_defaults_to_empty_dict(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "config.json"
    _write_single_file(root, "configless", _config_marker_source(marker))

    ExtensionRegistry.load(root)

    assert json.loads(marker.read_text(encoding="utf-8")) == {}


def test_manifest_enriches_record(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_package(
        root,
        "manifested",
        "def register(api):\n    pass\n",
        manifest={"version": "1.2.0", "description": "demo", "name": "Display Name"},
    )

    registry = ExtensionRegistry.load(root)

    record = _record(registry, "manifested")
    assert record.status == "loaded"
    assert record.manifest is not None
    assert record.manifest.version == "1.2.0"
    assert record.manifest.description == "demo"
    assert record.manifest.display_name == "Display Name"


def test_manifest_invalid_json_fails_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_package(
        root,
        "broken_manifest",
        "def register(api):\n    pass\n",
        manifest="{ not valid json",
    )

    registry = ExtensionRegistry.load(root)

    record = _record(registry, "broken_manifest")
    assert record.status == "failed"
    assert record.error is not None
    assert "invalid JSON" in record.error
    assert record in registry.diagnostics()


def test_manifest_wrong_field_type_fails_extension(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_package(
        root,
        "typed",
        "def register(api):\n    pass\n",
        manifest={"version": 123},
    )

    registry = ExtensionRegistry.load(root)

    record = _record(registry, "typed")
    assert record.status == "failed"
    assert "version must be a string" in (record.error or "")


def test_manifest_api_version_newer_than_supported_fails(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "imported.txt"
    package = _write_package(
        root,
        "future",
        _import_marker_source("future", marker),
        manifest={"api_version": API_VERSION + 1},
    )
    assert package.exists()

    registry = ExtensionRegistry.load(root)

    record = _record(registry, "future")
    assert record.status == "failed"
    assert "api_version" in (record.error or "")
    # api_version mismatch is decided before import: module body never ran
    assert _marker_lines(marker) == []


def test_failed_extension_does_not_block_others(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    root.mkdir(parents=True, exist_ok=True)
    (root / "boom.py").write_text("raise RuntimeError('import boom')\n", encoding="utf-8")
    _write_single_file(root, "healthy", "def register(api):\n    pass\n")

    registry = ExtensionRegistry.load(root)

    assert _record(registry, "boom").status == "failed"
    assert _record(registry, "healthy").status == "loaded"
    assert [record.name for record in registry.diagnostics()] == ["boom"]


def test_register_failure_records_diagnostic(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "bad_register", "def register(api):\n    raise ValueError('nope')\n")

    registry = ExtensionRegistry.load(root)

    record = _record(registry, "bad_register")
    assert record.status == "failed"
    assert "register() raised" in (record.error or "")


def test_async_register_awaited_before_apply_no_loop(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_single_file(
        root,
        "async_ext",
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "async def register(api):\n"
        "    def handler(ctx, **payload):\n"
        "        _MARKER.write_text('fired', encoding='utf-8')\n"
        "    api.on('run_start', handler)\n",
    )

    registry = ExtensionRegistry.load(root)

    # apply ran after the async register completed: handler is already installed
    ctx = HookContext(session_id="s", agent_id="a", run_id="r")
    asyncio.run(registry.dispatch_run_start(ctx, session_id="s", agent_id="a"))
    assert marker.read_text(encoding="utf-8") == "fired"


@pytest.mark.asyncio
async def test_async_register_awaited_before_apply_within_running_loop(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    _write_single_file(
        root,
        "async_ext",
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "async def register(api):\n"
        "    def handler(ctx, **payload):\n"
        "        _MARKER.write_text('fired', encoding='utf-8')\n"
        "    api.on('run_start', handler)\n",
    )

    # load() is sync but called from within a running loop (server lifespan shape):
    # the async register must still complete and apply before load() returns.
    registry = ExtensionRegistry.load(root)

    ctx = HookContext(session_id="s", agent_id="a", run_id="r")
    await registry.dispatch_run_start(ctx, session_id="s", agent_id="a")
    assert marker.read_text(encoding="utf-8") == "fired"


def test_startup_and_shutdown_fire_in_load_order(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "lifecycle.txt"
    _write_single_file(root, "alpha", _lifecycle_source("alpha", marker))
    _write_single_file(root, "zeta", _lifecycle_source("zeta", marker))

    registry = ExtensionRegistry.load(root)
    asyncio.run(registry.fire_startup())
    asyncio.run(registry.fire_shutdown())

    assert _marker_lines(marker) == [
        "alpha:startup",
        "zeta:startup",
        "alpha:shutdown",
        "zeta:shutdown",
    ]


def test_startup_handler_failure_is_isolated(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "lifecycle.txt"
    _write_single_file(root, "alpha", _lifecycle_source("alpha", marker, startup_boom=True))
    _write_single_file(root, "zeta", _lifecycle_source("zeta", marker))

    registry = ExtensionRegistry.load(root)
    asyncio.run(registry.fire_startup())

    # alpha's startup raised after writing nothing useful; zeta still fired
    assert "zeta:startup" in _marker_lines(marker)


def test_fire_shutdown_blocking_runs_handlers(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "lifecycle.txt"
    _write_single_file(root, "alpha", _lifecycle_source("alpha", marker))

    registry = ExtensionRegistry.load(root)
    registry.fire_shutdown_blocking()

    assert _marker_lines(marker) == ["alpha:shutdown"]


def test_disabled_extension_lifecycle_does_not_fire(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "lifecycle.txt"
    _write_single_file(root, "alpha", _lifecycle_source("alpha", marker))

    registry = ExtensionRegistry.load(root, disabled={"alpha"})
    asyncio.run(registry.fire_startup())
    asyncio.run(registry.fire_shutdown())

    assert _marker_lines(marker) == []
