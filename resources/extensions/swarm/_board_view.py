"""The Agent's view of the Board: recipient resolution, corrections, and readable results.

Everything here is pure. The Tool handlers own Store access, receipts, and wakes;
these functions turn Store values into the text an Agent reads and explain any
correction with the exact next call. Stored posts keep participant IDs; names
and the "all" recipient exist only at this Tool boundary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from core.tools.call_syntax import spelling
from core.utils.edit_distance import edit_distance

from . import agent_text as text
from ._store_values import discussion_ref, mentioned_participants, post_ref
from ._wake_pacing import ADDRESSED_ROUTES, QUIET_SECONDS

Json = dict[str, Any]

# Recipients that mean every other participant, compared by spelling.
_EVERYONE = frozenset({"all", "everyone", "everybody", "here", "channel", "team", "group"})
# Recipients that mean the user, who is not a participant and reads the whole Board.
_USER = frozenset({"user", "theuser", "human", "operator", "owner"})
_EXCERPT_CHARS = 80
_CLOSE_MATCH = 0.85
# Posts on a read page when the call sends no limit, as SwarmStore.read_posts applies it.
PAGE_LIMIT = 20


def call_text(arguments: Mapping[str, Any]) -> str:
    """Return a copyable JSON argument object."""

    return json.dumps(dict(arguments), ensure_ascii=False)


def discussion_label(sequence: int, title: str | None, *, main: bool) -> str:
    """Name a discussion by the number participants use for it, with its title."""

    if main:
        return text.BOARD_MAIN_LABEL.format(discussion_id=discussion_ref(sequence))
    return text.BOARD_DISCUSSION_LABEL.format(
        title=title or "", discussion_id=discussion_ref(sequence)
    )


def post_discussion_label(post: Json, main_id: str) -> str:
    """Name the discussion of a post that carries its discussion's number and title."""

    return discussion_label(
        post["discussion_sequence"],
        post.get("discussion_title"),
        main=post["discussion_id"] == main_id,
    )


def _header_discussion(post: Json, main_id: str) -> str:
    reference = discussion_ref(post["discussion_sequence"])
    if post["discussion_id"] == main_id:
        return text.POST_HEADER_MAIN.format(discussion_id=reference)
    return text.POST_HEADER_DISCUSSION.format(
        title=post.get("discussion_title") or "", discussion_id=reference
    )


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
            name + (text.STATE_ROSTER_YOU if participant_id == self.self_id else "")
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
    """Explain unknown recipients with any unique close name, the roster, and the retry."""

    suggestions: dict[str, str] = {}
    spelled = {spelling(name): name for name in roster.names.values()}
    for value in recipients.unknown:
        close = {
            *(
                roster.name(match)
                for match in get_close_matches(
                    value.strip(), list(roster.names), cutoff=_CLOSE_MATCH
                )
            ),
            *(
                spelled[match]
                for match in get_close_matches(spelling(value), list(spelled), cutoff=_CLOSE_MATCH)
            ),
        }
        if len(close) == 1:
            suggestions[value] = close.pop()
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
                    f"{name} for {json.dumps(value, ensure_ascii=False)}"
                    for value, name in suggestions.items()
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
        post_id=post_ref(candidate["sequence"]),
        author=candidate["author_name"],
        discussion=post_discussion_label(candidate, main_id),
        excerpt=excerpt(candidate["text"]),
    )


def discussion_choices(discussions: Iterable[Json], main_id: str) -> str:
    return text.DISCUSSION_CHOICES.format(
        discussions=", ".join(
            discussion_label(row["sequence"], row["title"], main=row["id"] == main_id)
            for row in discussions
        )
    )


def close_discussion(value: str, discussions: Sequence[Json]) -> Json | None:
    close = get_close_matches(
        value.strip(), [row["id"] for row in discussions], n=2, cutoff=_CLOSE_MATCH
    )
    return next(row for row in discussions if row["id"] == close[0]) if len(close) == 1 else None


