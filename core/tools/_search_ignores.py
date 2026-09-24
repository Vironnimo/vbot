"""Ignore-source precedence for the shared file-search traversal."""

from __future__ import annotations

import configparser
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import TypeVar

from pathspec import PathSpec

from core.tools._search_options import SearchOptions

_MAX_IGNORE_FILE_CHARS = 2 * 1024 * 1024
_MAX_IGNORE_LINE_CHARS = 4096
# Compiled ignore sources and Git configuration lookups are shared across
# searches. An entry is reused while its file's stat stamp is unchanged and the
# file is older than the racy window: a write inside one filesystem timestamp
# tick can keep the stamp, so recently modified files are always read again.
_SOURCE_CACHE_LIMIT = 4096
_RACY_WINDOW_NS = 3_000_000_000
_Stamp = tuple[int, int, int]
_K = TypeVar("_K")
_V = TypeVar("_V")
_SOURCES: OrderedDict[tuple[Path, bool], tuple[_Stamp, tuple[int, list]]] = OrderedDict()
_CONFIG_EXCLUDES: OrderedDict[Path, tuple[_Stamp, tuple[str | None]]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _stamp(path: Path) -> _Stamp:
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _cached(cache: OrderedDict[_K, tuple[_Stamp, _V]], key: _K, stamp: _Stamp) -> _V | None:
    with _CACHE_LOCK:
        entry = cache.get(key)
        if entry is None or entry[0] != stamp:
            return None
        cache.move_to_end(key)
        return entry[1]


def _remember(cache: OrderedDict[_K, tuple[_Stamp, _V]], key: _K, stamp: _Stamp, value: _V) -> None:
    if time.time_ns() - stamp[0] < _RACY_WINDOW_NS:
        return
    with _CACHE_LOCK:
        cache[key] = (stamp, value)
        cache.move_to_end(key)
        while len(cache) > _SOURCE_CACHE_LIMIT:
            cache.popitem(last=False)


def _compiled_source(path: Path, *, casefold: bool) -> tuple[int, list]:
    """Return one ignore file's line count and compiled patterns; missing is empty."""
    try:
        stamp = _stamp(path)
    except (FileNotFoundError, NotADirectoryError):
        return 0, []
    except OSError as error:
        raise RuntimeError(f"Cannot read ignore file {path}: {error}") from error
    cached = _cached(_SOURCES, (path, casefold), stamp)
    if cached is not None:
        return cached
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            text = stream.read(_MAX_IGNORE_FILE_CHARS + 1)
    except (FileNotFoundError, NotADirectoryError):
        return 0, []
    except OSError as error:
        raise RuntimeError(f"Cannot read ignore file {path}: {error}") from error
    if len(text) > _MAX_IGNORE_FILE_CHARS:
        raise ValueError(f"Ignore file exceeds 2 MiB: {path}")
    if casefold:
        text = text.casefold()
    lines = text.splitlines()
    if any(len(line) > _MAX_IGNORE_LINE_CHARS for line in lines):
        raise RuntimeError("Ignore rules exceed the processing bound; narrow paths.")
    compiled = (len(lines), list(PathSpec.from_lines("gitignore", lines).patterns))
    _remember(_SOURCES, (path, casefold), stamp, compiled)
    return compiled


def _configured_excludes_file(config: Path) -> str | None:
    """Return ``core.excludesfile`` from one Git configuration file, if set."""
    try:
        stamp = _stamp(config)
    except (FileNotFoundError, NotADirectoryError):
        return None
    cached = _cached(_CONFIG_EXCLUDES, config, stamp)
    if cached is not None:
        return cached[0]
    parser = configparser.RawConfigParser(strict=False)
    parser.read(config, encoding="utf-8")
    value = parser.get("core", "excludesfile", fallback=None)
    _remember(_CONFIG_EXCLUDES, config, stamp, (value,))
    return value


class IgnoreRules:
    def __init__(self, root: Path, cwd: Path, options: SearchOptions, warnings: list[str]):
        self.root = root if root.is_dir() else root.parent
        self.options = options
        self.warnings = warnings
        self.cache: dict[Path, list] = {}
        self.groups: dict[Path, list[tuple[Path, list]]] = {}
        self.pattern_count = 0
        self.repository_excludes: dict[Path, Path | None] = {}
        self.repositories: dict[Path, bool] = {}
        self.repository = next(
            (p for p in (self.root, *self.root.parents) if self._is_repository(p)), None
        )
        self.excludes: list[Path] = []
        if options.enabled("ignore_global", True):
            home = Path.home()
            config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
            exclude = config_home / "git" / "ignore"
            for config in (config_home / "git" / "config", home / ".gitconfig"):
                try:
                    value = _configured_excludes_file(config)
                    if value:
                        exclude = Path(value.strip('"')).expanduser()
                        if not exclude.is_absolute():
                            exclude = config.parent / exclude
                except (OSError, configparser.Error) as error:
                    warnings.append(f"Cannot read Git configuration {config}: {error}")
            self.excludes.append(exclude)
        self.extra = [cwd / Path(p).expanduser() for p in options.values("ignore_file")]
        for file in self.extra:
            if not file.is_file():
                raise ValueError(f"Ignore file not found: {file}")
        self.disabled = not options.enabled("ignore", True)
        if not self.disabled:
            # An explicitly selected ignored target opts its complete subtree in.
            self.disabled = any(
                self.is_ignored(p, p.is_dir()) for p in (root, *root.parents) if p != p.parent
            )

    def _is_repository(self, path: Path) -> bool:
        known = self.repositories.get(path)
        if known is None:
            known = self.repositories[path] = (path / ".git").exists()
        return known

    def _patterns(self, path: Path) -> list:
        if path not in self.cache:
            if len(self.cache) >= 4096:
                self.cache.pop(next(iter(self.cache)))
            line_count, patterns = _compiled_source(
                path, casefold=self.options.enabled("ignore_case")
            )
            self.pattern_count += line_count
            if self.pattern_count > 50_000:
                raise RuntimeError("Ignore rules exceed the processing bound; narrow paths.")
            self.cache[path] = patterns
        return self.cache[path]

    def is_ignored(self, path: Path, directory: bool) -> bool:
        if self.disabled:
            return False
        groups = self.groups.get(path.parent)
        if groups is None:
            groups = self._groups(path.parent)
            if len(self.groups) >= 4096:
                self.groups.pop(next(iter(self.groups)))
            self.groups[path.parent] = groups
        ignored = False
        for base, patterns in groups:
            try:
                candidate = path.relative_to(base).as_posix() + ("/" if directory else "")
            except ValueError:
                continue
            if self.options.enabled("ignore_case"):
                candidate = candidate.casefold()
            for pattern in patterns:
                if pattern.include is not None and pattern.match_file(candidate):
                    ignored = bool(pattern.include)
        return ignored

    def _repository_exclude(self, repository: Path) -> Path | None:
        if repository not in self.repository_excludes:
            git = repository / ".git"
            try:
                if git.is_file():
                    value = git.read_text(encoding="utf-8").strip()
                    if not value.startswith("gitdir:"):
                        self.repository_excludes[repository] = None
                        return None
                    git = repository / value.removeprefix("gitdir:").strip()
                common = git / "commondir"
                if common.is_file():
                    git = git / common.read_text(encoding="utf-8").strip()
                self.repository_excludes[repository] = git / "info" / "exclude"
            except OSError as error:
                raise RuntimeError(f"Cannot read repository excludes: {error}") from error
        return self.repository_excludes[repository]

    def _groups(self, parent: Path) -> list[tuple[Path, list]]:
        """Compile applicable sources once per directory, omitting empty sources."""
        options = self.options
        parents = list(reversed((parent, *parent.parents)))
        repository = next((p for p in reversed(parents) if self._is_repository(p)), None)
        # Explicit-root checks walk ancestors too; a containing checkout's Git
        # ignores must not enter a selected nested repository.
        if self.repository is not None and parent in self.repository.parents:
            repository = self.repository
        if not options.enabled("ignore_parent", True):
            parents = [p for p in parents if p == self.root or self.root in p.parents]
        groups: list[tuple[Path, Path]] = []
        if options.enabled("ignore_vcs", True) and (
            repository or not options.enabled("require_git")
        ):
            base = repository or self.root
            groups.extend((p, base) for p in self.excludes)
            if repository and options.enabled("ignore_exclude", True):
                exclude = self._repository_exclude(repository)
                if exclude is not None:
                    groups.append((exclude, repository))
            groups.extend(
                (p / ".gitignore", p)
                for p in parents
                if repository is None or p == repository or repository in p.parents
            )
        if options.enabled("ignore_dot", True):
            for name in (".ignore", ".rgignore"):
                groups.extend((p / name, p) for p in parents)
        if options.enabled("ignore_files", True):
            groups.extend((p, self.root) for p in self.extra)
        return [(base, patterns) for source, base in groups if (patterns := self._patterns(source))]
