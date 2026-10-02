"""Wire profiles: how vBot talks to one Model on one Provider Connection.

The Model catalog (``core/models``) says what a Model *is*: limits, modalities,
pricing and its reasoning capability. A wire profile says how vBot must *speak*
to that Model on one Connection: which protocol and endpoint family, which
request fields carry the output limit and sampling values, how an Agent's
reasoning effort is rendered, where readable reasoning comes back, what
Reasoning Replay must return, which media the wire carries, and whether the
Model may be used at all.

Profiles are resolved per ``(provider, connection, model id)`` from layers:

1. Code defaults of the resolved protocol (``_wire_protocol_defaults``).
2. ``defaults`` of the Provider's wire profile file (``resources/wire/<provider>.json``,
   or the ``wire`` block of a Custom Provider).
3. ``protocols[<protocol>]`` of that file.
4. ``connections[<connection>]`` of that file.
5. Catalog-derived values (reasoning capability, reported wire hints).
6. Matching ``rules`` in file order.
7. Observations learned from live traffic (learnable fields only).
8. The Model entry's ``set``.
9. The Model entry's ``connections[<connection>]``.

The protocol itself is decided first, from the Model entry, the last matching
rule, the catalog's protocol hint, the Connection, the file defaults and the
Adapter's default protocol, in that order.

Every resolved field remembers which layer set it (``WireProfile.provenance``),
and the profile carries a status: ``verified`` when its Model entry records a
verification for this Connection, ``configured`` when a Model entry or rule
shaped it, ``inferred`` when only defaults, catalog facts and observations did.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from core.providers.reasoning import (
    DEFAULT_REASONING_REPLAY_FIDELITY,
    DEFAULT_REASONING_REPLAY_POLICY,
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    THINKING_EFFORT_RANKS,
    ReasoningIntent,
    ReasoningReplayFidelity,
    ReasoningReplayPolicy,
    effort_budget_tokens,
    normalize_thinking_effort,
    snap_effort,
)

Protocol = Literal["chat_completions", "messages", "responses", "gemini", "ollama_chat"]
"""Wire protocol families vBot implements.

- ``chat_completions``: OpenAI-compatible ``/chat/completions``.
- ``messages``: Anthropic-compatible ``/messages``.
- ``responses``: OpenAI-compatible ``/responses`` (stateless item replay).
- ``gemini``: Google ``generateContent`` / ``streamGenerateContent``.
- ``ollama_chat``: Ollama native ``/api/chat``.
"""

PROTOCOLS: tuple[Protocol, ...] = (
    "chat_completions",
    "messages",
    "responses",
    "gemini",
    "ollama_chat",
)

ReasoningDialect = Literal[
    "none",
    "reasoning_effort",
    "openrouter_reasoning",
    "nous_reasoning",
    "thinking_toggle",
    "thinking_toggle_with_effort",
    "minimax_split",
    "minimax_thinking",
    "mistral_effort",
    "anthropic_thinking",
    "responses_reasoning",
    "gemini_thinking",
    "ollama_think",
]
"""How a reasoning decision is spelled on the wire (rendered by the protocol codec).

- ``none``: the wire takes no reasoning control; nothing is sent.
- ``reasoning_effort``: top-level ``reasoning_effort: <level>``; off is the
  ``none`` level.
- ``openrouter_reasoning``: ``reasoning: {effort|enabled|max_tokens}`` plus
  ``include_reasoning``.
- ``nous_reasoning``: ``reasoning: {enabled: true, effort}``.
- ``thinking_toggle``: ``thinking: {type: enabled|disabled[, keep]}``.
- ``thinking_toggle_with_effort``: the toggle plus ``reasoning_effort``.
- ``minimax_split``: ``reasoning_split: true``; no effort control.
- ``minimax_thinking``: ``thinking: {type: adaptive|disabled}`` plus
  ``reasoning_split``.
- ``mistral_effort``: ``reasoning_effort: high|none``.
- ``anthropic_thinking``: ``thinking`` adaptive with ``output_config.effort``
  for effort ladders, ``enabled`` with ``budget_tokens`` for budgets,
  ``disabled`` for off.
- ``responses_reasoning``: ``reasoning: {effort, summary}`` and the encrypted
  reasoning ``include``.
