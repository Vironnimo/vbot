"""Tests for debug-mode RPC handlers.

Test coverage:
- ``debug.status``: returns correct state, rejects params
- ``debug.trace_list``: enabled/disabled gating, returns traces in order
- ``debug.trace_get``: enabled/disabled gating, returns full trace
- ``debug.trace_clear``: always allowed, clears all traces
- ``debug.model_probe``: gating, error cases, success case (mocked HTTP)
- ``settings.get``: includes ``debug`` section
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.debug.store import DebugTraceStore
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST
from server.rpc.methods import dispatch_rpc
from tests.server.test_rpc import (
    StubAdapter,
    make_state,
)

JsonObject = dict[str, Any]

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _trace_data(
    trace_id: str,
    timestamp: str = "2026-06-01T12:00:00Z",
    provider_id: str = "openai",
    model_id: str = "gpt-4",
    request_method: str = "POST",
    request_url: str = "https://api.example.com/v1/chat",
    status_code: int | None = 200,
    duration_ms: int | None = 150,
) -> JsonObject:
    """Build a realistic trace payload used for store seeding."""
    return {
        "trace_id": trace_id,
        "timestamp": timestamp,
        "provider_id": provider_id,
        "model_id": model_id,
        "request_method": request_method,
        "request_url": request_url,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "request": {
            "method": request_method,
            "url": request_url,
            "headers": {"Content-Type": "application/json"},
            "body": {"model": model_id, "messages": [{"role": "user", "content": "hello"}]},
        },
        "response": {
            "status_code": status_code,
            "headers": {"Content-Type": "application/json"},
            "body": {"choices": [{"message": {"content": "hello back"}}]},
        },
    }


def _make_debug_state(
    tmp_path: Path,
    *,
    debug_enabled: bool = True,
    trace_limit: int = 50,
) -> SimpleNamespace:
    """Create a test RPC state with ``load_debug_settings`` on the stub storage."""
    state = make_state(tmp_path, StubAdapter())

    storage = state.runtime.storage
    storage._debug_settings = {"enabled": debug_enabled, "trace_limit": trace_limit}
    storage.load_debug_settings = lambda: dict(storage._debug_settings)

    return state


def _make_probe_provider(
    provider_id: str = "openrouter",
    base_url: str = "https://openrouter.ai/api/v1",
    models_endpoint: str = "/models",
    credential_key: str = "OPENROUTER_API_KEY",
    connection_id: str = "api-key",
) -> SimpleNamespace:
    """Create a provider stub with full ``auth`` attributes suitable for
    ``debug.model_probe`` testing."""
    connections = [
        SimpleNamespace(
            id=connection_id,
            type="api_key",
            label="API Key",
            auth=SimpleNamespace(
                header="Authorization",
                prefix="Bearer ",
                credential_key=credential_key,
            ),
        )
    ]
    return SimpleNamespace(
        id=provider_id,
        name=provider_id.title(),
        adapter="openai_compatible",
        base_url=base_url,
        defaults={"max_tokens": 8192},
        extra_headers={},
        models_endpoint=models_endpoint,
        connections=connections,
    )


def _seed_traces(tmp_path: Path, *, trace_limit: int = 50) -> DebugTraceStore:
    """Write three test traces to disk and return the store."""
    store = DebugTraceStore(tmp_path, trace_limit=trace_limit)
    store.save_trace("a-1", _trace_data("a-1", "2026-06-01T10:00:00Z"))
    store.save_trace("a-2", _trace_data("a-2", "2026-06-01T12:00:00Z"))
    store.save_trace("a-3", _trace_data("a-3", "2026-06-01T11:00:00Z"))
    return store


# ---------------------------------------------------------------------------
# debug.status
# ---------------------------------------------------------------------------


class TestDebugStatus:
    """Tests for the ``debug.status`` RPC method."""

    @pytest.mark.asyncio
    async def test_returns_disabled_state(self, tmp_path: Path) -> None:
        """``debug.status`` returns ``enabled=false`` when debug is off."""
        state = _make_debug_state(tmp_path, debug_enabled=False, trace_limit=30)

        response = await dispatch_rpc(state, {"method": "debug.status", "params": {}})

        assert response["ok"] is True
        result = response["result"]
        assert result["enabled"] is False
        assert result["trace_limit"] == 30
        assert result["trace_count"] == 0
        assert result["data_directory"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_returns_enabled_state_with_trace_count(self, tmp_path: Path) -> None:
        """``debug.status`` reports correct ``trace_count`` when traces exist."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=True, trace_limit=100)

        response = await dispatch_rpc(state, {"method": "debug.status", "params": {}})

        assert response["ok"] is True
        result = response["result"]
        assert result["enabled"] is True
        assert result["trace_limit"] == 100
        assert result["trace_count"] == 3
        assert result["data_directory"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_rejects_params(self, tmp_path: Path) -> None:
        """``debug.status`` does not accept extraneous params."""
        state = _make_debug_state(tmp_path)

        response = await dispatch_rpc(state, {"method": "debug.status", "params": {"extra": 1}})

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST


# ---------------------------------------------------------------------------
# debug.trace_list
# ---------------------------------------------------------------------------


class TestDebugTraceList:
    """Tests for the ``debug.trace_list`` RPC method."""

    @pytest.mark.asyncio
    async def test_rejects_when_disabled(self, tmp_path: Path) -> None:
        """``debug.trace_list`` rejects access when ``debug.enabled`` is ``false``."""
        state = _make_debug_state(tmp_path, debug_enabled=False)

        response = await dispatch_rpc(state, {"method": "debug.trace_list", "params": {}})

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_DOMAIN
        assert "not enabled" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_returns_traces_newest_first(self, tmp_path: Path) -> None:
        """``debug.trace_list`` returns metadata-only entries sorted newest-first."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(state, {"method": "debug.trace_list", "params": {}})

        assert response["ok"] is True
        traces = response["result"]["traces"]
        assert len(traces) == 3
        assert [entry["trace_id"] for entry in traces] == ["a-2", "a-3", "a-1"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_traces(self, tmp_path: Path) -> None:
        """An empty store produces an empty trace list, not an error."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(state, {"method": "debug.trace_list", "params": {}})

        assert response["ok"] is True
        assert response["result"]["traces"] == []

    @pytest.mark.asyncio
    async def test_entries_are_metadata_only(self, tmp_path: Path) -> None:
        """Each listed entry contains only metadata — no request/response bodies."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(state, {"method": "debug.trace_list", "params": {}})

        assert response["ok"] is True
        for entry in response["result"]["traces"]:
            assert "request" not in entry
            assert "response" not in entry
            assert "trace_id" in entry
            assert "timestamp" in entry
            assert "provider_id" in entry

    @pytest.mark.asyncio
    async def test_rejects_params(self, tmp_path: Path) -> None:
        """``debug.trace_list`` does not accept extraneous params."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(state, {"method": "debug.trace_list", "params": {"limit": 5}})

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST


# ---------------------------------------------------------------------------
# debug.trace_get
# ---------------------------------------------------------------------------


class TestDebugTraceGet:
    """Tests for the ``debug.trace_get`` RPC method."""

    @pytest.mark.asyncio
    async def test_rejects_when_disabled(self, tmp_path: Path) -> None:
        """``debug.trace_get`` rejects access when ``debug.enabled`` is ``false``."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=False)

        response = await dispatch_rpc(
            state,
            {"method": "debug.trace_get", "params": {"trace_id": "a-1"}},
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_DOMAIN
        assert "not enabled" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_returns_full_trace_when_enabled(self, tmp_path: Path) -> None:
        """``debug.trace_get`` returns the complete trace including request/response."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(
            state,
            {"method": "debug.trace_get", "params": {"trace_id": "a-1"}},
        )

        assert response["ok"] is True
        trace = response["result"]["trace"]
        assert trace["trace_id"] == "a-1"
        assert "request" in trace
        assert "response" in trace
        assert trace["response"]["body"]["choices"][0]["message"]["content"] == "hello back"

    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_trace(self, tmp_path: Path) -> None:
        """Requesting a non-existent trace returns a domain error."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(
            state,
            {"method": "debug.trace_get", "params": {"trace_id": "nonexistent"}},
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_DOMAIN
        assert "not found" in response["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_missing_trace_id(self, tmp_path: Path) -> None:
        """``debug.trace_get`` requires a ``trace_id`` param."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(state, {"method": "debug.trace_get", "params": {}})

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
        assert "trace_id" in response["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_unsupported_fields(self, tmp_path: Path) -> None:
        """``debug.trace_get`` rejects params beyond ``trace_id``."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.trace_get",
                "params": {"trace_id": "a-1", "extra": True},
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST
        assert "unsupported" in response["error"]["message"].lower()


# ---------------------------------------------------------------------------
# debug.trace_clear
# ---------------------------------------------------------------------------


class TestDebugTraceClear:
    """Tests for the ``debug.trace_clear`` RPC method.

    ``debug.trace_clear`` is **always** allowed, even when
    ``debug.enabled`` is ``false``.
    """

    @pytest.mark.asyncio
    async def test_clears_traces_when_disabled(self, tmp_path: Path) -> None:
        """``trace_clear`` works even when debug is disabled."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=False)

        response = await dispatch_rpc(state, {"method": "debug.trace_clear", "params": {}})

        assert response["ok"] is True
        assert response["result"]["cleared"] is True
        remaining = DebugTraceStore(tmp_path, trace_limit=50).get_traces()
        assert remaining == []

    @pytest.mark.asyncio
    async def test_clears_traces_when_enabled(self, tmp_path: Path) -> None:
        """``trace_clear`` deletes all traces and the index."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(state, {"method": "debug.trace_clear", "params": {}})

        assert response["ok"] is True
        assert response["result"]["cleared"] is True
        remaining = DebugTraceStore(tmp_path, trace_limit=50).get_traces()
        assert remaining == []

    @pytest.mark.asyncio
    async def test_no_op_on_empty_store(self, tmp_path: Path) -> None:
        """Calling ``trace_clear`` on an empty store is safe (no-op)."""
        state = _make_debug_state(tmp_path, debug_enabled=False)

        response = await dispatch_rpc(state, {"method": "debug.trace_clear", "params": {}})

        assert response["ok"] is True
        assert response["result"]["cleared"] is True

    @pytest.mark.asyncio
    async def test_rejects_params(self, tmp_path: Path) -> None:
        """``debug.trace_clear`` does not accept extraneous params."""
        state = _make_debug_state(tmp_path)

        response = await dispatch_rpc(
            state, {"method": "debug.trace_clear", "params": {"all": True}}
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST


# ---------------------------------------------------------------------------
# debug.model_probe
# ---------------------------------------------------------------------------


class TestDebugModelProbe:
    """Tests for the ``debug.model_probe`` RPC method."""

    @pytest.mark.asyncio
    async def test_rejects_when_disabled(self, tmp_path: Path) -> None:
        """``debug.model_probe`` rejects access when ``debug.enabled`` is ``false``."""
        state = _make_debug_state(tmp_path, debug_enabled=False)

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.model_probe",
                "params": {
                    "provider_id": "openai",
                    "connection_id": "openai:api-key",
                },
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_DOMAIN
        assert "not enabled" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_rejects_unknown_provider(self, tmp_path: Path) -> None:
        """Probing a non-existent provider returns a domain error."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.model_probe",
                "params": {
                    "provider_id": "nonexistent",
                    "connection_id": "nonexistent:key",
                },
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_DOMAIN
        assert "unknown provider" in response["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_provider_without_models_endpoint(self, tmp_path: Path) -> None:
        """Providers without ``models_endpoint`` cannot be probed."""
        state = _make_debug_state(tmp_path, debug_enabled=True)
        # The "anthropic" stub has no models_endpoint attribute by default
        state.runtime.storage._credentials["ANTHROPIC_API_KEY"] = "sk-test"

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.model_probe",
                "params": {
                    "provider_id": "anthropic",
                    "connection_id": "anthropic:api-key",
                },
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_DOMAIN
        assert "does not support model probing" in response["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_rejects_invalid_connection_id(self, tmp_path: Path) -> None:
        """A connection ID that does not match the provider raises an error."""
        state = _make_debug_state(tmp_path, debug_enabled=True)
        state.runtime.providers.add(_make_probe_provider())

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.model_probe",
                "params": {
                    "provider_id": "openrouter",
                    "connection_id": "wrong-prefix:api-key",
                },
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_DOMAIN
        assert (
            "unknown connection id 'wrong-prefix:api-key' for provider 'openrouter'"
            in response["error"]["message"].lower()
        )

    @pytest.mark.asyncio
    async def test_rejects_missing_provider_id(self, tmp_path: Path) -> None:
        """``debug.model_probe`` requires both ``provider_id`` and ``connection_id``."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.model_probe",
                "params": {"connection_id": "openai:api-key"},
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_rejects_missing_connection_id(self, tmp_path: Path) -> None:
        """``debug.model_probe`` requires both ``provider_id`` and ``connection_id``."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.model_probe",
                "params": {"provider_id": "openai"},
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_rejects_unsupported_fields(self, tmp_path: Path) -> None:
        """``debug.model_probe`` rejects params beyond ``provider_id`` and
        ``connection_id``."""
        state = _make_debug_state(tmp_path, debug_enabled=True)

        response = await dispatch_rpc(
            state,
            {
                "method": "debug.model_probe",
                "params": {
                    "provider_id": "openai",
                    "connection_id": "openai:api-key",
                    "extra": True,
                },
            },
        )

        assert response["ok"] is False
        assert response["error"]["code"] == RPC_ERROR_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_returns_raw_response_and_normalized_preview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Successfully probes a provider's models endpoint, returning raw
        JSON response + a normalized model-count/preview summary."""
        state = _make_debug_state(tmp_path, debug_enabled=True)
        state.runtime.providers.add(_make_probe_provider())
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")

        mock_raw_body = json.dumps(
            {
                "data": [
                    {"id": "gpt-4", "name": "GPT-4"},
                    {"id": "gpt-4-mini", "name": "GPT-4 Mini"},
                    {"id": "claude-3-opus", "name": "Claude 3 Opus"},
                ]
            }
        )

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = mock_raw_body
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("server.rpc.debug_methods.httpx.AsyncClient", return_value=mock_client):
            response = await dispatch_rpc(
                state,
                {
                    "method": "debug.model_probe",
                    "params": {
                        "provider_id": "openrouter",
                        "connection_id": "openrouter:api-key",
                    },
                },
            )

        assert response["ok"] is True, response
        result = response["result"]
        assert result["raw_response"] == mock_raw_body
        assert result["status_code"] == 200
        assert isinstance(result["duration_ms"], int)
        assert result["duration_ms"] >= 0
        assert result["trace_id"] is not None
        assert len(result["trace_id"]) > 0

        # Model preview
        model_preview = result["model_preview"]
        assert model_preview["model_count"] == 3
        assert model_preview["models"] == [
            {"id": "gpt-4", "name": "GPT-4"},
            {"id": "gpt-4-mini", "name": "GPT-4 Mini"},
            {"id": "claude-3-opus", "name": "Claude 3 Opus"},
        ]

        # Verify a model_probe trace was persisted
        store = DebugTraceStore(tmp_path, trace_limit=50)
        traces = store.get_traces()
        assert len(traces) == 1
        saved = store.get_trace(result["trace_id"])
        assert saved["type"] == "model_probe"
        assert saved["provider_id"] == "openrouter"


# ---------------------------------------------------------------------------
# settings.get debug section
# ---------------------------------------------------------------------------


class TestSettingsGetDebugSection:
    """Verify that ``settings.get`` includes the ``debug`` section."""

    @pytest.mark.asyncio
    async def test_includes_debug_section_when_disabled(self, tmp_path: Path) -> None:
        """``settings.get`` includes ``debug`` with ``enabled=false`` when debug
        is off."""
        state = _make_debug_state(tmp_path, debug_enabled=False, trace_limit=30)

        response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

        assert response["ok"] is True
        debug = response["result"]["debug"]
        assert debug == {
            "enabled": False,
            "trace_limit": 30,
            "trace_count": 0,
        }

    @pytest.mark.asyncio
    async def test_includes_debug_section_when_enabled_with_traces(self, tmp_path: Path) -> None:
        """``settings.get`` reports the live ``trace_count`` from disk."""
        _seed_traces(tmp_path)
        state = _make_debug_state(tmp_path, debug_enabled=True, trace_limit=75)

        response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

        assert response["ok"] is True
        debug = response["result"]["debug"]
        assert debug == {
            "enabled": True,
            "trace_limit": 75,
            "trace_count": 3,
        }

    @pytest.mark.asyncio
    async def test_debug_section_always_present(self, tmp_path: Path) -> None:
        """The ``debug`` key is always present in the ``settings.get`` response,
        even when no traces exist."""
        state = _make_debug_state(tmp_path, debug_enabled=False)

        response = await dispatch_rpc(state, {"method": "settings.get", "params": {}})

        assert response["ok"] is True
        assert "debug" in response["result"]
        assert isinstance(response["result"]["debug"]["enabled"], bool)
        assert isinstance(response["result"]["debug"]["trace_limit"], int)
        assert isinstance(response["result"]["debug"]["trace_count"], int)
