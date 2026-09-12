"""Persistence of Channel configuration, permissions and delivery state."""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from core.channels.adapter import (
    RunButtonBinding,
    RunButtonClaim,
)
from core.chat.messages import GroupRole
from core.config_validation import (
    JsonObject,
)
from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger

if TYPE_CHECKING:
    pass

from core.channels.config import (
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
    _normalize_channel_id,
    load_validated_channel_json,
)

_LOGGER = get_logger("channels")
_CHANNEL_CONFIG_FILENAME = "channel.json"


_CHANNEL_ACCESS_FILENAME = "access.json"


_CHANNEL_ACCESS_VERSION = 1


_RUN_BUTTON_BINDINGS_FILENAME = "run-button-bindings.json"


_RUN_BUTTON_BINDINGS_VERSION = 1


_POLLING_STATE_FILENAME = "polling.json"


_POLLING_STATE_VERSION = 1


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
                configs.append(self._read_config(config_path))
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
        return self._read_config(config_path)

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
            if update_id <= self.load_update_offset(normalized_id):
                return
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


def _normalize_platform_access_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChannelConfigError(f"{field_name} must be a non-empty string")
    return value.strip()
