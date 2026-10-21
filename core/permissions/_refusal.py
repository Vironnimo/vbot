"""What a Permission Boundary refused, and how the Agent reads that refusal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from core.utils.errors import VBotError

type PathAccess = Literal["read", "write", "start_dir"]
type PermissionAccess = Literal["read", "write", "start_dir", "command", "web", "connect"]
# A web access: ``web`` fetches from a host, ``connect`` reaches it with a network
# program such as ssh or scp.
type HostAccess = Literal["web", "connect"]
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
    "connect": "connecting to {target}",
}
_PROTECTED: Final[dict[str, str]] = {
    "data_dir": "it is in vBot's data folder",
    "installation": "it is part of vBot's installation",
    "vbot_server": "it is the vBot server itself",
    "vbot_cli": "it is vBot's own command line",
}
_LISTED: Final[dict[str, str]] = {
    "command": "only commands that match an allow rule can run",
    "web": "only hosts that match an allow rule can be fetched",
    "connect": "only hosts that match an allow rule can be reached",
}


@dataclass(frozen=True, slots=True)
class _Voice:
    """Whom a refusal addresses: the Agent itself, or anyone reading a Cron watch failure."""

    inherited: dict[str, str]
    rule_file: str
    scratch_only: str
    outside: dict[str, str]


# The Agent reads its refusal as the result of its own call.
_AGENT: Final = _Voice(
    inherited={
        "parent": "inherited from the Session that started you",
        "cron": "inherited from the Session that scheduled this job",
        "extension": "set by the Extension that runs you",
    },
    rule_file="it is a file your permission rules come from",
    scratch_only="you can change files only in your scratch folder",
    outside={
        "read": "it is outside the folders you can read",
        "write": "it is outside the folders you can change",
        "start_dir": "it is outside the folders you can work in",
    },
)
# A watch check runs no Agent: the user reads its failure in an alert, the Agent
# in the job. Every layer of a job's ceiling comes from the Session that scheduled it.
_WATCH: Final = _Voice(
    inherited=dict.fromkeys(
        ("parent", "cron", "extension"), "inherited from the Session that scheduled this job"
    ),
    rule_file="it is a file the Agent's permission rules come from",
    scratch_only="these rules allow changes only in a scratch folder",
    outside={
        "read": "it is outside the folders these rules allow reading",
        "write": "it is outside the folders these rules allow changing",
        "start_dir": "it is outside the folders these rules allow working in",
    },
)
_ALLOWED: Final[dict[str, tuple[str, str]]] = {
    "read": ("You can read in: {items}.", "You cannot read any files."),
    "start_dir": ("You can work in: {items}.", "You cannot work in any folder."),
    "write": ("You can change files in: {items}.", "You cannot change any files."),
    "command": ("You can run only: {items}.", "You cannot run any commands."),
    "web": ("You can fetch only from: {items}.", "You cannot fetch from any host."),
    "connect": ("You can connect only to: {items}.", "You cannot connect to any host."),
}
_SAME_EVERYWHERE: Final = (
    "vBot's Tools and commands refuse the same access, and the limits also apply to "
    "scripts and programs that vBot does not check."
)
_NEXT_STEP: Final = "If the task needs this access, tell the user."
_WATCH_NEXT_STEP: Final[dict[str, str]] = {
    "web": "Change the watch to an address these rules allow.",
    "command": "Change the watch to a command these rules allow.",
}


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
    is where a link or junction on the path leads, when that differs. ``below``:
    a recursive or wildcard access to ``target`` is refused for the part below
    it that ``rule`` denies. ``retryable``: the same call can succeed when
    repeated (the check itself failed), which Tools report.
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
    below: str | None = None
    retryable: bool = False

    def message(self, tool: str | None = None) -> str:
        """The Agent-facing refusal; with ``tool``, phrased as that Tool's failure."""
        action = self._action()
        lead = (
            f"{tool} was not run: the permission rules refuse {action}"
            if tool
            else f"The permission rules refuse {action}"
        )
        parts = [f"{lead}, because {self._reason(_AGENT)}."]
        # A hint names another way that works, so the same-everywhere sentence would be false.
        if self.reason != "unanalyzable" and self.hint is None:
            parts.append(_SAME_EVERYWHERE)
        if self.hint is not None:
            parts.append(self.hint)
        if self.allowed is not None:
            listed, nothing = _ALLOWED[self.access]
            parts.append(listed.format(items=", ".join(self.allowed)) if self.allowed else nothing)
        if self.hint is None:
            parts.append(_NEXT_STEP)
        return " ".join(parts)

    def watch_message(self) -> str:
        """The refusal of a Cron watch check, for the user's alert and the Agent's job.

        It starts in lower case, to follow the caller's ``The check did not run:``.
        """
        first = f"the permission rules refuse {self._action()}, because {self._reason(_WATCH)}."
        if self.retryable:
            return first
        if self.reason == "unanalyzable" and self.hint is not None:
            return f"{first} {self.hint}"
        return f"{first} {_WATCH_NEXT_STEP['web' if self.access == 'web' else 'command']}"

    def _action(self) -> str:
        action = _ACTIONS[self.access].format(target=self.target)
        if self.resolved is not None:
            action = f"{action}, which leads to {self.resolved}"
        return action

    def _reason(self, voice: _Voice) -> str:
        inherited = voice.inherited.get(self.source)
        if self.reason == "deny_rule" and self.below is not None:
            verb = "change" if self.access == "write" else "read"
            rule = f"{self.rule}, {inherited}," if inherited else self.rule
            return f"it would also {verb} {self.below}, which the rule {rule} denies"
        if self.reason == "deny_rule":
            if inherited:
                return f"the rule {self.rule}, {inherited}, denies it"
            return f"the rule {self.rule} denies it"
        if self.reason == "protected_location":
            # Also a Team member's repository files: they hold its rules as well.
            if self.location == "agent_config":
                return voice.rule_file
            return _PROTECTED.get(self.location or "", "it is a protected location")
        if self.reason == "unsafe_form":
            form = self.location or "path form"
            article = "an" if form[0] in "aeiou" else "a"
            return f"it is {article} {form} that no allow rule names"
        if self.reason == "unanalyzable":
            return (self.detail or "it could not be analyzed").rstrip(".")
        if self.access in _LISTED:
            text = _LISTED[self.access]
        elif self.access == "write" and self.base == "scratch":
            text = voice.scratch_only
        else:
            text = voice.outside[self.access]
        return f"{text} ({inherited})" if inherited else text


class PermissionDenied(VBotError):  # noqa: N818 - reads as the refusal it carries
    """An access the Permission Boundary refused, raised before any effect.

    Tools map it to the failure code ``permission_denied``, retryable as the refusal says.
    """

    def __init__(self, refusal: PermissionRefusal) -> None:
        super().__init__(refusal.message())
        self.refusal = refusal
