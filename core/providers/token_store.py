"""OAuth token persistence for provider connections.

Each token is one OAuth token document (``OAUTH_TOKEN_FORMAT``) stored as an
encrypted entry of the Secret Store (``core.secrets``), keyed by its token id
``<provider>-<connection>`` or ``<provider>-<connection>--<account>``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from core.config_validation import (
    JsonDiagnostic,
    add_error,
    error_diagnostic,
    validate_non_empty_string,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    preserve_unknown_fields,
    render_json_document,
    validate_format_version,
)
from core.providers.accounts import (
    ACCOUNT_ID_PATTERN,
    DEFAULT_ACCOUNT_ID,
    Account,
    sorted_account_ids,
)
from core.secrets import OAUTH_TOKENS, SecretStore, SecretStoreError, SecretTransaction
from core.utils.errors import ConfigError
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_ACCOUNT_FILE_SEPARATOR = "--"
OAUTH_TOKEN_FORMAT_VERSION = 1
# ``extra`` is keyed by data (Provider-specific metadata), so only the root has fields.
OAUTH_TOKEN_SHAPE = json_document({"access_token", "refresh_token", "expires_at", "extra"})
_RESET_HINT = "Disconnecting the Provider account removes the token; then connect it again."

_LOGGER = get_logger("providers.token_store")
_TOKEN_WORKERS = BoundedWorkerPool(name="provider-tokens", max_workers=4)


class OAuthTokenFileError(ConfigError):
    """Raised when a stored OAuth token fails to load or must not be overwritten."""


def validate_oauth_token_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw OAuth token document."""
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, OAUTH_TOKEN_FORMAT_VERSION):
        return diagnostics
    warn_unknown_keys(diagnostics, "$", data, OAUTH_TOKEN_SHAPE.fields, "OAuth token field")
    validate_non_empty_string(
        diagnostics, "$.access_token", data.get("access_token"), required=True
    )
    refresh_token = data.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        add_error(diagnostics, "$.refresh_token", "must be a string or null")
    expires_at = data.get("expires_at")
    if expires_at is not None:
        try:
            _parse_datetime(expires_at)
        except TypeError, ValueError:
            add_error(diagnostics, "$.expires_at", "must be an ISO 8601 timestamp or null")
    extra = data.get("extra", {})
    if not isinstance(extra, dict) or any(not isinstance(value, str) for value in extra.values()):
        add_error(diagnostics, "$.extra", "must be an object with string values")
    return diagnostics


OAUTH_TOKEN_FORMAT = JsonDocumentFormat(
    name="OAuth token",
    version=OAUTH_TOKEN_FORMAT_VERSION,
    shape=OAUTH_TOKEN_SHAPE,
    validate=validate_oauth_token_data,
    sort_keys=True,
)


@dataclass(frozen=True)
class OAuthToken:
    """Persisted OAuth token data for a provider connection."""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    extra: dict[str, str] = field(default_factory=dict)