- ``gemini_thinking``: ``generationConfig.thinkingConfig``.
- ``ollama_think``: ``think: true|false|<level>``.
"""

REASONING_DIALECTS: tuple[ReasoningDialect, ...] = (
    "none",
    "reasoning_effort",
    "openrouter_reasoning",
    "nous_reasoning",
    "thinking_toggle",
    "thinking_toggle_with_effort",
    "minimax_split",
    "minimax_thinking",
    "mistral_effort",
    "anthropic_thinking",
    "responses_reasoning",
    "gemini_thinking",
    "ollama_think",
)

ProfileStatus = Literal["verified", "configured", "inferred"]
PROFILE_STATUSES: tuple[ProfileStatus, ...] = ("verified", "configured", "inferred")

AdmissionState = Literal["available", "restricted", "retired"]
"""Whether vBot may send requests for the Model on this Connection.

- ``available``: normal use.
- ``restricted``: the Provider limits the Model to other clients or plans; vBot
  refuses before network I/O with the profile's message.
- ``retired``: the Provider no longer serves the Model; vBot refuses likewise.
"""
ADMISSION_STATES: tuple[AdmissionState, ...] = ("available", "restricted", "retired")

ParameterMode = Literal["send", "drop", "drop_while_thinking"]
PARAMETER_MODES: tuple[ParameterMode, ...] = ("send", "drop", "drop_while_thinking")

OutOfRange = Literal["clamp", "drop"]
OUT_OF_RANGE_POLICIES: tuple[OutOfRange, ...] = ("clamp", "drop")

OutputLimitField = Literal["max_tokens", "max_completion_tokens", "max_output_tokens"]
OUTPUT_LIMIT_FIELDS: tuple[OutputLimitField, ...] = (
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
)

ToolSchemaProfile = Literal["explicit_non_strict", "omit_strict"]
TOOL_SCHEMA_PROFILES: tuple[ToolSchemaProfile, ...] = ("explicit_non_strict", "omit_strict")

ToolCallIdProfile = Literal["none", "anthropic", "mistral", "responses"]
TOOL_CALL_ID_PROFILES: tuple[ToolCallIdProfile, ...] = ("none", "anthropic", "mistral", "responses")

PromptCacheStyle = Literal["none", "anthropic_breakpoints"]
PROMPT_CACHE_STYLES: tuple[PromptCacheStyle, ...] = ("none", "anthropic_breakpoints")

BudgetStrategy = Literal["fraction_of_max", "absolute"]
BUDGET_STRATEGIES: tuple[BudgetStrategy, ...] = ("fraction_of_max", "absolute")

SnapRule = Literal["down", "up"]
SNAP_RULES: tuple[SnapRule, ...] = ("down", "up")

OFF_RENDERS: tuple[str, ...] = ("auto", "omit", "enabled", "lowest")
"""How the Agent effort ``none`` renders (``ReasoningWire.off``).

- ``auto``: an ``off`` decision the dialect spells natively (``effort_level``
  ``"none"`` when the effective ladder has a ``none`` rung and the Model is known
  to reason); ``lowest`` instead when the Model's reasoning is mandatory.
