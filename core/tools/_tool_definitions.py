"""Immutable Tool definitions and definition validation."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from core.tools._tool_context import ToolHandler
from core.tools._tool_display import ToolDisplay
from core.tools.availability import (
    TOOL_ACTIVATION_CONFIGURABLE,
    TOOL_ACTIVATION_FOLLOWS,
    TOOL_ACTIVATION_KINDS,
)
from core.tools.contracts import JsonObject, ToolContract, compile_tool_contract
from core.utils.errors import VBotError
from core.utils.logging import get_logger

_LOGGER = get_logger("tools")
ToolReadinessPredicate = Callable[[], bool]
# What a Tool's ``announce_changes`` can name: its description, any part of its
# parameters, or one parameter as ``parameters.<name>``.
ANNOUNCE_DESCRIPTION = "description"
ANNOUNCE_PARAMETERS = "parameters"
_ANNOUNCE_PARAMETER_PREFIX = "parameters."
# The longest one-line summary an On-demand Tool listing shows for a Tool.
MAX_TOOL_SUMMARY_LENGTH = 200
_WHITESPACE = re.compile(r"\s+")


class ToolError(VBotError):
    """Base class for expected tool registry errors."""


class ToolNotFoundError(ToolError):
    """Raised when a tool name is unknown to the registry."""


class ToolNotAllowedError(ToolError):
    """Raised when a tool exists but is not on the caller's allowlist."""


class SessionToolUnavailableError(ToolError):
    """Raised when a Session-scoped tool has no grant in the current Session."""


class InvalidToolResultError(ValueError):
    """Raised when a tool handler returns a value that is not a valid result envelope.

    Subclasses ``ValueError`` so existing callers that catch ``ValueError`` keep
    working, while allowing the chat loop to distinguish an invalid handler
    result from invalid tool arguments without inspecting message text.
    """


class DuplicateToolError(ToolError):
    """Raised when registering a tool name more than once."""


@dataclass(frozen=True)
class ToolFamily:
    """Presentation metadata for a user-recognizable group of Tools."""

    id: str
    label: str
    extension: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Tool family id must be a non-empty string")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Tool family label must be a non-empty string")


@dataclass(frozen=True)
class ToolDefinitionProfileContext:
    """Stable configuration identity used to select model-facing Tool profiles."""

    agent_id: str
    project_id: str | None = None


@dataclass(frozen=True)
class ToolDefinitionProfile:
    """One immutable model-facing definition selected from stable configuration."""

    key: str
    description: str
    parameters: JsonObject = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("Tool definition profile key must be a non-empty string")
        if not isinstance(self.description, str) or not self.description:
            raise ValueError("Tool definition profile description must be a non-empty string")
        if not isinstance(self.parameters, dict):
            raise ValueError("Tool definition profile parameters must be a JSON Schema object")
        object.__setattr__(self, "parameters", copy.deepcopy(self.parameters))


ToolDefinitionProfileResolver = Callable[
    [ToolDefinitionProfileContext],
    ToolDefinitionProfile | None,
]


