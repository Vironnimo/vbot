"""Channel configuration, storage, and lifecycle management."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from core.attachments import AttachmentStore
from core.channels.adapter import (
    ChannelAdapter,
    DeniedChatFacts,
    FileData,
    ReplyPlanFacts,
    RouteFacts,
    RunButtonBinding,
    RunButtonClaim,
    bound_run_callback_data,
)
from core.chat.messages import GroupRole, ReplySurface
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
from core.extensions import InteractionButton, InteractionEvent, InteractionResponder
from core.sessions import SessionAddress
from core.settings import is_valid_agent_id
from core.utils.atomic import atomic_write_text
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.agents.agents import AgentStore
    from core.automation.automation import TriggerService
    from core.chat.commands import CommandDispatcher
    from core.runs import Run
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels")
_CHANNEL_IO_WORKERS = BoundedWorkerPool(name="channel-io", max_workers=4)

_CHANNEL_CONFIG_FILENAME = "channel.json"
_CHANNEL_ACCESS_FILENAME = "access.json"
_CHANNEL_ACCESS_VERSION = 1
_RUN_BUTTON_BINDINGS_FILENAME = "run-button-bindings.json"
_RUN_BUTTON_BINDINGS_VERSION = 1
_POLLING_STATE_FILENAME = "polling.json"
_POLLING_STATE_VERSION = 1
_DEFAULT_DM_SCOPE = "per_conversation"
ALLOWED_CHANNEL_DM_SCOPES = frozenset(
    ("per_conversation", "main", "per_peer", "per_account_channel_peer")
)
_DEFAULT_RESPONSE_MODE = "mention"
ALLOWED_CHANNEL_RESPONSE_MODES = frozenset(("mention", "all"))
ALLOWED_CHANNEL_PLATFORMS = frozenset(("discord", "telegram"))
MANAGED_CHANNEL_TOKEN_ENV_PREFIX = "VBOT_CHANNEL_TOKEN__"
_CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ADAPTER_RESTART_INITIAL_DELAY_SECONDS = 1.0
_ADAPTER_RESTART_MAX_DELAY_SECONDS = 30.0
# Fast-retry budget before a channel is marked failed. Exhausting it does NOT
# stop recovery: the channel keeps retrying at the capped backoff interval for
# as long as it is enabled — a channel is the operator's lifeline (e.g. Telegram
# control of the whole server), so a transient network blip at startup must
# never require manual intervention.
_ADAPTER_RESTART_MAX_RETRIES = 3
# An adapter that stayed up at least this long was healthy: its next crash
# starts a fresh restart cycle instead of counting toward chronic failure.
_ADAPTER_HEALTHY_RUN_RESET_SECONDS = 300.0
# Telegram caps an inline button's callback_data at 64 UTF-8 bytes; validated on
# send so an over-long payload fails cleanly rather than at the Bot API.
_MAX_CALLBACK_DATA_BYTES = 64
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
    validate_non_empty_string(diagnostics, "$.id", data.get("id"), required=True)
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
    _validate_user_id_list(diagnostics, "$.owner_user_ids", data.get("owner_user_ids", []))
    return diagnostics


def _validate_channel_agent_id(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        add_error(diagnostics, path, "must be a non-empty string")
    elif not is_valid_agent_id(value):
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


def _validate_user_id_list(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, list):
        add_error(diagnostics, path, "must be a list of platform user ids")
        return
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            add_error(diagnostics, f"{path}[{index}]", "must be a string or integer user id")
        elif isinstance(item, str) and not item.strip():
            add_error(diagnostics, f"{path}[{index}]", "must not be empty")


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
    # Legacy read-only migration input. New configs never persist or mutate this field.
    owner_user_ids: list[str] = field(default_factory=list, repr=False)
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
            owner_user_ids=list(payload.get("owner_user_ids") or []),
            observe_unaddressed=payload.get("observe_unaddressed", False),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Validate and normalize one channel config in-place."""
        if not isinstance(self.id, str) or not self.id.strip():
            raise ChannelConfigError("id must be a non-empty string")
        self.id = self.id.strip()
        if _CHANNEL_ID_PATTERN.fullmatch(self.id) is None:
            raise ChannelConfigError(
                "id must contain only letters, numbers, underscore, and hyphen"
            )

        if not isinstance(self.platform, str) or self.platform not in ALLOWED_CHANNEL_PLATFORMS:
            platforms = ", ".join(sorted(ALLOWED_CHANNEL_PLATFORMS))
            raise ChannelConfigError(f"platform must be one of: {platforms}")

        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ChannelConfigError("agent_id must be a non-empty string")
        self.agent_id = self.agent_id.strip()

        if not isinstance(self.dm_scope, str) or self.dm_scope not in ALLOWED_CHANNEL_DM_SCOPES:
            scopes = ", ".join(sorted(ALLOWED_CHANNEL_DM_SCOPES))
            raise ChannelConfigError(f"dm_scope must be one of: {scopes}")

        if not isinstance(self.allowed_chat_ids, list):
            raise ChannelConfigError("allowed_chat_ids must be a list of platform ids")
        normalized_chat_ids: list[str] = []
        for chat_id in self.allowed_chat_ids:
            if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
                raise ChannelConfigError("allowed_chat_ids must contain strings or integers only")
            normalized_chat_id = str(chat_id).strip()
            if not normalized_chat_id:
                raise ChannelConfigError("allowed_chat_ids must not contain empty values")
            normalized_chat_ids.append(normalized_chat_id)
        self.allowed_chat_ids = normalized_chat_ids

        if not isinstance(self.token_env_var, str) or not self.token_env_var.strip():
            raise ChannelConfigError("token_env_var must be a non-empty string")
        self.token_env_var = self.token_env_var.strip()

        if not isinstance(self.enabled, bool):
            raise ChannelConfigError("enabled must be a boolean")

        if not isinstance(self.observe_unaddressed, bool):
            raise ChannelConfigError("observe_unaddressed must be a boolean")

        if not isinstance(self.response_mode, str) or self.response_mode not in (
            ALLOWED_CHANNEL_RESPONSE_MODES
        ):
            modes = ", ".join(sorted(ALLOWED_CHANNEL_RESPONSE_MODES))
            raise ChannelConfigError(f"response_mode must be one of: {modes}")

        if not isinstance(self.mention_patterns, list):
            raise ChannelConfigError("mention_patterns must be a list of regex strings")
        normalized_patterns: list[str] = []
        for pattern in self.mention_patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ChannelConfigError("mention_patterns must contain non-empty strings only")
            try:
                re.compile(pattern)
            except re.error as error:
                raise ChannelConfigError(
                    f"mention_patterns contains an invalid regex {pattern!r}: {error}"
                ) from error
            normalized_patterns.append(pattern)
        self.mention_patterns = normalized_patterns

        # Platform user ids are strings end-to-end (Telegram ids are numeric, Discord
        # snowflakes are not); integers are accepted and normalized for convenience.
        if not isinstance(self.owner_user_ids, list):
            raise ChannelConfigError("owner_user_ids must be a list of platform user ids")
        normalized_owner_ids: list[str] = []
        for owner_user_id in self.owner_user_ids:
            if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, (int, str)):
                raise ChannelConfigError("owner_user_ids must contain strings or integers only")
            normalized_owner_id = str(owner_user_id).strip()
            if not normalized_owner_id:
                raise ChannelConfigError("owner_user_ids must not contain empty values")
            normalized_owner_ids.append(normalized_owner_id)
        self.owner_user_ids = normalized_owner_ids


