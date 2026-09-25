"""Terminal operations of a Live call: coding programs, typing, keys, and layout.

Live starts Codex and Claude Code through ``terminal.start`` with the program's
command alone. A task never becomes part of a command line: on Windows the
programs often resolve to ``.cmd`` shims, where quotes, ``&`` or ``%`` in free
text can escape into the shell. Live waits until the program shows its own
input line, types the task through guarded Terminal input (bracketed paste when
the program enabled it, control characters rejected, the screen revision
checked), and sends Enter separately once the text arrived. It never types while
the screen shows the shell or a startup question, where the text would run as
shell commands or answer the question.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.model_tasks.live import (
    CODING_PROGRAMS,
    MAX_LIVE_NAME_CHARS,
    MAX_LIVE_TEXT_CHARS,
    CliPrompt,
    CodingProgram,
    live_failure,
    live_success,
    program_input_visible,
    program_prompt,
    program_ready,
    shell_prompt_visible,
)
from core.tools._call_vocabulary import spelling
from server._live_context import (
    UNCERTAIN_DELIVERY,
    VOICE_STOPPED,
    JsonObject,
    LiveContext,
    LiveToolError,
    LiveUiError,
    join_words,
    text_field,
)
from server._live_targets import (
    GROUP,
    TERMINAL,
    LiveCatalog,
    LiveRefs,
    resolve_target,
    terminal_label,
)
from server.rpc.errors import RpcError

_LOGGER = logging.getLogger("vbot.server.live")

KEY_SEQUENCES = {
    "enter": "\r",
    "escape": "\x1b",
    "tab": "\t",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "left": "\x1b[D",
    "right": "\x1b[C",
    "ctrl-c": "\x03",
}
_KEY_LABELS = {
    "enter": "Enter",
    "escape": "Escape",
    "tab": "Tab",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "ctrl-c": "Ctrl+C",
}
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
_EDITABLE_GROUP_KINDS = frozenset({"user", "agent"})
_FINISHED_STATES = frozenset({"exited", "error"})
_STATE_WORDS = {
    "starting": "starting",
    "ready": "idle",
    "working": "working",
    "exited": "exited",
    "error": "failed",
}
_EXECUTABLE_SUFFIX = re.compile(r"\.(exe|cmd|bat|ps1)$", re.IGNORECASE)
# The Terminal manager's message when input was based on an older screen.
_STALE_SCREEN = "Terminal screen changed"
_INPUT_ATTEMPTS = 3
_MAX_SCREEN_CHARS = 8_000


@dataclass(frozen=True)
class TerminalTimings:
    """How Live waits for a coding program before typing into it."""

    poll_seconds: float = 0.5
    ready_timeout_seconds: float = 25.0
    stable_seconds: float = 0.8
    enter_delay_seconds: float = 0.5


@dataclass(frozen=True)
class _Snapshot:
    screen: str
    revision: int | None
    bracketed_paste: bool
    finished: bool


@dataclass(frozen=True)
class _Outcome:
    """What happened to one Terminal of a start or a message."""

    status: str
    prompt: CliPrompt | None = None
    detail: str = ""


def coding_program(item: JsonObject) -> CodingProgram | None:
    """The coding program a Terminal was started with, if it is one Live knows."""
    command = str(item.get("launch_command") or item.get("command") or "").strip()
    base = _EXECUTABLE_SUFFIX.sub("", re.split(r"[\\/]", command)[-1]).lower()
    return next((program for program in CODING_PROGRAMS.values() if program.command == base), None)


def terminal_state(item: JsonObject) -> str:
    state = str(item.get("state") or "")
    return _STATE_WORDS.get(state, state or "unknown")


def terminal_line(ref: str, item: JsonObject) -> str:
    """One overview line: ref, program, name, folder, state."""
    program = coding_program(item)
    parts = [ref, program.label if program else terminal_label(item)]
    name = item.get("name")
    if isinstance(name, str) and name.strip() and program is not None:
        parts.append(f'"{name.strip()}"')
    folder = item.get("workdir")
    if isinstance(folder, str) and folder:
        parts.append(f"in {folder}")
    return f"{' '.join(parts)}: {terminal_state(item)}"


def text_problem(text: str) -> str | None:
    """Why *text* cannot be typed into a Terminal, or ``None``."""
    if len(text) > MAX_LIVE_TEXT_CHARS:
        return f"The text is longer than {MAX_LIVE_TEXT_CHARS} characters."
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        return "The text contains control characters; send plain text only."
    return None


class LiveTerminals:
    """Terminal operations for one Live call's Tool executions."""

    def __init__(
        self,
        ctx: LiveContext,
        refs: LiveRefs,
        *,
        timings: TerminalTimings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ctx = ctx
        self._refs = refs
        self._timings = timings
        self._sleep = sleep
        self._clock = clock

    # -- start_coding_terminal --------------------------------------------

    async def start(
        self,
        *,
        program: CodingProgram,
        count: int,
        folder: str,
        name: str,
        task: str,
        catalog: LiveCatalog,
    ) -> JsonObject:
        """Start *count* Terminals running *program*, then type *task* into each."""
        if task and (problem := text_problem(task)):
            raise LiveToolError(
                "invalid_task",
                f"{problem} Call start_coding_terminal again with the task as plain text.",
            )
        if len(name) > MAX_LIVE_NAME_CHARS:
            raise LiveToolError(
                "invalid_name",
                f"name must be at most {MAX_LIVE_NAME_CHARS} characters. Call "
                "start_coding_terminal again with a shorter name.",
            )
        workdir = await self._workdir(folder, catalog)
        self._ctx.ensure_active()
        group_id = await self._program_group(program.label, await catalog.groups())
        started: list[str] = []
        try:
            for _ in range(count):
                self._ctx.ensure_active()
                params: JsonObject = {
                    "command": program.command,
                    "workdir": workdir,
                    "group_id": group_id,
                }
                if name:
                    params["name"] = name
                result = await self._ctx.call("terminal.start", params)
                started.append(str(result["terminal"]["terminal_id"]))
        except Exception as exc:
            catalog.forget_terminals()
            return self._start_failure(program, count, started, exc)
        catalog.forget_terminals()
        refs = [self._refs.terminal(terminal_id) for terminal_id in started]
        head = (
            f"Started {program.label} in {_count_phrase(len(refs), 'Terminal')} in {workdir}: "
            f"{', '.join(refs)}."
        )
        shown = await self._show_quietly("show", terminal_id=started[0])
        if not task:
            return live_success(head + ("" if shown else " The app did not switch to it."))
        outcomes = await asyncio.gather(
            *(self._start_task(terminal_id, program, task) for terminal_id in started),
            return_exceptions=True,
        )
        report = _task_report(
            program,
            refs,
            [_outcome(item) for item in outcomes],
            timeout_seconds=self._timings.ready_timeout_seconds,
        )
        return live_success(f"{head} {report}")

    def _start_failure(
        self, program: CodingProgram, count: int, started: list[str], exc: Exception
    ) -> JsonObject:
        if isinstance(exc, LiveToolError | RpcError):
            reason = exc.message
            uncertain = False
        else:
            _LOGGER.exception("Live Terminal start failed unexpectedly")
            reason = "The start failed unexpectedly."
            uncertain = True
        refs = [self._refs.terminal(terminal_id) for terminal_id in started]
        done = (
            f"Started {program.label} in {join_words(refs)}; "
            if refs
            else "No Terminal was started; "
        )
        message = (
            f"{done}starting Terminal {len(refs) + 1} of {count} failed: {reason} Nothing was "
            "retried and no task was typed."
        )
        if uncertain:
            message += f" {UNCERTAIN_DELIVERY}"
        return live_failure("partial" if refs else "start_failed", message)

    async def _start_task(self, terminal_id: str, program: CodingProgram, task: str) -> _Outcome:
        readiness = await self._await_ready(terminal_id, program)
        if readiness.status != "ready":
            return readiness
        return await self._type(terminal_id, program, task)

    async def _await_ready(self, terminal_id: str, program: CodingProgram) -> _Outcome:
        """Poll until the program is ready or asks a startup question on a stable screen."""
        timings = self._timings
        deadline = self._clock() + timings.ready_timeout_seconds
        last_screen: str | None = None
        stable_since = self._clock()
        while True:
            self._ctx.ensure_active()
            snapshot = await self._read(terminal_id)
            now = self._clock()
            if snapshot.finished:
                return _Outcome("exited")
            if snapshot.screen != last_screen:
                last_screen = snapshot.screen
                stable_since = now
            stable = now - stable_since >= timings.stable_seconds
            prompt = program_prompt(program, snapshot.screen)
            if stable and prompt is not None:
                return _Outcome("prompt", prompt=prompt)
            if stable and program_ready(program, snapshot.screen):
                return _Outcome("ready")
            if now >= deadline:
                return _Outcome("not_ready", prompt=prompt)
            await self._sleep(timings.poll_seconds)

    async def _workdir(self, folder: str, catalog: LiveCatalog) -> str:
        projects = await catalog.projects()
        if folder:
            key = spelling(folder)
            matches = [
                project
                for project in projects
                if project.folder
                and key
                and key in {spelling(project.name), spelling(project.project_id)}
            ]
            if len(matches) == 1:
                return matches[0].folder
            path = Path(folder).expanduser()
            if path.is_absolute() and await asyncio.to_thread(path.is_dir):
                return str(path)
            names = ", ".join(project.name for project in projects[:12])
            listed = f" Projects: {names}." if names else ""
            raise LiveToolError(
                "folder_not_found",
                f'No Project or existing folder matches "{folder}".{listed} Ask the user which '
                "Project or folder to use, then call start_coding_terminal again with it as "
                "folder.",
            )
        project = await catalog.selected_project()
        if project is not None and project.folder:
            return project.folder
        raise LiveToolError(
            "folder_missing",
            "No folder was given and no Project is selected in the app. Ask the user which "
            "Project or folder to work in, then call start_coding_terminal again with folder.",
        )

    async def _program_group(self, label: str, groups: list[JsonObject]) -> str:
        for group in groups:
            if (
                str(group.get("name") or "").casefold() == label.casefold()
                and group.get("kind") in _EDITABLE_GROUP_KINDS
            ):
                return str(group["group_id"])
        created = await self._ctx.call("terminal.group.create", {"name": label})
        return str(created["group"]["group_id"])

    # -- send_message, read, stop ------------------------------------------

    async def send(self, terminal: JsonObject, text: str) -> JsonObject:
        """Type *text* into a coding Terminal and send it with Enter."""
        ref, program = self._coding(terminal, "send_message")
        if problem := text_problem(text):
            raise LiveToolError(
                "invalid_text", f"{problem} Call send_message again with the message as text."
            )
        try:
            outcome = await self._type(str(terminal["terminal_id"]), program, text)
        except LiveToolError:
            raise
        except RpcError as exc:
            return live_failure(
                "send_failed", f"Nothing was sent to {ref}: {exc.message} Call read with {ref}."
            )
        except Exception:
            _LOGGER.exception("Live Terminal message failed unexpectedly")
            return live_failure(
                "send_failed", f"Sending to {ref} failed unexpectedly. {UNCERTAIN_DELIVERY}"
            )
        if outcome.status == "sent":
            return live_success(f"Sent to {ref} ({program.label}).")
        return live_failure("not_sent", _message_problem(ref, program, outcome))

    async def read(self, terminal: JsonObject) -> JsonObject:
        """The coding Terminal's screen as quoted text."""
        ref, program = self._coding(terminal, "read")
        snapshot = await self._read(str(terminal["terminal_id"]))
        lines = snapshot.screen.rstrip().splitlines()
        screen = "\n".join(line.rstrip() for line in lines)
        truncated = len(screen) > _MAX_SCREEN_CHARS
        screen = screen[-_MAX_SCREEN_CHARS:]
        state = "exited" if snapshot.finished else terminal_state(terminal)
        quoted = "\n".join(f"> {line}" if line else ">" for line in screen.splitlines())
        head = f"{ref} ({program.label}), {state}. Screen"
        head += ", last part" if truncated else ""
        return live_success(f"{head}, quoted:\n{quoted or '> (empty)'}")

    async def interrupt(self, terminal: JsonObject) -> JsonObject:
        """Press the program's interrupt key."""
        ref, program = self._coding(terminal, "stop")
        return await self._press(terminal, ref, program.interrupt_key, tool="stop")

    # -- terminal -----------------------------------------------------------

    async def run_action(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        """Run one ``terminal`` Tool action."""
        action = str(arguments.get("action") or "")
        if action == "restore":
            await self._ctx.view("restore")
            return live_success("Restored the Terminals view to its group layout.")
        if action == "create_group":
            return await self._create_group(text_field(arguments, "name"))
        if action == "reorder":
            return await self._reorder(arguments, catalog)
        target_text = text_field(arguments, "target")
        if not target_text:
            raise LiveToolError(
                "missing_target",
                f"{action} needs a target. Call terminal again with "
                f'{{"action": "{action}", "target": '
                f'"{"<group name>" if action.endswith("_group") else "t1"}"}}.',
            )
        if action in {"rename_group", "delete_group"}:
            target = await resolve_target(
                target_text,
                {GROUP},
                tool="terminal",
                field="target",
                refs=self._refs,
                catalog=catalog,
            )
            assert target.group is not None
            return await self._group_change(action, target.group, text_field(arguments, "name"))
        target = await resolve_target(
            target_text,
            {TERMINAL},
            tool="terminal",
            field="target",
            refs=self._refs,
            catalog=catalog,
        )
        terminal = target.terminal
        assert terminal is not None
        ref = self._refs.terminal(str(terminal["terminal_id"]))
        if action == "maximize":
            await self._ctx.view("maximize", terminal_id=terminal["terminal_id"])
            return live_success(f"Maximized {ref} in the Terminals view.")
        if action == "close":
            return await self._close(terminal, ref)
        key = text_field(arguments, "key")
        if key not in KEY_SEQUENCES:
            raise LiveToolError(
                "missing_key",
                f"key must be one of {', '.join(KEY_SEQUENCES)}. Call terminal again with "
                f'{{"action": "key", "target": "{ref}", "key": "enter"}}.',
            )
        ref, _program = self._coding(terminal, "terminal")
        return await self._press(terminal, ref, key, tool="terminal")

    async def _create_group(self, name: str) -> JsonObject:
        if not name or len(name) > MAX_LIVE_NAME_CHARS:
            raise LiveToolError(
                "invalid_name",
                f"create_group needs a name of at most {MAX_LIVE_NAME_CHARS} characters. Call "
                'terminal again with {"action": "create_group", "name": "<group name>"}.',
            )
        created = await self._ctx.call("terminal.group.create", {"name": name})
        text = f'Created the Terminal group "{name}".'
        shown = await self._show_quietly("show_group", group_id=created["group"]["group_id"])
        return live_success(text if shown else f"{text} The app did not switch to it.")

    async def _group_change(self, action: str, group: JsonObject, name: str) -> JsonObject:
        label = str(group.get("name") or group["group_id"])
        if group.get("kind") not in _EDITABLE_GROUP_KINDS:
            raise LiveToolError(
                "group_not_editable",
                f'The group "{label}" is kept by the app and cannot be changed; only groups the '
                "user or an Agent created can.",
            )
        if action == "rename_group":
            if not name or len(name) > MAX_LIVE_NAME_CHARS:
                raise LiveToolError(
                    "invalid_name",
                    f"rename_group needs a new name of at most {MAX_LIVE_NAME_CHARS} characters. "
                    f'Call terminal again with {{"action": "rename_group", "target": "{label}", '
                    '"name": "<new name>"}.',
                )
            await self._ctx.call(
                "terminal.group.rename", {"group_id": group["group_id"], "name": name}
            )
            text = f'Renamed the group "{label}" to "{name}".'
        else:
            result = await self._ctx.call("terminal.group.delete", {"group_id": group["group_id"]})
            killed = result.get("terminals_killed")
            stopped = (
                f" and stopped {_count_phrase(killed, 'Terminal')}"
                if isinstance(killed, int) and killed
                else ""
            )
            text = f'Deleted the group "{label}"{stopped}.'
        return live_success(await self._after_change(text))

    async def _reorder(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        order = arguments.get("order")
        if (
            not isinstance(order, list)
            or not order
            or not all(isinstance(item, str) for item in order)
        ):
            raise LiveToolError(
                "missing_order",
                "reorder needs order: every Terminal ref of the group in the new order. Call "
                'terminal again with {"action": "reorder", "order": ["t2", "t1"]}.',
            )
        terminals: list[JsonObject] = []
        for item in order:
            target = await resolve_target(
                item, {TERMINAL}, tool="terminal", field="order", refs=self._refs, catalog=catalog
            )
            assert target.terminal is not None
            terminals.append(target.terminal)
        target_text = text_field(arguments, "target")
        if target_text:
            target = await resolve_target(
                target_text,
                {GROUP},
                tool="terminal",
                field="target",
                refs=self._refs,
                catalog=catalog,
            )
            group = target.group
        else:
            group_ids = {item.get("group_id") for item in terminals}
            groups = await catalog.groups()
            group = next(
                (item for item in groups if len(group_ids) == 1 and item["group_id"] in group_ids),
                None,
            )
        if group is None:
            raise LiveToolError(
                "missing_group",
                "The Terminals are in different groups. Call terminal again with the group name "
                'as target, for example {"action": "reorder", "target": "Codex", "order": '
                '["t2", "t1"]}.',
            )
        label = str(group.get("name") or group["group_id"])
        if group.get("kind") not in _EDITABLE_GROUP_KINDS:
            raise LiveToolError(
                "group_not_editable",
                f'The group "{label}" is kept by the app and cannot be reordered.',
            )
        members = [
            item for item in await catalog.terminals() if item.get("group_id") == group["group_id"]
        ]
        ids = [str(item["terminal_id"]) for item in terminals]
        member_ids = {str(item["terminal_id"]) for item in members}
        if len(ids) != len(member_ids) or set(ids) != member_ids:
            refs = [self._refs.terminal(str(item["terminal_id"])) for item in members]
            raise LiveToolError(
                "invalid_order",
                f'order must name every Terminal of the group "{label}" exactly once: '
                f"{', '.join(refs)}. Call terminal again with all of them in the new order.",
            )
        await self._ctx.call("terminal.group.order", {"group_id": group["group_id"], "order": ids})
        new_order = ", ".join(self._refs.terminal(item) for item in ids)
        return live_success(
            await self._after_change(f'Reordered the group "{label}": {new_order}.')
        )

    async def _close(self, terminal: JsonObject, ref: str) -> JsonObject:
        """Stop the Terminal, then forget it, like the app's close button."""
        terminal_id = terminal["terminal_id"]
        stopped = terminal.get("state") in _FINISHED_STATES
        try:
            if not stopped:
                await self._ctx.call("terminal.kill", {"terminal_id": terminal_id})
                stopped = True
            self._ctx.ensure_active()
            await self._ctx.call("terminal.forget", {"terminal_id": terminal_id})
        except Exception as exc:
            if not isinstance(exc, LiveToolError | RpcError):
                _LOGGER.exception("Live Terminal close failed unexpectedly")
            reason = exc.message if isinstance(exc, LiveToolError | RpcError) else "It failed."
            if not stopped:
                done = f"{ref} may still be running: stopping it failed."
            elif isinstance(exc, LiveToolError) and exc.code == VOICE_STOPPED:
                done = f"{ref} was stopped but not removed."
            else:
                done = f"{ref} was stopped but may not have been removed."
            return live_failure(
                "partial", f"{done} {reason} Call overview to check before closing it again."
            )
        return live_success(await self._after_change(f"Closed {ref}: stopped and removed."))

    # -- shared -------------------------------------------------------------

    def _coding(self, terminal: JsonObject, tool: str) -> tuple[str, CodingProgram]:
        ref = self._refs.terminal(str(terminal["terminal_id"]))
        program = coding_program(terminal)
        if program is None:
            raise LiveToolError(
                "not_a_coding_terminal",
                f"{ref} does not run Codex or Claude Code; {tool} works only with coding "
                "Terminals. Use open to show it to the user.",
            )
        return ref, program

    async def _press(self, terminal: JsonObject, ref: str, key: str, *, tool: str) -> JsonObject:
        terminal_id = str(terminal["terminal_id"])
        label = _KEY_LABELS[key]
        for _ in range(_INPUT_ATTEMPTS):
            self._ctx.ensure_active()
            snapshot = await self._read(terminal_id)
            if snapshot.finished:
                raise LiveToolError("terminal_exited", f"{ref} has exited; no key was pressed.")
            try:
                await self._input(terminal_id, KEY_SEQUENCES[key], snapshot.revision)
            except RpcError as exc:
                if _STALE_SCREEN in exc.message:
                    continue
                return live_failure(
                    "key_failed", f"{label} was not pressed in {ref}: {exc.message}"
                )
            if tool == "stop":
                return live_success(
                    f"Pressed {label} in {ref} to interrupt it. Call read with {ref} to check that "
                    "it stopped."
                )
            return live_success(
                f"Pressed {label} in {ref}. Call read with {ref} to see the result."
            )
        return live_failure(
            "screen_busy",
            f"{ref} kept changing its screen, so {label} was not pressed. Call {tool} again.",
        )

    async def _type(self, terminal_id: str, program: CodingProgram, text: str) -> _Outcome:
        """Type *text* at the program's input line, then send Enter separately."""
        for _ in range(_INPUT_ATTEMPTS):
            self._ctx.ensure_active()
            snapshot = await self._read(terminal_id)
            if snapshot.finished:
                return _Outcome("exited")
            if not program_input_visible(program, snapshot.screen):
                return _Outcome("not_at_input", prompt=program_prompt(program, snapshot.screen))
            if snapshot.bracketed_paste:
                data = f"{_PASTE_START}{text}{_PASTE_END}"
            else:
                # Without paste mode each line break would send a line on its own.
                data = re.sub(r"[\r\n]+", " ", text)
            try:
                await self._input(terminal_id, data, snapshot.revision)
            except RpcError as exc:
                if _STALE_SCREEN not in exc.message:
                    raise
                continue
            break
        else:
            return _Outcome("busy")
        # Text and Enter in one burst only fill the input line.
        for _ in range(_INPUT_ATTEMPTS):
            await self._sleep(self._timings.enter_delay_seconds)
            self._ctx.ensure_active()
            snapshot = await self._read(terminal_id)
            if snapshot.finished or shell_prompt_visible(snapshot.screen):
                return _Outcome("typed_not_sent", detail="The program ended.")
            try:
                await self._input(terminal_id, KEY_SEQUENCES["enter"], snapshot.revision)
            except RpcError as exc:
                if _STALE_SCREEN in exc.message:
                    continue
                return _Outcome("typed_not_sent", detail=exc.message)
            return _Outcome("sent")
        return _Outcome("typed_not_sent", detail="Its screen kept changing.")

    async def _read(self, terminal_id: str) -> _Snapshot:
        snapshot = await self._ctx.call("terminal.read", {"terminal_id": terminal_id})
        summary = snapshot.get("terminal")
        summary = summary if isinstance(summary, dict) else {}
        revision = summary.get("screen_revision")
        return _Snapshot(
            screen=str(snapshot.get("screen") or ""),
            revision=revision
            if isinstance(revision, int) and not isinstance(revision, bool)
            else None,
            bracketed_paste=snapshot.get("bracketed_paste") is True,
            finished=summary.get("state") in _FINISHED_STATES,
        )

    async def _input(self, terminal_id: str, data: str, revision: int | None) -> None:
        params: JsonObject = {"terminal_id": terminal_id, "data": data}
        if revision is not None:
            # The manager rejects the input if the screen changed since this read.
            params["expected_screen_revision"] = revision
        await self._ctx.call("terminal.input", params)

    async def _show_quietly(self, op: str, **args: Any) -> bool:
        """Change the Terminals layout; ``False`` when the app did not."""
        try:
            self._ctx.ensure_active()
            await self._ctx.view(op, **args)
        except (LiveToolError, LiveUiError):
            return False
        except Exception:
            _LOGGER.exception("Live Terminal layout update failed unexpectedly")
            return False
        return True

    async def _after_change(self, text: str) -> str:
        """Refresh the Terminals layout after a completed change; the change stays reported."""
        if await self._show_quietly("refresh"):
            return text
        return f"{text} The app window did not update its Terminals view."


def _outcome(item: _Outcome | BaseException) -> _Outcome:
    if isinstance(item, _Outcome):
        return item
    if isinstance(item, LiveToolError):
        return _Outcome("stopped" if item.code == VOICE_STOPPED else "failed", detail=item.message)
    if isinstance(item, RpcError):
        return _Outcome("failed", detail=item.message)
    if isinstance(item, Exception):
        _LOGGER.error("Live task typing failed unexpectedly", exc_info=item)
        return _Outcome("uncertain")
    raise item


def _task_report(
    program: CodingProgram,
    refs: list[str],
    outcomes: list[_Outcome],
    *,
    timeout_seconds: float,
) -> str:
    """Sentences grouping the Terminals by what happened to the task."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for ref, outcome in zip(refs, outcomes, strict=True):
        kind = outcome.prompt.kind if outcome.prompt else ""
        groups.setdefault((outcome.status, kind, outcome.detail), []).append(ref)
    sentences = []
    for (status, kind, detail), members in groups.items():
        prompt = next((item for item in program.prompts if item.kind == kind), None)
        sentences.append(_task_sentence(program, members, status, prompt, detail, timeout_seconds))
    return " ".join(sentences)


def _task_sentence(
    program: CodingProgram,
    refs: list[str],
    status: str,
    prompt: CliPrompt | None,
    detail: str,
    timeout_seconds: float,
) -> str:
    who = join_words(refs)
    first = refs[0]
    later = f'send_message with {{"target": "{first}", "text": "<the task>"}}'
    # The next calls name the first Terminal; the others need the same.
    others = f" Do the same for {join_words(refs[1:])}." if len(refs) > 1 else ""
    if status == "sent":
        return f"Typed the task into {who} and sent it."
    if prompt is not None and status in {"prompt", "not_ready", "not_at_input"}:
        return f"{_prompt_sentence(program, who, first, prompt)} Afterwards, call {later}.{others}"
    if status == "not_ready":
        return (
            f"{who} did not show {program.label}'s input line within "
            f"{round(timeout_seconds)} seconds, so the task was not typed. "
            f"Call read with {first} to check it, then {later}.{others}"
        )
    if status == "exited":
        return f"{who} ended before {program.label} was ready; the task was not typed."
    if status in {"not_at_input", "busy"}:
        return (
            f"{who} did not show {program.label}'s input line, so the task was not typed. Call "
            f"read with {first}, then {later}.{others}"
        )
    if status == "typed_not_sent":
        return (
            f"The task was typed into {who} but not sent ({detail or 'Enter failed'}). Call "
            f'terminal with {{"action": "key", "target": "{first}", "key": "enter"}} to send '
            f"it.{others}"
        )
    if status == "stopped":
        return f"The voice call ended before the task was typed into {who}."
    if status == "uncertain":
        return f"Typing the task into {who} failed unexpectedly. {UNCERTAIN_DELIVERY}"
    return (
        f"The task was not typed into {who}: {detail} Call read with {first}, then {later}.{others}"
    )


def _prompt_sentence(program: CodingProgram, who: str, first: str, prompt: CliPrompt) -> str:
    keys = " then ".join(
        f'{{"action": "key", "target": "{first}", "key": "{key}"}}' for key in prompt.keys
    )
    if prompt.kind == "trust":
        return (
            f"{program.label} in {who} asks whether to trust the folder, so the task was not "
            f"typed. Ask the user; if they agree, call terminal with {keys}."
        )
    if prompt.kind == "update":
        return (
            f"{program.label} in {who} offers an update and waits, so the task was not typed. "
            f"Ask the user; to skip the update, call terminal with {keys}."
        )
    return (
        f"{program.label} in {who} asks a question, so the task was not typed. Call read with "
        f"{first} and ask the user."
    )


def _message_problem(ref: str, program: CodingProgram, outcome: _Outcome) -> str:
    if outcome.status == "exited":
        return f"{ref} has exited; nothing was sent."
    if outcome.prompt is not None:
        return (
            f"{program.label} in {ref} is asking a startup question, so nothing was sent. Call "
            f"read with {ref} and ask the user how to answer."
        )
    if outcome.status in {"not_at_input", "busy"}:
        return (
            f"{ref} does not show {program.label}'s input line right now (it may be showing a "
            f"menu or question), so nothing was sent. Call read with {ref} to see its screen."
        )
    return (
        f"The message was typed into {ref} but not sent ({outcome.detail or 'Enter failed'}). "
        f'Call terminal with {{"action": "key", "target": "{ref}", "key": "enter"}} to send it.'
    )


def _count_phrase(count: int, noun: str) -> str:
    return f"a {noun}" if count == 1 else f"{count} {noun}s"
