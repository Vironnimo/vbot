"""Channel platform registrations shared by built-ins and Extensions."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from core.channels.adapter import ChannelAdapter
from core.channels.config import (
    ChannelConfig,
    ChannelConfigError,
    ChannelPlatformUnavailableError,
)

if TYPE_CHECKING:
    from core.attachments import AttachmentStore
    from core.channels.engine import ChannelConversationEngine


@dataclass(frozen=True)
class ChannelAdapterContext:
    """The conversation services available to one platform adapter factory."""

    engine: ChannelConversationEngine
    credential_resolver: Callable[[str], str]
    attachment_store: AttachmentStore | None
    state_dir: Path


type ChannelAdapterFactory = Callable[[ChannelConfig, ChannelAdapterContext], ChannelAdapter]


@dataclass(frozen=True)
class _Platform:
    owner: object
    platform_id: str
    label: str
    factory: ChannelAdapterFactory
    settings_schema: dict[str, Any]
    is_current: Callable[[], bool]


class ChannelPlatforms:
    """One ChannelService's catalog, validation and factory selection."""

    def __init__(self) -> None:
        self._platforms: dict[str, _Platform] = {}

    def register(
        self,
        owner: object,
        platform_id: str,
        label: str,
        factory: ChannelAdapterFactory,
        *,
        settings_schema: Mapping[str, Any] | None = None,
        is_current: Callable[[], bool] = lambda: True,
    ) -> None:
        if (
            not isinstance(platform_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]*", platform_id) is None
        ):
            raise ChannelConfigError("Platform id must use lowercase letters, digits, _ or -")
        if platform_id in self._platforms:
            raise ChannelConfigError(f"Channel platform already registered: {platform_id}")
        if not isinstance(label, str) or not label.strip() or not callable(factory):
            raise ChannelConfigError("Channel platform needs a label and adapter factory")
        schema = copy.deepcopy(dict(settings_schema or {"type": "object"}))
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ChannelConfigError("Channel platform settings schema must describe an object")
        self._platforms[platform_id] = _Platform(
            owner, platform_id, label.strip(), factory, schema, is_current
        )

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.platform_id,
                "label": item.label,
                "settings_schema": copy.deepcopy(item.settings_schema),
            }
            for item in self._platforms.values()
            if item.is_current()
        ]

    def pending(self, config: ChannelConfig) -> bool:
        item = self._platforms.get(config.platform)
        return item is not None and not item.is_current()

    def owner_platforms(self, owner: object) -> set[str]:
        return {key for key, item in self._platforms.items() if item.owner == owner}

    def release(self, owner: object) -> set[str]:
        removed = self.owner_platforms(owner)
        self._platforms = {key: item for key, item in self._platforms.items() if key not in removed}
        return removed

    def validate(self, config: ChannelConfig, *, available: bool = True) -> None:
        item = self._platforms.get(config.platform)
        if item is None or not item.is_current():
            if not available:
                return
            choices = ", ".join(item["id"] for item in self.catalog())
            raise ChannelPlatformUnavailableError(
                f"Channel platform '{config.platform}' is unavailable. Available: {choices}"
            )
        errors = sorted(
            Draft202012Validator(item.settings_schema).iter_errors(config.settings),
            key=lambda error: str(list(error.path)),
        )
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.path)
            # Validation errors can contain secret values; expose the path and
            # failed rule, never the rejected instance.
            raise ChannelConfigError(
                f"Channel settings{'.' + path if path else ''}: {error.validator} validation failed"
            )

    def create(self, config: ChannelConfig, context: ChannelAdapterContext) -> ChannelAdapter:
        self.validate(config)
        item = self._platforms[config.platform]
        adapter = item.factory(config, context)
        if not isinstance(adapter, ChannelAdapter):
            raise ChannelConfigError("Channel platform factory must return a ChannelAdapter")
        return adapter
