"""Extension declaration values, identity, and capability diagnostics."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.extensions.interactions import (
    InteractionHandlerDeclaration,
)
from core.extensions.operations import ExtensionOperations
from core.extensions.settings_schema import SettingsFieldDeclaration
from core.utils.logging import get_logger

_LOGGER = get_logger("extensions")

# Public extension API version. Bumped when the extension contract changes in a
# way third-party extensions can detect via their manifest ``api_version``.
API_VERSION = 6
HookHandler = Callable[..., Any]
LifecycleHandler = Callable[[], Any]
CommandHandler = Callable[..., Any]
RegisteredHandler = tuple[str, HookHandler]


# Injected by chat so tool-result-envelope schema knowledge stays in the chat
# domain: given (extension_name, candidate dict) it returns the validated
# envelope or ``None`` when the candidate is rejected.
ToolResultValidator = Callable[[str, dict[str, Any]], "dict[str, Any] | None"]
ExtensionStatus = Literal["loaded", "failed", "disabled", "overridden"]


def _is_page_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value) is not None
    )


def _ignore_note(text: str) -> None:
    """Default no-op note sink for contexts built without a live session."""
    return None


@dataclass(frozen=True)
class HookContext:
    """First positional argument to every handler. Constructed in ``core/chat/``.

    ``add_note`` appends a kernel-internal ``role: "note"`` entry to the active
    session; chat wires it to ``session.add_note`` when constructing the context.
    """

    session_id: str
    agent_id: str
    run_id: str
    add_note: Callable[[str], None] = _ignore_note


@dataclass(frozen=True)
class Deny:
    """``tool_call`` decision: stop the pipeline and refuse execution with a reason."""

    reason: str


@dataclass(frozen=True)
class Modify:
    """``tool_call`` decision: replace the tool input; the pipeline keeps going."""

    input: dict[str, Any]


@dataclass(frozen=True)
class Replace:
    """``tool_call`` decision: skip execution and use this result envelope instead."""

    result: dict[str, Any]


@dataclass(frozen=True)
class ToolCallDecision:
    """Outcome of the ``tool_call`` decision pipeline handed back to chat.

    Exactly one disposition holds:

    - proceed — both ``deny_reason`` and ``replacement`` are ``None``: execute the
      tool with ``effective_input`` (reflects any ``Modify`` applied in the pipeline).
    - denied — ``deny_reason``/``deny_extension`` set: the tool is not executed and
      chat builds a deny error envelope naming the extension.
    - replaced — ``replacement`` is a validated result envelope used as the result;
      the tool is not executed.
    """

    effective_input: dict[str, Any]
    deny_reason: str | None = None
    deny_extension: str | None = None
    replacement: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExtensionManifest:
    """Optional ``extension.json`` enrichment for a directory-form extension.

    Identity stays the filesystem name; ``display_name`` (the manifest ``name``
    field) is display-only. ``api_version`` greater than :data:`API_VERSION`
    fails the extension at load time.
    """

    version: str | None = None
    description: str | None = None
    api_version: int | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class ToolDeclaration:
    """One ``api.register_tool`` declaration, mirroring ``ToolRegistry.register``.

    Collected during ``register`` and applied into the runtime ``ToolRegistry``
    after the last built-in tool is registered. ``display`` is forwarded
    untouched (a ``core.tools.ToolDisplay`` or ``None``) so the extensions
    module needs no dependency on the tools domain. ``ready`` is the optional
    zero-arg readiness predicate forwarded the same way — a not-ready tool stays
    registered but is hidden from the model-facing surfaces (see
    ``core.tools.tool_is_ready``).
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    internal: bool = False
    catalog_visible: bool = True
    requires_opt_in: bool = False
    display: Any = None
    ready: Callable[[], bool] | None = None
    # Optional English hint explaining the tool's readiness precondition, forwarded
    # verbatim into ``ToolRegistry.register`` and surfaced by ``tool.list``.
    readiness_hint: str | None = None
    result_schema: dict[str, Any] | None = None
    parallel_safe: bool = True
    open_input_schema: bool = False
    coerce_arguments: bool = True
    session_scoped: bool = False
    activation: str = "configurable"
    # Local family id declared through ``register_tool_family``. The apply phase
    # namespaces it by Extension identity before it reaches ToolRegistry.
    family: str | None = None


@dataclass(frozen=True)
class ToolFamilyDeclaration:
    """One Extension-local Tool family id and its human-facing label."""

    id: str
    label: str


@dataclass(frozen=True)
class SessionPromptBlockDeclaration:
    slug: str
    render: Callable[..., str]


@dataclass(frozen=True)
class SessionRuntimeDeclaration:
    before_request: Callable[..., Any]
    run_finished: Callable[..., Any]
    quiesce: Callable[..., Any]
    acknowledge_delivery: Callable[..., Any] | None = None
    reconcile_tool_batch: Callable[..., Any] | None = None


@dataclass(frozen=True)
class SessionCapability:
    """The effective private capability set for one bound temporary Session."""

    tool_names: tuple[str, ...]
    prompt_blocks: tuple[SessionPromptBlockDeclaration, ...]
    runtime: SessionRuntimeDeclaration | None
    identity: ExtensionRegistrationIdentity


class SessionCapabilityExpiredError(RuntimeError):
    """A bound Session callback outlived its Extension registration."""


