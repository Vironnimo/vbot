"""Shared setup contracts through Textual's mounted keyboard and button surface."""

from __future__ import annotations

import copy
from contextlib import nullcontext
from pathlib import Path
from typing import Any, override
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Checkbox, Input, OptionList, SelectionList, Static

from cli._server_target import ServerInstance
from cli.tui.app import ConversationApp
from cli.tui.setup import SetupApp, SetupScreen
from cli.tui.setup_services import ServicePanel
from cli.tui.transport import ClientError, TerminalClient

type JsonObject = dict[str, Any]


class SetupServer(TerminalClient):
    @override
    def __init__(self, step: str = "provider") -> None:
        self.state = {"status": "pending", "step": step, "agent_id": "first", "auto_show": True}
        self.target = ServerInstance(
            "127.0.0.1", 1234, Path("."), "http://127.0.0.1:1234", Path("log")
        )
        self.calls: list[tuple[str, JsonObject]] = []
        self.model = ""
        self.fail_method = ""
        self.connected = False
        self.connection_type = "api_key"
        self.extension_settings = {"disabled": ["kept-off"], "config": {"example": {"a": 1}}}
        self.service: JsonObject = {
            "state": "idle",
            "message": "Not installed",
            "installed": False,
            "plan": {
                "id": "plan-1",
                "steps": [{"id": "runtime", "label": "Install Podman"}],
                "requires_prerequisites": True,
                "can_install": True,
            },
            "actions": ["install"],
        }

    @override
    async def rpc(self, method: str, params: JsonObject | None = None) -> JsonObject:
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method == self.fail_method:
            raise ClientError("Test refusal", code="test_refusal")
        if method == "setup.get":
            return dict(self.state)
        if method == "setup.update":
            self.state["step"] = params["step"]
            return dict(self.state)
        if method == "setup.finish":
            self.state.update(status="completed" if self.model else "skipped", auto_show=False)
            return dict(self.state)
        if method == "settings.get":
            return {
                "providers": {
                    "items": [
                        {
                            "id": "test",
                            "connections": [
                                {
                                    "id": "test:api",
                                    "label": "Test API",
                                    "type": self.connection_type,
                                    "connectable": True,
                                }
                            ],
                        }
                    ]
                }
            }
        if method == "provider.connect":
            if self.connection_type == "external":
                return {"connected": False, "setup_command": "test login"}
            return {"verification_uri": "https://login.test", "user_code": "TEST"}
        if method == "provider.connection_status":
            self.connected = True
            return {"connected": True}
        if method == "provider.set_key":
            return {"connection_id": "test:api"}
        if method == "connection.set_enabled":
            self.connected = True
            return {"enabled": True}
        if method == "model.list":
            return {
                "models": [{"id": "test/model", "name": "Test Model"}] if self.connected else []
            }
        if method == "agent.get":
            return {"id": "first", "model": self.model}
        if method == "agent.update":
            assert params["id"] == "first"
            self.model = params["model"]
            self.state.update(status="completed", auto_show=False)
            return {"id": "first", "model": self.model}
        if method == "skill.inventory":
            return {
                "skills": [
                    {
                        "id": "package",
                        "name": "Bundled",
                        "source_kind": "bundled",
                        "disabled": False,
                    }
                ]
            }
        if method == "skill.set_disabled":
            return params
        if method == "extensions.list":
            return {
                "extensions": [{"name": "example", "disabled": False}],
                "settings": copy.deepcopy(self.extension_settings),
            }
        if method == "settings.update":
            self.extension_settings = params["extensions"]
            return {}
        if method == "managed_service.get":
            return copy.deepcopy(self.service)
        if method == "managed_service.apply":
            self.service["state"] = "running"
            return copy.deepcopy(self.service)
        if method == "settings.patch":
            return {}
        raise AssertionError(f"Unexpected RPC: {method}")


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["provider", "model", "components", "services"])
async def test_setup_can_finish_each_step_without_changing_defaults(step: str) -> None:
    server = SetupServer(step)
    app = SetupApp(server)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        if step == "model":
            assert app.screen.query_one("#setup-choices", OptionList).option_count == 0
        await pilot.click("#setup-finish")
        await pilot.pause()
    assert server.state["status"] == "skipped"
    assert server.state["auto_show"] is False
    assert not any(
        method in {"agent.update", "skill.set_disabled", "settings.update", "managed_service.apply"}
        for method, _ in server.calls
    )


@pytest.mark.asyncio
async def test_tui_setup_precedes_chat_and_reopen_keeps_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SetupServer()
    app = ConversationApp(server, setup_state=dict(server.state))
    bootstrap = AsyncMock()
    monkeypatch.setattr(app.navigation, "events", AsyncMock())
    monkeypatch.setattr(app.navigation, "bootstrap", bootstrap)
    monkeypatch.setattr(app.navigation, "load_roster", AsyncMock())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        bootstrap.assert_not_awaited()
        await pilot.press("escape")
        await pilot.pause()
        bootstrap.assert_awaited_once()
        assert server.state["status"] == "skipped"
        app.action_setup()
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert server.state["status"] == "skipped"
        assert server.state["auto_show"] is False
        await pilot.click("#setup-finish")
        await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_enablement", [False, True])
