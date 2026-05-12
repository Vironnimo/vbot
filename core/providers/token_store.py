"""OAuth token persistence for provider connections."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.utils.logging import get_logger

_LOGGER = get_logger("providers.token_store")

TOKEN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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
        self._tmp_dir = data_dir / ".tmp"

    def save(self, provider_id: str, local_connection_id: str, token: OAuthToken) -> None:
        """Persist *token* atomically for the provider connection."""

        self._oauth_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        token_path = self._token_path(provider_id, local_connection_id)
        temp_path = self._tmp_dir / f"{token_path.name}.{uuid4().hex}.tmp"

        temp_path.write_text(
            json.dumps(self._token_to_dict(token), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, token_path)
        _LOGGER.info(
            "Saved OAuth token for provider '%s' connection '%s'",
            provider_id,
            local_connection_id,
        )

    def load(self, provider_id: str, local_connection_id: str) -> OAuthToken | None:
        """Load a token for the provider connection, if one exists."""

        token_path = self._token_path(provider_id, local_connection_id)
        if not token_path.exists():
            return None

        data = json.loads(token_path.read_text(encoding="utf-8"))
        return OAuthToken(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=self._parse_datetime(data.get("expires_at")),
            extra=dict(data.get("extra", {})),
        )

    def delete(self, provider_id: str, local_connection_id: str) -> None:
        """Delete the token for a provider connection, if it exists."""

        token_path = self._token_path(provider_id, local_connection_id)
        try:
            token_path.unlink()
        except FileNotFoundError:
            return
        _LOGGER.info(
            "Deleted OAuth token for provider '%s' connection '%s'",
            provider_id,
            local_connection_id,
        )

    def has_valid_token(self, provider_id: str, local_connection_id: str) -> bool:
        """Return whether a token exists and is usable without user interaction."""

        token = self.load(provider_id, local_connection_id)
        if token is None:
            return False
        if token.expires_at is None:
            return True
        if token.expires_at > datetime.now(UTC):
            return True
        return bool(token.refresh_token or token.extra.get("github_oauth_token"))

    def _token_path(self, provider_id: str, local_connection_id: str) -> Path:
        safe_provider_id = self._validate_token_id("provider_id", provider_id)
        safe_connection_id = self._validate_token_id(
            "local_connection_id",
            local_connection_id,
        )
        token_path = self._oauth_dir / f"{safe_provider_id}-{safe_connection_id}.json"
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

    def _parse_datetime(self, value: str | None) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
