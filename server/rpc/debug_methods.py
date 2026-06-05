"""Debug-mode RPC method handlers.

Provides handlers for ``debug.status``, ``debug.trace_list``,
``debug.trace_get``, ``debug.trace_clear``, and ``debug.model_probe``.

Access gating:
  - ``debug.status`` — always available.
  - ``debug.trace_*`` (except ``trace_clear``) and ``debug.model_probe`` —
    reject when ``debug.enabled`` is ``false``.
  - ``debug.trace_clear`` — always allowed.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from core.debug import DebugTraceStore
from core.debug.redaction import redact_headers, redact_url
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.provider_access import _provider_connection
from server.rpc.validation import _required_string

JsonObject = dict[str, Any]
_logger = logging.getLogger("vbot.server.rpc.debug_methods")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_debug_enabled(runtime: Any) -> None:
    """Raise :exc:`RpcError` when ``debug.enabled`` is ``false``."""
    debug_settings = runtime.storage.load_debug_settings()
    if not debug_settings.get("enabled", False):
        raise RpcError(RPC_ERROR_DOMAIN, "debug mode is not enabled")


def _make_debug_store(runtime: Any) -> DebugTraceStore:
    """Create a :class:`DebugTraceStore` from current runtime settings."""
    debug_settings = runtime.storage.load_debug_settings()
    trace_limit = debug_settings.get("trace_limit", 50)
    return DebugTraceStore(runtime.storage.data_dir, trace_limit=trace_limit)


async def _resolve_connection_credential(
    runtime: Any,
    provider_id: str,
    connection_id: str,
    connection: Any,
) -> str:
    """Resolve the credential value for a single provider connection.

    Handles both API-key and OAuth connection types, raising on
    missing configuration or unavailable token stores.
    """
    if getattr(connection, "type", "") != "oauth" or getattr(connection, "oauth", None) is None:
        return str(runtime.provider_credentials.get_credentials(provider_id, connection_id))

    from core.providers.token_getter import OAuthTokenGetter

    token_store = getattr(runtime, "token_store", None)
    if token_store is None:
        raise RpcError(RPC_ERROR_DOMAIN, "OAuth token store is not available")
    getter = OAuthTokenGetter(token_store, provider_id, connection.id, connection.oauth)
    async with getter:
        return await getter()


def _save_model_probe_trace(
    runtime: Any,
    trace_id: str,
    url: str,
    headers: dict[str, str],
    status_code: int,
    response_headers: dict[str, str],
    raw_body: str,
    duration_ms: int,
    provider_id: str,
    connection_id: str,
) -> None:
    """Persist a model-probe trace via :class:`DebugTraceStore`.

    Trace persistence failures are logged but never raised — the caller
    still returns the probe result to the client.
    """
    try:
        store = _make_debug_store(runtime)
        store.save_trace(
            trace_id,
            {
                "trace_id": trace_id,
                "type": "model_probe",
                "timestamp": datetime.now(UTC).isoformat(),
                "duration_ms": duration_ms,
                "provider_id": provider_id,
                "model_id": "",
                "request": {
                    "method": "GET",
                    "url": redact_url(url),
                    "headers": redact_headers(headers),
                    "body": None,
                },
                "response": {
                    "status_code": status_code,
                    "headers": redact_headers(response_headers),
                    "body": raw_body,
                },
            },
        )
    except Exception:
        _logger.warning("Failed to persist model probe trace", exc_info=True)


def _build_model_preview(raw_body: str, status_code: int) -> JsonObject:
    """Extract a lightweight model preview from a raw JSON response.

    Returns a dict with ``model_count`` and a ``models`` list of the
    first 10 models (``id`` and ``name`` only).  On parse failure or
    non-200 status, returns an ``error`` key.
    """
    if status_code != 200:
        return {"error": f"HTTP {status_code}", "models": []}
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"error": "response is not valid JSON", "models": []}

    models = _extract_model_list(payload)
    if not models:
        return {"model_count": 0, "models": []}

    preview: list[dict[str, str]] = []
    max_preview = 10
    for raw_model in models[:max_preview]:
        if isinstance(raw_model, dict):
            model_id = raw_model.get("id", "")
            model_name = raw_model.get("name", "")
            preview.append(
                {
                    "id": str(model_id),
                    "name": str(model_name) if model_name else str(model_id),
                }
            )
        else:
            preview.append({"id": str(raw_model), "name": str(raw_model)})

    return {"model_count": len(models), "models": preview}


def _extract_model_list(payload: Any) -> list[Any]:
    """Extract the model array from a provider models-endpoint response.

    Handles common response shapes (top-level ``data`` list, ``models``
    list, or bare top-level list).
    """
    if not isinstance(payload, dict):
        return []
    if "data" in payload and isinstance(payload["data"], list):
        return payload["data"]
    if "models" in payload and isinstance(payload["models"], list):
        return payload["models"]
    return []


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _debug_status(state: Any, params: JsonObject) -> JsonObject:
    """Return current debug-mode state.

    Always available — does not gate on ``debug.enabled`` so the
    frontend can discover the current state.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "debug.status does not accept params")
    try:
        runtime = state.runtime
        debug_settings = runtime.storage.load_debug_settings()
        enabled = debug_settings.get("enabled", False)
        trace_limit = debug_settings.get("trace_limit", 50)
        store = _make_debug_store(runtime)
        trace_count = len(store.get_traces())
        return {
            "enabled": enabled,
            "trace_limit": trace_limit,
            "trace_count": trace_count,
            "data_directory": str(runtime.storage.data_dir),
        }
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _debug_trace_list(state: Any, params: JsonObject) -> JsonObject:
    """Return metadata for all stored debug traces (newest first).

    Requires ``debug.enabled`` to be ``true``.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "debug.trace_list does not accept params")
    try:
        runtime = state.runtime
        _ensure_debug_enabled(runtime)
        store = _make_debug_store(runtime)
        traces = store.get_traces()
        return {"traces": traces}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _debug_trace_get(state: Any, params: JsonObject) -> JsonObject:
    """Return the full sanitized trace for *trace_id*.

    Requires ``debug.enabled`` to be ``true``.
    """
    trace_id = _required_string(params, "trace_id")
    unsupported_fields = sorted(set(params) - {"trace_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported debug.trace_get fields: {', '.join(unsupported_fields)}",
        )
    try:
        runtime = state.runtime
        _ensure_debug_enabled(runtime)
        store = _make_debug_store(runtime)
        trace = store.get_trace(trace_id)
        return {"trace": trace}
    except FileNotFoundError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _debug_trace_clear(state: Any, params: JsonObject) -> JsonObject:
    """Delete all debug trace files and the metadata index.

    **Always allowed** — does not gate on ``debug.enabled``.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "debug.trace_clear does not accept params")
    try:
        runtime = state.runtime
        store = _make_debug_store(runtime)
        store.clear_all()
        return {"cleared": True}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _debug_model_probe(state: Any, params: JsonObject) -> JsonObject:
    """Fetch a provider's ``models_endpoint`` and return raw + normalized preview.

    Requires ``debug.enabled`` to be ``true``.  Does **not** write
    ``resources/models/*.json`` or reload the model registry.

    Stores the probe result as a ``model_probe`` type trace.
    """
    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported debug.model_probe fields: {', '.join(unsupported_fields)}",
        )

    runtime = state.runtime
    _ensure_debug_enabled(runtime)

    try:
        provider = runtime.providers.get(provider_id)
    except KeyError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, f"unknown provider: {provider_id}") from exc

    models_endpoint = getattr(provider, "models_endpoint", None)
    if not models_endpoint:
        raise RpcError(
            RPC_ERROR_DOMAIN,
            f"provider '{provider_id}' does not support model probing",
        )

    try:
        connection = _provider_connection(runtime, provider_id, connection_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    url = f"{provider.base_url.rstrip('/')}/{models_endpoint.lstrip('/')}"
    headers = dict(provider.extra_headers or {})

    try:
        credential_value = await _resolve_connection_credential(
            runtime, provider_id, connection_id, connection
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    headers[connection.auth.header] = f"{connection.auth.prefix}{credential_value}"

    trace_id = uuid4().hex
    start_time = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, headers=headers)
            status_code = response.status_code
            raw_body = response.text
    except httpx.HTTPError as exc:
        raise RpcError(
            RPC_ERROR_DOMAIN,
            f"failed to fetch models endpoint for provider '{provider_id}': {exc}",
        ) from exc

    duration_ms = int((time.monotonic() - start_time) * 1000)
    _save_model_probe_trace(
        runtime,
        trace_id,
        url,
        headers,
        status_code,
        dict(response.headers),
        raw_body,
        duration_ms,
        provider_id,
        connection_id,
    )

    model_preview = _build_model_preview(raw_body, status_code)

    return {
        "trace_id": trace_id,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "raw_response": raw_body,
        "model_preview": model_preview,
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return debug-mode RPC method handlers."""
    return {
        "debug.status": _debug_status,
        "debug.trace_list": _debug_trace_list,
        "debug.trace_get": _debug_trace_get,
        "debug.trace_clear": _debug_trace_clear,
        "debug.model_probe": _debug_model_probe,
    }
