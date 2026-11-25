"""Versioned Extension authoring API. Runtime values are owned by the vBot host."""

from typing import Any

from ._exports import HostUnavailableError as HostUnavailableError
from ._exports import exports_for, resolve

SDK_VERSION = "42.0.0"
API_VERSION = 42

_EXPORTS = exports_for(
    **{
        "core.extensions._api": ("ExtensionAPI",),
        "core.agents.temporary": ("TemporaryAgentConfig",),
        "core.agents.temporary_execution": ("TemporaryRunInput",),
        "core.extensions.operations": (
            "ExtensionHost",
            "IdentityAgentProfiles",
            "ExtensionOperations",
            "ManagementOperation",
            "ExtensionOperationError",
        ),
        "core.extensions.services": (
            "ExtensionServices",
            "ExtensionServiceError",
            "ExtensionSubscription",
            "ServiceKind",
        ),
        "core.extensions._declarations": (
            "HookContext",
            "Deny",
            "Modify",
            "Replace",
            "SessionRequestContext",
            "PreparedSessionDelivery",
            "ToolBatchDecision",
            "ToolCallDecision",
            "ExtensionUnavailableError",
        ),
        "core.extensions.interactions": (
            "InteractionButton",
            "InteractionEvent",
            "InteractionResponder",
        ),
        "core.extensions.settings_schema": ("SettingsFieldDeclaration",),
        "core.extensions.profile_references": ("write_profile_assignment",),
        "core.extensions.databases": (
            "Database",
            "DatabaseError",
            "ExtensionDataProtection",
            "Migration",
        ),
    }
)


def require_api(minimum: int = API_VERSION, *, maximum: int | None = None) -> int:
    """Check the running server API before using optional versioned capabilities.

    Manifest ``api_version`` is the normal load-time admission check. This
    function additionally supports code shared by Extensions with different
    minimum versions. It imports no host module until explicitly called.
    """
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("minimum must be a positive API version")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < minimum
    ):
        raise ValueError("maximum must be an API version at least minimum")
    version = int(
        resolve(__name__, {"API_VERSION": "core.extensions._declarations"}, "API_VERSION")
    )
    if version < minimum or (maximum is not None and version > maximum):
        raise RuntimeError(
            f"Extension requires API {minimum}..{maximum or '*'}; host has {version}"
        )
    return version


def __getattr__(name: str) -> Any:
    value = resolve(__name__, _EXPORTS, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = ["SDK_VERSION", "API_VERSION", "HostUnavailableError", "require_api", *_EXPORTS]
