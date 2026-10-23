"""Interactive accessor launch, isolated from ordinary CLI imports and commands."""

from __future__ import annotations

import argparse
import asyncio
import sys

from cli._server_target import ServerInstance


def dispatch_tui_command(args: argparse.Namespace, instance: ServerInstance) -> int:
    """Open the TUI only on an interactive input and output terminal."""
    if not all(stream is not None and stream.isatty() for stream in (sys.stdin, sys.stdout)):
        print("Error: vbot tui requires an interactive terminal. Use vbot chat for piped input.")
        return 2
    from core.config_validation import JsonConfigValidationError

    try:
        from cli.tui import run_tui

        return asyncio.run(
            run_tui(
                target=instance,
                agent_id=args.agent,
                session_id=args.session,
                project_id=args.project,
            )
        )
    except ModuleNotFoundError as error:
        if error.name != "textual" and not str(error.name).startswith("textual."):
            raise
        print("Error: the terminal UI dependency is missing. Install vBot with its cli extra.")
        return 1
    except KeyboardInterrupt:
        return 0
    except (JsonConfigValidationError, OSError) as error:
        print(f"Error: {error}")
        return 1


def open_tui_for_chat(args: argparse.Namespace, instance: ServerInstance) -> int:
    """Open the TUI for ``vbot chat`` without a message on an interactive terminal.

    ``-c`` opens the Agent's most recently active conversation, or a new one
    when it has none, like ``vbot chat -c`` does.
    """
    session_id = args.session
    if args.continue_latest:
        from cli._output import print_chat_command_result
        from cli.chat_management import latest_conversation
        from cli.server_management import CommandResult

        latest = latest_conversation(instance, args.agent)
        if isinstance(latest, CommandResult):
            print_chat_command_result(latest)
            return 1
        session_id = latest
    return dispatch_tui_command(
        argparse.Namespace(agent=args.agent, session=session_id, project=args.working_project),
        instance,
    )