def post_block(
    post: Json,
    roster: Roster,
    main_id: str,
    *,
    with_discussion: bool,
    opening: bool = False,
    page: bool = False,
) -> str:
    """Render one post as a header line and its verbatim text.

    With ``opening``, a long main-discussion post that reached the reader only
    through the main discussion shows its opening and the call to read it all.
    With ``page``, a post the reader has seen shows only what ``page_body`` keeps.
    """

    details = []
    if with_discussion:
        details.append(text.POST_HEADER_IN.format(discussion=_header_discussion(post, main_id)))
    if post.get("reply_sequence") is not None:
        details.append(text.POST_HEADER_REPLY.format(post_id=post_ref(post["reply_sequence"])))
    addressed = [
        text.POST_YOU if recipient == roster.self_id else roster.name(recipient)
        for recipient in roster.ordered(post.get("recipients") or [])
    ]
    if addressed:
        details.append(text.POST_HEADER_TO.format(names=spoken_list(addressed)))
    header = f"[{post_ref(post['sequence'])}] {post['author']['name']}"
    if details:
        header += f" ({'; '.join(details)})"
    body = page_body(post, roster.self_id) if page else _body(post, opening=opening)
    return f"{header}:\n{body}"


def delivered_chars(post: Json) -> int:
    """The length of the text a delivery shows for ``post``; delivery batches count it."""

    return len(_body(post, opening=True))


def _body(post: Json, *, opening: bool) -> str:
    if opening and shortened(post):
        return _opening_body(post)
    return str(post["text"])


def _opening_body(post: Json) -> str:
    body = str(post["text"])
    start = _opening(body)
    return f"{start} ...\n" + text.POST_SHORTENED.format(
        count=len(body) - len(start), call=_read_call(post)
    )


def _read_call(post: Json) -> str:
    return call_text({"action": "read", "message_id": post_ref(post["sequence"])})


def seen(post: Json, reader_id: str) -> bool:
    """Whether the reader has seen the post's text: its own post, or one delivered to it.

    A post outside the reader's audience (``pending`` absent) may never have reached it.
    """

    author = post["author"]
    own = author["kind"] == "participant" and author["id"] == reader_id
    return own or post.get("pending") is False


def page_body(post: Json, reader_id: str) -> str:
    """The text a read page shows for ``post``.

    A post the reader has seen shows only its length and the call that reads it,
    also when it is long: a delivery already showed at least its opening. Every
    other post shows whole, so a page repeats little.
    """

    if not seen(post, reader_id):
        return str(post["text"])
    own = post["author"]["kind"] == "participant" and post["author"]["id"] == reader_id
    template = text.POST_SEEN_OPENING if shortened(post) and not own else text.POST_SEEN
    return template.format(count=len(post["text"]), call=_read_call(post))


def page_chars(post: Json, reader_id: str) -> int:
    """The length of the text a read page shows for ``post``; page budgets count it."""

    return len(page_body(post, reader_id))


def shortened(post: Json) -> bool:
    """Whether a delivered post reaches its reader as its opening only."""

    return (
        post.get("route_class") == "main"
        and post["author"]["kind"] != "user"
        and len(post["text"]) > text.FULL_POST_CHARS
    )


def _opening(value: str) -> str:
    """Return about the first ``OPENING_CHARS`` characters, ending at a word boundary."""

    cut = value[: text.OPENING_CHARS]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > text.OPENING_CHARS // 2:
        cut = cut[:boundary]
    return cut.rstrip()


def page_text(posts: Iterable[Json], roster: Roster, main_id: str) -> str:
    """Render the posts of a read page; posts the reader has seen show compactly."""

    return "\n\n".join(
        post_block(post, roster, main_id, with_discussion=False, page=True) for post in posts
    )


def messages_text(posts: Iterable[Json], roster: Roster, main_id: str) -> str:
    """Render delivered messages in order, under a heading per run of one discussion."""

    blocks: list[str] = []
    current = None
    for post in posts:
        if post["discussion_id"] != current:
            current = post["discussion_id"]
            blocks.append(text.MESSAGES_IN.format(discussion=post_discussion_label(post, main_id)))
        blocks.append(post_block(post, roster, main_id, with_discussion=False, opening=True))
    return "\n\n".join(blocks)


_ROUTES = ("main", "discussion", "ping")
# Wake pacing holds posts on other routes for up to the longest Quiet period.
_QUIET_MINUTES = round(QUIET_SECONDS[-1] / 60)


def automatic_delivery(settings: Mapping[str, Any]) -> bool:
    """Whether every route delivers while the participant runs, so pending posts need no call."""

    return all(settings[route]["mode"] == "all" for route in _ROUTES)


