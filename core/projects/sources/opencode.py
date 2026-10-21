"""OpenCode Markdown and JSON Agents, translated into whole vBot Tools."""

from __future__ import annotations

import re
from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    ASK_DETAIL,
    SHELL_TOOLS,
    RuleDraft,
    permission_rules,
    profile,
    unavailable,
    unsupported,
)
from core.projects.sources.profile import AgentProfile, AgentTargetRule, ToolRule, Translation

_MAPPING = {
    "read": frozenset({"read"}),
    "edit": frozenset({"apply_patch"}),
    "write": frozenset({"apply_patch"}),
    "apply_patch": frozenset({"apply_patch"}),
    "bash": SHELL_TOOLS,
    "grep": frozenset({"search_files"}),
    "glob": frozenset({"search_files"}),
    "list": frozenset({"search_files"}),
    "webfetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "task": frozenset({"subagent"}),
    "skill": frozenset({"skill"}),
}
# Configuration entries under these names adjust OpenCode's own Agents; they
# define no Agent of their own.
_BUILT_IN_AGENTS = frozenset({"build", "plan", "general", "explore"})
CONFIG_FILES = (
    "opencode.json",
    "opencode.jsonc",
    ".opencode/opencode.json",
    ".opencode/opencode.jsonc",
)


class OpenCodeAdapter:
    source = "opencode"

    def scan(self, root: Path) -> list[AgentProfile]:
        return self._scan(root)

    def read(self, root: Path, selected: AgentProfile) -> AgentProfile | None:
        path = selected.source_path
        if path in (root / relative for relative in CONFIG_FILES):
            if not reading.is_file_strict(path):
                return None
        elif not any(
            reading.is_source_file(root / folder, path)
            for folder in (".opencode/agents", ".opencode/agent")
        ):
            return None
        return next(iter(self._scan(root, selected)), None)

    def _scan(self, root: Path, selected: AgentProfile | None = None) -> list[AgentProfile]:
        # Every profile reads the shared configuration files, also before they exist.
        shared = tuple(root / relative for relative in CONFIG_FILES)
        return [
            replace(agent, rule_files=(*shared, *agent.rule_files))
            for agent in self._profiles(root, selected)
        ]

    def _profiles(self, root: Path, selected: AgentProfile | None) -> list[AgentProfile]:
        result: list[AgentProfile] = []
        global_fields: dict[str, Any] = {}
        definitions: list[tuple[Path, str, dict[str, Any]]] = []
        problem = None
        for relative in CONFIG_FILES:
            path = root / relative
            try:
                if not reading.is_file_strict(path):
                    continue
                config = reading.json_object(path)
                for key in (
                    "permission",
                    "tools",
                    "model",
                    "instructions",
                    "mcp",
                    "plugin",
                    "hooks",
                ):
                    if key in config:
                        value = config[key]
                        if isinstance(value, dict) and isinstance(global_fields.get(key), dict):
                            value = {**global_fields[key], **value}
                        global_fields[key] = value
                agents = config.get("agent", {})
                if not isinstance(agents, dict):
                    raise reading.SourceError("agent must be an object keyed by name.")
                if selected is not None and path != selected.source_path:
                    continue
                for name, fields in agents.items():
                    if selected is not None and name != selected.display_name:
                        continue
                    if not isinstance(fields, dict):
                        result.append(
                            unavailable(
                                self.source, path, name, "Agent configuration must be an object."
                            )
                        )
                    elif name in _BUILT_IN_AGENTS and "prompt" not in fields:
                        continue
                    else:
                        definitions.append((path, name, fields))
            except (OSError, ValueError) as error:
                problem = str(error)
                if selected is None or path == selected.source_path:
                    name = path.stem if selected is None else selected.display_name
                    result.append(unavailable(self.source, path, name, problem))
        for path, name, fields in definitions:
            try:
                if problem:
                    raise reading.SourceError(problem)
                body = reading.string(fields, "prompt")
                references: tuple[Path, ...] = ()
                if body.startswith("{file:") and body.endswith("}"):
                    reference = (path.parent / body[6:-1]).resolve()
                    if not reference.is_relative_to(root.resolve()):
                        raise reading.SourceError("Prompt file is outside the repository.")
                    body = reading.read_text(reference)
                    references = (reference,)
                converted = self._convert(path, name, fields, body, global_fields)
                result.append(replace(converted, rule_files=references))
            except (OSError, ValueError) as error:
                result.append(unavailable(self.source, path, name, str(error)))
        # Markdown definitions outrank JSON definitions in this adapter. Keep
        # duplicates visible for the source report instead of silently dropping them.
        markdown: list[AgentProfile] = []
        for folder in (".opencode/agents", ".opencode/agent"):
            paths = (
                reading.files(root / folder, ".md", recursive=True)
                if selected is None
                else (
                    [selected.source_path]
                    if selected.source_path.is_relative_to(root / folder)
                    else []
                )
            )
            for path in paths:
                try:
                    fields, body = reading.markdown(path)
                    if problem:
                        raise reading.SourceError(problem)
                    markdown.append(self._convert(path, path.stem, fields, body, global_fields))
                except (OSError, ValueError) as error:
                    markdown.append(unavailable(self.source, path, path.stem, str(error)))
        return [*markdown, *result]

    def _convert(
        self,
        path: Path,
        name: str,
        fields: dict[str, Any],
        body: str,
        global_fields: dict[str, Any],
    ) -> AgentProfile:
        merged = {**global_fields, **fields}
        if fields.get("mode") == "subagent" and "model" not in fields:
            # Subagents run on the calling Agent's Model unless they name one.
            merged["model"] = "inherit"
        agent = profile(
            self.source,
            path,
            merged,
            body,
            name=name,
            effort_key="reasoningEffort",
        )
        if reading.boolean(fields, "disable"):
            return replace(
                agent,
                unavailable_reason="Disabled by its repository definition.",
                translations=(
                    *agent.translations,
                    Translation("disable", "applied", "Agent is unavailable."),
                ),
            )
        states = dict.fromkeys(_MAPPING, True)
        default_access = True
        target_rules: list[AgentTargetRule] = []
        skill_rules: list[AgentTargetRule] = []
        reports = list(agent.translations)
        for layer in (global_fields, fields):
            tools = layer.get("tools", {})
            if not isinstance(tools, dict):
                raise reading.SourceError("tools must be an object of boolean switches.")
            for pattern, enabled in tools.items():
                if not isinstance(enabled, bool):
                    raise reading.SourceError("Tool switches must be booleans.")
                if pattern == "*":
                    default_access = enabled
                for foreign in states:
                    if fnmatchcase(foreign, _capability(pattern)):
                        states[foreign] = enabled
                reports.append(
                    Translation(
                        f"tools.{pattern}",
                        "translated" if _mapped(pattern) else "not_supported",
                        "Applied to matching vBot capabilities.",
                    )
                )
            permissions = layer.get("permission", {})
            if isinstance(permissions, str):
                permissions = {"*": permissions}
            if not isinstance(permissions, dict):
                raise reading.SourceError("permission must be an action or object.")
            for pattern, value in permissions.items():
                rules = permission_rules(value)
                scoped = isinstance(value, dict)
                # Anything the Agent may do, even only for some commands or paths
                # or after approval, needs the whole vBot Tool.
                usable = any(rule.allowed for rule in rules)
                if pattern in {"task", "skill"}:
                    (target_rules if pattern == "task" else skill_rules).extend(rules)
                    if not scoped or usable:
                        states[pattern] = usable
                    reports.append(
                        Translation(
                            f"permission.{pattern}", "translated", "Applied to available names."
                        )
                    )
                    continue
                matches = [
                    foreign for foreign in states if fnmatchcase(foreign, _capability(pattern))
                ]
                if pattern == "edit":
                    matches = ["edit", "write", "apply_patch"]
                if pattern == "*":
                    default_access = _usable_after(default_access, rules)
                for foreign in matches:
                    states[foreign] = _usable_after(states[foreign], rules)
                    # A broad permission also supplies the default for named
                    # task/skill exceptions that follow, across both layers.
                    if foreign == "task":
                        target_rules.append(AgentTargetRule("*", states[foreign]))
                    elif foreign == "skill":
                        skill_rules.append(AgentTargetRule("*", states[foreign]))
                if pattern == "external_directory":
                    continue  # A path guard, not a capability: Permission Rules report it.
                if not matches:
                    detail = "vBot has no such capability."
                elif scoped and any(states[foreign] for foreign in matches):
                    detail = (
                        "Granted as the whole vBot Tool; its patterns become Permission Rules "
                        "where vBot has an equivalent."
                    )
                else:
                    detail = "Applied to matching capabilities."
                reports.append(Translation(f"permission.{pattern}", "translated", detail))
        # Several foreign capabilities can share one vBot Tool; any allowed one grants it.
        requirements: dict[str, list[str]] = {}
        for foreign, mapped in _MAPPING.items():
            for tool in mapped:
                requirements.setdefault(tool, []).append(foreign)
        granted = {
            tool: any(states[foreign] for foreign in required)
            for tool, required in requirements.items()
        }
        tool_rules = [ToolRule("*", default_access)]
        tool_rules.extend(ToolRule(tool, allowed) for tool, allowed in granted.items())
        agent = replace(
            agent,
            denied_tools=frozenset(tool for tool, allowed in granted.items() if not allowed),
            tool_rules=tuple(tool_rules)
            if any("tools" in layer or "permission" in layer for layer in (global_fields, fields))
            else (),
            agent_target_rules=tuple(target_rules),
            skill_rules=tuple(skill_rules),
            translations=tuple(reports),
        )
        agent = _permission_rules(agent, (global_fields, fields))
        if "mode" in fields:
            agent = replace(
                agent,
                translations=(
                    *agent.translations,
                    Translation(
                        "mode", "translated", "Agent is addressable as a vBot Team member."
                    ),
                ),
            )
        return unsupported(
            agent,
            {**global_fields, **fields},
            {
                "name",
                "description",
                "model",
                "temperature",
                "top_p",
                "reasoningEffort",
                "tools",
                "permission",
                "disable",
                "prompt",
                "mode",
            },
            cosmetic=frozenset({"color", "hidden"}),
        )


