"""App access through computer_apps: requests as pending inputs, tiers, masking and expiry."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from tests.resources.extensions.computer_use.computer_use_test_support import (
    CHROME,
    GRAY,
    NOTEPAD,
    PAINT,
    SLACK,
    TERMINAL,
    Harness,
    color,
    images,
    model_text,
)

pytestmark = pytest.mark.asyncio


async def test_accepted_request_grants_each_app_at_its_category_tier(computer: Harness) -> None:
    task = asyncio.ensure_future(
        computer.call(
            "computer_apps",
            {
                "action": "request",
                "apps": ["notepad", "Chrome", "Windows Terminal"],
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
                "- Notepad: full control: mouse and keyboard\n"
                "- Google Chrome: view only: screenshots, no input\n"
                "- Windows Terminal: click only: clicks and scrolling, no typing or keys\n"
                "Reason: Copy the totals into the report\n"
                "The Agent moves the real mouse and keyboard while it works; other apps stay "
                "hidden from it. Access ends 30 minutes after its last Computer Use action in "
                "this Session. Accept to allow, or decline."
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
        "The user granted access in this Session:\n"
        "- Notepad: full control: mouse and keyboard\n"
        "- Google Chrome: view only: screenshots, no input\n"
        "- Windows Terminal: click only: clicks and scrolling, no typing or keys\n"
    )
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]
    changes = [change for change in computer.published if change[0] == "pending_inputs"]
    assert [ids for _, ids, _ in changes] == [[request["id"]], [request["id"]]]

    result = await computer.call("computer_apps", {"action": "request", "apps": ["Notepad"]})
    assert model_text(result).startswith("Already granted in this Session: Notepad (full control)")
    listing = model_text(await computer.call("computer_apps", {"action": "list", "query": "calc"}))
    assert listing.startswith(
        "Granted in this Session: Notepad (full control), Google Chrome (view only), "
        "Windows Terminal (click only).\nDisplays:\n"
        '1. "Main" 1280x720 (primary)\n2. "Wide" 3136x1000\n'
        "Running apps (access they get): File Explorer (full control), Google Chrome (view "
        "only), Notepad (full control), Paint (full control), Slack (full control), Windows "
        "Terminal (click only).\n"
        'Installed apps matching "calc": Calculator (full control), LibreOffice Calc (full '
        "control)."
    )


@pytest.mark.parametrize("answer", ["decline", "cancel"])
async def test_declined_request_grants_nothing(computer: Harness, answer: str) -> None:
    result = await computer.grant("Notepad", answer=answer)
    assert result["error"]["code"] == "access_declined"
    assert "do not request them again unless the user asks" in result["error"]["message"]
    result = await computer.computer(action="type", text="x")
    assert result["error"]["code"] == "access_required"


async def test_unanswered_request_counts_as_declined(computer: Harness) -> None:
    computer.service.access.timeout = 0.01
    result = await computer.call("computer_apps", {"action": "request", "apps": ["Notepad"]})
    assert result["error"]["code"] == "access_declined"
    assert "within 5 minutes" in result["error"]["message"]
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]


async def test_cancelled_run_withdraws_its_request(computer: Harness) -> None:
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
        (["vBot"], "vBot is part of vBot, which Computer Use never operates."),
    ],
)
async def test_unclear_app_names_are_not_requested(
    computer: Harness, apps: list[str], message: str
) -> None:
    result = await computer.call("computer_apps", {"action": "request", "apps": apps})
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]
    assert computer.api.operations.pending_inputs() == []  # type: ignore[misc]


async def test_request_asks_only_for_names_that_resolve(computer: Harness) -> None:
    task = asyncio.ensure_future(
        computer.call("computer_apps", {"action": "request", "apps": ["Notepad", "Wordpad"]})
    )
    request = await computer.pending()
    assert "- Notepad:" in request["payload"]["message"]
    assert "Wordpad" not in request["payload"]["message"]
    await computer.api.operations.invoke(
        "respond", {"request_id": request["id"], "response": {"action": "accept"}}
    )
    assert '"Wordpad" was not found' in model_text(await task)


@pytest.mark.parametrize(
    ("front", "arguments", "code", "message"),
    [
        (CHROME, {"action": "left_click", "coordinate": [800, 200]}, "access_tier", "view only"),
        (TERMINAL, {"action": "type", "text": "dir"}, "access_tier", "click only"),
        (
            TERMINAL,
            {"action": "left_click", "coordinate": [200, 600], "text": "ctrl"},
            "access_tier",
            "left_click with modifier keys was not sent",
        ),
        (SLACK, {"action": "type", "text": "x"}, "access_required", "belongs to Slack"),
        (
            NOTEPAD,
            {"action": "left_click", "coordinate": [800, 600]},
            "access_required",
            "The window at [800, 600] belongs to Slack, which is not granted in this Session, so "
            'left_click was not sent. Ask the user for it with computer_apps {"action":"request",'
            '"apps":["Slack"],"reason":"..."}.',
        ),
        (
            NOTEPAD,
            {"action": "left_click", "coordinate": [1250, 50]},
            "access_required",
            "belongs to vBot itself",
        ),
        (
            NOTEPAD,
            {"action": "left_click_drag", "start_coordinate": [200, 200], "coordinate": [800, 200]},
            "access_tier",
            "The window at [800, 200] belongs to Google Chrome, which is view only",
        ),
    ],
)
async def test_input_needs_the_tier_of_the_foreground_app_and_of_each_point(
    computer: Harness, front, arguments: dict, code: str, message: str
) -> None:
    await computer.grant("Notepad", "Google Chrome", "Windows Terminal")
    computer.target.front(front)
    computer.target.inputs.clear()
    result = await computer.computer(**arguments)
    assert result["error"]["code"] == code
    assert message in result["error"]["message"]
    assert computer.target.inputs == []


async def test_click_only_apps_take_plain_clicks_and_view_only_apps_take_screenshots(
    computer: Harness,
) -> None:
    await computer.grant("Google Chrome", "Windows Terminal")
    computer.target.front(TERMINAL)
    result = await computer.computer(action="left_click", coordinate=[200, 600])
    assert result["ok"]
    computer.target.front(CHROME)
    assert (await computer.computer(action="screenshot"))["ok"]
    assert (await computer.computer(action="zoom", region=[700, 100, 900, 200]))["ok"]
    assert computer.target.inputs == [("click", 200, 600, "left", 1, [])]


async def test_elevated_windows_refuse_input(computer: Harness) -> None:
    await computer.grant("Notepad")
    computer.target.windows_[0] = replace(computer.target.windows_[0], elevated=True)
    result = await computer.computer(action="type", text="x")
    assert result["error"]["code"] == "target_elevated"
    assert "runs as administrator" in result["error"]["message"]
    assert computer.target.inputs == []


async def test_screenshots_hide_every_window_of_apps_without_access(computer: Harness) -> None:
    await computer.grant("Notepad")
    context = computer.context_for("computer")
    result = await computer.call("computer", {"action": "screenshot"}, context)
    assert model_text(result).endswith(
        "Hidden apps (gray, not granted): Google Chrome, Windows Terminal, Slack, vBot, "
        "File Explorer."
    )
    (image,) = images(context)
    assert image.getpixel((300, 300)) == color(1)  # Notepad
    for point in ((800, 300), (300, 600), (800, 600), (1250, 50), (650, 50)):
        assert image.getpixel(point) == GRAY


async def test_grants_and_display_choice_expire_after_idle_time(computer: Harness) -> None:
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


async def test_open_brings_a_granted_app_forward_and_shows_its_display(computer: Harness) -> None:
    await computer.grant("Paint")
    context = computer.context_for("computer_apps")
    result = await computer.call("computer_apps", {"action": "open", "app": "paint"}, context)
    assert computer.target.opened == ["Paint"]
    assert computer.target.foreground().app == PAINT
    assert computer.sleeps[-1] == 1.5 and len(images(context)) == 1
    assert model_text(result).startswith('Opened Paint.\nScreenshot of display 2 of 2 "Wide"')

    result = await computer.call("computer_apps", {"action": "open", "app": "Slack"})
    assert result["error"]["code"] == "access_required"
    assert '{"action":"request","apps":["Slack"],"reason":"..."}' in result["error"]["message"]
    result = await computer.call("computer_apps", {"action": "open", "app": "Wordpad"})
    assert result["error"]["code"] == "invalid_arguments"
    assert computer.target.opened == ["Paint"]