@dataclass(frozen=True)
class SessionRequestContext:
    """Narrow identity supplied to an owner at a safe Session request boundary."""

    binding: Any
    run_id: str
    agent_id: str
    session_id: str
    execution_owner: Any | None = None


@dataclass(frozen=True)
class PreparedSessionDelivery:
    """One owner-prepared note batch whose receipt is committed by Chat."""

    delivery_id: str
    content_hash: str
    entries: tuple[str, ...]
    settings_revision: str
    effect_kind: str = "before_request"


@dataclass(frozen=True)
class ToolBatchDecision:
    """Owner reconciliation after every sibling Tool Result is durable."""

    end: bool
    continuation: PreparedSessionDelivery | None = None


@dataclass(frozen=True)
class PageDeclaration:
    """One Extension-owned page asset contributed to the application shell."""

    page_id: str
    title: str
    entry: str
    icon: str = "network"


@dataclass(frozen=True)
class ExtensionRegistrationIdentity:
    """Opaque identity for declarations belonging to one loaded registry epoch."""

    name: str
    epoch: str


@dataclass(frozen=True)
class CommandDeclaration:
    """One ``api.register_command`` declaration for Chat-owned application.

    The Extensions domain stores only primitive metadata plus the handler. Chat
    validates and applies the declaration later so command recognition,
    scheduling, execution, and outcome semantics remain in ``CommandDispatcher``.
    """

    name: str
    description: str
    handler: CommandHandler
    argument: str = "optional"
    catalog_result: str = "notice"
    execution_mode: str = "serialized"
    argument_execution_mode: str | None = None
    unavailable_surfaces: object = ()


@dataclass(frozen=True)
class RecallBackendDeclaration:
    """One ``api.register_recall_backend`` declaration: name + backend factory.

    The factory is a ``core.recall.RecallBackendFactory``
    (``RecallBackendContext -> RecallBackend``); kept loosely typed so the
    extensions module stays decoupled from the recall domain.
    """

    name: str
    factory: Callable[..., Any]


@dataclass(frozen=True)
class PromptBlockDeclaration:
    """One ``api.register_prompt_block`` declaration (D6): a System Prompt block.

    Mirrors a tool/recall declaration — collected during ``register`` and turned
    into a ``core.prompts.BlockDefinition`` by the registry's
    :meth:`ExtensionRegistry.prompt_block_declarations` accessor (a lazy import, so
    the extensions module stays decoupled from the prompts domain). An extension's
    block is sourced/owned ``extension:<name>`` so gate 2 only renders it while the
    extension is loaded. Exactly one of ``default_text`` (static, editable via the
    override cascade) / ``render`` (dynamic, non-editable, build-time function) is
    set — the same static-vs-dynamic split as a core block.
    """

    slug: str
    default_text: str | None = None
    render: Callable[..., str] | None = None


@dataclass
class ExtensionDeclarations:
    """What an extension declares through :class:`ExtensionAPI` during ``register``.

    Collected per extension; applied to the dispatch table / domain registries
    or fired by the registry only after every extension has registered.
    """

    hooks: dict[str, list[HookHandler]] = field(default_factory=lambda: defaultdict(list))
    startup: list[LifecycleHandler] = field(default_factory=list)
    shutdown: list[LifecycleHandler] = field(default_factory=list)
    tools: list[ToolDeclaration] = field(default_factory=list)
    tool_families: list[ToolFamilyDeclaration] = field(default_factory=list)
    commands: list[CommandDeclaration] = field(default_factory=list)
    pages: list[PageDeclaration] = field(default_factory=list)
    recall_backends: list[RecallBackendDeclaration] = field(default_factory=list)
    prompt_blocks: list[PromptBlockDeclaration] = field(default_factory=list)
    session_prompt_blocks: list[SessionPromptBlockDeclaration] = field(default_factory=list)
    session_runtime: SessionRuntimeDeclaration | None = None
    interaction_handlers: list[InteractionHandlerDeclaration] = field(default_factory=list)
    settings_schema: list[SettingsFieldDeclaration] | None = None
    operations: ExtensionOperations | None = None


@dataclass
class ExtensionRecord:
    """One discovered extension and the outcome of loading it.

    ``name`` is the identity (directory or file name). ``status`` is ``loaded``
    (importable and registered), ``failed`` (import/register/manifest error —
    ``error`` carries the detail), ``disabled`` (listed disabled, never
    imported), or ``overridden`` (a later same-name copy an earlier root already
    claimed, never imported — see :meth:`ExtensionRegistry.load`).
    ``declarations`` are only meaningful for ``loaded`` records.
    """

    name: str
    root_path: Path
    entry_path: Path
    status: ExtensionStatus
    error: str | None = None
    manifest: ExtensionManifest | None = None
    declarations: ExtensionDeclarations = field(default_factory=ExtensionDeclarations)
    # Non-fatal per-capability diagnostics (e.g. a tool name collision skipped a
    # single tool). The extension still ``loaded``; only that capability dropped.
    capability_errors: list[str] = field(default_factory=list)
    # The winning record's ``entry_path`` (as a string) when this record was
    # shadowed (``status == "overridden"``); ``None`` for every other status.
    overridden_by: str | None = None


def _diagnose_capability(record: ExtensionRecord, message: str) -> None:
    """Record a non-fatal capability diagnostic and log it at ``warning``."""
    record.capability_errors.append(message)
    _LOGGER.warning("Extension %r %s", record.name, message)
