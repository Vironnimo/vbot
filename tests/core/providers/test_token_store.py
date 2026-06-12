"""Tests for OAuth token persistence."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.providers.token_store import OAuthToken, TokenStore


def test_token_store_save_load_round_trip(tmp_path: Path) -> None:
    """Saved OAuth tokens load back with all fields intact."""
    # Arrange
    store = TokenStore(tmp_path)
    expires_at = datetime(2026, 5, 12, 12, 0, tzinfo=UTC)
    token = OAuthToken(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=expires_at,
        extra={"github_oauth_token": "github-secret"},
    )

    # Act
    store.save("github-copilot", "oauth", token)
    loaded = store.load("github-copilot", "oauth")

    # Assert
    assert loaded == token


def test_token_store_save_uses_tmp_directory_for_atomic_write(tmp_path: Path) -> None:
    """Token writes use the data-dir temporary directory before replacement."""
    # Arrange
    store = TokenStore(tmp_path)
    token = OAuthToken(access_token="access-secret")

    # Act
    store.save("github-copilot", "oauth", token)

    # Assert
    assert (tmp_path / "oauth" / "github-copilot-oauth.json").exists()
    assert (tmp_path / ".tmp").is_dir()
    assert list((tmp_path / ".tmp").glob("github-copilot-oauth.json.*.tmp")) == []


def test_token_store_delete_removes_file_and_missing_delete_is_silent(tmp_path: Path) -> None:
    """Deleting removes existing token files and ignores absent files."""
    # Arrange
    store = TokenStore(tmp_path)
    store.save("github-copilot", "oauth", OAuthToken(access_token="access-secret"))

    # Act
    store.delete("github-copilot", "oauth")
    store.delete("github-copilot", "oauth")

    # Assert
    assert store.load("github-copilot", "oauth") is None


def test_token_store_has_valid_token_for_non_expired_token(tmp_path: Path) -> None:
    """A non-expired token is valid."""
    # Arrange
    store = TokenStore(tmp_path)
    token = OAuthToken(
        access_token="access-secret",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.save("github-copilot", "oauth", token)

    # Act / Assert
    assert store.has_valid_token("github-copilot", "oauth") is True


def test_token_store_has_valid_token_false_for_expired_token_without_refresh(
    tmp_path: Path,
) -> None:
    """An expired token without any refresh path is not valid."""
    # Arrange
    store = TokenStore(tmp_path)
    token = OAuthToken(
        access_token="access-secret",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    store.save("github-copilot", "oauth", token)

    # Act / Assert
    assert store.has_valid_token("github-copilot", "oauth") is False


def test_token_store_has_valid_token_true_for_expired_token_with_refresh(
    tmp_path: Path,
) -> None:
    """An expired token with refresh_token is valid."""
    # Arrange
    store = TokenStore(tmp_path)
    token = OAuthToken(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    store.save("github-copilot", "oauth", token)

    # Act / Assert
    assert store.has_valid_token("github-copilot", "oauth") is True


def test_token_store_has_valid_token_true_for_expired_token_with_github_oauth_token(
    tmp_path: Path,
) -> None:
    """An expired Copilot token with github_oauth_token is valid."""
    # Arrange
    store = TokenStore(tmp_path)
    token = OAuthToken(
        access_token="access-secret",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        extra={"github_oauth_token": "github-secret"},
    )
    store.save("github-copilot", "oauth", token)

    # Act / Assert
    assert store.has_valid_token("github-copilot", "oauth") is True


def test_token_store_has_valid_token_false_for_missing_token(tmp_path: Path) -> None:
    """A missing token is not valid."""
    # Arrange
    store = TokenStore(tmp_path)

    # Act / Assert
    assert store.has_valid_token("github-copilot", "oauth") is False


def test_token_store_does_not_log_token_values(tmp_path: Path, caplog: Any) -> None:
    """Token store lifecycle logs never include sensitive token values."""
    # Arrange
    store = TokenStore(tmp_path)
    token = OAuthToken(
        access_token="access-secret",
        refresh_token="refresh-secret",
        extra={"github_oauth_token": "github-secret"},
    )

    # Act
    with caplog.at_level(logging.INFO, logger="vbot.providers.token_store"):
        store.save("github-copilot", "oauth", token)
        store.delete("github-copilot", "oauth")

    # Assert
    log_output = caplog.text
    assert "access-secret" not in log_output
    assert "refresh-secret" not in log_output
    assert "github-secret" not in log_output


@pytest.mark.parametrize(
    ("provider_id", "connection_id"),
    [
        ("../outside", "oauth"),
        ("github-copilot", "../oauth"),
        ("github/copilot", "oauth"),
        ("github-copilot", "oauth.json"),
        ("", "oauth"),
    ],
)
def test_token_store_rejects_unsafe_token_ids(
    tmp_path: Path,
    provider_id: str,
    connection_id: str,
) -> None:
    """Provider and connection IDs cannot escape the oauth token directory."""
    # Arrange
    store = TokenStore(tmp_path)

    # Act / Assert
    with pytest.raises(ValueError, match="OAuth token"):
        store.save(provider_id, connection_id, OAuthToken(access_token="access-secret"))

    assert not (tmp_path / "outside-oauth.json").exists()


def test_token_store_named_account_uses_double_dash_file_name(tmp_path: Path) -> None:
    """Named accounts persist to <provider>-<connection>--<account>.json files."""
    # Arrange
    store = TokenStore(tmp_path)

    # Act
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-secret"),
        account_id="work",
    )

    # Assert
    assert (tmp_path / "oauth" / "github-copilot-oauth--work.json").exists()
    assert not (tmp_path / "oauth" / "github-copilot-oauth.json").exists()


def test_token_store_accounts_are_isolated_from_each_other(tmp_path: Path) -> None:
    """Save/load/delete on a named account leaves the default account untouched."""
    # Arrange
    store = TokenStore(tmp_path)
    store.save("github-copilot", "oauth", OAuthToken(access_token="default-secret"))
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-secret"),
        account_id="work",
    )

    # Act
    loaded_work = store.load("github-copilot", "oauth", account_id="work")
    store.delete("github-copilot", "oauth", account_id="work")

    # Assert
    assert loaded_work is not None
    assert loaded_work.access_token == "work-secret"
    assert store.load("github-copilot", "oauth", account_id="work") is None
    default_token = store.load("github-copilot", "oauth")
    assert default_token is not None
    assert default_token.access_token == "default-secret"


def test_token_store_has_valid_token_is_account_scoped(tmp_path: Path) -> None:
    """Token validity checks apply to the requested account only."""
    # Arrange
    store = TokenStore(tmp_path)
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-secret"),
        account_id="work",
    )

    # Act / Assert
    assert store.has_valid_token("github-copilot", "oauth", account_id="work") is True
    assert store.has_valid_token("github-copilot", "oauth") is False


def test_token_store_list_account_ids_orders_default_first(tmp_path: Path) -> None:
    """Stored accounts list as default first, then sorted account ids."""
    # Arrange
    store = TokenStore(tmp_path)
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="zeta-secret"),
        account_id="zeta",
    )
    store.save("github-copilot", "oauth", OAuthToken(access_token="default-secret"))
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="alpha-secret"),
        account_id="alpha",
    )

    # Act / Assert
    assert store.list_account_ids("github-copilot", "oauth") == ["default", "alpha", "zeta"]


def test_token_store_list_account_ids_ignores_non_matching_suffixes(tmp_path: Path) -> None:
    """Files whose account suffix is not a valid account id are ignored."""
    # Arrange
    store = TokenStore(tmp_path)
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken(access_token="work-secret"),
        account_id="work",
    )
    oauth_dir = tmp_path / "oauth"
    (oauth_dir / "github-copilot-oauth--Bad.json").write_text("{}", encoding="utf-8")
    (oauth_dir / "github-copilot-oauth--wo-rk.json").write_text("{}", encoding="utf-8")

    # Act / Assert
    assert store.list_account_ids("github-copilot", "oauth") == ["work"]


def test_token_store_list_account_ids_empty_without_tokens(tmp_path: Path) -> None:
    """Listing accounts on an empty store returns no account ids."""
    # Arrange
    store = TokenStore(tmp_path)

    # Act / Assert
    assert store.list_account_ids("github-copilot", "oauth") == []


@pytest.mark.parametrize("account_id", ["WORK", "wo-rk", "", "../escape", "a" * 33])
def test_token_store_rejects_invalid_account_ids(tmp_path: Path, account_id: str) -> None:
    """Account ids outside the account id alphabet are rejected."""
    # Arrange
    store = TokenStore(tmp_path)

    # Act / Assert
    with pytest.raises(ValueError, match="OAuth token account_id"):
        store.save(
            "github-copilot",
            "oauth",
            OAuthToken(access_token="access-secret"),
            account_id=account_id,
        )
