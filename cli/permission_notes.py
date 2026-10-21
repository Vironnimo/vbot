"""What the server made of Permission Rules a CLI command just saved.

The server normalizes a rule set on write and rewrites other harnesses'
spellings (``Bash(git push:*)`` is saved as ``Shell(git push *)``) without
saying so in the write's answer, and per-part resolution keeps an own
``write: anywhere`` in the work area while the read that applies stays there.
``permissions.validate``, the check the WebUI editors use, reports both; the
commands that save rule sets print them as ``Note:`` lines after their first
line.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cli.rpc_client import rpc_call
from cli.server_management import ServerInstance


def permission_notes(
    instance: ServerInstance,
    rules: object,
    *,
    agent_id: str | None = None,
) -> list[str]:
    """The ``Note:`` lines for ``rules`` as saved for ``agent_id``.

    ``agent_id`` is the Agent (``agent@project`` for a Team member's override)
    whose own rules these are; without it they stand alone (the shared
    default). Nothing to say, or no rule set, gives no lines.
    """
    if not isinstance(rules, Mapping):
        return []
    # Only rule strings get rewritten, and only a write without an own read is
    # narrowed: a preset alone has nothing to say and costs no call.
    if not (rules.get("allow") or rules.get("deny") or _write_without_read(rules)):
        return []
    params: dict[str, Any] = {"permission_rules": dict(rules)}
    if agent_id is not None:
        params["agent_id"] = agent_id
    payload = rpc_call(instance, "permissions.validate", params)
    if not payload.ok:
        return [
            "Note: the rules were saved, but checking them for rewritten rules failed "
            f"({payload.message.rstrip('. ')})."
        ]
    notes = [f"Note: {note}" for note in payload.data.get("notes") or [] if isinstance(note, str)]
    narrowed = _narrowed_write(rules, payload.data.get("bases"))
    if narrowed is not None:
        notes.append(narrowed)
    return notes


def with_notes(message: str, notes: list[str]) -> str:
    """``message`` with ``notes`` after its first line, which says what happened."""
    if not notes:
        return message
    first, newline, rest = message.partition("\n")
    return "\n".join([first, *notes, *([rest] if newline else [])])


def _write_without_read(rules: Mapping[str, Any]) -> bool:
    return rules.get("write") is not None and rules.get("read") is None


def _narrowed_write(rules: Mapping[str, Any], bases: object) -> str | None:
    """The note for an own write the inherited read narrows, as the editors say it."""
    own_write = rules.get("write")
    if not isinstance(own_write, str) or not _write_without_read(rules):
        return None
    if not isinstance(bases, dict) or not isinstance(bases.get("write"), dict):
        return None
    write = bases["write"]
    read = bases.get("read")
    read_value = read.get("value") if isinstance(read, dict) else None
    narrowing = write.get("narrowed_by_read")
    if own_write not in (write.get("excluded") or []) or not isinstance(narrowing, dict):
        return None
    effective = (narrowing.get(read_value) or {}).get(own_write)
    if not isinstance(effective, str):
        return None
    return (
        f"Note: write {own_write} applies as {effective}, because the inherited read is "
        f"{read_value} and an agent can change only files it can read. To change files "
        f"anywhere, also set read to anywhere."
    )
