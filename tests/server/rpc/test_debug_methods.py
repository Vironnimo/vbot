"""Tests for the debug-mode RPC handlers and the debug section of ``settings.get``."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from core.debug.store import DebugTraceStore
from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.storage.layout import DataDirectoryLayout
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST
from tests.server.rpc_test_support import (
    StubAdapter,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)

JsonObject = dict[str, Any]
TRACE_ID_1 = "00000000000040008000000000000001"
TRACE_ID_2 = "00000000000040008000000000000002"
TRACE_ID_3 = "00000000000040008000000000000003"
TRACE_ID_MISSING = "00000000000040008000000000000004"


def _trace_data(trace_id: str, timestamp: str) -> JsonObject:
    """Build a realistic trace payload used for store seeding."""
    return {
        "trace_id": trace_id,
        "timestamp": timestamp,
        "provider_id": "openai",
        "model_id": "gpt-4",
        "request_method": "POST",
        "request_url": "https://api.example.com/v1/chat",
        "status_code": 200,
        "duration_ms": 150,
        "request": {
            "method": "POST",
            "url": "https://api.example.com/v1/chat",
            "headers": {"Content-Type": "application/json"},
            "body": {"model": "gpt-4", "messages": [{"role": "user", "content": "hello"}]},
        },
        "response": {
            "status_code": 200,
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
    models_endpoint: str | None = "/models",
    credential_key: str = "OPENROUTER_API_KEY",
    connection_id: str = "api-key",
    *,
    adapter: str = "openai_compatible",
    connection_type: str = "api_key",
    auth: AuthConfig | None = None,
    connection_base_url: str | None = None,
    connection_models_endpoint: str | None = None,
) -> ProviderConfig:
    """Create a Provider config suitable for ``debug.model_probe`` testing."""
    if auth is None:
        auth = (
            AuthConfig(header="", prefix="")
            if connection_type == "none"
            else AuthConfig(header="Authorization", prefix="Bearer ", credential_key=credential_key)
        )
    return ProviderConfig(
        id=provider_id,
        name=provider_id.title(),
        adapter=adapter,
        base_url=base_url,
        connections=[
            ConnectionConfig(
                id=connection_id,
                type=connection_type,
                label="Probe Connection",
                auth=auth,
                base_url=connection_base_url,
                models_endpoint=connection_models_endpoint,
            )
        ],
        defaults={"max_tokens": 8192},
        extra_headers={},
        models_endpoint=models_endpoint,
    )


def _add_probe_provider(
    monkeypatch: pytest.MonkeyPatch,
    state: SimpleNamespace,
    provider: ProviderConfig,
    process_env: dict[str, str] | None = None,
) -> None:
    """Register *provider* and resolve its credentials with the production resolver."""
    state.runtime.providers.add(provider)
    resolver = ProviderCredentialResolver(
        ProviderRegistry({provider.id: provider}), process_env=process_env or {}
    )
    monkeypatch.setattr(
        type(state.runtime), "provider_credentials", property(lambda _runtime: resolver)
    )


def _seed_traces(tmp_path: Path) -> None:
    """Write three test traces to disk, out of timestamp order."""
    store = DebugTraceStore(tmp_path, trace_limit=50)
    store.save_trace(TRACE_ID_1, _trace_data(TRACE_ID_1, "2026-06-01T10:00:00Z"))
    store.save_trace(TRACE_ID_2, _trace_data(TRACE_ID_2, "2026-06-01T12:00:00Z"))
    store.save_trace(TRACE_ID_3, _trace_data(TRACE_ID_3, "2026-06-01T11:00:00Z"))


def _saved_trace(tmp_path: Path, trace_id: str) -> JsonObject:
    return DebugTraceStore(tmp_path, trace_limit=50).get_trace(trace_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "trace_limit", "seeded"),
    [pytest.param(False, 30, False, id="disabled"), pytest.param(True, 100, True, id="enabled")],
)
async def test_status_and_settings_report_the_debug_state(
    tmp_path: Path, enabled: bool, trace_limit: int, seeded: bool
) -> None:
    if seeded:
        _seed_traces(tmp_path)
    state = _make_debug_state(tmp_path, debug_enabled=enabled, trace_limit=trace_limit)

    status = await rpc_result(state, "debug.status")
    settings = await rpc_result(state, "settings.get")

    # Status is always available, so the client can discover the current state.
    trace_count = 3 if seeded else 0
    assert status == {
        "enabled": enabled,
        "trace_limit": trace_limit,
        "trace_count": trace_count,
        "data_directory": str(DataDirectoryLayout(tmp_path).debug),
    }
    assert settings["debug"] == {
        "enabled": enabled,
        "trace_limit": trace_limit,
        "trace_count": trace_count,
    }


@pytest.mark.asyncio
async def test_trace_list_returns_metadata_newest_first(tmp_path: Path) -> None:
    state = _make_debug_state(tmp_path)

    empty = await rpc_result(state, "debug.trace_list")
    _seed_traces(tmp_path)
    listed = await rpc_result(state, "debug.trace_list")

    assert empty == {"traces": []}
    traces = listed["traces"]
    assert [entry["trace_id"] for entry in traces] == [TRACE_ID_2, TRACE_ID_3, TRACE_ID_1]
    # Entries are metadata only; the bodies stay behind ``debug.trace_get``.
    for entry in traces:
        assert {"trace_id", "timestamp", "provider_id"} <= entry.keys()
        assert {"request", "response"}.isdisjoint(entry)


@pytest.mark.asyncio
async def test_trace_get_returns_the_full_trace_or_a_domain_error(tmp_path: Path) -> None:
    _seed_traces(tmp_path)
    state = _make_debug_state(tmp_path)

    trace = (await rpc_result(state, "debug.trace_get", trace_id=TRACE_ID_1))["trace"]
    missing = await rpc_error(state, "debug.trace_get", trace_id=TRACE_ID_MISSING)

    assert trace["trace_id"] == TRACE_ID_1
    assert "request" in trace
    assert trace["response"]["body"]["choices"][0]["message"]["content"] == "hello back"
    assert missing["code"] == RPC_ERROR_DOMAIN


@pytest.mark.asyncio
async def test_trace_clear_is_allowed_while_debug_is_disabled(tmp_path: Path) -> None:
    _seed_traces(tmp_path)
    state = _make_debug_state(tmp_path, debug_enabled=False)

    cleared = await rpc_result(state, "debug.trace_clear")
    # Clearing an empty store is a safe no-op.
    cleared_again = await rpc_result(state, "debug.trace_clear")

    assert cleared == cleared_again == {"cleared": True}
    assert DebugTraceStore(tmp_path, trace_limit=50).get_traces() == []
    assert resource_changes(state) == [{"kind": "debug_traces"}] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("debug.trace_list", {}),
        ("debug.trace_get", {"trace_id": TRACE_ID_1}),
        ("debug.model_probe", {"provider_id": "openai", "connection_id": "openai:api-key"}),
    ],
)
async def test_trace_reads_and_probes_require_debug_mode(
    tmp_path: Path, method: str, params: JsonObject
) -> None:
    _seed_traces(tmp_path)
    state = _make_debug_state(tmp_path, debug_enabled=False)

    error = await rpc_error(state, method, **params)

    assert error["code"] == RPC_ERROR_DOMAIN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "fragment"),
    [
        pytest.param("debug.status", {"extra": 1}, "", id="status-params"),
        pytest.param("debug.trace_list", {"limit": 5}, "", id="list-params"),
        pytest.param("debug.trace_clear", {"all": True}, "", id="clear-params"),
        pytest.param("debug.trace_get", {}, "trace_id", id="get-without-trace-id"),
        pytest.param(
            "debug.trace_get", {"trace_id": TRACE_ID_1, "extra": True}, "", id="get-extra-field"
        ),
        # A trace id cannot reach JSON outside the trace directory.
        pytest.param(
            "debug.trace_get",
            {"trace_id": "../../../channels/telegram/channel"},
            "",
            id="get-path-escape",
        ),
        pytest.param(
            "debug.model_probe", {"provider_id": "openai"}, "connection_id", id="probe-partial"
        ),
        pytest.param(
            "debug.model_probe",
            {"provider_id": "openai", "connection_id": "openai:api-key", "extra": True},
            "",
            id="probe-extra-field",
        ),
    ],
)
async def test_invalid_debug_requests_are_rejected(
    tmp_path: Path, method: str, params: JsonObject, fragment: str
) -> None:
    _seed_traces(tmp_path)
    state = _make_debug_state(tmp_path)

    error = await rpc_error(state, method, **params)

    assert error["code"] == RPC_ERROR_INVALID_REQUEST
    assert fragment in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "provider_id", "connection_id", "fragments"),
    [
        pytest.param(None, "nonexistent", "nonexistent:key", ("nonexistent",), id="unknown"),
        pytest.param(
            _make_probe_provider(),
            "openrouter",
            "wrong-prefix:api-key",
            ("wrong-prefix:api-key", "openrouter"),
            id="foreign-connection",
        ),
        # Neither the Connection nor its Provider names a catalog endpoint.
        pytest.param(
            _make_probe_provider(provider_id="no-catalog", models_endpoint=None),
            "no-catalog",
            "no-catalog:api-key",
            ("no-catalog:api-key",),
            id="no-endpoint",
        ),
    ],
)
async def test_model_probe_rejects_connections_it_cannot_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ProviderConfig | None,
    provider_id: str,
    connection_id: str,
    fragments: tuple[str, ...],
) -> None:
    state = _make_debug_state(tmp_path)
    if provider is not None:
        _add_probe_provider(monkeypatch, state, provider)

    error = await rpc_error(
        state, "debug.model_probe", provider_id=provider_id, connection_id=connection_id
    )

    assert error["code"] == RPC_ERROR_DOMAIN
    assert all(fragment in error["message"] for fragment in fragments)


_CATALOG = [
    {"id": "gpt-4", "name": "GPT-4"},
    {"id": "gpt-4-mini", "name": "GPT-4 Mini"},
    {"id": "claude-3-opus", "name": "Claude 3 Opus"},
]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("catalog", "preview"),
    [
        pytest.param(_CATALOG, {"model_count": 3, "models": _CATALOG}, id="catalog"),
        pytest.param([], {"model_count": 0, "models": []}, id="empty-catalog"),
    ],
)
async def test_model_probe_returns_raw_response_preview_and_a_redacted_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog: list[JsonObject],
    preview: JsonObject,
) -> None:
    state = _make_debug_state(tmp_path)
    _add_probe_provider(
        monkeypatch,
        state,
        _make_probe_provider(),
        process_env={"OPENROUTER_API_KEY": "sk-test-key"},
    )
    raw_body = json.dumps({"data": catalog})
    respx.get("https://openrouter.ai/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            text=raw_body,
            headers={
                "content-type": "application/json",
                "openai-organization": "org-123",
                "set-cookie": "session=abc",
            },
        )
    )

    result = await rpc_result(
        state, "debug.model_probe", provider_id="openrouter", connection_id="openrouter:api-key"
    )

    assert result["raw_response"] == raw_body
    assert result["status_code"] == 200
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0
    assert result["model_preview"] == preview
    # The probe is kept as a ``model_probe`` trace with credentials and session
    # headers redacted.
    assert len(DebugTraceStore(tmp_path, trace_limit=50).get_traces()) == 1
    saved = _saved_trace(tmp_path, result["trace_id"])
    assert saved["type"] == "model_probe"
    assert saved["provider_id"] == "openrouter"
    assert saved["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert (
        saved["response"]["headers"].items()
        >= {
            "content-type": "application/json",
            "openai-organization": "[REDACTED]",
            "set-cookie": "[REDACTED]",
        }.items()
    )
    assert "sk-test-key" not in json.dumps(saved)
    assert resource_changes(state) == [{"kind": "debug_traces"}]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "process_env", "request_url", "sent_headers", "unsent_headers"),
    [
        # A keyless Connection has no auth header name, so none is sent.
        pytest.param(
            _make_probe_provider(
                provider_id="local",
                base_url="http://127.0.0.1:1234/v1",
                connection_id="local",
                connection_type="none",
            ),
            {},
            "http://127.0.0.1:1234/v1/models",
            {},
            ("authorization",),
            id="keyless",
        ),
        # Connection ``base_url``/``models_endpoint`` override the Provider values.
        pytest.param(
            _make_probe_provider(
                provider_id="variant",
                base_url="https://api.variant.test/v1",
                models_endpoint=None,
                credential_key="VARIANT_API_KEY",
                connection_base_url="https://backend.variant.test/api",
                connection_models_endpoint="/connection/models",
            ),
            {"VARIANT_API_KEY": "sk-variant-secret"},
            "https://backend.variant.test/api/connection/models",
            {"authorization": "Bearer sk-variant-secret"},
            (),
            id="connection-endpoint",
        ),
        # The Adapter's discovery headers and parameters are sent as in discovery.
        pytest.param(
            _make_probe_provider(
                provider_id="anthropic-probe",
                base_url="https://api.anthropic.test/v1",
                adapter="anthropic",
                auth=AuthConfig(
                    header="x-api-key", prefix="", credential_key="PROBE_ANTHROPIC_KEY"
                ),
            ),
            {"PROBE_ANTHROPIC_KEY": "sk-ant-probe-secret"},
            "https://api.anthropic.test/v1/models?limit=1000",
            {"anthropic-version": "2023-06-01", "x-api-key": "sk-ant-probe-secret"},
            (),
            id="adapter-discovery",
        ),
    ],
)
async def test_model_probe_sends_the_connection_discovery_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: ProviderConfig,
    process_env: dict[str, str],
    request_url: str,
    sent_headers: dict[str, str],
    unsent_headers: tuple[str, ...],
) -> None:
    state = _make_debug_state(tmp_path)
    _add_probe_provider(monkeypatch, state, provider, process_env)
    route = respx.get(request_url.partition("?")[0]).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "probe-model"}]})
    )

    result = await rpc_result(
        state,
        "debug.model_probe",
        provider_id=provider.id,
        connection_id=f"{provider.id}:{provider.connections[0].id}",
    )

    request = route.calls.last.request
    assert str(request.url) == request_url
    assert {name: request.headers[name] for name in sent_headers} == sent_headers
    assert set(unsent_headers).isdisjoint(request.headers)
    assert all(name for name in request.headers)
    assert result["model_preview"]["model_count"] == 1
    saved = _saved_trace(tmp_path, result["trace_id"])
    assert saved["request"]["url"] == request_url
    assert not any(secret in json.dumps(saved) for secret in process_env.values())
