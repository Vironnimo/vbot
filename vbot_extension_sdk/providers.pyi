# Public authoring declarations; runtime identities come from the installed host.
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterator,
    Callable,
    Collection,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import (
    Any,
    ClassVar,
    Final,
    Literal,
)
from typing import (
    Protocol as TypingProtocol,
)

class TokenGetter(TypingProtocol):
    async def __call__(self) -> str: ...

class ProviderAdapter(ABC):
    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ...
    local_endpoint: bool = ...
    display_timezone: tzinfo = ...

    def __init__(
        self, model_lookup: ModelLookup | None = None, debug_recorder: Any | None = None
    ) -> None: ...
    def bind_availability(self, check: Callable[[], None]) -> None: ...
    def ensure_available(self) -> None: ...
    def set_debug_context(self, ctx: Any) -> None: ...
    def bind_wire_profiles(self, binding: WireBinding) -> None: ...
    @property
    def wire(self) -> WireBinding: ...
    def wire_profile(self, model_id: str) -> WireProfile: ...
    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy: ...
    def reasoning_replay_fidelity(self, model_id: str) -> ReasoningReplayFidelity: ...
    def on_demand_tools(self, model_id: str) -> bool: ...
    def effort_updates(self, model_id: str) -> bool: ...
    def reported_context_window(self, model_id: str) -> int | None: ...
    def input_token_limit(self, model_id: str) -> int | None: ...
    def minimum_output_tokens(
        self, model_id: str, thinking_effort: str | None, *, explicit_limit: Any = None
    ) -> int: ...
    def describe_reasoning_render(self, model_id: str, effort: str | None) -> ReasoningIntent: ...
    def wire_media_support(self, model_id: str) -> frozenset[str]: ...
    def image_size_limit(self, model_id: str) -> int | None: ...
    def request_body_limit(self, model_id: str) -> int | None: ...
    def request_image_limit(self, model_id: str) -> int | None: ...
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int: ...
    @classmethod
    def openai_compatible_base_url(cls, base_url: str) -> str: ...
    @classmethod
    def finalize_discovered_model(
        cls, model: Model, connection: ConnectionConfig | None
    ) -> Model: ...
    def fast_mode_kwargs(self, model_id: str, *, enabled: bool) -> dict[str, Any]: ...
    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
        session_generation_id: str | None = None,
        request_kind: Literal["chat", "compaction", "utility"] = "chat",
    ) -> dict[str, Any]: ...
    @abstractmethod
    async def aclose(self) -> None: ...
    @abstractmethod
    async def send(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> dict[str, Any]: ...
    @abstractmethod
    def stream(
        self, messages: list[dict[str, Any]], *, model_id: str, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]: ...
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]: ...

type ModelLookup = Callable[[str], Model | None]

class RequestContextText(str): ...

type TerminalOutcome = Literal[
    "stop", "tool_calls", "output_truncated", "content_filtered", "error", "unknown"
]

def terminal_outcome_from_response(response: dict[str, Any]) -> TerminalOutcome: ...
def normalize_tool_call_candidate(
    *,
    tool_call_id: Any,
    name: Any,
    arguments: Any,
    fallback_id: str,
    rejection: Any = ...,
    argument_sequence_index: Any = ...,
    argument_sequence_length: Any = ...,
) -> dict[str, Any]: ...
def normalize_tool_call_candidates(
    *,
    tool_call_id: Any,
    name: Any,
    arguments: Any,
    fallback_id: str,
    rejection: Any = ...,
    argument_sequence_index: Any = ...,
    argument_sequence_length: Any = ...,
) -> list[dict[str, Any]]: ...
def normalize_tool_call_ids(
    messages: list[dict[str, Any]], profile: ToolCallIdRules
) -> list[dict[str, Any]]: ...

@dataclass(frozen=True)
class ToolCallIdRules:
    name: str
    allowed_characters: frozenset[str]
    max_length: int
    exact_length: int | None = None
    trim_trailing_underscores: bool = False
    rewrite_responses_output_items: bool = False

