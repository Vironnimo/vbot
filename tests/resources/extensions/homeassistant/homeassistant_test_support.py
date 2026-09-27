"""Home Assistant test support: the shipped Extension behind production dispatch.

Tests load the real Extension from the bundled root and apply its Tools into a
fresh ``ToolRegistry``. Credentials and config come from a mutable
``LiveSettings`` store, so the per-call reads of token, URL and readiness can
change between calls exactly as they do through the settings UI. HTTP is mocked
with ``respx``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from core.extensions import ExtensionRegistry
from core.tools.contracts import ToolContractError
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope, tool_failure
from tests.resources.extensions.bundled_test_support import load_bundled

HA_LIST_ENTITIES_NAME = "ha_list_entities"
HA_GET_STATE_NAME = "ha_get_state"
HA_LIST_SERVICES_NAME = "ha_list_services"
HA_CALL_SERVICE_NAME = "ha_call_service"
HA_TOOL_NAMES = (
    HA_LIST_ENTITIES_NAME,
    HA_GET_STATE_NAME,
    HA_LIST_SERVICES_NAME,
    HA_CALL_SERVICE_NAME,
)

HASS_URL = "http://homeassistant.local:8123"
TOKEN = "test-ha-token"
EXTENSION_NAME = "homeassistant"

_EXTENSION_MODULE = "vbot_ext.homeassistant"


class LiveSettings:
    """Mutable credential and config store behind the Extension's live reads."""

    def __init__(self, *, token: str | None = None) -> None:
        self.credentials: dict[str, str] = {} if token is None else {"HASS_TOKEN": token}
        self.config: dict[str, Any] = {}

    def resolve_credential(self, key: str) -> str:
        return self.credentials.get(key, "")

    def config_for(self, name: str) -> dict[str, Any]:
        del name
        return dict(self.config)


def load(settings: LiveSettings) -> tuple[ExtensionRegistry, ToolRegistry]:
    """Load the shipped Extension and apply its Tools into a fresh registry."""
    extensions = load_bundled(
        EXTENSION_NAME,
        credential_resolver=settings.resolve_credential,
        config_provider=settings.config_for,
    )
    tools = ToolRegistry()
    extensions.apply_tools(tools)
    return extensions, tools


def tools_with_token() -> ToolRegistry:
    return load(LiveSettings(token=TOKEN))[1]


def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the loaded Extension's retry backoff; return the attempts it was asked for."""
    attempts: list[int] = []

    async def _record(attempt: int) -> None:
        attempts.append(attempt)

    monkeypatch.setattr(sys.modules[_EXTENSION_MODULE], "_sleep_for_retry", _record)
    return attempts


def make_context(tool_name: str) -> ToolContext:
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


async def dispatch(
    registry: ToolRegistry, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch a call with the same validation envelope as ``ToolExecutor``."""
    try:
        return await registry.dispatch(make_context(tool_name), arguments)
    except ToolContractError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)


def assert_success_envelope(result: dict[str, object]) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, Any]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error


def model_text(result: dict[str, Any]) -> str:
    """Return the plain text the Model reads for a Tool Result."""
    from core.providers.adapter import tool_result_text

    return str(tool_result_text(json.dumps(result)))


def request_body(route: Any, index: int = 0) -> Any:
    """Return the JSON body of one request a respx route received."""
    return json.loads(route.calls[index].request.content)
