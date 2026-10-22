"""TokenStore: per-Account OAuth tokens in the Secret Store, their format and validity."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.providers.accounts import Account
from core.providers.token_store import (
    OAUTH_TOKEN_FORMAT_VERSION,
    OAuthToken,
    OAuthTokenFileError,
    TokenStore,
)
from core.secrets import OAUTH_TOKENS, SecretStore, SecretTransaction
from tests.core.database.database_test_support import frozen_members


def _tamper(store_path: Path, token_id: str) -> None:
    """Flip one ciphertext byte of a stored entry, as a damaged copy would."""
    document = json.loads(store_path.read_text(encoding="utf-8"))
    sealed = bytearray(base64.b64decode(document[OAUTH_TOKENS][token_id]))
    sealed[-1] ^= 0x01
    document[OAUTH_TOKENS][token_id] = base64.b64encode(bytes(sealed)).decode("ascii")
    store_path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("mutation", ["save", "delete", "refresh", "retire"])
def test_snapshot_waiting_token_writes_leave_reads_and_refresh_locks_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    account = Account("github-copilot", "oauth")
    token = OAuthToken(access_token="original")
    replacement = OAuthToken(access_token="replacement")
    store.save(account, token)
    refresh_lock = store.refresh_lock(account)
    original = secrets.path.read_bytes()

    def change() -> None:
        if mutation == "save":
            store.save(account, replacement)
        elif mutation == "delete":
            store.delete(account)
        else:
            assert store.replace_if_current(
                account, token, replacement if mutation == "refresh" else None
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        with frozen_members(tmp_path, monkeypatch) as gate:
            writing = executor.submit(change)
            assert gate.waiting.acquire(timeout=2)
            assert executor.submit(store.refresh_lock, account).result(timeout=2) is refresh_lock
            other = Account("github-copilot", "oauth", "other")
            assert executor.submit(store.refresh_lock, other).result(timeout=2) is not refresh_lock
            assert executor.submit(store.load, account).result(timeout=2) == token
            assert secrets.path.read_bytes() == original
            assert not writing.done()
        writing.result(timeout=2)

    assert store.load(account) == (replacement if mutation in {"save", "refresh"} else None)
    assert gate.capture is not None
    assert (gate.capture.attempts, gate.capture.waited_changes) == (1, 1)


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
        store.save(Account("github-copilot", "oauth"), token)
        assert store.load(Account("github-copilot", "oauth")) == token

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
    store.save(Account("github-copilot", "oauth"), OAuthToken(access_token="second"))

    rewritten = json.loads(secrets.entry(OAUTH_TOKENS, "github-copilot-oauth") or "")
    assert rewritten["access_token"] == "second"
    assert rewritten["issuer"] == "kept"

    refreshed = OAuthToken(access_token="refreshed")
    assert store.replace_if_current(
        Account("github-copilot", "oauth"), OAuthToken(access_token="second"), refreshed
    )
    rewritten = json.loads(secrets.entry(OAUTH_TOKENS, "github-copilot-oauth") or "")
    assert rewritten["access_token"] == refreshed.access_token
    assert rewritten["issuer"] == "kept"
    assert store.replace_if_current(Account("github-copilot", "oauth"), refreshed, None)
    assert store.load(Account("github-copilot", "oauth")) is None


@pytest.mark.parametrize("reconnect", [False, True], ids=["disconnect", "reconnect"])
@pytest.mark.parametrize(
    "replacement",
    [OAuthToken(access_token="refreshed"), None],
    ids=["publish-refresh", "retire-rejected-token"],
)
def test_delayed_refresh_preserves_changes_from_another_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconnect: bool,
    replacement: OAuthToken | None,
) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    other = TokenStore(SecretStore(tmp_path))
    account = Account("github-copilot", "oauth")
    original = OAuthToken(access_token="original")
    newer = OAuthToken(access_token="new-login") if reconnect else None
    store.save(account, original)
    update = secrets.update

    def intervening_update[Result](change: Callable[[SecretTransaction], Result]) -> Result:
        # Another process finishes its mutation before this writer takes the
        # Secret Store's lock. Comparison must read that locked snapshot too.
        if newer is None:
            other.delete(account)
        else:
            other.save(account, newer)
        return update(change)

    monkeypatch.setattr(secrets, "update", intervening_update)

    assert store.replace_if_current(account, original, replacement) is False
    assert other.load(account) == newer


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
        store.save(Account("github-copilot", "oauth"), OAuthToken(access_token="old"))
        _tamper(secrets.path, "github-copilot-oauth")
    else:
        secrets.set_entry(OAUTH_TOKENS, "github-copilot-oauth", stored)
    before = secrets.path.read_bytes()

    with pytest.raises(OAuthTokenFileError, match="Disconnecting the Provider account"):
        store.load(Account("github-copilot", "oauth"))
    assert store.has_valid_token(Account("github-copilot", "oauth")) is False
    with pytest.raises(OAuthTokenFileError, match="Refusing to overwrite OAuth token"):
        store.save(Account("github-copilot", "oauth"), OAuthToken(access_token="fresh"))
    assert secrets.path.read_bytes() == before
    assert store.exists(Account("github-copilot", "oauth")) is True

    store.delete(Account("github-copilot", "oauth"))

    assert store.exists(Account("github-copilot", "oauth")) is False
    store.save(Account("github-copilot", "oauth"), OAuthToken(access_token="fresh"))
    assert store.load(Account("github-copilot", "oauth")) == OAuthToken(access_token="fresh")


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
    assert store.has_valid_token(Account("github-copilot", "oauth")) is False
    store.save(
        Account("github-copilot", "oauth"),
        OAuthToken(
            access_token="access-secret",
            refresh_token=refresh_token,
            expires_at=None if expires_in is None else datetime.now(UTC) + expires_in,
            extra=extra,
        ),
    )

    assert store.has_valid_token(Account("github-copilot", "oauth")) is valid


def test_named_accounts_use_their_own_entries_and_stay_isolated(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    store.save(Account("github-copilot", "oauth", "work"), OAuthToken(access_token="work-secret"))

    assert secrets.entry_names(OAUTH_TOKENS) == ("github-copilot-oauth--work",)
    assert store.has_valid_token(Account("github-copilot", "oauth", "work")) is True
    assert store.has_valid_token(Account("github-copilot", "oauth")) is False

    store.save(Account("github-copilot", "oauth"), OAuthToken(access_token="default-secret"))
    store.delete(Account("github-copilot", "oauth", "work"))
    store.delete(Account("github-copilot", "oauth", "work"))

    assert store.load(Account("github-copilot", "oauth", "work")) is None
    assert store.load(Account("github-copilot", "oauth")) == OAuthToken(
        access_token="default-secret"
    )


def test_list_account_ids_orders_default_first_and_ignores_foreign_suffixes(
    tmp_path: Path,
) -> None:
    secrets = SecretStore(tmp_path)
    store = TokenStore(secrets)
    assert store.list_account_ids("github-copilot", "oauth") == []

    for account_id in ("zeta", "default", "alpha"):
        store.save(
            Account("github-copilot", "oauth", account_id), OAuthToken(access_token="secret")
        )
    secrets.set_entry(OAUTH_TOKENS, "github-copilot-oauth--Bad", "{}")
    secrets.set_entry(OAUTH_TOKENS, "github-copilot-oauth--wo-rk", "{}")

    assert store.list_account_ids("github-copilot", "oauth") == ["default", "alpha", "zeta"]
