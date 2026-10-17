"""Paired device commands: list, approve or deny a pairing code, rename and revoke.

On the server's computer the CLI authenticates with the server's local access
token, so the host can always approve a new device, even when no paired device
is left (recovery).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from cli.formatting import record_fields, value_text
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

_RUN_AGENT_ENV = "VBOT_RUN_AGENT_ID"
_KIND_LABELS = {"browser": "browser", "desktop": "Desktop app", "cli": "CLI"}


def run_refusal(action: str, instead: str) -> str | None:
    """Why ``action`` refuses to run inside an Agent's Run; ``None`` outside one.

    Actions that hand out access (approving a device, minting a sign-in link,
    changing the access mode) come from the person, never from an Agent that
    some text asked to. The Run's shell environment (``VBOT_RUN_AGENT_ID``) is
    the only signal. It keeps a cooperative Agent from doing so by accident; it
    is no security boundary, since an Agent runs as the same user as the host
    and could unset it or read the local access token itself.
    """
    if not os.environ.get(_RUN_AGENT_ENV, "").strip():
        return None
    return f"{action} does not run inside an Agent's Run; {instead}"


def devices_list(instance: ServerInstance) -> CommandResult:
    payload = _rpc_call(instance, "devices.list", {})
    if not payload.ok:
        return payload.to_command_result()
    devices = payload.data.get("devices")
    if not isinstance(devices, list):
        return CommandResult(ok=False, message="RPC result missing devices list", instance=instance)
    if not devices:
        return CommandResult(ok=True, message="No paired devices.", instance=instance)
    rows = [_device_row(device) for device in devices if isinstance(device, Mapping)]
    return CommandResult(ok=True, message="\n".join(rows), instance=instance)


def devices_approve(
    instance: ServerInstance,
    code: str,
    *,
    assume_yes: bool,
    confirm: Callable[[str], bool] | None = None,
) -> CommandResult:
    """Approve the device waiting with ``code`` after showing what it is."""
    refusal = run_refusal(
        "devices approve", "approve devices yourself in a terminal or in the WebUI"
    )
    if refusal is not None:
        return CommandResult(ok=False, message=refusal, instance=instance)
    pending = _rpc_call(instance, "devices.pending", {"code": code})
    if not pending.ok:
        return pending.to_command_result()
    details = _pending_text(pending.data)
    if not assume_yes:
        ask = confirm or _ask
        if not sys.stdin.isatty() and confirm is None:
            return CommandResult(
                ok=False,
                message=f"{details}\nConfirm with --yes to approve without a terminal prompt",
                instance=instance,
            )
        if not ask(f"{details}\nApprove this device? [y/N] "):
            return CommandResult(ok=True, message="Not approved.", instance=instance)
    approved = _rpc_call(instance, "devices.approve", {"code": code})
    if not approved.ok:
        return approved.to_command_result()
    return CommandResult(
        ok=True,
        message=f"Approved {value_text(approved.data.get('name'))}; the device signs in now.",
        instance=instance,
    )


def devices_deny(instance: ServerInstance, code: str) -> CommandResult:
    denied = _rpc_call(instance, "devices.deny", {"code": code})
    if not denied.ok:
        return denied.to_command_result()
    return CommandResult(
        ok=True, message=f"Denied {value_text(denied.data.get('name'))}.", instance=instance
    )


def devices_rename(instance: ServerInstance, device_id: str, name: str) -> CommandResult:
    renamed = _rpc_call(instance, "devices.rename", {"id": device_id, "name": name})
    if not renamed.ok:
        return renamed.to_command_result()
    device = renamed.data.get("device")
    return CommandResult(
        ok=True,
        message=_device_row(device) if isinstance(device, Mapping) else f"renamed {device_id}",
        instance=instance,
    )


def devices_revoke(instance: ServerInstance, device_id: str) -> CommandResult:
    revoked = _rpc_call(instance, "devices.revoke", {"id": device_id})
    if not revoked.ok:
        return revoked.to_command_result()
    return CommandResult(
        ok=True,
        message=f"Revoked {device_id}; its open connections are closed.",
        instance=instance,
    )


def _device_row(device: Mapping[str, Any]) -> str:
    kind = device.get("kind")
    return record_fields(
        [
            value_text(device.get("id")),
            f"name={value_text(device.get('name'))}",
            f"kind={_KIND_LABELS.get(kind, value_text(kind)) if isinstance(kind, str) else '-'}",
            f"last_used={value_text(device.get('last_used_at'))}",
            f"last_client={value_text(device.get('last_client'))}",
            f"created={value_text(device.get('created_at'))}",
            *(["current"] if device.get("current") else []),
        ]
    )


def _pending_text(pending: Mapping[str, Any]) -> str:
    kind = pending.get("kind")
    return "\n".join(
        [
            f"Device: {value_text(pending.get('name'))}",
            f"Kind: {_KIND_LABELS.get(kind, value_text(kind)) if isinstance(kind, str) else '-'}",
            f"Requested from: {value_text(pending.get('client'))}",
            f"Requested at: {value_text(pending.get('requested_at'))}",
            f"Code: {value_text(pending.get('code'))}",
        ]
    )


def _ask(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError, KeyboardInterrupt:
        return False
    return answer.strip().casefold() in {"y", "yes"}
