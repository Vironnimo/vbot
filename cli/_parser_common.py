"""Shared CLI target arguments, value readers, and help vocabulary."""

from __future__ import annotations

import argparse
import json

from core.channels import (
    ALLOWED_CHANNEL_DM_SCOPES,
    ALLOWED_CHANNEL_PLATFORMS,
    ALLOWED_CHANNEL_RESPONSE_MODES,
)
from core.model_tasks import SUPPORTED_TASK_TYPES
from core.models import MODEL_TASK_ORDER
from core.providers.reasoning import THINKING_EFFORT_ORDER
from core.utils.config import DEFAULT_HOST

SERVER_COMMANDS = ("start", "stop", "restart", "status")


# Empty string = provider default; the rest is the canonical effort ladder.
THINKING_EFFORTS = ("", *THINKING_EFFORT_ORDER)


# Argparse choices need a deterministic order; the canonical sets are unordered.
CHANNEL_PLATFORMS = tuple(sorted(ALLOWED_CHANNEL_PLATFORMS))


CHANNEL_DM_SCOPES = tuple(sorted(ALLOWED_CHANNEL_DM_SCOPES))


CHANNEL_RESPONSE_MODES = tuple(sorted(ALLOWED_CHANNEL_RESPONSE_MODES))


CRON_STATUSES = ("active", "paused")


BOOTSTRAP_MODES = ("once", "always")


STATISTICS_SECTIONS = (
    "overview",
    "usage",
    "runs",
    "compactions",
    "errors",
    "tools",
    "skills",
)


TASK_TYPES = tuple(sorted(SUPPORTED_TASK_TYPES))


MODEL_TASK_TYPES = tuple(MODEL_TASK_ORDER)


AREA_HELP = {
    "server": "Start, stop, restart, and inspect the local server",
    "desktop": "Open the desktop window pointed at a local or remote server",
    "home": "Show the application and data directories",
    "update": "Update the installation from git, refresh deps/WebUI, and restart",
    "uninstall": "Remove the application, its data, or both with explicit confirmation",
    "autostart": "Enable, disable, or inspect OS autostart for the server",
    "agent": "Inspect and manage agent configs",
    "project": "Inspect and manage projects and their scanned teams",
    "session": "Inspect and manage agent chat sessions",
    "session-store": "Inspect, snapshot, verify, and recover the SQLite Session store",
    "channel": "Inspect and manage channel configs",
    "tool": "Inspect public tool catalog",
    "prompt": "Inspect and manage System Prompt blocks",
    "log": "Inspect parsed server logs",
    "provider": "Inspect and configure provider connections",
    "model": "Inspect and refresh model catalogs",
    "task-model": "Inspect and manage specialized task-model bindings",
    "skill": "Inspect and manage skills, including the disable/share policy",
    "memory": "Inspect and manage one agent's pinned memory entries",
    "extensions": "Inspect and toggle loaded extensions",
    "cron": "Inspect and manage scheduled cron jobs",
    "bootstrap": "Inspect and manage startup-triggered Agent Runs",
    "statistics": "Inspect usage statistics computed from persisted sessions",
    "config": "Inspect and update public Settings paths",
    "debug": "Inspect debug mode state and stored traces",
    "doctor": "Run local configuration health checks",
}


SERVER_HELP = {
    "start": "Start the local vBot server",
    "stop": "Stop the local vBot server",
    "restart": "Restart the local vBot server",
    "status": "Show local server status",
}


AGENT_HELP = {
    "list": "List configured agents",
    "show": "Show one agent config",
    "create": "Create an agent config",
    "update": "Update an agent config",
    "rename": "Change an Identity Agent id and retarget live references",
    "reorder": "Set the Identity Agent roster order",
    "delete": "Delete an agent config",
}


PROJECT_HELP = {
    "add": "Add a project from a repo directory and show its scan preview",
    "list": "List configured projects",
    "show": "Show one project's config, team, and scan report",
    "set": "Update one project's config",
    "set-override": "Set one project-team agent override",
    "clear-override": "Clear one project-team agent override",
    "rm": "Remove a project, archiving its anchor",
    "detect": "Detect source formats and context files in a directory",
}


