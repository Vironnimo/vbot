# Public authoring declarations; runtime identities come from the installed host.
from __future__ import annotations

import contextlib
import logging
import sqlite3
import typing
from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Final,
    Literal,
    Never,
)

from ._exports import HostUnavailableError as HostUnavailableError
from .tools import ToolContext

API_VERSION: Final[int] = 41
SDK_VERSION: Final[str] = "41.0.0"

def require_api(minimum: int = API_VERSION, *, maximum: int | None = None) -> int: ...

@dataclass(frozen=True)
class TemporaryAgentConfig:
    model: str
    cwd: Path
    tool_access: Any
    allowed_skills: list[str]
    tools: dict[str, Any]
    name: str
    temperature: float | None = None
    top_p: float | None = None
    thinking_effort: str | None = None
    fallback_models: list[str] | None = None
    instructions: str = ""
    prompt_blocks: list[str] | None = None
    project_memory_enabled: bool = True
    compaction_policy: dict[str, Any] | None = None
    repository_profile: str | None = None
    identity_profile: str | None = None
    identity_agent: str | None = None
    identity_incarnation: str | None = None
    permission_rules: Any = None
    permission_ceiling: Mapping[str, Any] | None = None
    model_override: str | None = None
    thinking_effort_override: str | None = None
    permission_ceiling_profile: str | None = None
    inherit_permission_defaults: bool = False
    permission_profile: str | None = None
    @property
    def has_profile(self) -> bool: ...

@dataclass(frozen=True)
class TemporaryRunInput:
    kind: Literal["initial", "continuation"]
    content: str
    request_id: str

class ExtensionAPI:
    config: dict[str, Any]
    logger: logging.Logger
    operations: ExtensionOperations

    def __init__(
        self,
        extension_name: str,
        declarations: Any,
        *,
        config: dict[str, Any],
        logger: Any,
        config_provider: Callable[[], dict[str, Any]] | None = None,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> None: ...
    def register_settings(self, fields: list[Any]) -> None: ...
    def register_resource(
        self, name: str, *, description: str = "", scope_schema: dict[str, Any] | None = None
    ) -> None: ...
    def register_service(
        self,
        name: str,
        kind: str,
        parameters: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
        *,
        resources: Sequence[str] = (),
        description: str = "",
    ) -> None: ...
    def get_config(self) -> dict[str, Any]: ...
    def on_config_change(
        self, handler: Callable[[dict[str, Any], dict[str, Any]], Any]
    ) -> None: ...
    def resolve_credential(self, key: str) -> str: ...
    def on(self, event: str, handler: Any) -> None: ...
    def register_shell_reviewer(
        self, handler: Callable[..., Any], *, active: Callable[[], bool]
    ) -> None: ...
    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        *,
        summary: str | None = None,
        internal: bool = False,
        catalog_visible: bool = True,
        requires_opt_in: bool = False,
        display: Any = None,
        ready: Callable[[], bool] | None = None,
        readiness_hint: str | None = None,
        result_schema: dict[str, Any] | None = None,
        parallel_safe: bool = True,
        open_input_schema: bool = False,
        unadvertised_parameters: dict[str, Any] | None = None,
        argument_normalizer: Callable[[Any], Any] | None = None,
        family: str | None = None,
        announce_changes: Sequence[str] = (),
        secret_sinks: Mapping[str, str] | None = None,
    ) -> None: ...
    def register_app_chat_profile(
        self,
        profile_id: str,
        *,
        name: str,
        instructions: str,
        tools: Sequence[str] = (),
        tool_settings: Mapping[str, Any] | None = None,
        permission_ceiling: Mapping[str, Any] | None = None,
        skills: Sequence[str] = (),
        prompt_blocks: Sequence[str] = ("core:tools", "core:system_reminders"),
        model: str = "",
    ) -> None: ...
    def register_page(
        self,
        page_id: str,
        title: str,
        entry: str,
        *,
        icon: str | dict[str, Any] = "network",
        navigation: bool = True,
        services: Sequence[str] = (),
    ) -> None: ...
    def register_contribution(self, contribution_id: str, **fields: Any) -> None: ...
    def register_session_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
        **kwargs: Any,
    ) -> None: ...
    def register_session_prompt_block(self, slug: str, *, render: Callable[..., str]) -> None: ...
    def register_session_runtime(
        self,
        *,
        before_request: Callable[..., Any],
        run_finished: Callable[..., Any],
        quiesce: Callable[..., Any],
        acknowledge_delivery: Callable[..., Any] | None = None,
        reconcile_tool_batch: Callable[..., Any] | None = None,
        before_tool_call: Callable[..., Any] | None = None,
        before_model_request: Callable[..., Any] | None = None,
    ) -> None: ...
    def register_tool_family(self, family_id: str, label: str) -> None: ...
    def register_command(
        self,
        name: str,
        description: str,
        handler: Any,
        *,
        argument: str = "optional",
        catalog_result: str = "notice",
        execution_mode: str = "serialized",
        argument_execution_mode: str | None = None,
        unavailable_surfaces: frozenset[str] | set[str] | tuple[str, ...] = (),
        argument_hint: str = "",
    ) -> None: ...
    def register_recall_backend(self, name: str, factory: Callable[..., Any]) -> None: ...
    def register_provider(
        self, definition: Mapping[str, Any], adapter_type: Any = None
    ) -> None: ...
    def register_channel_platform(
        self,
        platform_id: str,
        label: str,
        factory: Callable[..., Any],
        *,
        settings_schema: dict[str, Any] | None = None,
    ) -> None: ...
    def register_secret_source(self, source: Any) -> None: ...
    def register_interaction_handler(self, prefix: str, handler: Callable[..., Any]) -> None: ...
    def register_prompt_block(
        self,
        slug: str,
        *,
        default_text: str | None = None,
        render: Callable[..., str] | None = None,
        requires_tool: str | None = None,
    ) -> None: ...
    def on_startup(self, handler: Any) -> None: ...
    def on_shutdown(self, handler: Any) -> None: ...