TERMINAL_OUTCOME_STOP: Final[TerminalOutcome] = ...
TERMINAL_OUTCOME_TOOL_CALLS: Final[TerminalOutcome] = ...
TERMINAL_OUTCOME_OUTPUT_TRUNCATED: Final[TerminalOutcome] = ...
TERMINAL_OUTCOME_CONTENT_FILTERED: Final[TerminalOutcome] = ...
TERMINAL_OUTCOME_ERROR: Final[TerminalOutcome] = ...
TERMINAL_OUTCOME_UNKNOWN: Final[TerminalOutcome] = ...

@dataclass(frozen=True)
class ProviderConfig:
    id: str
    name: str
    adapter: str
    base_url: str
    connections: list[ConnectionConfig] = ...
    defaults: dict[str, Any] | None = None
    extra_headers: dict[str, str] | None = None
    models_endpoint: str | None = None
    models_dev_id: str | None = None
    context_window: int | None = None
    catalog_exclusions: frozenset[str] = ...
    catalog_scope: str = "shared"
    custom: bool = False
    extension_adapter: type[ProviderAdapter] | None = None
    registration_active: Callable[[], bool] | None = None

    def ensure_available(self) -> None: ...
    def effective_models_dev_id(self) -> str: ...
    def get_connection(self, local_id: str) -> ConnectionConfig: ...

@dataclass(frozen=True)
class ConnectionConfig:
    id: str
    type: str
    label: str
    auth: AuthConfig
    base_url: str | None = None
    oauth: OAuthConfig | None = None
    mode: str | None = None
    models_endpoint: str | None = None
    auto_refresh: bool = False
    catalog_requires_credentials: bool = True
    setup_command: str | None = None
    setup_url: str | None = None

@dataclass(frozen=True)
class AuthConfig:
    header: str
    prefix: str
    credential_key: str = ""

@dataclass(frozen=True)
class OAuthConfig:
    flow: str
    client_id: str
    device_auth_url: str
    token_url: str
    scopes: list[str]
    token_exchange_url: str | None = None
    device_flow: str = "oauth2"
    verification_uri: str | None = None
    redirect_uri: str | None = None
    expires_in: int | None = None

@dataclass(frozen=True)
class RequestInput:
    tokens: int
    estimated: bool
    estimated_tokens: int

    @classmethod
    def estimate(cls, tokens: int) -> RequestInput: ...
    @classmethod
    def measured(cls, tokens: int, *, estimated_change: int | None = None) -> RequestInput: ...
    @property
    def margin(self) -> int: ...
    @property
    def upper_bound(self) -> int: ...

@dataclass(frozen=True)
class Model:
    model_id: str
    name: str
    capabilities: Capabilities
    context_window: int | None
    max_output_tokens: int | None
    family: str = ""
    metadata: Mapping[str, Any] = ...
    connections: tuple[str, ...] = ()
    connection_context_windows: Mapping[str, int] = ...
    recommended_temperature: float | None = None
    recommended_top_p: float | None = None
    pricing: TokenPricing | None = None

    def __post_init__(self) -> None: ...
    def allows_connection(self, connection_id: str) -> bool: ...
    def context_window_for(self, connection_id: str) -> int | None: ...

@dataclass(frozen=True)
class Capabilities:
    vision: bool
    tools: bool
    json_mode: bool
    reasoning: ReasoningCapabilities
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    supported_voices: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    task_options: Mapping[str, Any] = ...

    def __post_init__(self) -> None: ...

@dataclass(frozen=True)
class ReasoningCapabilities:
    supported: bool | None
    control: str | None = None
    levels: tuple[str, ...] = ()
    budget_max: int | None = None
    mandatory: bool = False

@dataclass(frozen=True)
class TokenPricing:
    source: str
    rates: Any
    tiers: tuple[Any, ...] = ()
    supported: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> TokenPricing | None: ...
    @classmethod
    def from_cost(cls, cost: Any, *, source: str) -> TokenPricing | None: ...
    def to_dict(self) -> dict[str, Any]: ...