class ChannelStorage:
    """Persist channel configs under <data_root>/channels/<id>/channel.json."""

    def __init__(self, data_root: str | Path) -> None:
        self._data_root = Path(data_root).expanduser()
        self._channels_dir = self._data_root / "channels"
        self._run_button_bindings_lock = threading.RLock()
        self._update_offset_lock = threading.RLock()
        self._access_lock = threading.RLock()

    def load_all(self) -> list[ChannelConfig]:
        """Load all valid persisted channel configs in stable id-order.

        A config that fails to parse or validate is skipped with a logged warning rather
        than aborting the whole load: one corrupt ``channel.json`` must not block server
        startup or hide every other channel. Strict single-channel access stays in ``get``.
        """
        if not self._channels_dir.exists():
            return []

        configs: list[ChannelConfig] = []
        try:
            channel_directories = sorted(
                self._channels_dir.iterdir(),
                key=lambda path: path.name,
            )
        except OSError as error:
            _LOGGER.warning("Cannot scan Channel configs in %s: %s", self._channels_dir, error)
            return []

        for channel_dir in channel_directories:
            if not channel_dir.is_dir():
                continue
            config_path = channel_dir / _CHANNEL_CONFIG_FILENAME
            if not config_path.is_file():
                continue
            try:
                configs.append(self._migrate_legacy_owner_ids(self._read_config(config_path)))
            except ChannelError as error:
                _LOGGER.warning("Skipping invalid channel config %s: %s", config_path, error)

        return sorted(configs, key=lambda config: config.id)

    def save(self, config: ChannelConfig) -> None:
        """Persist one channel config using atomic replace."""
        if not isinstance(config, ChannelConfig):
            raise ChannelConfigError("config must be a ChannelConfig instance")
        config.validate()

        channel_dir = self._channel_dir(config.id)
        config_path = channel_dir / _CHANNEL_CONFIG_FILENAME
        serialized = (
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        try:
            atomic_write_text(config_path, serialized)
        except OSError as error:
            raise ChannelError(f"Cannot write {config_path}: {error}") from error

    def delete(self, channel_id: str) -> None:
        """Delete one channel directory from storage."""
        normalized_id = _normalize_channel_id(channel_id)
        channel_dir = self._channel_dir(normalized_id)
        if not channel_dir.exists():
            raise ChannelNotFoundError(f"Channel not found: {normalized_id}")
        if not channel_dir.is_dir():
            raise ChannelError(f"Channel path is not a directory: {channel_dir}")
        try:
            shutil.rmtree(channel_dir)
        except OSError as error:
            raise ChannelError(f"Cannot delete channel directory {channel_dir}: {error}") from error

    def get(self, channel_id: str) -> ChannelConfig:
        """Load one channel config by id."""
        normalized_id = _normalize_channel_id(channel_id)
        config_path = self._channel_dir(normalized_id) / _CHANNEL_CONFIG_FILENAME
        if not config_path.is_file():
            raise ChannelNotFoundError(f"Channel not found: {normalized_id}")
        return self._migrate_legacy_owner_ids(self._read_config(config_path))

    def access_state(self, channel_id: str) -> JsonObject:
        """Return the saved own identity and per-group participant/admin state."""
        normalized_id = _normalize_channel_id(channel_id)
        self.get(normalized_id)
        with self._access_lock:
            state = self._load_access_state(normalized_id)
            return self._public_access_state(normalized_id, state)

    def snapshot_participant_role(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
        display_name: str,
    ) -> GroupRole:
        """Persist one seen participant and return its role in the same lock."""
        normalized_id = _normalize_channel_id(channel_id)
        scope_id = _normalize_platform_access_id(access_scope_id, "access_scope_id")
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")
        normalized_display_name = (
            display_name.strip() if isinstance(display_name, str) else normalized_user_id
        )
        if not normalized_display_name:
            normalized_display_name = normalized_user_id
        with self._access_lock:
            state = self._load_access_state(normalized_id)
            group = self._access_group(state, scope_id)
            participants = cast(dict[str, JsonObject], group["participants"])
            participants[normalized_user_id] = {
                "display_name": normalized_display_name,
                "last_seen_at": datetime.now(UTC).isoformat(),
            }
            role = self._role_from_state(state, group, normalized_user_id)
            self._write_access_state(normalized_id, state)
            return role

    def role_for(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> GroupRole:
        """Resolve the current role without changing participant history."""
        normalized_id = _normalize_channel_id(channel_id)
        scope_id = _normalize_platform_access_id(access_scope_id, "access_scope_id")
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")
        with self._access_lock:
            state = self._load_access_state(normalized_id)
            group = self._access_group(state, scope_id)
            return self._role_from_state(state, group, normalized_user_id)

    def set_self_user_id(self, channel_id: str, user_id: str) -> JsonObject:
        """Set the Channel account's own identity from its durable participants."""
        normalized_id = _normalize_channel_id(channel_id)
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")
        self.get(normalized_id)
        with self._access_lock:
            state = self._load_access_state(normalized_id)
            groups = cast(dict[str, JsonObject], state["groups"])
            seen = any(
                normalized_user_id in cast(dict[str, JsonObject], group.get("participants", {}))
                for group in groups.values()
            )
            if not seen:
                raise ChannelConfigError(
                    f"Channel participant has not been seen: {normalized_user_id}"
                )
            state["self_user_id"] = normalized_user_id
            self._write_access_state(normalized_id, state)
            return self._public_access_state(normalized_id, state)

    def grant_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Add one user to one group's additional admin set, idempotently."""
        normalized_id = _normalize_channel_id(channel_id)
        scope_id = _normalize_platform_access_id(access_scope_id, "access_scope_id")
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")
        self.get(normalized_id)
        with self._access_lock:
            state = self._load_access_state(normalized_id)
            group = self._access_group(state, scope_id)
            admins = cast(list[str], group["admin_user_ids"])
            if normalized_user_id not in admins:
                admins.append(normalized_user_id)
                admins.sort()
                self._write_access_state(normalized_id, state)
            return self._public_access_state(normalized_id, state)

    def revoke_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Remove one additional admin; the configured own identity stays admin."""
        normalized_id = _normalize_channel_id(channel_id)
        scope_id = _normalize_platform_access_id(access_scope_id, "access_scope_id")
        normalized_user_id = _normalize_platform_access_id(user_id, "user_id")
        self.get(normalized_id)
        with self._access_lock:
            state = self._load_access_state(normalized_id)
            group = self._access_group(state, scope_id)
            admins = cast(list[str], group["admin_user_ids"])
            if normalized_user_id in admins:
                admins.remove(normalized_user_id)
                self._write_access_state(normalized_id, state)
            return self._public_access_state(normalized_id, state)

    def migrate_group_access(
        self,
        channel_id: str,
        old_access_scope_id: str,
        new_access_scope_id: str,
    ) -> None:
        """Merge and move durable group access state after a platform migration."""
        normalized_id = _normalize_channel_id(channel_id)
        old_scope_id = _normalize_platform_access_id(old_access_scope_id, "old_access_scope_id")
        new_scope_id = _normalize_platform_access_id(new_access_scope_id, "new_access_scope_id")
        if old_scope_id == new_scope_id:
            return
        with self._access_lock:
            state = self._load_access_state(normalized_id)
            groups = cast(dict[str, JsonObject], state["groups"])
            old_group = groups.pop(old_scope_id, None)
            if old_group is None:
                return
            new_group = self._access_group(state, new_scope_id)
            new_admins = cast(list[str], new_group["admin_user_ids"])
            for user_id in cast(list[str], old_group.get("admin_user_ids", [])):
                if user_id not in new_admins:
                    new_admins.append(user_id)
            new_admins.sort()
            new_participants = cast(dict[str, JsonObject], new_group["participants"])
            for user_id, participant in cast(
                dict[str, JsonObject], old_group.get("participants", {})
            ).items():
                current = new_participants.get(user_id)
                if current is None or str(participant.get("last_seen_at", "")) > str(
                    current.get("last_seen_at", "")
                ):
                    new_participants[user_id] = dict(participant)
            self._write_access_state(normalized_id, state)

    def save_run_button_binding(self, channel_id: str, binding: RunButtonBinding) -> None:
        """Persist one pending origin binding before its Telegram message is sent."""
        normalized_id = _normalize_channel_id(channel_id)
        with self._run_button_bindings_lock:
            bindings = self._load_run_button_bindings(normalized_id)
            bindings[binding.id] = binding
            self._write_run_button_bindings(normalized_id, bindings)

    def discard_run_button_binding(self, channel_id: str, binding_id: str) -> None:
        """Remove a binding whose outbound platform send failed."""
        normalized_id = _normalize_channel_id(channel_id)
        with self._run_button_bindings_lock:
            bindings = self._load_run_button_bindings(normalized_id)
            if bindings.pop(binding_id, None) is not None:
                self._write_run_button_bindings(normalized_id, bindings)

    def claim_run_button_binding(
        self,
        channel_id: str,
        binding_id: str,
        *,
        platform_target: str,
        thread_id: str | None,
    ) -> RunButtonClaim:
        """Atomically consume a binding when its original target taps a Run button."""
        normalized_id = _normalize_channel_id(channel_id)
        with self._run_button_bindings_lock:
            bindings = self._load_run_button_bindings(normalized_id)
            binding = bindings.get(binding_id)
            if binding is None:
                return RunButtonClaim(status="missing")
            if binding.platform_target != platform_target or binding.thread_id != thread_id:
                return RunButtonClaim(status="target_mismatch", binding=binding)
            if binding.consumed:
                return RunButtonClaim(status="consumed", binding=binding)
            claimed = replace(binding, consumed=True)
            bindings[binding_id] = claimed
            self._write_run_button_bindings(normalized_id, bindings)
            return RunButtonClaim(status="claimed", binding=claimed)

    def restore_run_button_binding(self, channel_id: str, binding_id: str) -> None:
        """Make a claimed binding retryable when Queue admission was rejected."""
        normalized_id = _normalize_channel_id(channel_id)
        with self._run_button_bindings_lock:
            bindings = self._load_run_button_bindings(normalized_id)
            binding = bindings.get(binding_id)
            if binding is None or not binding.consumed:
                return
            bindings[binding_id] = replace(binding, consumed=False)
            self._write_run_button_bindings(normalized_id, bindings)

    def load_update_offset(self, channel_id: str) -> int:
        """Return the persisted Telegram update high-water mark (0 when unknown)."""
        normalized_id = _normalize_channel_id(channel_id)
        path = self._channel_dir(normalized_id) / _POLLING_STATE_FILENAME
        if not path.is_file():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _POLLING_STATE_VERSION:
                raise ValueError("unsupported polling-state version")
            update_id = payload.get("last_update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool) and update_id >= 0:
                return update_id
            raise ValueError("last_update_id must be a non-negative integer")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            # A corrupt tiny state file must not brick the channel; degrading to
            # an empty watermark re-delivers recent messages instead of losing them.
            _LOGGER.warning(
                "Cannot read Telegram polling state for %s, treating as empty: %s",
                channel_id,
                error,
            )
            return 0

    def save_update_offset(self, channel_id: str, update_id: int) -> None:
        """Persist the Telegram update high-water mark with atomic replace."""
        normalized_id = _normalize_channel_id(channel_id)
        path = self._channel_dir(normalized_id) / _POLLING_STATE_FILENAME
        payload = {
            "version": _POLLING_STATE_VERSION,
            "last_update_id": int(update_id),
        }
        with self._update_offset_lock:
            try:
                atomic_write_text(
                    path,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                )
            except OSError as error:
                raise ChannelError(
                    f"Cannot write Telegram polling state for {channel_id}: {error}"
                ) from error

    def _channel_dir(self, channel_id: str) -> Path:
        return self._channels_dir / channel_id

    def _migrate_legacy_owner_ids(self, config: ChannelConfig) -> ChannelConfig:
        if not config.owner_user_ids:
            return config
        with self._access_lock:
            state = self._load_access_state(config.id)
            changed = False
            for access_scope_id in config.allowed_chat_ids:
                group = self._access_group(state, access_scope_id)
                admins = cast(list[str], group["admin_user_ids"])
                for user_id in config.owner_user_ids:
                    if user_id not in admins:
                        admins.append(user_id)
                        changed = True
                admins.sort()
            if changed:
                self._write_access_state(config.id, state)
            migrated = replace(config, owner_user_ids=[])
            self.save(migrated)
            return migrated

    def _load_access_state(self, channel_id: str) -> JsonObject:
        path = self._channel_dir(channel_id) / _CHANNEL_ACCESS_FILENAME
        if not path.is_file():
            return {
                "version": _CHANNEL_ACCESS_VERSION,
                "self_user_id": None,
                "groups": {},
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _CHANNEL_ACCESS_VERSION:
                raise ValueError("unsupported access-store version")
            self_user_id = payload.get("self_user_id")
            if self_user_id is not None and (not isinstance(self_user_id, str) or not self_user_id):
                raise ValueError("self_user_id must be a non-empty string or null")
            raw_groups = payload.get("groups")
            if not isinstance(raw_groups, dict):
                raise ValueError("groups must be an object")
            groups: dict[str, JsonObject] = {}
            for scope_id, raw_group in raw_groups.items():
                if not isinstance(scope_id, str) or not scope_id or not isinstance(raw_group, dict):
                    raise ValueError("group entries must be keyed objects")
                raw_admins = raw_group.get("admin_user_ids", [])
                raw_participants = raw_group.get("participants", {})
                if not isinstance(raw_admins, list) or not all(
                    isinstance(user_id, str) and user_id for user_id in raw_admins
                ):
                    raise ValueError("admin_user_ids must be a list of non-empty strings")
                if not isinstance(raw_participants, dict):
                    raise ValueError("participants must be an object")
                participants: dict[str, JsonObject] = {}
                for user_id, raw_participant in raw_participants.items():
                    if (
                        not isinstance(user_id, str)
                        or not user_id
                        or not isinstance(raw_participant, dict)
                    ):
                        raise ValueError("participant entries must be keyed objects")
                    display_name = raw_participant.get("display_name")
                    last_seen_at = raw_participant.get("last_seen_at")
                    if not isinstance(display_name, str) or not display_name:
                        raise ValueError("participant display_name must be non-empty")
                    if not isinstance(last_seen_at, str) or not last_seen_at:
                        raise ValueError("participant last_seen_at must be non-empty")
                    participants[user_id] = {
                        "display_name": display_name,
                        "last_seen_at": last_seen_at,
                    }
                groups[scope_id] = {
                    "admin_user_ids": sorted(set(raw_admins)),
                    "participants": participants,
                }
            return {
                "version": _CHANNEL_ACCESS_VERSION,
                "self_user_id": self_user_id,
                "groups": groups,
            }
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise ChannelError(
                f"Cannot read Channel access state for {channel_id}: {error}"
            ) from error

    def _write_access_state(self, channel_id: str, state: JsonObject) -> None:
        path = self._channel_dir(channel_id) / _CHANNEL_ACCESS_FILENAME
        try:
            atomic_write_text(
                path,
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        except OSError as error:
            raise ChannelError(
                f"Cannot write Channel access state for {channel_id}: {error}"
            ) from error

    @staticmethod
    def _access_group(state: JsonObject, access_scope_id: str) -> JsonObject:
        groups = cast(dict[str, JsonObject], state["groups"])
        group = groups.get(access_scope_id)
        if group is None:
            group = {"admin_user_ids": [], "participants": {}}
            groups[access_scope_id] = group
        return group

    @staticmethod
    def _role_from_state(
        state: JsonObject,
        group: JsonObject,
        user_id: str,
    ) -> GroupRole:
        if user_id == state.get("self_user_id"):
            return "admin"
        return "admin" if user_id in cast(list[str], group["admin_user_ids"]) else "member"

    def _public_access_state(self, channel_id: str, state: JsonObject) -> JsonObject:
        self_user_id = cast(str | None, state.get("self_user_id"))
        groups_payload: list[JsonObject] = []
        for access_scope_id, group in sorted(cast(dict[str, JsonObject], state["groups"]).items()):
            participants = cast(dict[str, JsonObject], group["participants"])
            participant_payload = [
                {
                    "user_id": user_id,
                    "display_name": participant["display_name"],
                    "last_seen_at": participant["last_seen_at"],
                    "role": self._role_from_state(state, group, user_id),
                }
                for user_id, participant in sorted(participants.items())
            ]
            admin_ids = set(cast(list[str], group["admin_user_ids"]))
            if self_user_id is not None:
                admin_ids.add(self_user_id)
            groups_payload.append(
                {
                    "access_scope_id": access_scope_id,
                    "admin_user_ids": sorted(admin_ids),
                    "participants": participant_payload,
                }
            )
        return {
            "channel_id": channel_id,
            "self_user_id": self_user_id,
            "groups": groups_payload,
        }

    def _load_run_button_bindings(self, channel_id: str) -> dict[str, RunButtonBinding]:
        path = self._channel_dir(channel_id) / _RUN_BUTTON_BINDINGS_FILENAME
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != _RUN_BUTTON_BINDINGS_VERSION
            ):
                raise ValueError("unsupported binding-store version")
            raw_bindings = payload.get("bindings")
            if not isinstance(raw_bindings, dict):
                raise ValueError("bindings must be an object")
            return {
                binding_id: _run_button_binding_from_dict(binding_id, raw_binding)
                for binding_id, raw_binding in raw_bindings.items()
                if isinstance(binding_id, str)
            }
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise ChannelError(
                f"Cannot read Run-button bindings for {channel_id}: {error}"
            ) from error

    def _write_run_button_bindings(
        self,
        channel_id: str,
        bindings: dict[str, RunButtonBinding],
    ) -> None:
        path = self._channel_dir(channel_id) / _RUN_BUTTON_BINDINGS_FILENAME
        payload = {
            "version": _RUN_BUTTON_BINDINGS_VERSION,
            "bindings": {
                binding_id: _run_button_binding_to_dict(binding)
                for binding_id, binding in sorted(bindings.items())
            },
        }
        try:
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        except OSError as error:
            raise ChannelError(
                f"Cannot write Run-button bindings for {channel_id}: {error}"
            ) from error

    def _read_config(self, config_path: Path) -> ChannelConfig:
        payload = load_validated_channel_json(config_path)
        config = ChannelConfig.from_dict(payload)
        if config.id != config_path.parent.name:
            raise ChannelConfigError(
                "Channel id mismatch for "
                f"{config_path}: expected {config_path.parent.name}, got {config.id}"
            )
        return config


class ChannelService:
    """Manage channel config CRUD and adapter task lifecycle."""

    def __init__(
        self,
        trigger_service: TriggerService,
        chat_sessions: ChatSessionManager,
        *,
        agent_store: AgentStore,
        data_root: str | Path,
        credential_resolver: Callable[[str], str],
        attachment_store: AttachmentStore | None = None,
        command_dispatcher: CommandDispatcher,
        interaction_dispatcher: (
            Callable[[InteractionEvent, InteractionResponder], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        self._trigger_service = trigger_service
        self._chat_sessions = chat_sessions
        self._agent_store = agent_store
        self._credential_resolver = credential_resolver
        self._attachment_store = attachment_store
        self._command_dispatcher = command_dispatcher
        self._interaction_dispatcher = interaction_dispatcher
        self._storage = ChannelStorage(Path(data_root))
        self._adapters: dict[str, ChannelAdapter] = {}
        self._adapter_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_task_created: dict[str, float] = {}
        self._adapter_stop_tasks: dict[str, asyncio.Task[None]] = {}
        self._adapter_restart_attempts: dict[str, int] = {}
        self._adapter_restart_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_start_requests: dict[str, tuple[bool, ChannelConfig | None]] = {}
        self._failed_channels: set[str] = set()
        self._failure_reasons: dict[str, str] = {}
        self._started = False
        self._notify_tool_registration_changed_hook: Callable[[], None] = lambda: None

    def start(self) -> None:
        """Start the channel service and launch enabled channel adapter tasks."""
        if self._started:
            return

        self._started = True
        for config in self._storage.load_all():
            if config.enabled:
                try:
                    self.start_channel(config.id)
                except Exception as error:
                    reason = str(error) or type(error).__name__
                    self._mark_channel_failed(config.id, reason)
                    _LOGGER.error(
                        "Cannot start channel adapter during service startup (channel=%s): %s",
                        config.id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )

    def stop(self) -> None:
        """Stop all active channel adapter tasks. Idempotent."""
        if (
            not self._started
            and not self._adapter_tasks
            and not self._adapter_restart_tasks
            and not self._adapter_stop_tasks
        ):
            return

        self._pending_start_requests.clear()
        for channel_id in list(self._adapter_restart_tasks):
            self._cancel_restart_task(channel_id)
        for channel_id in list(self._adapter_tasks):
            self.stop_channel(channel_id)
        self._adapter_restart_attempts.clear()
        self._adapter_task_created.clear()
        self._failed_channels.clear()
        self._failure_reasons.clear()
        self._started = False

    async def aclose(self) -> None:
        """Stop all channel tasks and await their cancellation/shutdown paths."""
        tasks = [*self._adapter_stop_tasks.values(), *self._adapter_restart_tasks.values()]
        self.stop()
        tasks.extend(self._adapter_stop_tasks.values())

        pending_tasks = _unique_pending_tasks(tasks)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    def start_channel(
        self,
        channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        """Start one enabled channel adapter task when not already running."""
        normalized_id = _normalize_channel_id(channel_id)

        if config_override is not None:
            if config_override.id != normalized_id:
                raise ChannelConfigError(
                    f"config_override.id mismatch: {config_override.id} != {normalized_id}"
                )
            config = replace(config_override)
            config.validate()
        else:
            config = self._storage.get(normalized_id)

        self._validate_agent_exists(config.agent_id)
        self._cancel_restart_task(normalized_id)
        if reset_backoff:
            self._adapter_restart_attempts.pop(normalized_id, None)
            self._failed_channels.discard(normalized_id)
            self._failure_reasons.pop(normalized_id, None)

        if self._is_stop_in_progress(normalized_id):
            self._schedule_pending_start(
                normalized_id,
                reset_backoff=reset_backoff,
                config_override=config,
            )
            return

        existing_task = self._adapter_tasks.get(normalized_id)
        if existing_task is not None and not existing_task.done():
            return
        if existing_task is not None and existing_task.done():
            self._adapter_tasks.pop(normalized_id, None)
            self._adapters.pop(normalized_id, None)

        if not config.enabled:
            return

        loop = _get_running_loop_or_none()
        if loop is None:
            _LOGGER.warning(
                "Cannot start channel adapter without a running event loop (channel=%s)",
                normalized_id,
            )
            return

        adapter = self._create_adapter(config)
        task = loop.create_task(
            self._run_adapter(normalized_id, adapter), name=f"channel:{normalized_id}"
        )
        self._adapters[normalized_id] = adapter
        self._adapter_tasks[normalized_id] = task
        self._adapter_task_created[normalized_id] = time.monotonic()

        def on_done(completed_task: asyncio.Task[None], channel: str = normalized_id) -> None:
            self._on_adapter_task_done(channel, completed_task)

        task.add_done_callback(on_done)

    def stop_channel(self, channel_id: str) -> None:
        """Stop one running channel adapter task when active."""
        normalized_id = _normalize_channel_id(channel_id)

        self._pending_start_requests.pop(normalized_id, None)
        self._cancel_restart_task(normalized_id)
        self._adapter_restart_attempts.pop(normalized_id, None)
        self._failed_channels.discard(normalized_id)
        self._failure_reasons.pop(normalized_id, None)

        task = self._adapter_tasks.pop(normalized_id, None)
        self._adapters.pop(normalized_id, None)
        self._adapter_task_created.pop(normalized_id, None)

        if task is not None and not task.done():
            task.cancel()
            loop = _get_running_loop_or_none()
            if loop is not None:
                stop_task = loop.create_task(
                    self._await_adapter_shutdown(normalized_id, task),
                    name=f"channel:{normalized_id}:stop",
                )
                self._adapter_stop_tasks[normalized_id] = stop_task

                def on_stop_done(
                    completed_task: asyncio.Task[None],
                    channel: str = normalized_id,
                ) -> None:
                    self._on_stop_task_done(channel, completed_task)

                stop_task.add_done_callback(on_stop_done)

    def restart_channel(self, channel_id: str) -> bool:
        """Rebuild one enabled channel adapter from its current config and credentials.

        Returns whether the channel is enabled and therefore received a start
        request. Disabled channels keep the updated credential for their next
        normal enable without creating an adapter.
        """
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        self._validate_agent_exists(config.agent_id)
        self._preflight_adapter_start(config)

        if not config.enabled:
            return False

        self.stop_channel(normalized_id)
        self.start_channel(normalized_id, config_override=config)
        return True

    async def send(
        self,
        channel_id: str,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
        run_origin: RouteFacts | None = None,
    ) -> None:
        """Delegate an outbound send to a running channel adapter."""
        normalized_id = _normalize_channel_id(channel_id)
        if not isinstance(platform_target, str) or not platform_target:
            raise ChannelConfigError("platform_target must be a non-empty string")
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
            raise ChannelConfigError("thread_id must be a non-empty string when provided")

        normalized_message: str | None
        if message is None:
            normalized_message = None
        elif isinstance(message, str) and message.strip():
            normalized_message = message.strip()
        else:
            raise ChannelConfigError("message must be a non-empty string when provided")

        normalized_files: list[FileData] | None
        if files is None:
            normalized_files = None
        elif not isinstance(files, list):
            raise ChannelConfigError("files must be a list of FileData when provided")
        else:
            normalized_files = []
            for file_data in files:
                if not isinstance(file_data, FileData):
                    raise ChannelConfigError("files must contain FileData values only")
                normalized_files.append(file_data)

        normalized_buttons = _normalize_outbound_buttons(buttons)
        if run_origin is not None and not isinstance(run_origin, RouteFacts):
            raise ChannelConfigError("run_origin must be RouteFacts when provided")

        if normalized_message is None and not normalized_files:
            raise ChannelConfigError("at least one of message or files must be provided")

        adapter, outbound_buttons, binding = await _CHANNEL_IO_WORKERS.run(
            self._prepare_outbound_dispatch,
            normalized_id,
            normalized_buttons,
            platform_target,
            thread_id,
            run_origin,
        )

        try:
            await adapter.send(
                normalized_message,
                platform_target,
                files=normalized_files,
                thread_id=thread_id,
                buttons=outbound_buttons,
            )
        except BaseException:
            if binding is not None:
                try:
                    await _CHANNEL_IO_WORKERS.run(
                        self._storage.discard_run_button_binding,
                        normalized_id,
                        binding.id,
                    )
                except Exception as cleanup_error:
                    _LOGGER.warning(
                        "Could not discard unsent Run-button binding (channel=%s): %s",
                        normalized_id,
                        cleanup_error,
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
            raise

    async def relay_completion_run(self, run: Run, reply_surface: ReplySurface) -> None:
        """Relay a background completion Run to its Session's latest Channel target."""
        if reply_surface.kind != "channel" or reply_surface.channel_id is None:
            return
        address = SessionAddress(
            project_id=run.project_id,
            agent_id=run.agent_id,
            session_id=run.session_id,
        )
        metadata = await self._chat_sessions.get_metadata_async(address)
        raw_target = metadata.get("last_reply_target")
        if not isinstance(raw_target, dict):
            raise ChannelConfigError(f"Session has no Channel reply target: {run.session_id}")
        channel_id = raw_target.get("channel_id")
        platform_target = raw_target.get("platform_target")
        thread_id = raw_target.get("thread_id")
        if channel_id != reply_surface.channel_id:
            raise ChannelConfigError(
                f"Session reply target does not match Channel {reply_surface.channel_id}"
            )
        if not isinstance(platform_target, str) or not platform_target:
            raise ChannelConfigError("Session Channel reply target is invalid")
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
            raise ChannelConfigError("Session Channel thread target is invalid")

        config = self._storage.get(channel_id)
        if config.agent_id != run.agent_id or config.platform != reply_surface.platform:
            raise ChannelConfigError(
                f"Session reply surface no longer matches Channel {channel_id}"
            )
        adapter = self._active_adapter(channel_id)
        await adapter.relay_run(
            run,
            ReplyPlanFacts(
                channel_id=channel_id,
                platform_target=platform_target,
                thread_id=thread_id,
            ),
        )

    def _prepare_outbound_dispatch(
        self,
        channel_id: str,
        buttons: list[list[InteractionButton]] | None,
        platform_target: str,
        thread_id: str | None,
        run_origin: RouteFacts | None,
    ) -> tuple[
        ChannelAdapter,
        list[list[InteractionButton]] | None,
        RunButtonBinding | None,
    ]:
        adapter = self._active_adapter(channel_id)
        binding: RunButtonBinding | None = None
        outbound_buttons = buttons
        if run_origin is not None and buttons is not None:
            config = self._storage.get(channel_id)
            if run_origin.agent_id != config.agent_id:
                raise ChannelConfigError(
                    f"Run-button origin agent {run_origin.agent_id} does not own Channel "
                    f"{channel_id}"
                )
            if not self._chat_sessions.exists(
                SessionAddress(
                    project_id=None, agent_id=run_origin.agent_id, session_id=run_origin.session_id
                )
            ):
                raise ChannelConfigError(
                    f"Run-button origin Session does not exist: {run_origin.session_id}"
                )
            outbound_buttons, binding = _bind_outbound_run_buttons(
                buttons,
                platform_target=platform_target,
                thread_id=thread_id,
                origin_session_id=run_origin.session_id,
            )
            if binding is not None:
                self._storage.save_run_button_binding(channel_id, binding)
        return adapter, outbound_buttons, binding

    def ensure_outbound_session(self, channel_id: str, platform_target: str) -> RouteFacts:
        """Ensure the Session mirroring an outbound target chat exists and return its route."""
        normalized_id = _normalize_channel_id(channel_id)
        if not isinstance(platform_target, str) or not platform_target:
            raise ChannelConfigError("platform_target must be a non-empty string")
        adapter = self._active_adapter(normalized_id)
        return adapter.ensure_outbound_session(platform_target)

    def list_channels(self) -> list[ChannelConfig]:
        """Return all persisted channels, enabled and disabled."""
        return self._storage.load_all()

    def channel_access(self, channel_id: str) -> JsonObject:
        """Return one Channel's durable identity and per-group access state."""
        return self._storage.access_state(channel_id)

    def set_channel_self_user_id(self, channel_id: str, user_id: str) -> JsonObject:
        """Set and return one Channel account's own platform identity."""
        return self._storage.set_self_user_id(channel_id, user_id)

    def grant_channel_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Grant one user admin access in one group and return saved state."""
        return self._storage.grant_group_admin(channel_id, access_scope_id, user_id)

    def revoke_channel_group_admin(
        self,
        channel_id: str,
        access_scope_id: str,
        user_id: str,
    ) -> JsonObject:
        """Revoke one additional group admin and return saved state."""
        return self._storage.revoke_group_admin(channel_id, access_scope_id, user_id)

    def create_channel(self, config: ChannelConfig) -> None:
        """Validate and persist one channel config, then start it when enabled."""
        if not isinstance(config, ChannelConfig):
            raise ChannelConfigError("config must be a ChannelConfig instance")
        config.validate()
        self._validate_agent_exists(config.agent_id)
        had_enabled_channels = self.has_enabled_channels()

        try:
            self._storage.get(config.id)
        except ChannelNotFoundError:
            pass
        else:
            raise ChannelConfigError(f"Channel already exists: {config.id}")

        self._preflight_adapter_start(config)
        self._storage.save(config)
        if config.enabled:
            try:
                self.start_channel(config.id, config_override=config)
            except Exception:
                self._rollback_created_channel(config.id)
                raise
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def update_channel(self, channel_id: str, **fields: Any) -> None:
        """Update mutable fields, persist, and restart when currently running."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)

        unknown_fields = sorted(set(fields) - _MUTABLE_FIELDS)
        if unknown_fields:
            joined = ", ".join(unknown_fields)
            raise ChannelConfigError(f"Unsupported channel fields: {joined}")
        if not fields:
            return

        had_enabled_channels = self.has_enabled_channels()
        updated = replace(config, **fields)
        updated.validate()
        self._validate_agent_exists(updated.agent_id)
        self._preflight_adapter_start(updated)

        was_running = self._is_running(normalized_id) or self._is_stop_in_progress(normalized_id)

        if was_running:
            self.stop_channel(normalized_id)

        self._storage.save(updated)
        try:
            if updated.enabled:
                self.start_channel(normalized_id, config_override=updated)
            else:
                self._pending_start_requests.pop(normalized_id, None)
        except Exception:
            self._rollback_updated_channel(normalized_id, config, was_running)
            raise
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def delete_channel(self, channel_id: str) -> None:
        """Delete one channel config and stop any active adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        had_enabled_channels = self.has_enabled_channels()
        self.stop_channel(normalized_id)
        self._pending_start_requests.pop(normalized_id, None)
        self._storage.delete(normalized_id)
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def enable_channel(self, channel_id: str) -> None:
        """Enable one channel and start its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        self._validate_agent_exists(config.agent_id)
        had_enabled_channels = self.has_enabled_channels()
        if not config.enabled:
            self._storage.save(replace(config, enabled=True))
        try:
            self.start_channel(normalized_id)
        finally:
            self._notify_tool_registration_if_changed(had_enabled_channels)

    def disable_channel(self, channel_id: str) -> None:
        """Disable one channel and stop its adapter task."""
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        had_enabled_channels = self.has_enabled_channels()
        if config.enabled:
            self._storage.save(replace(config, enabled=False))
        self.stop_channel(normalized_id)
        self._notify_tool_registration_if_changed(had_enabled_channels)

    def record_chat_id_migration(self, channel_id: str, old_chat_id: str, new_chat_id: str) -> None:
        """Persist a platform-side chat-id migration into the channel's allowlist.

        Swaps the old chat id for the new one in ``allowed_chat_ids`` and saves the
        config without restarting the adapter — the running adapter already swapped
        its in-memory allowlist and a restart would drop queued conversation work.
        Idempotent: a config that no longer lists the old id is left untouched.
        """
        normalized_id = _normalize_channel_id(channel_id)
        config = self._storage.get(normalized_id)
        self._storage.migrate_group_access(normalized_id, old_chat_id, new_chat_id)
        if old_chat_id not in config.allowed_chat_ids:
            return

        migrated_ids: list[str] = []
        for allowed_chat_id in config.allowed_chat_ids:
            candidate = new_chat_id if allowed_chat_id == old_chat_id else allowed_chat_id
            if candidate not in migrated_ids:
                migrated_ids.append(candidate)
        self._storage.save(replace(config, allowed_chat_ids=migrated_ids))
        _LOGGER.info(
            "Channel allowlist migrated (channel=%s old=%s new=%s)",
            normalized_id,
            old_chat_id,
            new_chat_id,
        )

    def has_active_channels(self) -> bool:
        """Return whether at least one channel adapter task is currently running."""
        return any(not task.done() for task in self._adapter_tasks.values())

    def has_enabled_channels(self) -> bool:
        """Return whether a valid Agent owns at least one enabled Channel config."""
        for config in self._storage.load_all():
            if not config.enabled:
                continue
            try:
                self._validate_agent_exists(config.agent_id)
            except ChannelConfigError:
                continue
            return True
        return False

    def is_running(self, channel_id: str) -> bool:
        """Return whether one channel's adapter task is currently running."""
        return self._is_running(_normalize_channel_id(channel_id))

    def is_failed(self, channel_id: str) -> bool:
        """Return whether one channel is currently marked failed.

        A running adapter is never reported failed: after a failed cycle the
        recovery loop keeps retrying, and a successful attempt must show as
        healthy immediately (the raw marker stays for the next crash).
        """
        normalized_id = _normalize_channel_id(channel_id)
        if self._is_running(normalized_id):
            return False
        return normalized_id in self._failed_channels

    def failure_reason(self, channel_id: str) -> str | None:
        """Return the latest failure reason for one failed channel, if any."""
        normalized_id = _normalize_channel_id(channel_id)
        if self._is_running(normalized_id):
            return None
        return self._failure_reasons.get(normalized_id)

    def denied_chats(self, channel_id: str) -> list[DeniedChatFacts]:
        """Return one channel's recently allowlist-denied inbound chats.

        Empty when the channel is not running: the log lives on the adapter
        instance, so there is nothing to report without an active adapter.
        """
        normalized_id = _normalize_channel_id(channel_id)
        adapter = self._adapters.get(normalized_id)
        if adapter is None:
            return []
        return adapter.denied_chats()

    def _notify_tool_registration_changed(self) -> None:
        try:
            self._notify_tool_registration_changed_hook()
        except Exception:
            _LOGGER.exception("Channel tool-registration hook failed")

    def _mark_channel_failed(self, channel_id: str, reason: str) -> None:
        self._failed_channels.add(channel_id)
        self._failure_reasons[channel_id] = reason

    def _notify_tool_registration_if_changed(self, had_enabled_channels: bool) -> None:
        if had_enabled_channels == self.has_enabled_channels():
            return
        self._notify_tool_registration_changed()

    def _create_adapter(self, config: ChannelConfig) -> ChannelAdapter:
        if config.platform == "discord":
            from core.channels.discord import DiscordChannelAdapter

            return DiscordChannelAdapter(
                config,
                self._trigger_service,
                self._chat_sessions,
                self._credential_resolver,
                attachment_store=self._attachment_store,
                command_dispatcher=self._command_dispatcher,
                access_registry=self._storage,
            )

        if config.platform == "telegram":
            from core.channels.telegram import TelegramChannelAdapter

            return TelegramChannelAdapter(
                config,
                self._trigger_service,
                self._chat_sessions,
                self._credential_resolver,
                attachment_store=self._attachment_store,
                command_dispatcher=self._command_dispatcher,
                chat_migration_persister=partial(self.record_chat_id_migration, config.id),
                interaction_dispatcher=self._interaction_dispatcher,
                run_button_binding_registry=self._storage,
                access_registry=self._storage,
                update_offset_store=self._storage,
            )

        raise ChannelConfigError(f"Unsupported channel platform: {config.platform}")

    def _preflight_adapter_start(self, config: ChannelConfig) -> None:
        if not config.enabled:
            return
        if _get_running_loop_or_none() is None:
            return
        self._create_adapter(config)

    def _validate_agent_exists(self, agent_id: str) -> None:
        try:
            self._agent_store.get(agent_id)
        except Exception as error:
            raise ChannelConfigError(f"Unknown agent_id: {agent_id}") from error

    def _rollback_created_channel(self, channel_id: str) -> None:
        try:
            self._storage.delete(channel_id)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while deleting newly created channel config (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _rollback_updated_channel(
        self,
        channel_id: str,
        previous_config: ChannelConfig,
        was_running: bool,
    ) -> None:
        self._pending_start_requests.pop(channel_id, None)
        try:
            self._storage.save(previous_config)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while restoring previous channel config (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

        if not was_running or not previous_config.enabled:
            return

        try:
            self.start_channel(channel_id, config_override=previous_config)
        except Exception as error:
            _LOGGER.error(
                "Rollback failed while restarting previous channel adapter (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _schedule_pending_start(
        self,
        channel_id: str,
        *,
        reset_backoff: bool,
        config_override: ChannelConfig | None,
    ) -> None:
        if not self._started:
            return
        self._pending_start_requests[channel_id] = (reset_backoff, config_override)

    def _is_stop_in_progress(self, channel_id: str) -> bool:
        task = self._adapter_stop_tasks.get(channel_id)
        return task is not None and not task.done()

    async def _await_adapter_shutdown(self, channel_id: str, task: asyncio.Task[None]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The task was already popped from _adapter_tasks before this runs, so its own
            # done-callback returns early without logging: log the shutdown failure here or
            # it surfaces nowhere.
            _LOGGER.error(
                "Channel adapter shutdown raised during stop (channel=%s)",
                channel_id,
                exc_info=True,
            )

        if self._adapter_stop_tasks.get(channel_id) is asyncio.current_task():
            self._adapter_stop_tasks.pop(channel_id, None)

        pending = self._pending_start_requests.pop(channel_id, None)
        if pending is None or not self._started:
            return

        reset_backoff, config_override = pending
        if config_override is None and not self._can_restart_channel(channel_id):
            return

        try:
            self.start_channel(
                channel_id,
                reset_backoff=reset_backoff,
                config_override=config_override,
            )
        except Exception as error:
            _LOGGER.error(
                "Cannot start queued channel adapter after stop completed (channel=%s): %s",
                channel_id,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _on_stop_task_done(self, channel_id: str, task: asyncio.Task[None]) -> None:
        if self._adapter_stop_tasks.get(channel_id) is task:
            self._adapter_stop_tasks.pop(channel_id, None)

        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        _LOGGER.error(
            "Channel adapter stop task failed for channel=%s: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )

    def _active_adapter(self, channel_id: str) -> ChannelAdapter:
        task = self._adapter_tasks.get(channel_id)
        adapter = self._adapters.get(channel_id)
        if task is None or adapter is None or task.done():
            raise ChannelNotFoundError(f"Channel not active: {channel_id}")
        return adapter

    def _is_running(self, channel_id: str) -> bool:
        task = self._adapter_tasks.get(channel_id)
        return task is not None and not task.done()

    async def _run_adapter(self, channel_id: str, adapter: ChannelAdapter) -> None:
        try:
            await adapter.start()
        finally:
            try:
                await adapter.stop()
            except Exception as error:
                _LOGGER.error(
                    "Channel adapter stop failed for channel=%s: %s",
                    channel_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

    def _on_adapter_task_done(self, channel_id: str, task: asyncio.Task[None]) -> None:
        if self._adapter_tasks.get(channel_id) is not task:
            return

        self._adapter_tasks.pop(channel_id, None)
        self._adapters.pop(channel_id, None)
        created_at = self._adapter_task_created.pop(channel_id, None)

        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            self._adapter_restart_attempts.pop(channel_id, None)
            self._failed_channels.discard(channel_id)
            self._failure_reasons.pop(channel_id, None)
            return

        if created_at is not None:
            runtime_seconds = time.monotonic() - created_at
            if runtime_seconds >= _ADAPTER_HEALTHY_RUN_RESET_SECONDS:
                # The adapter was up long enough to count as healthy: treat the
                # crash as a fresh incident instead of chronic failure.
                self._adapter_restart_attempts.pop(channel_id, None)
                self._failed_channels.discard(channel_id)
                self._failure_reasons.pop(channel_id, None)

        self._failure_reasons[channel_id] = str(error)

        _LOGGER.warning(
            "Channel adapter task failed for channel=%s; scheduling restart: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        self._schedule_restart(channel_id)

    def _schedule_restart(self, channel_id: str) -> None:
        if not self._started:
            return

        if self._is_stop_in_progress(channel_id):
            return

        existing_task = self._adapter_restart_tasks.get(channel_id)
        if existing_task is not None and not existing_task.done():
            return

        loop = _get_running_loop_or_none()
        if loop is None:
            _LOGGER.error(
                "Cannot restart channel adapter without a running event loop (channel=%s)",
                channel_id,
            )
            return

        restart_task = loop.create_task(
            self._restart_with_backoff(channel_id),
            name=f"channel:{channel_id}:restart",
        )
        self._adapter_restart_tasks[channel_id] = restart_task

        def on_done(completed_task: asyncio.Task[None], channel: str = channel_id) -> None:
            self._on_restart_task_done(channel, completed_task)

        restart_task.add_done_callback(on_done)

    async def _restart_with_backoff(self, channel_id: str) -> None:
        attempt = self._adapter_restart_attempts.get(channel_id, 0)
        if attempt >= _ADAPTER_RESTART_MAX_RETRIES:
            reason = self._failure_reasons.get(channel_id, "adapter restart attempts exhausted")
            self._mark_channel_failed(channel_id, reason)
            if attempt == _ADAPTER_RESTART_MAX_RETRIES:
                _LOGGER.error(
                    "Channel adapter exceeded max restart attempts and is marked failed; "
                    "recovery attempts continue at the capped backoff interval "
                    "(channel=%s, retries=%s)",
                    channel_id,
                    _ADAPTER_RESTART_MAX_RETRIES,
                )

        next_attempt = attempt + 1
        self._adapter_restart_attempts[channel_id] = next_attempt

        delay_seconds = self._restart_delay_seconds(next_attempt)
        if next_attempt <= _ADAPTER_RESTART_MAX_RETRIES:
            _LOGGER.warning(
                "Restarting channel adapter after %.1fs (channel=%s, attempt=%s/%s)",
                delay_seconds,
                channel_id,
                next_attempt,
                _ADAPTER_RESTART_MAX_RETRIES,
            )
        else:
            _LOGGER.warning(
                "Retrying failed channel adapter after %.1fs (channel=%s, recovery attempt=%s)",
                delay_seconds,
                channel_id,
                next_attempt,
            )
        await asyncio.sleep(delay_seconds)

        if not self._can_restart_channel(channel_id):
            return

        self.start_channel(channel_id, reset_backoff=False)

    def _restart_delay_seconds(self, attempt: int) -> float:
        delay = _ADAPTER_RESTART_INITIAL_DELAY_SECONDS * float(2 ** (attempt - 1))
        return float(min(_ADAPTER_RESTART_MAX_DELAY_SECONDS, delay))

    def _can_restart_channel(self, channel_id: str) -> bool:
        if not self._started:
            return False

        if self._is_running(channel_id):
            return False

        if self._is_stop_in_progress(channel_id):
            return False

        try:
            config = self._storage.get(channel_id)
        except ChannelNotFoundError:
            return False
        except ChannelError:
            _LOGGER.exception(
                "Cannot load channel config while checking restart eligibility (channel=%s)",
                channel_id,
            )
            return False

        return config.enabled

    def _cancel_restart_task(self, channel_id: str) -> None:
        task = self._adapter_restart_tasks.pop(channel_id, None)
        if task is None or task.done():
            return

        if task is asyncio.current_task():
            return

        task.cancel()

    def _on_restart_task_done(self, channel_id: str, task: asyncio.Task[None]) -> None:
        if self._adapter_restart_tasks.get(channel_id) is task:
            self._adapter_restart_tasks.pop(channel_id, None)

        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        _LOGGER.error(
            "Channel adapter restart task failed for channel=%s: %s",
            channel_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


def _normalize_outbound_buttons(
    buttons: list[list[InteractionButton]] | None,
) -> list[list[InteractionButton]] | None:
    """Validate an outbound inline-keyboard before it reaches an adapter.

    Returns ``None`` when no buttons were given (or every row is empty). Raises
    :class:`ChannelConfigError` on a malformed structure or a button whose
    callback ``data`` is empty or exceeds the 64-byte Telegram limit — so a bad
    payload fails at the service boundary, uniformly across adapters, rather than
    deep inside the Bot API call.
    """
    if buttons is None:
        return None
    if not isinstance(buttons, list):
        raise ChannelConfigError("buttons must be a list of button rows when provided")

    normalized: list[list[InteractionButton]] = []
    for row in buttons:
        if not isinstance(row, list):
            raise ChannelConfigError("each button row must be a list of buttons")
        normalized_row: list[InteractionButton] = []
        for button in row:
            if not isinstance(button, InteractionButton):
                raise ChannelConfigError("buttons must contain InteractionButton values only")
            if not isinstance(button.label, str) or not button.label:
                raise ChannelConfigError("each button label must be a non-empty string")
            if not isinstance(button.data, str) or not button.data:
                raise ChannelConfigError("each button data must be a non-empty string")
            if len(button.data.encode("utf-8")) > _MAX_CALLBACK_DATA_BYTES:
                raise ChannelConfigError(
                    f"button data exceeds {_MAX_CALLBACK_DATA_BYTES} bytes: {button.data!r}"
                )
            normalized_row.append(button)
        if normalized_row:
            normalized.append(normalized_row)

    return normalized or None


def _bind_outbound_run_buttons(
    rows: list[list[InteractionButton]],
    *,
    platform_target: str,
    thread_id: str | None,
    origin_session_id: str,
) -> tuple[list[list[InteractionButton]], RunButtonBinding | None]:
    binding_id = uuid4().hex
    original_data: list[str] = []
    bound_rows: list[list[InteractionButton]] = []
    for row in rows:
        bound_row: list[InteractionButton] = []
        for button in row:
            if button.data.split(":", 1)[0] != "run":
                bound_row.append(button)
                continue
            button_index = len(original_data)
            original_data.append(button.data)
            bound_row.append(
                InteractionButton(
                    label=button.label,
                    data=bound_run_callback_data(binding_id, button_index),
                )
            )
        bound_rows.append(bound_row)
    if not original_data:
        return rows, None
    return bound_rows, RunButtonBinding(
        id=binding_id,
        platform_target=platform_target,
        thread_id=thread_id,
        origin_session_id=origin_session_id,
        original_button_data=tuple(original_data),
        created_at=datetime.now(UTC).isoformat(),
    )


def _run_button_binding_to_dict(binding: RunButtonBinding) -> dict[str, Any]:
    return {
        "platform_target": binding.platform_target,
        "thread_id": binding.thread_id,
        "origin_session_id": binding.origin_session_id,
        "original_button_data": list(binding.original_button_data),
        "created_at": binding.created_at,
        "consumed": binding.consumed,
    }


def _run_button_binding_from_dict(binding_id: str, payload: Any) -> RunButtonBinding:
    if not isinstance(payload, dict):
        raise ValueError(f"binding {binding_id!r} must be an object")
    platform_target = payload.get("platform_target")
    thread_id = payload.get("thread_id")
    origin_session_id = payload.get("origin_session_id")
    original_button_data = payload.get("original_button_data")
    created_at = payload.get("created_at")
    consumed = payload.get("consumed")
    if not isinstance(platform_target, str) or not platform_target:
        raise ValueError(f"binding {binding_id!r} has invalid platform_target")
    if thread_id is not None and not isinstance(thread_id, str):
        raise ValueError(f"binding {binding_id!r} has invalid thread_id")
    if not isinstance(origin_session_id, str) or not origin_session_id:
        raise ValueError(f"binding {binding_id!r} has invalid origin_session_id")
    if (
        not isinstance(original_button_data, list)
        or not original_button_data
        or not all(
            isinstance(item, str) and item.split(":", 1)[0] == "run"
            for item in original_button_data
        )
    ):
        raise ValueError(f"binding {binding_id!r} has invalid original_button_data")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError(f"binding {binding_id!r} has invalid created_at")
    if not isinstance(consumed, bool):
        raise ValueError(f"binding {binding_id!r} has invalid consumed state")
    return RunButtonBinding(
        id=binding_id,
        platform_target=platform_target,
        thread_id=thread_id,
        origin_session_id=origin_session_id,
        original_button_data=tuple(original_button_data),
        created_at=created_at,
        consumed=consumed,
    )


def _normalize_channel_id(channel_id: str) -> str:
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


def _normalize_platform_access_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChannelConfigError(f"{field_name} must be a non-empty string")
    return value.strip()


def managed_channel_token_env_var(channel_id: str) -> str:
    """Return a collision-free, environment-safe key for a managed Channel token."""
    normalized_id = _normalize_channel_id(channel_id)
    encoded_id = normalized_id.encode("utf-8").hex().upper()
    return f"{MANAGED_CHANNEL_TOKEN_ENV_PREFIX}{encoded_id}"


def _get_running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _unique_pending_tasks(tasks: list[asyncio.Task[None]]) -> list[asyncio.Task[None]]:
    seen: set[asyncio.Task[None]] = set()
    pending_tasks: list[asyncio.Task[None]] = []
    for task in tasks:
        if task.done() or task in seen:
            continue
        seen.add(task)
        pending_tasks.append(task)
    return pending_tasks


__all__ = [
    "ChannelConfig",
    "ChannelConfigError",
    "ChannelError",
    "ChannelNotFoundError",
    "ChannelService",
    "ChannelStorage",
]
