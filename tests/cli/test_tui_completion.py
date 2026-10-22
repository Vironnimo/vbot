"""Composer suggestions: what the draft offers and which key takes it."""

from __future__ import annotations

from typing import Any, override

import pytest
from textual.widgets import Static, TextArea

from cli.tui.app import ConversationApp
from cli.tui.completion import (
    CompletionBar,
    Suggestion,
    command_suggestions,
    file_suggestions,
    matching,
    model_suggestions,
    trigger_for,
)
from tests.cli.test_tui import Server

type JsonObject = dict[str, Any]


@pytest.mark.parametrize(
    ("text", "row", "column", "expected"),
    [
        ("/he", 0, 3, ("commands", "he", 0, 3)),
        ("/", 0, 1, ("commands", "", 0, 1)),
        ("/model gp", 0, 9, ("models", "gp", 7, 9)),
        # The WebUI keeps the Model popup out of any other slash text.
        ("/modeling", 0, 9, ("commands", "modeling", 0, 9)),
        ("use @src/ap", 0, 11, ("files", "src/ap", 4, 11)),
        ("look\n@src", 1, 4, ("files", "src", 0, 4)),
        ("/help again", 0, 11, None),
        ("mail@example.com", 0, 16, None),
        ("plain text", 0, 10, None),
        ("", 0, 0, None),
    ],
)
def test_trigger_for_recognizes_the_tokens_the_composer_completes(
    text: str, row: int, column: int, expected: tuple[str, str, int, int] | None
) -> None:
    trigger = trigger_for(text, row, column)

    assert (
        None if trigger is None else (trigger.kind, trigger.query, trigger.start, trigger.end)
    ) == expected


def test_matching_keeps_the_rows_that_carry_every_word() -> None:
    rows = [
        Suggestion("/model · Set the Model", "model", "command"),
        Suggestion("/help · Show the help", "help", "command"),
    ]

    assert [item.value for item in matching(rows, "")] == ["model", "help"]
    assert [item.value for item in matching(rows, "HELP")] == ["help"]
    assert [item.value for item in matching(rows, "model Set")] == ["model"]
    assert matching(rows, "model missing") == []


def test_a_command_without_an_argument_is_submitted_when_it_is_chosen() -> None:
    rows = command_suggestions(
        [
            {"name": "/help", "description": "Show it.", "type": "command", "argument": "none"},
            {
                "name": "model",
                "description": "Set it.",
                "type": "command",
                "argument": "required",
                "argument_hint": "[model] | reset",
            },
            {"name": "debugging", "description": "A Skill.", "type": "skill"},
        ]
    )

    assert [(row.value, row.kind, row.send) for row in rows] == [
        ("help", "command", True),
        ("model", "command", False),
        ("debugging", "skill", False),
    ]
    assert [row.label for row in rows[:2]] == [
        "/help · Show it.",
        "/model [model] | reset · Set it.",
    ]


def test_models_offer_the_reference_a_model_message_needs() -> None:
    rows = model_suggestions(
        [
            {"id": "openai/gpt-5.2", "name": "GPT-5.2"},
            {"provider_id": "groq", "model_id": "llama-4", "name": "Llama 4"},
            {"summary": "not a model"},
        ]
    )

    assert [(row.value, row.send) for row in rows] == [
        ("openai/gpt-5.2", True),
        ("groq/llama-4", True),
    ]


def test_files_offer_the_server_path_itself() -> None:
    assert file_suggestions(["src/app.py"]) == [Suggestion("src/app.py", "src/app.py", "file")]


def _bar(app: ConversationApp) -> CompletionBar:
    return app.query_one(CompletionBar)


@pytest.mark.asyncio
async def test_typing_a_slash_offers_commands_and_enter_runs_the_one_shown() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)

        editor.insert("/hel")
        await pilot.pause()
        assert _bar(app).has_class("visible")
        assert _bar(app).options.option_count == 1

        await pilot.press("enter")
        await server.sent.wait()
        await pilot.pause()

        stream = next(params for method, params in server.calls if method == "chat.stream")
        assert stream["content"] == "/help"
        assert editor.text == ""
        assert not _bar(app).has_class("visible")


@pytest.mark.asyncio
async def test_choosing_a_command_with_an_argument_completes_the_token_without_sending() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)

        editor.insert("/mod")
        await pilot.pause()
        assert _bar(app).options.option_count == 1

        await pilot.press("enter")
        await pilot.pause()

        assert editor.text == "/model "
        assert not server.sent.is_set()


