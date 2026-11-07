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
from collections.abc import Mapping
from typing import Any

from core.providers._claude_agent_wire import JsonObject, tool_result
from core.providers.errors import ProviderAuthError, ProviderError
from core.utils.workers import BoundedWorkerPool

MINIMUM_VERSION = (2, 1, 248)
SDK_POOL = BoundedWorkerPool(name="claude-agent-sdk", max_workers=2)
SUBSCRIPTION_REQUIRED_MESSAGE = (
    "Claude Code is using API or cloud billing. Sign in with a Claude subscription "
    "and remove conflicting API or cloud credentials from the vBot server environment."
)

ISOLATION_ENV = {
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


def executable() -> str:
    selected = os.environ.get("VBOT_CLAUDE_PATH") or shutil.which("claude")
    if not selected:
        raise ProviderAuthError(
            "Install Claude Code on the vBot server, then run `claude auth login`."
        )
    if os.name == "nt" and selected.lower().endswith((".cmd", ".bat")):
        raise ProviderAuthError(
            "Install the native Claude Code executable or set VBOT_CLAUDE_PATH to claude.exe."
        )
    return selected


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
    )
    models = []
    for model in info.get("models", []):
        if isinstance(model, dict) and model.get("value") != "default":
            models.append(
                {
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
            )
    result: JsonObject = {
        "connected": connected,
        "status": "ready" if connected else "logged_out",
        "models": models,
        "subscription": subscription,
    }
    if not connected:
        result["message"] = "Run `claude auth login` on the vBot server."
    return result


class PendingTools:
    """Park native callbacks until the next already-masked canonical request."""

    def __init__(self, tools: list[JsonObject]) -> None:
        self.names: dict[str, str] = {}
        self.short_names: dict[str, str] = {}
        self.tools = tools
        for index, definition in enumerate(tools):
            name = definition["name"]
            short = (
                name
                if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                else f"tool_{index}_{hashlib.sha256(name.encode()).hexdigest()[:12]}"
            )
            self.names[f"mcp__vbot__{short}"] = name
            self.short_names[name] = short
        self.waiting: dict[str, asyncio.Future[tuple[JsonObject, list[JsonObject] | None]]] = {}
        self.replacements: dict[str, list[JsonObject]] = {}
        self.call_names: dict[str, str] = {}
        self.next_id: str | None = None

    def expect(self, call_id: str) -> asyncio.Future[tuple[JsonObject, list[JsonObject] | None]]:
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
            self.next_id = event["tool_use_id"]
            self.call_names[self.next_id] = name
            self.expect(self.next_id)
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
                or f"mcp__vbot__{params.name}" not in self.names
                or self.call_names.get(call_id) != f"mcp__vbot__{params.name}"
            ):
                raise ProviderError("Claude Code called an uncorrelated vBot Tool", retryable=False)
            result, replacement = await self.expect(call_id)
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

    def resolve(self, message: Mapping[str, Any]) -> None:
        call_id = message.get("tool_call_id")
        future = self.waiting.get(str(call_id))
        if future is None or future.done():
            raise ProviderError(
                "Claude Agent has no waiting Tool Call for this result", retryable=False
            )
        future.set_result(tool_result(message))

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
