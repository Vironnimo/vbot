"""Persistence of Channel configuration documents (``channel.json``)."""

from __future__ import annotations

import shutil
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
from core.json_documents import JsonDocumentWriteError, write_json_document
from core.utils.logging import get_logger

_LOGGER = get_logger("channels")
_CHANNEL_CONFIG_FILENAME = "channel.json"


class ChannelStorage:
    """Persist channel configs under <data_root>/channels/<id>/channel.json."""

    def __init__(self, data_root: str | Path) -> None:
        self._data_root = Path(data_root).expanduser()
        self._channels_dir = self._data_root / "channels"

    def platforms(self) -> dict[str, str | None]:
        """Map every Channel directory holding a config, valid or not, to its platform.

        The platform is None when the config cannot be loaded; the load failure
        itself surfaces where the config is used.
        """
        if not self._channels_dir.is_dir():
            return {}
        platforms: dict[str, str | None] = {}
        try:
            for channel_dir in self._channels_dir.iterdir():
                config_path = channel_dir / _CHANNEL_CONFIG_FILENAME
                if not config_path.is_file():
                    continue
                try:
                    channel_id = _normalize_channel_id(channel_dir.name)
                except ChannelConfigError:
                    continue
                try:
                    platforms[channel_id] = self._read_config(config_path).platform
                except ChannelError:
                    platforms[channel_id] = None
        except OSError as error:
            _LOGGER.warning("Cannot scan Channel configs in %s: %s", self._channels_dir, error)
        return dict(sorted(platforms.items()))

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
        return config
