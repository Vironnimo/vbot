"""Account id grammar, compositional Connection ids and credential-key derivation."""

from __future__ import annotations

import pytest

from core.providers.accounts import (
    account_id_from_credential_key,
    compose_connection_id,
    derive_credential_key,
    split_connection_id,
    validate_account_id,
)
from core.utils.errors import ConfigError


@pytest.mark.parametrize(
    ("account_id", "valid"),
    [
        pytest.param("a", True, id="one-character"),
        pytest.param("a" * 32, True, id="32-characters"),
        pytest.param("1team", True, id="leading-digit"),
        pytest.param("a_b_c", True, id="inner-underscores"),
        pytest.param("", False, id="empty"),
        pytest.param("a" * 33, False, id="33-characters"),
        pytest.param("Work", False, id="uppercase"),
        pytest.param("wo-rk", False, id="dash"),
        pytest.param("_work", False, id="leading-underscore"),
    ],
)
def test_account_id_grammar(account_id: str, valid: bool) -> None:
    if valid:
        assert validate_account_id(account_id) == account_id
    else:
        with pytest.raises(ConfigError):
            validate_account_id(account_id)


@pytest.mark.parametrize(
    ("account_id", "env_key"),
    [
        ("default", "OPENAI_API_KEY"),
        ("work", "OPENAI_API_KEY__WORK"),
        ("team_2", "OPENAI_API_KEY__TEAM_2"),
    ],
)
def test_credential_key_derivation_round_trips(account_id: str, env_key: str) -> None:
    """The default Account owns the base key; named Accounts own ``BASE__<ACCOUNT>``."""

    assert derive_credential_key("OPENAI_API_KEY", account_id) == env_key
    assert account_id_from_credential_key("OPENAI_API_KEY", env_key) == account_id


@pytest.mark.parametrize(
    ("base_key", "env_key"),
    [
        pytest.param("OPENAI_API_KEY", "OPENAI_API_KEY_WORK", id="foreign-key"),
        pytest.param("OPENAI_API_KEY", "OPENAI_API_KEY__", id="empty-suffix"),
        pytest.param("OPENAI_API_KEY", "OPENAI_API_KEY__DEFAULT", id="derived-default-spelling"),
        pytest.param("", "ANYTHING", id="connection-without-key"),
    ],
)
def test_unrelated_or_ambiguous_keys_name_no_account(base_key: str, env_key: str) -> None:
    assert account_id_from_credential_key(base_key, env_key) is None


@pytest.mark.parametrize(
    ("account_id", "connection_id"),
    [(None, "openai:api-key"), ("work", "openai:api-key:work")],
)
def test_compose_and_split_connection_ids(account_id: str | None, connection_id: str) -> None:
    assert compose_connection_id("openai", "api-key", account_id) == connection_id
    assert split_connection_id("openai", connection_id) == ("api-key", account_id)


@pytest.mark.parametrize(
    "connection_id",
    [
        pytest.param("other:api-key", id="foreign-provider"),
        pytest.param("openai:", id="empty-connection"),
        pytest.param("openai:api-key:work:extra", id="invalid-account"),
    ],
)
def test_split_rejects_malformed_connection_ids(connection_id: str) -> None:
    with pytest.raises(ConfigError):
        split_connection_id("openai", connection_id)
