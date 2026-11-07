"""Lazy public SDK setup and its private, in-process MCP Tool bridge."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.providers._claude_agent_wire import JsonObject, tool_result
from core.providers.errors import ProviderAuthError, ProviderError
from core.utils.workers import BoundedWorkerPool

MINIMUM_VERSION = (2, 1, 248)
MCP_PREFIX = "mcp__vbot__"
MCP_NAME_LIMIT = 64 - len(MCP_PREFIX)
SDK_POOL = BoundedWorkerPool(name="claude-agent-sdk", max_workers=2)
SUBSCRIPTION_REQUIRED_MESSAGE = (
    "Claude Code is using API or cloud billing. Sign in with a Claude subscription "
    "and remove conflicting API or cloud credentials from the vBot server environment."
)

ISOLATION_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTO_COMPACT": "1",
    "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",
    "MAX_MCP_OUTPUT_TOKENS": "200000",
    "CLAUDE_CODE_SIMPLE": "",
    "CLAUDE_CODE_EFFORT_LEVEL": "",
    "MCP_TOOL_TIMEOUT": "2147483647",
}


def load_sdk() -> Any:
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as error:
        raise ProviderError(
            "The Claude Agent SDK is unavailable in this vBot installation.", retryable=False
        ) from error


def executable(configured_path: str | None = None) -> str:
    from core.utils.file_status import is_file_strict

    explicit = os.environ.get("VBOT_CLAUDE_PATH") or configured_path
    selected = (
        str(Path(explicit).expanduser())
        if explicit
        else shutil.which("claude.exe" if os.name == "nt" else "claude")
    )
    if explicit and not is_file_strict(Path(explicit).expanduser()):
        raise ProviderAuthError(
            "The configured Claude Code executable is unavailable. "
            "Correct its path in Settings or VBOT_CLAUDE_PATH."
        )
    if not selected:
        native = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
        if is_file_strict(native):
            selected = str(native)
    if not selected:
        raise ProviderAuthError(
            "Claude Code was not found on PATH or in ~/.local/bin on the vBot server. "
            "Install the native Claude Code program or select its executable in Settings, "
            "then run `claude auth login`."
        )
    if os.name == "nt" and selected.lower().endswith((".cmd", ".bat")):
        raise ProviderAuthError(
            "Install the native Claude Code executable or set VBOT_CLAUDE_PATH to claude.exe."
        )
    return str(Path(selected).resolve())


def login_info(info: Any) -> JsonObject:
    info = info if isinstance(info, dict) else {}
    version = info.get("claude_code_version")
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if match is None or tuple(map(int, match.groups())) < MINIMUM_VERSION:
        return {
            "connected": False,
            "status": "incompatible",
            "models": [],
            "subscription": False,
            "message": "Claude Code 2.1.248 or later is required. Run `claude update`.",
        }
    account = info.get("account")
    account = account if isinstance(account, dict) else {}
    connected = bool(
        account.get("tokenSource") not in (None, "none")
        or account.get("apiKeySource")
        or account.get("subscriptionType")
    )
    subscription = (
        connected
        and account.get("apiProvider") in (None, "firstParty")
        and not account.get("apiKeySource")
        and isinstance(account.get("subscriptionType"), str)
        and bool(account["subscriptionType"].strip())
    )
    models: list[JsonObject] = []
    by_id: dict[str, JsonObject] = {}
    for model in info.get("models", []):
        if not isinstance(model, dict) or model.get("value") == "default":
            continue
        entry = {
            key: model[key]
            for key in (
                "value",
                "displayName",
                "description",
                "supportedEffortLevels",
                "resolvedModel",
                "contextWindow",
            )
            if key in model
        }
        # Claude Code lists its newest generation only under moving aliases such
        # as `opus`; the resolved id names the exact Model and stays stable.
        resolved = entry.get("resolvedModel")
        if isinstance(resolved, str) and resolved.strip():
            entry["value"] = resolved.strip()
        value = entry.get("value")
        if isinstance(value, str) and value in by_id:
            for key, item in entry.items():
                by_id[value].setdefault(key, item)
            continue
        if isinstance(value, str):
            by_id[value] = entry
        models.append(entry)
    result: JsonObject = {
        "connected": connected,
        "status": "ready" if connected else "logged_out",
        "models": models,
        "subscription": subscription,
    }
    if not connected:
        result["message"] = "Run `claude auth login` on the vBot server."
    elif not subscription:
        if account.get("apiKeySource") or account.get("apiProvider") not in (None, "firstParty"):
            result["status"] = "api_billing"
            result["message"] = SUBSCRIPTION_REQUIRED_MESSAGE
        else:
            result["status"] = "subscription_unconfirmed"
            result["message"] = (
                "Claude Code did not confirm a subscription for this login. "
                "Run `claude auth login` with your subscription account on the vBot server."
            )
    return result


class PendingTools:
    """Park native callbacks until the next already-masked canonical request."""

    def __init__(self, tools: list[JsonObject]) -> None:
        self.names: dict[str, str] = {}
        self.short_names: dict[str, str] = {}
        self.tools = tools
        reserved = {
            definition["name"]
            for definition in tools
            if re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MCP_NAME_LIMIT}}}", definition["name"])
        }
        used: set[str] = set()
        for definition in tools:
            name = definition["name"]
            short = name
            if name not in reserved:
                stem = re.sub(r"[^A-Za-z0-9_-]", "_", name)
                digest = hashlib.sha256(name.encode()).hexdigest()[:12]
                collision = 0
                while True:
                    suffix = f"_{digest}" + (f"_{collision}" if collision else "")
                    short = stem[: MCP_NAME_LIMIT - len(suffix)] + suffix
                    if short not in reserved and short not in used:
                        break
                    collision += 1
            used.add(short)
            self.names[MCP_PREFIX + short] = name
            self.short_names[name] = short
        self.waiting: dict[str, asyncio.Future[tuple[JsonObject, list[JsonObject] | None]]] = {}
        self.replacements: dict[str, list[JsonObject]] = {}
        self.call_names: dict[str, str] = {}
        self.next_id: str | None = None

    def expect(
        self, call_id: str, name: str
    ) -> asyncio.Future[tuple[JsonObject, list[JsonObject] | None]]:
        if name not in self.short_names or (
            call_id in self.call_names and self.call_names[call_id] != name
        ):
            raise ProviderError(
                "Claude Code emitted mismatched vBot Tool identities", retryable=False
            )
        self.call_names[call_id] = name
        if call_id not in self.waiting:
            self.waiting[call_id] = asyncio.get_running_loop().create_future()
        return self.waiting[call_id]

    async def before(self, event: Any, _call_id: Any, _context: Any) -> JsonObject:
        if event.get("hook_event_name") == "PreToolUse":
            name = event.get("tool_name")
            if name not in self.names:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                    }
                }
            call_id = event["tool_use_id"]
            self.expect(call_id, self.names[name])
            self.next_id = call_id
        return {}

    async def after(self, event: Any, _call_id: Any, _context: Any) -> JsonObject:
        replacement = self.replacements.pop(event.get("tool_use_id", ""), None)
        if replacement is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedMCPToolOutput": replacement,
            }
        }

    def mcp_server(self) -> JsonObject:
        # Public low-level MCP handlers deliberately leave argument validation to vBot.
        from mcp.server import Server
        from mcp.types import CallToolResult, ListToolsResult, Tool

        async def list_tools(_context: Any, _params: Any) -> Any:
            return ListToolsResult(
                tools=[
                    Tool.model_validate(
                        {
                            "name": self.short_names[tool["name"]],
                            "description": tool["description"],
                            "inputSchema": tool["parameters"],
                            "annotations": {"readOnlyHint": False},
                            "_meta": {
                                "anthropic/alwaysLoad": True,
                                "anthropic/maxResultSizeChars": 500_000,
                            },
                        }
                    )
                    for tool in self.tools
                ]
            )

        async def call_tool(_context: Any, params: Any) -> Any:
            call_id, self.next_id = self.next_id, None
            if (
                not call_id
                or MCP_PREFIX + params.name not in self.names
                or self.call_names.get(call_id) != self.names[MCP_PREFIX + params.name]
            ):
                raise ProviderError("Claude Code called an uncorrelated vBot Tool", retryable=False)
            result, replacement = await self.expect(call_id, self.names[MCP_PREFIX + params.name])
            self.waiting.pop(call_id, None)
            self.call_names.pop(call_id, None)
            if replacement:
                self.replacements[call_id] = replacement
            return CallToolResult.model_validate(result)

        return {
            "type": "sdk",
            "name": "vbot",
            "instance": Server("vbot", on_list_tools=list_tools, on_call_tool=call_tool),
        }

    async def prepare_results(self, messages: Sequence[Mapping[str, Any]]) -> Callable[[], None]:
        """Validate every Result before a queued Steer or any native handler is released."""
        prepared = []
        seen: set[str] = set()
        for message in messages:
            call_id = str(message.get("tool_call_id"))
            future = self.waiting.get(call_id)
            if future is None or future.done() or call_id in seen:
                raise ProviderError(
                    "Claude Agent has no waiting Tool Call for this result", retryable=False
                )
            seen.add(call_id)
            prepared.append((call_id, self.call_names[call_id], future))
        # Keep Future ownership on the Event Loop; only pure payload shaping
        # (including potentially large JSON/media results) runs on a worker.
        results = await SDK_POOL.run(lambda: [tool_result(message) for message in messages])

        def release() -> None:
            if any(
                future.done()
                or self.waiting.get(call_id) is not future
                or self.call_names.get(call_id) != name
                for call_id, name, future in prepared
            ):
                raise ProviderError(
                    "Claude Agent's Tool batch is no longer waiting", retryable=False
                )
            for (_call_id, _name, future), result in zip(prepared, results, strict=True):
                future.set_result(result)

        return release

    def clear(self) -> None:
        for future in self.waiting.values():
            if not future.done():
                future.cancel()
        self.waiting.clear()
        self.call_names.clear()
        self.replacements.clear()
        self.next_id = None


def options(
    sdk: Any,
    path: str,
    *,
    model: str | None = None,
    system: str = "",
    pending: PendingTools | None = None,
    controls: JsonObject | None = None,
) -> Any:
    env = dict(ISOLATION_ENV)
    controls = controls or {}
    limit = controls.get("max_tokens")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(limit)
    hooks = {}
    if pending:
        hooks = {
            "PreToolUse": [sdk.HookMatcher(hooks=[pending.before])],
            "PostToolUse": [sdk.HookMatcher(hooks=[pending.after])],
        }
    return sdk.ClaudeAgentOptions(
        cli_path=path,
        model=model,
        system_prompt=system,
        tools=[],
        allowed_tools=["mcp__vbot__*"],
        permission_mode="dontAsk",
        setting_sources=[],
        settings=json.dumps({"disableAllHooks": True}),
        strict_mcp_config=True,
        skills=[],
        include_partial_messages=True,
        verbatim_prompts=True,
        mcp_servers={"vbot": pending.mcp_server()} if pending and pending.tools else {},
        hooks=hooks,
        env=env,
        extra_args={"no-session-persistence": None},
        effort=controls.get("effort"),
        thinking=controls.get("thinking", {"type": "adaptive", "display": "summarized"}),
    )


def abort_client(client: Any) -> None:
    """Kill exactly the SDK-owned process when synchronous Runtime shutdown cannot await.

    Claude Agent SDK 0.2.179 exposes no public synchronous close. Its public
    default transport owns an AnyIO Process at these two attributes. This is
    the sole SDK-internal compatibility bridge; request and auth use public APIs.
    """
    transport = getattr(client, "_transport", None)
    process = getattr(transport, "_process", None)
    if process is not None and process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