def delivery_text(
    prefix: str,
    posts: Sequence[Json],
    remaining: int,
    roster: Roster,
    main_id: str,
    *,
    inbox: bool,
    automatic: bool,
    skipped: Sequence[Json] = (),
) -> str:
    """Render one automatic delivery: guidance, skipped posts, messages, and what remains.

    With ``automatic``, every route delivers while the participant runs, so the
    rest follows without a call. ``skipped`` holds the store's per-discussion
    summary of older posts the batch passed over.
    """

    parts = [prefix, _skipped_text(skipped, main_id), messages_text(posts, roster, main_id)]
    if remaining:
        key = (
            ("automatic_one" if remaining == 1 else "automatic_many")
            if automatic
            else "inbox"
            if inbox
            else "no_inbox"
        )
        parts.append(text.DELIVERY_MORE[key].format(count=remaining))
    return "\n\n".join(part for part in parts if part)


def _skipped_text(groups: Sequence[Json], main_id: str) -> str:
    """Say which older posts a delivery skipped, with the read call for each discussion."""

    if not groups:
        return ""
    count = sum(group["count"] for group in groups)
    if len(groups) > 1:
        reads = [
            text.DELIVERY_SKIPPED_READ.format(
                call=_skipped_read(group, main_id),
                discussion=post_discussion_label(group, main_id),
            )
            for group in groups
        ]
        return text.DELIVERY_SKIPPED["discussions"].format(count=count, reads=spoken_list(reads))
    (group,) = groups
    return text.DELIVERY_SKIPPED["one" if count == 1 else "many"].format(
        count=count,
        discussion=post_discussion_label(group, main_id),
        call=_skipped_read(group, main_id),
    )


def _skipped_read(group: Json, main_id: str) -> str:
    """The read call that shows a discussion's skipped posts, newest page first."""

    arguments: Json = {"action": "read"}
    if group["before"] is not None:
        arguments["before"] = post_ref(group["before"])
    elif group["discussion_id"] != main_id:
        arguments["discussion_id"] = discussion_ref(group["discussion_sequence"])
    # A shorter page holds exactly the skipped stretch; a longer one pages on.
    if group["span"] < PAGE_LIMIT:
        arguments["limit"] = group["span"]
    return call_text(arguments)


def wake_text(routes: Sequence[str]) -> str:
    """Say which posts start a Run of the idle participant, and how soon."""

    if not routes:
        return text.STATE_NO_WAKE
    paced = [route for route in routes if route not in ADDRESSED_ROUTES]
    if not paced:
        return text.STATE_WAKE.format(routes=_routes(routes, capital=True))
    template = (
        text.STATE_WAKE_ADDRESSED if ADDRESSED_ROUTES & set(routes) else text.STATE_WAKE_PACED
    )
    return template.format(routes=_routes(paced, capital=False), minutes=_QUIET_MINUTES)


def news_text(
    posts: Sequence[Json], self_id: str, settings: Mapping[str, Any], *, newest: bool
) -> str | None:
    """Say which shown posts had not reached the reader yet, or None when unknown.

    A post is new while it is pending for the reader. The reader's own posts and
    delivered ones are not; a post outside its audience (``pending`` absent) may be
    either, so a page holding one makes no claim that nothing is new. When the
    newest page holds nothing new, the reader is polling: ending the reply is how to
    wait, when a post can start the next Run.
    """

    new = [post_ref(post["sequence"]) for post in posts if post.get("pending")]
    if new:
        if len(new) == len(posts) > 1:
            return text.BOARD_NEW["all"].format(count=len(new))
        return text.BOARD_NEW["one" if len(new) == 1 else "many"].format(posts=spoken_list(new))
    known = all("pending" in post or post["author"]["id"] == self_id for post in posts)
    if not (posts and newest and known):
        return None
    routes = [route for route in _ROUTES if settings[route]["wake_idle"]]
    if not routes:
        waiting = text.STATE_NO_WAKE
    elif ADDRESSED_ROUTES & set(routes):
        waiting = text.BOARD_WAIT["addressed"]
    else:
        waiting = text.BOARD_WAIT["paced"].format(
            routes=_routes(routes, capital=False), minutes=_QUIET_MINUTES
        )
    return f"{text.BOARD_NOTHING_NEW} {waiting}"


