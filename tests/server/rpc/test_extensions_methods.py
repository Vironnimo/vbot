"""Tests for extension visibility RPC handlers.

Coverage:
- ``extensions.list``: payload for loaded / failed / disabled records, capability
  summary, persisted-config merge, empty when no registry, rejects params.
- ``settings.update`` ``extensions`` section: round-trip persistence plus the
  restart-required signal (and its absence for other sections).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.extensions.extensions import (
    ExtensionDeclarations,
    ExtensionManifest,
    ExtensionRecord,
    RecallBackendDeclaration,
    ToolDeclaration,
)
from server.rpc.methods import dispatch_rpc
from tests.server.test_rpc import StubAdapter, make_state

JsonObject = dict[str, Any]


def _noop_handler(*_args: Any, **_kwargs: Any) -> None:
    return None


class _Registry:
    """Minimal stand-in for ``ExtensionRegistry`` exposing ``records()``."""

    def __init__(self, records: list[ExtensionRecord]) -> None:
        self._records = records

    def records(self) -> list[ExtensionRecord]:
        return list(self._records)


class _Storage:
    def __init__(self, config: dict[str, dict[str, Any]]) -> None:
        self._config = config

    def load_extensions_settings(self) -> JsonObject:
        return {"disabled": [], "config": self._config}


def _state_with_records(
    records: list[ExtensionRecord],
    config: dict[str, dict[str, Any]] | None = None,
) -> SimpleNamespace:
    runtime = SimpleNamespace(extensions=_Registry(records), storage=_Storage(config or {}))
    return SimpleNamespace(runtime=runtime)


def _loaded_record() -> ExtensionRecord:
    declarations = ExtensionDeclarations()
    declarations.hooks["tool_call"].append(_noop_handler)
    declarations.hooks["run_end"].append(_noop_handler)
    declarations.tools.append(
        ToolDeclaration("word_count", "Count words", {"type": "object"}, _noop_handler)
    )
    declarations.recall_backends.append(RecallBackendDeclaration("my_backend", _noop_handler))
    declarations.startup.append(_noop_handler)
    return ExtensionRecord(
        name="guard_bash",
        root_path=Path("/ext/guard_bash"),
        entry_path=Path("/ext/guard_bash/__init__.py"),
        status="loaded",
        manifest=ExtensionManifest(
            version="1.2.0",
            description="Guards dangerous bash",
            api_version=1,
            display_name="Bash Guard",
        ),
        declarations=declarations,
    )


@pytest.mark.asyncio
async def test_extensions_list_returns_loaded_failed_disabled_records() -> None:
    failed = ExtensionRecord(
        name="broken",
        root_path=Path("/ext/broken.py"),
        entry_path=Path("/ext/broken.py"),
        status="failed",
        error="import failed: boom",
    )
    disabled = ExtensionRecord(
        name="off",
        root_path=Path("/ext/off.py"),
        entry_path=Path("/ext/off.py"),
        status="disabled",
    )
    state = _state_with_records(
        [_loaded_record(), failed, disabled],
        config={"guard_bash": {"deny": ["rm -rf"]}},
    )

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    assert result["ok"] is True
    extensions = result["result"]["extensions"]
    assert [item["name"] for item in extensions] == ["guard_bash", "broken", "off"]

    loaded, failed_item, disabled_item = extensions
    assert loaded == {
        "name": "guard_bash",
        "status": "loaded",
        "disabled": False,
        "root": str(Path("/ext/guard_bash")),
        "entry": str(Path("/ext/guard_bash/__init__.py")),
        "error": None,
        "capability_errors": [],
        "version": "1.2.0",
        "description": "Guards dangerous bash",
        "display_name": "Bash Guard",
        "api_version": 1,
        "config": {"deny": ["rm -rf"]},
        "capabilities": {
            "hooks": {"tool_call": 1, "run_end": 1},
            "tools": ["word_count"],
            "recall_backends": ["my_backend"],
            "startup": True,
            "shutdown": False,
        },
    }
    assert failed_item["status"] == "failed"
    assert failed_item["error"] == "import failed: boom"
    assert failed_item["config"] == {}
    assert failed_item["capabilities"]["tools"] == []
    assert disabled_item["status"] == "disabled"
    assert disabled_item["disabled"] is True


@pytest.mark.asyncio
async def test_extensions_list_empty_without_registry() -> None:
    runtime = SimpleNamespace(extensions=None, storage=_Storage({}))
    state = SimpleNamespace(runtime=runtime)

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {}})

    assert result == {"ok": True, "result": {"extensions": []}}


@pytest.mark.asyncio
async def test_extensions_list_rejects_params() -> None:
    state = _state_with_records([])

    result = await dispatch_rpc(state, {"method": "extensions.list", "params": {"name": "x"}})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_settings_update_extensions_sets_restart_required(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {
                "extensions": {
                    "disabled": ["legacy"],
                    "config": {"guard_bash": {"deny": ["rm -rf"]}},
                }
            },
        },
    )

    assert result["ok"] is True
    assert result["result"]["restart_required"] is True
    assert state.runtime.storage.load_extensions_settings() == {
        "disabled": ["legacy"],
        "config": {"guard_bash": {"deny": ["rm -rf"]}},
    }


@pytest.mark.asyncio
async def test_settings_update_without_extensions_has_no_restart_flag(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"appearance": {"language": "en"}}},
    )

    assert result["ok"] is True
    assert "restart_required" not in result["result"]
