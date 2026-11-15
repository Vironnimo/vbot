"""Native Terminal contribution, loaded only by the local Desktop."""

from desktop.extensions import DesktopExtensionAPI, EventSink, ExecutionHandler


def _create(_emit: EventSink) -> ExecutionHandler:
    from desktop.terminal_execution import TerminalCapability

    return TerminalCapability()


def register(api: DesktopExtensionAPI) -> None:
    api.register_capability(
        "terminal",
        "Terminal",
        _create,
        description="Start and control local terminal programs. Disabling sharing stops them.",
    )
