"""Channel configuration schema and validated values."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_allowed_string,
    validate_json_file,
    validate_non_empty_string,
    warn_unknown_keys,
)
from core.settings import is_valid_agent_id
from core.utils.errors import VBotError

if TYPE_CHECKING:
    pass

_DEFAULT_DM_SCOPE = "per_conversation"


ALLOWED_CHANNEL_DM_SCOPES = frozenset(
    ("per_conversation", "main", "per_peer", "per_account_channel_peer")
)


_DEFAULT_RESPONSE_MODE = "mention"


ALLOWED_CHANNEL_RESPONSE_MODES = frozenset(("mention", "all"))


ALLOWED_CHANNEL_PLATFORMS = frozenset(("discord", "telegram"))


MANAGED_CHANNEL_TOKEN_ENV_PREFIX = "VBOT_CHANNEL_TOKEN__"


_CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


_MUTABLE_FIELDS = frozenset(
    (
        "platform",
        "agent_id",
        "dm_scope",
        "allowed_chat_ids",
        "token_env_var",
        "enabled",
        "response_mode",
        "mention_patterns",
        "observe_unaddressed",
    )
)


_CHANNEL_CONFIG_FIELDS = _MUTABLE_FIELDS | {"id", "owner_user_ids"}


class ChannelError(VBotError):
    """Base class for expected channel-domain errors.

    ``retryable`` marks transient transport failures (network blips, platform
    rate limits) that reply delivery may retry; permanent platform rejections
    stay non-retryable. ``retry_after`` carries a rate-limit wait hint honored
    as a floor by the retry loop.
    """

    def __init__(
        self,
        message: str = "",
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ChannelNotFoundError(ChannelError):
    """Raised when a channel id is unknown."""


class ChannelConfigError(ChannelError):
    """Raised when channel config data is invalid."""


def validate_channel_file(config_path: str | Path) -> JsonValidationReport:
    """Validate one persisted ``channel.json`` without consuming it."""
    return validate_json_file(config_path, validate_channel_data, missing_ok=False)


def load_validated_channel_json(config_path: str | Path) -> JsonObject:
    """Load one schema-valid ``channel.json`` mapping."""
    try:
        return cast(
            "JsonObject",
            load_validated_json_file(config_path, validate_channel_data, missing_ok=False),
        )
    except JsonConfigValidationError as error:
        raise ChannelConfigError(str(error)) from error


def validate_channel_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``channel.json`` mapping."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    warn_unknown_keys(diagnostics, "$", data, _CHANNEL_CONFIG_FIELDS, "channel field")
    try:
        _normalize_channel_id(data.get("id"))
    except ChannelConfigError as error:
        add_error(diagnostics, "$.id", str(error))
    validate_allowed_string(
        diagnostics, "$.platform", data.get("platform"), ALLOWED_CHANNEL_PLATFORMS
    )
    _validate_channel_agent_id(diagnostics, "$.agent_id", data.get("agent_id"))
    validate_allowed_string(
        diagnostics,
        "$.dm_scope",
        data.get("dm_scope", _DEFAULT_DM_SCOPE),
        ALLOWED_CHANNEL_DM_SCOPES,
    )
    _validate_platform_id_list(diagnostics, "$.allowed_chat_ids", data.get("allowed_chat_ids", []))
    validate_non_empty_string(
        diagnostics, "$.token_env_var", data.get("token_env_var"), required=True
    )
    if "enabled" in data and not isinstance(data["enabled"], bool):
        add_error(diagnostics, "$.enabled", "must be a boolean")
    if "observe_unaddressed" in data and not isinstance(data["observe_unaddressed"], bool):
        add_error(diagnostics, "$.observe_unaddressed", "must be a boolean")
    validate_allowed_string(
        diagnostics,
        "$.response_mode",
        data.get("response_mode", _DEFAULT_RESPONSE_MODE),
        ALLOWED_CHANNEL_RESPONSE_MODES,
    )
    _validate_regex_list(diagnostics, "$.mention_patterns", data.get("mention_patterns", []))
    if "owner_user_ids" in data:
        add_error(
            diagnostics,
            "$.owner_user_ids",
            "is retired; configure group admins in channel access settings and remove this field",
        )
    return diagnostics


def _validate_channel_agent_id(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        add_error(diagnostics, path, "must be a non-empty string")
    elif not is_valid_agent_id(value.strip()):
        add_error(
            diagnostics,
            path,
            "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
        )


def _validate_platform_id_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of platform ids")
        return
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            add_error(diagnostics, f"{path}[{index}]", "must be a string or integer id")
        elif isinstance(item, str) and not item.strip():
            add_error(diagnostics, f"{path}[{index}]", "must not be empty")


def _validate_regex_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of regex strings")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(diagnostics, f"{path}[{index}]", "must be a non-empty string")
            continue
        try:
            re.compile(item)
        except re.error as error:
            add_error(diagnostics, f"{path}[{index}]", f"must be a valid regex: {error}")


@dataclass(slots=True)
class ChannelConfig:
    """Persisted channel configuration."""

    id: str
    platform: str
    agent_id: str
    dm_scope: str = _DEFAULT_DM_SCOPE
    allowed_chat_ids: list[str] = field(default_factory=list)
    token_env_var: str = ""
    enabled: bool = True
    response_mode: str = _DEFAULT_RESPONSE_MODE
    mention_patterns: list[str] = field(default_factory=list)
    observe_unaddressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize one channel config to JSON-compatible data."""
        return {
            "id": self.id,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "dm_scope": self.dm_scope,
            "allowed_chat_ids": list(self.allowed_chat_ids),
            "token_env_var": self.token_env_var,
            "enabled": self.enabled,
            "response_mode": self.response_mode,
            "mention_patterns": list(self.mention_patterns),
            "observe_unaddressed": self.observe_unaddressed,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChannelConfig:
        """Create one ChannelConfig from persisted JSON data."""
        _raise_channel_config_errors(payload)
        config = cls(
            id=payload.get("id", ""),
            platform=payload.get("platform", ""),
            agent_id=payload.get("agent_id", ""),
            dm_scope=payload.get("dm_scope", _DEFAULT_DM_SCOPE),
            allowed_chat_ids=list(payload.get("allowed_chat_ids") or []),
            token_env_var=payload.get("token_env_var", ""),
            enabled=payload.get("enabled", True),
            response_mode=payload.get("response_mode", _DEFAULT_RESPONSE_MODE),
            mention_patterns=list(payload.get("mention_patterns") or []),
            observe_unaddressed=payload.get("observe_unaddressed", False),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Validate with the persisted schema, then normalize accepted values."""
        _raise_channel_config_errors(
            {
                "id": self.id,
                "platform": self.platform,
                "agent_id": self.agent_id,
                "dm_scope": self.dm_scope,
                "allowed_chat_ids": self.allowed_chat_ids,
                "token_env_var": self.token_env_var,
                "enabled": self.enabled,
                "response_mode": self.response_mode,
                "mention_patterns": self.mention_patterns,
                "observe_unaddressed": self.observe_unaddressed,
            }
        )
        self.id = self.id.strip()
        self.agent_id = self.agent_id.strip()
        self.token_env_var = self.token_env_var.strip()
        self.allowed_chat_ids = [str(value).strip() for value in self.allowed_chat_ids]
        self.mention_patterns = list(self.mention_patterns)


def _raise_channel_config_errors(payload: Any) -> None:
    errors = [
        diagnostic
        for diagnostic in validate_channel_data(payload)
        if diagnostic.severity == "error"
    ]
    if errors:
        raise ChannelConfigError("; ".join(f"{error.path}: {error.message}" for error in errors))


def _normalize_channel_id(channel_id: Any) -> str:
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise ChannelConfigError("channel_id must be a non-empty string")
    normalized = channel_id.strip()
    # The id becomes a path segment under the channels directory, and delete recursively
    # removes that directory: a separator or traversal component (``../agents``, ``/etc``)
    # would let an operation escape storage and rmtree an arbitrary directory. Enforce the
    # same bare-slug rule ChannelConfig already requires, at the choke point every storage
    # and service call funnels through.
    if _CHANNEL_ID_PATTERN.fullmatch(normalized) is None:
        raise ChannelConfigError(
            "channel_id must contain only letters, numbers, underscore, and hyphen"
        )
    return normalized


def managed_channel_token_env_var(channel_id: str) -> str:
    """Return a collision-free, environment-safe key for a managed Channel token."""
    normalized_id = _normalize_channel_id(channel_id)
    encoded_id = normalized_id.encode("utf-8").hex().upper()
    return f"{MANAGED_CHANNEL_TOKEN_ENV_PREFIX}{encoded_id}"
