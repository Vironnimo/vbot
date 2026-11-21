"""The terminal presentation of the server's shared, voluntary first setup."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, override

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, OptionList, Select, SelectionList, Static
from textual.widgets.option_list import Option

from cli._server_target import ServerInstance
from cli.tui.models import model_choices
from cli.tui.rendering import terminal_text
from cli.tui.setup_services import ServicePanel
from cli.tui.theme import DEFAULT_THEME, THEMES
from cli.tui.transport import ClientError, TerminalClient

type JsonObject = dict[str, Any]

CORE_STEPS = ("provider", "model")
CORE_STEP_NAMES = ("Provider", "Model")
STEPS = ("provider", "model", "components", "services")


def _method_label(connection: JsonObject) -> str:
    kind = connection.get("type")
    if kind == "api_key":
        return "API key"
    if kind == "none":
        return "Local"
    return "Sign in"


def _connectable_connections(provider: JsonObject) -> list[JsonObject]:
    return [
        {**row, "provider_id": provider["id"], "provider_name": provider.get("name")}
        for row in provider.get("connections", [])
        if row.get("type") in {"api_key", "none", "external"}
        or row.get("connectable") is True
        or row.get("usable") is True
    ]


def _model_matches_provider(model: JsonObject, provider_id: str) -> bool:
    if not provider_id:
        return True
    value = model.get("provider_id")
    if isinstance(value, str):
        return value == provider_id
    model_id = model.get("id")
    return isinstance(model_id, str) and model_id.startswith(f"{provider_id}/")


class SetupScreen(ModalScreen[bool]):
    """Use the same saved setup state and management APIs as every other Accessor."""

    BINDINGS = [
        Binding("escape", "handle_escape", "Clear or back", priority=True),
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
    SetupScreen OptionList { height: 1fr; min-height: 4; max-height: 12; }
    SetupScreen #setup-methods { height: auto; min-height: 3; max-height: 6; margin-top: 1; }
    SetupScreen SelectionList { height: 1fr; min-height: 4; }
    SetupScreen Select { margin-bottom: 1; }
    SetupScreen Checkbox { margin-bottom: 1; }
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
        self.providers: list[JsonObject] = []
        self.usable_providers: list[tuple[str, str]] = []
        self.has_connected_provider = False
        self.preferred_provider_id = ""
        self.selected_provider_methods: list[JsonObject] = []
        self.filtered: list[tuple[str, Any]] = []
        self.choices: list[tuple[str, Any]] = []
        self.raw_models: list[JsonObject] = []
        self.only_free = False
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
                "Skip anytime — saved choices stay, everything else keeps its defaults. "
                "Reopen via Ctrl+K → Open setup.",
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
                yield Button("Add another Provider", id="setup-back")
                yield Button("Skip for now", id="setup-skip")
                yield Button("Continue to Model", id="setup-next")
                yield Button("Start chatting", id="setup-finish", variant="primary")

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

    def _update_title_and_actions(self) -> None:
        title = self.query_one("#setup-title", Static)
        if self.step in CORE_STEPS:
            index = CORE_STEPS.index(self.step)
            title.update(
                f"Set up vBot · {index + 1} of {len(CORE_STEPS)} · {CORE_STEP_NAMES[index]}"
            )
        elif self.step == "components":
            title.update("Set up vBot · Optional · Skills & Extensions")
        else:
            title.update("Set up vBot · Optional · Web search")

        back_button = self.query_one("#setup-back", Button)
        skip_button = self.query_one("#setup-skip", Button)
        next_button = self.query_one("#setup-next", Button)
        finish_button = self.query_one("#setup-finish", Button)

        if self.step == "provider":
            back_button.display = False
            skip_button.display = True
            next_button.display = True
            next_button.label = "Continue to Model"
            next_button.variant = "primary"
            next_button.disabled = not self.has_connected_provider
            finish_button.display = False
        elif self.step == "model":
            back_button.display = True
            back_button.label = "Add another Provider"
            skip_button.display = True
            next_button.display = True
            next_button.label = "Optional setup"
            next_button.variant = "default"
            next_button.disabled = False
            finish_button.display = True
            finish_button.label = "Start chatting"
            finish_button.disabled = not bool(self.selected_model)
        elif self.step == "components":
            back_button.display = True
            back_button.label = "Back to Model"
            skip_button.display = True
            next_button.display = True
            next_button.label = "Next: Web search"
            next_button.variant = "default"
            next_button.disabled = False
            finish_button.display = True
            finish_button.label = "Start chatting"
            finish_button.disabled = False
        else:
            back_button.display = True
            back_button.label = "Back to Skills & Extensions"
            skip_button.display = True
            next_button.display = False
            finish_button.display = True
            finish_button.label = "Start chatting"
            finish_button.disabled = False

    async def _load_step(self) -> None:
        self._loaded_step = None
        body = self.query_one("#setup-body", VerticalScroll)
        await body.remove_children()
        self.selected_connection = None
        self.selected_provider_methods = []
        self.login = None
        self._update_title_and_actions()
        if self.step == "provider":
            await self._load_providers(body)
        elif self.step == "model":
            await self._load_models(body)
        elif self.step == "components":
            await self._load_components(body)
        else:
            await body.mount(ServicePanel(self.client))
        self._loaded_step = self.step
        self._update_title_and_actions()
        self.query_one("#setup-recovery", Horizontal).display = False

    def _apply_provider_catalog(self, settings_result: JsonObject) -> None:
        providers: list[JsonObject] = []
        usable: list[tuple[str, str]] = []
        for provider in (settings_result.get("providers") or {}).get("items", []):
            connections = _connectable_connections(provider)
            if not connections:
                continue
            connected = any(row.get("usable") is True for row in connections)
            name = str(provider.get("name") or provider.get("id") or "")
            providers.append(
                {
                    "id": provider["id"],
                    "name": name,
                    "connections": connections,
                    "connected": connected,
                }
            )
            if connected:
                usable.append((str(provider["id"]), name))
        providers.sort(key=lambda item: item["name"].casefold())
        self.providers = providers
        self.usable_providers = usable
        self.has_connected_provider = bool(usable)
        if not any(pid == self.preferred_provider_id for pid, _ in usable):
            self.preferred_provider_id = usable[0][0] if usable else ""

    async def _load_providers(self, body: VerticalScroll) -> None:
        result = await self.client.rpc("settings.get")
        self._apply_provider_catalog(result)
        self.choices = [
            (
                f"{item['name']} · "
                f"{', '.join(dict.fromkeys(_method_label(row) for row in item['connections']))}"
                + (" · connected" if item["connected"] else ""),
                item,
            )
            for item in self.providers
        ]
        connected_names = ", ".join(name for _, name in self.usable_providers)
        summary = (
            f"Connected: {connected_names}. Add another Provider or continue to Model."
            if connected_names
            else "Choose a Provider to connect."
        )
        await body.mount(
            Input(placeholder="Search Providers…", id="setup-search"),
            OptionList(id="setup-choices", markup=False),
            OptionList(id="setup-methods", markup=False),
            Static(summary, id="setup-detail", markup=False),
            Input(placeholder="API key (press Enter to connect)", password=True, id="setup-key"),
            Horizontal(
                Button("Connect", id="setup-connect"),
                Button("Check sign-in", id="setup-login-check"),
                classes="setup-inline",
            ),
        )
        self.query_one("#setup-methods", OptionList).display = False
        self.query_one("#setup-key", Input).display = False
        self.query_one("#setup-connect", Button).disabled = True
        self.query_one("#setup-login-check", Button).display = False
        self._filter("")
        self._update_title_and_actions()

    async def _load_models(self, body: VerticalScroll) -> None:
        catalog, agent, settings_result = await asyncio.gather(
            self.client.rpc("model.list", {"task": "chat"}),
            self.client.rpc("agent.get", {"id": self.agent_id}),
            self.client.rpc("settings.get"),
        )
        self._apply_provider_catalog(settings_result)
        self.raw_models = [row for row in (catalog.get("models") or []) if isinstance(row, dict)]
        effective_model = ((agent.get("effective") or {}).get("model") or {}).get("value")
        saved = agent.get("model") or effective_model
        self.saved_model = saved if isinstance(saved, str) and saved.strip() else None
        self.only_free = False

        # If the saved Model belongs to a connected Provider and no Provider was
        # freshly picked during this session, prefer that Provider.
        if self.saved_model and not self.preferred_provider_id:
            prefix = self.saved_model.partition("/")[0]
            if any(pid == prefix for pid, _ in self.usable_providers):
                self.preferred_provider_id = prefix

        widgets: list[Any] = []
        if len(self.usable_providers) > 1:
            widgets.append(
                Select(
                    [(name, pid) for pid, name in self.usable_providers],
                    value=self.preferred_provider_id,
                    allow_blank=False,
                    id="setup-provider-select",
                )
            )
        widgets.append(Input(placeholder="Find a Model…", id="setup-search"))
        widgets.append(Checkbox("Free Models only", value=False, id="setup-free-only"))
        widgets.append(OptionList(id="setup-choices", markup=False))
        widgets.append(Static("", id="setup-detail", markup=False))
        await body.mount(*widgets)
        self._refresh_model_choices(auto_pick=True)
        self._update_title_and_actions()

    def _refresh_model_choices(self, *, auto_pick: bool) -> None:
        provider_id = self.preferred_provider_id
        provider_models = (
            [row for row in self.raw_models if _model_matches_provider(row, provider_id)]
            if provider_id
            else list(self.raw_models)
        )
        # Fallback for sparse catalogs without provider metadata.
        if not provider_models and len(self.usable_providers) <= 1:
            provider_models = list(self.raw_models)
        if self.only_free and provider_id == "openrouter":
            provider_models = [
                row
                for row in provider_models
                if str(row.get("id") or "").partition("::")[0].endswith(":free")
            ]

        self.choices = model_choices(provider_models, current=self.saved_model)
        valid_values = {value for _, value in self.choices}

        if (
            self.saved_model
            and self.saved_model in valid_values
            and (auto_pick or not self.selected_model)
        ):
            self.selected_model = self.saved_model
        elif self.selected_model not in valid_values:
            self.selected_model = self.choices[0][1] if (auto_pick and self.choices) else None
        elif auto_pick and not self.selected_model and self.choices:
            self.selected_model = self.choices[0][1]

        free_toggle = self.query_one("#setup-free-only", Checkbox)
        free_toggle.display = provider_id == "openrouter"

        search = self.query_one("#setup-search", Input).value
        self._filter(search)

        detail = self.query_one("#setup-detail", Static)
        if self.selected_model:
            detail.update(
                terminal_text(
                    f"Agent: {self.agent_id} · Selected: {self.selected_model}. "
                    "Start chatting or pick another Model above."
                )
            )
        elif self.choices:
            detail.update(
                terminal_text(f"Agent: {self.agent_id}. Select a Model, then Start chatting.")
            )
        elif self.only_free:
            detail.update("No free Models match. Turn off Free Models only to see more.")
        else:
            detail.update(
                "No Models are available. Add another Provider or check your local service."
            )
        self._update_title_and_actions()

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
                "Space switches a selection. Changes save when you move or finish.",
                markup=False,
            ),
            SelectionList[str](*options, id="setup-components"),
        )

    @on(Input.Changed, "#setup-search")
    def search_changed(self, event: Input.Changed) -> None:
        self._filter(event.value)

    @on(Select.Changed, "#setup-provider-select")
    def provider_select_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value != self.preferred_provider_id:
            self.preferred_provider_id = event.value
            self.selected_model = None
            self.only_free = False
            self.query_one("#setup-free-only", Checkbox).value = False
            self._refresh_model_choices(auto_pick=True)

    @on(Checkbox.Changed, "#setup-free-only")
    def free_only_changed(self, event: Checkbox.Changed) -> None:
        self.only_free = event.value
        if (
            self.only_free
            and self.selected_model
            and not self.selected_model.partition("::")[0].endswith(":free")
        ):
            self.selected_model = None
        self._refresh_model_choices(auto_pick=False)

    def _filter(self, query: str) -> None:
        words = query.casefold().split()
        self.filtered = [
            choice for choice in self.choices if all(word in choice[0].casefold() for word in words)
        ]
        options = self.query_one("#setup-choices", OptionList)
        options.clear_options()
        options.add_options([Option(terminal_text(label)) for label, _ in self.filtered])
        if self.step == "model" and self.selected_model:
            for index, (_, value) in enumerate(self.filtered):
                if value == self.selected_model:
                    options.highlighted = index
                    break

    @on(OptionList.OptionSelected, "#setup-choices")
    def choice_selected(self, event: OptionList.OptionSelected) -> None:
        value = self.filtered[event.option_index][1]
        detail = self.query_one("#setup-detail", Static)
        if self.step == "model":
            self.selected_model = value
            detail.update(
                terminal_text(
                    f"Agent: {self.agent_id} · Selected: {value}. Start chatting to continue."
                )
            )
            self._update_title_and_actions()
            return

        connections = value["connections"]
        methods = self.query_one("#setup-methods", OptionList)
        if len(connections) > 1:
            self.selected_provider_methods = connections
            methods.clear_options()
            methods.add_options(
                [
                    Option(
                        terminal_text(
                            f"{row.get('label') or _method_label(row)}"
                            + (" · connected" if row.get("usable") else "")
                        )
                    )
                    for row in connections
                ]
            )
            methods.display = True
            methods.highlighted = 0
            methods.focus()
            self._select_connection(connections[0], focus_key=False)
        else:
            self.selected_provider_methods = []
            methods.display = False
            self._select_connection(connections[0], focus_key=True)

    @on(OptionList.OptionSelected, "#setup-methods")
    def method_selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self.selected_provider_methods):
            connection = self.selected_provider_methods[event.option_index]
            self._select_connection(connection, focus_key=True)

    def _select_connection(self, value: JsonObject, *, focus_key: bool) -> None:
        self.selected_connection = value
        self.login = None
        key = self.query_one("#setup-key", Input)
        key.value = ""
        key.display = value.get("type") == "api_key"
        self.query_one("#setup-connect", Button).disabled = False
        self.query_one("#setup-login-check", Button).display = False
        self.query_one("#setup-detail", Static).update(
            terminal_text(
                f"{value.get('provider_name') or value.get('provider_id')} · "
                f"{value.get('label') or value['id']} · "
                + (
                    "Connected. You can continue or replace its credential."
                    if value.get("usable")
                    else "Connect using the saved API key, or enter a replacement."
                    if key.display and value.get("configured")
                    else "Enter the API key and press Enter to connect."
                    if key.display
                    else "Press Connect to enable this local service."
                    if value.get("type") == "none"
                    else "Press Connect to see the sign-in instructions."
                )
            )
        )
        if focus_key:
            if key.display:
                key.focus()
            else:
                self.query_one("#setup-connect", Button).focus()

    @on(Input.Submitted, "#setup-key")
    def key_submitted(self) -> None:
        self.run_worker(self._guard(self._connect), group="setup-action")

    @on(Button.Pressed, "#setup-connect")
    def connect_pressed(self) -> None:
        self.run_worker(self._guard(self._connect), group="setup-action")

    async def _after_provider_connected(self, provider_id: str, *, had_connected: bool) -> None:
        self.preferred_provider_id = provider_id
        with contextlib.suppress(ClientError, OSError):
            await self.client.rpc("model.refresh_db", {})
        if not had_connected:
            self.state = await self.client.rpc("setup.update", {"step": "model"})
            self.step = "model"
            self._retry_action = self._load_step
        await self._load_step()

    async def _connect(self) -> None:
        row = self.selected_connection
        if row is None:
            return
        had_connected = self.has_connected_provider
        params = {"provider_id": row["provider_id"], "connection_id": row["id"]}
        kind = row.get("type")
        if kind == "api_key":
            field = self.query_one("#setup-key", Input)
            value = field.value.strip()
            if value:
                await self.client.rpc("provider.set_key", {**params, "value": value})
                row["configured"] = True
                if self.selected_connection is row and field.value.strip() == value:
                    field.value = ""
            elif not row.get("configured"):
                raise ClientError("Enter an API key, or skip setup to configure it later.")
            await self.client.rpc("connection.set_enabled", {**params, "enabled": True})
            await self._after_provider_connected(row["provider_id"], had_connected=had_connected)
        elif kind == "none":
            response = await self.client.rpc("connection.set_enabled", {**params, "enabled": True})
            if response.get("reachable") is False and had_connected:
                await self._load_step()
                if self.is_mounted:
                    self.query_one("#setup-detail", Static).update(
                        "Connection enabled. "
                        "Start the local service so its Models can be discovered."
                    )
                return
            await self._after_provider_connected(row["provider_id"], had_connected=had_connected)
        else:
            response = await self.client.rpc("provider.connect", params)
            self.login = {**params, "_had_connected": had_connected}
            if response.get("connected"):
                await self._after_provider_connected(
                    row["provider_id"], had_connected=had_connected
                )
                return
            if response.get("verification_uri"):
                text = (
                    f"Open {response['verification_uri']}\n"
                    f"Code: {response.get('user_code', '')}\nThen check sign-in."
                )
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
            self.query_one("#setup-login-check", Button).display = True

    @on(Button.Pressed, "#setup-login-check")
    def login_check_pressed(self) -> None:
        self.run_worker(self._guard(self._check_login), group="setup-action")

    async def _check_login(self) -> None:
        if self.login:
            had_connected = bool(self.login.get("_had_connected", self.has_connected_provider))
            rpc_params = {
                "provider_id": self.login["provider_id"],
                "connection_id": self.login["connection_id"],
            }
            result = await self.client.rpc("provider.connection_status", rpc_params)
            if result.get("connected"):
                self.query_one("#setup-login-check", Button).display = False
                await self._after_provider_connected(
                    rpc_params["provider_id"], had_connected=had_connected
                )
            else:
                self.query_one("#setup-error", Static).update(
                    "Sign-in is not complete. Follow the instructions above, then check again."
                )

    async def _save_step(self, *, save_model: bool = True) -> None:
        if self._loaded_step != self.step:
            return
        if (
            save_model
            and self.step == "model"
            and self.selected_model
            and self.selected_model != self.saved_model
        ):
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

    def action_handle_escape(self) -> None:
        if self._finished:
            return
        try:
            search = self.query_one("#setup-search", Input)
            if search.value:
                search.value = ""
                return
        except Exception:
            pass
        if self.step == "provider":
            try:
                key = self.query_one("#setup-key", Input)
                if key.has_focus:
                    self.query_one("#setup-choices", OptionList).focus()
                    return
            except Exception:
                pass
            return
        if self.step in ("components", "services"):
            self.run_worker(self._guard(lambda: self._move(-1)), group="setup-action")

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
        await self._save_step(save_model=direction > 0)
        step = STEPS[max(0, min(len(STEPS) - 1, STEPS.index(self.step) + direction))]
        self.state = await self.client.rpc("setup.update", {"step": step})
        self.step = step
        self._retry_action = self._load_step
        await self._load_step()

    @on(Button.Pressed, "#setup-skip")
    def skip_pressed(self) -> None:
        self.action_skip()

    def action_skip(self) -> None:
        self.run_worker(self._guard(self._finish_saved), group="setup-action")

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
