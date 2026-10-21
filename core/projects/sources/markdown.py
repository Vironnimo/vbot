"""Adapters for Claude Code, Copilot, Cursor and Gemini repository Agents."""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    ASK_DETAIL,
    READ_ONLY_TOOLS,
    SHELL_TOOLS,
    RuleDraft,
    profile,
    tool_list,
    unavailable,
    unsupported,
)
from core.projects.sources.profile import AgentProfile, AgentTargetRule, Translation

_CLAUDE_TOOLS = {
    "read": frozenset({"read"}),
    "edit": frozenset({"apply_patch"}),
    "write": frozenset({"apply_patch"}),
    "glob": frozenset({"search_files"}),
    "grep": frozenset({"search_files"}),
    "bash": frozenset({"bash"}),
    "powershell": frozenset({"bash"}),
    "webfetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "agent": frozenset({"subagent"}),
    "skill": frozenset({"skill"}),
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
    "search_file_content": frozenset({"search_files"}),
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
        settings, problem = self._settings(root)
        return [
            agent for path in paths if (agent := self._read(path, settings, problem)) is not None
        ]

    def read(self, root: Path, selected: AgentProfile) -> AgentProfile | None:
        if not reading.is_source_file(root / self.folder, selected.source_path):
            return None
        settings, problem = self._settings(root)
        return self._read(
            selected.source_path, settings, problem, unavailable_name=selected.display_name
        )

    def _settings(self, root: Path) -> tuple[dict[str, Any], str | None]:
        settings: dict[str, Any] = {"other": []}
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
                            if key in {"allow", "deny", "ask", "additionalDirectories"}:
                                settings[key] = [*settings.get(key, []), *reading.names(value)]
                            elif key == "defaultMode":
                                settings[key] = value
                            elif key == "blockReadsOutsideWorkingDirectories":
                                settings[key] = reading.boolean(permissions, key)
                            elif key not in settings["other"]:
                                settings["other"].append(key)
            except (OSError, ValueError) as error:
                problem = str(error)
        return settings, problem

    def _read(
        self,
        path: Path,
        settings: dict[str, Any],
        problem: str | None,
        *,
        unavailable_name: str | None = None,
    ) -> AgentProfile | None:
        name = path.stem.removesuffix(".agent")
        try:
            if not reading.has_frontmatter(path) and not path.name.endswith(".agent.md"):
                return None  # Agent definitions declare frontmatter; other files document.
            fields, body = reading.markdown(path)
            name = reading.string(fields, "name", name)
            if problem:
                raise reading.SourceError(problem)
            return self._convert(path, fields, body, settings)
        except (OSError, ValueError) as error:
            return unavailable(self.source, path, unavailable_name or name, str(error))

    def _convert(
        self, path: Path, fields: dict[str, Any], body: str, settings: dict[str, Any]
    ) -> AgentProfile:
        agent = profile(
            self.source,
            path,
            fields,
            body,
            # Claude, Cursor and Gemini subagents use the caller's Model unless named.
            model_default="inherit" if self.source != "copilot" else "",
        )
        known = {"name", "description", "model", "temperature", "top_p", "effort", "tools"}
        if self.source == "claude":
            if "tools" in fields:
                agent = tool_list(agent, fields["tools"], _CLAUDE_TOOLS)
            if "disallowedTools" in fields:
                agent = tool_list(
                    agent,
                    fields["disallowedTools"],
                    _CLAUDE_TOOLS,
                    deny=True,
                    setting="disallowedTools",
                    translated_scopes=_TRANSLATED_SCOPES,
                )
            if "deny" in settings:
                agent = tool_list(
                    agent,
                    settings["deny"],
                    _CLAUDE_TOOLS,
                    deny=True,
                    setting="permissions.deny",
                    translated_scopes=_TRANSLATED_SCOPES,
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
                    agent,
                    settings.get("allow", []),
                    _CLAUDE_TOOLS,
                    setting="permissions.allow",
                    translated_scopes=_TRANSLATED_SCOPES,
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
                            "vBot asks no approvals; the Agent keeps its Tools.",
                        ),
                    ),
                )
            elif mode:
                raise reading.SourceError("Unknown permissionMode.")
            agent = _claude_permission_rules(agent, fields, settings, mode)
            known |= {"disallowedTools", "skills", "permissionMode"}
        elif self.source == "copilot":
            if "tools" in fields:
                agent = tool_list(agent, fields["tools"], _COPILOT_TOOLS, wildcard=True)
            if "agents" in fields:
                targets = reading.names(fields["agents"])
                if "*" not in targets:
                    agent = replace(
                        agent,
                        agent_target_rules=(
                            *agent.agent_target_rules,
                            AgentTargetRule("*", False),
                            *(AgentTargetRule(target, True) for target in targets),
                        ),
                        translations=(
                            *agent.translations,
                            Translation("agents", "translated", "Limits delegation targets."),
                        ),
                    )
            known.add("agents")
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
                )
            agent = replace(agent, denied_tools=agent.denied_tools | {"subagent"})
            known.add("kind")
        return unsupported(agent, fields, known)