SESSION_HELP = {
    "list": "List one agent's chat sessions",
    "create": "Create a new chat session for one agent",
    "delete": "Delete (archive) one agent's chat session",
    "fork": "Fork a session, optionally to another agent",
    "rename": "Set or clear a session's display title",
    "set-compaction-policy": "Set or clear a Session Policy override",
    "link-channel": "Link a session to a channel conversation for outbound replies",
}


SESSION_STORE_HELP = {
    "status": "Show current SQLite Session-store health and recovery state",
    "snapshot": "Manage verified SQLite Session snapshots",
    "incident": "Manage durable Session-store recovery incidents",
}


SESSION_STORE_SNAPSHOT_HELP = {
    "list": "List verified Session-store snapshots",
    "create": "Create a verified Session-store snapshot through the running server",
    "verify": "Verify one Session-store snapshot",
    "restore": "Restore one verified Session-store snapshot",
}


CHANNEL_HELP = {
    "add": "Create a channel config",
    "list": "List channel configs",
    "remove": "Delete a channel config",
    "update": "Update a channel config",
    "enable": "Enable a channel listener",
    "disable": "Disable a channel listener",
    "status": "Show one channel listener status",
    "set-token": "Set or rotate a channel bot token from stdin",
    "identity": "Show or set the Channel account's own platform identity",
    "access": "List participants and roles for one group",
    "grant-admin": "Grant one user admin access in one group",
    "revoke-admin": "Revoke one user's additional admin access in one group",
}


PROMPT_HELP = {
    "list": "List System Prompt blocks",
    "update": "Replace one editable prompt block's text",
    "reset": "Reset one editable prompt block to its inherited default",
    "create": "Create a custom user prompt block",
    "remove": "Remove a custom user prompt block",
    "set-layout": "Replace a scope's prompt block order and enabled states",
    "reset-layout": "Reset a scope's prompt layout to the bundled default",
    "preview": "Render one agent's complete system prompt",
}


LOG_HELP = {
    "list": "List available daily log files",
    "read": "Read parsed entries from one daily log file",
}


PROVIDER_HELP = {
    "list": "List provider connections and usability",
    "status": "Show one provider or connection status",
    "usage": "Show live Provider subscription usage and reset windows",
    "usage-history": "Show recorded Provider usage-limit observations",
    "usage-history-clear": "Delete all recorded Provider usage-limit observations",
    "set-key": "Set an API-key provider credential",
    "unset-key": "Remove an API-key provider credential",
    "enable": "Enable a provider connection (local providers start disabled)",
    "disable": "Disable a provider connection (no probes, no listed models)",
    "connect": "Start the OAuth device flow for one provider connection",
    "disconnect": "Remove the stored OAuth token for one provider connection",
    "connect-status": "Show OAuth connection and device-flow state",
    "custom-list": "List Settings-owned Custom Providers",
    "custom-save": "Create or replace a Custom Provider",
    "custom-delete": "Delete a Custom Provider",
}


MODEL_HELP = {
    "list": "List available models",
    "show": "Show complete data for one Model",
    "refresh": "Refresh model catalogs",
}


TASK_MODEL_HELP = {
    "list": "List configured task-model bindings",
    "status": "Show whether one task type is configured and usable",
    "targets": "List available targets for one task type",
    "options": "Show supported, configured, and effective options for one task target",
    "set": "Bind one task type to a target and optionally set options",
    "set-option": "Set one option on the currently bound task target",
    "unset-option": "Remove one option from the currently bound task target",
    "clear": "Remove one task-type binding",
}


CRON_HELP = {
    "list": "List scheduled cron jobs",
    "create": "Create a cron job for one agent",
    "update": "Update a cron job",
    "delete": "Delete a cron job",
    "enable": "Enable a cron job",
    "disable": "Disable a cron job",
}


BOOTSTRAP_HELP = {
    "list": "List Bootstrap jobs",
    "create": "Create a Bootstrap job for a future server startup",
    "update": "Update and rearm a Bootstrap job",
    "delete": "Delete a Bootstrap job",
    "enable": "Enable and rearm a Bootstrap job",
    "disable": "Pause a Bootstrap job",
}


