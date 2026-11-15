"""Provider adapters, configuration, model values, wire policy and transport errors."""

from typing import Any

from ._exports import exports_for, resolve

_EXPORTS = exports_for(
    **{
        "core.providers.adapter": (
            "ProviderAdapter",
            "ModelLookup",
            "RequestContextText",
            "TerminalOutcome",
            "terminal_outcome_from_response",
            "normalize_tool_call_candidate",
            "normalize_tool_call_candidates",
            "normalize_tool_call_ids",
            "TERMINAL_OUTCOME_STOP",
            "TERMINAL_OUTCOME_TOOL_CALLS",
            "TERMINAL_OUTCOME_OUTPUT_TRUNCATED",
            "TERMINAL_OUTCOME_CONTENT_FILTERED",
            "TERMINAL_OUTCOME_ERROR",
            "TERMINAL_OUTCOME_UNKNOWN",
        ),
        "core.providers.providers": (
            "ProviderConfig",
            "ConnectionConfig",
            "AuthConfig",
            "OAuthConfig",
            "RequestInput",
        ),
        "core.models.models": ("Model", "Capabilities", "ReasoningCapabilities"),
        "core.models.pricing": ("TokenPricing",),
        "core.providers.wire_profile": (
            "WireProfile",
            "Protocol",
            "ProfileStatus",
            "AdmissionState",
            "ParameterMode",
            "OutOfRange",
            "OutputLimitField",
            "ToolSchemaProfile",
            "ToolCallIdProfile",
            "PromptCacheStyle",
            "BudgetStrategy",
            "SnapRule",
            "DecisionProtocol",
            "ReasoningDialect",
            "ParameterRule",
            "RequestRules",
            "BudgetRule",
            "ReasoningWire",
            "ResponseRules",
            "ReplayRules",
            "MediaRules",
            "Admission",
            "Verification",
            "DecisionWire",
        ),
        "core.providers.wire_profiles": ("WireBinding",),
        "core.providers.token_getter": ("TokenGetter",),
        "core.providers.reasoning": (
            "ReasoningIntent",
            "ReasoningReplayPolicy",
            "ReasoningReplayFidelity",
        ),
        "core.utils.errors": ("ProviderError",),
        "core.providers.errors": (
            "NetworkError",
            "ProviderAuthError",
            "ProviderRequestTooLargeError",
            "ProviderImageLimitError",
            "ProviderContextOverflowError",
            "ProviderStreamingUnsupportedError",
            "ProviderRateLimitError",
            "ProviderUsageLimitError",
            "ProviderTimeoutError",
            "ProviderOutcomeUnknownError",
            "ProviderContentRefusedError",
        ),
    }
)
_EXPORTS["ToolCallIdRules"] = "core.providers.adapter:ToolCallIdProfile"


def __getattr__(name: str) -> Any:
    value = resolve(__name__, _EXPORTS, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = list(_EXPORTS)
