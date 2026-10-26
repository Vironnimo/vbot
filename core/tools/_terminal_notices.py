"""Terminal screen and attention wording, with bounded delivery size."""

from __future__ import annotations

import json

from core.tools.terminal_emulator import (
    ScreenObservation,
)

from ._terminal_activity import (
    IdleReason,
)
from ._terminal_state import (
    TERMINAL_NOTICE_MESSAGE_CAP_CHARS,
    TERMINAL_STATUS_MAX_LINES,
    TerminalAttention,
)


def _attention_body(
    terminal_id: str,
    label: str,
    attention: TerminalAttention,
    *,
    observation: ScreenObservation | None,
    unseen: str | None,
    idle_seconds: float,
    behind: str | None = None,
) -> str:
    """The message that tells the attached Agent its interactive program needs a look.

    It shows the whole screen; when the message would exceed its cap, the
    screen's top rows are left out, and the message says how many.
    """
    rows = observation.screen.split("\n") if observation is not None and observation.screen else []
    cursor = observation.cursor if observation is not None else None
    left_out = 0
    while True:
        body = _compose_attention(
            terminal_id,
            label,
            attention,
            rows=rows[left_out:],
            readable=observation is not None,
            cursor=cursor,
            left_out=left_out,
            unseen=unseen,
            behind=behind,
            idle_seconds=idle_seconds,
        )
        if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS or left_out >= len(rows):
            break
        left_out += 1
    if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS:
        return body
    return body[:TERMINAL_NOTICE_MESSAGE_CAP_CHARS] + "\n[message cut]"


def _compose_attention(
    terminal_id: str,
    label: str,
    attention: TerminalAttention,
    *,
    rows: list[str],
    readable: bool,
    cursor: tuple[int, int] | None,
    left_out: int,
    unseen: str | None,
    behind: str | None,
    idle_seconds: float,
) -> str:
    if not readable:
        screen = "(the screen could not be read)"
    elif rows:
        screen = _fenced("\n".join(rows))
    else:
        screen = "(the screen is empty)"
    if attention.kind == "idle":
        reason: IdleReason | None = attention.details.get("reason")
        shown = (
            "Screen"
            if cursor is None
            else f"Screen (cursor at row {cursor[0]}, column {cursor[1]})"
        )
        if reason == "fallback":
            headline = (
                f"Terminal {terminal_id} ({label}) has printed nothing for {idle_seconds:g} "
                f"seconds; its program still runs and can still be working. {shown}:"
            )
        else:
            headline = (
                f"Terminal {terminal_id} ({label}) went idle; its program still runs. {shown}:"
            )
        closing = [
            f'To type into it, call terminal with action "input", terminal_id "{terminal_id}", '
            'your text as text and key "enter". Its next new screen arrives the same way.'
        ]
    elif attention.kind == "exited":
        headline = (
            f"The program in terminal {terminal_id} ({label}) exited with code "
            f"{attention.details.get('exit_code')}. Last screen:"
        )
        closing = []
    else:
        headline = (
            f"Terminal {terminal_id} ({label}) failed and its program no longer runs: "
            f"{attention.details.get('error')}. Last screen:"
        )
        closing = []
    lines = [headline, screen]
    if left_out:
        status = json.dumps({"action": "status", "terminal_id": terminal_id})
        lines.append(
            f"The top {_count(left_out, 'line')} of the screen {_are(left_out)} left out; "
            f"to see the whole screen, call terminal {status}."
        )
    if behind:
        lines.append(behind)
    if unseen:
        lines.append(unseen)
    lines.extend(closing)
    return "\n".join(lines)


def revoked_computer_notice(terminal_id: str, label: str, computer: str) -> str:
    """Told once when the attached Session lost the computer a terminal runs on."""
    return (
        f"Terminal {terminal_id} ({label}) runs on {computer}, which is no longer enabled "
        "for this Session. Its screen and its end are no longer shown to you; its programs "
        "keep running there. The user can enable that computer again in the Session's "
        "computer settings, or view and stop the terminal in Terminals."
    )


def screen_behind_note(error: str, log: str | None, *, complete: bool) -> str:
    """The screen misses output: drawing it failed."""
    note = (
        f"This screen is out of date: drawing the program's output failed ({error}), so the "
        "screen lacks some of that output."
    )
    if log is None:
        return note
    if complete:
        return f"{note} The output is in the log {log}."
    return f"{note} The log {log} has the output only until writing it failed."


