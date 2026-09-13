"""Durable, per-Tool-call update handoff capabilities for Bash."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.utils.atomic import atomic_write_bytes

HANDOFF_ENV = "VBOT_UPDATE_HANDOFF"
HANDOFF_SCHEMA = 1
HANDOFF_DIRECTORY = Path("runtime") / "update-handoffs"
HANDOFF_FILE_MODE = 0o600
_TICKET_BYTES = 20
_MAX_TICKET_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class UpdateHandoffTicket:
    ticket_id: str
    run_id: str
    tool_call_id: str
    agent_id: str
    project_id: str | None
    session_id: str
    data_dir: str
    acknowledged: bool
    path: Path


def create_update_handoff_ticket(
    data_dir: str | Path,
    *,
    run_id: str,
    tool_call_id: str,
    agent_id: str,
    project_id: str | None,
    session_id: str,
) -> UpdateHandoffTicket:
    """Create one opaque capability scoped to an exact Bash Tool call."""
    root = Path(data_dir).expanduser().resolve()
    ticket_id = secrets.token_urlsafe(_TICKET_BYTES)
    path = _ticket_path(root, ticket_id)
    ticket = UpdateHandoffTicket(
        ticket_id=ticket_id,
        run_id=_required(run_id, "run_id"),
        tool_call_id=_required(tool_call_id, "tool_call_id"),
        agent_id=_required(agent_id, "agent_id"),
        project_id=_optional(project_id, "project_id"),
        session_id=_required(session_id, "session_id"),
        data_dir=str(root),
        acknowledged=False,
        path=path,
    )
    atomic_write_bytes(path, _encode(ticket), mode=HANDOFF_FILE_MODE)
    return ticket


def acknowledge_update_handoff_ticket(ticket: UpdateHandoffTicket) -> None:
    """Mark the ticket acknowledged after its complete Tool batch is durable."""
    current = read_update_handoff_ticket(ticket.path.parents[2], ticket.ticket_id)
    if current != ticket:
        raise ValueError("Update handoff ticket changed before acknowledgement")
    atomic_write_bytes(
        ticket.path,
        _encode(replace(ticket, acknowledged=True)),
        mode=HANDOFF_FILE_MODE,
    )


def read_update_handoff_ticket(data_dir: str | Path, ticket_id: str) -> UpdateHandoffTicket:
    """Read one ticket by opaque id from the exact server data directory."""
    root = Path(data_dir).expanduser().resolve()
    path = _ticket_path(root, ticket_id)
    try:
        if path.stat().st_size > _MAX_TICKET_BYTES:
            raise ValueError("Update handoff ticket is too large")
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Update handoff ticket is unavailable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != HANDOFF_SCHEMA:
        raise ValueError("Update handoff ticket schema is invalid")
    expected = {
        "schema",
        "ticket_id",
        "run_id",
        "tool_call_id",
        "agent_id",
        "project_id",
        "session_id",
        "data_dir",
        "acknowledged",
    }
    if set(payload) != expected or payload.get("ticket_id") != ticket_id:
        raise ValueError("Update handoff ticket content is invalid")
    stored_root = payload.get("data_dir")
    if not isinstance(stored_root, str) or Path(stored_root).resolve() != root:
        raise ValueError("Update handoff ticket belongs to another data directory")
    acknowledged = payload.get("acknowledged")
    if not isinstance(acknowledged, bool):
        raise ValueError("Update handoff acknowledgement is invalid")
    return UpdateHandoffTicket(
        ticket_id=ticket_id,
        run_id=_required(payload.get("run_id"), "run_id"),
        tool_call_id=_required(payload.get("tool_call_id"), "tool_call_id"),
        agent_id=_required(payload.get("agent_id"), "agent_id"),
        project_id=_optional(payload.get("project_id"), "project_id"),
        session_id=_required(payload.get("session_id"), "session_id"),
        data_dir=str(root),
        acknowledged=acknowledged,
        path=path,
    )


def read_handoff_ticket(data_dir: Path, ticket_id: str) -> dict[str, Any]:
    """Return the validated ticket as a worker-friendly JSON mapping."""
    ticket = read_update_handoff_ticket(data_dir, ticket_id)
    return {
        "schema": HANDOFF_SCHEMA,
        "ticket_id": ticket.ticket_id,
        "run_id": ticket.run_id,
        "tool_call_id": ticket.tool_call_id,
        "agent_id": ticket.agent_id,
        "project_id": ticket.project_id,
        "session_id": ticket.session_id,
        "data_dir": ticket.data_dir,
        "acknowledged": ticket.acknowledged,
    }


def ticket_id_from_path(data_dir: str | Path, path: str | Path) -> str:
    """Resolve an environment path only when it names an exact ticket child."""
    root = Path(data_dir).expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    directory = (root / HANDOFF_DIRECTORY).resolve()
    if candidate.parent != directory or candidate.suffix != ".json":
        raise ValueError("Update handoff path is outside the server ticket directory")
    ticket_id = candidate.stem
    _ticket_path(root, ticket_id)
    return ticket_id


def _ticket_path(root: Path, ticket_id: str) -> Path:
    if (
        not isinstance(ticket_id, str)
        or not ticket_id
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in ticket_id
        )
    ):
        raise ValueError("Update handoff ticket id is invalid")
    path = (root / HANDOFF_DIRECTORY / f"{ticket_id}.json").resolve()
    if path.parent != (root / HANDOFF_DIRECTORY).resolve():
        raise ValueError("Update handoff ticket path is invalid")
    return path


def _required(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Update handoff {field} must be a non-empty string")
    return value


def _optional(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required(value, field)


def _encode(ticket: UpdateHandoffTicket) -> bytes:
    payload = {
        "schema": HANDOFF_SCHEMA,
        "ticket_id": ticket.ticket_id,
        "run_id": ticket.run_id,
        "tool_call_id": ticket.tool_call_id,
        "agent_id": ticket.agent_id,
        "project_id": ticket.project_id,
        "session_id": ticket.session_id,
        "data_dir": ticket.data_dir,
        "acknowledged": ticket.acknowledged,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


__all__ = [
    "HANDOFF_ENV",
    "UpdateHandoffTicket",
    "acknowledge_update_handoff_ticket",
    "create_update_handoff_ticket",
    "read_handoff_ticket",
    "read_update_handoff_ticket",
    "ticket_id_from_path",
]
