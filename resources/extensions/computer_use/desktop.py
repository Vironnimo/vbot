"""Native Computer Use contribution, loaded only by the local Desktop."""

import sys

from desktop.extensions import DesktopExtensionAPI, EventSink, ExecutionHandler


def _create(emit: EventSink) -> ExecutionHandler:
    from desktop.computer_execution import ComputerExecution

    return ComputerExecution(emit=emit)


def register(api: DesktopExtensionAPI) -> None:
    api.register_capability(
        "computer_use",
        "Computer Use",
        _create,
        description="See and operate this desktop with its mouse and keyboard.",
        available=sys.platform == "win32",
    )
