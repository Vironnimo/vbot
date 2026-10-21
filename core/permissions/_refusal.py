"""What a Permission Boundary refused, and how the Agent reads that refusal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from core.utils.errors import VBotError

type PathAccess = Literal["read", "write", "start_dir"]
type PermissionAccess = Literal["read", "write", "start_dir", "command", "web"]
type RefusalReason = Literal[
    "deny_rule", "protected_location", "not_permitted", "unsafe_form", "unanalyzable"
]
# Who set a layer: the Agent's own rules, or a ceiling inherited from the Session
# that started this Sub-Agent, the Session that created this Cron job, or an
# Extension that runs this Agent.
type LayerSource = Literal["agent", "parent", "cron", "extension"]
type ProtectedLocation = Literal[
    "data_dir", "installation", "agent_config", "vbot_server", "vbot_cli"
]

LAYER_SOURCES: Final[tuple[LayerSource, ...]] = ("agent", "parent", "cron", "extension")

_ACTIONS: Final[dict[str, str]] = {
    "read": "reading {target}",
    "write": "changing {target}",
    "start_dir": "working in {target}",
    "command": 'running "{target}"',
    "web": "fetching {target}",
}
_INHERITED: Final[dict[str, str]] = {
    "parent": "inherited from the Session that started this Sub-Agent",
    "cron": "inherited from the Session that scheduled this job",
    "extension": "set by the Extension that runs this Agent",
}
_PROTECTED: Final[dict[str, str]] = {
    "data_dir": "it is in vBot's data folder",
    "installation": "it is part of vBot's installation",
    "agent_config": "it is this Agent's own configuration",
    "vbot_server": "it is the vBot server itself",
    "vbot_cli": "it is vBot's own command line",
}
_ALLOWED: Final[dict[str, tuple[str, str]]] = {
    "read": ("This Agent can read in: {items}.", "This Agent cannot read any files."),
    "start_dir": ("This Agent can work in: {items}.", "This Agent cannot work in any folder."),
    "write": ("This Agent can change files in: {items}.", "This Agent cannot change any files."),
    "command": ("This Agent can run only: {items}.", "This Agent cannot run any commands."),
    "web": ("This Agent can fetch only from: {items}.", "This Agent cannot fetch from any host."),
}
_SAME_EVERYWHERE: Final = (
    "The user set these rules for this Agent; every Tool and command refuses the same "
    "access, so another way does not work either."
)
_NEXT_STEP: Final = "If the task needs this access, tell the user."


@dataclass(frozen=True, slots=True)
class PermissionRefusal:
    """One refused access, with everything its message needs.

    ``target`` is the normalized path (``/`` separators), the command or the host
    or URL. ``reason`` says why: a ``deny_rule`` (``rule``, set by the layer
    ``source``), a ``protected_location`` (``location``), ``not_permitted`` by the
    ``base`` of the layer ``source`` and no allow rule, an ``unsafe_form``
    (``location`` names the form, such as ``network path``) or ``unanalyzable``
    (``detail`` says what could not be analyzed; ``hint`` is a sentence saying how
    to write the access so it can be checked). ``allowed`` lists the folders, commands or hosts this
    access kind may reach; ``None`` when its base allows everything. ``resolved``
    is where a link or junction on the path leads, when that differs.
    """

    access: PermissionAccess
    target: str
    reason: RefusalReason
    source: LayerSource = "agent"
    rule: str | None = None
    base: str | None = None
    location: str | None = None
    detail: str | None = None
    resolved: str | None = None
    allowed: tuple[str, ...] | None = None
    hint: str | None = None

    def message(self, tool: str | None = None) -> str:
        """The Agent-facing refusal; with ``tool``, phrased as that Tool's failure."""
        action = _ACTIONS[self.access].format(target=self.target)
        if self.resolved is not None:
            action = f"{action}, which leads to {self.resolved}"
        lead = (
            f"{tool} was not run: the permission rules refuse {action}"
            if tool
            else f"The permission rules refuse {action}"
        )
        parts = [f"{lead}, because {self._reason()}."]
        # What could not be analyzed can still pass when written so it can be.
        if self.reason != "unanalyzable":
            parts.append(_SAME_EVERYWHERE)
        if self.hint is not None:
            parts.append(self.hint)
        if self.allowed is not None:
            listed, nothing = _ALLOWED[self.access]
            parts.append(listed.format(items=", ".join(self.allowed)) if self.allowed else nothing)
        if self.hint is None:
            parts.append(_NEXT_STEP)
        return " ".join(parts)

    def _reason(self) -> str:
        inherited = _INHERITED.get(self.source)
        if self.reason == "deny_rule":
            if inherited:
                return f"the rule {self.rule}, {inherited}, denies it"
            return f"the rule {self.rule} denies it"
        if self.reason == "protected_location":
            return _PROTECTED.get(self.location or "", "it is a protected location")
        if self.reason == "unsafe_form":
            return f"it is a {self.location or 'path form'} that no allow rule names"
        if self.reason == "unanalyzable":
            return (self.detail or "it could not be analyzed").rstrip(".")
        text = self._not_permitted()
        return f"{text} ({inherited})" if inherited else text

    def _not_permitted(self) -> str:
        if self.access == "command":
            return "only commands that match an allow rule can run"
        if self.access == "web":
            return "only hosts that match an allow rule can be fetched"
        if self.access == "write" and self.base == "scratch":
            return "this Agent can change files only in its scratch folder"
        if self.access == "write":
            return "it is outside the folders this Agent can change"
        return "it is outside the folders this Agent can read"


class PermissionDenied(VBotError):  # noqa: N818 - reads as the refusal it carries
    """An access the Permission Boundary refused, raised before any effect.

    Tools map it to the failure code ``permission_denied`` (not retryable).
    """

    def __init__(self, refusal: PermissionRefusal) -> None:
        super().__init__(refusal.message())
        self.refusal = refusal
