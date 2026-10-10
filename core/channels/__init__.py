"""Channel domain public API.

Exports load on first access, so importing a light member such as the
configuration constants does not import the adapter and service stack.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.channels._state_schema import channel_database_spec
    from core.channels.adapter import (
        ChannelAdapter,
        ConversationFacts,
        DeniedChatFacts,
        DeniedChatLog,
        MessageFacts,
        QuotedMessageFacts,
        ReplyPlanFacts,
        RouteFacts,
    )
    from core.channels.channels import ChannelService
    from core.channels.config import (
        ALLOWED_CHANNEL_DM_SCOPES,
        ALLOWED_CHANNEL_PLATFORMS,
        ALLOWED_CHANNEL_RESPONSE_MODES,
        MANAGED_CHANNEL_TOKEN_ENV_PREFIX,
        ChannelConfig,
        ChannelConfigError,
        ChannelError,
        ChannelNotFoundError,
        load_validated_channel_json,
        managed_channel_token_env_var,
        validate_channel_data,
        validate_channel_document,
        validate_channel_file,
    )
    from core.channels.storage import ChannelStorage


_EXPORT_MODULES = {
    "ALLOWED_CHANNEL_DM_SCOPES": "core.channels.config",
    "ALLOWED_CHANNEL_PLATFORMS": "core.channels.config",
    "ALLOWED_CHANNEL_RESPONSE_MODES": "core.channels.config",
    "MANAGED_CHANNEL_TOKEN_ENV_PREFIX": "core.channels.config",
    "ChannelAdapter": "core.channels.adapter",
    "ChannelConfig": "core.channels.config",
    "ChannelConfigError": "core.channels.config",
    "ChannelError": "core.channels.config",
    "ChannelNotFoundError": "core.channels.config",
    "ChannelService": "core.channels.channels",
    "ChannelStorage": "core.channels.storage",
    "ConversationFacts": "core.channels.adapter",
    "DeniedChatFacts": "core.channels.adapter",
    "DeniedChatLog": "core.channels.adapter",
    "MessageFacts": "core.channels.adapter",
    "QuotedMessageFacts": "core.channels.adapter",
    "ReplyPlanFacts": "core.channels.adapter",
    "RouteFacts": "core.channels.adapter",
    "channel_database_spec": "core.channels._state_schema",
    "load_validated_channel_json": "core.channels.config",
    "managed_channel_token_env_var": "core.channels.config",
    "validate_channel_data": "core.channels.config",
    "validate_channel_document": "core.channels.config",
    "validate_channel_file": "core.channels.config",
}


def __getattr__(name: str) -> Any:
    """Load public channel exports only when a caller requests them."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "ALLOWED_CHANNEL_DM_SCOPES",
    "ALLOWED_CHANNEL_PLATFORMS",
    "ALLOWED_CHANNEL_RESPONSE_MODES",
    "MANAGED_CHANNEL_TOKEN_ENV_PREFIX",
    "ChannelAdapter",
    "ChannelConfig",
    "ChannelConfigError",
    "ChannelError",
    "ChannelNotFoundError",
    "ChannelService",
    "ChannelStorage",
    "ConversationFacts",
    "DeniedChatFacts",
    "DeniedChatLog",
    "MessageFacts",
    "QuotedMessageFacts",
    "ReplyPlanFacts",
    "RouteFacts",
    "channel_database_spec",
    "load_validated_channel_json",
    "managed_channel_token_env_var",
    "validate_channel_data",
    "validate_channel_document",
    "validate_channel_file",
]
