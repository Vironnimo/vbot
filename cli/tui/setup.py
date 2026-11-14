"""The terminal presentation of the server's shared, voluntary first setup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, override

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from cli._server_target import ServerInstance
from cli.tui.models import model_choices
from cli.tui.rendering import terminal_text
from cli.tui.setup_services import ServicePanel
from cli.tui.theme import DEFAULT_THEME, THEMES
from cli.tui.transport import ClientError, TerminalClient

type JsonObject = dict[str, Any]

STEPS = ("provider", "model", "components", "services")
STEP_NAMES = ("Provider", "Model", "Skills and Extensions", "Optional services")


class SetupScreen(ModalScreen[bool]):
    """Use the same saved setup state and management APIs as every other Accessor."""

    BINDINGS = [
        Binding("escape,ctrl+q", "finish", "Finish setup", priority=True),
    ]
    DEFAULT_CSS = """
    SetupScreen { align: center middle; }
    SetupScreen > Vertical {
        width: 96%; max-width: 120; height: 94%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    SetupScreen #setup-title { height: 1; text-style: bold; color: $accent; }
    SetupScreen #setup-hint { height: auto; margin-bottom: 1; color: $text-muted; }
    SetupScreen #setup-body { height: 1fr; }
    SetupScreen #setup-error { height: auto; color: $error; }
    SetupScreen #setup-error:empty { display: none; }
    SetupScreen #setup-actions, SetupScreen #setup-recovery { height: auto; margin-top: 1; }
    SetupScreen Button { margin-right: 1; min-width: 12; }
    SetupScreen Input { margin-bottom: 1; }
    SetupScreen OptionList { height: 1fr; min-height: 4; max-height: 14; }
    SetupScreen SelectionList { height: 1fr; min-height: 4; }
    SetupScreen .setup-detail { height: auto; margin: 1 0; }
    SetupScreen .setup-inline { height: auto; }
    """

    def __init__(self, client: TerminalClient, state: JsonObject | None = None) -> None:
        super().__init__()
        self.client = client
        self.state = state or {}
        self._state_loaded = state is not None
        state = self.state
        self.step = state.get("step") if state.get("step") in STEPS else "provider"
        self.agent_id = str(state.get("agent_id") or "main")
        self.connections: list[JsonObject] = []
        self.filtered: list[tuple[str, Any]] = []
        self.choices: list[tuple[str, Any]] = []
        self.selected_connection: JsonObject | None = None
        self.selected_model: str | None = None
        self.saved_model: str | None = None
        self.login: JsonObject | None = None
        self.skills: dict[str, JsonObject] = {}
        self.extensions: dict[str, JsonObject] = {}
        self.selected_components: set[str] = set()
        self._operation_lock = asyncio.Lock()
        self._finished = False
        self._loaded_step: str | None = None
        self._retry_action: Callable[[], Awaitable[None]] | None = None

    @override
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="setup-title", markup=False)
            yield Static(
                "Finish at any step. Saved choices stay; everything else keeps its defaults. "
                "You can reopen setup later.",
                id="setup-hint",
                markup=False,
            )
            yield VerticalScroll(id="setup-body")
            yield Static("", id="setup-error", markup=False)
            with Horizontal(id="setup-recovery"):
                yield Button("Retry", id="setup-retry")
                yield Button("Finish saved choices", id="setup-finish-saved")
                yield Button(
                    "Close",
                    id="setup-close",
                    tooltip="Close without marking setup finished. You can retry next time.",
                )
            with Horizontal(id="setup-actions"):
                yield Button("Back", id="setup-back")
                yield Button("Continue", id="setup-next", variant="primary")
                yield Button("Finish setup", id="setup-finish")

    def on_mount(self) -> None:
        self.query_one("#setup-recovery", Horizontal).display = False
        self.query_one("#setup-finish-saved", Button).display = False
        self.run_worker(self._guard(self._initialize), exclusive=True, group="setup-load")

    async def _initialize(self) -> None:
        if not self._state_loaded:
            self.query_one("#setup-title", Static).update("Set up vBot")
            self.state = await self.client.rpc("setup.get")
            self.step = self.state.get("step") if self.state.get("step") in STEPS else "provider"
            self.agent_id = str(self.state.get("agent_id") or "main")
            self._state_loaded = True
        await self._load_step()

    async def _guard(self, action: Callable[[], Awaitable[None]]) -> None:
        async with self._operation_lock:
            if self._finished:
                return
            try:
                self._retry_action = action
                self.query_one("#setup-error", Static).update("")
                await action()
                self._retry_action = None
                if self.is_mounted:
                    self.query_one("#setup-recovery", Horizontal).display = False
            except (ClientError, OSError) as error:
                if self.is_mounted:
                    self.query_one("#setup-error", Static).update(terminal_text(str(error)))
                    self.query_one("#setup-recovery", Horizontal).display = True

    async def _load_step(self) -> None:
        self._loaded_step = None
        body = self.query_one("#setup-body", VerticalScroll)
        await body.remove_children()
        index = STEPS.index(self.step)
        self.query_one("#setup-title", Static).update(
            f"Set up vBot · {index + 1} of {len(STEPS)} · {STEP_NAMES[index]}"
        )
        self.query_one("#setup-back", Button).disabled = index == 0
        self.query_one("#setup-next", Button).display = index < len(STEPS) - 1
        self.selected_connection = None
        self.login = None
        if self.step == "provider":
            await self._load_providers(body)
        elif self.step == "model":
            await self._load_models(body)
        elif self.step == "components":
            await self._load_components(body)
        else:
            await body.mount(ServicePanel(self.client))
        self._loaded_step = self.step
        self.query_one("#setup-recovery", Horizontal).display = False

    async def _load_providers(self, body: VerticalScroll) -> None:
        result = await self.client.rpc("settings.get")
        self.connections = [
            {**row, "provider_id": provider["id"], "provider_name": provider.get("name")}
            for provider in (result.get("providers") or {}).get("items", [])
            for row in provider.get("connections", [])
            if row.get("type") in {"api_key", "none", "external"}
            or row.get("connectable") is True
            or row.get("usable") is True
        ]
        self.choices = [
            (
                f"{row.get('provider_name') or row.get('provider_id', '')} · "
                f"{row.get('label') or row['id']}" + (" · connected" if row.get("usable") else ""),
                row,
            )
            for row in self.connections
        ]
        await body.mount(
            Input(placeholder="Find a Provider or sign-in method", id="setup-search"),
            OptionList(id="setup-choices", markup=False),
            Static("Choose a Provider and connection method.", id="setup-detail", markup=False),
            Input(placeholder="API key", password=True, id="setup-key"),
            Horizontal(
                Button("Connect", id="setup-connect"),
                Button("Check sign-in", id="setup-login-check"),
                classes="setup-inline",
            ),
        )
        self.query_one("#setup-key", Input).display = False
        self.query_one("#setup-connect", Button).disabled = True
        self.query_one("#setup-login-check", Button).display = False
        self._filter("")

    async def _load_models(self, body: VerticalScroll) -> None:
        catalog, agent = await asyncio.gather(
            self.client.rpc("model.list", {"task": "chat"}),
            self.client.rpc("agent.get", {"id": self.agent_id}),
        )
        self.saved_model = agent.get("model") or (
            (agent.get("effective") or {}).get("model") or {}
        ).get("value")
        self.selected_model = self.saved_model
        self.choices = model_choices(catalog.get("models"), current=self.saved_model)
        await body.mount(
            Input(placeholder="Find a Model", id="setup-search"),
            OptionList(id="setup-choices", markup=False),
            Static(
                f"Agent: {self.agent_id}. Current Model: {self.saved_model or 'not selected'}. "
                "Select a Model, then continue or finish."
                if self.choices
                else "No Models are available. Connect a Provider on the previous step, "
                "or finish and configure it later.",
                id="setup-detail",
                markup=False,
            ),
        )
        self._filter("")

    async def _load_components(self, body: VerticalScroll) -> None:
        skills, extensions = await asyncio.gather(
            self.client.rpc("skill.inventory", {"include_usage": False}),
            self.client.rpc("extensions.list"),
        )
        self.skills = {
            row["id"]: row
            for row in skills.get("skills", [])
            if row.get("source_kind") == "bundled" and isinstance(row.get("id"), str)
        }
        self.extensions = {
            row["name"]: row
            for row in extensions.get("extensions", [])
            if row.get("status") != "overridden" and isinstance(row.get("name"), str)
        }
        options: list[tuple[Content, str, bool]] = []
        for kind, rows in (("skill", self.skills), ("extension", self.extensions)):
            for identity, row in rows.items():
                title = row.get("display_name") or row.get("name") or identity
                label = f"{kind.title()}: {title}"
                if row.get("description"):
                    label += f" — {row['description']}"
                options.append(
                    (
                        Content(terminal_text(label)),
                        f"{kind}:{identity}",
                        not row.get("disabled", False),
                    )
                )
        self.selected_components = {value for _, value, selected in options if selected}
        await body.mount(
            Static(
                "Space switches a selection. Changes are saved when you continue, "
                "go back or finish.",
                markup=False,
            ),
            SelectionList[str](*options, id="setup-components"),
        )

    @on(Input.Changed, "#setup-search")
    def search_changed(self, event: Input.Changed) -> None:
        self._filter(event.value)

    def _filter(self, query: str) -> None:
        words = query.casefold().split()
        self.filtered = [
            choice for choice in self.choices if all(word in choice[0].casefold() for word in words)
        ]
        options = self.query_one("#setup-choices", OptionList)
        options.clear_options()
        options.add_options([Option(terminal_text(label)) for label, _ in self.filtered])

    @on(OptionList.OptionSelected, "#setup-choices")
    def choice_selected(self, event: OptionList.OptionSelected) -> None:
        value = self.filtered[event.option_index][1]
        detail = self.query_one("#setup-detail", Static)
        if self.step == "model":
            self.selected_model = value
            detail.update(
                terminal_text(f"Selected {value} for {self.agent_id}. Continue or finish to save.")
            )
            return
        self.selected_connection = value
        self.login = None
        key = self.query_one("#setup-key", Input)
        key.value = ""
        key.display = value.get("type") == "api_key"
        self.query_one("#setup-connect", Button).disabled = False
        self.query_one("#setup-login-check", Button).display = False
        detail.update(
            terminal_text(
                f"{value.get('label') or value['id']} · "
                + (
                    "Connected. You can continue or replace its credential."
                    if value.get("usable")
                    else "Enter the API key and connect."
                    if key.display
                    else "Connect to enable this local service."
                    if value.get("type") == "none"
                    else "Connect to see the sign-in instructions."
                )
            )
        )
        if key.display:
            key.focus()

    @on(Button.Pressed, "#setup-connect")
    def connect_pressed(self) -> None:
        self.run_worker(self._guard(self._connect), group="setup-action")

    async def _connect(self) -> None:
        row = self.selected_connection
        if row is None:
            return
        params = {"provider_id": row["provider_id"], "connection_id": row["id"]}
        kind = row.get("type")
        if kind == "api_key":
            field = self.query_one("#setup-key", Input)
            value = field.value.strip()
            if not value:
                raise ClientError("Enter an API key, or finish setup to configure it later.")
            await self.client.rpc("provider.set_key", {**params, "value": value})
            field.value = ""
            await self.client.rpc("connection.set_enabled", {**params, "enabled": True})
            self.query_one("#setup-detail", Static).update(
                "Provider connected. Continue to choose a Model."
            )
        elif kind == "none":
            response = await self.client.rpc("connection.set_enabled", {**params, "enabled": True})
            message = "Connection enabled. Continue to choose a Model."
            if response.get("reachable") is False:
                message = (
                    "Connection enabled. Start the local service so its Models can be discovered."
                )
            self.query_one("#setup-detail", Static).update(message)
        else:
            response = await self.client.rpc("provider.connect", params)
            self.login = params
            text = ""
            if response.get("verification_uri"):
                text = (
                    f"Open {response['verification_uri']}\n"
                    f"Code: {response.get('user_code', '')}\nThen check sign-in."
                )
            elif response.get("connected"):
                text = "Provider connected. Continue to choose a Model."
            else:
                auth = response.get("external_auth") or response
                text = str(
                    auth.get("message") or "Complete the Provider's login on the server computer."
                )
                if response.get("setup_command") or row.get("setup_command"):
                    text += f"\n{response.get('setup_command') or row['setup_command']}"
                if response.get("setup_url") or row.get("setup_url"):
                    text += f"\n{response.get('setup_url') or row['setup_url']}"
            self.query_one("#setup-detail", Static).update(terminal_text(text))
            self.query_one("#setup-login-check", Button).display = not response.get(
                "connected", False
            )

    @on(Button.Pressed, "#setup-login-check")
    def login_check_pressed(self) -> None:
        self.run_worker(self._guard(self._check_login), group="setup-action")

    async def _check_login(self) -> None:
        if self.login:
            result = await self.client.rpc("provider.connection_status", self.login)
            if result.get("connected"):
                self.query_one("#setup-detail", Static).update(
                    "Provider connected. Continue to choose a Model."
                )
                self.query_one("#setup-login-check", Button).display = False
            else:
                self.query_one("#setup-error", Static).update(
                    "Sign-in is not complete. Follow the instructions above, then check again."
                )

    async def _save_step(self) -> None:
        if self._loaded_step != self.step:
            return
        if self.step == "model" and self.selected_model and self.selected_model != self.saved_model:
            await self.client.rpc(
                "agent.update", {"id": self.agent_id, "model": self.selected_model}
            )
            self.saved_model = self.selected_model
        if self.step != "components":
            return
        selected = set(self.query_one("#setup-components", SelectionList).selected)
        changed = selected.symmetric_difference(self.selected_components)
        for identity in self.skills:
            token = f"skill:{identity}"
            if token in changed:
                await self.client.rpc(
                    "skill.set_disabled", {"id": identity, "disabled": token not in selected}
                )
                self.selected_components.symmetric_difference_update({token})
        extension_changes = {name for name in self.extensions if f"extension:{name}" in changed}
        if extension_changes:
            current = await self.client.rpc("extensions.list")
            base = current["settings"]
            disabled = set(base["disabled"])
            for name in extension_changes:
                if f"extension:{name}" in selected:
                    disabled.discard(name)
                else:
                    disabled.add(name)
            await self.client.rpc(
                "settings.update",
                {
                    "extensions": {**base, "disabled": sorted(disabled)},
                    "base": {"extensions": base},
                },
            )
            self.selected_components.symmetric_difference_update(
                {f"extension:{name}" for name in extension_changes}
            )

    @on(Button.Pressed, "#setup-next")
    def next_pressed(self) -> None:
        self.run_worker(self._guard(lambda: self._move(1)), group="setup-action")

    @on(Button.Pressed, "#setup-back")
    def back_pressed(self) -> None:
        self.run_worker(self._guard(lambda: self._move(-1)), group="setup-action")

    async def _move(self, direction: int) -> None:
        if not self._state_loaded:
            await self._initialize()
            return
        await self._save_step()
        step = STEPS[max(0, min(len(STEPS) - 1, STEPS.index(self.step) + direction))]
        self.state = await self.client.rpc("setup.update", {"step": step})
        self.step = step
        self._retry_action = self._load_step
        await self._load_step()

    @on(Button.Pressed, "#setup-finish")
    def action_finish(self) -> None:
        self.run_worker(self._guard(self._finish), group="setup-action")

    async def _finish(self) -> None:
        try:
            await self._save_step()
        except ClientError, OSError:
            self.query_one("#setup-finish-saved", Button).display = True
            raise
        await self._finish_saved()

    @on(Button.Pressed, "#setup-finish-saved")
    def finish_saved_pressed(self) -> None:
        self.run_worker(self._guard(self._finish_saved), group="setup-action")

    @on(Button.Pressed, "#setup-retry")
    def retry_pressed(self) -> None:
        self.run_worker(self._guard(self._retry_action or self._initialize), group="setup-action")

    @on(Button.Pressed, "#setup-close")
    def close_pressed(self) -> None:
        self._finished = True
        self.dismiss(False)

    async def _finish_saved(self) -> None:
        self.state = await self.client.rpc("setup.finish")
        self._finished = True
        self.dismiss(True)


class SetupApp(App[int]):
    """Standalone host for exactly the same screen the conversation TUI opens."""

    def __init__(self, client: TerminalClient) -> None:
        super().__init__()
        self.client = client
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_THEME

    def on_mount(self) -> None:
        self.run_worker(self._open(), exit_on_error=False)

    async def _open(self) -> None:
        finished = await self.push_screen_wait(SetupScreen(self.client))
        self.exit(0 if finished else 1)


async def run_setup(*, target: ServerInstance) -> int:
    """Explicit interactive setup command; the dispatcher checks terminal ownership."""
    async with TerminalClient(target) as client:
        return await SetupApp(client).run_async() or 0
