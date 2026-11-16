# Public authoring declarations; runtime identities come from the installed host.
from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
)

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Any
    preflight: Any | None = None
    defer_secret_consumption: bool = False
    result_schema: dict[str, Any] | None = None
    summary: str | None = None
    contract: Any = field(init=False)
    internal: bool = False
    deferred: bool = False
    session_scoped: bool = False
    catalog_visible: bool = True
    family: str | None = None
    family_label: str | None = None
    activation: str = "configurable"
    activation_source: str | tuple[str, ...] | None = None
    requires_opt_in: bool = False
    constraints: tuple[str, ...] = ()
    display: Any = ...
    ready: Any | None = None
    readiness_hint: str | None = None
    readiness_retryable: bool = False
    extension: str | None = None
    parallel_safe: bool = True
    scheduling_target: Callable[[Any], str | None] | None = None
    execution_slot_required: bool = True
    open_input_schema: bool = False
    unadvertised_parameters: dict[str, Any] | None = None
    handler_validates_arguments: bool = False
    coerce_arguments: bool = True
    argument_normalizer: Callable[[Any], Any] | None = None
    definition_profile_resolver: Any | None = None
    announce_changes: tuple[str, ...] = ()
    secret_sinks: Mapping[str, str] = ...

    def __post_init__(self) -> None: ...