def unseen_lines_note(
    terminal_id: str,
    *,
    unseen_from: int,
    shown_from: int,
    first_line: int,
    log: str | None,
    seen: bool,
    log_complete: bool = True,
    log_end_line: int | None = None,
) -> str | None:
    """Say how many output lines above what a message shows are left out, and
    where to read them; None when none are.

    Lines are history line numbers: *unseen_from* is the first line the
    reader has not seen (0 for all output), *shown_from* the first line the
    message shows, *first_line* the oldest line the terminal keeps. With
    *seen*, the reader is the attached Agent, whose seen lines are known.
    Without *log_complete*, the log ends where writing it failed: it lacks
    *log_end_line* and the lines after it, when that line is known.
    """
    count = shown_from - unseen_from
    if count <= 0:
        return None
    lines = _count(count, "earlier line")
    what = f"{lines} you have not seen" if seen else lines
    kept_from = max(unseen_from, first_line)
    kept = max(0, shown_from - kept_from)
    if log and log_complete:
        # The log has every line; the page is the newest of them the terminal
        # keeps, right above what the message shows (user decision D1).
        newest = min(kept, TERMINAL_STATUS_MAX_LINES)
        page = _status_page(terminal_id, shown_from - newest, newest)
        if newest == count:
            where = f"to read them, call {page}, or read the log {log}"
        else:
            where = f"to read them, read the log {log}" + (
                f"; the newest {newest} of them can also be read with {page}" if newest else ""
            )
        return f"{what} {_are(count)} not shown; {where}."
    page = _status_page(terminal_id, kept_from, TERMINAL_STATUS_MAX_LINES)
    if log and not log_complete:
        if log_end_line is not None:
            where = _partial_log_sources(
                count, kept, max(0, min(shown_from, log_end_line) - unseen_from), page, log
            )
            return f"{what} {_are(count)} not shown; {where}."
        log = f"{log}, which ends where writing it failed"
    if kept == count:
        where = f"to read them, call {page}" + (f", or read the log {log}" if log else "")
    elif log:
        where = f"to read them, read the log {log}" + (
            f"; the newest {kept} of them can also be read with {page}" if kept else ""
        )
    elif kept:
        where = f"the newest {kept} of them can be read with {page}; the others are no longer kept"
    else:
        where = "they are no longer kept"
    return f"{what} {_are(count)} not shown; {where}."


def _status_page(terminal_id: str, start_line: int, lines: int) -> str:
    """The status call that reads *lines* history lines from *start_line*."""
    page = {
        "action": "status",
        "terminal_id": terminal_id,
        "start_line": start_line,
        "lines": lines,
    }
    return "terminal " + json.dumps(page)


def _partial_log_sources(count: int, kept: int, logged: int, page: str, log: str) -> str:
    """Where *count* unseen lines can be read when writing the log failed: the
    oldest *logged* of them are in the log, the newest *kept* in the terminal;
    the others are lost."""
    if kept == count:
        return f"to read them, call {page}" + (
            f", or read the log {log}" if logged == count else ""
        )
    unkept = count - kept
    in_log = min(logged, unkept)
    lost = unkept - in_log
    parts: list[str] = []
    if in_log == count:
        parts.append(f"to read them, read the log {log}")
    elif in_log:
        parts.append(f"the oldest {_lines(in_log)} {_are(in_log)} in the log {log}")
    if lost == count:
        parts.append(f"they are no longer kept, and writing the log {log} failed before them")
    elif lost:
        subject = f"{'the next' if in_log else 'the oldest'} {_lines(lost)}"
        named = "the log" if in_log else f"the log {log}"
        them = "it" if lost == 1 else "them"
        parts.append(
            f"{subject} {_are(lost)} no longer kept, and writing {named} failed before {them}"
        )
    if kept:
        parts.append(f"the newest {_lines(kept)} can be read with {page}")
    return "; ".join(parts)


def _lines(number: int) -> str:
    """Some of the lines a note counts: "line" or "7 lines"."""
    return "line" if number == 1 else f"{number} lines"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _are(number: int) -> str:
    return "is" if number == 1 else "are"


def _seen_through(observation: ScreenObservation, *, alternate: bool) -> int:
    """The history line below the last screen row *observation* shows.

    Showing the screen shows its rows, which scroll into history as these
    lines; a full-screen view (alternate screen) shows none of them.
    """
    if alternate or not observation.screen:
        return observation.screen_start_line
    return observation.screen_start_line + observation.screen.count("\n") + 1


def _fenced(text: str) -> str:
    """Screen text in a fence that no line of it can close."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}text\n{text}\n{fence}"
