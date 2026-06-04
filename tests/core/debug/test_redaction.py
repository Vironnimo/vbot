"""Tests for debug-trace redaction utilities.

Covers header redaction, URL query-parameter redaction, and recursive
JSON-body redaction — including edge cases such as empty containers,
partial-word exclusion, case-insensitive matching, and free-text
pass-through.
"""

from core.debug.redaction import redact_headers, redact_json_body, redact_url

_REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# redact_headers
# ---------------------------------------------------------------------------


def test_redacts_exact_match_authorization():
    """The Authorization header is redacted regardless of case."""
    headers = {"Authorization": "Bearer secret-token"}
    result = redact_headers(headers)
    assert result["Authorization"] == _REDACTED


def test_redacts_exact_match_x_api_key_lowercase():
    """x-api-key is redacted when lowercase."""
    headers = {"x-api-key": "sk-abc123"}
    result = redact_headers(headers)
    assert result["x-api-key"] == _REDACTED


def test_redacts_exact_match_x_api_key_uppercase():
    """X-API-KEY is redacted when uppercase."""
    headers = {"X-API-KEY": "sk-abc123"}
    result = redact_headers(headers)
    assert result["X-API-KEY"] == _REDACTED


def test_redacts_header_containing_sensitive_word_token():
    """A header whose name contains 'token' as a whole word is redacted."""
    headers = {"x-access-token": "value"}
    result = redact_headers(headers)
    assert result["x-access-token"] == _REDACTED


def test_redacts_header_containing_sensitive_word_secret():
    """A header whose name contains 'secret' is redacted."""
    headers = {"client-secret": "value"}
    result = redact_headers(headers)
    assert result["client-secret"] == _REDACTED


def test_redacts_header_containing_sensitive_word_key():
    """A header containing 'key' as a whole hyphen-delimited word is redacted."""
    headers = {"api-key": "value"}
    result = redact_headers(headers)
    assert result["api-key"] == _REDACTED


def test_redacts_header_containing_sensitive_word_password():
    """'password' is a sensitive word."""
    headers = {"x-password-hash": "abc"}
    result = redact_headers(headers)
    assert result["x-password-hash"] == _REDACTED


def test_redacts_header_containing_sensitive_word_credential():
    """'credential' is a sensitive word."""
    headers = {"x-credential-id": "abc"}
    result = redact_headers(headers)
    assert result["x-credential-id"] == _REDACTED


def test_does_not_redact_partial_word_match_donkey():
    """A header like 'donkey' is not redacted — 'key' must be a whole word."""
    headers = {"donkey": "safe"}
    result = redact_headers(headers)
    assert result["donkey"] == "safe"


def test_does_not_redact_partial_word_match_mickey():
    """'mickey' is not redacted — no whole-word boundary for 'key'."""
    headers = {"mickey": "safe"}
    result = redact_headers(headers)
    assert result["mickey"] == "safe"


def test_preserves_non_sensitive_headers():
    """Headers that do not match any pattern are left unchanged."""
    headers = {"Content-Type": "application/json", "Accept": "*/*"}
    result = redact_headers(headers)
    assert result == {"Content-Type": "application/json", "Accept": "*/*"}


def test_redact_headers_handles_empty_dict():
    """An empty headers dict returns an empty dict."""
    assert redact_headers({}) == {}


def test_redacts_underscore_delimited_sensitive_words():
    """Underscores are treated as word separators like hyphens."""
    headers = {"x_token_value": "sensitive"}
    result = redact_headers(headers)
    assert result["x_token_value"] == _REDACTED


def test_redact_headers_does_not_mutate_original():
    """The input dict is not modified."""
    headers = {"Authorization": "secret", "Content-Type": "json"}
    redact_headers(headers)
    assert headers["Authorization"] == "secret"


# ---------------------------------------------------------------------------
# redact_url
# ---------------------------------------------------------------------------


def test_redact_url_redacts_sensitive_param_token():
    """A query param named 'token' is redacted."""
    url = "http://example.com/api?token=abc123&user=john"
    result = redact_url(url)
    assert "token" in result
    assert "abc123" not in result
    assert "user=john" in result


def test_redact_url_redacts_sensitive_param_key():
    """A query param named 'key' is redacted."""
    url = "http://example.com/api?key=secret&page=1"
    result = redact_url(url)
    assert "key" in result
    assert "page=1" in result
    assert "secret" not in result


def test_redact_url_redacts_sensitive_param_secret():
    """A query param named 'secret' is redacted."""
    url = "http://example.com/api?secret=value&safe=ok"
    result = redact_url(url)
    assert "secret" in result
    assert "value" not in result
    assert "safe=ok" in result


def test_redact_url_preserves_non_sensitive_params():
    """Non-sensitive query params are left intact."""
    url = "http://example.com/api?user=john&page=2&limit=10"
    result = redact_url(url)
    assert result == url


