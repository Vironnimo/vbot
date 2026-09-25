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
    Path("artifacts/performance"),
    Path("artifacts/temp"),
    Path("artifacts/temp/atomic"),
    Path("artifacts/temp/bash"),
    Path("artifacts/temp/subagents"),
    Path("artifacts/temp/terminals"),
    Path("artifacts/temp/web_fetch"),
    Path("statistics"),
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
# An empty Settings document in the current Settings format. Kept literal so this
# module stays importable without the Settings domain (setup runs it as a script);
# a test pins it to ``core.settings.SETTINGS_FORMAT``.
INITIAL_SETTINGS_DOCUMENT = '{\n  "format_version": 1\n}\n'
ENVIRONMENT_FILE_NAME = ".env"


@dataclass(frozen=True, slots=True)
class DataDirectoryLayout:
    """Immutable named paths rooted at one vBot data directory."""

    root: Path

    def __init__(self, root: str | Path) -> None:
        object.__setattr__(self, "root", Path(root).expanduser())

    @property
    def decisions_db(self) -> Path:
        return self.root / "decisions.db"

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
    def speech_engines(self) -> Path:
        return self.root / "speech-engines"

    @property
    def models(self) -> Path:
        return self.artifacts / "models"

    @property
    def debug(self) -> Path:
        return self.artifacts / "debug"

    @property
    def performance(self) -> Path:
        return self.artifacts / "performance"

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
    def data_store_marker_path(self) -> Path:
        """Current-format marker authorizing the canonical SQLite databases."""
        return self.root / "data-store.json"

    @property
    def directories(self) -> tuple[Path, ...]:
        return tuple(self.root / relative_path for relative_path in DATA_DIRECTORY_RELATIVE_PATHS)


def _write_bootstrap_marker_fallback(data_dir: Path) -> None:
    """Fallback bootstrap marker writer for ``python core/storage/layout.py``.

    The canonical writer lives in :mod:`core.database.marker` and is imported
    at call time. When the storage layout is executed as a standalone script
    (``python core/storage/layout.py``) the ``core`` package is not on
    ``sys.path`` and that import fails. This fallback writes the same JSON
    shape directly (an empty database list authorizes creating every
    canonical database) so manual invocations still produce a current-format
    data directory without requiring ``PYTHONPATH``.
    """

    import json as _json
    import os as _os
    import uuid as _uuid

    payload = {"format_version": 1, "databases": {}}
    target = Path(data_dir) / "data-store.json"
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


def _create_directory(path: Path) -> bool:
    """Create *path* and report whether this call created it.

    Returns ``False`` when the path already exists, including when a
    concurrent initializer created it between the existence check and
    ``mkdir``. Callers still verify that an existing path is a directory.
    """

    if path.exists():
        return False
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        return False
    return True


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
    require_new: bool = False,
) -> DataDirectoryInitializationResult:
    """Create the canonical layout, optionally requiring exclusive root creation."""

    layout = DataDirectoryLayout(data_dir)
    resources_root = (
        Path(resources_dir)
        if resources_dir is not None
        else Path(__file__).resolve().parents[2] / "resources"
    )
    environment_template = resources_root / ENVIRONMENT_TEMPLATE_RELATIVE_PATH

    created_directories: list[Path] = []
    created_files: list[Path] = []
    if _create_directory(layout.root):
        created_directories.append(layout.root)
        # Only the initializer whose mkdir created the root publishes the
        # bootstrap marker - immediately, so a crash before the remaining
        # directories are created does not leave an existing root without
        # authorization. An existing root, including one another process
        # created concurrently, never manufactures authorization here.
        # Import at call time: storage is at the bottom of the import graph
        # (models.database imports this module), so no database-kernel import
        # may run at module level here. The fallback handles ``python
        # core/storage/layout.py`` invocations where ``core`` is not on
        # ``sys.path``.
        try:
            from core.database.marker import write_bootstrap_marker
        except ImportError:
            _write_bootstrap_marker_fallback(layout.root)
        else:
            write_bootstrap_marker(layout.root)
        created_files.append(layout.data_store_marker_path)
    elif require_new:
        raise FileExistsError(f"Data-directory path already exists: {layout.root}")
    elif not layout.root.is_dir():
        raise NotADirectoryError(f"Data-directory path is not a directory: {layout.root}")

    for directory in layout.directories:
        if _create_directory(directory):
            created_directories.append(directory)
        elif not directory.is_dir():
            raise NotADirectoryError(
                f"Canonical data-directory path is not a directory: {directory}"
            )
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
            settings_file.write(INITIAL_SETTINGS_DOCUMENT)
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
