"""Adapters for Claude Code, Copilot, Cursor and Gemini repository Agents."""

from __future__ import annotations

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
from core.projects.sources.profile import AgentProfile, ToolRule, Translation

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
                            if key in {"deny", "ask"}:
                                settings[key] = [*settings.get(key, []), *reading.names(value)]
                            elif key == "defaultMode":
                                settings[key] = value
                        if data.get("hooks"):
                            settings["hooks"] = data["hooks"]
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
            elif mode in {"default", "manual", "auto", "acceptEdits"}:
                agent = replace(
                    agent,
                    tool_rules=(*agent.tool_rules, ToolRule("*", False)),
                    translations=(
                        *agent.translations,
                        Translation(
                            "permissionMode",
                            "not_supported",
                            "Approval modes are unavailable; Tools are disabled until a vBot "
                            "override is set.",
                        ),
                    ),
                )
            elif mode in {"dontAsk", "bypassPermissions"}:
                agent = replace(
                    agent,
                    translations=(
                        *agent.translations,
                        Translation(
                            "permissionMode",
                            "translated",
                            "vBot permissions and Project ceilings still apply.",
                        ),
                    ),
                )
            elif mode:
                raise reading.SourceError("Unknown permissionMode.")
            if "hooks" in fields or "hooks" in settings:
                agent = replace(
                    agent,
                    tool_rules=(*agent.tool_rules, ToolRule("*", False)),
                    translations=(
                        *agent.translations,
                        Translation(
                            "hooks",
                            "not_supported",
                            "Hooks cannot enforce their restrictions; Tools are disabled.",
                        ),
                    ),
                )
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
