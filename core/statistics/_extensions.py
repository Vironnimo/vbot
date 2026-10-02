"""Extension-owned Session attribution: the reserved actor key and participant labels.

Extension participant Sessions feed every ordinary report section under one
reserved actor key per owner (``extension:<name>``) instead of their synthetic
participant Agent ids; the ``extensions`` section reports them per owner,
group and participant.
"""

from __future__ import annotations

from dataclasses import dataclass

# Agent ids and ``agent@project`` addresses never contain ``:``, so this actor
# key cannot collide with an Agent in per-agent report rows.
EXTENSION_ACTOR_PREFIX = "extension:"


def extension_actor_key(owner_name: str) -> str:
    """Return the per-agent report key attributing one owner's Sessions."""
    return f"{EXTENSION_ACTOR_PREFIX}{owner_name}"


@dataclass(frozen=True)
class ExtensionSliceKey:
    """One owner-managed participant Session and its display labels."""

    owner_name: str
    group_id: str
    group_title: str | None
    participant_id: str
    participant_name: str | None
    model: str | None
    session_id: str
    created_at: str | None
