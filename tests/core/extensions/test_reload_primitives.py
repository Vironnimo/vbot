"""Tests for the reload primitives the runtime rebuild leans on.

Covers the two module-level / registry seams added for restart-equivalent
extension reload:

- ``purge_extension_modules`` drops exactly the synthetic ``vbot_ext`` namespace
  (parent, entry points, and submodules) from ``sys.modules`` and nothing else.
- ``ExtensionRegistry.remove_applied_tools`` detaches only the tools a loaded
  extension actually registered (handler-identity matched, so a collision-skipped
  name stays with its real owner) and never mutates record statuses.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.extensions import purge_extension_modules
from core.extensions.extensions import ExtensionRegistry
from core.tools import ToolRegistry


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _write_single_file(root: Path, name: str, source: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(source, encoding="utf-8")


def _tool_extension_source(tool_name: str) -> str:
    return (
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        "    return tool_success({'value': arguments.get('value')})\n"
        "def register(api):\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _handler)\n"
    )


def test_purge_removes_only_vbot_ext_namespace() -> None:
    # Seed the parent, an entry point, and a submodule, plus two decoys that must
    # survive: an unrelated top-level module and one whose name merely *prefixes*
    # ``vbot_ext`` without the dot boundary.
    sys.modules["vbot_ext"] = types.ModuleType("vbot_ext")
    sys.modules["vbot_ext.pkg"] = types.ModuleType("vbot_ext.pkg")
    sys.modules["vbot_ext.pkg.sub"] = types.ModuleType("vbot_ext.pkg.sub")
    survivor = types.ModuleType("vbot_extra")
    lookalike = types.ModuleType("vbot_extras_helper")
    sys.modules["vbot_extra"] = survivor
    sys.modules["vbot_extras_helper"] = lookalike

    try:
        purge_extension_modules()

        assert "vbot_ext" not in sys.modules
        assert "vbot_ext.pkg" not in sys.modules
        assert "vbot_ext.pkg.sub" not in sys.modules
        # A name that only *looks* like the namespace is untouched.
        assert sys.modules.get("vbot_extra") is survivor
        assert sys.modules.get("vbot_extras_helper") is lookalike
    finally:
        sys.modules.pop("vbot_extra", None)
        sys.modules.pop("vbot_extras_helper", None)


def test_remove_applied_tools_detaches_only_owned_tools(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "tooly", _tool_extension_source("ext_echo"))
    # This extension declares a tool named "read" a built-in already owns, so its
    # copy is skipped on apply and must never be yanked from the built-in.
    _write_single_file(root, "shadow", _tool_extension_source("read"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()

    def _builtin_read(context, arguments):
        return {"ok": True, "error": None, "data": {}, "artifacts": []}

    tool_registry.register("read", "builtin read", {"type": "object"}, _builtin_read)
    registry.apply_tools(tool_registry)

    assert "ext_echo" in [tool.name for tool in tool_registry.list_tools()]

    registry.remove_applied_tools(tool_registry)

    # The extension's own tool is gone; the built-in "read" survives with its owner.
    assert "ext_echo" not in [tool.name for tool in tool_registry.list_tools()]
    assert tool_registry.get("read").handler is _builtin_read


def test_remove_applied_tools_leaves_record_statuses_untouched(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "tooly", _tool_extension_source("ext_echo"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)

    registry.remove_applied_tools(tool_registry)

    # The registry object is discarded after the swap, so the record stays "loaded"
    # (unlike live deactivate, which flips the record and clears declarations).
    record = next(item for item in registry.records() if item.name == "tooly")
    assert record.status == "loaded"
    assert [declaration.name for declaration in record.declarations.tools] == ["ext_echo"]
