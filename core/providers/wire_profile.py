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

The protocol itself is decided first, from the Model entry (its Connection
block, then its ``set``), the last matching rule, the catalog protocol hint,
the Connection, the file defaults and the Adapter's default protocol, in that
order; a value the Adapter does not speak is reported and skipped. The
catalog protocol hint is the Model's own AI SDK package from models.dev
(``metadata.<provider>.npm``): ``@ai-sdk/anthropic`` names Messages,
``@ai-sdk/openai`` Responses, ``@ai-sdk/google`` Gemini and
``@ai-sdk/openai-compatible`` Chat Completions. Any other package, or one
naming a protocol the Adapter does not speak, is skipped silently.

Every resolved field remembers which layer set it (``WireProfile.provenance``),
and the profile carries a status: ``verified`` when its Model entry records a
verification for this Connection, ``configured`` when a Model entry or a rule
naming the Model id shaped it, ``inferred`` when only defaults, catalog facts
(the protocol hint included), observations and pattern rules did.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Mapping
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
from core.utils.errors import ProviderError

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
    "anthropic_thinking",
    "responses_reasoning",
    "gemini_thinking",
    "ollama_think",
]
"""How a reasoning decision is spelled on the wire (rendered by the protocol codec).

- ``none``: the wire takes no reasoning control; nothing is sent.
- ``reasoning_effort``: top-level ``reasoning_effort: <level>``; off is the
  ``none`` level.
- ``openrouter_reasoning``: ``reasoning: {effort}`` (``{enabled: true}`` for a
  plain on and on/off Models) plus ``include_reasoning``; off is
  ``{effort: "none"}`` or ``{enabled: false}``. No token budget is sent.
- ``nous_reasoning``: ``reasoning: {enabled: true, effort}``.
- ``thinking_toggle``: ``thinking: {type: enabled|disabled[, keep]}``.
- ``thinking_toggle_with_effort``: ``reasoning_effort: <level>`` for an effort,
  the toggle otherwise; ``reasoning.options.switch_with_effort`` sends the
  enabled toggle with the effort too.
- ``minimax_split``: ``reasoning_split: true``; no effort control.
- ``minimax_thinking``: ``thinking: {type: adaptive|disabled}`` plus
  ``reasoning_split``.
- ``anthropic_thinking``: ``thinking`` adaptive with ``output_config.effort``
  for effort ladders, ``enabled`` with ``budget_tokens`` for budgets,
  ``disabled`` for off. ``reasoning.options.adaptive_on`` spells a plain on as
  adaptive thinking without an effort.
- ``responses_reasoning``: ``reasoning: {effort, summary}``; off is the
  ``none`` level. ``reasoning.options.context`` adds ``reasoning.context``, and
  a Model known to reason always gets the encrypted reasoning ``include``.
- ``gemini_thinking``: ``generationConfig.thinkingConfig: {includeThoughts,
  thinkingLevel}``; a decision without a level sends nothing.
- ``ollama_think``: native Ollama ``think``: the level for Models with a level
  ladder, otherwise ``true``; off is ``false``.
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

ParameterMode = Literal["send", "drop", "drop_while_thinking", "reject"]
PARAMETER_MODES: tuple[ParameterMode, ...] = ("send", "drop", "drop_while_thinking", "reject")
"""Whether an optional request parameter is sent.

- ``send``: sent (subject to its bounds and allowed values).
- ``drop``: never sent.
- ``drop_while_thinking``: not sent while the request asks for reasoning.
- ``reject``: the Provider does not accept it; a request carrying it is refused
  before network I/O.
"""

OutOfRange = Literal["clamp", "drop", "reject"]
OUT_OF_RANGE_POLICIES: tuple[OutOfRange, ...] = ("clamp", "drop", "reject")
"""What happens to a parameter value outside its bounds or allowed values.

- ``clamp``: a number is clamped into the range (a value outside ``values`` or
  below an exclusive minimum cannot be clamped and is dropped).
- ``drop``: the parameter is not sent.
- ``reject``: the request is refused before network I/O; a value that is not a
  finite number is out of range for bounded parameters.
"""

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
- ``none``: an ``off`` decision spelled as the ``none`` effort level, whatever
  the ladder or control (wires whose explicit off is that level for every Model).
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

    ``mode`` decides whether the value is sent at all (see :data:`PARAMETER_MODES`);
    ``minimum``/``maximum`` bound a number (``exclusive_minimum`` makes the lower
    bound exclusive), ``values`` lists the only accepted values (compared by
    JSON type and value), and ``out_of_range`` decides what happens to a value
    outside them (see :data:`OUT_OF_RANGE_POLICIES`).
    """

    mode: ParameterMode = "send"
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    out_of_range: OutOfRange = "clamp"
    values: tuple[JsonValue, ...] | None = None

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

    def range_text(self) -> str:
        """The accepted numeric range in interval notation, for example ``(0, 1]``."""

        low = "(-inf" if self.minimum is None else f"{'(' if self.exclusive_minimum else '['}"
        if self.minimum is not None:
            low += _number_text(self.minimum)
        high = "inf)" if self.maximum is None else f"{_number_text(self.maximum)}]"
        return f"{low}, {high}"


