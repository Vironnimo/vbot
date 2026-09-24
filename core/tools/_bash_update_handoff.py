"""Update handoff capabilities for Bash calls that run ``vbot update``.

Every Bash call with a persistence boundary receives an unguessable in-memory
token through ``VBOT_UPDATE_HANDOFF``. Only a packaged ``vbot update`` or
``vbot customize activate`` claims it: the server then writes one durable,
opaque ticket that the independent updater and the private application RPCs
validate. Tickets and continuation receipts matter for one update operation
only, so server startup removes those past ``UPDATE_HANDOFF_FILE_RETENTION``.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from core.utils.atomic import atomic_write_bytes
from core.utils.logging import get_logger

HANDOFF_ENV = "VBOT_UPDATE_HANDOFF"
HANDOFF_SCHEMA = 1
HANDOFF_DIRECTORY = Path("runtime") / "update-handoffs"
CONTINUATION_DIRECTORY = Path("runtime") / "update-continuations"
HANDOFF_FILE_MODE = 0o600
# The updater waits at most 300 seconds for acknowledgement; a week leaves every
# plausible operation, recovery and inspection window intact.
UPDATE_HANDOFF_FILE_RETENTION = timedelta(days=7)
_TOKEN_BYTES = 32
_MAX_TOKEN_CHARS = 128
_TICKET_BYTES = 20
_MAX_TICKET_BYTES = 16_384
_ID_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

_LOGGER = get_logger("tools.bash")


class UpdateHandoffUnavailableError(ValueError):
    """A presented token names no Bash call whose command may still run here."""


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


class UpdateHandoffGrant:
    """One Bash call's in-memory handoff capability.

    The Bash handler exports ``token`` to the command, calls ``acknowledge``
    after the Tool Result enters Session history, and calls ``release`` once no
    process of the call can still run. Acknowledgement keeps working after
    release so a ticket claimed by a finished command is still acknowledged.
    """

    __slots__ = (
        "_acknowledged",
        "_key",
        "_registry",
        "_ticket",
        "_token",
        "agent_id",
        "project_id",
        "run_id",
        "session_id",
        "tool_call_id",
    )

    def __init__(
        self,
        registry: UpdateHandoffs,
        token: str,
        *,
        run_id: str,
        tool_call_id: str,
        agent_id: str,
        project_id: str | None,
        session_id: str,
    ) -> None:
        self._registry = registry
        self._token = token
        self._key = _token_key(token)
        self._acknowledged = False
        self._ticket: UpdateHandoffTicket | None = None
        self.run_id = run_id
        self.tool_call_id = tool_call_id
        self.agent_id = agent_id
        self.project_id = project_id
        self.session_id = session_id

    @property
    def token(self) -> str:
        return self._token

    def acknowledge(self) -> None:
        """Record the persisted Tool Result and acknowledge a claimed ticket durably."""
        if self._acknowledged:
            return
        self._acknowledged = True
        ticket = self._ticket
        if ticket is None or ticket.acknowledged:
            return
        try:
            self._ticket = acknowledge_update_handoff_ticket(ticket)
        except (OSError, ValueError) as error:
            # Paths and ids are capabilities; the updater reports the timeout.
            _LOGGER.warning(
                "Update handoff acknowledgement failed (%s); the requested update "
                "will not continue its Session",
                type(error).__name__,
            )

    def release(self) -> None:
        """Withdraw the capability; later claims of this token are rejected."""
        self._registry._release(self)

    def _claim(self, root: Path) -> UpdateHandoffTicket:
        if self._ticket is None:
            self._ticket = _create_ticket(
                root,
                run_id=self.run_id,
                tool_call_id=self.tool_call_id,
                agent_id=self.agent_id,
                project_id=self.project_id,
                session_id=self.session_id,
                acknowledged=self._acknowledged,
            )
        return self._ticket


class UpdateHandoffs:
    """Live Bash update-handoff capabilities of this server process.

    State is in memory and confined to the event loop: a server restart forgets
    every token, while claimed tickets stay durable for the updater.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._root = Path(data_dir).expanduser().resolve()
        self._grants: dict[bytes, UpdateHandoffGrant] = {}

    def issue(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        agent_id: str,
        project_id: str | None,
        session_id: str,
    ) -> UpdateHandoffGrant:
        """Create one capability scoped to an exact Bash Tool call."""
        grant = UpdateHandoffGrant(
            self,
            secrets.token_urlsafe(_TOKEN_BYTES),
            run_id=_required(run_id, "run_id"),
            tool_call_id=_required(tool_call_id, "tool_call_id"),
            agent_id=_required(agent_id, "agent_id"),
            project_id=_optional(project_id, "project_id"),
            session_id=_required(session_id, "session_id"),
        )
        self._grants[grant._key] = grant
        return grant

    def mint(self, token: str) -> UpdateHandoffTicket:
        """Return the durable ticket for a live token, writing it on the first claim."""
        grant = self._grants.get(_token_key(token)) if _plausible_token(token) else None
        if grant is None:
            raise UpdateHandoffUnavailableError(
                "update handoff is not active on this server; it ends when the command "
                "of its Tool call exits or the server restarts"
            )
        return grant._claim(self._root)

    def remove_expired_files(self, *, now: float | None = None) -> None:
        """Delete tickets and continuation receipts older than their retention."""
        cutoff = (time.time() if now is None else now) - (
            UPDATE_HANDOFF_FILE_RETENTION.total_seconds()
        )
        tickets, ticket_failures = _remove_files_older_than(self._root / HANDOFF_DIRECTORY, cutoff)
        receipts, receipt_failures = _remove_files_older_than(
            self._root / CONTINUATION_DIRECTORY, cutoff
        )
        if tickets or receipts:
            _LOGGER.info(
                "Removed expired update handoff files: tickets=%d continuation_receipts=%d",
                tickets,
                receipts,
            )
        if ticket_failures or receipt_failures:
            _LOGGER.warning(
                "Could not remove expired update handoff files: tickets=%d "
                "continuation_receipts=%d",
                ticket_failures,
                receipt_failures,
            )

    def _release(self, grant: UpdateHandoffGrant) -> None:
        if self._grants.get(grant._key) is grant:
            del self._grants[grant._key]


