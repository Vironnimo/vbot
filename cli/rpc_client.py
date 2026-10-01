"""Shared RPC transport client for CLI management commands."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Literal

import httpx

from cli._progress import operation_progress
from cli._server_target import RpcFailure
from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0

# Methods that legitimately run far longer than the default cap. Model refreshes
# fan out across Provider endpoints, while data snapshots copy and verify
# databases whose size is user-controlled. Archiving, restoring and permanently
# deleting move or delete every Session and file of an Agent, Project or Session,
# and a purge of the whole archive is one request. These calls leave the read
# phase unbounded after the local server accepts them, while the ordinary
# connect, write, and pool limits still fail fast when the server cannot be reached.
_LONG_RUNNING_METHODS: frozenset[str] = frozenset(
    {
        "model.refresh_db",
        "performance.recording_stop",
        "performance.heap",
        "data_store.snapshot_create",
        "skill.install",
        "agent.delete",
        "project.rm",
        "session.delete",
        "archive.restore",
        "archive.purge",
    }
)
RPC_LONG_RUNNING_TIMEOUT = httpx.Timeout(RPC_TIMEOUT_SECONDS, read=None)
# A Run's SSE stream sends a heartbeat after 10 quiet seconds; six missed
# heartbeats mean the connection is gone, not that a Tool call is slow.
RUN_EVENTS_TIMEOUT = httpx.Timeout(RPC_TIMEOUT_SECONDS, read=60.0)

_PROGRESS_PHASES = {
    "skill.install": "Preparing and validating the Skill package",
    "model.refresh_db": "Refreshing Model catalogs from Providers",
    "data_store.snapshot_create": "Creating and verifying a data snapshot",
    "extensions.reload": "Reloading Extensions and checking their load results",
    "provider.usage": "Checking live Provider usage limits",
    "performance.recording_stop": "Writing the performance trace file",
    "archive.purge": "Deleting archived Sessions and files",
}


class RpcPayload:
    """Normalized server RPC success or failure payload.

    ``error_data`` is the server error's structured ``data`` object, when it sent one.
    """

    def __init__(
        self,
        *,
        ok: bool,
        instance: ServerInstance,
        data: Mapping[str, Any] | None = None,
        message: str = "",
        failure: RpcFailure | None = None,
        error_data: Mapping[str, Any] | None = None,
    ) -> None:
        self.ok = ok
        self.instance = instance
        self.data = data or {}
        self.message = message
        self.failure = failure
        self.error_data = error_data or {}

    def to_command_result(self) -> CommandResult:
        return CommandResult(
            ok=False, message=self.message, instance=self.instance, failure=self.failure
        )


def rpc_call(instance: ServerInstance, method: str, params: dict[str, Any]) -> RpcPayload:
    """Call one server RPC method and return normalized success/error payload."""

    request_body = {"method": method, "params": params}
    if method in _PROGRESS_PHASES:
        operation_progress(_PROGRESS_PHASES[method])
    timeout: httpx.Timeout | float = (
        RPC_LONG_RUNNING_TIMEOUT if method in _LONG_RUNNING_METHODS else RPC_TIMEOUT_SECONDS
    )
    try:
        response = httpx.post(
            f"{instance.url}{RPC_PATH}",
            json=request_body,
            timeout=timeout,
            # The CLI only ever talks to the local server over loopback, and RPC bodies
            # carry secrets (e.g. provider.set_key). Ignore ambient HTTP_PROXY/.netrc so a
            # plaintext credential can never be diverted through an environment proxy — the
            # same hardening the health/webui probes already apply.
            trust_env=False,
        )
    except httpx.RequestError as exc:
        not_sent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        return _transport_failure(
            instance,
            method,
            f"RPC request failed: {exc.__class__.__name__}",
            request_state="not_sent" if not_sent else "unknown",
        )

    try:
        payload = response.json()
    except ValueError:
        return _transport_failure(
            instance, method, f"RPC response was not JSON (HTTP {response.status_code})"
        )

    if not isinstance(payload, dict):
        return _transport_failure(instance, method, "RPC response must be an object")

    ok_flag = payload.get("ok")
    error = payload.get("error")
    if (
        ok_flag is False
        and isinstance(error, dict)
        and (isinstance(error.get("code"), str) and isinstance(error.get("message"), str))
    ):
        error_data = error.get("data")
        return RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(error, fallback="RPC request failed"),
            failure=RpcFailure(method, "responded", error["code"], response.status_code),
            error_data=error_data if isinstance(error_data, dict) else None,
        )
    if response.status_code != httpx.codes.OK:
        return _transport_failure(
            instance,
            method,
            f"No valid RPC error result (HTTP {response.status_code})",
            http_status=response.status_code,
        )
    if ok_flag is True:
        result = payload.get("result")
        if not isinstance(result, dict):
            return _transport_failure(instance, method, "RPC result must be an object")
        return RpcPayload(ok=True, instance=instance, data=result)
    if ok_flag is False:
        return _transport_failure(instance, method, "RPC response missing a valid error")

    return _transport_failure(instance, method, "RPC response missing boolean ok flag")


def _transport_failure(
    instance: ServerInstance,
    method: str,
    reason: str,
    *,
    request_state: Literal["not_sent", "unknown"] = "unknown",
    http_status: int | None = None,
) -> RpcPayload:
    """Preserve delivery uncertainty without exposing request or response bodies."""
    recovery = (
        "This RPC was not sent; earlier steps in the command may already be applied. "
        "Check connectivity to this target; server lifecycle checks must run on the server machine."
        if request_state == "not_sent"
        else "No valid result was received. The operation may have taken effect. Inspect the "
        "same target's current state before retrying a mutation; do not repeat completed steps."
    )
    return RpcPayload(
        ok=False,
        instance=instance,
        failure=RpcFailure(method, request_state, http_status=http_status),
        message=(
            f"{reason}\nrpc_method: {method}\nserver: {instance.url}\n"
            f"request_state: {request_state}\n{recovery}"
        ),
    )


class RunEventStreamError(Exception):
    """A Run's event stream could not be opened or read; the Run itself may continue."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def stream_run_events(
    instance: ServerInstance, sse_path: str, *, after_sequence: int = 0
) -> Iterator[dict[str, Any]]:
    """Yield one Run's timeline events after ``after_sequence`` from its SSE stream.

    ``sse_path`` is the server-relative ``sse_url`` a ``chat.stream`` result names.
    Transport heartbeats are skipped. The iteration ends when the server closes the
    stream, normally right after the terminal event; a lagging subscriber can be
    closed earlier and reconnects with the last sequence it received.
    """
    try:
        with httpx.stream(
            "GET",
            f"{instance.url}{sse_path}",
            params={"after_sequence": after_sequence},
            timeout=RUN_EVENTS_TIMEOUT,
            # Same hardening as rpc_call: never route through ambient proxies.
            trust_env=False,
        ) as response:
            if response.status_code != httpx.codes.OK:
                raise RunEventStreamError(
                    f"Run event stream unavailable (HTTP {response.status_code})",
                    status_code=response.status_code,
                )
            yield from _server_sent_events(response.iter_lines())
    except httpx.RequestError as exc:
        raise RunEventStreamError(f"Run event stream failed: {exc.__class__.__name__}") from exc


def _server_sent_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Decode SSE frames whose data is one JSON object; skip heartbeats and comments."""
    name = ""
    data: list[str] = []
    for line in lines:
        if not line:
            if data and name != "heartbeat":
                try:
                    event = json.loads("\n".join(data))
                except ValueError as exc:
                    raise RunEventStreamError("Run event stream sent malformed JSON") from exc
                if isinstance(event, dict):
                    yield event
            name, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _separator, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)


def _rpc_error_message(error: object, *, fallback: str) -> str:
    """Format a stable error message from server RPC error payload."""

    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(code, str) and isinstance(message, str):
            return f"{code}: {message}"
        if isinstance(message, str):
            return message
    return fallback
