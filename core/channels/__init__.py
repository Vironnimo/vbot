"""Channel domain public API.

Exports load on first access, so importing a light member such as the
configuration constants does not import the adapter and service stack.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.channels._choices import (
        ALLOWED_CHANNEL_DM_SCOPES,
        ALLOWED_CHANNEL_PLATFORMS,
        ALLOWED_CHANNEL_RESPONSE_MODES,
    )
    from core.channels._state_schema import CHANNEL_DATABASE_NAME, channel_database_spec
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
    from core.channels.checklist import (
        CHECKLIST_ITEM_ID_MAX_BYTES,
        ChecklistItem,
        checklist_keyboard,
    )
    from core.channels.config import (
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
    "CHANNEL_DATABASE_NAME": "core.channels._state_schema",
    "ALLOWED_CHANNEL_DM_SCOPES": "core.channels._choices",
    "ALLOWED_CHANNEL_PLATFORMS": "core.channels._choices",
    "ALLOWED_CHANNEL_RESPONSE_MODES": "core.channels._choices",
    "CHECKLIST_ITEM_ID_MAX_BYTES": "core.channels.checklist",
    "MANAGED_CHANNEL_TOKEN_ENV_PREFIX": "core.channels.config",
    "ChannelAdapter": "core.channels.adapter",
    "ChannelConfig": "core.channels.config",
    "ChannelConfigError": "core.channels.config",
    "ChannelError": "core.channels.config",
    "ChannelNotFoundError": "core.channels.config",
    "ChannelService": "core.channels.channels",
    "ChannelStorage": "core.channels.storage",
    "ChecklistItem": "core.channels.checklist",
    "ConversationFacts": "core.channels.adapter",
    "DeniedChatFacts": "core.channels.adapter",
    "DeniedChatLog": "core.channels.adapter",
    "MessageFacts": "core.channels.adapter",
    "QuotedMessageFacts": "core.channels.adapter",
    "ReplyPlanFacts": "core.channels.adapter",
    "RouteFacts": "core.channels.adapter",
    "channel_database_spec": "core.channels._state_schema",
    "checklist_keyboard": "core.channels.checklist",
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
    "CHANNEL_DATABASE_NAME",
    "ALLOWED_CHANNEL_DM_SCOPES",
    "ALLOWED_CHANNEL_PLATFORMS",
    "ALLOWED_CHANNEL_RESPONSE_MODES",
    "CHECKLIST_ITEM_ID_MAX_BYTES",
    "MANAGED_CHANNEL_TOKEN_ENV_PREFIX",
    "ChannelAdapter",
    "ChannelConfig",
    "ChannelConfigError",
    "ChannelError",
    "ChannelNotFoundError",
    "ChannelService",
    "ChannelStorage",
    "ChecklistItem",
    "ConversationFacts",
    "DeniedChatFacts",
    "DeniedChatLog",
    "MessageFacts",
    "QuotedMessageFacts",
    "ReplyPlanFacts",
    "RouteFacts",
    "channel_database_spec",
    "checklist_keyboard",
    "load_validated_channel_json",
    "managed_channel_token_env_var",
    "validate_channel_data",
    "validate_channel_document",
    "validate_channel_file",
]
