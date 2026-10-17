"""TokenStore: per-Account OAuth tokens in the Secret Store, their format and validity."""

from __future__ import annotations

import base64
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
from core.secrets import OAUTH_TOKENS, SecretStore


def _tamper(store_path: Path, token_id: str) -> None:
    """Flip one ciphertext byte of a stored entry, as a damaged copy would."""
    document = json.loads(store_path.read_text(encoding="utf-8"))
    sealed = bytearray(base64.b64decode(document[OAUTH_TOKENS][token_id]))
    sealed[-1] ^= 0x01
    document[OAUTH_TOKENS][token_id] = base64.b64encode(bytes(sealed)).decode("ascii")
    store_path.write_text(json.dumps(document), encoding="utf-8")


def test_token_round_trips_as_a_versioned_document_without_exposing_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    token = OAuthToken(
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
        extra={"github_oauth_token": "github-secret"},
    )

    with caplog.at_level(logging.DEBUG):
        store.save("github-copilot", "oauth", token)
        assert store.load("github-copilot", "oauth") == token

    stored = secrets.entry(OAUTH_TOKENS, "github-copilot-oauth")
    assert stored is not None
    document = json.loads(stored)
    assert document["format_version"] == OAUTH_TOKEN_FORMAT_VERSION
    on_disk = secrets.path.read_text(encoding="utf-8")
    for secret in ("access-secret", "refresh-secret", "github-secret"):
        assert secret not in caplog.text
        assert secret not in on_disk
    assert not (tmp_path / "oauth").exists()

    # Fields a newer vBot added to the token stay through a later save.
    document["issuer"] = "kept"
    secrets.set_entry(OAUTH_TOKENS, "github-copilot-oauth", json.dumps(document))
    store.save("github-copilot", "oauth", OAuthToken(access_token="second"))

    rewritten = json.loads(secrets.entry(OAUTH_TOKENS, "github-copilot-oauth") or "")
    assert rewritten["access_token"] == "second"
    assert rewritten["issuer"] == "kept"


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("not json", id="corrupt"),
        pytest.param('{"access_token": "legacy"}', id="unversioned"),
        pytest.param('{"format_version": 2, "access_token": "newer"}', id="newer"),
        pytest.param('{"format_version": 1, "access_token": ""}', id="invalid-field"),
        pytest.param(None, id="does-not-decrypt"),
    ],
)
def test_token_that_fails_to_load_is_guarded_but_can_be_deleted(
    tmp_path: Path, stored: str | None
) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    if stored is None:
        store.save("github-copilot", "oauth", OAuthToken(access_token="old"))
        _tamper(secrets.path, "github-copilot-oauth")
    else:
        secrets.set_entry(OAUTH_TOKENS, "github-copilot-oauth", stored)
    before = secrets.path.read_bytes()

    with pytest.raises(OAuthTokenFileError, match="Disconnecting the Provider account"):
        store.load("github-copilot", "oauth")
    assert store.has_valid_token("github-copilot", "oauth") is False
    with pytest.raises(OAuthTokenFileError, match="Refusing to overwrite OAuth token"):
        store.save("github-copilot", "oauth", OAuthToken(access_token="fresh"))
    assert secrets.path.read_bytes() == before
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
    store = TokenStore(SecretStore(tmp_path))
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


def test_named_accounts_use_their_own_entries_and_stay_isolated(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    store.save("github-copilot", "oauth", OAuthToken(access_token="work-secret"), account_id="work")

    assert secrets.entry_names(OAUTH_TOKENS) == ("github-copilot-oauth--work",)
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
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    assert store.list_account_ids("github-copilot", "oauth") == []

    for account_id in ("zeta", "default", "alpha"):
        store.save(
            "github-copilot", "oauth", OAuthToken(access_token="secret"), account_id=account_id
        )
    secrets.set_entry(OAUTH_TOKENS, "github-copilot-oauth--Bad", "{}")
    secrets.set_entry(OAUTH_TOKENS, "github-copilot-oauth--wo-rk", "{}")

    assert store.list_account_ids("github-copilot", "oauth") == ["default", "alpha", "zeta"]


@pytest.mark.parametrize(
    ("provider_id", "connection_id", "account_id"),
    [
        pytest.param("../outside", "oauth", "default", id="provider"),
        pytest.param("github-copilot", "../oauth", "default", id="connection"),
        pytest.param("github-copilot", "oauth", "../escape", id="account"),
    ],
)
def test_token_ids_are_validated_before_anything_is_stored(
    tmp_path: Path, provider_id: str, connection_id: str, account_id: str
) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)

    with pytest.raises(ValueError):
        store.save(
            provider_id, connection_id, OAuthToken(access_token="secret"), account_id=account_id
        )

    assert not secrets.path.exists()
