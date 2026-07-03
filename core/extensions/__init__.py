"""core.extensions public API."""

from core.extensions.extensions import (
    API_VERSION,
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
)
from core.extensions.settings_schema import (
    SettingsFieldDeclaration,
    parse_settings_fields,
    validate_extension_config,
)

__all__ = [
    "API_VERSION",
    "Deny",
    "ExtensionAPI",
    "ExtensionManifest",
    "ExtensionRecord",
    "ExtensionRegistry",
    "HookContext",
    "Modify",
    "PromptBlockDeclaration",
    "Replace",
    "SettingsFieldDeclaration",
    "ToolCallDecision",
    "ToolResultValidator",
    "parse_settings_fields",
    "validate_extension_config",
]