# Claude Code rule kinds with a vBot Permission Rule kind. Claude Code itself
# consults only Read and Edit path rules; a Write, Glob or similar path rule still
# says what the repository means to keep the Agent from.
_CLAUDE_RULE_KINDS = {
    "read": "Read",
    "glob": "Read",
    "grep": "Read",
    "ls": "Read",
    "edit": "Edit",
    "write": "Edit",
    "multiedit": "Edit",
    "notebookedit": "Edit",
    "bash": "Shell",
    "powershell": "Shell",
    "webfetch": "Web",
}
_TRANSLATED_SCOPES = frozenset(_CLAUDE_RULE_KINDS)
_PATH_KINDS = frozenset({"Read", "Edit"})
_SINGLE_SEGMENT_DIRECTORY = re.compile(r"[^/]+/\*\*")
_WINDOWS_DRIVE = re.compile(r"([A-Za-z])(?:/(.*))?", re.DOTALL)
_ABSOLUTE = re.compile(r"(?:[A-Za-z]:)?/")


def _claude_permission_rules(
    agent: AgentProfile, fields: dict[str, Any], settings: dict[str, Any], mode: str
) -> AgentProfile:
    """Translate Claude Code's permission settings into Permission Rules.

    Scoped deny entries (``permissions.deny`` and the Agent's ``disallowedTools``)
    become deny rules in every mode. ``dontAsk`` refuses whatever its allow rules
    do not pre-approve: reads outside the working directories, and commands,
    hosts and file changes where the Agent keeps the Tool only through scoped
    allow rules. ``additionalDirectories`` widen such a restriction; ``ask`` rules
    are not translated.
    """
    draft = RuleDraft()
    for setting, value in (
        ("permissions.deny", settings.get("deny", [])),
        ("disallowedTools", fields.get("disallowedTools", [])),
    ):
        for entry in reading.names(value, comma_separated=True):
            scoped = _claude_scope(entry)
            if scoped is not None:
                _claude_rule(draft, "deny", *scoped, f"{setting}.{entry}")
    if settings.get("ask"):
        draft.report("permissions.ask", "not_supported", ASK_DETAIL)
    for key in settings["other"]:
        draft.report(f"permissions.{key}", "not_supported", "This setting has no vBot equivalent.")
    directory_kind = "Edit"
    if mode == "dontAsk":
        mode_setting = "permissionMode" if "permissionMode" in fields else "defaultMode"
        # Reads in the working directories run; everything else needs an allow rule.
        directory_kind = "Read"
        whole: set[str] = set()
        scoped_kinds: set[str] = set()
        for entry in settings.get("allow", []):
            scoped = _claude_scope(entry)
            if scoped is None:
                kind = _CLAUDE_RULE_KINDS.get(entry.strip().partition("(")[0].strip().lower())
                if kind is not None:
                    whole.add(kind)
            elif _claude_rule(draft, "allow", *scoped, f"permissions.allow.{entry}"):
                scoped_kinds.add(scoped[0])
        if "Read" not in whole:
            draft.narrow(
                "read",
                "work_area",
                mode_setting,
                "dontAsk reads only the working directories and what allow rules name "
                "(read: work_area).",
            )
        for kind, part, value, detail in (
            ("Edit", "write", "scratch", "File changes are limited to the Edit allow rules"),
            ("Shell", "commands", "listed", "Commands are limited to the Bash allow rules"),
            ("Web", "web", "listed", "Web access is limited to the WebFetch allow rules"),
        ):
            if kind in scoped_kinds and kind not in whole:
                draft.narrow(part, value, mode_setting, f"{detail} ({part}: {value}).")
        if "Shell" in scoped_kinds and "Shell" not in whole:
            draft.report(
                mode_setting,
                "translated",
                "Commands Claude Code runs without asking, such as ls or git status, need "
                "Shell allow rules in vBot.",
            )
    if settings.get("blockReadsOutsideWorkingDirectories"):
        draft.narrow(
            "read",
            "work_area",
            "permissions.blockReadsOutsideWorkingDirectories",
            "Reads are limited to the working directories (read: work_area).",
        )
    for directory in settings.get("additionalDirectories", []):
        draft.widen(
            f"{directory_kind}({_claude_directory(directory)})",
            f"permissions.additionalDirectories.{directory}",
        )
    return draft.apply(agent)


