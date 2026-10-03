"""Storage manager for vBot data directories, settings, and prompt fragments.

``StorageManager`` owns the data-directory lifecycle, ``.env`` credential snapshots,
and serialized read-modify-write transactions over ``settings.json``. Section
normalization is delegated to :mod:`core.settings.normalizers` (the settings domain
owns the section schemas) and prompt fragments to
:class:`core.storage.prompt_fragments.PromptFragmentStore`.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Protocol, cast

from core.json_documents import (
    FORMAT_VERSION_FIELD,
    JsonDocumentWriteError,
    check_json_document_writable,
    strip_unknown_fields,
    write_json_document,
)
from core.settings import (
    SETTINGS_FORMAT,
    SettingsValidationError,
    load_runtime_settings_json,
    load_validated_settings_json,
)
from core.settings.normalizers import (
    SUPPORTED_APPEARANCE_LANGUAGES,
    custom_provider_revision,
    normalize_appearance_settings,
    normalize_archive_settings,
    normalize_compaction_settings,
    normalize_custom_provider_id,
    normalize_custom_provider_settings,
    normalize_debug_settings,
    normalize_defaults_settings,
    normalize_extensions_settings,
    normalize_librarian_settings,
    normalize_local_models_settings,
    normalize_model_task_settings,
    normalize_notification_settings,
    normalize_providers_settings,
    normalize_recall_settings,
    normalize_reflection_settings,
    normalize_session_title_settings,
    normalize_skill_directories,
    normalize_speech_settings,
    normalize_subagent_integer,
    normalize_web_fetch_settings,
    normalize_web_search_settings,
)
from core.settings.paths import (
    SUBAGENT_SETTING_DEFAULTS,
    SettingsPatchOperation,
    apply_settings_patch,
)
from core.settings.settings import SETTINGS_UPDATE_SECTIONS
from core.storage import _settings_updates as settings_updates
from core.storage.errors import SettingsConflictError, StorageError
from core.storage.layout import DataDirectoryLayout, initialize_data_directory
from core.storage.prompt_blocks import PromptBlockStore
from core.storage.prompt_fragments import PromptFragmentStore
from core.storage.temp_files import TemporaryFileManager
from core.utils.atomic import atomic_write_text
from core.utils.config import (
    build_environment_snapshot,
    format_env_value,
    read_env_file,
    split_env_lines,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.prompts import LayoutEntry

# A settings file modified this recently may change again within the same
# filesystem timestamp tick, so its stat stamp cannot prove it unchanged yet.
_SETTINGS_RACY_WINDOW_NS = 3_000_000_000
_LOGGER = get_logger("storage")

DEFAULT_DATA_DIR = Path.home() / ".vbot"
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigProtocol(Protocol):
    """Minimal config interface used to resolve the data directory."""

    def get(self, key: str, default: Any = None) -> Any:
        """Return a config value."""


class StorageManager:
    """Owns data-directory setup, settings JSON, and prompt fragments."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        config: ConfigProtocol | None = None,
        resources_dir: str | Path | None = None,
    ) -> None:
        self.data_dir = self._resolve_data_dir(data_dir, config).expanduser()
        self.resources_dir = self._resolve_resources_dir(resources_dir)
        self.layout = DataDirectoryLayout(self.data_dir)
        self._settings_lock = RLock()
        self._settings_diagnostic_signature: tuple[str, ...] | None = None
        # Usable Settings of the last loaded file version as JSON, with its stat stamp.
        self._settings_cache: tuple[tuple[int, int, int], str] | None = None
        self.temporary_files = TemporaryFileManager(self.data_dir)
        self._prompt_fragments = PromptFragmentStore(
            data_dir=self.data_dir,
            resources_dir=self.resources_dir,
            ensure_directories=self.ensure_directories,
        )
        self._prompt_blocks = PromptBlockStore(
            data_dir=self.data_dir,
            ensure_directories=self.ensure_directories,
        )

    @property
    def settings_path(self) -> Path:
        """Path to the instance settings JSON file."""

        return self.layout.settings_file

    @property
    def prompts_dir(self) -> Path:
        """Path to user-copy prompt fragments in the data directory."""

        return self.layout.prompts

    @property
    def resource_prompts_dir(self) -> Path:
        """Path to bundled default prompt fragments."""

        return self.resources_dir / "prompts"

    def ensure_directories(self) -> None:
        """Create the canonical data-directory structure if it is missing."""

        initialize_data_directory(self.data_dir, resources_dir=self.resources_dir)

    def load_environment(self) -> dict[str, str]:
        """Return a read-only snapshot of credentials from ``<data_dir>/.env``.

        The returned mapping is suitable for later merging with the live
        process environment, but this method never mutates ``os.environ``.
        """

        return self.load_data_dir_credentials()

    def load_data_dir_credentials(self) -> dict[str, str]:
        """Read ``<data_dir>/.env`` as a credential fallback snapshot."""

        return read_env_file(self.data_dir / ".env")

    def set_data_dir_credential(self, key: str, value: str) -> None:
        """Write or replace one credential in ``<data_dir>/.env``."""

        if not ENV_KEY_PATTERN.fullmatch(key):
            raise StorageError(f"Invalid environment key: {key}")
        if not value:
            raise StorageError("Credential value must not be empty")
        if "\n" in value or "\r" in value:
            raise StorageError("Credential value must be a single line")

        self.ensure_directories()
        with self._settings_lock:
            env_path = self.data_dir / ".env"
            # A file that exists but cannot be read is never replaced: rewriting it
            # from nothing would drop every other credential.
            try:
                lines = split_env_lines(env_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                lines = []
            except OSError as exc:
                raise StorageError(f"Cannot read {env_path}: {exc}") from exc

            new_line = f"{key}={format_env_value(value)}"
            updated_lines: list[str] = []
            replaced = False
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    candidate_key = stripped.partition("=")[0].strip()
                    if candidate_key == key:
                        if not replaced:
                            updated_lines.append(new_line)
                            replaced = True
                        continue
                updated_lines.append(line)

            if not replaced:
                updated_lines.append(new_line)

            try:
                atomic_write_text(
                    env_path,
                    "\n".join(updated_lines) + "\n",
                    data_dir=self.data_dir,
                )
            except OSError as exc:
                raise StorageError(f"Cannot write {env_path}: {exc}") from exc

    def remove_data_dir_credential(self, key: str) -> bool:
        """Remove one credential from ``<data_dir>/.env``.

        Returns whether a matching entry existed. Process-environment values
        are never touched; removing a key that is also set in the process
        environment leaves that credential resolvable.
        """

        if not ENV_KEY_PATTERN.fullmatch(key):
            raise StorageError(f"Invalid environment key: {key}")

        with self._settings_lock:
            env_path = self.data_dir / ".env"
            try:
                lines = split_env_lines(env_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise StorageError(f"Cannot read {env_path}: {exc}") from exc

            updated_lines: list[str] = []
            removed = False
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    candidate_key = stripped.partition("=")[0].strip()
                    if candidate_key == key:
                        removed = True
                        continue
                updated_lines.append(line)

            if not removed:
                return False

            self.ensure_directories()
            try:
                atomic_write_text(
                    env_path,
                    "\n".join(updated_lines) + "\n",
                    data_dir=self.data_dir,
                )
            except OSError as exc:
                raise StorageError(f"Cannot write {env_path}: {exc}") from exc
            return True

    def build_environment_snapshot(self) -> dict[str, str]:
        """Return process-env-over-data-dir merged credentials without mutation."""

        return build_environment_snapshot(
            process_env=os.environ,
            fallback_env=self.load_data_dir_credentials(),
        )

    def load_settings(self) -> dict[str, Any]:
        """Load usable Settings, isolating invalid keys or falling back to defaults.

        Read paths must not make one malformed global setting fatal to the app.
        Valid siblings remain live when a schema error can be isolated. The
        original file stays untouched and write transactions still use the strict
        loader so a later save can never overwrite invalid user data.

        An unchanged file version is served from memory: its stat stamp (taken
        before reading) matches and the file is older than the racy window.
        Every call returns an independent copy.
        """

        with self._settings_lock:
            stamp = self._settings_stamp()
            cached = self._settings_cache
            if stamp is not None and cached is not None and cached[0] == stamp:
                return cast(dict[str, Any], json.loads(cached[1]))
            settings = self._load_usable_settings()
            self._settings_cache = None
            if stamp is not None and time.time_ns() - stamp[0] >= _SETTINGS_RACY_WINDOW_NS:
                self._settings_cache = (stamp, json.dumps(settings))
            return settings

    def _settings_stamp(self) -> tuple[int, int, int] | None:
        try:
            stat = self.settings_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _load_usable_settings(self) -> dict[str, Any]:
        """Read and validate ``settings.json``, logging each new degradation once."""
        try:
            settings, ignored = load_runtime_settings_json(self.settings_path)
        except SettingsValidationError as exc:
            self._warn_settings_degradation(
                (str(exc),),
                "Ignoring invalid settings file %s and using defaults: %s",
                exc,
            )
            return {}
        if ignored:
            details = "; ".join(
                f"{diagnostic.path}: {diagnostic.message}" for diagnostic in ignored
            )
            self._warn_settings_degradation(
                tuple(f"{diagnostic.path}: {diagnostic.message}" for diagnostic in ignored),
                "Ignoring invalid Settings keys in %s while keeping valid siblings: %s",
                details,
            )
        else:
            self._settings_diagnostic_signature = None
        return settings

    def _warn_settings_degradation(
        self,
        signature: tuple[str, ...],
        message: str,
        details: Any,
    ) -> None:
        """Log a persisted Settings problem once until its diagnostics change."""
        if signature == self._settings_diagnostic_signature:
            return
        self._settings_diagnostic_signature = signature
        _LOGGER.warning(message, self.settings_path, details)

    def _load_settings_for_update(self) -> dict[str, Any]:
        """Strictly load Settings before a mutation so invalid data is preserved."""
        try:
            return load_validated_settings_json(self.settings_path)
        except SettingsValidationError as exc:
            raise StorageError(str(exc)) from exc

    def update_settings[SettingsUpdateResult](
        self,
        mutator: Callable[[dict[str, Any]], SettingsUpdateResult],
    ) -> SettingsUpdateResult:
        """Apply one read-modify-write transaction to ``settings.json``."""

        if not callable(mutator):
            raise StorageError("Settings mutator must be callable")

        with self._settings_lock:
            merged_settings = dict(self._load_settings_for_update())
            result = mutator(merged_settings)
            self.save_settings(merged_settings)
            return result

    def patch_settings(
        self,
        operations: list[SettingsPatchOperation],
        *,
        validate_candidate: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]:
        """Patch known Settings while pruning against a strict raw snapshot.

        The optional runtime validator sees only known fields and runs before
        the write. Unknown fields stay within the persistence boundary, where
        their presence prevents a leaf reset from pruning their parent object.
        """
        with self._settings_lock:
            try:
                document = check_json_document_writable(self.settings_path, SETTINGS_FORMAT) or {}
            except JsonDocumentWriteError as exc:
                raise StorageError(str(exc)) from exc
            previous = cast("dict[str, Any]", strip_unknown_fields(document, SETTINGS_FORMAT.shape))
            previous.pop(FORMAT_VERSION_FIELD, None)
            candidate, changed_paths = apply_settings_patch(
                previous, operations, preservation_source=document
            )
            if validate_candidate is not None:
                validate_candidate(previous, candidate)
            self.save_settings(candidate)
            return previous, candidate, changed_paths

    def update_settings_sections(
        self,
        settings_update: Mapping[str, Any],
        *,
        base: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a parsed public Settings update in one settings transaction.

        With ``base`` (the caller's view of the edited sections, in the update's
        shape), the write is refused with :class:`SettingsConflictError` when it
        would change a value that no longer matches that view.
        """

        if not isinstance(settings_update, Mapping):
            raise StorageError("Settings update must be a mapping")

        unsupported_sections = sorted(set(settings_update) - SETTINGS_UPDATE_SECTIONS)
        if unsupported_sections:
            raise StorageError(f"Unsupported settings sections: {', '.join(unsupported_sections)}")

        def apply_update(settings: dict[str, Any]) -> dict[str, Any]:
            if base is not None:
                stale_paths = settings_updates.stale_setting_paths(settings, settings_update, base)
                if stale_paths:
                    # An expected outcome: the writer re-reads and reapplies its change.
                    _LOGGER.debug("Stale settings update refused (paths=%s)", ",".join(stale_paths))
                    raise SettingsConflictError(stale_paths)
            return settings_updates.apply_settings_update(settings, settings_update)

        return self.update_settings(apply_update)

    def supported_appearance_languages(self) -> list[str]:
        """Return language codes supported by the persisted Settings surface."""

        return sorted(SUPPORTED_APPEARANCE_LANGUAGES)

    def load_appearance_settings(self) -> dict[str, str]:
        """Return normalized persisted Appearance settings."""

        settings = self.load_settings()
        return normalize_appearance_settings(settings.get("appearance"))

    def load_speech_settings(self) -> dict[str, Any]:
        """Return live Provider-facing transcription audio settings."""

        settings = self.load_settings()
        return normalize_speech_settings(settings.get("speech"))

    def load_skill_directory_settings(self) -> list[str]:
        """Return normalized extra skill directory settings."""

        settings = self.load_settings()
        return normalize_skill_directories(settings.get("skill_directories"))

    def load_subagent_settings(self) -> dict[str, int]:
        """Return normalized persisted Sub-Agent settings."""

        settings = self.load_settings()
        return {
            key: normalize_subagent_integer(key, settings.get(key), default)
            for key, default in SUBAGENT_SETTING_DEFAULTS.items()
        }

    def load_compaction_settings(self) -> dict[str, Any]:
        """Return normalized persisted compaction settings."""

        settings = self.load_settings()
        return normalize_compaction_settings(settings.get("compaction"))

    def load_defaults(self) -> dict[str, Any]:
        """Return normalized persisted defaults settings."""

        settings = self.load_settings()
        return normalize_defaults_settings(settings.get("defaults"))

    def load_session_title_settings(self) -> dict[str, Any]:
        """Return live automatic Session-title settings."""
        settings = self.load_settings()
        return normalize_session_title_settings(settings.get("session_titles"))

    def load_recall_settings(self) -> dict[str, str]:
        """Return normalized persisted recall backend settings."""

        settings = self.load_settings()
        return normalize_recall_settings(settings.get("recall"))

    def load_local_models_settings(self) -> dict[str, Any]:
        """Return normalized persisted local-models settings.

        Shape: ``{"context_windows": {"<provider>/<model_id>": positive int}}``.
        Read live at call time (no reload hook) by the effective-context-window
        resolution and the Ollama adapter's ``num_ctx`` enforcement.
        """

        settings = self.load_settings()
        return normalize_local_models_settings(settings.get("local_models"))

    def load_providers_settings(self) -> dict[str, Any]:
        """Return normalized persisted providers settings.

        This established Connection-facing surface remains intentionally narrow;
        OpenRouter request policy and Custom Provider definitions have their own
        loaders below.
        """

        settings = self.load_settings()
        normalized = normalize_providers_settings(settings.get("providers"))
        return {"connections": normalized["connections"]}

    def load_custom_providers_settings(self) -> dict[str, dict[str, Any]]:
        """Return normalized, secret-free Custom Provider definitions."""

        settings = self.load_settings()
        normalized = normalize_providers_settings(settings.get("providers"))
        return {
            provider_id: dict(provider) for provider_id, provider in normalized["custom"].items()
        }

    def save_custom_provider_settings(
        self,
        provider_id: str,
        provider: Mapping[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Create or replace one Custom Provider in a Settings transaction.

        With ``expected_revision`` (the ``custom_provider_revision()`` of the
        record the caller read), the write is refused with
        :class:`SettingsConflictError` when the stored record has changed or
        is gone.
        """

        normalized_id = normalize_custom_provider_id(provider_id)
        normalized_provider = normalize_custom_provider_settings(normalized_id, provider)

        def _mutate(settings: dict[str, Any]) -> dict[str, Any]:
            current = normalize_providers_settings(settings.get("providers"))
            custom = dict(current["custom"])
            if expected_revision is not None:
                stored = custom.get(normalized_id)
                if stored is None or custom_provider_revision(stored) != expected_revision:
                    raise SettingsConflictError((f"providers.custom.{normalized_id}",))
            custom[normalized_id] = normalized_provider
            connections = dict(current["connections"])
            connections.setdefault(f"{normalized_id}:default", True)
            settings["providers"] = normalize_providers_settings(
                {
                    "connections": connections,
                    "custom": custom,
                    "openrouter": current["openrouter"],
                }
            )
            return dict(settings["providers"]["custom"][normalized_id])

        return self.update_settings(_mutate)

    def update_custom_provider_settings(
        self,
        provider_id: str,
        update: Callable[[dict[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Replace one existing Custom Provider with ``update(current)`` atomically.

        ``update`` receives a copy of the normalized record and returns the new
        record; it runs inside the Settings transaction, so a concurrent write
        to another Settings field is not lost. Raises :class:`StorageError`
        when the Provider does not exist or the new record is invalid.
        """

        normalized_id = normalize_custom_provider_id(provider_id)

        def _mutate(settings: dict[str, Any]) -> dict[str, Any]:
            current = normalize_providers_settings(settings.get("providers"))
            custom = dict(current["custom"])
            existing = custom.get(normalized_id)
            if existing is None:
                raise StorageError(f"Custom Provider '{normalized_id}' does not exist")
            custom[normalized_id] = normalize_custom_provider_settings(
                normalized_id, update(copy.deepcopy(dict(existing)))
            )
            settings["providers"] = normalize_providers_settings(
                {
                    "connections": current["connections"],
                    "custom": custom,
                    "openrouter": current["openrouter"],
                }
            )
            return dict(settings["providers"]["custom"][normalized_id])

        return self.update_settings(_mutate)

    def delete_custom_provider_settings(self, provider_id: str) -> dict[str, Any] | None:
        """Delete one Custom Provider and its Connection override atomically."""

        normalized_id = normalize_custom_provider_id(provider_id)

        def _mutate(settings: dict[str, Any]) -> dict[str, Any] | None:
            current = normalize_providers_settings(settings.get("providers"))
            custom = dict(current["custom"])
            removed = custom.pop(normalized_id, None)
            if removed is None:
                return None
            connections = dict(current["connections"])
            connections.pop(f"{normalized_id}:default", None)
            settings["providers"] = normalize_providers_settings(
                {
                    "connections": connections,
                    "custom": custom,
                    "openrouter": current["openrouter"],
                }
            )
            return dict(removed)

        return self.update_settings(_mutate)

    def load_openrouter_routing_settings(self) -> dict[str, Any]:
        """Return the normalized OpenRouter default and per-Model routing policy."""

        settings = self.load_settings()
        normalized = normalize_providers_settings(settings.get("providers"))
        return dict(normalized["openrouter"]["routing"])

    def set_provider_connection_enabled(self, connection_key: str, enabled: bool) -> None:
        """Persist one connection's enabled override in a settings transaction.

        *connection_key* is the public ``<provider>:<connection>`` id. The value
        is stored explicitly (not as a delta against the type default) so a later
        change of a connection's shipped default never flips a user's choice.
        """

        if not isinstance(connection_key, str) or ":" not in connection_key:
            raise StorageError("Provider connection key must be a '<provider>:<connection>' string")
        if not isinstance(enabled, bool):
            raise StorageError("Provider connection enabled value must be a boolean")

        def _mutate(settings: dict[str, Any]) -> None:
            connections = dict(
                normalize_providers_settings(settings.get("providers"))["connections"]
            )
            connections[connection_key] = enabled
            current = normalize_providers_settings(settings.get("providers"))
            settings["providers"] = normalize_providers_settings(
                {
                    "connections": connections,
                    "custom": current["custom"],
                    "openrouter": current["openrouter"],
                }
            )

        self.update_settings(_mutate)

    def load_debug_settings(self) -> dict[str, Any]:
        """Return normalized persisted debug settings."""

        settings = self.load_settings()
        return normalize_debug_settings(settings.get("debug"))

    def load_archive_settings(self, *, strict: bool = False) -> dict[str, Any]:
        """Return normalized persisted archive settings.

        Like every section, the default read falls back to the defaults when
        ``settings.json`` is degraded. A ``strict`` read never does, because a
        default retention period could delete entries the user meant to keep
        longer; retention reads this way. A file that cannot be read (invalid
        JSON, another format version) or an invalid ``archive`` section then
        raises :class:`StorageError`, while a missing file or section reads as
        the defaults and an invalid other section does not matter.
        """

        if not strict:
            return normalize_archive_settings(self.load_settings().get("archive"))
        try:
            settings, ignored = load_runtime_settings_json(self.settings_path)
        except SettingsValidationError as exc:
            raise StorageError(str(exc)) from exc
        problems = [
            f"{diagnostic.path}: {diagnostic.message}"
            for diagnostic in ignored
            if diagnostic.path == "$.archive" or diagnostic.path.startswith("$.archive.")
        ]
        if problems:
            raise StorageError(f"{self.settings_path}: {'; '.join(problems)}")
        return normalize_archive_settings(settings.get("archive"))

    def load_reflection_settings(self) -> dict[str, Any]:
        """Return normalized persisted background-reflection settings."""

        settings = self.load_settings()
        return normalize_reflection_settings(settings.get("reflection"))

    def load_librarian_settings(self) -> dict[str, Any]:
        """Return normalized persisted Librarian settings."""

        settings = self.load_settings()
        return normalize_librarian_settings(settings.get("librarian"))

    def load_notification_settings(self) -> dict[str, bool]:
        """Return all persisted desktop-notification switches."""

        return normalize_notification_settings(self.load_settings().get("notifications"))

    def load_web_fetch_settings(self) -> dict[str, Any]:
        return normalize_web_fetch_settings(self.load_settings().get("web_fetch"))

    def load_web_search_settings(self) -> dict[str, Any]:
        """Return normalized persisted web search provider settings."""

        settings = self.load_settings()
        return normalize_web_search_settings(settings.get("web_search"))

    def load_model_task_settings(self) -> dict[str, dict[str, Any]]:
        """Return normalized persisted task-model bindings."""

        settings = self.load_settings()
        return normalize_model_task_settings(settings.get("model_tasks"))

    def load_extensions_settings(self) -> dict[str, Any]:
        """Return the normalized persisted ``extensions`` section.

        Shape ``{"disabled": [...], "config": {<name>: {...}}}``. Restart-applied
        — the runtime reads it at ``Runtime.start()``; this accessor exists so the
        ``extensions.list`` RPC can surface persisted config alongside records.
        """

        settings = self.load_settings()
        return normalize_extensions_settings(settings.get("extensions"))

    def update_model_task_settings(
        self,
        model_tasks: Mapping[str, Any],
        *,
        base: Mapping[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Persist sparse task-model binding updates and return the full section.

        ``base`` is the caller's view of the bindings it writes; like
        :meth:`update_settings_sections`, the write is refused with
        :class:`SettingsConflictError` when it would change a value that no
        longer matches that view.
        """

        if not isinstance(model_tasks, Mapping):
            raise StorageError("Model task settings must be a mapping")

        updated = self.update_settings_sections(
            {"model_tasks": model_tasks},
            base=None if base is None else {"model_tasks": base},
        )
        return cast("dict[str, dict[str, Any]]", updated["model_tasks"])

    def save_settings(self, settings: Mapping[str, Any]) -> None:
        """Atomically write ``settings.json``, keeping the unknown fields on disk.

        Refuses to overwrite a file that fails to load (see
        :func:`core.json_documents.write_json_document`).
        """

        if not isinstance(settings, Mapping):
            raise StorageError("Settings must be a mapping")

        with self._settings_lock:
            self.ensure_directories()
            self._settings_cache = None
            try:
                write_json_document(
                    self.settings_path, settings, SETTINGS_FORMAT, data_dir=self.data_dir
                )
            except JsonDocumentWriteError as exc:
                raise StorageError(str(exc)) from exc
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"Settings contain a value that cannot be serialized: {exc}"
                ) from exc
            except OSError as exc:
                raise StorageError(f"Cannot write {self.settings_path}: {exc}") from exc

    def copy_agent_prompt_fragments(self, agent_id: str, *, overwrite: bool = False) -> list[Path]:
        """Seed an Agent prompt scope from the currently effective default fragments."""

        return self._prompt_fragments.copy_agent_prompt_fragments(agent_id, overwrite=overwrite)

    def agent_prompts_dir(self, agent_id: str) -> Path:
        """Return the prompt-fragment directory for one Agent."""

        return self._prompt_fragments.agent_prompts_dir(agent_id)

    def read_agent_prompt_fragment(self, agent_id: str, fragment_name: str) -> str:
        """Read an Agent prompt fragment, returning an empty string when absent."""

        return self._prompt_fragments.read_agent_prompt_fragment(agent_id, fragment_name)

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Read a prompt fragment from the data directory, falling back to resources."""

        return self._prompt_fragments.read_prompt_fragment(fragment_name)

    def read_block_layout(self, scope: str | None) -> list[LayoutEntry]:
        """Read a scope's ordered block layout, or ``[]`` when none is written yet."""

        return self._prompt_blocks.read_layout(scope)

    def write_block_layout(
        self,
        scope: str | None,
        entries: Sequence[LayoutEntry],
        *,
        reset: bool = False,
    ) -> Path:
        """Atomically write a scope's ordered block layout.

        ``reset`` replaces a layout file that fails to load instead of refusing.
        """

        return self._prompt_blocks.write_layout(scope, entries, reset=reset)

    def prune_block_layout(
        self,
        scope: str | None,
        entries: Sequence[LayoutEntry],
        known_ids: frozenset[str] | set[str],
    ) -> Path:
        """Write a scope's block layout keeping only entries with a live definition."""

        return self._prompt_blocks.prune_layout(scope, entries, known_ids)

    def seed_agent_block_layout(
        self,
        agent_id: str,
        default_layout: Sequence[LayoutEntry],
        *,
        overwrite: bool = False,
    ) -> Path | None:
        """Seed an agent scope's block layout from the current default layout."""

        return self._prompt_blocks.seed_agent_layout(agent_id, default_layout, overwrite=overwrite)

    def read_block_override(self, scope: str | None, block_id: str) -> str | None:
        """Read a block's text override in a scope, or ``None`` when absent."""

        return self._prompt_blocks.read_block_override(scope, block_id)

    def write_block_override(self, scope: str | None, block_id: str, content: str) -> Path:
        """Atomically write a block's text override in a scope."""

        return self._prompt_blocks.write_block_override(scope, block_id, content)

    def remove_block_override(self, scope: str | None, block_id: str) -> bool:
        """Remove a block's text override in a scope, returning whether one existed."""

        return self._prompt_blocks.remove_block_override(scope, block_id)

    @staticmethod
    def _resolve_data_dir(data_dir: str | Path | None, config: ConfigProtocol | None) -> Path:
        if data_dir is not None:
            return Path(data_dir)

        if config is not None and hasattr(config, "data_dir"):
            return Path(config.data_dir)

        if config is not None:
            configured = config.get("DATA_DIR") or config.get("VBOT_DATA_DIR")
            if configured:
                return Path(configured)

        return DEFAULT_DATA_DIR

    @staticmethod
    def _resolve_resources_dir(resources_dir: str | Path | None) -> Path:
        if resources_dir is not None:
            return Path(resources_dir)
        return Path(__file__).resolve().parents[2] / "resources"
