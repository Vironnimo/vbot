"""Install Provider and Channel declarations at their existing domain owners."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from functools import partial
from typing import TYPE_CHECKING

from core.extensions._callbacks import invoke_lifecycle_handler
from core.extensions._declarations import ExtensionRegistrationIdentity, _diagnose_capability
from core.utils.logging import get_logger

_LOGGER = get_logger("extensions")

if TYPE_CHECKING:
    from core.channels import ChannelService
    from core.extensions.extensions import ExtensionRegistry
    from core.providers import ProviderRegistry


def apply_providers(registry: ExtensionRegistry, providers: ProviderRegistry) -> None:
    for record in registry.records():
        if record.status != "loaded":
            continue
        identity = registry.registration_identity(record.name)
        registry._integration_owners[record.name] = identity
        for declaration in record.declarations.providers:
            try:
                providers.register_provider(
                    identity,
                    declaration.definition,
                    declaration.adapter_type,
                    is_current=partial(registry.is_registration_current, identity),
                )
            except Exception as error:
                _diagnose_capability(record, f"Provider declaration skipped: {error}")


def apply_channels(registry: ExtensionRegistry, channels: ChannelService) -> None:
    for record in registry.records():
        if record.status != "loaded":
            continue
        identity = registry.registration_identity(record.name)
        registry._integration_owners[record.name] = identity
        for declaration in record.declarations.channel_platforms:
            try:
                channels.register_platform(
                    identity,
                    declaration.platform_id,
                    declaration.label,
                    declaration.factory,
                    settings_schema=declaration.settings_schema,
                    is_current=partial(_channel_ready, registry, identity),
                )
            except Exception as error:
                _diagnose_capability(record, f"Channel platform declaration skipped: {error}")


async def release_integrations(
    registry: ExtensionRegistry,
    providers: ProviderRegistry | None,
    channels: ChannelService | None,
    names: set[str] | None = None,
    *,
    publication: Callable[[], AbstractAsyncContextManager[None]] = nullcontext,
    retire: bool = False,
) -> None:
    owners = {
        name: identity
        for name, identity in registry._integration_owners.items()
        if names is None or name in names
    }
    # Provider withdrawal and catalog publication share admission. Channel
    # shutdown can await arbitrary adapter work and must happen outside it.
    async with publication():
        if retire:
            registry.retire_registration()
        if providers is not None:
            for identity in owners.values():
                providers.release_providers(identity)
    for name, identity in owners.items():
        if channels is not None:
            try:
                await invoke_lifecycle_handler(
                    partial(channels.release_platforms, identity),
                    extension_name=name,
                    description="Channel drain",
                    cancel_on_timeout=False,
                )
            except Exception:
                # Admission has already been withdrawn. A slow old Channel
                # keeps draining; its replacement waits at ChannelService.
                _LOGGER.exception("Extension Channel drain did not finish (extension=%s)", name)
        registry._integration_owners.pop(name, None)


def _channel_ready(registry: ExtensionRegistry, identity: ExtensionRegistrationIdentity) -> bool:
    return registry._integrations_ready and registry.is_registration_current(identity)


def resume_channels(
    registry: ExtensionRegistry, channels: ChannelService, *, start: bool = True
) -> None:
    registry._integrations_ready = True
    if not start:
        return
    for record in registry.records():
        if record.status == "loaded":
            channels.resume_platforms(registry.registration_identity(record.name))