@dataclass(frozen=True)
class Tool:
    """A callable tool exposed to an agent."""

    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler
    result_schema: JsonObject | None = field(default=None, repr=False)
    # Optional one-sentence, Model-facing summary that lists the Tool where its
    # full definition is not shown (On-demand Tools). ``None`` summarizes the
    # description's first sentence (:func:`tool_summary`).
    summary: str | None = None
    contract: ToolContract = field(init=False, repr=False, compare=False)
    internal: bool = False
    # Discoverable through a stable routing Tool; still registered for policy and dispatch.
    deferred: bool = False
    # A Session-scoped tool is configurable nowhere and model-visible only when
    # the current Session supplies a matching persisted-state grant.
    session_scoped: bool = False
    # Public catalog projections hide capability-local tools while the registry
    # continues to retain them for collision checks, binding, and dispatch.
    catalog_visible: bool = True
    # Optional presentation grouping. Standalone Tools leave this unset; a family
    # is useful only when multiple Tools share one user-recognizable capability.
    family: str | None = None
    # Human-facing label from the registry declaration. This lets transport
    # surfaces render Extension families without understanding their ids.
    family_label: str | None = None
    # How policy activates this Tool. Configurable Tools are selected directly;
    # followed, memory-mode, and Session-grant Tools are automatic. A followed
    # Tool names its source, or a tuple of sources it follows while any is active.
    activation: str = TOOL_ACTIVATION_CONFIGURABLE
    activation_source: str | tuple[str, ...] | None = None
    requires_opt_in: bool = False
    # Stable machine-readable preconditions. Runtime readiness and Chat route
    # availability remain separate live projections.
    constraints: tuple[str, ...] = ()
    display: ToolDisplay = field(default_factory=ToolDisplay)
    # Optional readiness predicate (zero-arg, cheap, I/O-free) — e.g. "the token
    # is a non-empty string", never a network ping, since it runs on every
    # prompt/tool-definition build. ``None`` means always ready. A predicate that
    # raises is treated as **not ready** (logged once at ``warning``). Readiness
    # is a separate axis from the allowlist: a not-ready tool stays registered
    # (its persisted permissions survive) but is filtered out of the model-facing
    # surfaces and returns a clean failure envelope on a direct dispatch.
    ready: ToolReadinessPredicate | None = None
    # Optional human-readable hint explaining what makes this tool ready — shown by
    # the ``tool.list`` RPC so an accessor can tell the user why a not-ready tool is
    # unavailable (e.g. "set the extension's token"). Server-delivered English text
    # like the description, never frontend i18n. ``None`` when the tool has no
    # readiness precondition to explain.
    readiness_hint: str | None = None
    # Whether a not-ready Tool's failure is transient: ``tool_not_ready`` carries it
    # as ``retryable``. True only when the hint names a retry that can succeed (an
    # MCP remote Tool whose connection reconnects); a missing setup is not.
    readiness_retryable: bool = False
    # The name of the extension that registered this tool, or ``None`` for a
    # built-in tool. Set at extension-tool apply time so ``tool.list`` can attribute
    # a tool to its owning extension.
    extension: str | None = None
    parallel_safe: bool = True
    # Coordinators wait on separately bounded child work and must not hold its slots.
    execution_slot_required: bool = True
    open_input_schema: bool = False
    # Root parameters the handler accepts without offering them to the Model,
    # such as options copied from older conversation history, mapped to their
    # schemas. Dispatch validates them like advertised parameters but never
    # lists them in Provider definitions or argument errors.
    unadvertised_parameters: JsonObject | None = None
    # Independently executed batches may need malformed items to reach the
    # handler so valid siblings still run. The handler then owns complete root
    # and per-item validation; the precise Provider schema remains unchanged.
    handler_validates_arguments: bool = False
    coerce_arguments: bool = True
    argument_normalizer: Callable[[Any], Any] | None = field(
        default=None, repr=False, compare=False
    )
    definition_profile_resolver: ToolDefinitionProfileResolver | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # The parts of this Tool's definition whose change Chat announces to a
    # Session that already knows the Tool (:func:`definition_change_announced`).
    # Chat keeps a Session's Tool list unchanged until Compaction; a change of
    # any part not named here reaches the Model only with the next prompt epoch.
    announce_changes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.summary is not None:
            if not isinstance(self.summary, str) or not self.summary.strip():
                raise ValueError("Tool summary must be a non-empty string or None")
            summary = _WHITESPACE.sub(" ", self.summary).strip()
            if len(summary) > MAX_TOOL_SUMMARY_LENGTH:
                raise ValueError(
                    f"Tool summary must be at most {MAX_TOOL_SUMMARY_LENGTH} characters"
                )
            object.__setattr__(self, "summary", summary)
        if self.argument_normalizer is not None and not callable(self.argument_normalizer):
            raise ValueError("argument_normalizer must be callable")
        object.__setattr__(
            self, "announce_changes", _checked_announce_changes(self.announce_changes)
        )
        if self.activation not in TOOL_ACTIVATION_KINDS:
            raise ValueError(f"Unsupported Tool activation: {self.activation}")
        if not isinstance(self.requires_opt_in, bool):
            raise ValueError("requires_opt_in must be a boolean")
        if self.requires_opt_in and (
            self.internal or self.session_scoped or self.activation != TOOL_ACTIVATION_CONFIGURABLE
        ):
            raise ValueError("Only configurable, non-internal Tools can require opt-in")
        if self.activation == TOOL_ACTIVATION_FOLLOWS:
            if not self.activation_source:
                raise ValueError("A followed Tool requires activation_source")
            if not isinstance(self.activation_source, str):
                sources = tuple(self.activation_source)
                if any(not isinstance(name, str) or not name for name in sources) or len(
                    set(sources)
                ) != len(sources):
                    raise ValueError("activation_source must name distinct Tools")
                object.__setattr__(self, "activation_source", sources)
        elif self.activation_source is not None:
            raise ValueError("activation_source is only valid for a followed Tool")
        if self.family is not None and not self.family.strip():
            raise ValueError("Tool family must be a non-empty string or None")
        if any(not isinstance(item, str) or not item.strip() for item in self.constraints):
            raise ValueError("Tool constraints must be non-empty strings")
        contract = compile_tool_contract(
            name=self.name,
            input_schema=self.parameters,
            result_schema=self.result_schema,
            parallel_safe=self.parallel_safe,
            require_closed_input=not self.open_input_schema,
        )
        object.__setattr__(
            self,
            "unadvertised_parameters",
            _checked_unadvertised_parameters(self.unadvertised_parameters, contract.input_schema),
        )
        object.__setattr__(self, "parameters", copy.deepcopy(contract.input_schema))
        object.__setattr__(self, "result_schema", copy.deepcopy(contract.result_schema))
        object.__setattr__(self, "contract", contract)


