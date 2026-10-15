"""Persistence of Channel configuration documents (``channel.json``)."""

from __future__ import annotations

import shutil
import threading
from copy import deepcopy
from pathlib import Path

from core.channels.config import (
    CHANNEL_FORMAT,
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
    _normalize_channel_id,
    load_validated_channel_json,
)
from core.json_documents import JsonDocumentWriteError, document_change, write_json_document
from core.utils.file_status import exists_strict, is_dir_strict, is_file_strict
from core.utils.logging import get_logger, register_log_channel_ids

_LOGGER = get_logger("channels")
_CHANNEL_CONFIG_FILENAME = "channel.json"

type _ConfigFailure = tuple[type[ChannelError], str]


class ChannelStorage:
    """Persist channel configs under <data_root>/channels/<id>/channel.json."""

    def __init__(self, data_root: str | Path) -> None:
        self._data_root = Path(data_root).expanduser()
        self._channels_dir = self._data_root / "channels"
        self._config_lock = threading.RLock()
        # Readers retain one published immutable view; they never wait for file I/O.
        self._snapshot: tuple[dict[str, ChannelConfig], dict[str, _ConfigFailure]] = ({}, {})

    def platforms(self) -> dict[str, str | None]:
        """Map every Channel directory holding a config, valid or not, to its platform.

        The platform is None when the config cannot be loaded; the load failure
        itself surfaces where the config is used.
        """
        platforms: dict[str, str | None] = {}
        try:
            for channel_dir in self._channels_dir.iterdir():
                config_path = channel_dir / _CHANNEL_CONFIG_FILENAME
                try:
                    channel_id = _normalize_channel_id(channel_dir.name)
                except ChannelConfigError:
                    continue
                try:
                    if not is_dir_strict(channel_dir) or not is_file_strict(config_path):
                        continue
                    platforms[channel_id] = self._read_config(config_path).platform
                except ChannelError, OSError:
                    platforms[channel_id] = None
        except FileNotFoundError:
            return {}
        except OSError as error:
            _LOGGER.warning("Cannot scan Channel configs in %s: %s", self._channels_dir, error)
        return dict(sorted(platforms.items()))

    def snapshot(self) -> list[ChannelConfig]:
        """Return the last successful read/write projection without filesystem work."""
        configs, _errors = self._snapshot
        return [deepcopy(configs[key]) for key in sorted(configs)]

    def cached(self, channel_id: str) -> ChannelConfig:
        """Resolve the current in-memory projection, including known unreadable configs."""
        normalized_id = _normalize_channel_id(channel_id)
        configs, errors = self._snapshot
        if normalized_id in errors:
            error_type, message = errors[normalized_id]
            raise error_type(message)
        if normalized_id not in configs:
            raise ChannelNotFoundError(f"Channel not found: {normalized_id}")
        return deepcopy(configs[normalized_id])

    def _publish(
        self, channel_id: str, config: ChannelConfig | None, error: ChannelError | None = None
    ) -> None:
        configs, errors = self._snapshot
        updated, failures = dict(configs), dict(errors)
        updated.pop(channel_id, None)
        failures.pop(channel_id, None)
        if config is not None:
            updated[channel_id] = deepcopy(config)
        if error is not None:
            failures[channel_id] = (type(error), str(error))
        self._snapshot = updated, failures

    def load_all(self) -> list[ChannelConfig]:
        """Refresh and publish all persisted configs; blocking, for worker threads."""
        with self._config_lock:
            errors: dict[str, _ConfigFailure] = {}
            configs = self._load_all(errors)
            self._snapshot = {config.id: deepcopy(config) for config in configs}, errors
            return configs

    def _load_all(self, errors: dict[str, _ConfigFailure]) -> list[ChannelConfig]:
        """Load all valid persisted channel configs in stable id-order.

        A config that fails to parse or validate is skipped with a logged warning rather
        than aborting the whole load: one corrupt ``channel.json`` must not block server
        startup or hide every other channel. Strict single-channel access stays in ``get``.
        """
        configs: list[ChannelConfig] = []
        try:
            channel_directories = sorted(
                self._channels_dir.iterdir(),
                key=lambda path: path.name,
            )
        except FileNotFoundError:
            return []
        except OSError as error:
            _LOGGER.warning("Cannot scan Channel configs in %s: %s", self._channels_dir, error)
            return []

        for channel_dir in channel_directories:
            config_path = channel_dir / _CHANNEL_CONFIG_FILENAME
            try:
                if not is_dir_strict(channel_dir) or not is_file_strict(config_path):
                    continue
                configs.append(self._read_config(config_path))
            except (ChannelError, OSError) as error:
                errors[channel_dir.name] = (
                    type(error) if isinstance(error, ChannelError) else ChannelError,
                    str(error),
                )
                _LOGGER.warning("Skipping invalid channel config %s: %s", config_path, error)

        return sorted(configs, key=lambda config: config.id)

    def save(self, config: ChannelConfig) -> None:
        """Publish only after a successful durable save; blocking."""
        with self._config_lock:
            self._save(config)
            self._publish(config.id, config)

    def _save(self, config: ChannelConfig) -> None:
        """Persist one channel config using atomic replace.

        Unknown fields of the file on disk are kept; a file that fails to load
        is never overwritten.
        """
        if not isinstance(config, ChannelConfig):
            raise ChannelConfigError("config must be a ChannelConfig instance")
        config.validate()

        config_path = self._channel_dir(config.id) / _CHANNEL_CONFIG_FILENAME
        try:
            write_json_document(config_path, config.to_dict(), CHANNEL_FORMAT)
        except JsonDocumentWriteError as error:
            raise ChannelConfigError(str(error)) from error
        except OSError as error:
            raise ChannelError(f"Cannot write {config_path}: {error}") from error
        register_log_channel_ids((config.id,))

    def delete(self, channel_id: str) -> None:
        """Delete and retire the config from the live projection; blocking."""
        with self._config_lock:
            self._delete(channel_id)
            self._publish(_normalize_channel_id(channel_id), None)

    def _delete(self, channel_id: str) -> None:
        """Delete one channel directory from storage."""
        normalized_id = _normalize_channel_id(channel_id)
        channel_dir = self._channel_dir(normalized_id)
        try:
            if not exists_strict(channel_dir):
                raise ChannelNotFoundError(f"Channel not found: {normalized_id}")
            if not is_dir_strict(channel_dir):
                raise ChannelError(f"Channel path is not a directory: {channel_dir}")
            with document_change(channel_dir):
                shutil.rmtree(channel_dir)
        except OSError as error:
            raise ChannelError(f"Cannot delete channel directory {channel_dir}: {error}") from error

    def get(self, channel_id: str) -> ChannelConfig:
        """Read and publish one current config, including failures; blocking."""
        with self._config_lock:
            normalized_id = _normalize_channel_id(channel_id)
            try:
                config = self._get(normalized_id)
            except ChannelNotFoundError:
                self._publish(normalized_id, None)
                raise
            except ChannelError as error:
                self._publish(normalized_id, None, error)
                raise
            self._publish(normalized_id, config)
            return config

    def _get(self, channel_id: str) -> ChannelConfig:
        """Load one channel config by id."""
        normalized_id = _normalize_channel_id(channel_id)
        config_path = self._channel_dir(normalized_id) / _CHANNEL_CONFIG_FILENAME
        try:
            if not is_file_strict(config_path):
                raise ChannelNotFoundError(f"Channel not found: {normalized_id}")
        except OSError as error:
            raise ChannelError(f"Cannot read {config_path}: {error}") from error
        return self._read_config(config_path)

    def _channel_dir(self, channel_id: str) -> Path:
        return self._channels_dir / channel_id

    def _read_config(self, config_path: Path) -> ChannelConfig:
        payload = load_validated_channel_json(config_path)
        config = ChannelConfig.from_dict(payload)
        if config.id != config_path.parent.name:
            raise ChannelConfigError(
                "Channel id mismatch for "
                f"{config_path}: expected {config_path.parent.name}, got {config.id}"
            )
        # Log lines name this Channel in its pseudonymized Session ids.
        register_log_channel_ids((config.id,))
        return config