def status_data(status: Json, roster: Roster, *, inbox: bool) -> Json:
    """Render participant status as readable fields and a roster listing.

    The roster marks the reader, so its own state needs no field. When every
    route delivers while running, the pending line says so instead of a delivery
    field; state totals appear only beside a partial roster.
    """

    count = status["pending_count"]
    amount = "none" if not count else "one" if count == 1 else "many"
    automatic = set(status["delivery"]) == {"automatic"}
    if automatic:
        key = f"{amount}_automatic"
    elif not count or inbox:
        key = amount
    else:
        key = f"{amount}_no_inbox"
    data: Json = {"pending": text.STATE_PENDING[key].format(count=count)}
    if not count and status["wake_on_messages"]:
        # Nothing to receive: the reader is polling, and ending the reply is how to wait.
        data["pending"] += " " + text.STATE_WAIT
    if not automatic:
        data["delivery"] = " ".join(
            text.STATE_DELIVERY[
                mode if inbox or mode != "on_request" else "on_request_no_inbox"
            ].format(routes=_routes(routes, capital=True))
            for mode, routes in status["delivery"].items()
        )
    data["wake"] = wake_text(status["wake_on_messages"])
    totals = status["state_totals"]
    if len(status["roster"]) < sum(totals.values()):
        data["participants"] = text.STATE_PARTICIPANTS.format(
            count=sum(totals.values()),
            totals=", ".join(f"{number} {state}" for state, number in sorted(totals.items())),
        )
    lines = [text.STATE_ROSTER_HEADER]
    lines.extend(
        text.STATE_ROSTER_LINE.format(
            name=row["name"],
            you=text.STATE_ROSTER_YOU if row["id"] == roster.self_id else "",
            state=row["state"],
        )
        for row in status["roster"]
    )
    data["content"] = "\n".join(lines)
    return data


def _routes(routes: Sequence[str], *, capital: bool) -> str:
    spoken = spoken_list([text.STATE_ROUTES[route] for route in routes])
    return spoken[:1].upper() + spoken[1:] if capital else spoken


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
                discussion_id=discussion_ref(entry["sequence"]),
                title=entry["title"],
                details=", ".join(details),
            )
        )
    return "\n".join(lines)


def queued_text(
    routes: Mapping[str, int],
    addressed: Sequence[str],
    *,
    opening: int,
    stopped: Sequence[str] = (),
    outside: int = 0,
    named_outside: Sequence[str] = (),
) -> str:
    """Say who receives a new post, who receives it in full, and who only its opening.

    ``stopped`` names addressed participants whose work ended, who see the post
    only once the user resumes them. ``outside`` counts the participants a post
    outside the main discussion does not reach; ``named_outside`` names those of
    them its text names without "@".
    """

    count = sum(routes.values())
    if not count:
        return outside_text(outside, named_outside) or text.BOARD_QUEUED_NONE
    participants = text.BOARD_PARTICIPANTS["one" if count == 1 else "many"].format(count=count)
    parts = [text.BOARD_QUEUED.format(participants=participants)]
    if addressed:
        parts.append(text.BOARD_ADDRESSED.format(names=spoken_list(addressed)))
    if stopped:
        parts.append(
            text.BOARD_ADDRESSED_STOPPED["one" if len(stopped) == 1 else "many"].format(
                names=spoken_list(stopped)
            )
        )
    if opening:
        parts.append(text.BOARD_OPENING["one" if opening == 1 else "many"].format(count=opening))
    outside_sentence = outside_text(outside, named_outside)
    if outside_sentence:
        parts.append(outside_sentence)
    return " ".join(parts)


def outside_text(count: int, named: Sequence[str]) -> str:
    """Say how many participants a post outside the main discussion does not reach."""

    if not count:
        return ""
    names = spoken_list(named)
    if count == 1:
        return text.BOARD_OUTSIDE["one_named" if named else "one"].format(names=names)
    return text.BOARD_OUTSIDE["many_named" if named else "many"].format(count=count, names=names)


def plain_mentions(value: str, names: Mapping[str, str]) -> list[str]:
    """Return the participants ``value`` names without "@" before the name, in roster order."""

    return [
        participant_id
        for participant_id, name in names.items()
        if re.search(rf"(?<![\w@]){re.escape(name)}(?!\w)", value)
    ]


