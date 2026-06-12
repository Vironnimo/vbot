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


@dataclass(frozen=True)
class HookContext:
    session_id: str
    agent_id: str


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

    async def dispatch_context(self, ctx: HookContext, *, messages: list) -> list | None:
        """Pipeline event: first handler returning a list wins, the rest are skipped.

        Returns the replacement message list, or ``None`` when no handler
        replaced the messages (chat then keeps its own list).
        """
        payload = {"messages": messages}
        for extension_name, handler in self._handlers.get("context", []):
            result = await self._invoke("context", extension_name, handler, ctx, payload)
            if result is _HANDLER_FAILED:
                continue
            if isinstance(result, list):
                return result
        return None

    async def dispatch_tool_call(
        self,
        ctx: HookContext,
        *,
        tool_name: str,
        tool_call_id: str,
        input: dict[str, Any],
        validator: ToolResultValidator,
    ) -> dict[str, Any] | None:
        """Pipeline event: first valid result envelope short-circuits the tool.

        Each handler dict is run through ``validator``; the first that passes
        wins and is returned. Returns ``None`` when nothing short-circuits.
        """
        payload = {"tool_name": tool_name, "tool_call_id": tool_call_id, "input": input}
        for extension_name, handler in self._handlers.get("tool_call", []):
            result = await self._invoke("tool_call", extension_name, handler, ctx, payload)
            if result is _HANDLER_FAILED:
                continue
            if isinstance(result, dict):
                validated = validator(extension_name, result)
                if validated is None:
                    continue
                return validated
        return None

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
        """Pipeline event: every handler shallow-merge-patches the envelope in turn.

        Each handler's dict is shallow-merged onto the current envelope and
        re-validated; valid patches replace the running result, invalid ones are
        dropped. Returns the final (possibly unchanged) envelope.
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
            if hook_result is _HANDLER_FAILED:
                continue
            if isinstance(hook_result, dict):
                patched = dict(current)
                patched.update(hook_result)
                validated = validator(extension_name, patched)
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


__all__ = ["ExtensionRegistry", "HookContext", "HooksAPI", "ToolResultValidator"]