- ``omit``: no reasoning field; the Provider default applies.
- ``enabled``: reasoning stays on (always-on Models).
- ``lowest``: the lowest active rung of the ladder.
- an effort level name: that level (a verified minimum).
"""

UNSET_RENDERS: tuple[str, ...] = ("omit", "enabled")
"""How "no effort selected" renders (``ReasoningWire.unset``): ``omit`` sends
no reasoning field, ``enabled`` sends the dialect's plain enabled spelling, and
an effort level name behaves as if the Agent had selected that level."""

JsonValue = Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ParameterRule:
    """What happens to one optional request parameter (for example ``temperature``).

    ``mode`` decides whether the value is sent at all; ``minimum``/``maximum``
    bound it (``exclusive_minimum`` makes the lower bound exclusive) and
    ``out_of_range`` decides whether an out-of-range value is clamped into the
    range or dropped.
    """

    mode: ParameterMode = "send"
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    out_of_range: OutOfRange = "clamp"

    def bounded(self, value: float) -> float | None:
        """Return ``value`` within the bounds, or ``None`` when it must be dropped."""

        low, high = self.minimum, self.maximum
        too_low = low is not None and (value <= low if self.exclusive_minimum else value < low)
        too_high = high is not None and value > high
        if not too_low and not too_high:
            return value
        if self.out_of_range == "drop" or (too_low and self.exclusive_minimum):
            return None
        return low if too_low else high


@dataclass(frozen=True)
class RequestRules:
    """Request shaping that does not depend on reasoning.

    ``allowed_parameters`` is an allowlist of optional caller parameters (for
    example the Responses wires that accept only some sampling fields); ``None``
    allows every parameter the codec knows. ``parameters`` refines individual
    parameters. ``body_defaults`` are set when the caller supplied no value;
    ``extra_body`` and ``extra_headers`` are always added.
    """

    output_limit_field: OutputLimitField | None = "max_tokens"
    output_limit_default: int | None = None
    output_limit_cap: int | None = None
    allowed_parameters: tuple[str, ...] | None = None
    parameters: Mapping[str, ParameterRule] = field(default_factory=lambda: _EMPTY)
    body_defaults: Mapping[str, JsonValue] = field(default_factory=lambda: _EMPTY)
    extra_body: Mapping[str, JsonValue] = field(default_factory=lambda: _EMPTY)
    extra_headers: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    tool_schema: ToolSchemaProfile = "omit_strict"
    tool_call_ids: ToolCallIdProfile = "none"
    list_announced_tools: bool = False
    prompt_cache: PromptCacheStyle = "none"
    options: Mapping[str, JsonValue] = field(default_factory=lambda: _EMPTY)

    def shape_parameters(self, payload: dict[str, Any], *, reasoning_active: bool) -> None:
        """Apply ``parameters`` to the top-level request fields of ``payload``.

        ``drop`` removes the field, ``drop_while_thinking`` removes it while the
        request asks for reasoning, and numeric values outside the bounds are
        clamped into range or dropped (an exclusive lower bound cannot be
        clamped onto, so such a value is dropped).
        """

        for name, rule in self.parameters.items():
            if name not in payload:
                continue
            if rule.mode == "drop" or (rule.mode == "drop_while_thinking" and reasoning_active):
                del payload[name]
                continue
            value = payload[name]
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            bounded = rule.bounded(value)
            if bounded is None:
                del payload[name]
            else:
                payload[name] = bounded


@dataclass(frozen=True)
class BudgetRule:
    """How an effort becomes a thinking-token budget for budget-controlled Models."""

    strategy: BudgetStrategy = "fraction_of_max"
    minimum: int = 1024
    maximum: int | None = None


@dataclass(frozen=True)
class ReasoningWire:
    """How the Agent's reasoning effort reaches the wire.

    ``supported``, ``control``, ``catalog_levels``, ``budget_max`` and
    ``mandatory`` mirror the catalog capability (overridable by explicit profile
    data). ``levels`` is an explicit wire ladder that beats the catalog ladder;
    ``floor`` is used only when neither exists. ``effort_map`` maps an Agent
    effort directly to a wire level and bypasses snapping; ``snap`` breaks ties
    between equally distant rungs.
    """

    dialect: ReasoningDialect = "none"
    supported: bool | None = None
    control: str | None = None
    catalog_levels: tuple[str, ...] = ()
    levels: tuple[str, ...] | None = None
    floor: tuple[str, ...] = ()
    effort_map: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    snap: SnapRule = "down"
    off: str = "auto"
    unset: str = "omit"
    mandatory: bool = False
    budget_max: int | None = None
    budget: BudgetRule = field(default_factory=BudgetRule)
    options: Mapping[str, JsonValue] = field(default_factory=lambda: _EMPTY)

    @property
    def ladder(self) -> tuple[str, ...]:
        """The effective wire ladder: explicit levels, catalog levels, then floor."""

        if self.levels is not None:
            return self.levels
        if self.catalog_levels:
            return self.catalog_levels
        return self.floor

    def plan(self, effort: Any, *, output_allowance: int | None = None) -> ReasoningIntent:
        """Decide what the next request asks of the Model's reasoning.

        This is the only place that turns an Agent's thinking effort into a wire
        decision; dialect renderers spell the returned intent, and status and
        diagnostics describe the same intent. ``output_allowance`` is the
        resolved output-token limit of the request (a budget must fit below it).

        - Unsupported reasoning or the ``none`` dialect: ``default`` (send nothing).
        - No effort selected: ``unset`` (``omit`` -> ``default``, ``enabled`` ->
          ``on``, a level -> as if that effort were selected).
        - Effort ``none``: ``off`` (see :data:`OFF_RENDERS`).
        - ``effort_map`` entry: ``effort`` at the mapped wire value, unsnapped.
        - ``on_off`` control: ``on`` carrying the snapped level.
        - ``budget`` control: ``budget`` with the budget rule, or ``on`` when no
          budget fits the output allowance.
        - Otherwise ``effort`` snapped to the ladder, or ``default`` when nothing
          snaps.
        """

        if self.supported is False or self.dialect == "none":
            return ReasoningIntent(REASONING_INTENT_DEFAULT)
        normalized = normalize_thinking_effort(effort)
        if not normalized:
            if self.unset == "omit":
                return ReasoningIntent(REASONING_INTENT_DEFAULT)
            if self.unset == "enabled":
                return ReasoningIntent(REASONING_INTENT_ON)
            normalized = self.unset
        ladder = self.ladder
        if normalized == "none":
            return self._plan_off(ladder)
        mapped = self.effort_map.get(normalized)
        if mapped is not None:
            return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=mapped)
        snapped = snap_effort(normalized, ladder, prefer=self.snap)
        if self.control == "on_off":
            return ReasoningIntent(REASONING_INTENT_ON, effort_level=snapped)
        if self.control == "budget":
            budget = effort_budget_tokens(
                normalized,
                budget_max=self.budget_max,
                strategy=self.budget.strategy,
                minimum=self.budget.minimum,
                maximum=self.budget.maximum,
                output_allowance=output_allowance,
            )
            if budget is None:
                return ReasoningIntent(REASONING_INTENT_ON, effort_level=snapped)
            return ReasoningIntent(
                REASONING_INTENT_BUDGET, effort_level=snapped, budget_tokens=budget
            )
        if snapped is None:
            return ReasoningIntent(REASONING_INTENT_DEFAULT)
        return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=snapped)

    def _plan_off(self, ladder: tuple[str, ...]) -> ReasoningIntent:
        render = self.off
        if render == "auto" and self.mandatory:
            render = "lowest"
        if render == "auto":
            if self.control in ("on_off", "budget"):
                return ReasoningIntent(REASONING_INTENT_OFF)
            # An effort-spelled off is sent only to a Model known to reason; an
            # unknown Model may reject the ``none`` value outright.
            level = "none" if "none" in ladder and self.supported is True else None
            return ReasoningIntent(REASONING_INTENT_OFF, effort_level=level)
        if render == "omit":
            return ReasoningIntent(REASONING_INTENT_DEFAULT)
        if render == "enabled":
            return ReasoningIntent(REASONING_INTENT_ON)
        if render == "lowest":
            active = sorted(
                (level for level in ladder if level in THINKING_EFFORT_RANKS and level != "none"),
                key=THINKING_EFFORT_RANKS.__getitem__,
            )
            if not active:
                return ReasoningIntent(REASONING_INTENT_DEFAULT)
            return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=active[0])
        return ReasoningIntent(REASONING_INTENT_EFFORT, effort_level=render)


@dataclass(frozen=True)
class ResponseRules:
    """Where readable reasoning arrives, in priority order (stream and non-stream alike)."""

    reasoning_fields: tuple[str, ...] = ()
    options: Mapping[str, JsonValue] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True)
class ReplayRules:
    """What Reasoning Replay returns to the Model on later requests.

    ``scope`` selects the Assistant turns (Chat applies it); ``fidelity`` selects
    the class of reasoning state per turn; ``history_field`` is the readable
    carrier on Assistant history where the protocol uses one;
    ``echo_response_field`` lets the field a Model was observed answering in
    become that carrier (for wires whose Models expect their own field back);
    ``echo_empty_on_tool_calls`` sends an empty readable carrier on Tool-call
    turns without reasoning; ``strip_when_off`` removes historical reasoning
    when the current request disables reasoning.
    """

    scope: ReasoningReplayPolicy = DEFAULT_REASONING_REPLAY_POLICY
    fidelity: ReasoningReplayFidelity = DEFAULT_REASONING_REPLAY_FIDELITY
    history_field: str | None = None
    echo_response_field: bool = False
    echo_empty_on_tool_calls: bool = False
    strip_when_off: bool = False


@dataclass(frozen=True)
class MediaRules:
    """Media the wire carries natively and its byte limits."""

    types: frozenset[str] = frozenset()
    image_max_bytes: int | None = None
    request_max_bytes: int | None = None


@dataclass(frozen=True)
class Admission:
    """Whether the Model may be used on this Connection."""

    state: AdmissionState = "available"
    message: str | None = None


@dataclass(frozen=True)
class Verification:
    """A recorded live verification of the profile on one or more Connections."""

    date: str
    connections: tuple[str, ...] = ()
    evidence: str = ""


@dataclass(frozen=True)
class WireProfile:
    """The resolved wire contract for one Model on one Provider Connection."""

    provider_id: str
    connection_id: str
    model_id: str
    protocol: Protocol
    status: ProfileStatus = "inferred"
    verification: Verification | None = None
    admission: Admission = field(default_factory=Admission)
    request: RequestRules = field(default_factory=RequestRules)
    reasoning: ReasoningWire = field(default_factory=ReasoningWire)
    response: ResponseRules = field(default_factory=ResponseRules)
    replay: ReplayRules = field(default_factory=ReplayRules)
    media: MediaRules = field(default_factory=MediaRules)
    known_model: bool = True
    provenance: Mapping[str, str] = field(default_factory=lambda: _EMPTY)

    def source_of(self, path: str) -> str | None:
        """Return the layer that set ``path`` (for example ``"reasoning.dialect"``)."""

        return self.provenance.get(path)
