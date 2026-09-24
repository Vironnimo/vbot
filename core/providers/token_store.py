"""OAuth token persistence for provider connections."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
    validate_non_empty_string,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    JsonDocumentWriteError,
    json_document,
    validate_format_version,
    write_json_document,
)
from core.providers.accounts import ACCOUNT_ID_PATTERN, DEFAULT_ACCOUNT_ID, sorted_account_ids
from core.utils.errors import ConfigError
from core.utils.logging import get_logger

TOKEN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ACCOUNT_FILE_SEPARATOR = "--"
OAUTH_TOKEN_FORMAT_VERSION = 1
# ``extra`` is keyed by data (Provider-specific metadata), so only the root has fields.
OAUTH_TOKEN_SHAPE = json_document({"access_token", "refresh_token", "expires_at", "extra"})
_RESET_HINT = "Disconnecting the Provider account removes the file; then connect it again."

_LOGGER = get_logger("providers.token_store")


class OAuthTokenFileError(ConfigError):
    """Raised when a stored OAuth token file fails to load or must not be overwritten."""


def validate_oauth_token_file(token_path: str | Path) -> JsonValidationReport:
    """Validate one persisted OAuth token file without consuming it."""
    return validate_json_file(token_path, validate_oauth_token_data, missing_ok=False)


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
        except (TypeError, ValueError):
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
    """File-backed OAuth token store rooted in the runtime data directory."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._oauth_dir = data_dir / "oauth"
        self._refresh_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._mutation_lock = RLock()

    def refresh_lock(
        self,
        provider_id: str,
        local_connection_id: str,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> asyncio.Lock:
        """Return the shared refresh lock for one Provider Connection Account."""

        key = (
            self._validate_token_id("provider_id", provider_id),
            self._validate_token_id("local_connection_id", local_connection_id),
            self._validate_account_id(account_id),
        )
        lock = self._refresh_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[key] = lock
        return lock

    def save(
        self,
        provider_id: str,
        local_connection_id: str,
        token: OAuthToken,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        """Persist *token* atomically for the provider connection account.

        Raises :class:`OAuthTokenFileError` instead of overwriting a stored token
        file that fails to load.
        """

        token_path = self._token_path(provider_id, local_connection_id, account_id)
        with self._mutation_lock:
            try:
                write_json_document(
                    token_path,
                    self._token_to_dict(token),
                    OAUTH_TOKEN_FORMAT,
                    data_dir=self._data_dir,
                )
            except JsonDocumentWriteError as error:
                raise OAuthTokenFileError(f"{error} {_RESET_HINT}") from error

    def load(
        self,
        provider_id: str,
        local_connection_id: str,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> OAuthToken | None:
        """Load a token for the provider connection account, if one exists.

        Raises :class:`OAuthTokenFileError` when the stored file fails to load.
        """

        token_path = self._token_path(provider_id, local_connection_id, account_id)
        try:
            data = load_validated_json_file(token_path, validate_oauth_token_data, missing_ok=True)
        except (JsonConfigValidationError, OSError) as error:
            raise OAuthTokenFileError(
                f"OAuth token file failed to load: {error}. {_RESET_HINT}"
            ) from error
        if data is None:
            return None
        return OAuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=_parse_datetime(data.get("expires_at")),
            extra=dict(data.get("extra", {})),
        )

    def exists(
        self,
        provider_id: str,
        local_connection_id: str,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> bool:
        """Return whether a token file is stored, without loading it."""

        return self._token_path(provider_id, local_connection_id, account_id).exists()

    def delete(
        self,
        provider_id: str,
        local_connection_id: str,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        """Delete the token for a provider connection account, if it exists."""

        token_path = self._token_path(provider_id, local_connection_id, account_id)
        with self._mutation_lock:
            try:
                token_path.unlink()
            except FileNotFoundError:
                return

    def replace_if_current(
        self,
        provider_id: str,
        local_connection_id: str,
        expected: OAuthToken,
        replacement: OAuthToken | None,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> bool:
        """Apply a delayed refresh only while its source credentials remain current.

        Disconnect and a fresh login remain immediate synchronous mutations.
        A network refresh may neither revive their retired token nor remove
        replacement credentials after its own exchange fails.
        """
        with self._mutation_lock:
            if self.load(provider_id, local_connection_id, account_id=account_id) != expected:
                return False
            if replacement is None:
                self.delete(provider_id, local_connection_id, account_id=account_id)
            else:
                self.save(provider_id, local_connection_id, replacement, account_id=account_id)
            return True

    def has_valid_token(
        self,
        provider_id: str,
        local_connection_id: str,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> bool:
        """Return whether a token exists and is usable without user interaction.

        A token file that fails to load is not usable; the failure is logged.
        """

        try:
            token = self.load(provider_id, local_connection_id, account_id=account_id)
        except OAuthTokenFileError as error:
            _LOGGER.warning("Stored OAuth token is unusable: %s", error)
            return False
        if token is None:
            return False
        if token.expires_at is None:
            return True
        if token.expires_at > datetime.now(UTC):
            return True
        return bool(token.refresh_token or token.extra.get("github_oauth_token"))

    def list_account_ids(self, provider_id: str, local_connection_id: str) -> list[str]:
        """Return stored account ids for the connection, default first then sorted."""

        safe_provider_id = self._validate_token_id("provider_id", provider_id)
        safe_connection_id = self._validate_token_id(
            "local_connection_id",
            local_connection_id,
        )
        account_ids: list[str] = []
        if (self._oauth_dir / f"{safe_provider_id}-{safe_connection_id}.json").exists():
            account_ids.append(DEFAULT_ACCOUNT_ID)
        if not self._oauth_dir.is_dir():
            return account_ids

        prefix = f"{safe_provider_id}-{safe_connection_id}{_ACCOUNT_FILE_SEPARATOR}"
        for token_path in self._oauth_dir.glob(f"{prefix}*.json"):
            suffix = token_path.name.removeprefix(prefix).removesuffix(".json")
            if ACCOUNT_ID_PATTERN.fullmatch(suffix) and suffix not in account_ids:
                account_ids.append(suffix)
        return sorted_account_ids(account_ids)

    def _token_path(self, provider_id: str, local_connection_id: str, account_id: str) -> Path:
        safe_provider_id = self._validate_token_id("provider_id", provider_id)
        safe_connection_id = self._validate_token_id(
            "local_connection_id",
            local_connection_id,
        )
        safe_account_id = self._validate_account_id(account_id)
        file_name = f"{safe_provider_id}-{safe_connection_id}.json"
        if safe_account_id != DEFAULT_ACCOUNT_ID:
            file_name = (
                f"{safe_provider_id}-{safe_connection_id}"
                f"{_ACCOUNT_FILE_SEPARATOR}{safe_account_id}.json"
            )
        token_path = self._oauth_dir / file_name
        oauth_root = self._oauth_dir.resolve()
        resolved_path = token_path.resolve()
        if resolved_path.parent != oauth_root:
            raise ValueError("OAuth token path must stay within the token store directory")
        return token_path

    def _validate_token_id(self, field_name: str, value: str) -> str:
        if not TOKEN_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"OAuth token {field_name} must contain only letters, numbers, underscores, "
                "or hyphens, and must start with a letter or number"
            )
        return value

    def _validate_account_id(self, value: str) -> str:
        if not ACCOUNT_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "OAuth token account_id must contain only lowercase letters, digits, or "
                "underscores, start with a letter or digit, and be at most 32 characters"
            )
        return value

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


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
