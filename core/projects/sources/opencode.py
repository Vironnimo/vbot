"""OpenCode Markdown and JSON Agents, with fail-closed permission translation."""

from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    FILE_TOOLS,
    SHELL_TOOLS,
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
    "webfetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "task": frozenset({"subagent"}),
    "skill": frozenset({"skill"}),
}


class OpenCodeAdapter:
    source = "opencode"

    def scan(self, root: Path) -> list[AgentProfile]:
        result: list[AgentProfile] = []
        global_fields: dict[str, Any] = {}
        definitions: list[tuple[Path, str, dict[str, Any]]] = []
        problem = None
        for relative in ("opencode.json", ".opencode/opencode.json"):
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
                for name, fields in agents.items():
                    if not isinstance(fields, dict):
                        result.append(
                            unavailable(
                                self.source, path, name, "Agent configuration must be an object."
                            )
                        )
                    else:
                        definitions.append((path, name, fields))
            except (OSError, ValueError) as error:
                problem = str(error)
                result.append(unavailable(self.source, path, path.stem, problem))
        for path, name, fields in definitions:
            try:
                if problem:
                    raise reading.SourceError(problem)
                body = reading.string(fields, "prompt")
                if body.startswith("{file:") and body.endswith("}"):
                    reference = (path.parent / body[6:-1]).resolve()
                    if not reference.is_relative_to(root.resolve()):
                        raise reading.SourceError("Prompt file is outside the repository.")
                    body = reading.read_text(reference)
                result.append(self._convert(path, name, fields, body, global_fields))
            except (OSError, ValueError) as error:
                result.append(unavailable(self.source, path, name, str(error)))
        # Markdown definitions outrank JSON definitions in this adapter. Keep
        # duplicates visible for the source report instead of silently dropping them.
        markdown: list[AgentProfile] = []
        for folder in (".opencode/agents", ".opencode/agent"):
            for path in reading.files(root / folder, ".md", recursive=True):
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
        agent = profile(
            self.source,
            path,
            {**global_fields, **fields},
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
        blocked: set[str] = set()
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
                    if fnmatchcase(foreign, pattern):
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
                if pattern in {"task", "skill"}:
                    (target_rules if pattern == "task" else skill_rules).extend(rules)
                    if not scoped:
                        states[pattern] = value == "allow"
                    approval = any(not rule.allowed for rule in rules) and (
                        value == "ask" or scoped and "ask" in value.values()
                    )
                    reports.append(
                        Translation(
                            f"permission.{pattern}",
                            "not_supported" if approval else "translated",
                            "Applied to available names; approval requests are denied.",
                        )
                    )
                    continue
                matches = [foreign for foreign in states if fnmatchcase(foreign, pattern)]
                if pattern == "edit":
                    matches = ["edit", "write", "apply_patch"]
                if pattern == "*":
                    default_access = value == "allow"
                if scoped:
                    for foreign in matches:
                        states[foreign] = False
                    if pattern in {"read", "external_directory"}:
                        blocked.update(FILE_TOOLS)
                    reports.append(
                        Translation(
                            f"permission.{pattern}",
                            "not_supported",
                            "Per-command/path rules are unavailable; affected Tools are disabled.",
                        )
                    )
                else:
                    for foreign in matches:
                        states[foreign] = value == "allow"
                    reports.append(
                        Translation(
                            f"permission.{pattern}",
                            "translated" if matches and value != "ask" else "not_supported",
                            "Applied to matching capabilities; approval requests disable Tools.",
                        )
                    )
                if not matches and value != "allow":
                    default_access = False
                    blocked.update(tool for mapped in _MAPPING.values() for tool in mapped)
        # Several foreign capabilities can share one vBot Tool. Every capability
        # it supplies must be allowed, so a later Glob allow cannot undo Grep deny.
        requirements: dict[str, list[str]] = {}
        for foreign, mapped in _MAPPING.items():
            for tool in mapped:
                requirements.setdefault(tool, []).append(foreign)
        tool_rules = [ToolRule("*", default_access)]
        tool_rules.extend(
            ToolRule(tool, all(states[foreign] for foreign in required) and tool not in blocked)
            for tool, required in requirements.items()
        )
        for tool in blocked:
            tool_rules.append(ToolRule(tool, False))
        agent = replace(
            agent,
            denied_tools=frozenset(
                tool
                for tool, required in requirements.items()
                if tool in blocked or not all(states[foreign] for foreign in required)
            ),
            tool_rules=tuple(tool_rules)
            if any("tools" in layer or "permission" in layer for layer in (global_fields, fields))
            else (),
            agent_target_rules=tuple(target_rules),
            skill_rules=tuple(skill_rules),
            translations=tuple(reports),
        )
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


def _mapped(pattern: str) -> frozenset[str]:
    if pattern == "*":
        return frozenset({"*"})
    return frozenset(
        tool for name, mapped in _MAPPING.items() if fnmatchcase(name, pattern) for tool in mapped
    )
