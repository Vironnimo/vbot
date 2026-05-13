"""Tests for GitHub Copilot runtime policy."""

from core.providers.github_copilot_policy import (
    CHAT_COMPLETIONS_ENDPOINT,
    MESSAGES_ENDPOINT,
    RESPONSES_ENDPOINT,
    copilot_model_policy,
)


def copilot_metadata(**overrides):
    metadata = {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.2",
            "version": "gpt-5.2",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, RESPONSES_ENDPOINT],
            "reasoning_efforts": ["low", "medium", "high", "xhigh"],
            "tool_calls": True,
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
        }
    }
    metadata["github_copilot"].update(overrides)
    return metadata


def test_openai_like_model_prefers_responses_from_metadata() -> None:
    policy = copilot_model_policy("gpt-5.2", copilot_metadata())

    assert policy.endpoint_path == RESPONSES_ENDPOINT
    assert policy.allows_reasoning_effort("xhigh") is True
    assert policy.supports_tools is True
    assert policy.supports_structured_outputs is True
    assert policy.supports_request_parameter("max_output_tokens") is True
    assert policy.supports_request_parameter("temperature") is False


def test_claude_like_model_prefers_messages_from_metadata() -> None:
    policy = copilot_model_policy(
        "claude-sonnet-4.6",
        copilot_metadata(
            vendor="Anthropic",
            family="claude-sonnet-4.6",
            supported_endpoints=[CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT],
            adaptive_thinking=True,
            min_thinking_budget=1024,
            max_thinking_budget=32000,
        ),
    )

    assert policy.endpoint_path == MESSAGES_ENDPOINT
    assert policy.supports_adaptive_thinking is True
    assert policy.supports_thinking_budget is True
    assert policy.supports_request_parameter("max_tokens") is True
    assert policy.supports_request_parameter("max_output_tokens") is True
    assert policy.supports_request_parameter("max_completion_tokens") is True


def test_gemini_model_stays_chat_first_when_chat_is_advertised() -> None:
    policy = copilot_model_policy(
        "gemini-3.1-pro-preview",
        copilot_metadata(
            vendor="Google",
            family="gemini-3.1-pro-preview",
            supported_endpoints=[CHAT_COMPLETIONS_ENDPOINT, RESPONSES_ENDPOINT],
        ),
    )

    assert policy.endpoint_path == CHAT_COMPLETIONS_ENDPOINT


def test_metadata_driven_routing_wins_over_static_fallback() -> None:
    policy = copilot_model_policy(
        "gpt-5-mini",
        copilot_metadata(supported_endpoints=[RESPONSES_ENDPOINT], reasoning_efforts=["low"]),
    )

    assert policy.endpoint_path == RESPONSES_ENDPOINT
    assert policy.allows_reasoning_effort("low") is True
    assert policy.allows_reasoning_effort("high") is False


def test_static_fallback_applies_when_metadata_is_missing() -> None:
    policy = copilot_model_policy("gpt-5-mini")

    assert policy.endpoint_path == CHAT_COMPLETIONS_ENDPOINT
    assert policy.allows_reasoning_effort("high") is True
    assert policy.supports_tools is True


def test_unknown_model_is_conservative_without_metadata() -> None:
    policy = copilot_model_policy("new-unknown-model")

    filtered = policy.filter_request_kwargs(
        {
            "thinking_effort": "high",
            "tools": [{"type": "function"}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_output_tokens": 2048,
        }
    )

    assert policy.endpoint_path == CHAT_COMPLETIONS_ENDPOINT
    assert filtered == {"temperature": 0.2}


def test_filter_request_kwargs_keeps_only_metadata_supported_features() -> None:
    policy = copilot_model_policy(
        "gpt-5.2",
        copilot_metadata(parallel_tool_calls=False, structured_outputs=False),
    )

    filtered = policy.filter_request_kwargs(
        {
            "thinking_effort": "xhigh",
            "tools": [{"type": "function"}],
            "parallel_tool_calls": True,
            "response_format": {"type": "json_object"},
        }
    )

    assert filtered == {
        "thinking_effort": "xhigh",
        "tools": [{"type": "function"}],
    }


def test_responses_policy_drops_temperature_for_reasoning_gpt_models() -> None:
    policy = copilot_model_policy("gpt-5.4", copilot_metadata(family="gpt-5.4", version="gpt-5.4"))

    filtered = policy.filter_request_kwargs(
        {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_tokens": 4096,
            "max_output_tokens": 2048,
        }
    )

    assert policy.endpoint_path == RESPONSES_ENDPOINT
    assert filtered == {
        "top_p": 0.9,
        "max_tokens": 4096,
        "max_output_tokens": 2048,
    }


def test_chat_policy_keeps_temperature_when_endpoint_supports_it() -> None:
    policy = copilot_model_policy("gpt-5-mini")

    filtered = policy.filter_request_kwargs(
        {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_tokens": 4096,
            "max_output_tokens": 2048,
        }
    )

    assert policy.endpoint_path == CHAT_COMPLETIONS_ENDPOINT
    assert filtered == {
        "temperature": 0.4,
        "top_p": 0.9,
        "max_tokens": 4096,
    }


def test_messages_policy_keeps_messages_safe_output_token_field() -> None:
    policy = copilot_model_policy(
        "claude-haiku-4.5",
        copilot_metadata(
            vendor="Anthropic",
            family="claude-haiku-4.5",
            version="claude-haiku-4.5",
            supported_endpoints=[CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT],
        ),
    )

    filtered = policy.filter_request_kwargs(
        {
            "max_tokens": 4096,
            "max_output_tokens": 2048,
            "temperature": 0.2,
            "top_k": 10,
            "stop_sequences": ["END"],
        }
    )

    assert policy.endpoint_path == MESSAGES_ENDPOINT
    assert filtered == {
        "max_tokens": 4096,
        "max_output_tokens": 2048,
        "temperature": 0.2,
        "top_k": 10,
        "stop_sequences": ["END"],
    }


def test_responses_policy_omits_temperature_for_partial_openai_like_metadata() -> None:
    policy = copilot_model_policy(
        "gpt-5.4",
        {
            "github_copilot": {
                "vendor": "OpenAI",
                "family": "gpt-5.4",
                "supported_endpoints": [RESPONSES_ENDPOINT],
            }
        },
    )

    filtered = policy.filter_request_kwargs(
        {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_tokens": 4096,
            "max_output_tokens": 2048,
        }
    )

    assert policy.endpoint_path == RESPONSES_ENDPOINT
    assert filtered == {
        "top_p": 0.9,
        "max_tokens": 4096,
        "max_output_tokens": 2048,
    }
