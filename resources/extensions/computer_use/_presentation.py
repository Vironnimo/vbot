"""Desktop app listing and failure presentation; no desktop-control ownership."""

from __future__ import annotations

from typing import Any

from ._actions import Action
from ._desktop import CallRefusedError, Desktop
from ._screens import describe_displays
from .target import AppInfo, InputInterrupted, TargetError

_LIST_LIMIT = 25
_STOPPED_ADVICE = (
    "Computer Use stays off until the user's next message: do not try to operate the computer "
    "again in this reply. Tell the user what you did and what is left."
)


def _listing(desktop: Desktop, query: Any) -> str:
    """The text of ``computer_apps list``; runs on the desktop worker thread."""
    target, access = desktop.target, desktop.access
    apps = target.apps()
    if not access.ask:
        lines = ["Access: you may operate every app; no approval is needed."]
    elif access.state.grants:
        lines = [
            "Access: the user approves each app for this Session. Approved: "
            f"{access.state.describe()}."
        ]
    else:
        lines = [
            "Access: the user approves each app for this Session, and none is approved yet. "
            'Ask with computer_apps {"action":"request","apps":["..."],"reason":"..."}.'
        ]
    lines.append("Displays:\n" + describe_displays(target.displays(), access.state.shown))

    def described(app: AppInfo) -> str:
        return app.name if access.allows(app) else f"{app.name} (not approved)"

    running = sorted({app.name: app for app in apps if app.running}.values(), key=_name)
    lines.append("Running apps: " + (", ".join(described(app) for app in running) or "none") + ".")
    if isinstance(query, str) and query.strip():
        text = query.strip().casefold()
        found = sorted(
            {app.name: app for app in apps if text in app.name.casefold()}.values(), key=_name
        )
        shown = ", ".join(described(app) for app in found[:_LIST_LIMIT])
        more = (
            f", and {len(found) - _LIST_LIMIT} more; use a longer query"
            if len(found) > _LIST_LIMIT
            else ""
        )
        lines.append(f'Installed apps matching "{query.strip()}": {shown or "none"}{more}.')
    else:
        lines.append('Search installed apps with {"action":"list","query":"..."}.')
    return "\n".join(lines)


def _name(app: AppInfo) -> str:
    return app.name.casefold()


def _sent_text(desktop: Desktop | None) -> str:
    if desktop is not None and desktop.failure_screen is not None:
        return "Input may have been sent."
    if desktop is not None and desktop.input_started:
        return "Input may have been sent; take a screenshot before repeating it."
    return "No input was sent."


def _with_screen(message: str, desktop: Desktop | None) -> str:
    """*message* followed by the screenshot taken after the failure, if any."""
    if desktop is None or desktop.failure_screen is None:
        return message
    return f"{message}\nThe screen now: {desktop.failure_screen}"


def _target_text(error: TargetError, desktop: Desktop | None) -> str:
    message = str(error).rstrip()
    if error.code == "computer_use_failed":
        return f"{message} {_sent_text(desktop)}"
    return message


def _batch_error(
    error: CallRefusedError | TargetError,
    actions: list[Action],
    number: int,
    lines: list[str],
    stopped: str,
    shot: str | None = None,
) -> Exception:
    """The error of a batch stopped at action *number*, naming the steps that ran.

    *shot* is the text of a screenshot taken after the failure, which the
    caller appends to the message.
    """
    action = actions[number - 1]
    ran = (
        "Actions that ran"
        + ("" if shot else " (take a screenshot to see the current state)")
        + ":\n"
        + "\n".join(lines)
        if lines
        else "No action ran before it."
    )
    rest = len(actions) - number
    skipped = f" The remaining {rest} did not run." if rest else ""
    if isinstance(error, InputInterrupted):
        message = (
            f"{stopped} during action {number} of {len(actions)} ({action.describe()}).{skipped} "
            f"Held keys and buttons were released. {_STOPPED_ADVICE}\n{ran}"
        )
        return TargetError(message, "computer_use_interrupted")
    detail = str(error).rstrip()
    if isinstance(error, TargetError) and error.code == "computer_use_failed":
        detail += " Input may have been sent." if action.sends_input else " No input was sent."
    message = f"Action {number} of {len(actions)} ({action.name}) failed: {detail}{skipped}\n{ran}"
    if isinstance(error, CallRefusedError):
        return CallRefusedError(error.code, message)
    return TargetError(message, error.code)
