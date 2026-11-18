"""Task profiles for specialized built-in Agents used by app views."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

type AppChatBuiltin = Literal["app_assistant"]


@dataclass(frozen=True)
class AppChatProfile:
    """Server-defined identity and capabilities, independent of the opening view."""

    id: str
    builtin: AppChatBuiltin
    agent_id: str
    name: str
    role: str
    tools: tuple[str, ...]
    prompt_blocks: tuple[str, ...] = ("core:agent_body", "core:tools", "core:system_reminders")
    skills: tuple[str, ...] = ()


_PROFILES = (
    AppChatProfile(
        id="app",
        builtin="app_assistant",
        agent_id="app-assistant",
        name="App Agent",
        role="Help the user understand and operate vBot from the view they are using.",
        tools=("skill", "vbot"),
        skills=("vbot-docs",),
    ),
)
APP_CHAT_PROFILES: Mapping[str, AppChatProfile] = MappingProxyType(
    {profile.id: profile for profile in _PROFILES}
)
_BY_BUILTIN: dict[str, AppChatProfile] = {profile.builtin: profile for profile in _PROFILES}


def app_chat_profile(agent: object) -> AppChatProfile | None:
    """Identify an app Agent by its trusted built-in mark, never by a user-chosen id."""
    kind = getattr(agent, "builtin", None)
    return _BY_BUILTIN.get(kind) if isinstance(kind, str) else None


def app_chat_tool_grants(agent: object, grants: Sequence[str]) -> tuple[str, ...]:
    """The profile grants its private Tools in every Session, including a preview."""
    if (profile := app_chat_profile(agent)) is None:
        return tuple(grants)
    return tuple(dict.fromkeys((*grants, *profile.tools)))
