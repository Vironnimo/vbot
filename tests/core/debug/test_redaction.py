"""Tests for debug-trace redaction utilities.

Header names, URL query-parameter names and JSON object keys share one rule:
a name is sensitive when it is ``Authorization``/``x-api-key`` or contains a
sensitive whole word, split on hyphens, underscores and dots, in any case.
Values are never scanned for secrets.
"""

import pytest

from core.debug.redaction import redact_headers, redact_json_body, redact_url

_REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# The shared name rule, exercised through redact_headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "X-API-KEY",
        "x-access-token",
        "client-secret",
        "api-key",
        "x-password-hash",
        "x-credential-id",
        # Identifying values: account ids, organization ids and cookies.
        "chatgpt-account-id",
        "ChatGPT-Account-Id",
        "openai-organization",
        "cookie",
        "Set-Cookie",
        # Underscores and dots separate words like hyphens.
        "x_token_value",
        "auth.token",
    ],
)
def test_names_with_a_sensitive_whole_word_are_redacted(name: str):
    headers = {name: "sensitive-value", "Content-Type": "application/json"}

    assert redact_headers(headers) == {name: _REDACTED, "Content-Type": "application/json"}
    assert headers[name] == "sensitive-value"


@pytest.mark.parametrize(
    "name",
    [
        "Content-Type",
        "Accept",
        # A sensitive word only as a fragment of a longer word.
        "x-accounting-mode",
        "donkey",
        "monkey_keychain",
        # Usage counters are observability data, not credential fields.
        "reasoning_tokens",
    ],
)
def test_names_without_a_sensitive_whole_word_are_kept(name: str):
    assert redact_headers({name: "safe"}) == {name: "safe"}


# ---------------------------------------------------------------------------
# redact_url
# ---------------------------------------------------------------------------


def test_redact_url_redacts_every_sensitive_query_value_and_keeps_the_rest():
    url = (
        "https://api.example.com:8080/v1/chat"
        "?token=a&token=b&API.Key=secret&account_id=acct-123&user=john#section"
    )

    assert redact_url(url) == (
        "https://api.example.com:8080/v1/chat"
        "?token=%5BREDACTED%5D&token=%5BREDACTED%5D&API.Key=%5BREDACTED%5D"
        "&account_id=%5BREDACTED%5D&user=john#section"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api?user=john&page=2&limit=10",
        "http://example.com/api",
        # An unparseable URL is returned as it is.
        "http://[::1/api",
    ],
)
def test_redact_url_returns_a_url_without_sensitive_values_unchanged(url: str):
    assert redact_url(url) == url


# ---------------------------------------------------------------------------
# redact_json_body
# ---------------------------------------------------------------------------


def test_redact_json_body_redacts_sensitive_keys_at_every_depth():
    body = {
        "password": "hunter2",
        "username": "alice",
        "auth": {"TOKEN": "abc123", "type": "bearer"},
        "messages": [
            {"role": "user", "client_secret": {"nested": "x"}},
            {"role": "assistant", "content": "my token is abc123 and secret is xyz"},
        ],
        "level1": {"level2": {"level3": {"api.key": "hidden"}}},
    }

    assert redact_json_body(body) == {
        "password": _REDACTED,
        "username": "alice",
        "auth": {"TOKEN": _REDACTED, "type": "bearer"},
        "messages": [
            {"role": "user", "client_secret": _REDACTED},
            # String values are never inspected for secrets.
            {"role": "assistant", "content": "my token is abc123 and secret is xyz"},
        ],
        "level1": {"level2": {"level3": {"api.key": _REDACTED}}},
    }
    assert body["password"] == "hunter2"
    assert body["auth"]["TOKEN"] == "abc123"


@pytest.mark.parametrize("value", ["hello", 42, None, True, {}, []])
def test_redact_json_body_returns_values_without_sensitive_keys_unchanged(value: object):
    assert redact_json_body(value) == value