# OpenCode permissions with a Permission Rule kind, by the permission names that
# address them (``shell`` is the current name, ``bash`` the earlier one).
_RULE_PERMISSIONS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "read": ("Read", ("read",)),
    "edit": ("Edit", ("edit", "write", "apply_patch")),
    "shell": ("Shell", ("shell", "bash")),
    "webfetch": ("Web", ("webfetch",)),
    "external_directory": (None, ("external_directory",)),
}
# What a wildcard denial with later allow exceptions narrows, per permission.
_LISTED: dict[str, tuple[tuple[str, str, str], ...]] = {
    "read": (("read", "work_area", "Reads are limited to the Work Area and the allow patterns"),),
    "edit": (("write", "scratch", "File changes are limited to the allow patterns"),),
    "shell": (("commands", "listed", "Commands are limited to the allow patterns"),),
    "webfetch": (("web", "listed", "Web access is limited to the allow patterns"),),
    "external_directory": (
        ("read", "work_area", "Reads outside the Work Area are limited to the allow patterns"),
        ("write", "work_area", "Changes outside the Work Area are limited to the allow patterns"),
    ),
}
_ABSOLUTE = re.compile(r"(?:[A-Za-z]:)?[/\\]")


def _permission_rules(agent: AgentProfile, layers: tuple[dict[str, Any], ...]) -> AgentProfile:
    """Translate OpenCode ``permission`` patterns into Permission Rules.

    The rules of each permission apply in order, global before the Agent's own,
    and the last matching one wins (https://opencode.ai/docs/permissions/). After
    the last ``"*"`` rule, deny patterns become deny rules; a ``"*"`` denial with
    later allow exceptions narrows the base to those exceptions (commands or hosts
    listed, writes to the Scratch Area plus the exceptions, reads to the Work
    Area plus the exceptions). ``external_directory: deny`` keeps the Agent in its
    Work Area. ``ask`` counts as allowed: vBot asks no approvals.
    """
    draft = RuleDraft()
    for name, (kind, keys) in _RULE_PERMISSIONS.items():
        ordered: list[tuple[str, str]] = []
        written: list[str] = []
        for layer in layers:
            permissions = layer.get("permission", {})
            if isinstance(permissions, str):
                permissions = {"*": permissions}
            for key, value in permissions.items():
                if any(fnmatchcase(candidate, key) for candidate in keys):
                    entries = {"*": value} if isinstance(value, str) else value
                    ordered.extend(entries.items())
                    if key != "*" and key not in written:
                        written.append(key)
        if not ordered:
            continue
        # Rows carry the name the repository wrote (``bash`` or ``shell``).
        label = written[0] if written else name
        if any(action == "ask" for _pattern, action in ordered):
            draft.report(f"permission.{label}", "not_supported", ASK_DETAIL)
        last_any = max(
            (index for index, (pattern, _action) in enumerate(ordered) if pattern == "*"),
            default=-1,
        )
        denied_by_default = last_any >= 0 and ordered[last_any][1] == "deny"
        tail = ordered[last_any + 1 :]
        exceptions = [pattern for pattern, action in tail if action != "deny"]
        if denied_by_default and (exceptions or kind is None):
            for part, value, detail in _LISTED[name]:
                draft.narrow(part, value, f"permission.{label}", f"{detail} ({part}: {value}).")
            for pattern in exceptions:
                _rule(draft, "allow", label, kind or "Edit", pattern)
        for pattern, action in tail:
            if action == "deny":
                _rule(draft, "deny", label, kind or "Read", pattern)
        first_denial = next(
            (index for index, (_pattern, action) in enumerate(tail) if action == "deny"), None
        )
        if first_denial is not None and any(
            action != "deny" for _pattern, action in tail[first_denial + 1 :]
        ):
            draft.report(
                f"permission.{label}",
                "translated",
                "A vBot deny rule always wins, so an allow pattern after a deny pattern "
                "does not reopen what the deny covers.",
            )
    return draft.apply(agent)