class IdentityAgentProfiles(typing.Protocol):
    async def get(self, agent_id: str) -> dict[str, Any]: ...
    async def profiles(self, agent_ids: list[str]) -> dict[str, dict[str, Any]]: ...
    async def create(
        self, agent_id: str, *, request_id: str | None = None, **config: Any
    ) -> dict[str, Any]: ...
    async def update(
        self, agent_id: str, *, expected_incarnation: str, **changes: Any
    ) -> dict[str, Any]: ...

class ExtensionDataProtection(typing.Protocol):
    def seal(self, identity: str, content: bytes) -> str: ...
    def open(self, identity: str, ciphertext: str) -> bytes: ...

@dataclass(frozen=True)
class ExtensionHost:
    data_dir: Path
    sample: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]
    resolve_agent: Callable[[str | None, str], Any]
    resolve_tool_agent: Callable[[ToolContext], Any]
    store_attachment: Callable[[str, bytes], Any]
    resolve_credential: Callable[[str], str]
    set_credential: Callable[[str, str], None]
    resolve_cwd: Callable[[str | None, str], Path] | None = None
    for_owner: Callable[[Any], ExtensionHost] | None = None
    temporary_agents: Any | None = None
    identity_agents: IdentityAgentProfiles | None = None
    state_dir: Path | None = None
    catalog: Callable[[], Awaitable[dict[str, Any]]] | None = None
    publish_change: Callable[[str, Sequence[str], int], None] | None = None
    inspect_prompt: Callable[[Any, str | None], Awaitable[dict[str, Any]]] | None = None
    open_database: Callable[..., Awaitable[Any]] | None = None
    release_owner: Callable[[Any], Awaitable[None]] | None = None
    load_result_payload: Callable[[ToolContext, str], Awaitable[Any]] | None = None
    oauth_redirects: Any | None = None
    resolve_tool_agent_async: Callable[[ToolContext], Awaitable[Any]] | None = None
    capture_tool_call: Callable[[ToolContext], Awaitable[str | None]] | None = None
    restore_tool_call: Callable[[str], Awaitable[ToolContext | None]] | None = None
    deliver_tool_outcome: Callable[[str, str], Awaitable[bool]] | None = None
    defer_tool_call: Callable[[ToolContext], Awaitable[str | None]] | None = None
    resolve_permissions: Callable[[str | None, str], Awaitable[Any]] | None = None
    computers: Any | None = None
    decide: Callable[[ToolContext | None, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    review_context: Callable[[ToolContext], Awaitable[dict[str, Any]]] | None = None
    notify: Any | None = None
    set_page_badge: Callable[[str, int], None] | None = None
    pending_approvals: Callable[[str, Sequence[str]], Awaitable[dict[str, Any]]] | None = None
    execution_usage: Any | None = None
    input_authority: (
        Callable[[ToolContext], Awaitable[Literal["user", "authorized_task", "none"]]] | None
    ) = None
    triggers: Any | None = None
    services: ExtensionServices | None = None
    execution_usage_changes: Any | None = None
    protected_data: ExtensionDataProtection | None = None

class ExtensionOperations:
    # Supplied by ExtensionAPI; private host wiring is not an authoring constructor.
    def __init__(self, _host: Never) -> None: ...
    name: str
    operations: dict[str, ManagementOperation]
    startup: list[Callable[[ExtensionHost], Awaitable[None]]]
    pending_inputs: Callable[[], list[dict[str, Any]]] | None
    input_response_operation: str | None

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        secret: bool = False,
    ) -> None: ...
    def describe(self) -> list[dict[str, Any]]: ...
    async def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    @property
    def tool_names(self) -> tuple[str, ...]: ...
    @property
    def catalog_visible_tool_names(self) -> tuple[str, ...]: ...
    @property
    def tool_registry(self) -> Any | None: ...
    def bind(self, registry: Any) -> None: ...
    def replace_tools(self, catalog: str, tools: Sequence[Mapping[str, Any]]) -> None: ...
    def retire(self) -> None: ...

@dataclass(frozen=True)
class ManagementOperation:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    secret: bool = False

class ExtensionOperationError(ValueError):
    code: str
    def __init__(self, code: str, message: str) -> None: ...

class ExtensionServices:
    def __init__(self, catalog: Any, owner: Any, require_current: Callable[[], None]) -> None: ...
    def describe(self) -> dict[str, Any]: ...
    async def query(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def execute(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def subscribe(
        self,
        resources: Iterable[str],
        handler: Callable[[dict[str, Any]], Any],
        *,
        on_error: Callable[[dict[str, Any]], Any] | None = None,
    ) -> ExtensionSubscription: ...
    def publish(self, resource: str, scope: dict[str, Any] | None = None) -> None: ...

class ExtensionServiceError(ValueError):
    code: str
    data: Any

    def __init__(self, code: str, message: str, data: Any = None) -> None: ...

class ExtensionSubscription:
    def __init__(self, subscription: Any) -> None: ...
    def __call__(self) -> None: ...
    def close(self) -> None: ...
    @property
    def status(self) -> dict[str, Any]: ...

type ServiceKind = Literal["query", "execute"]

@dataclass(frozen=True)
class HookContext:
    session_id: str
    agent_id: str
    run_id: str
    add_note: Callable[[str], None] = ...

@dataclass(frozen=True)
class Deny:
    reason: str

@dataclass(frozen=True)
class Modify:
    input: dict[str, Any]

@dataclass(frozen=True)
class Replace:
    result: dict[str, Any]

@dataclass(frozen=True)
class SessionRequestContext:
    binding: Any
    run_id: str
    agent_id: str
    session_id: str
    execution_owner: Any | None = None
    model: str | None = None

@dataclass(frozen=True)
class PreparedSessionDelivery:
    delivery_id: str
    content_hash: str
    entries: tuple[str, ...]
    settings_revision: str
    effect_kind: str = "before_request"

@dataclass(frozen=True)
class ToolBatchDecision:
    end: bool
    continuation: PreparedSessionDelivery | None = None

@dataclass(frozen=True)
class ToolCallDecision:
    effective_input: dict[str, Any]
    deny_reason: str | None = None
    deny_extension: str | None = None
    replacement: dict[str, Any] | None = None

class ExtensionUnavailableError(Exception): ...

@dataclass(frozen=True)
class InteractionButton:
    label: str
    data: str

@dataclass(frozen=True)
class InteractionEvent:
    platform: str
    channel_id: str
    chat_id: str
    user_id: str
    message_id: str
    data: str
    buttons: tuple[tuple[InteractionButton, ...], ...]
    text: str | None = None
    user_display_name: str | None = None
    thread_id: str | None = None

class InteractionResponder(typing.Protocol):
    async def answer(self, text: str | None = None, *, alert: bool = False) -> None: ...
    async def edit(
        self, *, text: str | None = None, buttons: list[list[InteractionButton]] | None = None
    ) -> None: ...

@dataclass(frozen=True)
class SettingsFieldDeclaration:
    key: str
    type: str
    label: str
    description: str | None = None
    default: Any = None
    required: bool = False
    env_key: str | None = None

class Database:
    def __init__(
        self,
        spec: Any,
        runtime: Any,
        *,
        database_id: str,
        data_dir: Path | None,
        workers: Any | None = None,
        restore_id: str | None = None,
    ) -> None: ...
    @property
    def name(self) -> str: ...
    @property
    def path(self) -> Path: ...
    @property
    def database_id(self) -> str: ...
    @property
    def data_dir(self) -> Path | None: ...
    @property
    def restore_id(self) -> str | None: ...
    @property
    def writer(self) -> sqlite3.Connection: ...
    def read(self) -> contextlib.AbstractContextManager[sqlite3.Connection]: ...
    def write[Result](
        self, operation: Callable[[sqlite3.Connection], Result], *, patience_s: float = 20.0
    ) -> Result: ...
    async def run_async[Result](
        self, function: Callable[..., Result], *arguments: Any, **keyword_arguments: Any
    ) -> Result: ...
    async def read_async[Result](
        self, operation: Callable[[sqlite3.Connection], Result], *, patience_s: float = 20.0
    ) -> Result: ...
    async def write_async[Result](
        self, operation: Callable[[sqlite3.Connection], Result], *, patience_s: float = 20.0
    ) -> Result: ...
    def backup(self, destination: Path, *, cancelled: Callable[[], bool] | None = None) -> bool: ...
    def checkpoint(self) -> None: ...
    def verify_read_write(self) -> None: ...
    def health(self) -> Any: ...
    def wal_active(self) -> bool: ...
    def reader_stats(self) -> tuple[int, int]: ...
    def live_connection_count(self) -> int: ...
    def is_closed(self) -> bool: ...
    def close(self) -> None: ...

class DatabaseError(Exception): ...

def write_profile_assignment[T](
    database: Database, names: Iterable[str | None], write: Callable[[sqlite3.Connection], T]
) -> T: ...

@dataclass(frozen=True)
class Migration:
    name: str
    breaks_older: bool = False
    apply: Callable[[sqlite3.Connection], None] | None = None
