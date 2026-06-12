"""Tests for provider connection account identifiers and key derivation."""

from __future__ import annotations

import pytest

from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
    account_id_from_credential_key,
    compose_connection_id,
    derive_credential_key,
    sorted_account_ids,
    split_connection_id,
    validate_account_id,
)
from core.utils.errors import ConfigError


class TestValidateAccountId:
    @pytest.mark.parametrize(
        "account_id",
        ["default", "work", "a", "1team", "a_b_c", "a" * 32],
    )
    def test_accepts_well_formed_account_ids(self, account_id: str) -> None:
        """Lowercase alphanumeric/underscore ids up to 32 characters pass."""
        # Act / Assert
        assert validate_account_id(account_id) == account_id

    @pytest.mark.parametrize(
        "account_id",
        ["", "Work", "wo-rk", "_work", "wo rk", "wo:rk", "a" * 33],
    )
    def test_rejects_malformed_account_ids(self, account_id: str) -> None:
        """Uppercase, dashes, leading underscore, and overlong ids are rejected."""
        # Act / Assert
        with pytest.raises(ConfigError, match="Invalid account id"):
            validate_account_id(account_id)


class TestCredentialKeyDerivation:
    def test_default_account_uses_base_key(self) -> None:
        """The default account maps to the unchanged base credential key."""
        # Act / Assert
        assert derive_credential_key("OPENAI_API_KEY", DEFAULT_ACCOUNT_ID) == "OPENAI_API_KEY"

    def test_named_account_appends_uppercased_suffix(self) -> None:
        """Named accounts derive BASE__ACCOUNT environment keys."""
        # Act / Assert
        assert derive_credential_key("OPENAI_API_KEY", "work") == "OPENAI_API_KEY__WORK"

    def test_base_key_maps_back_to_default_account(self) -> None:
        """The base key is the default account's canonical key."""
        # Act / Assert
        assert account_id_from_credential_key("OPENAI_API_KEY", "OPENAI_API_KEY") == "default"

    def test_derived_key_maps_back_to_named_account(self) -> None:
        """A derived key maps back to its lowercased account id."""
        # Act / Assert
        assert account_id_from_credential_key("OPENAI_API_KEY", "OPENAI_API_KEY__WORK") == "work"

    @pytest.mark.parametrize(
        "env_key",
        [
            "OTHER_KEY",
            "OPENAI_API_KEY__",
            "OPENAI_API_KEY__WO-RK",
            "OPENAI_API_KEY__DEFAULT",
            "OPENAI_API_KEY_WORK",
        ],
    )
    def test_unrelated_or_ambiguous_keys_map_to_none(self, env_key: str) -> None:
        """Foreign keys, empty suffixes, and the derived default spelling are rejected."""
        # Act / Assert
        assert account_id_from_credential_key("OPENAI_API_KEY", env_key) is None

    def test_empty_base_key_matches_nothing(self) -> None:
        """Connections without a credential key own no environment accounts."""
        # Act / Assert
        assert account_id_from_credential_key("", "ANYTHING") is None

    @pytest.mark.parametrize("account_id", ["default", "work", "team_2"])
    def test_derivation_round_trip(self, account_id: str) -> None:
        """derive/inverse round-trips every valid account id."""
        # Arrange
        base_key = "OPENAI_API_KEY"

        # Act
        derived_key = derive_credential_key(base_key, account_id)

        # Assert
        assert account_id_from_credential_key(base_key, derived_key) == account_id


class TestSplitConnectionId:
    def test_splits_connection_without_account(self) -> None:
        """A bare provider:connection id yields no account."""
        # Act / Assert
        assert split_connection_id("openai", "openai:api-key") == ("api-key", None)

    def test_splits_connection_with_account(self) -> None:
        """provider:connection:account yields the account id."""
        # Act / Assert
        assert split_connection_id("openai", "openai:api-key:work") == ("api-key", "work")

    @pytest.mark.parametrize(
        "connection_id",
        ["openai", "openai:", "other:api-key", "api-key", ""],
    )
    def test_rejects_malformed_connection_ids(self, connection_id: str) -> None:
        """Missing or foreign provider prefixes raise ConfigError."""
        # Act / Assert
        with pytest.raises(ConfigError, match="Unknown connection id"):
            split_connection_id("openai", connection_id)

    @pytest.mark.parametrize(
        "connection_id",
        ["openai:api-key:", "openai:api-key:WORK", "openai:api-key:work:extra"],
    )
    def test_rejects_invalid_account_parts(self, connection_id: str) -> None:
        """Empty, uppercase, or multi-part account ids raise ConfigError."""
        # Act / Assert
        with pytest.raises(ConfigError, match="Invalid account id"):
            split_connection_id("openai", connection_id)

    @pytest.mark.parametrize("account_id", [None, "work"])
    def test_compose_round_trips_with_split(self, account_id: str | None) -> None:
        """compose_connection_id output parses back to the same parts."""
        # Act
        connection_id = compose_connection_id("openai", "api-key", account_id)

        # Assert
        assert split_connection_id("openai", connection_id) == ("api-key", account_id)


class TestSortedAccountIds:
    def test_default_sorts_first_then_alphabetical(self) -> None:
        """Ordering is deterministic: default first, the rest sorted."""
        # Act / Assert
        assert sorted_account_ids(["zeta", "default", "alpha"]) == ["default", "alpha", "zeta"]
