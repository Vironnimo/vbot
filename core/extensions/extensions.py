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

    async def fire(self, event: str, ctx: HookContext, **payload: Any) -> list[Any]:
        results: list[Any] = []
        for extension_name, handler in self._handlers.get(event, []):
            try:
                result = handler(ctx, **payload)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                _LOGGER.warning(
                    "Extension %r %s handler raised: %s",
                    extension_name,
                    event,
                    exc,
                )
                continue

            if result is not None:
                results.append(result)

        return results


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


__all__ = ["ExtensionRegistry", "HookContext", "HooksAPI"]
