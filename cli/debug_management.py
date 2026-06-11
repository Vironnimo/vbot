"""Debug-mode management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

_PROBE_PREVIEW_LIMIT = 10


def debug_status(instance: ServerInstance) -> CommandResult:
    """Return debug-mode state from `debug.status` RPC."""

    payload = _rpc_call(instance, "debug.status", {})
    if not payload.ok:
        return payload.to_command_result()
    enabled = "yes" if payload.data.get("enabled") else "no"
    trace_limit = _value_text(payload.data.get("trace_limit"))
    trace_count = _value_text(payload.data.get("trace_count"))
    data_directory = _string_or_default(payload.data.get("data_directory"), "-")
    return CommandResult(
        ok=True,
        message=(
            f"enabled={enabled} trace_limit={trace_limit} trace_count={trace_count} "
            f"data_directory={data_directory}"
        ),
        instance=instance,
    )


def debug_trace_list(instance: ServerInstance) -> CommandResult:
    """Return formatted trace metadata rows from `debug.trace_list` RPC."""

    payload = _rpc_call(instance, "debug.trace_list", {})
    if not payload.ok:
        return payload.to_command_result()
    traces = payload.data.get("traces")
    if not isinstance(traces, list):
        return CommandResult(ok=False, message="RPC result missing traces list", instance=instance)
    return CommandResult(ok=True, message=_format_trace_rows(traces), instance=instance)


def debug_trace_show(instance: ServerInstance, trace_id: str) -> CommandResult:
    """Return one full trace as JSON from `debug.trace_get` RPC."""

    payload = _rpc_call(instance, "debug.trace_get", {"trace_id": trace_id})
    if not payload.ok:
        return payload.to_command_result()
    trace = payload.data.get("trace")
    if trace is None:
        return CommandResult(ok=False, message="RPC result missing trace", instance=instance)
    return CommandResult(
        ok=True,
        message=json.dumps(trace, indent=2, ensure_ascii=False, sort_keys=True),
        instance=instance,
    )


def debug_trace_clear(instance: ServerInstance) -> CommandResult:
    """Delete all stored traces via `debug.trace_clear` RPC."""

    payload = _rpc_call(instance, "debug.trace_clear", {})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message="cleared all debug traces", instance=instance)


def debug_model_probe(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str,
) -> CommandResult:
    """Probe one provider models endpoint via `debug.model_probe` RPC."""

    params = {"provider_id": provider_id, "connection_id": connection_id}
    payload = _rpc_call(instance, "debug.model_probe", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_probe_result(provider_id, payload.data),
        instance=instance,
    )


def _format_probe_result(provider_id: str, data: Mapping[str, Any]) -> str:
    status_code = _value_text(data.get("status_code"))
    duration_ms = _value_text(data.get("duration_ms"))
    trace_id = _string_or_default(data.get("trace_id"), "-")
    lines = [
        f"probe {provider_id}: status_code={status_code} duration_ms={duration_ms} "
        f"trace_id={trace_id}"
    ]

    preview = data.get("model_preview")
    if not isinstance(preview, dict):
        return "\n".join(lines)

    error = preview.get("error")
    if isinstance(error, str) and error:
        lines.append(f"error: {error}")
        return "\n".join(lines)

    lines.append(f"model_count: {_value_text(preview.get('model_count'))}")
    models = preview.get("models")
    if isinstance(models, list) and models:
        lines.append(f"first {min(len(models), _PROBE_PREVIEW_LIMIT)} models:")
        for model in models[:_PROBE_PREVIEW_LIMIT]:
            if isinstance(model, dict):
                model_id = _string_or_default(model.get("id"), "?")
                lines.append(f"- {model_id}")
    lines.append("full raw response stored in the trace; read it with: debug trace " + trace_id)
    return "\n".join(lines)


def _format_trace_rows(traces: Sequence[object]) -> str:
    if not traces:
        return "no debug traces stored"

    lines = ["traces:"]
    for trace in traces:
        lines.append(_format_trace_row(trace))
    return "\n".join(lines)


def _format_trace_row(trace: object) -> str:
    if not isinstance(trace, dict):
        return "- invalid trace entry"

    trace_id = _string_or_default(trace.get("trace_id"), "?")
    trace_type = _string_or_default(trace.get("type"), "?")
    timestamp = _string_or_default(trace.get("timestamp"), "-")
    duration_ms = _value_text(trace.get("duration_ms"))
    provider_id = _string_or_default(trace.get("provider_id"), "-")
    model_id = _string_or_default(trace.get("model_id"), "-")
    return (
        f"- id={trace_id}"
        f" type={trace_type}"
        f" timestamp={timestamp}"
        f" duration_ms={duration_ms}"
        f" provider={provider_id}"
        f" model={model_id}"
    )


def _value_text(value: object) -> str:
    if value is None:
        return "-"
    return str(value)


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
