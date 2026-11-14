"""Optional service controls inside the shared terminal setup screen."""

from __future__ import annotations

import asyncio
from typing import Any, override

from textual import on
from textual.app import ComposeResult, SuspendNotSupported
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, Static

from cli._dispatch_service import ActionRequiredError, install_local_service_prerequisites
from cli.tui.rendering import terminal_text
from cli.tui.transport import ClientError, TerminalClient


class ServicePanel(Vertical):
    """Display the server's concrete install plan before starting a managed service."""

    DEFAULT_CSS = """
    ServicePanel { height: auto; }
    ServicePanel Static { height: auto; margin-bottom: 1; }
    ServicePanel Horizontal { height: auto; }
    ServicePanel Checkbox { margin-bottom: 1; }
    """

    def __init__(self, client: TerminalClient) -> None:
        super().__init__()
        self.client = client
        self.state: dict[str, Any] = {}
        self._request_running = False

    @override
    def compose(self) -> ComposeResult:
        yield Static("SearXNG · private web search", markup=False)
        yield Static("Loading service information…", id="service-state", markup=False)
        yield Static("", id="service-plan", markup=False)
        yield Checkbox("Install the prerequisites listed above", id="service-prerequisites")
        with Horizontal():
            for action, label in (
                ("install", "Set up SearXNG"),
                ("start", "Start"),
                ("stop", "Stop"),
                ("update", "Update"),
                ("remove", "Remove"),
            ):
                yield Button(label, id=f"service-{action}")
        yield Button("Refresh status", id="service-refresh")
        yield Input(
            placeholder="Existing SearXNG URL, e.g. http://127.0.0.1:8080",
            id="service-existing-url",
        )
        yield Button("Use existing instance", id="service-connect")
        yield Static("", id="service-error", markup=False)

    def on_mount(self) -> None:
        self.run_worker(self.refresh_state(), group="service-read", exclusive=True)
        self.set_interval(2, self._poll)

    async def _poll(self) -> None:
        if self.state.get("state") == "running" and not self._request_running:
            await self.refresh_state()

    async def refresh_state(self) -> None:
        self._request_running = True
        try:
            result = await self.client.rpc("managed_service.get", {"id": "searxng"})
            if self.is_mounted:
                self._show(result)
        except (ClientError, OSError) as error:
            if self.is_mounted:
                self.query_one("#service-error", Static).update(terminal_text(str(error)))
        finally:
            self._request_running = False

    def _show(self, state: dict[str, Any]) -> None:
        previous_plan = (self.state.get("plan") or {}).get("id")
        self.state = state
        text = state.get("message") or state.get("phase") or state.get("state") or "Not installed"
        if state.get("url"):
            text += f"\n{state['url']}"
        self.query_one("#service-state", Static).update(terminal_text(str(text)))
        plan = state.get("plan") or {}
        self.query_one("#service-plan", Static).update(
            terminal_text("\n".join(f"• {step['label']}" for step in plan.get("steps", [])))
        )
        checkbox = self.query_one("#service-prerequisites", Checkbox)
        checkbox.display = plan.get("requires_prerequisites", False)
        if previous_plan != plan.get("id"):
            checkbox.value = False
        actions = state.get("actions") or []
        for action in ("install", "start", "stop", "update", "remove"):
            button = self.query_one(f"#service-{action}", Button)
            button.display = action in actions
            button.disabled = state.get("state") == "running"
        self.query_one("#service-error", Static).update(
            terminal_text(str(state.get("error") or ""))
        )

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id or ""
        if not identifier.startswith("service-"):
            return
        event.stop()
        action = identifier.removeprefix("service-")
        self.run_worker(self._apply(action), group="service-write", exclusive=False)

    async def _apply(self, action: str) -> None:
        if self._request_running:
            return
        self._request_running = True
        try:
            if action == "refresh":
                state = await self.client.rpc("managed_service.get", {"id": "searxng"})
                if self.is_mounted:
                    self._show(state)
                return
            if action == "connect":
                address = self.query_one("#service-existing-url", Input).value.strip()
                if not address:
                    raise ClientError("Enter the existing instance's HTTP or HTTPS URL.")
                await self.client.rpc(
                    "settings.patch",
                    {
                        "operations": [
                            {"op": "set", "path": "web_search.provider", "value": "searxng"},
                            {"op": "set", "path": "web_search.searxng.base_url", "value": address},
                        ]
                    },
                )
                if self.is_mounted:
                    self.query_one("#service-error", Static).update("")
                    self.query_one("#service-state", Static).update(
                        "Existing SearXNG instance configured."
                    )
                return
            plan = self.state.get("plan") or {}
            allow = self.query_one("#service-prerequisites", Checkbox).value
            if action in {"install", "update"} and plan.get("requires_prerequisites") and not allow:
                raise ClientError(
                    "Select the prerequisite installation above to include those steps."
                )
            params: dict[str, Any] = {"id": "searxng", "action": action}
            if action in {"install", "update"}:
                if plan.get("local_authorization") is True:
                    # Restore the actual terminal for the fixed local sudo command.
                    # The helper proves this target belongs to this installation;
                    # remote targets receive host instructions instead.
                    authorization_error: ActionRequiredError | OSError | None = None
                    with self.app.suspend():
                        try:
                            installed = await asyncio.to_thread(
                                install_local_service_prerequisites,
                                self.client.target,
                                self.state,
                                allow_prerequisites=allow,
                            )
                        except (ActionRequiredError, OSError) as error:
                            # Textual must resume before reporting refused authorization.
                            authorization_error = error
                    if authorization_error is not None:
                        raise authorization_error
                    if installed:
                        state = await self.client.rpc("managed_service.get", {"id": "searxng"})
                        if self.is_mounted:
                            self._show(state)
                        plan = state.get("plan") or {}
                params.update(plan_id=plan.get("id"), allow_prerequisites=allow)
            state = await self.client.rpc("managed_service.apply", params)
            if self.is_mounted:
                self._show(state)
        except (ClientError, OSError, ActionRequiredError, SuspendNotSupported) as error:
            if self.is_mounted:
                self.query_one("#service-error", Static).update(terminal_text(str(error)))
        finally:
            self._request_running = False
