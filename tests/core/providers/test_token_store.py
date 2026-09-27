"""TokenStore: per-Account OAuth token files, their Generation 1 format and validity."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.providers.token_store import (
    OAUTH_TOKEN_FORMAT_VERSION,
    OAuthToken,
    OAuthTokenFileError,
    TokenStore,
)
from core.storage.layout import DataDirectoryLayout


def test_token_round_trips_as_a_versioned_document_without_logging_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = TokenStore(tmp_path)
    token = OAuthToken(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
        extra={"github_oauth_token": "github-secret"},
    )
    token_path = tmp_path / "oauth" / "github-copilot-oauth.json"

    with caplog.at_level(logging.DEBUG, logger="vbot.providers.token_store"):
        store.save("github-copilot", "oauth", token)
        assert store.load("github-copilot", "oauth") == token

    document = json.loads(token_path.read_text(encoding="utf-8"))
    assert document["format_version"] == OAUTH_TOKEN_FORMAT_VERSION
    # Atomic writes stage in the data directory, never beside the token or in OS temp.
    staging = DataDirectoryLayout(tmp_path).atomic_temporary
    assert staging.is_dir()
    assert list(staging.iterdir()) == []
    for secret in ("access-secret", "refresh-secret", "github-secret"):
        assert secret not in caplog.text

    document["issuer"] = "kept"
    token_path.write_text(json.dumps(document), encoding="utf-8")
    store.save("github-copilot", "oauth", OAuthToken(access_token="second"))

    rewritten = json.loads(token_path.read_text(encoding="utf-8"))
    assert rewritten["access_token"] == "second"
    assert rewritten["issuer"] == "kept"


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("not json", id="corrupt"),
        pytest.param('{"access_token": "legacy"}', id="unversioned"),
        pytest.param('{"format_version": 2, "access_token": "newer"}', id="newer"),
        pytest.param('{"format_version": 1, "access_token": ""}', id="invalid-field"),
    ],
)
def test_token_file_that_fails_to_load_is_guarded_but_can_be_deleted(
    tmp_path: Path, stored: str
) -> None:
    store = TokenStore(tmp_path)
    token_path = tmp_path / "oauth" / "github-copilot-oauth.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(stored, encoding="utf-8")

    with pytest.raises(OAuthTokenFileError, match="Disconnecting the Provider account"):
        store.load("github-copilot", "oauth")
    assert store.has_valid_token("github-copilot", "oauth") is False
    with pytest.raises(OAuthTokenFileError, match="Refusing to overwrite OAuth token"):
        store.save("github-copilot", "oauth", OAuthToken(access_token="fresh"))
    assert token_path.read_text(encoding="utf-8") == stored
    assert store.exists("github-copilot", "oauth") is True

    store.delete("github-copilot", "oauth")

    assert store.exists("github-copilot", "oauth") is False
    store.save("github-copilot", "oauth", OAuthToken(access_token="fresh"))
    assert store.load("github-copilot", "oauth") == OAuthToken(access_token="fresh")


_PAST = timedelta(minutes=-5)
_FUTURE = timedelta(minutes=5)


@pytest.mark.parametrize(
    ("expires_in", "refresh_token", "extra", "valid"),
    [
        pytest.param(_FUTURE, None, {}, True, id="not-expired"),
        pytest.param(None, None, {}, True, id="no-expiry"),
        pytest.param(_PAST, None, {}, False, id="expired-without-refresh-path"),
        pytest.param(_PAST, "refresh-secret", {}, True, id="expired-with-refresh-token"),
        pytest.param(
            _PAST,
            None,
            {"github_oauth_token": "github-secret"},
            True,
            id="expired-with-copilot-exchange",
        ),
    ],
)
def test_token_is_valid_while_usable_without_user_interaction(
    tmp_path: Path,
    expires_in: timedelta | None,
    refresh_token: str | None,
    extra: dict[str, str],
    valid: bool,
) -> None:
    store = TokenStore(tmp_path)
    assert store.has_valid_token("github-copilot", "oauth") is False
    store.save(
        "github-copilot",
        "oauth",
        OAuthToken(
            access_token="access-secret",
            refresh_token=refresh_token,
            expires_at=None if expires_in is None else datetime.now(UTC) + expires_in,
            extra=extra,
        ),
    )

    assert store.has_valid_token("github-copilot", "oauth") is valid


def test_named_accounts_use_their_own_files_and_stay_isolated(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    store.save("github-copilot", "oauth", OAuthToken(access_token="work-secret"), account_id="work")

    assert (tmp_path / "oauth" / "github-copilot-oauth--work.json").exists()
    assert store.has_valid_token("github-copilot", "oauth", account_id="work") is True
    assert store.has_valid_token("github-copilot", "oauth") is False

    store.save("github-copilot", "oauth", OAuthToken(access_token="default-secret"))
    store.delete("github-copilot", "oauth", account_id="work")
    store.delete("github-copilot", "oauth", account_id="work")

    assert store.load("github-copilot", "oauth", account_id="work") is None
    assert store.load("github-copilot", "oauth") == OAuthToken(access_token="default-secret")


def test_list_account_ids_orders_default_first_and_ignores_foreign_suffixes(
    tmp_path: Path,
) -> None:
    store = TokenStore(tmp_path)
    assert store.list_account_ids("github-copilot", "oauth") == []

    for account_id in ("zeta", "default", "alpha"):
        store.save(
            "github-copilot", "oauth", OAuthToken(access_token="secret"), account_id=account_id
        )
    oauth_dir = tmp_path / "oauth"
    (oauth_dir / "github-copilot-oauth--Bad.json").write_text("{}", encoding="utf-8")
    (oauth_dir / "github-copilot-oauth--wo-rk.json").write_text("{}", encoding="utf-8")

    assert store.list_account_ids("github-copilot", "oauth") == ["default", "alpha", "zeta"]


@pytest.mark.parametrize(
    ("provider_id", "connection_id", "account_id"),
    [
        pytest.param("../outside", "oauth", "default", id="provider"),
        pytest.param("github-copilot", "../oauth", "default", id="connection"),
        pytest.param("github-copilot", "oauth", "../escape", id="account"),
    ],
)
def test_token_ids_cannot_escape_the_token_directory(
    tmp_path: Path, provider_id: str, connection_id: str, account_id: str
) -> None:
    store = TokenStore(tmp_path)

    with pytest.raises(ValueError):
        store.save(
            provider_id, connection_id, OAuthToken(access_token="secret"), account_id=account_id
        )

    assert not (tmp_path / "outside-oauth.json").exists()
    assert not (tmp_path / "oauth").exists() or list((tmp_path / "oauth").iterdir()) == []
