"""The Agent's view of the Board: recipient resolution, corrections, and readable results.

Everything here is pure. The Tool handlers own Store access, receipts, and wakes;
these functions turn Store values into the text an Agent reads and explain any
correction with the exact next call. Stored posts keep participant IDs; names
and the "all" ping exist only at this Tool boundary.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from core.tools._call_vocabulary import spelling

from . import agent_text as text

Json = dict[str, Any]

# Recipients that mean every other participant, compared by spelling.
_EVERYONE = frozenset({"all", "everyone", "everybody", "here", "channel", "team", "group"})
# Recipients that mean the user, who is not a participant and reads the whole Board.
_USER = frozenset({"user", "theuser", "human", "operator", "owner"})
_EXCERPT_CHARS = 80
_CLOSE_MATCH = 0.85


def call_text(arguments: Mapping[str, Any]) -> str:
    """Return a copyable JSON argument object."""

    return json.dumps(dict(arguments), ensure_ascii=False)


def discussion_label(discussion_id: str, title: str | None, main_id: str) -> str:
    if discussion_id == main_id:
        return text.BOARD_MAIN_LABEL.format(discussion_id=discussion_id)
    return text.BOARD_DISCUSSION_LABEL.format(title=title or "", discussion_id=discussion_id)


def header_discussion(discussion_id: str, title: str | None, main_id: str) -> str:
    if discussion_id == main_id:
        return text.POST_HEADER_MAIN.format(discussion_id=discussion_id)
    return text.POST_HEADER_DISCUSSION.format(title=title or "", discussion_id=discussion_id)


def spoken_list(items: Sequence[str]) -> str:
    """Join names as a sentence does: "A", "A and B", "A, B and C"."""

    if len(items) < 3:
        return text.LIST_AND.join(items)
    return ", ".join(items[:-1]) + text.LIST_AND + items[-1]


def excerpt(value: str) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= _EXCERPT_CHARS else flat[: _EXCERPT_CHARS - 3].rstrip() + "..."


@dataclass(frozen=True)
class Roster:
    """Participants of one group as the Agent addresses them."""

    self_id: str
    names: Mapping[str, str]

    @classmethod
    def of(cls, swarm: Json, self_id: str) -> Roster:
        return cls(
            self_id,
            {
                participant["id"]: participant["display_name"]
                for participant in swarm["participants"]
            },
        )

    def name(self, participant_id: str) -> str:
        return self.names.get(participant_id, participant_id)

    def ordered(self, participant_ids: Iterable[str]) -> list[str]:
        """Return IDs in roster order, so the Agent reads names in a stable order."""

        position = {participant_id: index for index, participant_id in enumerate(self.names)}
        return sorted(participant_ids, key=lambda value: position.get(value, len(position)))

    def listing(self) -> str:
        return ", ".join(
            f"{name} ({participant_id}{', you' if participant_id == self.self_id else ''})"
            for participant_id, name in self.names.items()
        )


@dataclass
class Recipients:
    """Resolved recipient IDs, with notes and the values no participant matches."""

    ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def resolve_recipients(values: Sequence[str], roster: Roster) -> Recipients:
    """Resolve exact IDs, exact names, "all", and the user; never guess a participant."""

    result = Recipients()
    by_name = {spelling(name): participant_id for participant_id, name in roster.names.items()}
    user_mentioned = False
    for raw in values:
        value = raw.strip().strip("\"'`[]<>").lstrip("@#").strip()
        key = spelling(value)
        # An ID prefix around an exact name ("prt_ada") still names that participant.
        named = value[4:] if value[:4].lower() == "prt_" else value
        if value in roster.names:
            resolved = [value]
        elif spelling(named) in by_name:
            resolved = [by_name[spelling(named)]]
        elif value == "*" or key in _EVERYONE:
            resolved = [pid for pid in roster.names if pid != roster.self_id]
        elif key in _USER:
            user_mentioned = True
            continue
        else:
            result.unknown.append(raw)
            continue
        for participant_id in resolved:
            if participant_id not in result.ids:
                result.ids.append(participant_id)
    if user_mentioned:
        result.notes.append(text.USER_RECIPIENT)
    return result


def unknown_recipients_message(
    values: Sequence[str], recipients: Recipients, roster: Roster
) -> str:
    """Explain unknown recipients with any unique close ID, the roster, and the retry."""

    suggestions: dict[str, str] = {}
    ids = list(roster.names)
    for value in recipients.unknown:
        close = get_close_matches(value.strip(), ids, n=2, cutoff=_CLOSE_MATCH)
        if len(close) == 1:
            suggestions[value] = close[0]
    quoted = ", ".join(json.dumps(value, ensure_ascii=False) for value in recipients.unknown)
    parts = [
        text.RECIPIENT_UNKNOWN.format(
            values=quoted, verb="is" if len(recipients.unknown) == 1 else "are"
        )
    ]
    if suggestions:
        parts.append(
            text.RECIPIENT_SUGGESTIONS.format(
                suggestions=", ".join(
                    f"{roster.name(pid)} ({pid}) for {json.dumps(value, ensure_ascii=False)}"
                    for value, pid in suggestions.items()
                )
            )
        )
    parts.append(text.RECIPIENT_ROSTER.format(roster=roster.listing()))
    if len(suggestions) == len(recipients.unknown):
        corrected = [suggestions.get(value, value) for value in values]
        parts.append(text.RECIPIENT_RETRY.format(corrected=json.dumps(corrected)))
    else:
        parts.append(text.RECIPIENT_CHOOSE)
    return " ".join(parts)


def post_suggestion(candidate: Json, main_id: str) -> str:
    return text.POST_SUGGESTION.format(
        post_id=candidate["id"],
        author=candidate["author_name"],
        discussion=discussion_label(
            candidate["discussion_id"], candidate["discussion_title"], main_id
        ),
        excerpt=excerpt(candidate["text"]),
    )


def discussion_choices(discussions: Iterable[Json], main_id: str) -> str:
    return text.DISCUSSION_CHOICES.format(
        discussions=", ".join(
            discussion_label(row["id"], row["title"], main_id) for row in discussions
        )
    )


def close_discussion(value: str, discussions: Sequence[Json]) -> Json | None:
    close = get_close_matches(
        value.strip(), [row["id"] for row in discussions], n=2, cutoff=_CLOSE_MATCH
    )
    return next(row for row in discussions if row["id"] == close[0]) if len(close) == 1 else None


def post_block(post: Json, roster: Roster, main_id: str, *, with_discussion: bool) -> str:
    """Render one post as a header line and its verbatim text."""

    details = []
    if with_discussion:
        details.append(
            text.POST_HEADER_IN.format(
                discussion=header_discussion(
                    post["discussion_id"], post.get("discussion_title"), main_id
                )
            )
        )
    if post.get("reply_to"):
        details.append(text.POST_HEADER_REPLY.format(post_id=post["reply_to"]))
    pinged = [
        text.POST_YOU if recipient == roster.self_id else roster.name(recipient)
        for recipient in roster.ordered(post.get("recipients") or [])
    ]
    if pinged:
        details.append(text.POST_HEADER_PINGED.format(names=spoken_list(pinged)))
    header = f"[{post['id']}] {post['author']['name']}"
    if details:
        header += f" ({'; '.join(details)})"
    return f"{header}:\n{post['text']}"


def posts_text(
    posts: Iterable[Json], roster: Roster, main_id: str, *, with_discussion: bool = False
) -> str:
    return "\n\n".join(
        post_block(post, roster, main_id, with_discussion=with_discussion) for post in posts
    )


def discussions_text(entries: Iterable[Json], main_id: str) -> str:
    lines = [text.BOARD_DISCUSSIONS_HEADER]
    for entry in entries:
        details = []
        if entry["id"] == main_id:
            details.append(text.BOARD_MAIN_DETAIL)
        details.append(
            text.BOARD_JOINED_DETAIL if entry["joined"] else text.BOARD_NOT_JOINED_DETAIL
        )
        details.append(text.BOARD_MEMBERS_DETAIL.format(count=entry["member_count"]))
        if entry["pending_count"]:
            details.append(text.BOARD_PENDING_DETAIL.format(count=entry["pending_count"]))
        lines.append(
            text.BOARD_DISCUSSION_LINE.format(
                discussion_id=entry["id"], title=entry["title"], details=", ".join(details)
            )
        )
    return "\n".join(lines)


def queued_text(routes: Mapping[str, int]) -> str:
    count = sum(routes.values())
    if not count:
        return text.BOARD_QUEUED_NONE
    pinged = text.BOARD_PINGED.format(count=routes["ping"]) if routes.get("ping") else ""
    participants = text.BOARD_PARTICIPANTS["one" if count == 1 else "many"].format(count=count)
    return text.BOARD_QUEUED.format(participants=participants, pinged=pinged)


def with_notes(data: Json, notes: Sequence[str]) -> Json:
    if notes:
        data["note"] = " ".join(dict.fromkeys(notes))
    return data


__all__ = [
    "Recipients",
    "Roster",
    "call_text",
    "close_discussion",
    "discussion_choices",
    "discussion_label",
    "discussions_text",
    "excerpt",
    "post_block",
    "post_suggestion",
    "posts_text",
    "queued_text",
    "resolve_recipients",
    "unknown_recipients_message",
    "with_notes",
]