def test_redact_url_handles_no_query_string():
    """A URL with no query string is returned unchanged."""
    url = "http://example.com/api"
    assert redact_url(url) == url


def test_redact_url_redacts_repeated_sensitive_params():
    """Multiple values for the same sensitive param are all redacted."""
    url = "http://example.com/api?token=a&token=b&user=john"
    result = redact_url(url)
    assert "user=john" in result
    # Both token values should be redacted — the literal values "a" and "b"
    # must not appear as query-param values alongside "token=".
    assert "token=a" not in result
    assert "token=b" not in result


def test_redact_url_preserves_url_structure():
    """Scheme, host, path, and fragment are preserved."""
    url = "https://api.example.com:8080/v1/chat?api_key=secret#section"
    result = redact_url(url)
    assert result.startswith("https://api.example.com:8080/v1/chat?")
    assert result.endswith("#section")
    assert "secret" not in result


def test_redact_url_returns_unparseable_url_unchanged():
    """A malformed URL that cannot be parsed is returned as-is."""
    url = "not-a-valid-url::://"
    assert redact_url(url) == url


def test_redact_url_redacts_case_insensitive_param_name():
    """Query param names are matched case-insensitively."""
    url = "http://example.com/api?TOKEN=abc"
    result = redact_url(url)
    assert "abc" not in result


# ---------------------------------------------------------------------------
# redact_json_body
# ---------------------------------------------------------------------------


def test_redacts_top_level_sensitive_key():
    """A sensitive key at the top level has its value replaced."""
    body = {"password": "hunter2", "username": "alice"}
    result = redact_json_body(body)
    assert result == {"password": _REDACTED, "username": "alice"}


def test_recursively_redacts_nested_dict_keys():
    """Sensitive keys in nested dicts are redacted."""
    body = {
        "auth": {
            "token": "abc123",
            "type": "bearer",
        }
    }
    result = redact_json_body(body)
    assert result == {
        "auth": {
            "token": _REDACTED,
            "type": "bearer",
        }
    }


def test_recursively_redacts_keys_in_list_items():
    """Sensitive keys inside list elements are redacted."""
    body = {
        "messages": [
            {"role": "user", "secret": "x"},
            {"role": "assistant", "content": "hello"},
        ]
    }
    result = redact_json_body(body)
    assert result == {
        "messages": [
            {"role": "user", "secret": _REDACTED},
            {"role": "assistant", "content": "hello"},
        ]
    }


def test_redact_json_does_not_scan_string_values():
    """String values are never inspected for secrets."""
    body = {"data": "my token is abc123 and secret is xyz"}
    result = redact_json_body(body)
    assert result == body


def test_redact_json_returns_primitive_unchanged():
    """Non-dict, non-list values are returned as-is."""
    assert redact_json_body("hello") == "hello"
    assert redact_json_body(42) == 42
    assert redact_json_body(None) is None
    assert redact_json_body(True) is True


def test_redact_json_handles_empty_dict():
    """An empty dict is returned as an empty dict."""
    assert redact_json_body({}) == {}


def test_redact_json_handles_empty_list():
    """An empty list is returned as an empty list."""
    assert redact_json_body([]) == []


def test_redact_json_handles_deeply_nested():
    """Deeply nested sensitive keys are recursively redacted."""
    body = {
        "level1": {
            "level2": {
                "level3": {
                    "secret": "hidden",
                }
            }
        }
    }
    result = redact_json_body(body)
    assert result == {
        "level1": {
            "level2": {
                "level3": {
                    "secret": _REDACTED,
                }
            }
        }
    }


def test_redact_json_does_not_mutate_original():
    """The input dict is not modified."""
    body = {"token": "abc"}
    redact_json_body(body)
    assert body["token"] == "abc"


def test_redact_json_case_insensitive_keys():
    """JSON keys are matched case-insensitively."""
    body = {"TOKEN": "abc", "Secret": "xyz", "Key": "val"}
    result = redact_json_body(body)
    assert result == {"TOKEN": _REDACTED, "Secret": _REDACTED, "Key": _REDACTED}


def test_redact_json_hyphenated_keys():
    """Hyphen-delimited keys containing sensitive words are redacted."""
    body = {"api-key": "abc", "x-secret-token": "xyz"}
    result = redact_json_body(body)
    assert result == {"api-key": _REDACTED, "x-secret-token": _REDACTED}


def test_redact_json_underscore_delimited_keys():
    """Underscore-delimited keys containing sensitive words are redacted."""
    body = {"api_key": "abc", "client_secret": "xyz"}
    result = redact_json_body(body)
    assert result == {"api_key": _REDACTED, "client_secret": _REDACTED}


def test_redact_json_partial_word_keys_not_redacted():
    """Keys like 'donkey' are not redacted — whole-word match only."""
    body = {"donkey": "value", "monkey_keychain": "value"}
    result = redact_json_body(body)
    assert result == body
