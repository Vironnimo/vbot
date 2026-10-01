"""Runtime-owned Recall: backend selection, the Passage index and its indexer."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.database import DatabaseError, has_live_connection
from core.extensions import ExtensionRegistry
from core.model_tasks import EmbeddingService
from core.models.models import ModelRegistry
from core.recall import (
    DEFAULT_RECALL_BACKEND,
    SEMANTIC_RECALL_BACKENDS,
    IndexStatus,
    PassageIndex,
    RecallBackend,
    RecallBackendContext,
    RecallBackendRegistry,
    SemanticIndexer,
    SupportsClose,
    SupportsSessionRemoval,
)
from core.recall.passage_index import INDEX_DIR_NAME, INDEX_FILE_NAME
from core.runs import ChatRunManager, Run, RunStatus
from core.runtime.interfaces import LoggerProtocol
from core.sessions import ChatSessionManager
from core.settings.settings import effective_timezone_name
from core.storage.storage import StorageManager
from core.tools import (
    register_session_search_tool,
)
from core.tools.tools import ToolRegistry

# Index cleanup is best-effort: the index is disposable and catches up later.
_INDEX_FAILURES = (OSError, sqlite3.Error, DatabaseError)
# SQLite files that belong to a database file of the same name.
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def text_embedding_binding(settings: Any) -> Any:
    """The persisted ``text_embedding`` binding of *settings*, or ``None``."""
    model_tasks = settings.get("model_tasks") if isinstance(settings, dict) else None
    return model_tasks.get("text_embedding") if isinstance(model_tasks, dict) else None


class RecallIntegration:
    """Select live Recall backends and keep their Session Tools in sync.

    It owns the one Passage index the ``vector`` and ``hybrid`` backends share
    and the :class:`SemanticIndexer` that fills its vectors; both outlive
    backend reloads. ``<data>/recall/`` belongs to it alone.
    """

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
        self._remove_stale_recall_files(storage.data_dir / INDEX_DIR_NAME)
        self.index = PassageIndex(storage.data_dir)
        self.indexer = SemanticIndexer(
            index=self.index,
            sessions=sessions,
            embeddings=embeddings,
            binding_configured=self._embedding_configured,
            pricing=models.pricing_for if models is not None else None,
            logger=logger,
        )
        # A local Model that just finished installing may be the bound one.
        embeddings.add_local_ready_listener(lambda _target: self.indexer.settings_changed())
        self._unobserve_runs: Callable[[], None] | None = None
        # The configured backend name last reported as unknown: every reload of an
        # unchanged setting would otherwise warn again.
        self._unknown_backend: str | None = None
        self._recall_backend_registry = self._build_recall_backend_registry()
        self.backend_name = DEFAULT_RECALL_BACKEND
        self.backend = self._create_recall_backend(self._recall_backend_registry)
        self.indexer.set_enabled(self.backend_name in SEMANTIC_RECALL_BACKENDS)
        # Closing replaced backends, kept referenced until they finish.
        self._retiring: set[asyncio.Task[None]] = set()
        self._register_session_search()

    # -- Lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Start background indexing on the running Event Loop."""
        self.indexer.start()

    def observe_runs(self, runs: ChatRunManager) -> None:
        """Nudge the indexer after every Run reaches a terminal state."""

        async def finished(_status: RunStatus) -> None:
            self.indexer.run_finished()

        def started(run: Run) -> None:
            run.add_completion_observer(finished)

        self._unobserve_runs = runs.add_run_started_callback(started)

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

    def _embedding_configured(self) -> bool:
        binding = text_embedding_binding(self._storage.load_settings())
        return isinstance(binding, dict) and bool(binding.get("target"))

    def _build_recall_backend_registry(self) -> RecallBackendRegistry:
        """Build a builtins registry with extension recall backends applied.

        Extension declarations were collected during extension load, so a fresh
        ``with_builtins()`` registry plus ``apply_recall_backends`` yields the
        same backend set on first build and on every ``reload_recall_backend``.
        """
        registry = RecallBackendRegistry.with_builtins(
            passage_index=self.index, on_waiting=self.indexer.waiting_found
        )
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
            self._unknown_backend = None
            self.backend_name = backend_name
            return backend
        except KeyError:
            if self.logger is not None and backend_name != self._unknown_backend:
                self.logger.warning(
                    "Used default recall backend instead of an unknown one "
                    "(configured=%s default=%s)",
                    backend_name,
                    DEFAULT_RECALL_BACKEND,
                )
            self._unknown_backend = backend_name
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
        self.backend_name = DEFAULT_RECALL_BACKEND
        return registry.create(DEFAULT_RECALL_BACKEND, context)

    def reload_recall_backend(self) -> None:
        """Reload Session Recall tools from the current persisted backend setting.

        Rebuilds the registry from ``with_builtins()`` and re-applies extension
        recall backends, so a live backend switch can still resolve an
        extension-registered backend. The Passage index and its indexer stay.
        """
        recall_registry = self._build_recall_backend_registry()
        self._recall_backend_registry = recall_registry
        previous = self.backend
        self.backend = self._create_recall_backend(recall_registry)
        self.indexer.set_enabled(self.backend_name in SEMANTIC_RECALL_BACKENDS)
        if self._tools is not None:
            self._tools.unregister("session_search")
            self._register_session_search()
        self._retire(previous)

    def embedding_binding_changed(self) -> None:
        """The ``text_embedding`` binding changed: index for it without waiting."""
        self.indexer.settings_changed()

    def _retire(self, backend: RecallBackend) -> None:
        """Close a replaced backend so the resources it owns are released.

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
        """Stop indexing, close the active backend and the Passage index."""
        self._stop_observing_runs()
        retiring = set(self._retiring)
        try:
            await self.indexer.aclose()
        except Exception as error:
            self._warn_close_failure(error)
        if isinstance(self.backend, SupportsClose):
            await self._aclose_quietly(self.backend)
        if retiring:
            await asyncio.wait(retiring)
        self._close_index()

    def close(self) -> None:
        """Close everything without waiting for background work."""
        self._stop_observing_runs()
        try:
            self.indexer.close()
        except Exception as error:
            self._warn_close_failure(error)
        if isinstance(self.backend, SupportsClose):
            self._close_quietly(self.backend)
        self._close_index()

    def _stop_observing_runs(self) -> None:
        unobserve, self._unobserve_runs = self._unobserve_runs, None
        if unobserve is not None:
            unobserve()

    def _close_index(self) -> None:
        try:
            self.index.close()
        except Exception as error:
            self._warn_close_failure(error)

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

    # -- Semantic index ------------------------------------------------------------

    async def index_status(self) -> IndexStatus:
        """State, coverage, spent usage and estimate of semantic indexing."""
        return await self.indexer.status()

    async def rebuild_index(self) -> IndexStatus:
        """Drop the vectors of the current space and embed every Passage again."""
        await self.indexer.rebuild()
        return await self.indexer.status()

    def add_index_status_listener(
        self, listener: Callable[[IndexStatus], None]
    ) -> Callable[[], None]:
        """Call *listener* with every published index status; returns the unsubscribe call."""
        return self.indexer.add_listener(listener)

    # -- Deletion hygiene ----------------------------------------------------------

    async def remove_session_from_recall(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> None:
        """Evict a removed Session from every Recall index (best-effort).

        Session deletion calls this so a deleted Session stops surfacing at
        once. The Passage index is cleaned whenever it exists, whichever backend
        is active; an Extension backend with its own index cleans it through
        ``remove_session``. Index cleanup is non-fatal — the indexes are
        disposable and catch up on the next refresh — so an index I/O error is
        logged and swallowed instead of failing the delete.
        """
        try:
            if self.index.path.exists():
                await self.index.remove_session(agent_id, project_id, session_id)
            backend = self.backend
            if isinstance(backend, SupportsSessionRemoval):
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
        except _INDEX_FAILURES as error:
            if self.logger is not None:
                self.logger.warning(
                    "Recall index cleanup failed for session %s/%s: %s",
                    agent_id,
                    session_id,
                    error,
                )

    async def remove_agent_from_recall(self, agent_id: str, project_id: str | None = None) -> None:
        """Evict every Session of one Agent scope from the Passage index (best-effort)."""
        if not self.index.path.exists():
            return
        try:
            await self.index.remove_scope(agent_id, project_id)
        except _INDEX_FAILURES as error:
            if self.logger is not None:
                self.logger.warning("Recall index cleanup failed for agent %s: %s", agent_id, error)

    def _remove_stale_recall_files(self, directory: Path) -> None:
        """Delete every file in *directory* that is not a current Recall database.

        Recall owns the directory, so files of earlier index layouts and their
        SQLite sidecars go. Files a database handle in this process holds open
        stay, as does a file that cannot be deleted now.
        """
        if not directory.is_dir():
            return
        removed = 0
        for path in sorted(directory.iterdir()):
            if not path.is_file() or _is_current_recall_file(path.name):
                continue
            database = _sidecar_database(path)
            if has_live_connection(path) or has_live_connection(database):
                continue
            try:
                path.unlink()
            except OSError as error:
                if self.logger is not None:
                    self.logger.warning(
                        "Stale Recall file could not be removed (file=%s): %s", path.name, error
                    )
                continue
            removed += 1
        if removed and self.logger is not None:
            self.logger.info("Removed stale Recall files (count=%d)", removed)


def _is_current_recall_file(name: str) -> bool:
    return name == INDEX_FILE_NAME or any(
        name == INDEX_FILE_NAME + suffix for suffix in _SQLITE_SIDECARS
    )


def _sidecar_database(path: Path) -> Path:
    """The database a SQLite sidecar belongs to; *path* itself otherwise."""
    for suffix in _SQLITE_SIDECARS:
        if path.name.endswith(suffix):
            return path.with_name(path.name.removesuffix(suffix))
    return path
