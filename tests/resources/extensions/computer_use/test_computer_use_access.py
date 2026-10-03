"""App access: every app by default; with ask_per_app, approvals per Session and masking."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.resources.extensions.computer_use.computer_use_test_support import (
    CHROME,
    GRAY,
    NOTEPAD,
    PAINT,
    TERMINAL,
    VBOT,
    Harness,
    color,
    images,
    model_text,
)

pytestmark = pytest.mark.asyncio


async def test_by_default_every_app_takes_input_without_approval(computer: Harness) -> None:
    for app, arguments in (
        (CHROME, {"action": "type", "text": "news"}),
        (TERMINAL, {"action": "key", "text": "ctrl+c"}),
        (VBOT, {"action": "left_click", "coordinate": [1250, 50], "text": "shift"}),
    ):
        computer.target.front(app)
        await computer.computer(action="screenshot", view="display")
        assert (await computer.computer(**arguments))["ok"]
    assert len(computer.target.inputs) == 3

    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "screenshot"}, context)
    assert "Hidden apps" not in model_text(result)
    (image,) = images(context)
    assert image.getpixel((800, 300)) == color(2)  # Google Chrome, not masked

    listing = model_text(await computer.call("computer_apps", {"action": "list"}))
    assert listing.startswith("Access: you may operate every app; no approval is needed.\n")
    assert "Running apps: File Explorer, Google Chrome, Notepad, Paint, Slack, vBot, " in listing
    result = await computer.call("computer_apps", {"action": "request", "apps": ["Slack"]})
    assert model_text(result).startswith("No approval is needed: you may operate every app.")
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]
    result = await computer.call("computer_apps", {"action": "open", "app": "paint"})
    assert model_text(result).startswith('Opened Paint.\nScreenshot of display 2 of 2 "Wide"')


@pytest.mark.parametrize("ask", [False, True])
async def test_elevated_windows_refuse_input(computer: Harness, ask: bool) -> None:
    if ask:
        computer.ask_per_app()
        await computer.grant("Notepad")
    computer.target.windows_[0] = replace(computer.target.windows_[0], elevated=True)
    result = await computer.computer(action="type", text="x")
    assert result["error"]["code"] == "target_elevated"
    assert "runs as administrator" in result["error"]["message"]
    assert computer.target.inputs == []


async def test_accepted_request_approves_apps_for_the_session(computer: Harness) -> None:
    computer.ask_per_app()
    task = asyncio.ensure_future(
        computer.call(
            "computer_apps",
            {
                "action": "request",
                "apps": ["notepad", "Chrome"],
                "reason": "Copy the totals into the report",
            },
        )
    )
    request = await computer.pending()
    assert request == {
        "id": request["id"],
        "kind": "computer_access",
        "payload": {
            "message": (
                'Agent "Helper" asks to use these apps on the vBot server\'s desktop:\n'
                "- Notepad\n"
                "- Google Chrome\n"
                "Reason: Copy the totals into the report\n"
                "The Agent moves the real mouse and keyboard while it works; apps you have not "
                "approved stay hidden from it. Approval ends 30 minutes after its last Computer "
                "Use action in this Session. Accept to allow, or decline."
            )
        },
        "session_id": "session",
    }
    answer = await computer.api.operations.invoke(
        "respond", {"request_id": request["id"], "response": {"action": "accept"}}
    )
    assert answer == {"id": request["id"], "answered": True}
    result = await task
    assert model_text(result).startswith(
        "The user approved Notepad, Google Chrome in this Session.\nNext: bring the app to the "
        'front with computer_apps {"action":"open","app":"Notepad"}'
    )
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]
    changes = [change for change in computer.published if change[0] == "pending_inputs"]
    assert [ids for _, ids, _ in changes] == [[request["id"]], [request["id"]]]

    result = await computer.call("computer_apps", {"action": "request", "apps": ["Notepad"]})
    assert model_text(result).startswith(
        "Already approved in this Session: Notepad, Google Chrome."
    )
    listing = model_text(await computer.call("computer_apps", {"action": "list", "query": "calc"}))
    assert listing.startswith(
        "Access: the user approves each app for this Session. Approved: Notepad, Google "
        'Chrome.\nDisplays:\n1. "Main" 1280x720 (primary)\n2. "Wide" 3136x1000\n'
        "Running apps: File Explorer (not approved), Google Chrome, Notepad, Paint (not "
        "approved), Slack (not approved), vBot (not approved), Windows Terminal (not approved).\n"
        'Installed apps matching "calc": Calculator (not approved), LibreOffice Calc (not '
        "approved)."
    )
    computer.target.front(CHROME)
    assert (await computer.computer(action="type", text="x"))["ok"]


@pytest.mark.parametrize("answer", ["decline", "cancel"])
async def test_declined_request_approves_nothing(computer: Harness, answer: str) -> None:
    computer.ask_per_app()
    result = await computer.grant("Notepad", answer=answer)
    assert result["error"]["code"] == "access_declined"
    assert "do not request them again unless the user asks" in result["error"]["message"]
    result = await computer.computer(action="type", text="x")
    assert result["error"]["code"] == "access_required"


async def test_unanswered_request_counts_as_declined(computer: Harness) -> None:
    computer.ask_per_app()
    computer.service.access.timeout = 0.01
    result = await computer.call("computer_apps", {"action": "request", "apps": ["Notepad"]})
    assert result["error"]["code"] == "access_declined"
    assert "within 5 minutes" in result["error"]["message"]
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]


async def test_cancelled_run_withdraws_its_request(computer: Harness) -> None:
    computer.ask_per_app()
    callbacks: list = []
    context = computer.context_for("computer_apps", cancel_registration_hook=callbacks.append)
    task = asyncio.ensure_future(
        computer.call("computer_apps", {"action": "request", "apps": ["Notepad"]}, context)
    )
    await computer.pending()
    for callback in callbacks:
        callback()
    result = await task
    assert result["error"]["code"] == "computer_use_interrupted"
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]
    with pytest.raises(ValueError, match="no longer exists"):
        await computer.api.operations.invoke(
            "respond", {"request_id": "req_missing", "response": {"action": "accept"}}
        )


@pytest.mark.parametrize(
    ("apps", "message"),
    [
        (["Calc"], '"Calc" matches several apps: Calculator, LibreOffice Calc.'),
        (["Notpad"], '"Notpad" was not found. Did you mean: Notepad?'),
    ],
)
async def test_unclear_app_names_are_not_requested(
    computer: Harness, apps: list[str], message: str
) -> None:
    computer.ask_per_app()
    result = await computer.call("computer_apps", {"action": "request", "apps": apps})
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]


async def test_request_asks_only_for_names_that_resolve(computer: Harness) -> None:
    computer.ask_per_app()
    task = asyncio.ensure_future(
        computer.call("computer_apps", {"action": "request", "apps": ["Notepad", "Wordpad"]})
    )
    request = await computer.pending()
    assert "- Notepad\n" in request["payload"]["message"]
    assert "Wordpad" not in request["payload"]["message"]
    await computer.api.operations.invoke(
        "respond", {"request_id": request["id"], "response": {"action": "accept"}}
    )
    assert '"Wordpad" was not found' in model_text(await task)


@pytest.mark.parametrize(
    ("front", "arguments", "message"),
    [
        (
            CHROME,
            {"action": "scroll", "scroll_direction": "down"},
            "The foreground window belongs to Google Chrome, which the user has not approved",
        ),
        (
            NOTEPAD,
            {"action": "left_click", "coordinate": [800, 600]},
            "The window at [800, 600] belongs to Slack, which the user has not approved in this "
            'Session, so left_click was not sent. Ask the user for it with computer_apps {"action":'
            '"request","apps":["Slack"],"reason":"..."}.',
        ),
        (
            NOTEPAD,
            {"action": "left_click_drag", "start_coordinate": [200, 200], "coordinate": [800, 200]},
            "The window at [800, 200] belongs to Google Chrome",
        ),
    ],
)
async def test_asking_refuses_input_unless_the_foreground_app_and_each_point_are_approved(
    computer: Harness, front, arguments: dict, message: str
) -> None:
    computer.ask_per_app()
    await computer.grant("Notepad")
    computer.target.front(front)
    await computer.computer(action="screenshot", view="display")
    computer.target.inputs.clear()
    result = await computer.computer(**arguments)
    assert result["error"]["code"] == "access_required"
    assert message in result["error"]["message"]
    assert computer.target.inputs == []


async def test_asking_hides_every_window_of_apps_not_approved(computer: Harness) -> None:
    computer.ask_per_app()
    await computer.grant("Notepad")
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "screenshot", "view": "display"}, context)
    assert model_text(result).endswith(
        "Hidden apps (gray, not approved): Google Chrome, Windows Terminal, Slack, vBot, "
        "File Explorer."
    )
    (image,) = images(context)
    assert image.getpixel((300, 300)) == color(1)  # Notepad
    for point in ((800, 300), (300, 600), (800, 600), (1250, 50), (650, 50)):
        assert image.getpixel(point) == GRAY


async def test_approvals_and_display_choice_expire_after_idle_time(computer: Harness) -> None:
    computer.ask_per_app()
    now = [1000.0]
    computer.service.sessions.clock = lambda: now[0]
    await computer.grant("Notepad")
    await computer.computer(action="screenshot", display="Wide")
    now[0] += 29 * 60
    assert '"Wide"' in model_text(await computer.computer(action="screenshot"))
    now[0] += 31 * 60
    assert '"Main"' in model_text(await computer.computer(action="screenshot"))
    result = await computer.computer(action="type", text="x")
    assert result["error"]["code"] == "access_required"


async def test_asking_opens_only_approved_apps(computer: Harness) -> None:
    computer.ask_per_app()
    await computer.grant("Paint")
    context = computer.context_for("computer_apps")
    result = await computer.call("computer_apps", {"action": "open", "app": "paint"}, context)
    assert computer.target.opened == ["Paint"]
    assert computer.target.foreground().app == PAINT
    assert computer.sleeps[-1] == 1.5 and len(images(context)) == 1

    result = await computer.call("computer_apps", {"action": "open", "app": "Slack"})
    assert result["error"]["code"] == "access_required"
    assert '{"action":"request","apps":["Slack"],"reason":"..."}' in result["error"]["message"]
    result = await computer.call("computer_apps", {"action": "open", "app": "Wordpad"})
    assert result["error"]["code"] == "invalid_arguments"
    assert computer.target.opened == ["Paint"]
