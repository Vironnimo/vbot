"""Codex TOML Agent definitions, including config-file role references."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    SHELL_TOOLS,
    profile,
    restrict,
    unavailable,
    unsupported,
)
from core.projects.sources.profile import AgentProfile, Translation

# Permission profiles that keep the Agent from editing files at all.
_READ_ONLY = frozenset({"read-only", ":read-only"})


class CodexAdapter:
    source = "codex"

    def scan(self, root: Path) -> list[AgentProfile]:
        return self._scan(root)

    def read(self, root: Path, selected: AgentProfile) -> AgentProfile | None:
        if not reading.is_file_strict(selected.source_path):
            return None
        return next(iter(self._scan(root, selected)), None)

    def _scan(self, root: Path, selected: AgentProfile | None = None) -> list[AgentProfile]:
        paths = (
            reading.files(root / ".codex/agents", ".toml", recursive=True)
            if selected is None
            else (
                [selected.source_path]
                if selected.source_path.name.endswith(".toml")
                and reading.is_source_file(root / ".codex/agents", selected.source_path)
                else []
            )
        )
        definitions: list[tuple[Path, dict[str, Any]]] = []
        result: list[AgentProfile] = []
        referenced: set[Path] = set()
        inherited: dict[str, Any] = {}
        config_path = root / ".codex/config.toml"
        try:
            if reading.is_file_strict(config_path):
                config = tomllib.loads(reading.read_text(config_path))
                inherited = {
                    key: config[key]
                    for key in (
                        "sandbox_mode",
                        "approval_policy",
                        "model",
                        "model_reasoning_effort",
                        "mcp_servers",
                        "hooks",
                        "sandbox_workspace_write",
                        "shell_environment_policy",
                        "network",
                        "skills",
                        "features",
                        "default_permissions",
                        "permissions",
                        "web_search",
                    )
                    if key in config
                }
                roles = config.get("agents", {})
                if not isinstance(roles, dict):
                    raise reading.SourceError("agents must be a table.")
                for name, role in roles.items():
                    if not isinstance(role, dict):
                        continue  # Collection limits are not Agent definitions.
                    raw_path = reading.string(role, "config_file")
                    path = (config_path.parent / raw_path).resolve() if raw_path else config_path
                    if not path.is_relative_to(root.resolve()):
                        raise reading.SourceError("Agent config_file is outside the repository.")
                    fields = tomllib.loads(reading.read_text(path)) if raw_path else {}
                    fields = {
                        **fields,
                        "name": name,
                        "description": reading.string(role, "description"),
                    }
                    if selected is None or (
                        path == selected.source_path and name == selected.display_name
                    ):
                        definitions.append((path, fields))
                    referenced.add(path)
        except (OSError, ValueError) as error:
            if selected is not None:
                return [
                    unavailable(
                        self.source, selected.source_path, selected.display_name, str(error)
                    )
                ]
            result.append(unavailable(self.source, config_path, "codex-config", str(error)))
            # A broken repository config cannot turn its files into unrestricted Agents.
            return [
                *result,
                *(unavailable(self.source, path, path.stem, str(error)) for path in paths),
            ]
        for path in paths:
            if path.resolve() in referenced:
                continue
            try:
                definitions.append((path, tomllib.loads(reading.read_text(path))))
            except (OSError, ValueError) as error:
                name = path.stem if selected is None else selected.display_name
                result.append(unavailable(self.source, path, name, str(error)))
        for path, fields in definitions:
            try:
                fields = {**inherited, **fields}
                body = reading.string(fields, "developer_instructions")
                normalized = dict(fields)
                if "model_reasoning_effort" in fields:
                    normalized["effort"] = fields["model_reasoning_effort"]
                agent = profile(self.source, path, normalized, body)
                agent = _restrictions(agent, fields)
                agent = unsupported(
                    agent,
                    fields,
                    {
                        "name",
                        "description",
                        "model",
                        "developer_instructions",
                        "model_reasoning_effort",
                        "sandbox_mode",
                        "approval_policy",
                        "default_permissions",
                        "permissions",
                        "web_search",
                    },
                )
                result.append(agent)
            except (OSError, ValueError) as error:
                name = fields.get("name") if selected is None else selected.display_name
                result.append(
                    unavailable(
                        self.source,
                        path,
                        name if isinstance(name, str) else path.stem,
                        str(error),
                    )
                )
        return result


def _restrictions(agent: AgentProfile, fields: dict[str, Any]) -> AgentProfile:
    """Remove only capabilities the Codex Agent lacks entirely.

    vBot has no sandbox and asks no approvals: an Agent that may do something,
    even confined or after approval, keeps the vBot Tool for it.
    """
    for setting in ("sandbox_mode", "default_permissions"):
        value = fields.get(setting)
        if value in _READ_ONLY:
            agent = restrict(agent, setting, frozenset({"apply_patch"}), "File edits are disabled.")
        elif value is not None:
            agent = _translated(agent, setting, "vBot has no sandbox; the Agent keeps its Tools.")
    if "approval_policy" in fields:
        agent = _translated(
            agent, "approval_policy", "vBot asks no approvals; the Agent keeps its Tools."
        )
    if fields.get("web_search") == "disabled":
        agent = restrict(
            agent, "web_search", frozenset({"web_search", "web_fetch"}), "Web access is disabled."
        )
    features = fields.get("features")
    if isinstance(features, dict) and features.get("shell_tool") is False:
        agent = restrict(agent, "features.shell_tool", SHELL_TOOLS, "The shell is disabled.")
    return agent


def _translated(agent: AgentProfile, setting: str, detail: str) -> AgentProfile:
    return replace(
        agent, translations=(*agent.translations, Translation(setting, "translated", detail))
    )
