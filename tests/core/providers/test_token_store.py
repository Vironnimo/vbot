"""Tests for OAuth token persistence."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