async def test_setup_masks_key_and_finishes_after_assigning_first_agent_model(
    retry_enablement: bool,
) -> None:
    server = SetupServer()
    app = SetupApp(server)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#setup-choices", OptionList)
        choices.focus()
        choices.highlighted = 0
        await pilot.press("enter")
        key = app.screen.query_one("#setup-key", Input)
        assert key.password
        key.value = "test-secret"
        if retry_enablement:
            server.fail_method = "connection.set_enabled"
        await pilot.click("#setup-connect")
        await pilot.pause()
        assert key.value == ""
        if retry_enablement:
            assert not server.connected
            server.fail_method = ""
            await pilot.click("#setup-retry")
            await pilot.pause()
        assert server.connected
        await pilot.click("#setup-next")
        await pilot.pause()
        choices = app.screen.query_one("#setup-choices", OptionList)
        choices.focus()
        choices.highlighted = 0
        await pilot.press("enter")
        await pilot.click("#setup-finish")
        await pilot.pause()
    assert server.model == "test/model"
    assert server.state["status"] == "completed"
    assert ("agent.update", {"id": "first", "model": "test/model"}) in server.calls
    assert len([method for method, _ in server.calls if method == "provider.set_key"]) == 1
    assert not hasattr(app, "draft_store")


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_type", ["none", "oauth", "external"])
async def test_setup_connects_local_and_login_connections(connection_type: str) -> None:
    server = SetupServer()
    server.connection_type = connection_type
    app = SetupApp(server)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        choices = app.screen.query_one("#setup-choices", OptionList)
        choices.focus()
        choices.highlighted = 0
        await pilot.press("enter")
        app.screen.query_one("#setup-connect", Button).press()
        await pilot.pause()
        if connection_type != "none":
            app.screen.query_one("#setup-login-check", Button).press()
            await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert server.connected
    assert server.state["status"] == "skipped"
    assert not any(method == "provider.set_key" for method, _ in server.calls)


@pytest.mark.asyncio
async def test_setup_saves_component_choices_on_exit_without_overwriting_other_settings() -> None:
    server = SetupServer("components")
    app = SetupApp(server)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        selection = app.screen.query_one("#setup-components", SelectionList)
        selection.deselect_all()
        await pilot.click("#setup-finish")
        await pilot.pause()
    assert ("skill.set_disabled", {"id": "package", "disabled": True}) in server.calls
    assert server.extension_settings == {
        "disabled": ["example", "kept-off"],
        "config": {"example": {"a": 1}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_setup_loading_failure_remains_visible_and_allows_finish(retry: bool) -> None:
    server = SetupServer()
    server.fail_method = "model.list"
    app = SetupApp(server)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#setup-next")
        await pilot.pause()
        assert str(app.screen.query_one("#setup-error", Static).content) == "Test refusal"
        if retry:
            server.fail_method = ""
            await pilot.click("#setup-retry")
            await pilot.pause()
            assert isinstance(app.screen, SetupScreen)
            assert app.screen.step == "model"
            assert server.state["step"] == "model"
        await pilot.click("#setup-finish")
        await pilot.pause()
    assert server.state["status"] == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["setup.get", "setup.update", "setup.finish"])
async def test_setup_rpc_failure_can_retry_and_close_without_claiming_completion(
    method: str,
) -> None:
    server = SetupServer()
    server.fail_method = method
    app = SetupApp(server)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        if method != "setup.get":
            app.screen.query_one(
                "#setup-next" if method == "setup.update" else "#setup-finish", Button
            ).press()
            await pilot.pause()
        assert str(app.screen.query_one("#setup-error", Static).content) == "Test refusal"
        app.screen.query_one("#setup-retry", Button).press()
        await pilot.pause()
        assert len([name for name, _ in server.calls if name == method]) == 2
        app.screen.query_one("#setup-close", Button).press()
        await pilot.pause()
    assert server.state["status"] == "pending"
    assert app.return_value == 1


@pytest.mark.asyncio
async def test_setup_failed_save_can_retry_or_finish_with_already_saved_choices() -> None:
    server = SetupServer("components")
    server.fail_method = "skill.set_disabled"
    app = SetupApp(server)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.screen.query_one("#setup-components", SelectionList).deselect_all()
        await pilot.click("#setup-finish")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert server.state["status"] == "pending"
        await pilot.click("#setup-finish-saved")
        await pilot.pause()
    assert server.state["status"] == "skipped"
    assert server.extension_settings["disabled"] == ["kept-off"]


@pytest.mark.asyncio
async def test_service_plan_requires_explicit_prerequisite_selection() -> None:
    server = SetupServer("services")
    app = SetupApp(server)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        panel = app.screen.query_one(ServicePanel)
        await pilot.click("#service-install")
        await pilot.pause()
        assert not any(method == "managed_service.apply" for method, _ in server.calls)
        panel.query_one("#service-prerequisites", Checkbox).value = True
        panel.query_one("#service-install", Button).press()
        await pilot.pause()
        await pilot.click("#setup-finish")
        await pilot.pause()
    assert (
        "managed_service.apply",
        {"id": "searxng", "action": "install", "plan_id": "plan-1", "allow_prerequisites": True},
    ) in server.calls
    assert server.state["status"] == "skipped"


@pytest.mark.asyncio
async def test_service_authorizes_local_prerequisites_then_uses_fresh_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SetupServer("services")
    server.service["plan"]["local_authorization"] = True
    app = SetupApp(server)
    authorized = []

    def install(target: ServerInstance, state: JsonObject, *, allow_prerequisites: bool) -> bool:
        authorized.append((target, allow_prerequisites))
        server.service["plan"].update(
            id="plan-2", requires_prerequisites=False, local_authorization=False
        )
        return True

    monkeypatch.setattr("cli.tui.setup_services.install_local_service_prerequisites", install)
    monkeypatch.setattr(app, "suspend", nullcontext)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        app.screen.query_one("#service-prerequisites", Checkbox).value = True
        app.screen.query_one("#service-install", Button).press()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert authorized == [(server.target, True)]
    assert (
        "managed_service.apply",
        {"id": "searxng", "action": "install", "plan_id": "plan-2", "allow_prerequisites": True},
    ) in server.calls
