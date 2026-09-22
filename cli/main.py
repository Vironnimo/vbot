"""Lightweight process entrypoint; command routing loads only when needed."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Resolve this checkout before an installed package for python cli/main.py.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if TYPE_CHECKING:
    from cli._commands import (
        _agent_changes_from_args,
        _channel_changes_from_args,
        _model_filters_from_args,
        _project_add_fields_from_args,
        _project_set_changes_from_args,
        exit_code_for,
        parse_args,
        print_channel_command_result,
        print_command_result,
        print_management_command_result,
        print_update_command_result,
        print_update_command_start,
        run,
    )


def __getattr__(name: str) -> Any:
    """Keep command helpers available without loading them during GUI startup."""
    if name.startswith("__"):
        raise AttributeError(name)
    from cli import _commands

    return getattr(_commands, name)


def main(argv: Sequence[str] | None = None) -> None:
    """Route the standard native GUI shortcut before loading console commands."""
    _configure_console_output()
    arguments = list(sys.argv[1:] if argv is None else argv)
    # The installer owns exactly this shortcut. All other invocations retain
    # the complete CLI parser, output policy and argument validation.
    if Path(sys.executable).name.casefold() == "vbot.gui.exe" and arguments == ["desktop"]:
        from cli.application.desktop import open_desktop
        from cli.application.state import discover

        try:
            install = discover()
            if install is not None:
                open_desktop(install)
                sys.exit(0)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            sys.exit(1)
    from cli._commands import run

    sys.exit(run(argv))


def _configure_console_output() -> None:
    """Emit deterministic UTF-8 without crashing on legacy Windows code pages."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            # Imported/test streams may expose ``reconfigure`` while refusing an
            # encoding change. CLI output still uses the stream's own contract.
            continue


__all__ = [
    "main",
    "run",
    "parse_args",
    "exit_code_for",
    "print_command_result",
    "print_management_command_result",
    "print_channel_command_result",
    "print_update_command_result",
    "print_update_command_start",
    "_agent_changes_from_args",
    "_channel_changes_from_args",
    "_model_filters_from_args",
    "_project_add_fields_from_args",
    "_project_set_changes_from_args",
]


if __name__ == "__main__":
    main()