def _claude_scope(entry: str) -> tuple[str, str] | None:
    """The vBot kind and Claude Code pattern of a scoped rule entry, if it has one."""
    base, separator, rest = entry.strip().partition("(")
    kind = _CLAUDE_RULE_KINDS.get(base.strip().lower())
    if kind is None or not separator or not rest.endswith(")") or not rest[:-1].strip():
        return None
    return kind, rest[:-1].strip()


def _claude_rule(draft: RuleDraft, effect: str, kind: str, pattern: str, setting: str) -> bool:
    detail = ""
    if kind in _PATH_KINDS:
        if pattern.startswith("!"):
            draft.report(
                setting,
                "not_supported",
                "Not translated: vBot rules have no exceptions, so the rules this one "
                "carves from apply in full.",
            )
            return False
        translated = _claude_path(pattern, deny=effect == "deny")
        if translated is None:
            draft.report(
                setting,
                "not_supported",
                "Not translated: a vBot rule on Windows names one drive, not every drive.",
            )
            return False
    elif kind == "Web":
        translated = pattern.removeprefix("domain:").strip()
        if not translated.startswith("*"):
            # Claude Code matches exactly this host; a vBot host rule covers subdomains too.
            detail = "It also covers the host's subdomains."
    else:
        translated = pattern
    return draft.rule(effect, f"{kind}({translated})", setting, detail=detail)


def _claude_path(pattern: str, *, deny: bool) -> str | None:
    """A Claude Code path pattern in vBot's form (code.claude.com/docs/en/permissions).

    ``//path`` is absolute (on Windows ``//c/...`` names drive C:), ``~/path`` is
    in the home directory, ``/path`` is relative to the project whose settings
    file holds the rule, and ``path`` or ``./path`` is relative to the working
    directory with gitignore depth: a bare name matches at any depth, and so does
    a single directory segment such as ``secrets/**`` in a deny rule. ``None`` when
    vBot cannot express the pattern.
    """
    if pattern.startswith("//"):
        rest = pattern[2:]
        if sys.platform != "win32":
            return "/" + rest
        drive = _WINDOWS_DRIVE.fullmatch(rest)
        if drive is None:
            return None
        return f"{drive.group(1).upper()}:/{drive.group(2) or ''}"
    if pattern == "~" or pattern.startswith("~/"):
        return pattern
    if pattern.startswith("/"):
        return "{project}" + pattern.rstrip("/")
    body = pattern
    while body.startswith("./"):
        body = body[2:]
    body = body.rstrip("/")
    if body in ("", "."):
        return "{workdir}"
    if "/" not in body or (deny and _SINGLE_SEGMENT_DIRECTORY.fullmatch(body)):
        return "{workdir}/**/" + body
    return "{workdir}/" + body


def _claude_directory(directory: str) -> str:
    """An ``additionalDirectories`` entry, an ordinary path; relative to the working directory."""
    text = directory.strip().replace("\\", "/").rstrip("/")
    if not (text == "~" or text.startswith("~/") or _ABSOLUTE.match(text + "/")):
        text = "{workdir}/" + text.removeprefix("./")
    return text + "/**"
