"""core.extensions public API."""

from core.extensions.extensions import (
    API_VERSION,
    CommandDeclaration,
    Deny,
    ExtensionAPI,
    ExtensionManifest,
    ExtensionRecord,
    ExtensionRegistry,
    HookContext,
    Modify,
    PromptBlockDeclaration,
    Replace,
    ToolCallDecision,
    ToolResultValidator,
    purge_extension_modules,
)
from core.extensions.interactions import (
    RESERVED_INTERACTION_PREFIXES,
    RUN_TRIGGER_PREFIX,
    InteractionButton,
    InteractionEvent,
    InteractionHandlerDeclaration,
    InteractionResponder,
)
from core.extensions.settings_schema import (
    SettingsFieldDeclaration,
    parse_settings_fields,
    validate_extension_config,
)

__all__ = [
    "API_VERSION",
    "CommandDeclaration",
    "Deny",
    "ExtensionAPI",
    "ExtensionManifest",
    "ExtensionRecord",
    "ExtensionRegistry",
    "HookContext",
    "RESERVED_INTERACTION_PREFIXES",
    "RUN_TRIGGER_PREFIX",
    "InteractionButton",
    "InteractionEvent",
    "InteractionHandlerDeclaration",
    "InteractionResponder",
    "Modify",
    "PromptBlockDeclaration",
    "Replace",
    "SettingsFieldDeclaration",
    "ToolCallDecision",
    "ToolResultValidator",
    "parse_settings_fields",
    "purge_extension_modules",
    "validate_extension_config",
]