class TokenStore:
    """OAuth token store of the Provider Connections, kept in the Secret Store."""

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets
        self._refresh_locks: dict[Account, asyncio.Lock] = {}
        # Protect only the refresh-lock catalog; SecretStore serializes writes
        # in its transaction after snapshot admission, without holding this lock.
        self._refresh_locks_lock = RLock()
        self._unusable = LoggedConditions()

    def refresh_lock(
        self,
        account: Account,
    ) -> asyncio.Lock:
        """Return the shared refresh lock for one Provider Connection Account."""

        key = account
        with self._refresh_locks_lock:
            lock = self._refresh_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._refresh_locks[key] = lock
            return lock

    def save(
        self,
        account: Account,
        token: OAuthToken,
    ) -> None:
        """Persist *token* for the provider connection account.

        Fields a newer vBot added to the stored token stay. Raises
        :class:`OAuthTokenFileError` instead of overwriting a stored token that
        fails to load, or when the Secret Store cannot be written.
        """

        token_id = self._token_id(account)
        payload = self._token_to_dict(token)
        try:
            self._secrets.update(lambda transaction: self._write(transaction, token_id, payload))
        except SecretStoreError as error:
            raise OAuthTokenFileError(f"{error} {_RESET_HINT}") from error

    async def save_async(
        self,
        account: Account,
        token: OAuthToken,
    ) -> None:
        """Persist a login off the Event Loop, settling writes before cancellation."""
        await _TOKEN_WORKERS.run(self.save, account, token)

    def load(
        self,
        account: Account,
    ) -> OAuthToken | None:
        """Load a token for the provider connection account, if one exists.

        Raises :class:`OAuthTokenFileError` when the stored token fails to load,
        including while the Secret Store is unavailable.
        """

        token_id = self._token_id(account)
        try:
            text = self._secrets.entry(OAUTH_TOKENS, token_id)
        except SecretStoreError as error:
            raise OAuthTokenFileError(
                f"OAuth token failed to load: {error}. {_RESET_HINT}"
            ) from error
        if text is None:
            return None
        return _token_from_text(text)

    def exists(
        self,
        account: Account,
    ) -> bool:
        """Return whether a token is stored, without loading it."""

        token_id = self._token_id(account)
        return token_id in self._secrets.entry_names(OAUTH_TOKENS)

    async def load_async(
        self,
        account: Account,
    ) -> OAuthToken | None:
        """Read current credentials without store work on the Event Loop."""
        return await _TOKEN_WORKERS.run(self.load, account)

    def delete(
        self,
        account: Account,
    ) -> None:
        """Delete the token for a provider connection account, if it exists.

        Raises :class:`OAuthTokenFileError` when the Secret Store cannot be written.
        """

        token_id = self._token_id(account)
        try:
            self._secrets.remove_entry(OAUTH_TOKENS, token_id)
        except SecretStoreError as error:
            raise OAuthTokenFileError(str(error)) from error

    def replace_if_current(
        self,
        account: Account,
        expected: OAuthToken,
        replacement: OAuthToken | None,
    ) -> bool:
        """Apply a delayed refresh only while its source credentials remain current.

        Disconnect and a fresh login remain immediate synchronous mutations.
        A network refresh may neither revive their retired token nor remove
        replacement credentials after its own exchange fails.
        """
        token_id = self._token_id(account)

        def replace(transaction: SecretTransaction) -> bool:
            text = transaction.get(OAUTH_TOKENS, token_id)
            if text is None or _token_from_text(text) != expected:
                return False
            if replacement is None:
                transaction.remove(OAUTH_TOKENS, token_id)
            else:
                self._write(transaction, token_id, self._token_to_dict(replacement))
            return True

        try:
            return self._secrets.update(replace)
        except SecretStoreError as error:
            raise OAuthTokenFileError(f"{error} {_RESET_HINT}") from error

    async def replace_if_current_async(
        self,
        account: Account,
        expected: OAuthToken,
        replacement: OAuthToken | None,
    ) -> bool:
        """Settle an atomic credential comparison/publication off the Event Loop."""
        return await _TOKEN_WORKERS.run(
            self.replace_if_current,
            account,
            expected,
            replacement,
        )

    def has_valid_token(
        self,
        account: Account,
    ) -> bool:
        """Return whether a token exists and is usable without user interaction.

        A token that fails to load is not usable; the failure is logged when it
        starts or changes, not on every check.
        """

        token_id = self._token_id(account)
        try:
            token = self.load(account)
        except OAuthTokenFileError as error:
            if self._unusable.started(token_id, str(error)):
                _LOGGER.warning("Stored OAuth token is unusable: %s", error)
            return False
        if self._unusable.ended(token_id):
            _LOGGER.info("Stored OAuth token loads again (token=%s)", token_id)
        if token is None:
            return False
        if token.expires_at is None:
            return True
        if token.expires_at > datetime.now(UTC):
            return True
        return bool(token.refresh_token or token.extra.get("github_oauth_token"))

    def list_account_ids(self, provider_id: str, local_connection_id: str) -> list[str]:
        """Return stored account ids for the connection, default first then sorted."""

        base = self._token_id(Account(provider_id, local_connection_id))
        prefix = f"{base}{_ACCOUNT_FILE_SEPARATOR}"
        account_ids: list[str] = []
        for token_id in self._secrets.entry_names(OAUTH_TOKENS):
            if token_id == base:
                account_ids.append(DEFAULT_ACCOUNT_ID)
                continue
            suffix = token_id.removeprefix(prefix)
            if (
                suffix != token_id
                and ACCOUNT_ID_PATTERN.fullmatch(suffix)
                and suffix not in account_ids
            ):
                account_ids.append(suffix)
        return sorted_account_ids(account_ids)

    @staticmethod
    def _token_id(account: Account) -> str:
        """Encode an Account with the stable Generation 1 Secret Store key."""
        base = f"{account.provider_id}-{account.local_connection_id}"
        if account.account_id == DEFAULT_ACCOUNT_ID:
            return base
        return f"{base}{_ACCOUNT_FILE_SEPARATOR}{account.account_id}"

    def _write(
        self, transaction: SecretTransaction, token_id: str, payload: dict[str, object]
    ) -> None:
        # A stored token that fails to load is never overwritten.
        try:
            previous_text = transaction.get(OAUTH_TOKENS, token_id)
            previous = None if previous_text is None else _decode_token(previous_text)
        except (SecretStoreError, OAuthTokenFileError) as error:
            raise OAuthTokenFileError(
                f"Refusing to overwrite OAuth token {token_id}, which fails to load. {_RESET_HINT}"
            ) from error
        body: Any = payload
        if previous is not None:
            body = preserve_unknown_fields(previous, payload, OAUTH_TOKEN_SHAPE)
        text = render_json_document(
            body, version=OAUTH_TOKEN_FORMAT_VERSION, sort_keys=OAUTH_TOKEN_FORMAT.sort_keys
        )
        transaction.set(OAUTH_TOKENS, token_id, text)

    def _token_to_dict(self, token: OAuthToken) -> dict[str, object]:
        return {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": self._format_datetime(token.expires_at),
            "extra": token.extra,
        }

    def _format_datetime(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat()


def _token_from_text(text: str) -> OAuthToken:
    data = _decode_token(text)
    return OAuthToken(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=_parse_datetime(data.get("expires_at")),
        extra=dict(data.get("extra", {})),
    )


def _decode_token(text: str) -> dict[str, Any]:
    """Decode and validate one stored token document."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise OAuthTokenFileError(f"OAuth token is not valid JSON. {_RESET_HINT}") from error
    errors = [item for item in validate_oauth_token_data(data) if item.severity == "error"]
    if errors:
        details = "; ".join(f"{item.path}: {item.message}" for item in errors)
        raise OAuthTokenFileError(f"OAuth token failed to load: {details}. {_RESET_HINT}")
    assert isinstance(data, dict)
    return data


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