def _checked_announce_changes(parts: Any) -> tuple[str, ...]:
    if isinstance(parts, str) or not isinstance(parts, (list, tuple)):
        raise ValueError("announce_changes must be a list of definition parts")
    for part in parts:
        if not isinstance(part, str) or not (
            part in (ANNOUNCE_DESCRIPTION, ANNOUNCE_PARAMETERS)
            or (
                part.startswith(_ANNOUNCE_PARAMETER_PREFIX)
                and part[len(_ANNOUNCE_PARAMETER_PREFIX) :].strip()
            )
        ):
            raise ValueError(
                "announce_changes entries must be 'description', 'parameters' or "
                f"'parameters.<name>', not {part!r}"
            )
    return tuple(dict.fromkeys(parts))


def definition_change_announced(
    parts: Sequence[str], known: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """Return whether a part named in ``announce_changes`` differs between two definitions.

    Both are Model definitions (``{name, description, parameters}``): the one the
    Model was last told about and the current one. ``parameters.<name>`` covers
    that parameter's schema and whether it is required.
    """

    for part in parts:
        if part == ANNOUNCE_DESCRIPTION:
            if known.get("description") != current.get("description"):
                return True
        elif part == ANNOUNCE_PARAMETERS:
            if known.get("parameters") != current.get("parameters"):
                return True
        elif _parameter_shape(known, part) != _parameter_shape(current, part):
            return True
    return False


def _parameter_shape(definition: Mapping[str, Any], part: str) -> tuple[Any, bool]:
    name = part[len(_ANNOUNCE_PARAMETER_PREFIX) :]
    parameters = definition.get("parameters")
    if not isinstance(parameters, Mapping):
        return None, False
    properties = parameters.get("properties")
    required = parameters.get("required")
    schema = properties.get(name) if isinstance(properties, Mapping) else None
    return schema, isinstance(required, list) and name in required


def _checked_unadvertised_parameters(
    parameters: JsonObject | None, input_schema: JsonObject
) -> JsonObject | None:
    if not parameters:
        return None
    if not isinstance(parameters, dict):
        raise ValueError("unadvertised_parameters must map parameter names to schemas")
    advertised = input_schema.get("properties", {})
    for name, schema in parameters.items():
        if not isinstance(name, str) or not name or not isinstance(schema, dict):
            raise ValueError("unadvertised_parameters must map parameter names to schemas")
        if name in advertised:
            raise ValueError(f"unadvertised parameter is also advertised: {name}")
        Draft202012Validator.check_schema(schema)
    return copy.deepcopy(parameters)


def tool_summary(tool: Tool, description: str | None = None) -> str:
    """Return the one-line summary that lists *tool* without its full definition.

    The Tool's declared ``summary`` wins. Otherwise the first sentence of
    *description* (the Tool's own description when ``None``, else the one a
    Definition Profile or route resolved) is used: cut at the first ". " or line
    break, whitespace collapsed, and capped at :data:`MAX_TOOL_SUMMARY_LENGTH`
    characters with an ellipsis.
    """
    if tool.summary is not None:
        return tool.summary
    return first_sentence_summary(tool.description if description is None else description)


def first_sentence_summary(description: str) -> str:
    """Return *description*'s first sentence as one bounded line (see :func:`tool_summary`)."""
    text = description.strip()
    ends = [index for index in (text.find(". ") + 1, text.find("\n")) if index > 0]
    if ends:
        text = text[: min(ends)]
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > MAX_TOOL_SUMMARY_LENGTH:
        text = text[: MAX_TOOL_SUMMARY_LENGTH - 1].rstrip() + "…"
    return text


def tool_is_ready(tool: Tool) -> bool:
    """Return whether *tool* is ready to be offered right now.

    A tool with no predicate is always ready. A predicate that raises is logged
    once at ``warning`` and treated as **not ready** — a broken predicate must
    never take a prompt/tool-definition build down or make a tool spuriously
    available.
    """
    if tool.ready is None:
        return True
    try:
        return bool(tool.ready())
    except Exception as error:
        _LOGGER.warning("Tool %s readiness predicate raised: %s", tool.name, error)
        return False
