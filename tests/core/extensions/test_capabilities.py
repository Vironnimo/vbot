"""Tests for extension capability surfaces: tools and recall backends.

Covers ``api.register_tool`` / ``api.register_recall_backend`` declaration plus
the registry apply phases (``apply_tools`` / ``apply_recall_backends``):
extension tools are callable through a real ``ToolRegistry`` dispatch and obey
allowlists; name collisions are skipped and diagnosed (built-in wins, and
between two extensions the first-loaded wins with both sides diagnosed); recall
backends become selectable through ``RecallBackendRegistry`` and duplicate /
invalid names are diagnosed. Extensions are loaded through the real filesystem
loader so the whole declare → apply path is exercised.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.extensions import ExtensionRegistry
from core.recall.recall import RecallBackendContext, RecallBackendRegistry
from core.sessions import ChatSessionManager
from core.tools import ToolContext, ToolRegistry


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


def _record(registry: ExtensionRegistry, name: str):
    return next(record for record in registry.records() if record.name == name)


def _tool_extension_source(tool_name: str, marker: str) -> str:
    """Extension that registers one echo tool returning *marker* with the input."""
    return (
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        f"    return tool_success({{'marker': {marker!r}, 'value': arguments.get('value')}})\n"
        "def register(api):\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _handler)\n"
    )


def _recall_extension_source(backend_name: str) -> str:
    """Extension registering a trivial recall backend class as a factory."""
    return (
        "class ExtBackend:\n"
        "    def __init__(self, context):\n"
        "        self.context = context\n"
        "    def browse(self, request):\n"
        "        return {'kind': 'browse'}\n"
        "    def overview(self, request):\n"
        "        return {'kind': 'overview'}\n"
        "    def search(self, request):\n"
        "        return {'kind': 'search'}\n"
        "    def scroll(self, request):\n"
        "        return {'kind': 'scroll'}\n"
        "def register(api):\n"
        f"    api.register_recall_backend({backend_name!r}, ExtBackend)\n"
    )


def _tool_context(tool_name: str, tmp_path: Path) -> ToolContext:
    return ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="c1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=tmp_path,
        app_root=tmp_path,
        data_root=tmp_path,
    )


def _recall_context(tmp_path: Path) -> RecallBackendContext:
    return RecallBackendContext(data_dir=tmp_path, sessions=ChatSessionManager(tmp_path))


# --- tools -------------------------------------------------------------------


def test_extension_tool_dispatches_through_tool_registry(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "echo_ext", _tool_extension_source("ext_echo", "from-ext"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)

    context = _tool_context("ext_echo", tmp_path)
    result = asyncio.run(tool_registry.dispatch(context, {"value": "hi"}))

    assert result["ok"] is True
    assert result["data"] == {"marker": "from-ext", "value": "hi"}
    assert _record(registry, "echo_ext").capability_errors == []


def test_extension_tool_respects_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "echo_ext", _tool_extension_source("ext_echo", "from-ext"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)

    allowed = [tool.name for tool in tool_registry.list_tools(allowed_tools=["ext_echo"])]
    excluded = [tool.name for tool in tool_registry.list_tools(allowed_tools=[])]

    assert "ext_echo" in allowed
    assert "ext_echo" not in excluded


def test_extension_tool_colliding_with_builtin_is_skipped_and_diagnosed(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "shadow_ext", _tool_extension_source("read", "from-ext"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    tool_registry.register("read", "builtin read", {"type": "object"}, lambda context, args: {})
    registry.apply_tools(tool_registry)

    # The built-in is untouched and the extension's tool never took effect.
    assert tool_registry.get("read").description == "builtin read"
    errors = _record(registry, "shadow_ext").capability_errors
    assert any("read" in message and "built-in" in message for message in errors)


def test_two_extensions_same_tool_name_first_wins_both_diagnosed(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    # Load order is sorted by name: alpha applies before zeta.
    _write_single_file(root, "alpha", _tool_extension_source("dup", "from-alpha"))
    _write_single_file(root, "zeta", _tool_extension_source("dup", "from-zeta"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)

    context = _tool_context("dup", tmp_path)
    result = asyncio.run(tool_registry.dispatch(context, {"value": "hi"}))

    # First-declared (alpha) won the name.
    assert result["data"]["marker"] == "from-alpha"
    alpha_errors = _record(registry, "alpha").capability_errors
    zeta_errors = _record(registry, "zeta").capability_errors
    assert any("zeta" in message for message in alpha_errors)
    assert any("alpha" in message and "skipped" in message for message in zeta_errors)


def test_extension_with_skipped_tool_stays_loaded(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "shadow_ext", _tool_extension_source("read", "from-ext"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    tool_registry.register("read", "builtin read", {"type": "object"}, lambda context, args: {})
    registry.apply_tools(tool_registry)

    record = _record(registry, "shadow_ext")
    # A skipped capability is non-fatal: the extension still loaded and is not
    # counted among failed diagnostics.
    assert record.status == "loaded"
    assert record not in registry.diagnostics()
    assert record.capability_errors != []


# --- recall backends ---------------------------------------------------------


def test_extension_recall_backend_becomes_selectable(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "recall_ext", _recall_extension_source("my_backend"))

    registry = ExtensionRegistry.load(root)
    recall_registry = RecallBackendRegistry.with_builtins()
    registry.apply_recall_backends(recall_registry)

    assert "my_backend" in recall_registry.names()
    backend = recall_registry.create("my_backend", _recall_context(tmp_path))
    assert backend.search(object()) == {"kind": "search"}
    assert _record(registry, "recall_ext").capability_errors == []


def test_extension_recall_backend_duplicate_name_is_diagnosed(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "dup_backend", _recall_extension_source("jsonl_scan"))

    registry = ExtensionRegistry.load(root)
    recall_registry = RecallBackendRegistry.with_builtins()
    registry.apply_recall_backends(recall_registry)

    # Built-in jsonl_scan factory is unchanged; the extension's was skipped.
    errors = _record(registry, "dup_backend").capability_errors
    assert any("jsonl_scan" in message for message in errors)


def test_extension_recall_backend_invalid_name_is_diagnosed(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "bad_backend", _recall_extension_source("Bad_Name"))

    registry = ExtensionRegistry.load(root)
    recall_registry = RecallBackendRegistry.with_builtins()
    registry.apply_recall_backends(recall_registry)

    assert "Bad_Name" not in recall_registry.names()
    errors = _record(registry, "bad_backend").capability_errors
    assert any("Bad_Name" in message and "snake_case" in message for message in errors)