def acknowledge_update_handoff_ticket(ticket: UpdateHandoffTicket) -> UpdateHandoffTicket:
    """Mark the ticket acknowledged after its complete Tool batch is durable."""
    current = read_update_handoff_ticket(ticket.path.parents[2], ticket.ticket_id)
    if current != ticket:
        raise ValueError("Update handoff ticket changed before acknowledgement")
    acknowledged = replace(ticket, acknowledged=True)
    atomic_write_bytes(ticket.path, _encode(acknowledged), mode=HANDOFF_FILE_MODE)
    return acknowledged


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
    """Resolve a saved ticket path only when it names an exact ticket child."""
    root = Path(data_dir).expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    directory = (root / HANDOFF_DIRECTORY).resolve()
    if candidate.parent != directory or candidate.suffix != ".json":
        raise ValueError("Update handoff path is outside the server ticket directory")
    ticket_id = candidate.stem
    _ticket_path(root, ticket_id)
    return ticket_id


def _create_ticket(
    root: Path,
    *,
    run_id: str,
    tool_call_id: str,
    agent_id: str,
    project_id: str | None,
    session_id: str,
    acknowledged: bool,
) -> UpdateHandoffTicket:
    ticket_id = secrets.token_urlsafe(_TICKET_BYTES)
    ticket = UpdateHandoffTicket(
        ticket_id=ticket_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        agent_id=agent_id,
        project_id=project_id,
        session_id=session_id,
        data_dir=str(root),
        acknowledged=acknowledged,
        path=_ticket_path(root, ticket_id),
    )
    atomic_write_bytes(ticket.path, _encode(ticket), mode=HANDOFF_FILE_MODE)
    return ticket


def _remove_files_older_than(directory: Path, cutoff: float) -> tuple[int, int]:
    removed = failed = 0
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return 0, 0
    except OSError:
        return 0, 1
    with entries:
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
                continue
            removed += 1
    return removed, failed


def _token_key(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _plausible_token(token: object) -> bool:
    return (
        isinstance(token, str)
        and 0 < len(token) <= _MAX_TOKEN_CHARS
        and all(character in _ID_CHARACTERS for character in token)
    )


def _ticket_path(root: Path, ticket_id: str) -> Path:
    if (
        not isinstance(ticket_id, str)
        or not ticket_id
        or any(character not in _ID_CHARACTERS for character in ticket_id)
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
    "CONTINUATION_DIRECTORY",
    "HANDOFF_DIRECTORY",
    "HANDOFF_ENV",
    "UPDATE_HANDOFF_FILE_RETENTION",
    "UpdateHandoffGrant",
    "UpdateHandoffTicket",
    "UpdateHandoffUnavailableError",
    "UpdateHandoffs",
    "acknowledge_update_handoff_ticket",
    "read_handoff_ticket",
    "read_update_handoff_ticket",
    "ticket_id_from_path",
]