# "@" before a word, outside code and quotations, which may address a participant.
_AT_WORD = re.compile(r"(?<!\w)@(\w+)")
_CODE = re.compile(r"```.*?(?:```|$)|`[^`\n]*`", re.DOTALL)
# Text in double quotation marks on one line, as Agents quote a format such as "@X".
_QUOTED = re.compile(r'"[^"\n]*"|„[^“”"\n]*[“”"]|“[^”\n]*”|«[^»\n]*»|»[^«\n]*«')
# The example address of the Board description, quoted as a placeholder.
_EXAMPLE = "name"
# A word this short has too many one-letter neighbours to suggest a name for.
_CLOSE_MIN_CHARS = 4


@dataclass(frozen=True)
class Correction:
    """An "@" word that addressed no one and the participant name it most likely meant.

    ``possessive`` marks letters right after the whole name, as in "@Konrads";
    otherwise the word is one edit away from the name.
    """

    written: str
    name: str
    possessive: bool


@dataclass(frozen=True)
class Unaddressed:
    """Words after "@" in a post's text that address no participant, as written.

    ``others`` holds "@all" and its synonyms and capitalized words that name no
    participant (such as a name with a suffix); ``user`` holds words meaning the
    user. Lowercase words such as code decorators are left out, and so are words
    in code or quotations and the example "@Name". ``corrections`` names the
    participant meant by each of ``others`` that most likely meant one the post
    does not address anyway; a word meaning one it does address is left out.
    """

    others: list[str]
    user: list[str]
    corrections: list[Correction] = field(default_factory=list)


def unaddressed_mentions(value: str, roster: Roster) -> Unaddressed:
    """Find the "@" words of a post's text that address no participant."""

    names = {spelling(name): name for name in roster.names.values()}
    known = set(names) | {spelling(participant_id) for participant_id in roster.names}
    addressed = {
        spelling(roster.name(participant_id))
        for participant_id in mentioned_participants(value, roster.names)
    }
    others: dict[str, str] = {}
    user: dict[str, str] = {}
    corrections: dict[str, Correction] = {}
    for match in _AT_WORD.finditer(_QUOTED.sub(" ", _CODE.sub(" ", value))):
        word = match.group(1)
        key = spelling(word)
        if key in known or key == _EXAMPLE:
            continue
        if key in _EVERYONE or (word[:1].isupper() and key not in _USER):
            meant = None if key in _EVERYONE else _meant(key, names)
            if meant is not None:
                if spelling(meant[0]) in addressed:
                    continue  # The post addresses that participant anyway.
                corrections.setdefault(word.casefold(), Correction(match.group(0), *meant))
            others.setdefault(word.casefold(), match.group(0))
        elif key in _USER:
            user.setdefault(word.casefold(), match.group(0))
    return Unaddressed(list(others.values()), list(user.values()), list(corrections.values()))


def _meant(key: str, names: Mapping[str, str]) -> tuple[str, bool] | None:
    """Return the participant name a spelled "@" word most likely meant, and whether it
    is that whole name with a possessive "s", as in "@Konrads"; else None.

    Otherwise the word, or the word without a final "s", must be one edit away from
    exactly one name: a ratio of shared letters misses one-letter typos in short
    names, such as "Kondad" for "Konrad".
    """

    if key.endswith("s") and key[:-1] in names:
        return names[key[:-1]], True
    if len(key) < _CLOSE_MIN_CHARS:
        return None
    forms = {key, key.removesuffix("s")}
    close = {
        name
        for spelled, name in names.items()
        if any(edit_distance(form, spelled, 1) <= 1 for form in forms)
    }
    return (close.pop(), False) if len(close) == 1 else None


def with_notes(data: Json, notes: Sequence[str]) -> Json:
    if notes:
        data["note"] = " ".join(dict.fromkeys(notes))
    return data


__all__ = [
    "PAGE_LIMIT",
    "Correction",
    "Recipients",
    "Unaddressed",
    "Roster",
    "automatic_delivery",
    "call_text",
    "close_discussion",
    "discussion_choices",
    "discussion_label",
    "delivered_chars",
    "delivery_text",
    "discussions_text",
    "excerpt",
    "messages_text",
    "news_text",
    "outside_text",
    "page_body",
    "page_chars",
    "page_text",
    "plain_mentions",
    "post_block",
    "post_discussion_label",
    "post_suggestion",
    "queued_text",
    "resolve_recipients",
    "seen",
    "shortened",
    "status_data",
    "unaddressed_mentions",
    "unknown_recipients_message",
    "wake_text",
    "with_notes",
]
