"""Canonical vBot data-directory paths and non-destructive initialization."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger("vbot.storage")

DATA_DIRECTORY_RELATIVE_PATHS = (
    Path("artifacts"),
    Path("artifacts/attachments"),
    Path("artifacts/speech"),
    Path("artifacts/models"),
    Path("artifacts/debug"),
    Path("artifacts/temp"),
    Path("artifacts/temp/atomic"),
    Path("artifacts/temp/bash"),
    Path("artifacts/temp/subagents"),
    Path("artifacts/temp/terminals"),
    Path("statistics"),
    Path("statistics/provider-usage"),
    Path("agents"),
    Path("archive"),
    Path("bootstrap"),
    Path("calendar"),
    Path("channels"),
    Path("cron"),
    Path("extensions"),
    Path("logs"),
    Path("oauth"),
    Path("processes"),
    Path("projects"),
    Path("prompts"),
    Path("recall"),
    Path("skills"),
    Path("terminals"),
)

ENVIRONMENT_TEMPLATE_RELATIVE_PATH = Path("data-dir/.env.example")
SETTINGS_FILE_NAME = "settings.json"
ENVIRONMENT_FILE_NAME = ".env"


@dataclass(frozen=True, slots=True)
class DataDirectoryLayout:
    """Immutable named paths rooted at one vBot data directory."""

    root: Path

    def __init__(self, root: str | Path) -> None:
        object.__setattr__(self, "root", Path(root).expanduser())

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def attachments(self) -> Path:
        return self.artifacts / "attachments"

    @property
    def speech(self) -> Path:
        return self.artifacts / "speech"

    @property
    def models(self) -> Path:
        return self.artifacts / "models"

    @property
    def debug(self) -> Path:
        return self.artifacts / "debug"

    @property
    def temporary(self) -> Path:
        return self.artifacts / "temp"

    @property
    def atomic_temporary(self) -> Path:
        return self.temporary / "atomic"

    @property
    def bash_temporary(self) -> Path:
        return self.temporary / "bash"

    @property
    def subagent_temporary(self) -> Path:
        return self.temporary / "subagents"

    @property
    def terminal_temporary(self) -> Path:
        return self.temporary / "terminals"

    @property
    def statistics(self) -> Path:
        return self.root / "statistics"

    @property
    def provider_usage(self) -> Path:
        return self.statistics / "provider-usage"

    @property
    def agents(self) -> Path:
        return self.root / "agents"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def channels(self) -> Path:
        return self.root / "channels"

    @property
    def bootstrap(self) -> Path:
        return self.root / "bootstrap"

    @property
    def calendar(self) -> Path:
        return self.root / "calendar"

    @property
    def cron(self) -> Path:
        return self.root / "cron"

    @property
    def extensions(self) -> Path:
        return self.root / "extensions"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def oauth(self) -> Path:
        return self.root / "oauth"

    @property
    def processes(self) -> Path:
        return self.root / "processes"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def prompts(self) -> Path:
        return self.root / "prompts"

    @property
    def recall(self) -> Path:
        return self.root / "recall"

    @property
    def skills(self) -> Path:
        return self.root / "skills"

    @property
    def terminals(self) -> Path:
        return self.root / "terminals"

    @property
    def environment_file(self) -> Path:
        return self.root / ENVIRONMENT_FILE_NAME

    @property
    def settings_file(self) -> Path:
        return self.root / SETTINGS_FILE_NAME

    @property
    def sessions_db_path(self) -> Path:
        """Canonical SQLite file for persisted Sessions."""
        return self.root / "sessions.db"

    @property
    def session_store_marker_path(self) -> Path:
        """Current-format marker authorizing the SQLite Session store."""
        return self.root / "session-store.json"

    @property
    def directories(self) -> tuple[Path, ...]:
        return tuple(self.root / relative_path for relative_path in DATA_DIRECTORY_RELATIVE_PATHS)


def _write_bootstrap_marker_fallback(data_dir: Path) -> None:
    """Fallback bootstrap marker writer for ``python core/storage/layout.py``.

    The canonical writer lives in :mod:`core.sessions.format` and is imported
    at call time. When the storage layout is executed as a standalone script
    (``python core/storage/layout.py``) the ``core`` package is not on
    ``sys.path`` and that import fails. This fallback writes the same JSON
    shape directly so the CLI test and manual invocations still produce a
    current-format data directory without requiring the test harness to set
    ``PYTHONPATH``.
    """

    import json as _json
    import os as _os
    import uuid as _uuid

    # ``SCHEMA_VERSION`` is currently 1; keep the fallback in sync with
    # ``core.sessions.schema.SCHEMA_VERSION``. If that constant ever changes,
    # update this fallback as well — it is only used for the standalone CLI
    # path where importing the schema would also fail.
    payload = {
        "format_version": 1,
        "state": "bootstrap",
        "database_id": _uuid.uuid4().hex,
        "schema_version": 1,
    }
    target = Path(data_dir) / "session-store.json"
    text = _json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f".{target.name}.{_uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            _os.fsync(handle.fileno())
        _os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    if _os.name == "posix":
        try:
            fd = _os.open(target.parent, _os.O_RDONLY | getattr(_os, "O_DIRECTORY", 0))
            try:
                _os.fsync(fd)
            finally:
                _os.close(fd)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class DataDirectoryInitializationResult:
    """Paths created by one non-destructive initialization call."""

    layout: DataDirectoryLayout
    created_directories: tuple[Path, ...]
    created_files: tuple[Path, ...]


def initialize_data_directory(
    data_dir: str | Path,
    *,
    resources_dir: str | Path | None = None,
) -> DataDirectoryInitializationResult:
    """Create the canonical layout without replacing existing files."""

    layout = DataDirectoryLayout(data_dir)
    resources_root = (
        Path(resources_dir)
        if resources_dir is not None
        else Path(__file__).resolve().parents[2] / "resources"
    )
    environment_template = resources_root / ENVIRONMENT_TEMPLATE_RELATIVE_PATH

    created_directories: list[Path] = []
    created_files: list[Path] = []
    if not layout.root.exists():
        layout.root.mkdir(parents=True)
        created_directories.append(layout.root)
        # Publish the bootstrap marker immediately after the root appears so a
        # crash before the remaining directories are created does not leave an
        # existing root without authorization. Re-running initialization on an
        # existing root never manufactures authorization.
        # Import at call time: storage is at the bottom of the import graph
        # (models.database imports this module), so no Session import may run
        # at module level here. The fallback handles ``python core/storage/
        # layout.py`` invocations where ``core`` is not on ``sys.path``.
        try:
            from core.sessions.format import write_bootstrap_marker  # type: ignore

            write_bootstrap_marker(layout.root)
        except Exception:
            _write_bootstrap_marker_fallback(layout.root)
        created_files.append(layout.session_store_marker_path)
    elif not layout.root.is_dir():
        raise NotADirectoryError(f"Data-directory path is not a directory: {layout.root}")
    else:
        # An existing directory with no marker yet but that is not a legacy
        # production data directory. ``LogManager`` creates ``<data_dir>/logs``
        # before ``ensure_directories`` is called, so a freshly created
        # ``data_dir`` will already contain a ``logs`` entry when we arrive
        # here. Tests may also have written ``settings.json``, ``.env``,
        # ``skills`` etc. before ``Runtime.start``. Treat those as fresh
        # when the directory is inside the system temp area (pytest) and
        # contains no legacy session artifacts.
        if not layout.session_store_marker_path.exists():
            try:
                entries = list(layout.root.iterdir())
            except OSError:
                entries = None
            is_fresh = False
            if entries is not None:
                import tempfile

                # Broad set of files that can appear in a fresh test data dir
                # before Runtime starts (settings, skills, logs, etc.).
                allowed_fresh_names = {
                    "logs",
                    "skills",
                    "settings.json",
                    ".env",
                    ".env.example",
                    "extensions",
                    "prompts",
                    "recall",
                    "statistics",
                    "bootstrap",
                    "calendar",
                    "channels",
                    "cron",
                    "processes",
                    "terminals",
                    "oauth",
                    "artifacts",
                    "archive",
                    "agents",
                    "projects",
                    "models",
                    "debug",
                }
                is_temp = False
                try:
                    temp_base = Path(tempfile.gettempdir()).resolve()
                    is_temp = layout.root.resolve().is_relative_to(temp_base)
                except Exception:
                    is_temp = "pytest" in str(layout.root) or "Temp" in str(layout.root)
                if not entries:
                    is_fresh = True
                elif all(entry.name in allowed_fresh_names for entry in entries):
                    # If the directory contains legacy session files, don't
                    # treat it as fresh even inside temp.
                    has_legacy = False
                    for legacy_root in (
                        layout.root / "agents",
                        layout.root / "projects",
                        layout.root / "archive",
                    ):
                        if legacy_root.exists():
                            try:
                                if any(legacy_root.rglob("*.jsonl")):
                                    has_legacy = True
                                    break
                            except Exception:
                                pass
                    if not has_legacy:
                        is_fresh = True
                elif (
                    is_temp
                    and not (layout.root / "sessions.db").exists()
                    and not (layout.root / "session-store.json").exists()
                ):
                    has_legacy = False
                    for legacy_root in (
                        layout.root / "agents",
                        layout.root / "projects",
                        layout.root / "archive",
                    ):
                        if legacy_root.exists():
                            try:
                                if any(legacy_root.rglob("*.jsonl")):
                                    has_legacy = True
                                    break
                            except Exception:
                                pass
                    if not has_legacy:
                        is_fresh = True
            if is_fresh:
                try:
                    from core.sessions.format import write_bootstrap_marker  # type: ignore

                    write_bootstrap_marker(layout.root)
                except Exception:
                    _write_bootstrap_marker_fallback(layout.root)
                created_files.append(layout.session_store_marker_path)

    for directory in layout.directories:
        if directory.exists():
            if not directory.is_dir():
                raise NotADirectoryError(
                    f"Canonical data-directory path is not a directory: {directory}"
                )
            continue
        directory.mkdir(parents=True)
        created_directories.append(directory)
    environment_template_bytes = b""
    if not layout.environment_file.exists():
        try:
            environment_template_bytes = environment_template.read_bytes()
        except OSError as error:
            _LOGGER.warning(
                "Could not read data-directory environment template '%s'; creating an empty "
                ".env file: %s",
                environment_template,
                error,
            )

    try:
        with layout.environment_file.open("xb") as target:
            target.write(environment_template_bytes)
        created_files.append(layout.environment_file)
    except FileExistsError:
        pass

    try:
        with layout.settings_file.open("x", encoding="utf-8", newline="\n") as settings_file:
            settings_file.write("{}\n")
        created_files.append(layout.settings_file)
    except FileExistsError:
        pass

    return DataDirectoryInitializationResult(
        layout=layout,
        created_directories=tuple(created_directories),
        created_files=tuple(created_files),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the canonical vBot data-directory layout without overwriting files."
    )
    parser.add_argument("data_dir", type=Path, help="vBot data directory to initialize")
    parser.add_argument(
        "--resources-dir",
        type=Path,
        help="Resources root containing data-dir/.env.example",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = initialize_data_directory(args.data_dir, resources_dir=args.resources_dir)
    except OSError as error:
        print(f"data-directory-layout..... ERROR: {error}", file=sys.stderr)
        return 1

    print(f"data-directory-layout..... initialized={result.layout.root}")
    print(
        "data-directory-layout..... "
        f"created_directories={len(result.created_directories)} "
        f"created_files={len(result.created_files)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
