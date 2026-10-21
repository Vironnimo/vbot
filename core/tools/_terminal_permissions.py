"""Permission Rules for the terminal Tool: start, input at a shell prompt, attach.

A restricted Session's terminal is checked where vBot can see what runs:

- ``start``: the program and its arguments as one command (``CommandChecker``)
  and its folder as the start directory. An interactive shell started with
  vBot's shell integration runs nothing by itself, so only its folder is
  checked, like the shell Tool; a shell without that integration is refused,
  since the lines typed at its prompt could not be checked.
- ``input`` while the terminal shows an integrated shell prompt
  (``_terminal_shell_state``): each command line it submits, with what the
  Agent typed there before, is checked as a line of that shell in the folder
  the shell reported. Keys that change the line in ways vBot cannot follow
  (completion, history, cursor movement) are refused there. Input to a
  running program passes unchecked.
- ``attach`` (and the screen ``status`` shows): only terminals whose
  Permission Rules are the Session's own or narrower, recorded as the
  ``permission_scope`` of the layers when the terminal started.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from core.permissions import (
    CommandChecker,
    PermissionBoundary,
    PermissionDenied,
    PermissionRefusal,
)

from ._terminal_launch import integrated_shell_kind, starts_shell_prompt
from ._terminal_shell_state import ShellState
from ._terminal_state import TERMINAL_INPUT_KEY_SEQUENCES, TerminalInfo

if TYPE_CHECKING:
    from core.permissions.commands.commands import SimpleCommand

ShellKind = Literal["powershell", "cmd", "bash"]

_SHOWN_CHARS: Final = 300
# Environment variables a typed line is analyzed with. Others are unknown,
# since the shell may have changed them at an earlier prompt.
_LINE_ENVIRONMENT: Final = ("PATH", "PATHEXT")
_UNCHECKED_SHELL: Final = (
    "vBot cannot check the command lines typed at its prompt: it starts without "
    "vBot's shell integration"
)
_UNCHECKED_SHELL_HINT: Final = (
    "Start the terminal without command for the default shell, whose lines are checked, "
    "or start one program with command and args."
)
_NESTED_SHELL: Final = "the shell it starts would take the next lines at a prompt vBot cannot check"
_NESTED_SHELL_HINT: Final = (
    "Run the commands in this shell, or start one program with command and args."
)
_NO_FOLDER: Final = (
    "the shell did not report a folder vBot can read, so the check cannot tell which "
    "folder the line works in"
)
_NO_FOLDER_HINT: Final = 'Start a new terminal with terminal {"action": "start"}.'
_NO_CHECKER: Final = "this vBot cannot analyze commands to check them"
_STARTED_OUTSIDE: Final = (
    "it was started outside this Session's permission rules (by the user, or by a "
    "Session with other rules)"
)
_SUBMIT: Final = frozenset("\r\n")
_ERASE: Final = frozenset("\x7f\b")
_CANCEL: Final = "\x03"
_END: Final = "\x04"


def permission_scope(boundary: PermissionBoundary) -> frozenset[str] | None:
    """The layers of a restricted *boundary* as a terminal keeps them; None for full access.

    Who set a layer does not matter: the same rules from the Agent or a ceiling
    limit the same way.
    """
    if not boundary.restricted:
        return None
    return frozenset(layer.fingerprint for layer in boundary.layers)


def may_attach(boundary: PermissionBoundary, info: TerminalInfo) -> bool:
    """Whether a Session with *boundary* may attach, and so read and type into, *info*.

    Every layer of the Session must have limited the terminal's start too: then
    the terminal's boundary is the Session's own or narrower.
    """
    scope = permission_scope(boundary)
    if scope is None:
        return True
    return info.permission_scope is not None and scope <= info.permission_scope


def attach_refused_message(terminal_id: str) -> str:
    return (
        f"terminal was not run: the permission rules refuse attaching {terminal_id}, because "
        f"{_STARTED_OUTSIDE}. The user set these rules for this Agent. To work in a terminal, "
        'start one with terminal {"action": "start"}.'
    )


def screen_hidden_note() -> str:
    return (
        f"Its screen and history are not shown: {_STARTED_OUTSIDE}, and the permission "
        "rules let this Session read only terminals started under them."
    )


async def check_start(
    checker: CommandChecker | None,
    boundary: PermissionBoundary,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> PermissionRefusal | None:
    """The refusal of starting *argv* in *cwd* for a restricted Session, or None."""
    if not boundary.restricted:
        return None
    if integrated_shell_kind(argv) is not None:
        return boundary.check_path(cwd, "start_dir")
    shown = _shown(" ".join(argv))
    if starts_shell_prompt(argv):
        return boundary.refusal(
            "command", shown, "unanalyzable", detail=_UNCHECKED_SHELL, hint=_UNCHECKED_SHELL_HINT
        )
    if checker is None:
        return boundary.refusal("command", shown, "unanalyzable", detail=_NO_CHECKER)
    return await checker.check_argv(boundary, argv, cwd=cwd, env=env, home=Path.home())


type _Piece = tuple[str, Literal["data", "text", "key"]]


@dataclass(frozen=True, slots=True)
class TypedInput:
    """What one input does to the line at an integrated shell prompt.

    ``check`` is the text to check before the input is sent: every line it
    submits and the rest of the line, joined by line breaks (None: it submits
    nothing, or only empty lines). Whether text is pasted as one block is
    known only once it is sent, so the check counts each line break as Enter,
    which checks at least what can run.
    """

    check: str | None
    state: ShellState
    shell: ShellKind
    prompt: int
    before: str
    pieces: tuple[_Piece, ...]

    def sent(self, *, pasted: bool) -> None:
        """Record what the shell holds after the input was sent (*pasted*: text as one block)."""
        lines, line, _ = _follow(self.before, self.pieces, pasted=pasted)
        if lines:
            # A shell that continues an incomplete line keeps all of it at this
            # prompt; one that ran the lines reads the rest at a later prompt.
            joined = "\n".join([*lines, line])
            self.state.typed(self.prompt, joined, carry=line, submitted=len(lines))
            if self.shell == "cmd":
                # cmd marks no command start (OSC 133 C).
                self.state.line_submitted(self.prompt)
        else:
            # Typed before the shell's first prompt, the line reaches that prompt.
            first = self.prompt == 0
            self.state.typed(self.prompt, line, carry=line if first else "", submitted=int(first))


@dataclass(frozen=True, slots=True)
class UnfollowableInput:
    """Input at a prompt that changes the line in a way the check cannot follow."""

    what: str

    def message(self) -> str:
        return (
            "terminal was not run: the permission rules check each command line typed at this "
            f"shell prompt, and {self.what} would change the line in a way the check cannot "
            'follow. Type the whole command as text and submit it with key "enter"; to clear '
            'the line first, send key "ctrl_c".'
        )


def typed_input(
    state: ShellState,
    shell: ShellKind,
    *,
    data: str | None,
    text: str | None,
    key: str | None,
) -> TypedInput | UnfollowableInput | None:
    """How *data*, or *text* and *key*, change the line at the prompt; None off a prompt."""
    if not state.at_prompt:
        return None
    pieces: list[_Piece] = []
    if data is not None:
        pieces.append((data, "data"))
    if text is not None:
        pieces.append((text, "text"))
    if key is not None:
        sequence = TERMINAL_INPUT_KEY_SEQUENCES.get(key, "")
        if len(sequence) != 1:
            return UnfollowableInput(f'key "{key}"')
        pieces.append((sequence, "key"))
    before = state.line()
    lines, line, unfollowable = _follow(before, pieces, pasted=False)
    if unfollowable is not None:
        if key is not None and unfollowable == pieces[-1][0]:
            return UnfollowableInput(f'key "{key}"')
        return UnfollowableInput(f"the control character U+{ord(unfollowable):04X}")
    check = "\n".join([*lines, line] if line else lines) if lines else None
    return TypedInput(
        check=check if check is not None and check.strip() else None,
        state=state,
        shell=shell,
        prompt=state.prompt_count,
        before=before,
        pieces=tuple(pieces),
    )


async def check_line(
    checker: CommandChecker | None,
    boundary: PermissionBoundary,
    typed: TypedInput,
) -> PermissionRefusal | None:
    """The refusal of the command lines *typed* submits at its prompt, or None."""
    line = typed.check
    if line is None or not boundary.restricted:
        return None
    shown = _shown(line)
    if typed.state.cwd is None:
        return boundary.refusal(
            "command", shown, "unanalyzable", detail=_NO_FOLDER, hint=_NO_FOLDER_HINT
        )
    if checker is None:
        return boundary.refusal("command", shown, "unanalyzable", detail=_NO_CHECKER)
    environment = {name: os.environ[name] for name in _LINE_ENVIRONMENT if name in os.environ}
    return await checker.check_line(
        boundary,
        line,
        shell=typed.shell,
        cwd=Path(typed.state.cwd),
        env=environment,
        home=Path.home(),
        refuse_command=_nested_shell,
    )


def _nested_shell(command: SimpleCommand) -> tuple[str, str] | None:
    if starts_shell_prompt([command.name, *(word.text for word in command.words)]):
        return _NESTED_SHELL, _NESTED_SHELL_HINT
    return None


def _follow(
    line: str, pieces: Sequence[_Piece], *, pasted: bool
) -> tuple[list[str], str, str | None]:
    """The lines *pieces* submit after *line*, the line left, and the first
    character the check cannot follow (or None)."""
    lines: list[str] = []
    for value, kind in pieces:
        for character in value.replace("\r\n", "\n"):
            if character in _SUBMIT:
                if kind == "text" and pasted:
                    line += "\n"
                else:
                    lines.append(line)
                    line = ""
            elif character in _ERASE:
                line = line[:-1]
            elif character == _CANCEL:
                line = ""
            elif character == _END and not line:
                # Ends the shell at an empty line.
                continue
            elif character < " " or "\x7f" <= character < "\xa0":
                return lines, line, character
            else:
                line += character
    return lines, line, None


def _shown(text: str) -> str:
    shown = " ".join(text.split())
    return shown if len(shown) <= _SHOWN_CHARS else shown[: _SHOWN_CHARS - 3] + "..."


async def check_input(
    checker: CommandChecker | None,
    boundary: PermissionBoundary,
    state: ShellState | None,
    shell: ShellKind | None,
    *,
    data: str | None,
    text: str | None,
    key: str | None,
) -> TypedInput | UnfollowableInput | None:
    """Check input to a terminal before it is sent; raises ``PermissionDenied``.

    Returns the ``TypedInput`` to record once sent (``sent``), the input a
    restricted Session may not send at this prompt, or None: no integrated
    shell prompt, or input whose effect is not followed.
    """
    if state is None or shell is None:
        return None
    typed = typed_input(state, shell, data=data, text=text, key=key)
    if isinstance(typed, UnfollowableInput) and not boundary.restricted:
        state.forget()
        return None
    if isinstance(typed, TypedInput):
        refused = await check_line(checker, boundary, typed)
        if refused is not None:
            raise PermissionDenied(refused)
    return typed