def _rule(draft: RuleDraft, effect: str, name: str, kind: str, pattern: str) -> None:
    setting = f"permission.{name}.{pattern}"
    detail = ""
    if kind == "Shell":
        translated = pattern
    elif kind == "Web":
        # OpenCode matches the URL; a vBot host rule names its host.
        address = pattern.split("://", 1)[1] if "://" in pattern else pattern
        translated, _, path = address.partition("/")
        if path.strip("*"):
            detail = "It covers the whole host."
    else:
        translated = _path(pattern)
    draft.rule(effect, f"{kind}({translated})", setting, detail=detail)


def _path(pattern: str) -> str:
    """An OpenCode path pattern in vBot's form.

    OpenCode's ``*`` also matches across folders, so a segment with a ``*``
    matches at any depth below the part before it. A relative pattern names
    paths in the working directory; ``~`` and ``$HOME`` start in the home folder.
    """
    text = pattern.strip().replace("\\", "/")
    if text.startswith("$HOME"):
        text = "~" + text[len("$HOME") :]
    if text == "~" or text.startswith("~/"):
        anchor, rest = "~", text[1:].lstrip("/")
    elif _ABSOLUTE.match(text):
        anchor, _, rest = text.partition("/")
    else:
        anchor, rest = "{workdir}", text.removeprefix("./")
    segments: list[str] = []
    for segment in rest.split("/"):
        if segment == "*":
            segment = "**"
        elif "*" in segment and segment != "**" and segments[-1:] != ["**"]:
            segments.append("**")
        segments.append(segment)
    return "/".join([anchor, *segments]).rstrip("/") or "/"


def _capability(pattern: str) -> str:
    """OpenCode now names the shell ``shell``; ``bash`` is its earlier name."""
    return "bash" if pattern == "shell" else pattern


def _usable_after(previous: bool, rules: tuple[AgentTargetRule, ...]) -> bool:
    """Whether a capability may still do something after these ordered rules.

    The last matching rule wins: after a ``"*"`` rule only it and later allow (or
    ask) exceptions count; rules for named patterns alone keep what was usable
    and can add to it, since everything else keeps its earlier action.
    """
    last_any = max((index for index, rule in enumerate(rules) if rule.pattern == "*"), default=-1)
    if last_any < 0:
        return previous or any(rule.allowed for rule in rules)
    return any(rule.allowed for rule in rules[last_any:])


def _mapped(pattern: str) -> frozenset[str]:
    if pattern == "*":
        return frozenset({"*"})
    return frozenset(
        tool
        for name, mapped in _MAPPING.items()
        if fnmatchcase(name, _capability(pattern))
        for tool in mapped
    )
