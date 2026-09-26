"""Voice-style Live requests and a scripted vBot; expectations never reach the Model.

Each case is one delegated request as the voice model hands it on, with the
recent conversation and vBot updates the backend model would see. The scripted
vBot answers the Live Tools with plain-text results shaped like the real ones,
from one fixed state, and changes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.model_tasks._live_tools import (
    TOOL_OPEN,
    TOOL_OVERVIEW,
    TOOL_READ,
    TOOL_SEND_MESSAGE,
    TOOL_START_AGENT_SESSION,
    TOOL_START_CODING_TERMINAL,
    TOOL_STOP,
    TOOL_TERMINAL,
    live_failure,
    live_success,
)

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Contains:
    """A text argument that contains *text*, ignoring case."""

    text: str


class _Absent:
    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Any = _Absent()
"""An argument that is left out (or empty)."""


@dataclass(frozen=True)
class Expected:
    """One right first Tool call: the Tool and the arguments that matter.

    A plain string matches the same text ignoring case and surrounding spaces;
    other values match exactly. Arguments not named here may have any value.
    """

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveCase:
    """One delegated request.

    ``right`` lists the right first calls; ``None`` among them means answering
    without a Tool call (asking the user). ``lookup_ok`` also accepts a valid
    read-only first call (overview or read) as right, though not ideal.
    """

    id: str
    request: str
    right: tuple[Expected | None, ...]
    earlier: tuple[str, ...] = ()
    updates: tuple[str, ...] = ()
    lookup_ok: bool = True

    @property
    def conversation(self) -> str:
        return "\n".join((*self.earlier, f"User: {self.request}"))


def _update(payload: str) -> str:
    return f"vBot update: {payload}"


def live_cases() -> list[LiveCase]:
    return [
        LiveCase(
            id="sessions_count",
            request="Schick dreimal den Coder los: prüf, ob die Login-Tests unter Windows grün "
            "sind.",
            right=(
                Expected(
                    TOOL_START_AGENT_SESSION,
                    {"agent": "Coder", "count": 3, "task": Contains("Login")},
                ),
            ),
        ),
        LiveCase(
            id="codex_task",
            request="Start codex in vBot and fix the failing test.",
            right=(
                Expected(
                    TOOL_START_CODING_TERMINAL,
                    {"program": "codex", "folder": Contains("vbot"), "task": Contains("test")},
                ),
            ),
        ),
        LiveCase(
            id="claude_no_task",
            request="Starte Claude Code im vBot-Projekt.",
            right=(
                Expected(
                    TOOL_START_CODING_TERMINAL,
                    {"program": "claude", "folder": Contains("vbot"), "task": ABSENT},
                ),
            ),
        ),
        LiveCase(
            id="answer_question",
            request="Sag dem Coder ja, mach weiter.",
            earlier=("Assistant: Der Coder fragt, ob er auch die Tests anpassen soll.",),
            updates=(
                _update(
                    '{"run": "completed", "agent": "coder", "session": "s4", "result_excerpt": '
                    '"Die Migration ist fertig. Soll ich auch die Tests anpassen?", '
                    '"excerpt_truncated": false}'
                ),
            ),
            right=(Expected(TOOL_SEND_MESSAGE, {"target": "s4", "text": Contains("ja")}),),
        ),
        LiveCase(
            id="status",
            request="Was läuft gerade?",
            right=(Expected(TOOL_OVERVIEW),),
        ),
        LiveCase(
            id="stop_second",
            request="Stopp den zweiten.",
            earlier=(
                "User: Schick dreimal den Coder los: prüf, ob die Login-Tests unter Windows "
                "grün sind.",
                "Assistant: Erledigt, drei Sessions beim Coder arbeiten daran.",
            ),
            right=(Expected(TOOL_OVERVIEW), Expected(TOOL_STOP, {"target": "s2"})),
        ),
        LiveCase(
            id="show_terminal",
            request="Zeig mir das Terminal.",
            earlier=(
                "User: Starte Codex in vBot und reparier den fehlschlagenden Test.",
                "Assistant: Codex läuft und hat die Aufgabe bekommen.",
            ),
            right=(
                Expected(TOOL_OPEN, {"target": "t1"}),
                Expected(TOOL_OPEN, {"view": "terminals"}),
            ),
        ),
        LiveCase(
            id="read_result",
            request="What did the Reviewer find?",
            updates=(
                _update(
                    '{"run": "completed", "agent": "reviewer", "session": "s5", '
                    '"result_excerpt": "Found three issues in the parser. First, empty input", '
                    '"excerpt_truncated": true}'
                ),
            ),
            right=(
                Expected(TOOL_READ, {"target": "s5"}),
                Expected(TOOL_READ, {"target": "Reviewer"}),
            ),
        ),
        LiveCase(
            id="open_agent_page",
            request="Open the Reviewer's page.",
            right=(Expected(TOOL_OPEN, {"target": "Reviewer", "view": "agents"}),),
        ),
        LiveCase(
            id="maximize_terminal",
            request="Mach das Codex-Terminal groß.",
            right=(Expected(TOOL_TERMINAL, {"action": "maximize", "target": "t1"}),),
        ),
        LiveCase(
            id="unclear",
            request="Schick den mal los.",
            right=(None,),
        ),
    ]


def matches(expected: Expected, tool: str | None, arguments: Mapping[str, Any] | None) -> bool:
    """Whether a prepared call is *expected*."""

    if tool != expected.tool:
        return False
    given = arguments or {}
    return all(
        _value_matches(want, given.get(key, ABSENT)) for key, want in expected.arguments.items()
    )


def describe(expected: Expected | None) -> str:
    """A short text of one right first call, for reports."""

    if expected is None:
        return "no Tool call (ask the user)"
    parts = [f"{key}={_describe_value(value)}" for key, value in expected.arguments.items()]
    return f"{expected.tool}({', '.join(parts)})"


def example_arguments(expected: Expected) -> JsonObject:
    """Arguments that satisfy *expected*, to check that it is a valid call."""

    return {
        key: value.text if isinstance(value, Contains) else value
        for key, value in expected.arguments.items()
        if value is not ABSENT
    }


def _value_matches(want: Any, have: Any) -> bool:
    if want is ABSENT:
        return have is ABSENT or have is None or have == ""
    if isinstance(want, Contains):
        return isinstance(have, str) and want.text.casefold() in have.casefold()
    if isinstance(want, str):
        return isinstance(have, str) and have.strip().casefold() == want.casefold()
    return bool(have == want)


def _describe_value(value: Any) -> str:
    if value is ABSENT:
        return "left out"
    if isinstance(value, Contains):
        return f"contains {value.text!r}"
    return repr(value)


# -- scripted vBot ------------------------------------------------------------

_VBOT_FOLDER = "C:/Development/projects/vBot"
_AGENTS = ("Coder", "Reviewer", "Researcher")


@dataclass
class _Session:
    ref: str
    agent: str
    title: str
    state: str
    last: str = ""


class ScriptedVbot:
    """Answers Live Tool calls from one fixed state; nothing changes anywhere."""

    def __init__(self) -> None:
        self.sessions = [
            _Session("s1", "Coder", "Login-Tests", "working"),
            _Session("s2", "Coder", "Login-Tests", "working"),
            _Session("s3", "Coder", "Login-Tests", "working"),
            _Session(
                "s4",
                "Coder",
                "Migration",
                "waiting for an answer",
                "Die Migration ist fertig. Soll ich auch die Tests anpassen?",
            ),
            _Session(
                "s5",
                "Reviewer",
                "Parser review",
                "finished",
                "Found three issues in the parser. First, empty input crashes the tokenizer. "
                "Second, line numbers are off by one after comments. Third, the error messages "
                "omit the file name.",
            ),
        ]
        self.terminals = {"t1": "Codex"}
        self._next_session = len(self.sessions) + 1
        self._next_terminal = len(self.terminals) + 1

    async def __call__(self, name: str, arguments: JsonObject) -> JsonObject:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return live_failure("unknown_tool", f"There is no Tool called {name}.")
        result: JsonObject = handler(arguments)
        return result

    # -- Tools ----------------------------------------------------------------

    def _overview(self, arguments: JsonObject) -> JsonObject:
        agent = arguments.get("agent")
        if agent:
            label = self._agent(str(agent))
            if label is None:
                return self._unknown_agent(str(agent))
            own = [item for item in self.sessions if item.agent == label]
            if not own:
                return live_success(f"{label} has no Sessions.")
            lines = [f"Sessions of {label}, most recent first:"]
            return live_success("\n".join(lines + [_line(item) for item in reversed(own)]))
        lines = [
            "App: chat view; selected Agent: Coder; selected Project: vBot.",
            f"Agents: {', '.join(_AGENTS)}.",
            f"Projects: vBot ({_VBOT_FOLDER}).",
            "Sessions (running, then recently finished):",
            *(_line(item) for item in self.sessions),
            "Terminals:",
            *(
                f"- {ref} {label} in {_VBOT_FOLDER}: running"
                for ref, label in self.terminals.items()
            ),
        ]
        return live_success("\n".join(lines))

    def _start_agent_session(self, arguments: JsonObject) -> JsonObject:
        label = self._agent(str(arguments.get("agent") or ""))
        if label is None:
            return self._unknown_agent(str(arguments.get("agent") or ""))
        count = arguments.get("count", 1)
        count = count if isinstance(count, int) and count >= 1 else 1
        refs = []
        for _ in range(count):
            ref = f"s{self._next_session}"
            self._next_session += 1
            self.sessions.append(
                _Session(ref, label, str(arguments.get("task", ""))[:40], "working")
            )
            refs.append(ref)
        if count == 1:
            return live_success(
                f"Started a Session at {label} with the task: {refs[0]}. It works in the "
                "background; an update follows when it finishes."
            )
        return live_success(
            f"Started {count} Sessions at {label} with the task: {', '.join(refs)}. They work in "
            "the background; an update follows when each finishes."
        )

    def _start_coding_terminal(self, arguments: JsonObject) -> JsonObject:
        label = "Claude Code" if arguments.get("program") == "claude" else "Codex"
        folder = str(arguments.get("folder") or "vBot")
        if "vbot" not in folder.casefold():
            return live_failure(
                "unknown_folder",
                f'No Project or folder "{folder}" was found. Projects: vBot ({_VBOT_FOLDER}). '
                "Ask the user where to work.",
            )
        count = arguments.get("count", 1)
        count = count if isinstance(count, int) and count >= 1 else 1
        refs = []
        for _ in range(count):
            ref = f"t{self._next_terminal}"
            self._next_terminal += 1
            self.terminals[ref] = label
            refs.append(ref)
        noun = "a Terminal" if count == 1 else f"{count} Terminals"
        head = f"Started {label} in {noun} in {_VBOT_FOLDER}: {', '.join(refs)}."
        if not arguments.get("task"):
            return live_success(head)
        return live_success(f"{head} Typed the task into {', '.join(refs)} and sent it.")

    def _send_message(self, arguments: JsonObject) -> JsonObject:
        found = self._target(arguments.get("target"), "send_message")
        if not isinstance(found, str):
            return found
        if found in self.terminals:
            return live_success(f"Sent to {found} ({self.terminals[found]}).")
        session = self._session(found)
        if session.state == "working":
            return live_success(
                f"Sent to {found} ({session.agent}). It is still working, so the message waits "
                "in its Queue and runs next."
            )
        session.state = "working"
        return live_success(
            f"Sent to {found} ({session.agent}). An update follows when it finishes."
        )

    def _read(self, arguments: JsonObject) -> JsonObject:
        found = self._target(arguments.get("target"), "read")
        if not isinstance(found, str):
            return found
        if found in self.terminals:
            return live_success(
                f"{found} {self.terminals[found]} in {_VBOT_FOLDER}, running. Screen, quoted:\n"
                "> Running the test suite (48 of 120 tests passed so far)"
            )
        session = self._session(found)
        body = f"Assistant: {session.last}" if session.last else "User: " + session.title
        return live_success(
            f"{found} at {session.agent}, {session.state}. Latest messages, quoted:\n{body}"
        )

    def _stop(self, arguments: JsonObject) -> JsonObject:
        found = self._target(arguments.get("target"), "stop")
        if not isinstance(found, str):
            return found
        who = self.terminals.get(found) or self._session(found).agent
        return live_success(
            f"Stopped the current work of {found} ({who}). It stays open for new messages."
        )

    def _open(self, arguments: JsonObject) -> JsonObject:
        target = arguments.get("target")
        view = arguments.get("view")
        if not target:
            if not view:
                return live_failure(
                    "invalid_arguments", 'Name a target or a view, such as {"view": "chat"}.'
                )
            return live_success(f"Opened the {view} view.")
        if isinstance(target, str) and target.strip().casefold() == "vbot":
            return live_success("Showing the page of Project vBot.")
        label = self._agent(str(target))
        if label is not None and view == "agents":
            return live_success(f"Showing the page of Agent {label}.")
        found = self._target(target, "open")
        if not isinstance(found, str):
            return found
        if found in self.terminals:
            return live_success(f"Showing {found} in the Terminals view.")
        return live_success(f"Showing {found} ({self._session(found).agent}) in the chat.")

    def _terminal(self, arguments: JsonObject) -> JsonObject:
        action = arguments.get("action")
        target = arguments.get("target")
        if action in {"create_group", "rename_group", "delete_group", "reorder"}:
            return live_success(f"Done: {action}.")
        found = self._target(target, "terminal")
        if not isinstance(found, str):
            return found
        if found not in self.terminals:
            return live_failure(
                "not_a_terminal",
                f"{found} is a Session, not a Terminal. Terminals: {', '.join(self.terminals)}.",
            )
        if action == "maximize":
            return live_success(f"Maximized {found}.")
        if action == "restore":
            return live_success("Restored the group layout.")
        if action == "key":
            return live_success(f"Pressed {arguments.get('key')} in {found}.")
        if action == "close":
            del self.terminals[found]
            return live_success(f"Closed {found}: stopped and removed.")
        return live_success(f"Done: {action} {found}.")

    # -- resolution -----------------------------------------------------------

    def _agent(self, text: str) -> str | None:
        wanted = text.strip().casefold()
        return next((label for label in _AGENTS if label.casefold() == wanted), None)

    def _unknown_agent(self, text: str) -> JsonObject:
        return live_failure(
            "not_found",
            f'No Agent is called "{text}". Agents: {", ".join(_AGENTS)}. Ask the user which one '
            "they mean.",
        )

    def _session(self, ref: str) -> _Session:
        return next(item for item in self.sessions if item.ref == ref)

    def _target(self, value: Any, tool: str) -> str | JsonObject:
        text = str(value or "").strip()
        ref = re.sub(r"[^a-z0-9]", "", text.casefold())
        if ref in self.terminals or any(item.ref == ref for item in self.sessions):
            return ref
        label = self._agent(text)
        if label is not None:
            own = [item.ref for item in self.sessions if item.agent == label]
            if len(own) == 1:
                return own[0]
            if own:
                return live_failure(
                    "ambiguous",
                    f"{label} has several Sessions: {', '.join(own)}. Ask the user which one they "
                    f'mean, then call {tool} with its ref, such as {{"target": "{own[-1]}"}}.',
                )
            return live_failure("not_found", f"{label} has no Sessions.")
        return live_failure(
            "not_found",
            f'Nothing is called "{text}". Call overview to see the refs of the Sessions and '
            "Terminals.",
        )


def _line(item: _Session) -> str:
    head = f'- {item.ref} {item.agent} "{item.title}"'
    if item.last and item.state != "working":
        return f'{head}: {item.state}: "{item.last}"'
    return f"{head}: {item.state}"