STATISTICS_HELP = {
    "overview": "Show the overview section: agents, sessions, runs, and message totals",
    "usage": "Show the usage section: token totals and per-provider/model breakdowns",
    "runs": "Show the runs section: counts, status rates, and durations",
    "compactions": "Show checkpoint counts, reclaimed context, Strategies, and top Sessions",
    "errors": "Show the errors section: totals and breakdowns by kind, provider, and agent",
    "tools": "Show the tools section: call counts and per-tool success rates",
    "skills": "Show Skill offers, activations, and evidence-backed offer conversion",
}


CONFIG_HELP = {
    "list": "List public Settings paths and metadata",
    "describe": "Describe one public Settings path",
    "effective": "Show all normalized public Settings values",
    "raw": "Show the internal settings.json document for diagnostics",
    "get": "Show one effective public Settings value",
    "set": "Set one public Settings path",
    "unset": "Remove one configured Settings override",
    "patch": "Apply multiple Settings changes atomically",
}


DEBUG_HELP = {
    "status": "Show debug mode state and trace count",
    "traces": "List stored debug trace metadata",
    "trace": "Show one stored debug trace as JSON",
    "clear": "Delete all stored debug traces",
    "probe": "Fetch one provider's models endpoint and preview the response",
}


DOCTOR_HELP = {
    "settings": "Validate the target data-dir settings.json",
    "config": "Validate all user-editable JSON config files in the target data-dir",
}


AUTOSTART_HELP = {
    "enable": "Register OS autostart and start the server now",
    "disable": "Remove the OS autostart entry",
    "status": "Show whether OS autostart is registered",
}


TOOL_HELP = {"list": "List public registered tools"}


SKILL_HELP = {
    "list": "List effective skills and diagnostics",
    "inventory": "List every skill from every source with policy state",
    "read": "Read editable skills in a global or private agent scope",
    "create": "Create a skill in a global or private agent scope",
    "update": "Replace a skill's SKILL.md in an editable scope",
    "delete": "Delete a skill from an editable scope",
    "write-file": "Write one supporting file inside an editable skill",
    "remove-file": "Remove one supporting file from an editable skill",
    "disable": "Disable one skill everywhere (master switch)",
    "enable": "Re-enable a disabled skill",
    "share": "Share one agent's private skill with other agents",
    "unshare": "Stop sharing one agent's private skill",
}


MEMORY_HELP = {
    "list": "List one agent's pinned memory entries",
    "add": "Add one pinned memory entry",
    "replace": "Replace one pinned memory entry's content",
    "remove": "Remove one pinned memory entry",
}


EXTENSIONS_HELP = {
    "list": "List loaded, failed, and disabled extensions",
    "reload": "Reload all extensions from disk (applies code changes, applied live)",
    "enable": "Enable a disabled extension (applied live)",
    "disable": "Disable an extension (applied live)",
    "show": "Show one extension's settings (schema, current values, secret set-state)",
    "set": "Set one extension setting (secret -> .env, other fields -> live config)",
}


def _add_target_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_host: str | None = DEFAULT_HOST,
) -> None:
    parser.add_argument(
        "--host",
        default=default_host,
        help=(
            f"Server host (default: {default_host})"
            if default_host is not None
            else "Server host override; omission uses this command's instance selection"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Server port; normally --port > VBOT_SERVER_PORT > local Settings > 8420",
    )
    parser.add_argument(
        "--data-dir",
        help="Local instance configuration directory; does not redirect RPC state on the server",
    )


def _json_object_argument(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return value


def _json_array_argument(raw: str) -> list[object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, list):
        raise argparse.ArgumentTypeError("value must be a JSON array")
    return value


def _add_command_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    command: str,
    help_text: str,
    *,
    example: str | None = None,
) -> argparse.ArgumentParser:
    description = help_text if example is None else f"{help_text}. Example: vbot {example}"
    command_parser = subparsers.add_parser(command, help=help_text, description=description)
    _add_target_arguments(command_parser)
    return command_parser
