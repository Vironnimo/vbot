"""Extension hooks registry and loader for local Python extensions."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("extensions")

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


def _load_extension_root(root: Path, registry: ExtensionRegistry) -> None:
    for name, entry_path in _discover_extension_paths(root):
        try:
            spec = importlib.util.spec_from_file_location(f"vbot_ext.{name}", entry_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"No loader for extension entry point: {entry_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
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
                loop.create_task(result)
            except RuntimeError:
                try:
                    asyncio.run(result)
                except Exception as exc:
                    _LOGGER.error("Extension %r async register() raised: %s", name, exc)


__all__ = ["ExtensionRegistry", "HookContext", "HooksAPI"]