@dataclass(frozen=True)
class WireProfile:
    provider_id: str
    connection_id: str
    model_id: str
    protocol: Protocol
    status: ProfileStatus = "inferred"
    verification: Verification | None = None
    admission: Admission = ...
    request: RequestRules = ...
    reasoning: ReasoningWire = ...
    response: ResponseRules = ...
    replay: ReplayRules = ...
    media: MediaRules = ...
    decision: DecisionWire = ...
    known_model: bool = True
    provenance: Mapping[str, str] = ...

    def source_of(self, path: str) -> str | None: ...

type Protocol = Literal[
    "chat_completions", "messages", "responses", "gemini", "ollama_chat", "claude_agent"
]
type ProfileStatus = Literal["verified", "configured", "inferred"]
type AdmissionState = Literal["available", "restricted", "retired"]
type ParameterMode = Literal["send", "drop", "drop_while_thinking", "reject"]
type OutOfRange = Literal["clamp", "drop", "reject"]
type OutputLimitField = Literal["max_tokens", "max_completion_tokens", "max_output_tokens"]
type ToolSchemaProfile = Literal["explicit_non_strict", "omit_strict"]
type ToolCallIdProfile = Literal["none", "anthropic", "mistral", "responses"]
type PromptCacheStyle = Literal["none", "anthropic_breakpoints"]
type BudgetStrategy = Literal["fraction_of_max", "absolute"]
type SnapRule = Literal["down", "up"]
type DecisionProtocol = Literal["none", "system_one", "openai_decisions"]
type ReasoningDialect = Literal[
    "none",
    "claude_agent",
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

@dataclass(frozen=True)
class ParameterRule:
    mode: ParameterMode = "send"
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    out_of_range: OutOfRange = "clamp"
    values: tuple[Any, ...] | None = None

    def bounded(self, value: float) -> float | None: ...
    def range_text(self) -> str: ...

@dataclass(frozen=True)
class RequestRules:
    output_limit_field: OutputLimitField | None = "max_tokens"
    output_limit_default: int | None = None
    output_limit_cap: int | None = None
    output_limit_collapse: bool = False
    allowed_parameters: tuple[str, ...] | None = None
    parameters: Mapping[str, ParameterRule] = ...
    exclusive_parameters: tuple[tuple[str, ...], ...] = ()
    body_defaults: Mapping[str, Any] = ...
    extra_body: Mapping[str, Any] = ...
    extra_headers: Mapping[str, str] = ...
    tool_schema: ToolSchemaProfile = "omit_strict"
    tool_call_ids: ToolCallIdProfile = "none"
    on_demand_tools: bool = True
    fast_mode: bool = False
    prompt_cache: PromptCacheStyle = "none"
    options: Mapping[str, Any] = ...

    def apply_body(self, payload: dict[str, Any]) -> None: ...
    def shape_parameters(
        self,
        payload: dict[str, Any],
        *,
        reasoning_active: bool,
        protected: Collection[str] = (),
        provider_label: str = "The Provider",
    ) -> None: ...

@dataclass(frozen=True)
class BudgetRule:
    strategy: BudgetStrategy = "fraction_of_max"
    minimum: int = 1024
    maximum: int | None = None

@dataclass(frozen=True)
class ReasoningWire:
    dialect: ReasoningDialect = "none"
    supported: bool | None = None
    control: str | None = None
    catalog_levels: tuple[str, ...] = ()
    levels: tuple[str, ...] | None = None
    floor: tuple[str, ...] = ()
    effort_map: Mapping[str, str] = ...
    snap: SnapRule = "down"
    off: str = "auto"
    unset: str = "omit"
    mandatory: bool = False
    budget_max: int | None = None
    budget: BudgetRule = ...
    effort_updates: bool = False
    options: Mapping[str, Any] = ...

    @property
    def ladder(self) -> tuple[str, ...]: ...
    def plan(self, effort: Any, *, output_allowance: int | None = None) -> ReasoningIntent: ...

@dataclass(frozen=True)
class ResponseRules:
    reasoning_fields: tuple[str, ...] = ()
    options: Mapping[str, Any] = ...

@dataclass(frozen=True)
class ReplayRules:
    scope: ReasoningReplayPolicy = "full_history"
    fidelity: ReasoningReplayFidelity = "meta_preferred"
    history_field: str | None = None
    echo_response_field: bool = False
    echo_empty_on_tool_calls: bool = False
    strip_when_off: bool = False

@dataclass(frozen=True)
class MediaRules:
    types: frozenset[str] = ...
    image_max_bytes: int | None = None
    request_max_bytes: int | None = None
    request_max_images: int | None = None
    image_max_dimension: int | None = None
    many_images_threshold: int | None = None
    many_images_max_dimension: int | None = None

@dataclass(frozen=True)
class Admission:
    state: AdmissionState = "available"
    message: str | None = None

@dataclass(frozen=True)
class Verification:
    date: str
    connections: tuple[str, ...] = ()
    evidence: str = ""

@dataclass(frozen=True)
class DecisionWire:
    protocol: DecisionProtocol = "none"
    endpoint: str = ""
    state_field: str = ""
    allowed_options: tuple[str, ...] = ()

@dataclass(frozen=True)
class WireBinding:
    provider_id: str
    connection_id: str
    profiles: Any
    observations: Any | None = None

    def profile(self, model_id: str) -> WireProfile: ...
    def observe_reasoning_field(self, model_id: str, field: str) -> None: ...
    def observe_reasoning_returned(self, model_id: str) -> None: ...
    def observe_rejected_parameter(self, model_id: str, parameter: str) -> None: ...
    def observe_exclusive_parameters(self, model_id: str, group: Sequence[str]) -> None: ...
    def observe_rejected_effort(self, model_id: str, effort: str) -> None: ...
    def observe_off_ignored(self, model_id: str) -> None: ...
    def observe_effort_updates_rejected(self, model_id: str) -> None: ...
    def observe_max_output_tokens(self, model_id: str, limit: int) -> None: ...
    def observe_context_window(self, model_id: str, window: int) -> None: ...
    def observe_request_max_images(self, model_id: str, limit: int) -> None: ...
    def learned_context_window(self, model_id: str) -> int | None: ...

@dataclass(frozen=True)
class ReasoningIntent:
    kind: str
    effort_level: str | None = None
    budget_tokens: int | None = None

    @property
    def requests_reasoning(self) -> bool: ...

type ReasoningReplayPolicy = Literal["none", "current_run", "tool_turns", "full_history"]
type ReasoningReplayFidelity = Literal["meta_preferred", "meta_only", "readable_only"]

class ProviderError(Exception):
    retryable: bool
    retry_after: float | None = ...
    status_code: int | None = ...

    def __init__(self, message: str = "", retryable: bool = False) -> None: ...

class NetworkError(Exception):
    retryable: bool = ...

class ProviderAuthError(ProviderError):
    def __init__(self, message: str = "") -> None: ...

class ProviderRequestTooLargeError(ProviderError):
    def __init__(
        self, size_bytes: int | None = None, max_bytes: int | None = None, *, detail: str = ""
    ) -> None: ...

class ProviderImageLimitError(ProviderError):
    def __init__(self, max_images: int, detail: str) -> None: ...

class ProviderContextOverflowError(ProviderError):
    def __init__(self, message: str = "") -> None: ...

class ProviderStreamingUnsupportedError(ProviderError):
    def __init__(self, message: str = "") -> None: ...

class ProviderRateLimitError(ProviderError):
    def __init__(self, message: str = "") -> None: ...

class ProviderUsageLimitError(ProviderError):
    resets_at: datetime | None
    def __init__(self, message: str, *, resets_at: datetime | None = None) -> None: ...

class ProviderTimeoutError(ProviderError):
    def __init__(self, message: str = "") -> None: ...

class ProviderOutcomeUnknownError(ProviderError):
    operation_key: str
    def __init__(self, message: str, *, operation_key: str) -> None: ...

class ProviderContentRefusedError(ProviderError):
    def __init__(self, reason: str | None = None) -> None: ...
