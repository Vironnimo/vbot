#!/usr/bin/env python
"""Safely convert one explicit vBot data directory to the canonical layout."""

from __future__ import annotations

import argparse
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.storage.layout import DataDirectoryLayout, initialize_data_directory  # noqa: E402


class DataDirectoryConversionError(Exception):
    """Raised when a legacy data directory cannot be converted safely."""


@dataclass(frozen=True, slots=True)
class DirectoryMapping:
    """One supported legacy root mapped to a canonical layout property."""

    source_relative: Path
    destination_attribute: str


@dataclass(frozen=True, slots=True)
class FileMove:
    """One regular source file and its collision-checked destination."""

    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class DataDirectoryConversionPlan:
    """Complete read-only preflight result for one data directory."""

    data_root: Path
    mappings: tuple[DirectoryMapping, ...]
    moves: tuple[FileMove, ...]
    existing_source_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class DataDirectoryConversionResult:
    """Summary of one applied conversion."""

    moved_files: int
    removed_legacy_roots: int


LEGACY_DIRECTORY_MAPPINGS = (
    DirectoryMapping(Path("attachments"), "attachments"),
    DirectoryMapping(Path("speech"), "speech"),
    DirectoryMapping(Path("models"), "models"),
    DirectoryMapping(Path("debug"), "debug"),
    DirectoryMapping(Path(".tmp"), "atomic_temporary"),
    DirectoryMapping(Path("temp/bash"), "bash_temporary"),
    DirectoryMapping(Path("temp/subagents"), "subagent_temporary"),
    DirectoryMapping(Path("provider-usage"), "provider_usage"),
)
# Skill drafts were temporary authoring state, not durable product data. The
# retired legacy category remains allowlisted so conversion can proceed without
# deleting it, but it is deliberately not moved into the canonical layout.
_LEGACY_TEMP_CATEGORIES = frozenset({"bash", "subagents", "skill-drafts"})


def plan_data_directory_conversion(data_dir: str | Path) -> DataDirectoryConversionPlan:
    """Preflight every supported source and destination without changing disk."""

    data_root = _resolve_safe_data_root(data_dir)
    layout = DataDirectoryLayout(data_root)
    _validate_legacy_temp_categories(data_root)

    moves: list[FileMove] = []
    existing_source_roots: list[Path] = []
    for mapping in LEGACY_DIRECTORY_MAPPINGS:
        source_root = data_root / mapping.source_relative
        destination_root = getattr(layout, mapping.destination_attribute)
        if not source_root.exists() and not source_root.is_symlink():
            continue
        _validate_directory(source_root, label="Legacy source")
        _validate_destination_root(destination_root)
        existing_source_roots.append(source_root)
        moves.extend(_inventory_moves(source_root, destination_root))

    return DataDirectoryConversionPlan(
        data_root=data_root,
        mappings=LEGACY_DIRECTORY_MAPPINGS,
        moves=tuple(moves),
        existing_source_roots=tuple(existing_source_roots),
    )


def apply_data_directory_conversion(
    data_dir: str | Path,
) -> DataDirectoryConversionResult:
    """Apply one fully preflighted, resumable conversion without overwrites."""

    plan = plan_data_directory_conversion(data_dir)
    for move in plan.moves:
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        move.source.replace(move.destination)

    removed_roots = _prune_legacy_directories(plan)
    initialize_data_directory(plan.data_root, resources_dir=PROJECT_ROOT / "resources")
    return DataDirectoryConversionResult(
        moved_files=len(plan.moves),
        removed_legacy_roots=removed_roots,
    )


def _resolve_safe_data_root(data_dir: str | Path) -> Path:
    requested = Path(data_dir).expanduser()
    if requested.is_symlink():
        raise DataDirectoryConversionError(
            f"Data directory must not be a symbolic link: {requested}"
        )
    try:
        data_root = requested.resolve(strict=True)
    except FileNotFoundError as error:
        raise DataDirectoryConversionError(f"Data directory does not exist: {requested}") from error
    if not data_root.is_dir():
        raise DataDirectoryConversionError(f"Data directory is not a directory: {data_root}")

    filesystem_root = Path(data_root.anchor).resolve()
    if data_root == filesystem_root:
        raise DataDirectoryConversionError(f"Refusing to convert a filesystem root: {data_root}")
    if data_root == Path.home().resolve():
        raise DataDirectoryConversionError(
            f"Refusing to convert the user home directory: {data_root}"
        )
    return data_root


def _validate_legacy_temp_categories(data_root: Path) -> None:
    legacy_temp = data_root / "temp"
    if not legacy_temp.exists() and not legacy_temp.is_symlink():
        return
    _validate_directory(legacy_temp, label="Legacy temp root")
    for child in legacy_temp.iterdir():
        if child.name not in _LEGACY_TEMP_CATEGORIES:
            raise DataDirectoryConversionError(f"Unsupported legacy temp category: {child}")


