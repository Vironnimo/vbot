"""F4 Model and reasoning choices follow the vBot roster and Model RPCs."""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest
from textual.widgets import TextArea

from cli.tui.app import ConversationApp
from cli.tui.dialogs import Picker
from tests.cli.test_tui import Server

type JsonObject = dict[str, Any]


class ModelServer(Server):
    @override
    def __init__(
        self, *, address: str = "main", session_overrides: JsonObject | None = None
    ) -> None:
        super().__init__()
        self.address = address
        self.session_overrides = session_overrides or {}
        self.model_get_requests: list[str] = []
        self.model_list_requests = 0
        self.override_updates: list[JsonObject] = []
        self.effective_model = "openai/gpt-5.2::preferred:account-one"
        self.effective_effort = "high"
        self.models = [
            {
                "id": "openai/gpt-5.2",
                "provider_id": "openai",
                "model_id": "gpt-5.2",
                "name": "GPT-5.2",
            },
            {
                "id": "anthropic/claude-sonnet-4",
                "provider_id": "anthropic",
                "model_id": "claude-sonnet-4",
                "name": "Claude Sonnet 4",
            },
        ]

    def _agent_list(self) -> JsonObject:
        return {
            "agents": [
                {
                    "id": "main",
                    "name": "Main",
                    "effective": {
                        "model": {"value": self.effective_model, "source": "agent"},
                        "thinking_effort": {
                            "value": self.effective_effort,
                            "source": "agent",
                        },
                    },
                }
            ]
        }

    def _project_list(self) -> JsonObject:
        member: JsonObject = {
            "agent_id": "worker",
            "display_name": "Worker",
            "effective": {
                "model": {"value": self.effective_model, "source": "agent"},
                "thinking_effort": {"value": self.effective_effort, "source": "agent"},
            },
        }
        return {
            "projects": [
                {
                    "project_id": "project-one",
                    "display_name": "Project One",
                    "cwd": ".",
                    "scan": {"team": [member]},
                }
            ]
        }

    def _model_detail(self) -> JsonObject:
        return {
            "model": {
                "capabilities": {
                    "reasoning": {
                        "supported": True,
                        "control": "levels",
                        "mandatory": False,
                    }
                },
                "usable_connections": ["preferred", "first"],
                "wire_profiles": {
                    "preferred": {"reasoning_ladder": ["minimal", "high", "ultra", "none"]},
                    "first": {"reasoning_ladder": ["low", "max"]},
                },
            }
        }

    @override
    async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
        params = params or {}
        if method == "agent.list":
            return self._agent_list()
        if method == "project.list":
            return self._project_list()
        if method == "model.list":
            self.model_list_requests += 1
            return {"models": self.models}
        if method == "model.get":
            self.model_get_requests.append(params["model"])
            return self._model_detail()
        if method == "session.get":
            return {
                "session": {
                    "id": params["session_id"],
                    "title": "Existing Session",
                    "working_project_id": "project-one",
                    "agent_overrides": self.session_overrides,
                }
            }
        if method == "session.set_agent_overrides":
            self.override_updates.append(params["agent_overrides"])
            updated = dict(self.session_overrides)
            for field, value in params["agent_overrides"].items():
                if value is None:
                    updated.pop(field, None)
                else:
                    updated[field] = value
            return {"agent_overrides": updated}
        return await super().rpc(method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["main", "worker@project-one"])
async def test_thinking_choices_use_effective_connection_ladder(
    address: str,
) -> None:
    server = ModelServer(address=address)
    app = ConversationApp(server, agent_id=address)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#composer", TextArea).text == ""

        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert "Model openai/gpt-5.2::preferred" in app.screen.heading
        assert "effort high" in app.screen.heading

        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert server.model_get_requests == ["openai/gpt-5.2::preferred:account-one"]
        assert app.screen.choices == [
            ("Provider default", ""),
            ("minimal", "minimal"),
            ("high · current", "high"),
            ("none", "none"),
        ]

        await pilot.press("escape")
        await pilot.pause()
        assert app.conversation is not None
        assert app.conversation.overrides == {}


@pytest.mark.asyncio
async def test_unusable_pinned_connection_does_not_borrow_another_ladder() -> None:
    server = ModelServer()
    server.effective_model = "openai/gpt-5.2::missing:account-one"
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f4")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()

        assert isinstance(app.screen, Picker)
        assert server.model_get_requests == ["openai/gpt-5.2::missing:account-one"]
        assert app.screen.choices == [
            ("Provider default · pinned Connection missing unavailable", "")
        ]

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_model_picker_marks_the_effective_model() -> None:
    server = ModelServer()
    server.models.extend(
        [
            {"provider_id": "groq", "model_id": "llama-4", "name": "Llama 4"},
            {"summary": "not a model"},
        ]
    )
    app = ConversationApp(server, agent_id="main")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(app.screen, Picker)

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert server.model_list_requests == 1
        assert app.screen.choices == [
            ("GPT-5.2 · openai/gpt-5.2 · current", "openai/gpt-5.2"),
            ("Claude Sonnet 4 · anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4"),
            ("Llama 4 · groq/llama-4", "groq/llama-4"),
        ]

        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_effort", [None, "high"])
async def test_session_effort_default_uses_captured_model_and_saves_explicit_default(
    initial_effort: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_model = "openai/gpt-5.2::preferred:account-one"
    server = ModelServer(
        address="worker@project-one",
        session_overrides={
            "model": captured_model,
            **(
                {"thinking_effort": initial_effort}
                if initial_effort
                else {"thinking_effort_default": True}
            ),
        },
    )
    app = ConversationApp(server, agent_id="worker@project-one", session_id="session-one")
    read_started, release_read = asyncio.Event(), asyncio.Event()
    refresh: asyncio.Task[None] | None = None
    rpc = server.rpc

    async def with_pending_read(method: str, params: JsonObject | None = None) -> JsonObject:
        nonlocal refresh
        if method == "session.set_agent_overrides":
            assert app.conversation is not None
            refresh = asyncio.create_task(app.navigation.refresh(app.conversation))
            await read_started.wait()
        response = await rpc(method, params)
        if method == "session.get" and refresh is not None:
            read_started.set()
            await release_read.wait()
        return response

    monkeypatch.setattr(server, "rpc", with_pending_read)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert f"Model {captured_model}" in app.screen.heading
        assert f"effort {initial_effort or 'Provider default'}" in app.screen.heading

        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert server.model_get_requests == [captured_model]
        assert app.screen.choices[0] == (
            "Provider default" if initial_effort else "Provider default · current",
            "",
        )

        await pilot.press("enter")
        await pilot.pause()
        assert server.override_updates == [
            {"thinking_effort": None, "thinking_effort_default": True}
        ]
        release_read.set()
        assert refresh is not None
        await refresh
        assert app.conversation is not None
        assert app.conversation.overrides == {
            "model": captured_model,
            "thinking_effort_default": True,
        }
