"""Codex TOML Agent definitions, including config-file role references."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    FILE_TOOLS,
    SHELL_TOOLS,
    profile,
    restrict,
    unavailable,
    unsupported,
)
from core.projects.sources.profile import AgentProfile, Translation

# What each sandbox leaves out that vBot's unconfined Tools would allow. vBot's
# file Tools can be closed exactly; its shell cannot be confined, so any sandbox
# closes it.
_SANDBOXES = {
    "read-only": frozenset({"apply_patch", *SHELL_TOOLS}),
    "workspace-write": SHELL_TOOLS,
    "danger-full-access": frozenset(),
}
_PERMISSION_PROFILES = {
    ":read-only": "read-only",
    ":workspace": "workspace-write",
    ":danger-no-sandbox": "danger-full-access",
}


class CodexAdapter:
    source = "codex"

    def scan(self, root: Path) -> list[AgentProfile]:
        paths = reading.files(root / ".codex/agents", ".toml", recursive=True)
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
                    definitions.append((path, fields))
                    referenced.add(path)
        except (OSError, ValueError) as error:
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
                result.append(unavailable(self.source, path, path.stem, str(error)))
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
                name = fields.get("name")
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
    """Translate Codex sandbox, approval and capability switches without widening."""
    sandbox = fields.get("sandbox_mode")
    profile_name = fields.get("default_permissions")
    if isinstance(profile_name, str) and profile_name in _PERMISSION_PROFILES:
        sandbox = _PERMISSION_PROFILES[profile_name]
    elif profile_name is not None or "permissions" in fields:
        agent = restrict(
            agent,
            "default_permissions",
            FILE_TOOLS,
            "Custom permission profiles are unavailable; file and shell Tools are disabled.",
        )
    if sandbox is not None:
        closed = _SANDBOXES.get(sandbox) if isinstance(sandbox, str) else None
        if closed is None:
            agent = restrict(
                agent,
                "sandbox_mode",
                FILE_TOOLS,
                "Unknown sandbox; file and shell Tools are disabled.",
            )
        elif closed:
            agent = restrict(
                agent,
                "sandbox_mode",
                closed,
                "vBot cannot confine its shell; Tools this sandbox forbids are disabled.",
            )
        else:
            agent = _translated(agent, "sandbox_mode", "No sandbox; vBot Tool access applies.")
    approval = fields.get("approval_policy")
    if approval is not None and approval != "never":
        closed = SHELL_TOOLS | ({"apply_patch"} if approval == "untrusted" else set())
        agent = restrict(
            agent,
            "approval_policy",
            frozenset(closed),
            "vBot asks no approvals; Tools that would ask are disabled.",
        )
    web_search = fields.get("web_search")
    if web_search == "disabled":
        agent = restrict(
            agent, "web_search", frozenset({"web_search", "web_fetch"}), "Web access is disabled."
        )
    elif web_search is not None:
        agent = _translated(agent, "web_search", "Web search stays available.")
    features = fields.get("features")
    if isinstance(features, dict) and features.get("shell_tool") is False:
        agent = restrict(agent, "features.shell_tool", SHELL_TOOLS, "The shell is disabled.")
    return agent


def _translated(agent: AgentProfile, setting: str, detail: str) -> AgentProfile:
    return replace(
        agent, translations=(*agent.translations, Translation(setting, "translated", detail))
    )
