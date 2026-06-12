"""Runtime-level tests for extension loading, config, and lifecycle wiring.

These exercise the bootstrap path end to end: disabled extensions are never
imported, per-extension config from ``settings.json`` reaches ``register()``, and
startup/shutdown handlers fire at runtime start/stop.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.runtime.runtime import Runtime
from core.utils.config import Config


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _write_extension(data_dir: Path, name: str, source: str) -> None:
    extensions_dir = data_dir / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    (extensions_dir / f"{name}.py").write_text(source, encoding="utf-8")


def _write_settings(data_dir: Path, settings: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _marker_lines(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return marker.read_text(encoding="utf-8").split()


def test_disabled_extension_is_never_imported(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    import_marker = tmp_path / "imported.txt"
    _write_extension(
        data_dir,
        "disabled_ext",
        "import pathlib\n"
        f"pathlib.Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        "def register(api):\n    pass\n",
    )
    _write_settings(data_dir, {"extensions": {"disabled": ["disabled_ext"]}})

    runtime = Runtime(config)
    runtime.start()
    record = next(r for r in runtime.extensions.records() if r.name == "disabled_ext")
    runtime.stop()

    assert not import_marker.exists()
    assert record.status == "disabled"


def test_config_reaches_register(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    config_marker = tmp_path / "config.json"
    _write_extension(
        data_dir,
        "configured",
        "import json, pathlib\n"
        "def register(api):\n"
        f"    pathlib.Path({str(config_marker)!r}).write_text("
        "json.dumps(api.config), encoding='utf-8')\n",
    )
    _write_settings(
        data_dir,
        {"extensions": {"config": {"configured": {"token": "abc", "level": 2}}}},
    )

    runtime = Runtime(config)
    runtime.start()
    runtime.stop()

    assert json.loads(config_marker.read_text(encoding="utf-8")) == {"token": "abc", "level": 2}


def test_startup_and_shutdown_hooks_fire_at_runtime_lifecycle(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    lifecycle_marker = tmp_path / "lifecycle.txt"
    _write_extension(
        data_dir,
        "lifecycle_ext",
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(lifecycle_marker)!r})\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "def register(api):\n"
        "    api.on_startup(lambda: _write('startup'))\n"
        "    api.on_shutdown(lambda: _write('shutdown'))\n",
    )

    runtime = Runtime(config)
    runtime.start()

    # startup has not fired yet — it is gated on the serving lifespan
    assert _marker_lines(lifecycle_marker) == []

    asyncio.run(runtime.fire_extension_startup())
    assert _marker_lines(lifecycle_marker) == ["startup"]

    runtime.stop()
    assert _marker_lines(lifecycle_marker) == ["startup", "shutdown"]
