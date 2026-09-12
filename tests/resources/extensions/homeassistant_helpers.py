"""Shared fixtures and fakes for homeassistant behavior tests.

These load the **real** extension out of ``resources/extensions/homeassistant``
through ``ExtensionRegistry.load`` (with a bundled root) and apply its declared
tools into a fresh ``ToolRegistry`` — so they double as proof that the bundled
root ships a loadable extension. Credential and config are supplied through
mutable stubs so the live per-call reads (token, URL, readiness) can change
between calls exactly as they would through the settings UI.

The HTTP mocking approach mirrors the retired ``tests/core/tools/`` suite
(``respx`` + ``httpx``), and every behavior assertion from it is ported here.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.extensions import ExtensionRegistry
from core.tools.contracts import ToolContractError
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope, tool_failure

_REPO_ROOT = Path(__file__).resolve().parents[3]

_BUNDLED_EXTENSIONS_DIR = _REPO_ROOT / "resources" / "extensions"

HA_LIST_ENTITIES_NAME = "ha_list_entities"

HA_GET_STATE_NAME = "ha_get_state"

HA_LIST_SERVICES_NAME = "ha_list_services"

HA_CALL_SERVICE_NAME = "ha_call_service"

_HASS_URL = "http://homeassistant.local:8123"

_TOKEN = "test-ha-token"

_EXTENSION_NAME = "homeassistant"

_EXTENSION_MODULE = "vbot_ext.homeassistant"


class _State:
    """Mutable credential + config store backing the live per-call reads."""

    def __init__(self) -> None:
        self.credentials: dict[str, str] = {}
        self.config: dict[str, Any] = {}

    def resolve_credential(self, key: str) -> str:
        return self.credentials.get(key, "")

    def config_for(self, name: str) -> dict[str, Any]:
        del name
        return dict(self.config)


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _load_registry(state: _State) -> tuple[ExtensionRegistry, ToolRegistry]:
    """Load the shipped extension and apply its tools into a fresh registry."""
    extensions = ExtensionRegistry.load(
        _REPO_ROOT / "does-not-exist-data-extensions",
        bundled_dir=_BUNDLED_EXTENSIONS_DIR,
        credential_resolver=state.resolve_credential,
        config_provider=state.config_for,
    )
    tools = ToolRegistry()
    extensions.apply_tools(tools)
    return extensions, tools


def _tools_with_token() -> ToolRegistry:
    state = _State()
    state.credentials["HASS_TOKEN"] = _TOKEN
    _, tools = _load_registry(state)
    return tools


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch the loaded extension's backoff sleep; return the attempts list."""
    module = sys.modules[_EXTENSION_MODULE]
    sleep_attempts: list[int] = []

    async def _fake_sleep(attempt: int) -> None:
        sleep_attempts.append(attempt)

    monkeypatch.setattr(module, "_sleep_for_retry", _fake_sleep)
    return sleep_attempts


def make_context(tool_name: str = HA_LIST_ENTITIES_NAME) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=Path("/tmp/workspace"),
        vbot_root=Path("/tmp/app"),
        data_root=Path("/tmp/data"),
    )


def assert_success_envelope(result: dict[str, object]) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]


async def _dispatch(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a call with the same validation envelope as ``ToolExecutor``."""
    try:
        return await registry.dispatch(make_context(tool_name), arguments)
    except ToolContractError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)
