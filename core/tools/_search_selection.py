"""Shared bounded selection and ordering for path and content searches."""

from __future__ import annotations

import fnmatch
import os
import sqlite3
import stat
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from core.tools._search_ignores import IgnoreRules
from core.tools._search_options import SearchOptions, size_bytes
from core.tools.search import SearchBudget, _expand_brace_alternations


class Glob:
    def __init__(self, pattern: str, *, sensitive: bool = False, basename: bool = False):
        if not pattern or "\x00" in pattern:
            raise ValueError("Path patterns and file globs must be nonempty and contain no NUL.")
        self.directory = pattern.endswith("/")
        if os.name == "nt":
            pattern = pattern.replace("\\", "/")
        if (
            pattern.startswith(("/", "~"))
            or (len(pattern) > 1 and pattern[1] == ":")
            or ".." in pattern.split("/")
        ):
            raise ValueError("Use root-relative glob patterns; put literal search roots in paths.")
        # Bound expansion before using the existing brace grammar.
        if len(pattern) > 4096 or pattern.count(",") > 32 or pattern.count("{") > 8:
            raise ValueError("Glob expansion is too large; split it into simpler patterns.")
        self.sensitive = sensitive
        self.basename = basename and "/" not in pattern.rstrip("/")
        self.alternatives = [
            tuple(p for p in alternative.split("/") if p not in {"", "."})
            for alternative in _expand_brace_alternations(pattern)
        ]

    def matches(self, relative: str, directory: bool) -> bool:
        if self.directory and not directory:
            return False
        parts = tuple(relative.split("/"))
        if self.basename:
            parts = parts[-1:]
        if not self.sensitive:
            parts = tuple(p.casefold() for p in parts)
        for alternative in self.alternatives:
            pattern = alternative if self.sensitive else tuple(p.casefold() for p in alternative)

            @lru_cache(maxsize=4096)
            def match(i: int, j: int, pattern=pattern) -> bool:
                if j == len(pattern):
                    return i == len(parts)
                if pattern[j] == "**":
                    return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
                return (
                    i < len(parts)
                    and fnmatch.fnmatchcase(parts[i], pattern[j])
                    and match(i + 1, j + 1)
                )

            if match(0, 0):
                return True
        return False


