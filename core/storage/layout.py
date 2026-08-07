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
    Path("artifacts/images"),
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
    def images(self) -> Path:
        return self.artifacts / "images"

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
    def environment_file(self) -> Path:
        return self.root / ENVIRONMENT_FILE_NAME

    @property
    def settings_file(self) -> Path:
        return self.root / SETTINGS_FILE_NAME

    @property
    def directories(self) -> tuple[Path, ...]:
        return tuple(self.root / relative_path for relative_path in DATA_DIRECTORY_RELATIVE_PATHS)


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
    if not layout.root.exists():
        layout.root.mkdir(parents=True)
        created_directories.append(layout.root)
    elif not layout.root.is_dir():
        raise NotADirectoryError(f"Data-directory path is not a directory: {layout.root}")

    for directory in layout.directories:
        if directory.exists():
            if not directory.is_dir():
                raise NotADirectoryError(
                    f"Canonical data-directory path is not a directory: {directory}"
                )
            continue
        directory.mkdir(parents=True)
        created_directories.append(directory)

    created_files: list[Path] = []
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
