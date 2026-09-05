"""Extension-owned live rebuild and deactivation sequencing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.extensions.extensions import ExtensionRegistry, purge_extension_modules
from core.extensions.operations import ExtensionHost
from core.storage import StorageManager


class ExtensionRuntime:
    """Serialize and apply the complete mutable Extension layer."""

    def __init__(
        self,
        *,
        storage: StorageManager,
        resources_path: Path,
        tools: Any,
        get_registry: Callable[[], ExtensionRegistry | None],
        set_registry: Callable[[ExtensionRegistry], None],
        get_command_dispatcher: Callable[[], Any | None],
        extra_directories: Callable[[dict[str, object]], list[Path]],
        load_options: Callable[
            [dict[str, object]],
            tuple[set[str], dict[str, dict[str, object]]],
        ],
        live_config: Callable[[str], dict[str, Any]],
        resolve_credential: Callable[[str], str],
        reload_recall: Callable[[], None],
        refresh_prompts: Callable[[], None],
        reload_skills: Callable[[], None],
        recover_recall: Callable[[set[str]], None],
        logger: Any,
        make_host: Callable[[], ExtensionHost] | None = None,
    ) -> None:
        self._storage = storage
        self._resources_path = resources_path
        self._tools = tools
        self._get_registry = get_registry
        self._set_registry = set_registry
        self._get_command_dispatcher = get_command_dispatcher
        self._extra_directories = extra_directories
        self._load_options = load_options
        self._live_config = live_config
        self._resolve_credential = resolve_credential
        self._reload_recall = reload_recall
        self._refresh_prompts = refresh_prompts
        self._reload_skills = reload_skills
        self._recover_recall = recover_recall
        self._logger = logger
        self._make_host = make_host
        self._mutation_lock = asyncio.Lock()

    async def reload(self) -> None:
        """Rebuild the layer from current disk and Settings, restart-equivalently."""
        async with self._mutation_lock:
            settings = self._storage.load_settings()
            extension_dirs = self._extra_directories(settings)
            disabled, config = self._load_options(settings)
            dispatcher = self._get_command_dispatcher()

            old_registry = self._get_registry()
            if old_registry is not None:
                old_registry.remove_applied_tools(self._tools)
                if dispatcher is not None:
                    old_registry.remove_applied_commands(dispatcher)
                await old_registry.fire_shutdown()

            purge_extension_modules()
            new_registry = await ExtensionRegistry.aload(
                self._storage.data_dir / "extensions",
                extra_dirs=extension_dirs,
                disabled=disabled,
                config=config,
                bundled_dir=self._resources_path / "extensions",
                config_provider=self._live_config,
                credential_resolver=self._resolve_credential,
            )
            failed_count = len(new_registry.diagnostics())
            if failed_count > 0:
                self._logger.warning(
                    "Reloaded extensions with %s failed extensions; "
                    "see vbot.extensions errors for details",
                    failed_count,
                )

            self._set_registry(new_registry)
            new_registry.apply_tools(self._tools)
            if dispatcher is not None:
                new_registry.apply_commands(dispatcher)
            self._reload_recall()
            self._refresh_prompts()
            self._reload_skills()
            if self._make_host is not None:
                new_registry.bind_host(self._make_host())
            await new_registry.fire_startup()

            records = new_registry.records()
            self._logger.info(
                "Extension layer reloaded: %s loaded, %s failed, %s disabled, %s overridden",
                sum(1 for record in records if record.status == "loaded"),
                sum(1 for record in records if record.status == "failed"),
                sum(1 for record in records if record.status == "disabled"),
                sum(1 for record in records if record.status == "overridden"),
            )

    async def apply_disabled_change(self, newly_disabled: set[str]) -> None:
        """Deactivate newly disabled Extensions and rebuild their live projections."""
        if not newly_disabled:
            return
        async with self._mutation_lock:
            registry = self._get_registry()
            if registry is None:
                return
            removed_backends = self._recall_backend_names(registry, newly_disabled)
            dispatcher = self._get_command_dispatcher()
            for name in newly_disabled:
                await registry.deactivate(name, self._tools, dispatcher)
            self._refresh_prompts()
            self._reload_skills()
            self._recover_recall(removed_backends)

    @staticmethod
    def _recall_backend_names(
        registry: ExtensionRegistry,
        names: set[str],
    ) -> set[str]:
        backend_names: set[str] = set()
        for record in registry.records():
            if record.name in names and record.status == "loaded":
                backend_names.update(
                    declaration.name for declaration in record.declarations.recall_backends
                )
        return backend_names
