"""Codex TOML Agent definitions, including config-file role references."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.projects.sources import _reading as reading
from core.projects.sources._translation import (
    SHELL_TOOLS,
    RuleDraft,
    profile,
    restrict,
    unavailable,
    unsupported,
)
from core.projects.sources.profile import AgentProfile, Translation


class CodexAdapter:
    source = "codex"

    def scan(self, root: Path) -> list[AgentProfile]:
        return self._scan(root)

    def read(self, root: Path, selected: AgentProfile) -> AgentProfile | None:
        if not reading.is_file_strict(selected.source_path):
            return None
        return next(iter(self._scan(root, selected)), None)

    def _scan(self, root: Path, selected: AgentProfile | None = None) -> list[AgentProfile]:
        # Every profile inherits the shared config, also before it exists.
        shared = (root / ".codex/config.toml",)
        return [
            replace(agent, rule_files=(*shared, *agent.rule_files))
            for agent in self._profiles(root, selected)
        ]

    def _profiles(self, root: Path, selected: AgentProfile | None) -> list[AgentProfile]:
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
        except (OSError, ValueError, RecursionError) as error:
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
            except (OSError, ValueError, RecursionError) as error:
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
                        "sandbox_workspace_write",
                        "approval_policy",
                        "default_permissions",
                        "permissions",
                        "web_search",
                    },
                )
                result.append(agent)
            except (OSError, ValueError, RecursionError) as error:
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
    """Translate the sandbox into Permission Rules; remove only missing capabilities.

    vBot asks no approvals: an Agent that may do something after approval keeps
    the vBot Tool for it. A sandbox limits where the Agent writes, which
    Permission Rules express; its network limit has no vBot equivalent.
    """
    agent = _sandbox_rules(agent, fields)
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


_NETWORK_DETAIL = "Not translated: vBot does not control the network access of shell commands."
_SANDBOX_BASES = {
    "read-only": ("scratch", "Writes are limited to the Scratch Area (write: scratch)."),
    "workspace-write": (
        "work_area",
        "Writes are limited to the Work Area and the writable roots (write: work_area).",
    ),
}


def _sandbox_rules(agent: AgentProfile, fields: dict[str, Any]) -> AgentProfile:
    """``sandbox_mode`` or a permission profile as Permission Rules (developers.openai.com/codex).

    ``read-only`` writes only to the Scratch Area, ``workspace-write`` to the Work
    Area plus ``sandbox_workspace_write.writable_roots``; ``danger-full-access``
    adds nothing. A named profile translates its ``extends`` chain and its
    filesystem entries: ``write`` to Edit allow rules, ``deny`` to Read deny rules.
    """
    draft = RuleDraft()
    mode = fields.get("sandbox_mode")
    if mode in _SANDBOX_BASES:
        base, detail = _SANDBOX_BASES[mode]
        draft.narrow(
            "write", base, "sandbox_mode", f"{detail} Its network limit is not translated."
        )
    elif mode == "danger-full-access":
        draft.report("sandbox_mode", "translated", "Full access: no Permission Rules.")
    elif mode is not None:
        raise reading.SourceError("Unknown sandbox_mode.")
    workspace = fields.get("sandbox_workspace_write")
    if workspace is not None:
        if not isinstance(workspace, dict):
            raise reading.SourceError("sandbox_workspace_write must be a table.")
        for key, value in workspace.items():
            setting = f"sandbox_workspace_write.{key}"
            if key == "writable_roots" and mode == "workspace-write":
                for root in reading.names(value):
                    draft.rule("allow", f"Edit({root.rstrip('/')}/**)", f"{setting}.{root}")
            elif key == "network_access":
                draft.report(setting, "not_supported", _NETWORK_DETAIL)
            elif key in {"exclude_tmpdir_env_var", "exclude_slash_tmp"}:
                draft.report(
                    setting,
                    "translated",
                    "vBot adds no temporary folders; the Agent writes temporary files to its "
                    "Scratch Area.",
                )
            else:
                draft.report(setting, "not_supported", "Applies only to workspace-write.")
    profile_name = fields.get("default_permissions")
    if profile_name is not None:
        _permission_profile(draft, profile_name, fields.get("permissions", {}))
    return draft.apply(agent)


def _permission_profile(draft: RuleDraft, name: object, profiles: object) -> None:
    if not isinstance(name, str) or not isinstance(profiles, dict):
        raise reading.SourceError("default_permissions must name a permission profile.")
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = name
    while current is not None and current not in {":read-only", ":workspace", "read-only"}:
        if current == ":danger-full-access":
            draft.report("default_permissions", "translated", "Full access: no Permission Rules.")
            return
        profile = profiles.get(current)
        if current in seen or not isinstance(profile, dict):
            raise reading.SourceError(f"Unknown or cyclic permission profile {current}.")
        seen.add(current)
        chain.insert(0, profile)
        parent = profile.get("extends")
        if parent is not None and not isinstance(parent, str):
            raise reading.SourceError("extends must name a permission profile.")
        current = parent
    # A profile without a built-in parent writes only where its entries say.
    base = "work_area" if current == ":workspace" else "scratch"
    draft.narrow(
        "write",
        base,
        "default_permissions",
        f"Writes are limited to {'the Work Area' if base == 'work_area' else 'the Scratch Area'} "
        f"and the profile's write entries (write: {base}).",
    )
    for profile in chain:
        for key in profile:
            if key not in {"description", "extends", "filesystem"}:
                draft.report(
                    f"permissions.{name}.{key}",
                    "not_supported",
                    _NETWORK_DETAIL if key == "network" else "This setting has no vBot equivalent.",
                )
        filesystem = profile.get("filesystem", {})
        if not isinstance(filesystem, dict):
            raise reading.SourceError("filesystem must be a table.")
        for path, access in filesystem.items():
            if path == ":workspace_roots" and isinstance(access, dict):
                for sub, nested in access.items():
                    target = "{workdir}" if sub == "." else "{workdir}/" + sub.removeprefix("./")
                    _filesystem_entry(draft, f"{name}.filesystem.{path}.{sub}", target, nested)
            elif path.startswith(":") or path == "glob_scan_max_depth":
                if path != "glob_scan_max_depth":
                    draft.report(
                        f"permissions.{name}.filesystem.{path}",
                        "not_supported",
                        "Not translated: vBot has no such folder set.",
                    )
            elif isinstance(access, dict):
                for sub, nested in access.items():
                    target = path.rstrip("/") + ("" if sub == "." else "/" + sub)
                    _filesystem_entry(draft, f"{name}.filesystem.{path}.{sub}", target, nested)
            else:
                _filesystem_entry(draft, f"{name}.filesystem.{path}", path, access)


def _filesystem_entry(draft: RuleDraft, setting: str, path: str, access: object) -> None:
    setting = f"permissions.{setting}"
    if access == "write":
        draft.rule("allow", f"Edit({path})", setting)
    elif access == "deny":
        draft.rule("deny", f"Read({path})", setting)
    elif access == "read":
        draft.report(setting, "translated", "Needs no rule: reading is not limited.")
    else:
        raise reading.SourceError("Filesystem access must be read, write or deny.")


def _translated(agent: AgentProfile, setting: str, detail: str) -> AgentProfile:
    return replace(
        agent, translations=(*agent.translations, Translation(setting, "translated", detail))
    )