@dataclass(frozen=True)
class ToolContext:
    agent_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    tool_call_index: int
    workspace: Path
    vbot_root: Path
    data_root: Path
    permissions: Any
    session_generation_id: str | None = None
    approval_hook: Any | None = None
    approval_refresh_hook: Any | None = None
    approval_wait_hook: Any | None = None
    permission_access: Any = field(init=False)
    public_arguments: dict[str, Any] = field(init=False)
    secret_commit_hook: Callable[[], Awaitable[None]] | None = field(init=False)
    authority_recheck: Callable[[], None] | None = field(init=False)
    approval_rejected: bool = field(init=False)
    execution: dict[str, Any] = ...
    iteration_number: int = 0
    agent_model: str | None = None
    agent_thinking_effort: str | None = None
    execution_owner: Any | None = None
    cwd: Path | None = None
    project_id: str | None = None
    skill_project_id: str | None = None
    working_project_id: str | None = None
    memory_prompt_mode: Any = "agent_user"
    project_memory_enabled: bool = False
    skill_agent_id: str | None = None
    run_kind: Any | None = None
    direct_user_input: bool = False
    subagent_session: bool = False
    programmatic: bool = False
    parent_tool_name: str | None = None
    tool_call_hook: Any | None = None
    reply_surface: Any | None = None
    emit_hook: Any | None = None
    cancellation_hook: Any | None = None
    cancel_registration_hook: Any | None = None
    cancel_check_hook: Any | None = None
    background_registration_hook: Callable[[Callable[[], bool]], None] | None = None
    note_hook: Any | None = None
    skill_activation_hook: Any | None = None
    result_persisted_hook: Any | None = None
    delivery_receipt_hook: Any | None = None
    request_turn_end_hook: Any | None = None
    result_payload_hook: Any | None = None
    loadable_tools: Mapping[str, dict[str, Any]] = ...
    unloadable_tools: frozenset[str] = ...
    tool_load_hook: Any | None = None
    worked_paths_hook: Any | None = None
    allowed_skills: Sequence[str] | None = None
    skill_ceiling: frozenset[str] | None = None
    skill_env_keys: Sequence[str] = ...
    tool_settings: Mapping[str, Any] | None = None
    delegation_bindings: Mapping[str, str | int] = ...
    tool_restriction: Sequence[str] | None = None
    tool_denial_resolver: Callable[[str], str | None] | None = None
    session_tool_grants: Sequence[str] = ...
    input_contract: Any | None = None
    result_contract: Any | None = field(init=False)
    offered_tools: Collection[str] | None = None
    dispatch_allowed_tools: Sequence[str] | None = field(init=False)
    presentation_facts: list[dict[str, Any]] = ...
    presentation_media: list[dict[str, Any]] = ...
    presentation_details: list[dict[str, Any]] = ...
    presentation_secret_uses: list[str] = ...
    secret_literals: dict[str, str] = ...
    prepared_secret_literals: dict[str, str] = ...
    presentation_widget: dict[str, Any] = ...
    presentation_portal: dict[str, Any] = ...
    result_media: list[dict[str, Any]] = ...
    change_tracker: Any | None = None

    def __post_init__(self) -> None: ...
    async def request_approval(
        self,
        reason: str,
        requirements: Sequence[Any],
        *,
        arguments: dict[str, Any],
        cwd: Path | None = None,
    ) -> bool: ...
    def invocation_approved(
        self, arguments: dict[str, Any], *, cwd: Path | None = None
    ) -> bool: ...
    def mask_prepared_secrets[T](self, value: T) -> T: ...
    async def commit_prepared_secrets(self) -> None: ...
    def add_permission_recheck(self, callback: Callable[[], Awaitable[None]]) -> None: ...
    async def revalidate_permissions(
        self, *, enforce_cancel: bool = True, force_refresh: bool = False
    ) -> None: ...
    async def authorize(
        self,
        requirements: Sequence[Any],
        *,
        reason: str,
        arguments: dict[str, Any],
        cwd: Path | None = None,
    ) -> bool: ...
    @property
    def skill_subject_id(self) -> str: ...
    @property
    def effective_cwd(self) -> Path: ...
    def path_target(self, path: str | Path, *, access: Any) -> Path: ...
    def resolve_path(
        self, path: str | Path, *, access: Any, follow_final_link: bool = True
    ) -> Path: ...
    def can_call(self, tool_name: str) -> bool: ...
    def tool_denial(self, tool_name: str) -> str | None: ...
    def offers(self, tool_name: str) -> bool: ...
    def add_display_count(self, value: int, unit: str, *, at_least: bool = False) -> None: ...
    def add_display_line_changes(self, *, added: int, removed: int) -> None: ...
    def add_display_file_change(
        self,
        path: str,
        change: str,
        before: str | None,
        after: str | None,
        *,
        destination: str | None = None,
    ) -> dict[str, Any]: ...
    def add_display_notice(self, level: str, text: str, *, subject: str | None = None) -> None: ...
    def record_secret_uses(self, uses: Sequence[str]) -> None: ...
    def add_display_text(self, label: str, text: str) -> None: ...
    def add_display_results(self, items: Sequence[Mapping[str, str | None]]) -> None: ...
    def add_display_media(
        self,
        path: Path | str,
        media_type: str,
        *,
        filename: str | None = None,
        source: bool = False,
    ) -> None: ...
    def show_display_widget(self, widget_id: str, title: str) -> None: ...
    def show_display_portal(self, portal_id: str, title: str, port: int) -> None: ...
    def add_display_memory_changes(
        self,
        scope: str,
        changes: Sequence[Mapping[str, str | None]],
        *,
        revision: int | None = None,
    ) -> None: ...
    async def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...
    def is_cancelled(self) -> bool: ...
    def on_cancel(self, callback: Any) -> None: ...
    def was_cancelled_by_user(self) -> bool: ...
    def add_note(self, content: str) -> None: ...
    def activate_skill(
        self, name: str, content: str, package: Any | None = None
    ) -> bool | None: ...
    @property
    def tool_calls_available(self) -> bool: ...
    async def call_tool(
        self, name: str, arguments: dict[str, Any], positional: Sequence[Any] = ()
    ) -> dict[str, Any]: ...
    def after_result_persisted(self, callback: Any) -> None: ...
    def record_delivery_receipt(
        self, receipt_id: str, content_hash: str, effect_kind: str
    ) -> None: ...
    @property
    def result_payloads_available(self) -> bool: ...
    def attach_result_payload(self, payload: Any) -> str: ...
    def request_turn_end(self) -> None: ...
    def record_loaded_tools(self, names: Sequence[str]) -> None: ...
    def record_worked_paths(self, *paths: Path) -> None: ...

class ToolError(Exception): ...

def tool_success(
    data: dict[str, Any], artifacts: list[dict[str, Any]] | None = ...
) -> dict[str, Any]: ...
def tool_failure(
    code: str,
    message: str,
    artifacts: list[dict[str, Any]] | None = ...,
    *,
    retryable: bool | None = ...,
    attempts_made: int | None = ...,
) -> dict[str, Any]: ...
