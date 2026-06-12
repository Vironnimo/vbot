"""Extension hooks registry and loader for local Python extensions."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import sys
import types
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("extensions")
_EXTENSION_PARENT_PACKAGE = "vbot_ext"

HookHandler = Callable[..., Any]
RegisteredHandler = tuple[str, HookHandler]
# Injected by chat so tool-result-envelope schema knowledge stays in the chat
# domain: given (extension_name, candidate dict) it returns the validated
# envelope or ``None`` when the candidate is rejected.
ToolResultValidator = Callable[[str, dict[str, Any]], "dict[str, Any] | None"]

# Sentinel distinguishing "handler raised and was skipped" from a handler that
# legitimately returned ``None``.
_HANDLER_FAILED = object()


def _ignore_note(text: str) -> None:
    """Default no-op note sink for contexts built without a live session."""
    return None


@dataclass(frozen=True)
class HookContext:
    """First positional argument to every handler. Constructed in ``core/chat/``.

    ``add_note`` appends a kernel-internal ``role: "note"`` entry to the active
    session; chat wires it to ``session.add_note`` when constructing the context.
    """

    session_id: str
    agent_id: str
    run_id: str
    add_note: Callable[[str], None] = _ignore_note


@dataclass(frozen=True)
class Deny:
    """``tool_call`` decision: stop the pipeline and refuse execution with a reason."""

    reason: str


@dataclass(frozen=True)
class Modify:
    """``tool_call`` decision: replace the tool input; the pipeline keeps going."""

    input: dict[str, Any]


@dataclass(frozen=True)
class Replace:
    """``tool_call`` decision: skip execution and use this result envelope instead."""

    result: dict[str, Any]


@dataclass(frozen=True)
class ToolCallDecision:
    """Outcome of the ``tool_call`` decision pipeline handed back to chat.

    Exactly one disposition holds:

    - proceed — both ``deny_reason`` and ``replacement`` are ``None``: execute the
      tool with ``effective_input`` (reflects any ``Modify`` applied in the pipeline).
    - denied — ``deny_reason``/``deny_extension`` set: the tool is not executed and
      chat builds a deny error envelope naming the extension.
    - replaced — ``replacement`` is a validated result envelope used as the result;
      the tool is not executed.
    """

    effective_input: dict[str, Any]
    deny_reason: str | None = None
    deny_extension: str | None = None
    replacement: dict[str, Any] | None = None


class HooksAPI:
    def __init__(self, registry: ExtensionRegistry, extension_name: str) -> None:
        self._registry = registry
        self._extension_name = extension_name

    def on(self, event: str, handler: HookHandler) -> None:
        self._registry._handlers[event].append((self._extension_name, handler))


class ExtensionRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, list[RegisteredHandler]] = defaultdict(list)

    @classmethod
    def load(
        cls,
        extensions_dir: Path,
        extra_dirs: list[Path] | None = None,
    ) -> ExtensionRegistry:
        registry = cls()
        scan_roots = [extensions_dir, *(extra_dirs or [])]
        for root in scan_roots:
            _load_extension_root(root, registry)
        return registry

    async def _invoke(
        self,
        event: str,
        extension_name: str,
        handler: HookHandler,
        ctx: HookContext,
        payload: dict[str, Any],
    ) -> Any:
        """Call one handler with per-handler exception isolation.

        Awaits async handlers. On failure logs at ``warning`` and returns the
        ``_HANDLER_FAILED`` sentinel so callers can skip the handler without
        confusing a raised handler with one that returned ``None``.
        """
        try:
            result = handler(ctx, **payload)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:
            _LOGGER.warning(
                "Extension %r %s handler raised: %s",
                extension_name,
                event,
                exc,
            )
            return _HANDLER_FAILED

    async def dispatch_run_start(self, ctx: HookContext, *, session_id: str, agent_id: str) -> None:
        """Observer event: run all ``run_start`` handlers; ignore return values."""
        payload = {"session_id": session_id, "agent_id": agent_id}
        for extension_name, handler in self._handlers.get("run_start", []):
            await self._invoke("run_start", extension_name, handler, ctx, payload)

    async def dispatch_run_end(
        self, ctx: HookContext, *, session_id: str, agent_id: str, outcome: str
    ) -> None:
        """Observer event: run all ``run_end`` handlers; ignore return values."""
        payload = {"session_id": session_id, "agent_id": agent_id, "outcome": outcome}
        for extension_name, handler in self._handlers.get("run_end", []):
            await self._invoke("run_end", extension_name, handler, ctx, payload)

    async def dispatch_before_agent_start(
        self, ctx: HookContext, *, agent: Any, session: Any, messages: Any, run: Any
    ) -> list[str]:
        """Accumulator event: collect every handler's ``system_prompt_append``.

        Returns the appends in load order; applying them to the system message
        stays in chat (domain knowledge about message shape).
        """
        payload = {"agent": agent, "session": session, "messages": messages, "run": run}
        appends: list[str] = []
        for extension_name, handler in self._handlers.get("before_agent_start", []):
            result = await self._invoke("before_agent_start", extension_name, handler, ctx, payload)
            if result is _HANDLER_FAILED:
                continue
            if isinstance(result, dict) and isinstance(result.get("system_prompt_append"), str):
                appends.append(result["system_prompt_append"])
        return appends

    async def dispatch_context(self, ctx: HookContext, *, messages: list) -> list:
        """Pipeline event: each handler may replace the running message list.

        Threads the list through every handler in load order: a handler returning
        a list makes it the current list (the next handler sees it); any other
        return leaves the running list unchanged. Returns the final list. Chat
        passes a shallow per-message copy in, so this is safe to use as the
        request messages.
        """
        current = messages
        for extension_name, handler in self._handlers.get("context", []):
            payload = {"messages": current}
            result = await self._invoke("context", extension_name, handler, ctx, payload)
            if result is _HANDLER_FAILED:
                continue
            if isinstance(result, list):
                current = result
        return current

    async def dispatch_tool_call(
        self,
        ctx: HookContext,
        *,
        tool_name: str,
        tool_call_id: str,
        input: dict[str, Any],
        validator: ToolResultValidator,
    ) -> ToolCallDecision:
        """Decision pipeline: handlers may modify the input, deny, or replace.

        Each handler returns ``None`` (continue unchanged), ``Modify(input)``
        (the input is replaced and the next handler sees it), ``Deny(reason)``
        (stops the pipeline; the tool is not executed), or ``Replace(result)``
        (stops the pipeline; ``result`` must pass ``validator`` or it is logged
        and treated as continue). Any other return is ignored with a warning —
        plain dicts no longer short-circuit a tool call. Returns a
        ``ToolCallDecision`` describing the effective input and disposition.
        """
        current_input = input
        for extension_name, handler in self._handlers.get("tool_call", []):
            payload = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "input": current_input,
            }
            decision = await self._invoke("tool_call", extension_name, handler, ctx, payload)
            if decision is _HANDLER_FAILED or decision is None:
                continue
            if isinstance(decision, Modify):
                if isinstance(decision.input, dict):
                    current_input = decision.input
                else:
                    _LOGGER.warning(
                        "Extension %r tool_call Modify ignored: input is not a dict",
                        extension_name,
                    )
                continue
            if isinstance(decision, Deny):
                return ToolCallDecision(
                    effective_input=current_input,
                    deny_reason=decision.reason,
                    deny_extension=extension_name,
                )
            if isinstance(decision, Replace):
                validated = validator(extension_name, decision.result)
                if validated is None:
                    continue
                return ToolCallDecision(effective_input=current_input, replacement=validated)
            _LOGGER.warning(
                "Extension %r tool_call handler returned an unsupported value (%s); "
                "ignoring. Return None, Modify, Deny, or Replace.",
                extension_name,
                type(decision).__name__,
            )
        return ToolCallDecision(effective_input=current_input)

    async def dispatch_tool_result(
        self,
        ctx: HookContext,
        *,
        tool_name: str,
        tool_call_id: str,
        input: dict[str, Any],
        result: dict[str, Any],
        validator: ToolResultValidator,
    ) -> dict[str, Any]:
        """Replace-style pipeline: each handler may swap in a full envelope.

        Each handler receives the running envelope and returns a full
        replacement envelope (validated; valid replaces the running result,
        invalid is dropped) or ``None`` to leave it unchanged. There is no
        shallow-merge patching. Returns the final (possibly unchanged) envelope.
        """
        current = result
        for extension_name, handler in self._handlers.get("tool_result", []):
            payload = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "input": input,
                "result": current,
            }
            hook_result = await self._invoke("tool_result", extension_name, handler, ctx, payload)
            if hook_result is _HANDLER_FAILED or hook_result is None:
                continue
            if isinstance(hook_result, dict):
                validated = validator(extension_name, hook_result)
                if validated is not None:
                    current = validated
        return current


def _discover_extension_paths(extensions_dir: Path) -> list[tuple[str, Path]]:
    if not extensions_dir.is_dir():
        return []

    discovered: list[tuple[str, Path]] = []
    for entry in extensions_dir.iterdir():
        if entry.is_file() and entry.suffix == ".py" and entry.stem != "__init__":
            discovered.append((entry.stem, entry))
            continue

        if not entry.is_dir():
            continue

        init_entry = entry / "__init__.py"
        if init_entry.is_file():
            discovered.append((entry.name, init_entry))
            continue

        extension_entry = entry / "extension.py"
        if extension_entry.is_file():
            discovered.append((entry.name, extension_entry))

    return sorted(discovered, key=lambda item: item[0])


def _ensure_extension_parent_package() -> None:
    parent_module = sys.modules.get(_EXTENSION_PARENT_PACKAGE)
    if parent_module is None:
        parent_module = types.ModuleType(_EXTENSION_PARENT_PACKAGE)
        parent_module.__package__ = _EXTENSION_PARENT_PACKAGE
        parent_module.__path__ = []
        sys.modules[_EXTENSION_PARENT_PACKAGE] = parent_module
        return

    if not isinstance(getattr(parent_module, "__path__", None), list):
        parent_module.__path__ = []


def _extension_spec(module_name: str, entry_path: Path) -> Any:
    if entry_path.name == "__init__.py":
        return importlib.util.spec_from_file_location(
            module_name,
            entry_path,
            submodule_search_locations=[str(entry_path.parent)],
        )

    return importlib.util.spec_from_file_location(module_name, entry_path)


def _log_async_register_task_result(extension_name: str, task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        _LOGGER.warning("Extension %r async register() was cancelled", extension_name)
        return

    exc = task.exception()
    if exc is not None:
        _LOGGER.error("Extension %r async register() raised: %s", extension_name, exc)


def _async_register_done_callback(
    extension_name: str,
) -> Callable[[asyncio.Task[Any]], None]:
    def _done_callback(task: asyncio.Task[Any]) -> None:
        _log_async_register_task_result(extension_name, task)

    return _done_callback


def _load_extension_root(root: Path, registry: ExtensionRegistry) -> None:
    for name, entry_path in _discover_extension_paths(root):
        module_name = f"{_EXTENSION_PARENT_PACKAGE}.{name}"
        try:
            spec = _extension_spec(module_name, entry_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"No loader for extension entry point: {entry_path}")
            _ensure_extension_parent_package()
            module = importlib.util.module_from_spec(spec)
            previous_module = sys.modules.get(module_name)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                if previous_module is None:
                    sys.modules.pop(module_name, None)
                else:
                    sys.modules[module_name] = previous_module
                raise

            parent_module = sys.modules.get(_EXTENSION_PARENT_PACKAGE)
            if parent_module is not None:
                setattr(parent_module, name, module)
        except Exception as exc:
            _LOGGER.error("Failed to load extension %r from %s: %s", name, entry_path, exc)
            continue

        register_fn = getattr(module, "register", None)
        if register_fn is None:
            continue

        api = HooksAPI(registry, name)
        try:
            result = register_fn(api)
        except Exception as exc:
            _LOGGER.error("Extension %r register() raised: %s", name, exc)
            continue

        if inspect.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(result)
                task.add_done_callback(_async_register_done_callback(name))
            except RuntimeError:
                try:
                    asyncio.run(result)
                except Exception as exc:
                    _LOGGER.error("Extension %r async register() raised: %s", name, exc)


__all__ = [
    "Deny",
    "ExtensionRegistry",
    "HookContext",
    "HooksAPI",
    "Modify",
    "Replace",
    "ToolCallDecision",
    "ToolResultValidator",
]