def _validate_directory(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise DataDirectoryConversionError(f"{label} must not be a symbolic link: {path}")
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise DataDirectoryConversionError(
            f"Cannot inspect {label.lower()} {path}: {error}"
        ) from error
    if not stat.S_ISDIR(mode):
        raise DataDirectoryConversionError(f"{label} is not a directory: {path}")


def _validate_destination_root(destination_root: Path) -> None:
    current = destination_root
    existing_chain: list[Path] = []
    while not current.exists() and not current.is_symlink():
        existing_chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    if current.is_symlink():
        raise DataDirectoryConversionError(
            f"Canonical destination must not traverse a symbolic link: {current}"
        )
    if current.exists() and not current.is_dir():
        raise DataDirectoryConversionError(
            f"Canonical destination parent is not a directory: {current}"
        )
    for path in reversed(existing_chain):
        if path.exists() and not path.is_dir():
            raise DataDirectoryConversionError(
                f"Canonical destination path is not a directory: {path}"
            )


def _inventory_moves(source_root: Path, destination_root: Path) -> list[FileMove]:
    moves: list[FileMove] = []

    def visit(source_directory: Path) -> None:
        relative_directory = source_directory.relative_to(source_root)
        destination_directory = destination_root / relative_directory
        if destination_directory.is_symlink():
            raise DataDirectoryConversionError(
                f"Canonical destination must not be a symbolic link: {destination_directory}"
            )
        if destination_directory.exists() and not destination_directory.is_dir():
            raise DataDirectoryConversionError(
                f"Destination collision with a non-directory: {destination_directory}"
            )

        try:
            children = sorted(source_directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise DataDirectoryConversionError(
                f"Cannot inventory legacy source {source_directory}: {error}"
            ) from error
        for child in children:
            if child.is_symlink():
                raise DataDirectoryConversionError(
                    f"Legacy source contains a symbolic link: {child}"
                )
            try:
                mode = child.stat(follow_symlinks=False).st_mode
            except OSError as error:
                raise DataDirectoryConversionError(
                    f"Cannot inspect legacy source entry {child}: {error}"
                ) from error
            if stat.S_ISDIR(mode):
                visit(child)
                continue
            if not stat.S_ISREG(mode):
                raise DataDirectoryConversionError(
                    f"Legacy source contains a special file: {child}"
                )

            destination = destination_root / child.relative_to(source_root)
            if destination.exists() or destination.is_symlink():
                raise DataDirectoryConversionError(
                    f"Destination collision: {child} -> {destination}"
                )
            moves.append(FileMove(source=child, destination=destination))

    visit(source_root)
    return moves


def _prune_legacy_directories(plan: DataDirectoryConversionPlan) -> int:
    removed_roots = 0
    for source_root in sorted(
        plan.existing_source_roots,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if _prune_empty_tree(source_root):
            removed_roots += 1

    legacy_temp = plan.data_root / "temp"
    _prune_empty_tree(legacy_temp)
    return removed_roots


def _prune_empty_tree(directory: Path) -> bool:
    """Remove only empty directories below and including ``directory``."""

    if not directory.is_dir() or directory.is_symlink():
        return False
    try:
        children = list(directory.iterdir())
    except OSError:
        return False
    for child in children:
        if child.is_dir() and not child.is_symlink():
            _prune_empty_tree(child)
    try:
        directory.rmdir()
    except OSError:
        return False
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or apply the canonical vBot data-directory artifact layout. "
            "Stop the target vBot instance before using --apply."
        )
    )
    parser.add_argument("data_dir", type=Path, help="Existing vBot data directory")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move files after a complete collision-safe preflight",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = plan_data_directory_conversion(args.data_dir)
        _print_plan(plan)
        if not args.apply:
            print("data-dir-artifacts-layout..... dry-run only; use --apply to move files")
            return 0
        result = apply_data_directory_conversion(plan.data_root)
    except (DataDirectoryConversionError, OSError) as error:
        print(f"data-dir-artifacts-layout..... ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "data-dir-artifacts-layout..... "
        f"applied moved_files={result.moved_files} "
        f"removed_legacy_roots={result.removed_legacy_roots}"
    )
    return 0


def _print_plan(plan: DataDirectoryConversionPlan) -> None:
    layout = DataDirectoryLayout(plan.data_root)
    move_counts = dict.fromkeys(plan.mappings, 0)
    for mapping in plan.mappings:
        source_root = plan.data_root / mapping.source_relative
        move_counts[mapping] = sum(
            1 for move in plan.moves if move.source.is_relative_to(source_root)
        )
        destination_root = getattr(layout, mapping.destination_attribute)
        print(
            "data-dir-artifacts-layout..... "
            f"{mapping.source_relative.as_posix()} -> "
            f"{destination_root.relative_to(plan.data_root).as_posix()} "
            f"files={move_counts[mapping]}"
        )
    print(
        f"data-dir-artifacts-layout..... preflight_ok files={len(plan.moves)} root={plan.data_root}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
