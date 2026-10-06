"""Codex TOML Agent definitions, including config-file role references."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    FILE_TOOLS,
    profile,
    restrict,
    unavailable,
    unsupported,
)
from core.projects.sources.profile import AgentProfile


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
                if "sandbox_mode" in fields:
                    agent = restrict(
                        agent,
                        "sandbox_mode",
                        FILE_TOOLS,
                        "Sandbox isolation is unavailable; file and shell Tools are disabled.",
                    )
                if "approval_policy" in fields and fields["approval_policy"] != "never":
                    agent = replace(agent, allowed_tools=frozenset())
                    agent = restrict(
                        agent,
                        "approval_policy",
                        FILE_TOOLS,
                        "Approval policies are unavailable; Tools are disabled until "
                        "explicitly overridden in vBot.",
                    )
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
