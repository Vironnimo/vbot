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