class FileSelection:
    """A call-scoped disk spool bounds union/dedup/sorting memory, without handles."""

    def __init__(
        self,
        database: Path,
        roots: list[Path],
        cwd: Path,
        options: SearchOptions,
        budget: SearchBudget,
        warnings: list[str],
    ):
        self.db = sqlite3.connect(database)
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("PRAGMA max_page_count=32768")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute(
            "CREATE TABLE entries (identity TEXT PRIMARY KEY, path TEXT, directory INTEGER, m"
            "odified REAL, accessed REAL, created REAL)"
        )
        self.roots, self.cwd, self.options, self.budget, self.warnings = (
            roots,
            cwd,
            options,
            budget,
            warnings,
        )
        self.observed = 0
        self.skipped = 0
        self.complete = True

    def close(self) -> None:
        self.db.close()

    def populate(self, patterns: list[str], kind: str, types: dict[str, list[str]]) -> None:
        options = self.options
        sensitive = options.get("glob_case") == "sensitive"
        path_patterns = [Glob(p, sensitive=sensitive) for p in patterns]
        filters: list[tuple[bool, Glob]] = []
        for option, value in options.entries:
            if option.key in {"glob", "iglob"}:
                filters.append(
                    (
                        not value.startswith("!"),
                        Glob(
                            value.removeprefix("!"),
                            sensitive=sensitive and option.key != "iglob",
                            basename=True,
                        ),
                    )
                )
        type_filters = [
            (option.key == "type", value)
            for option, value in options.entries
            if option.key in {"type", "type_not"}
        ]
        for _, name in type_filters:
            if name != "all" and name not in types:
                raise ValueError(
                    f"Unknown file type {name!r}; use options=['--type-list'] to list types."
                )
        compiled_types = {
            name: [Glob(p, sensitive=sensitive, basename=True) for p in values]
            for name, values in types.items()
        }
        for root in self.roots:
            ignores = IgnoreRules(root, self.cwd, options, self.warnings)
            for path, directory, metadata, relative in self._walk(root, ignores):
                if not self.budget.keep_going():
                    self.complete = False
                    return
                self.observed += 1
                if self.observed > 1_000_000:
                    self._warn("Traversal stopped after one million entries; narrow paths.")
                    return
                if kind != "all" and directory != (kind == "directories"):
                    continue
                if path_patterns and not any(p.matches(relative, directory) for p in path_patterns):
                    continue
                included = not any(positive for positive, _ in filters)
                for positive, pattern in filters:
                    hit = pattern.matches(relative, directory)
                    if not positive:
                        parts = relative.split("/")
                        hit = hit or any(
                            pattern.matches("/".join(parts[:i]), True) for i in range(1, len(parts))
                        )
                    if hit:
                        included = positive
                if not included:
                    continue
                if not directory:
                    size = options.get("size")
                    if size and metadata.st_size > size_bytes(size):
                        continue
                    included = not any(positive for positive, _ in type_filters)
                    for positive, name in type_filters:
                        chosen = (
                            compiled_types.values() if name == "all" else [compiled_types[name]]
                        )
                        if any(p.matches(relative, False) for group in chosen for p in group):
                            included = positive
                    if not included:
                        continue
                raw = str(path)
                if any(0xD800 <= ord(c) <= 0xDFFF for c in raw):
                    self._warn(f"Unrepresentable filename bytes (hex): {os.fsencode(path).hex()}")
                    continue
                created = getattr(
                    metadata, "st_birthtime", metadata.st_ctime if os.name == "nt" else None
                )
                if options.ordering and options.ordering[0] == "created" and created is None:
                    raise ValueError(
                        "Creation-time sorting is unavailable on this filesystem; use modi"
                        "fied, accessed, or path."
                    )
                try:
                    self.db.execute(
                        "INSERT OR IGNORE INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            os.path.normcase(raw),
                            raw,
                            directory,
                            metadata.st_mtime,
                            metadata.st_atime,
                            created,
                        ),
                    )
                except sqlite3.Error as error:
                    raise RuntimeError(
                        "Candidate storage is unavailable or full; narrow paths."
                    ) from error
                if self.observed >= 1_000_000:
                    self._warn(
                        "Traversal stopped after one million entries; narrow paths or --max-depth."
                    )
                    return
        self.db.commit()

    def _warn(self, message: str) -> None:
        self.complete = False
        self.skipped += 1
        if len(self.warnings) < 30:
            self.warnings.append(message)

    def _walk(
        self, root: Path, ignores: IgnoreRules
    ) -> Iterator[tuple[Path, bool, os.stat_result, str]]:
        options = self.options
        follow = options.enabled("follow")
        depth_limit = int(options.get("depth", "2147483647"))
        root_stat = root.stat()
        root_is_directory = root.is_dir()
        ancestors: set[tuple[int, int]] = set()

        def visit(path: Path, depth: int) -> Iterator[tuple[Path, bool, os.stat_result, str]]:
            if not self.budget.keep_going():
                self.complete = False
                return
            if any((part.casefold() if os.name == "nt" else part) == ".git" for part in path.parts):
                return
            try:
                link = path.is_symlink()
                if link and depth and not follow:
                    return
                metadata = path.stat()
                directory = stat.S_ISDIR(metadata.st_mode)
                if not directory and not stat.S_ISREG(metadata.st_mode):
                    return
                if depth and options.enabled("one_fs") and metadata.st_dev != root_stat.st_dev:
                    return
                if (
                    depth
                    and not options.enabled("hidden", True)
                    and (
                        path.name.startswith(".") or getattr(metadata, "st_file_attributes", 0) & 2
                    )
                ):
                    return
                if depth and ignores.is_ignored(path, directory):
                    return
                relative = path.relative_to(root).as_posix() if root_is_directory else path.name
                if depth or not directory or depth_limit == 0:
                    yield path, directory, metadata, relative
                if directory and depth < depth_limit:
                    identity = metadata.st_dev, metadata.st_ino
                    if identity in ancestors:
                        self._warn(f"Skipped symbolic-link loop: {path}")
                        return
                    ancestors.add(identity)
                    try:
                        with os.scandir(path) as entries:
                            for entry in entries:
                                yield from visit(Path(entry.path), depth + 1)
                    finally:
                        ancestors.remove(identity)
            except (OSError, RecursionError) as error:
                self._warn(f"Cannot inspect {path}: {error}")

        yield from visit(root, 0)

    def entries(self, *, action: str) -> Iterator[tuple[Path, bool]]:
        order, reverse = self.options.ordering or (
            ("path", False) if action == "content" else ("modified", True)
        )
        columns = {
            "path": "path",
            "modified": "modified",
            "accessed": "accessed",
            "created": "created",
            "none": "rowid",
        }
        direction = "DESC" if reverse else "ASC"
        query = (
            f"SELECT path, directory FROM entries ORDER BY {columns[order]} {direction}, path ASC"
        )
        for path, directory in self.db.execute(query):
            yield Path(path), bool(directory)