@pytest.mark.asyncio
async def test_an_at_token_offers_server_files_and_enter_mentions_the_path() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)

        editor.insert("look at @first")
        await pilot.pause()
        assert _bar(app).has_class("visible")
        assert _bar(app).options.option_count == 1

        await pilot.press("enter")
        await pilot.pause()

        assert editor.text == "look at @first.txt "
        assert app.conversation is not None and app.conversation.mentions == ["first.txt"]
        assert not server.sent.is_set()


@pytest.mark.asyncio
async def test_the_model_argument_offers_models_and_choosing_one_sends_the_command() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)

        editor.insert("/model ")
        await pilot.pause()
        await pilot.pause()
        assert _bar(app).has_class("visible")

        await pilot.press("enter")
        await server.sent.wait()
        await pilot.pause()

        stream = next(params for method, params in server.calls if method == "chat.stream")
        assert stream["content"] == "/model openai/gpt-5.2"
        assert editor.text == ""


@pytest.mark.asyncio
async def test_the_arrows_walk_the_popup_and_escape_closes_it_until_the_draft_changes() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)

        # A wide query keeps both fixed files, so the highlight can move.
        editor.insert("@.txt")
        await pilot.pause()
        assert _bar(app).options.option_count == 2
        assert _bar(app).options.highlighted == 0

        await pilot.press("down")
        await pilot.pause()
        assert _bar(app).options.highlighted == 1

        await pilot.press("escape")
        await pilot.pause()
        assert not _bar(app).has_class("visible")

        # Any further edit offers again, as the WebUI recomputes on input.
        await pilot.press("backspace")
        await pilot.pause()
        assert _bar(app).has_class("visible")

        # Without a popup the arrows stay with the composer's cursor.
        editor.load_text("")
        editor.insert("first\nsecond")
        await pilot.pause()
        assert editor.cursor_location[0] == 1
        await pilot.press("up")
        await pilot.pause()
        assert not _bar(app).has_class("visible")
        assert editor.cursor_location[0] == 0


@pytest.mark.asyncio
async def test_enter_sends_and_shift_enter_keeps_a_new_line() -> None:
    server = Server()
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        editor = app.query_one("#composer", TextArea)

        # Shift+Enter reaches the composer and inserts a line instead of sending.
        editor.insert("first")
        await pilot.press("shift+enter")
        editor.insert("second")
        await pilot.pause()
        assert editor.text == "first\nsecond"
        assert not [method for method, _ in server.calls if method == "chat.stream"]

        # Enter sends what the composer holds, newline included.
        await pilot.press("enter")
        await pilot.pause()
        sends = [params for method, params in server.calls if method == "chat.stream"]
        assert sends == [
            {
                "agent_id": "main",
                "session_id": "session-one",
                "reply_surface": "tui",
                "content": "first\nsecond",
            }
        ]


@pytest.mark.asyncio
async def test_the_row_under_the_composer_names_the_project_and_its_directory() -> None:
    class ProjectServer(Server):
        @override
        async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
            if method == "project.list":
                return {
                    "projects": [
                        {
                            "project_id": "vbot",
                            "display_name": "vBot",
                            "cwd": "C:\\Development\\vBot",
                        }
                    ]
                }
            if method == "agent.list":
                return {
                    "agents": [
                        {
                            "id": "main",
                            "name": "Main",
                            "current_session_id": "session-one",
                            "effective": {
                                "model": {"value": "openai/gpt-5.2::api-key"},
                                "thinking_effort": {"value": "high"},
                            },
                        }
                    ]
                }
            if method == "session.get":
                return {
                    "session": {
                        "id": "session-one",
                        "title": "Test conversation",
                        "working_project_id": "vbot",
                    }
                }
            return await super().rpc(method, params)

    app = ConversationApp(ProjectServer(), agent_id="main", session_id="session-one")
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await pilot.pause()

        assert str(app.query_one("#settings-where", Static).content) == "vBot · C:/Development/vBot"
        model = app.query_one("#settings-model", Static)
        assert str(model.content) == "gpt-5.2 · high"
        # Its width follows its text, so it must have been laid out to be visible.
        assert model.region.width >= len("gpt-5.2 · high")
        assert model.region.right <= app.size.width
        # The Project no longer rides in the header, where the WebUI does not show it.
        assert str(app.query_one("#header-title", Static).content) == "Test conversation"
        assert str(app.query_one("#header-agent", Static).content) == "main"