def _number_text(value: float) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _same_json_value(left: Any, right: Any) -> bool:
    """JSON equality that keeps booleans, integers and floats apart."""

    return type(left) is type(right) and left == right


def thaw_json(value: Any) -> Any:
    """Return a mutable deep copy of a frozen JSON value (for payload building)."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class RequestRules:
    """Request shaping that does not depend on reasoning.

    ``allowed_parameters`` is an allowlist of optional caller parameters (for
    example the Responses wires that accept only some sampling fields); ``None``
    allows every parameter the codec knows. ``parameters`` refines individual
    parameters. ``body_defaults`` are set when the request body has no value for
    the key; ``extra_body`` is always added and replaces any value the body
    holds (both through :meth:`apply_body`, at the top level of the request body
    on every protocol); ``extra_headers`` are always added to the request's
    headers and replace any header of the same name the Adapter sets, auth
    included. Every chat request path of every Adapter applies all three.
    ``output_limit_collapse`` sends exactly one output-limit field: every
    output-limit alias a caller supplies collapses into ``output_limit_field``
    and the smallest positive value wins.
    """

    output_limit_field: OutputLimitField | None = "max_tokens"
    output_limit_default: int | None = None
    output_limit_cap: int | None = None
    output_limit_collapse: bool = False
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

    def apply_body(self, payload: dict[str, Any]) -> None:
        """Add ``body_defaults`` and ``extra_body`` to a built request body.

        A codec calls this once its body is complete (caller values, Provider
        defaults and the rendered reasoning included) and before
        :meth:`shape_parameters` shapes that body: a body default fills only a
        top-level key the body lacks, and an ``extra_body`` entry then replaces
        whatever the body holds. Values are mutable copies, so later shaping may
        change them.
        """

        for key, value in self.body_defaults.items():
            payload.setdefault(key, thaw_json(value))
        for key, value in self.extra_body.items():
            payload[key] = thaw_json(value)

    def shape_parameters(
        self,
        payload: dict[str, Any],
        *,
        reasoning_active: bool,
        protected: Collection[str] = (),
        provider_label: str = "The Provider",
    ) -> None:
        """Apply ``parameters`` to the top-level request fields of ``payload``.

        ``drop`` removes the field, ``drop_while_thinking`` removes it while the
        request asks for reasoning, and a value outside the bounds or allowed
        values is clamped into range, dropped or rejected (an exclusive lower
        bound or an allowed-value list cannot be clamped onto, so such a value
        is dropped unless the rule rejects it). ``protected`` fields (the
        reasoning dialect's own output) are never shaped, unless ``extra_body``
        replaced them: an ``extra_body`` value is shaped like a caller's.

        Raises:
            ProviderError: (not retryable) when the payload carries a ``reject``
                parameter or a value a ``reject`` rule refuses; nothing was sent.
        """

        protected = frozenset(protected).difference(self.extra_body)
        refused = sorted(
            name
            for name, rule in self.parameters.items()
            if rule.mode == "reject" and name in payload and name not in protected
        )
        if refused:
            raise ProviderError(
                f"{provider_label} does not accept the request parameter(s): {', '.join(refused)}",
                retryable=False,
            )
        for name, rule in self.parameters.items():
            if name not in payload or name in protected:
                continue
            if rule.mode == "drop" or (rule.mode == "drop_while_thinking" and reasoning_active):
                del payload[name]
                continue
            self._shape_value(payload, name, rule, provider_label)

    @staticmethod
    def _shape_value(
        payload: dict[str, Any], name: str, rule: ParameterRule, provider_label: str
    ) -> None:
        value = payload[name]
        if rule.values is not None and not any(
            _same_json_value(value, allowed) for allowed in rule.values
        ):
            if rule.out_of_range == "reject":
                allowed_text = ", ".join(json.dumps(item) for item in rule.values)
                requirement = (
                    f"exactly {allowed_text}" if len(rule.values) == 1 else f"one of {allowed_text}"
                )
                raise ProviderError(
                    f"{provider_label} {name} must be {requirement}", retryable=False
                )
            del payload[name]
            return
        if rule.minimum is None and rule.maximum is None:
            return
        is_number = not isinstance(value, bool) and isinstance(value, int | float)
        if rule.out_of_range == "reject":
            if not is_number or not math.isfinite(value) or rule.bounded(value) != value:
                raise ProviderError(
                    f"{provider_label} {name} must be a finite number in the range "
                    f"{rule.range_text()}",
                    retryable=False,
                )
            return
        if not is_number:
            return
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
    effort to a wire value; a mapped effort level that a known ladder
    (explicit or catalog levels) lacks snaps onto that ladder, any other mapped
    value is sent as is. ``snap`` breaks ties between equally distant rungs.
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
        - ``effort_map`` entry: ``effort`` at the mapped wire value; a mapped
          effort level missing from a known (explicit or catalog) ladder snaps
          onto that ladder first.
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
            known = self.levels if self.levels is not None else self.catalog_levels
            if known and mapped not in known and mapped in THINKING_EFFORT_RANKS:
                snapped_map = snap_effort(mapped, known, prefer=self.snap)
                if snapped_map is None:
                    return ReasoningIntent(REASONING_INTENT_DEFAULT)
                mapped = snapped_map
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
        if render == "none":
            return ReasoningIntent(REASONING_INTENT_OFF, effort_level="none")
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
