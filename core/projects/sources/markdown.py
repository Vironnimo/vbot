"""Adapters for Claude Code, Copilot, Cursor and Gemini repository Agents."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    FILE_TOOLS,
    READ_ONLY_TOOLS,
    SHELL_TOOLS,
    profile,
    restrict,
    tool_list,
    unavailable,
    unsupported,
)
from core.projects.sources.profile import AgentProfile, Translation

_CLAUDE_TOOLS = {
    "read": frozenset({"read"}),
    "edit": frozenset({"apply_patch"}),
    "write": frozenset({"apply_patch"}),
    "glob": frozenset({"search_files"}),
    "grep": frozenset({"search_files"}),
    "bash": frozenset({"bash"}),
    "webfetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "agent": frozenset({"subagent"}),
    "skill": frozenset({"skill"}),
}
# Claude's own Tool names, which hook matchers are written against.
_CLAUDE_TOOL_NAMES = {
    "Read": "read",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Write": "write",
    "NotebookEdit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "Bash": "bash",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "Agent": "agent",
    "Task": "agent",
    "Skill": "skill",
}
# Only these hook events can block or rewrite a Tool call; the others observe.
_GATING_HOOKS = ("PreToolUse", "PermissionRequest")
_COMPOUND = {
    "apply_patch": frozenset({"edit", "write"}),
    "search_files": frozenset({"grep", "glob"}),
}
_COPILOT_TOOLS = {
    **_CLAUDE_TOOLS,
    "execute": frozenset({"bash"}),
    "shell": frozenset({"bash"}),
    "powershell": frozenset({"bash"}),
    "search": frozenset({"search_files"}),
    "web": frozenset({"web_fetch", "web_search"}),
    "custom-agent": frozenset({"subagent"}),
    "multiedit": frozenset({"apply_patch"}),
    "notebookread": frozenset({"read"}),
    "notebookedit": frozenset({"apply_patch"}),
}
_GEMINI_TOOLS = {
    "read_file": frozenset({"read"}),
    "read_many_files": frozenset({"read"}),
    "list_directory": frozenset({"search_files"}),
    "glob": frozenset({"search_files"}),
    "grep_search": frozenset({"search_files"}),
    "run_shell_command": frozenset({"bash"}),
    "replace": frozenset({"apply_patch"}),
    "write_file": frozenset({"apply_patch"}),
    "web_fetch": frozenset({"web_fetch"}),
    "google_web_search": frozenset({"web_search"}),
    "activate_skill": frozenset({"skill"}),
}


class MarkdownAdapter:
    def __init__(self, source: str, folder: str, *, suffix: str = ".md") -> None:
        self.source = source
        self.folder = folder
        self.suffix = suffix

    def scan(self, root: Path) -> list[AgentProfile]:
        paths = reading.files(root / self.folder, self.suffix, recursive=True)
        settings: dict[str, Any] = {}
        problem = None
        if self.source == "claude":
            try:
                for relative in (".claude/settings.json", ".claude/settings.local.json"):
                    path = root / relative
                    if reading.is_file_strict(path):
                        # Permissions compose; a local deny must retain a shared deny.
                        data = reading.json_object(path)
                        permissions = data.get("permissions", {})
                        if not isinstance(permissions, dict):
                            raise reading.SourceError("permissions must be an object.")
                        for key, value in permissions.items():
                            if key in {"allow", "deny", "ask"}:
                                settings[key] = [*settings.get(key, []), *reading.names(value)]
                            elif key == "defaultMode":
                                settings[key] = value
                        if data.get("hooks"):
                            settings["hooks"] = [*settings.get("hooks", []), data["hooks"]]
            except (OSError, ValueError) as error:
                problem = str(error)
        result: list[AgentProfile] = []
        for path in paths:
            name = path.stem.removesuffix(".agent")
            try:
                fields, body = reading.markdown(path)
                name = reading.string(fields, "name", name)
                if problem:
                    raise reading.SourceError(problem)
                result.append(self._convert(path, fields, body, settings))
            except (OSError, ValueError) as error:
                result.append(unavailable(self.source, path, name, str(error)))
        return result

    def _convert(
        self, path: Path, fields: dict[str, Any], body: str, settings: dict[str, Any]
    ) -> AgentProfile:
        agent = profile(
            self.source,
            path,
            fields,
            body,
            model_default="inherit" if self.source in {"cursor", "gemini"} else "",
        )
        known = {"name", "description", "model", "temperature", "top_p", "effort", "tools"}
        if self.source == "claude":
            if "tools" in fields:
                agent = tool_list(agent, fields["tools"], _CLAUDE_TOOLS, compound=_COMPOUND)
            if "disallowedTools" in fields:
                agent = tool_list(
                    agent,
                    fields["disallowedTools"],
                    _CLAUDE_TOOLS,
                    deny=True,
                    setting="disallowedTools",
                )
            for key in ("deny", "ask"):
                if key in settings:
                    agent = tool_list(
                        agent, settings[key], _CLAUDE_TOOLS, deny=True, setting=f"permissions.{key}"
                    )
            if "skills" in fields:
                agent = replace(
                    agent,
                    preload_skills=tuple(reading.names(fields["skills"])),
                    translations=(
                        *agent.translations,
                        Translation(
                            "skills",
                            "translated",
                            "Skills are preloaded from the effective Project pool.",
                        ),
                    ),
                )
            mode = reading.string(fields, "permissionMode", settings.get("defaultMode", ""))
            if mode == "plan":
                agent = replace(
                    agent,
                    allowed_tools=(
                        READ_ONLY_TOOLS if agent.allowed_tools is None else agent.allowed_tools
                    )
                    & READ_ONLY_TOOLS,
                    denied_tools=agent.denied_tools | SHELL_TOOLS | {"apply_patch"},
                    translations=(
                        *agent.translations,
                        Translation(
                            "permissionMode",
                            "translated",
                            "Plan mode permits only read and search capabilities.",
                        ),
                    ),
                )
            elif mode == "dontAsk":
                # dontAsk refuses every Tool the allow rules do not pre-approve.
                selected = agent.allowed_tools
                agent = tool_list(
                    agent, settings.get("allow", []), _CLAUDE_TOOLS, setting="permissions.allow"
                )
                if selected is not None and agent.allowed_tools is not None:
                    agent = replace(agent, allowed_tools=agent.allowed_tools & selected)
            elif mode in {"default", "acceptEdits", "auto", "bypassPermissions"}:
                agent = replace(
                    agent,
                    translations=(
                        *agent.translations,
                        Translation(
                            "permissionMode",
                            "translated",
                            "vBot asks no approvals; the Agent's vBot Tool access applies.",
                        ),
                    ),
                )
            elif mode:
                raise reading.SourceError("Unknown permissionMode.")
            for hooks in settings.get("hooks", []):
                agent = _gating_hooks(agent, hooks)
            if "hooks" in fields:
                agent = _gating_hooks(agent, fields["hooks"], own=True)
            if "isolation" in fields:
                agent = restrict(
                    agent,
                    "isolation",
                    FILE_TOOLS,
                    "Worktree isolation is unavailable; file and shell Tools are disabled.",
                )
            known |= {"disallowedTools", "skills", "permissionMode", "hooks", "isolation"}
        elif self.source == "copilot":
            if "tools" in fields:
                agent = tool_list(agent, fields["tools"], _COPILOT_TOOLS, wildcard=True)
        elif self.source == "cursor":
            known.discard("tools")
            if reading.boolean(fields, "readonly"):
                agent = replace(
                    agent,
                    allowed_tools=READ_ONLY_TOOLS,
                    translations=(
                        *agent.translations,
                        Translation(
                            "readonly",
                            "translated",
                            "File edits and shell Tools are disabled; only read capabilities "
                            "remain.",
                        ),
                    ),
                )
            known.add("readonly")
        elif self.source == "gemini":
            if reading.string(fields, "kind", "local") != "local":
                raise reading.SourceError("Remote Agents cannot run as local vBot Agents.")
            if "tools" in fields:
                agent = tool_list(
                    agent,
                    fields["tools"],
                    _GEMINI_TOOLS,
                    wildcard=True,
                    compound={
                        "apply_patch": frozenset({"replace", "write_file"}),
                        "search_files": frozenset({"glob", "grep_search"}),
                    },
                )
            agent = replace(agent, denied_tools=agent.denied_tools | {"subagent"})
            known.add("kind")
        return unsupported(agent, fields, known)


def _gating_hooks(agent: AgentProfile, hooks: Any, *, own: bool = False) -> AgentProfile:
    """Disable the Tools a blocking hook would gate; vBot cannot run hooks.

    Project-wide hooks that only observe (session start, after a Tool call) belong to
    Claude Code's session, not to the Agent, and pass without a report.
    """
    if not isinstance(hooks, dict):
        raise reading.SourceError("hooks must be an object.")
    matched: set[str] = set()
    for event in _GATING_HOOKS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise reading.SourceError(f"hooks.{event} must be a list.")
        for entry in entries:
            matcher = entry.get("matcher", "") if isinstance(entry, dict) else None
            if not isinstance(matcher, str):
                raise reading.SourceError(f"hooks.{event} matchers must be strings.")
            if matcher in {"", "*"}:
                matched.update(_CLAUDE_TOOL_NAMES)
                continue
            try:
                pattern = re.compile(matcher)
            except re.error as error:
                raise reading.SourceError(f"Invalid hook matcher: {matcher}") from error
            matched.update(name for name in _CLAUDE_TOOL_NAMES if pattern.search(name))
    if not matched and not own:
        return agent
    reports = list(agent.translations)
    denied = set(agent.denied_tools)
    for name in sorted(matched):
        denied |= _CLAUDE_TOOLS[_CLAUDE_TOOL_NAMES[name]]
        if name == "Bash":
            denied |= SHELL_TOOLS
    if matched:
        reports.append(
            Translation(
                "hooks",
                "not_supported",
                "Hooks cannot run in vBot; the Tools they gate are disabled: "
                + ", ".join(sorted(matched))
                + ".",
            )
        )
    else:
        reports.append(
            Translation("hooks", "not_supported", "Hooks do not run in vBot; no Tool is gated.")
        )
    return replace(agent, denied_tools=frozenset(denied), translations=tuple(reports))
