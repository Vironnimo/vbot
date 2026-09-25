"""Runtime-owned Recall selection, reload and derived-index cleanup."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Callable

from core.database import DatabaseError
from core.extensions import ExtensionRegistry
from core.model_tasks import EmbeddingService
from core.models.models import ModelRegistry
from core.recall import (
    DEFAULT_RECALL_BACKEND,
    RecallBackend,
    RecallBackendContext,
    RecallBackendRegistry,
    SupportsClose,
    SupportsSessionRemoval,
)
from core.runtime.interfaces import LoggerProtocol
from core.sessions import ChatSessionManager
from core.settings.settings import effective_timezone_name
from core.storage.storage import StorageManager
from core.tools import (
    register_session_search_tool,
)
from core.tools.tools import ToolRegistry


class RecallIntegration:
    """Select live Recall backends and keep their Session Tools in sync."""

    def __init__(
        self,
        *,
        storage: StorageManager,
        sessions: ChatSessionManager,
        tools: ToolRegistry,
        models: ModelRegistry,
        embeddings: EmbeddingService,
        extensions: Callable[[], ExtensionRegistry | None],
        logger: LoggerProtocol | None,
    ) -> None:
        self._storage = storage
        self._chat_sessions = sessions
        self._tools = tools
        self._models = models
        self._embeddings = embeddings
        self._extensions = extensions
        self.logger = logger
        self._recall_backend_registry = self._build_recall_backend_registry()
        self.backend = self._create_recall_backend(self._recall_backend_registry)
        # Closing replaced backends, kept referenced until they finish.
        self._retiring: set[asyncio.Task[None]] = set()
        self._register_session_search()

    def _register_session_search(self) -> None:
        register_session_search_tool(
            self._tools,
            self.backend,
            self._chat_sessions,
            timezone_name_loader=self._timezone_name,
        )

    def _timezone_name(self) -> str:
        """The Settings timezone that reads search periods given without an offset."""
        return effective_timezone_name(self._storage.load_settings())

    def _build_recall_backend_registry(self) -> RecallBackendRegistry:
        """Build a builtins registry with extension recall backends applied.

        Extension declarations were collected during extension load, so a fresh
        ``with_builtins()`` registry plus ``apply_recall_backends`` yields the
        same backend set on first build and on every ``reload_recall_backend``.
        """
        registry = RecallBackendRegistry.with_builtins()
        extensions = self._extensions()
        if extensions is not None:
            extensions.apply_recall_backends(registry)
        return registry

    def _create_recall_backend(self, registry: RecallBackendRegistry) -> RecallBackend:
        settings = self._storage.load_recall_settings()
        backend_name = settings["backend"]
        context = RecallBackendContext(
            data_dir=self._storage.data_dir,
            sessions=self._chat_sessions,
            logger=self.logger,
            embeddings=self._embeddings,
            model_registry=self._models,
        )
        try:
            backend = registry.create(backend_name, context)
            return backend
        except KeyError:
            if self.logger is not None:
                self.logger.warning(
                    "Unknown recall backend %r; using %s",
                    backend_name,
                    DEFAULT_RECALL_BACKEND,
                )
            return registry.create(DEFAULT_RECALL_BACKEND, context)
        except Exception as error:
            if backend_name == DEFAULT_RECALL_BACKEND:
                raise
            if self.logger is not None:
                self.logger.warning(
                    "Recall backend %r could not start; using %s: %s",
                    backend_name,
                    DEFAULT_RECALL_BACKEND,
                    error,
                )
            return registry.create(DEFAULT_RECALL_BACKEND, context)

    def reload_recall_backend(self) -> None:
        """Reload Session Recall tools from the current persisted backend setting.

        Rebuilds the registry from ``with_builtins()`` and re-applies extension
        recall backends, so a live backend switch can still resolve an
        extension-registered backend.
        """
        recall_registry = self._build_recall_backend_registry()
        self._recall_backend_registry = recall_registry
        previous = self.backend
        self.backend = self._create_recall_backend(recall_registry)
        if self._tools is not None:
            self._tools.unregister("session_search")
            self._register_session_search()
        self._retire(previous)

    def _retire(self, backend: RecallBackend) -> None:
        """Close a replaced backend so its index files are released.

        On the Event Loop the backend closes in a task that waits for its
        background work; without a running loop it closes at once.
        """
        if not isinstance(backend, SupportsClose):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._close_quietly(backend)
            return
        task = loop.create_task(self._aclose_quietly(backend))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def aclose(self) -> None:
        """Close the active backend and wait for replaced ones to finish closing."""
        retiring = set(self._retiring)
        if isinstance(self.backend, SupportsClose):
            await self._aclose_quietly(self.backend)
        if retiring:
            await asyncio.wait(retiring)

    def close(self) -> None:
        """Close the active backend without waiting for its background work."""
        if isinstance(self.backend, SupportsClose):
            self._close_quietly(self.backend)

    async def _aclose_quietly(self, backend: SupportsClose) -> None:
        try:
            await backend.aclose()
        except Exception as error:
            self._warn_close_failure(error)

    def _close_quietly(self, backend: SupportsClose) -> None:
        try:
            backend.close()
        except Exception as error:
            self._warn_close_failure(error)

    def _warn_close_failure(self, error: Exception) -> None:
        if self.logger is not None:
            self.logger.warning("Recall backend could not close cleanly: %s", error)

    def _recover_recall_backend_if_deactivated(self, removed_backend_names: set[str]) -> None:
        """Fall the active recall backend back to the default if its provider left.

        If a deactivated extension provided the *currently-selected* recall backend,
        the live backend instance now points into dormant extension code. Rather than
        leave recall broken, rebuild the registry (which no longer contains the
        deactivated extension's backend) and re-resolve: an unknown selected name
        falls back to the built-in default (``sqlite_fts``) with a warning, exactly
        as it would on the next restart. The persisted ``recall.backend`` selection
        is left untouched — re-enabling the extension (a restart) restores it.
        """
        if not removed_backend_names or self._storage is None:
            return
        active_backend = self._storage.load_recall_settings()["backend"]
        if active_backend not in removed_backend_names:
            # Removed declarations must leave the selectable catalog even when
            # the active backend can remain intact.
            self._recall_backend_registry = self._build_recall_backend_registry()
            return
        if self.logger is not None:
            self.logger.warning(
                "Recall backend %r was provided by a disabled extension; "
                "falling back to %s until the extension is re-enabled",
                active_backend,
                DEFAULT_RECALL_BACKEND,
            )
        self.reload_recall_backend()

    def available_recall_backends(self) -> list[str]:
        """Return all selectable recall backend names (built-ins + extensions)."""
        return self._recall_backend_registry.names()

    async def remove_session_from_recall(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict a removed session from the active recall index (best-effort).

        Session deletion calls this so a deleted session stops surfacing in
        search immediately, rather than waiting for the next self-healing
        reconcile. Backends without a derived index (the canonical scan) do not
        implement removal and are skipped. Index cleanup is non-fatal — the index
        is disposable and reconciles on the next search — so an index I/O error is
        logged and swallowed instead of failing the delete.
        """
        backend = self.backend
        if not isinstance(backend, SupportsSessionRemoval):
            return
        try:
            remove_session = backend.remove_session
            if inspect.iscoroutinefunction(remove_session):
                await remove_session(agent_id, session_id, project_id)
            else:
                result = await asyncio.to_thread(
                    remove_session,
                    agent_id,
                    session_id,
                    project_id,
                )
                if inspect.isawaitable(result):
                    await result
        except (OSError, sqlite3.Error, DatabaseError) as error:
            if self.logger is not None:
                self.logger.warning(
                    "Recall index cleanup failed for session %s/%s: %s",
                    agent_id,
                    session_id,
                    error,
                )
