"""Ignore-source precedence for the shared file-search traversal."""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from pathspec import PathSpec

from core.tools._search_options import SearchOptions


class IgnoreRules:
    def __init__(self, root: Path, cwd: Path, options: SearchOptions, warnings: list[str]):
        self.root = root if root.is_dir() else root.parent
        self.options = options
        self.warnings = warnings
        self.cache: dict[Path, list] = {}
        self.groups: dict[Path, list[tuple[Path, list]]] = {}
        self.pattern_count = 0
        self.repository_excludes: dict[Path, Path | None] = {}
        self.repository = next(
            (p for p in (self.root, *self.root.parents) if (p / ".git").exists()), None
        )
        self.excludes: list[Path] = []
        if options.enabled("ignore_global", True):
            home = Path.home()
            config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
            exclude = config_home / "git" / "ignore"
            for config in (config_home / "git" / "config", home / ".gitconfig"):
                parser = configparser.RawConfigParser(strict=False)
                try:
                    parser.read(config, encoding="utf-8")
                    value = parser.get("core", "excludesfile", fallback=None)
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

    def _patterns(self, path: Path) -> list:
        if path not in self.cache:
            if len(self.cache) >= 4096:
                self.cache.pop(next(iter(self.cache)))
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    text = stream.read(2 * 1024 * 1024 + 1)
                if len(text) > 2 * 1024 * 1024:
                    raise ValueError(f"Ignore file exceeds 2 MiB: {path}")
                if self.options.enabled("ignore_case"):
                    text = text.casefold()
                lines = text.splitlines()
                self.pattern_count += len(lines)
                if self.pattern_count > 50_000 or any(len(line) > 4096 for line in lines):
                    raise RuntimeError("Ignore rules exceed the processing bound; narrow paths.")
                self.cache[path] = list(PathSpec.from_lines("gitignore", lines).patterns)
            except (FileNotFoundError, NotADirectoryError):
                self.cache[path] = []
            except OSError as error:
                raise RuntimeError(f"Cannot read ignore file {path}: {error}") from error
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
        repository = next((p for p in reversed(parents) if (p / ".git").exists()), None)
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
