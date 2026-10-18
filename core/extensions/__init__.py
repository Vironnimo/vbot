"""Extension public API, loaded on first access."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.extensions.extensions import (
        API_VERSION,
        CommandDeclaration,
        Deny,
        ExtensionAPI,
        ExtensionManifest,
        ExtensionRecord,
        ExtensionRegistrationIdentity,
        ExtensionRegistry,
        HookContext,
        Modify,
        PageDeclaration,
        PreparedSessionDelivery,
        PromptBlockDeclaration,
        Replace,
        SessionRequestContext,
        ToolBatchDecision,
        ToolCallDecision,
        ToolFamilyDeclaration,
        ToolResultValidator,
        invoke_extension_handler,
        purge_extension_modules,
    )
    from core.extensions.interactions import (
        CHECKLIST_PREFIX,
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

_EXPORT_MODULES = {
    "API_VERSION": "core.extensions.extensions",
    "CommandDeclaration": "core.extensions.extensions",
    "Deny": "core.extensions.extensions",
    "ExtensionAPI": "core.extensions.extensions",
    "ExtensionManifest": "core.extensions.extensions",
    "ExtensionRecord": "core.extensions.extensions",
    "ExtensionRegistrationIdentity": "core.extensions.extensions",
    "ExtensionRegistry": "core.extensions.extensions",
    "HookContext": "core.extensions.extensions",
    "Modify": "core.extensions.extensions",
    "PageDeclaration": "core.extensions.extensions",
    "PreparedSessionDelivery": "core.extensions.extensions",
    "PromptBlockDeclaration": "core.extensions.extensions",
    "Replace": "core.extensions.extensions",
    "SessionRequestContext": "core.extensions.extensions",
    "ToolBatchDecision": "core.extensions.extensions",
    "ToolCallDecision": "core.extensions.extensions",
    "ToolFamilyDeclaration": "core.extensions.extensions",
    "ToolResultValidator": "core.extensions.extensions",
    "invoke_extension_handler": "core.extensions.extensions",
    "purge_extension_modules": "core.extensions.extensions",
    "CHECKLIST_PREFIX": "core.extensions.interactions",
    "RESERVED_INTERACTION_PREFIXES": "core.extensions.interactions",
    "RUN_TRIGGER_PREFIX": "core.extensions.interactions",
    "InteractionButton": "core.extensions.interactions",
    "InteractionEvent": "core.extensions.interactions",
    "InteractionHandlerDeclaration": "core.extensions.interactions",
    "InteractionResponder": "core.extensions.interactions",
    "SettingsFieldDeclaration": "core.extensions.settings_schema",
    "parse_settings_fields": "core.extensions.settings_schema",
    "validate_extension_config": "core.extensions.settings_schema",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "API_VERSION",
    "CHECKLIST_PREFIX",
    "CommandDeclaration",
    "Deny",
    "ExtensionAPI",
    "ExtensionManifest",
    "ExtensionRegistrationIdentity",
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
    "PageDeclaration",
    "PreparedSessionDelivery",
    "ToolBatchDecision",
    "PromptBlockDeclaration",
    "Replace",
    "SettingsFieldDeclaration",
    "ToolCallDecision",
    "ToolFamilyDeclaration",
    "ToolResultValidator",
    "SessionRequestContext",
    "invoke_extension_handler",
    "parse_settings_fields",
    "purge_extension_modules",
    "validate_extension_config",
]
