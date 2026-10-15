"""Resolve file-search roots and explain missing or glob-shaped paths."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.tools._path_suggestions import corrected_paths
from core.tools._search_query import (
    SearchArgumentError,
    SearchQuery,
)
from core.tools._search_results import (
    path_label,
)
from core.tools._swarm_references import wiki_page_call
from core.tools._tool_context import _path_argument
from core.tools.search import _expand_brace_alternations

_GLOB_CHARACTERS = re.compile(r"[*?\[]")


@dataclass
class _Roots:
    """The roots one call searches, and the requested paths that do not exist."""

    paths: list[Path] = field(default_factory=list)
    missing: list[tuple[str, Path]] = field(default_factory=list)


def _resolve_roots(query: SearchQuery, cwd: Path) -> _Roots:
    """Resolve the requested roots; a missing path is reported, never replaced.

    A missing path written with glob characters, braces or commas runs as what
    it evidently means when that names existing paths: ``src/**/*.py`` searches
    ``src`` with that glob, ``{src,tests}`` and ``src,tests`` search both. A
    missing path written as ``!vendor/**`` is the excluding glob it reads as.
    """
    roots = _Roots()
    excluded = False
    for raw in query.roots or [str(cwd)]:
        text = str(_path_argument(raw, windows=os.name == "nt"))
        if not text.strip() or text == "-":
            raise SearchArgumentError(
                f'path must name a file or directory; received "{raw}". Omit path to search '
                "the working directory."
            )
        resolved = _absolute(text, cwd)
        if os.path.lexists(resolved):
            _add(roots.paths, resolved)
            continue
        alternatives = _existing_alternatives(text, cwd)
        if alternatives:
            for path in alternatives:
                _add(roots.paths, path)
            shown = ", ".join(path_label(path, cwd) for path in alternatives)
            query.notes.append(f'path "{raw}" names several paths, so {shown} were searched.')
            continue
        if text.startswith("!") and len(text) > 1:
            query.globs.append(text)
            query.notes.append(
                f'path "{raw}" starts with !, so it was applied as an excluding glob.'
            )
            excluded = True
            continue
        base = _glob_root(text, cwd, query)
        if base is not None:
            _add(roots.paths, base)
            continue
        roots.missing.append((raw, resolved))
    if excluded and not roots.paths and not roots.missing:
        roots.paths.append(cwd)
    return roots


def _missing_warnings(
    query: SearchQuery,
    roots: _Roots,
    cwd: Path,
    offers: Callable[[str], bool],
    args: object = None,
) -> list[str]:
    """Name each missing root, and how args operands that were meant otherwise work.

    *args* is the call's args as sent, for a suggestion that keeps its other items.
    """
    warnings = [_missing_root_message(raw, path, cwd, query, offers) for raw, path in roots.missing]
    missing_operands = [raw for raw, _ in roots.missing if raw in query.operand_roots]
    if missing_operands and query.searches_contents and not query.pattern_operand:
        warnings.extend(_operand_pattern_warning(roots, query, cwd, args))
    if missing_operands and query.mode == "list_files":
        warnings.append(
            "--files lists files by name, so every args operand is a path. To list the files "
            "whose contents match a pattern, replace --files with -l."
        )
    if missing_operands and query.pattern_operand:
        # Words without a path separator were most likely meant as further patterns.
        words = [raw for raw in missing_operands if "/" not in raw and "\\" not in raw]
        patterns = [query.patterns[0], *(words or missing_operands[:1])]
        if query.literal:
            flags = ["-F"]
            for text in patterns:
                flags += ["-e", text]
            combined = "args " + json.dumps(flags, ensure_ascii=False)
        else:
            combined = f'pattern "{"|".join(patterns)}"'
        warnings.append(
            f'The first args operand "{query.patterns[0]}" is the pattern and later operands '
            "are paths. If a missing path was meant as another pattern, combine the patterns: "
            f"{combined}."
        )
    return warnings


def _operand_pattern_warning(
    roots: _Roots, query: SearchQuery, cwd: Path, args: object
) -> list[str]:
    """Say how to search args items that were read as paths as further patterns.

    With pattern set, an args item without a leading - is a path: pattern "class "
    with args ["def ", "return Result"] reported both items as missing paths.
    They stay paths, since a missing path can also be mistyped; only items
    without a path separator and without a similar path count as text.
    """
    words = [
        raw
        for raw, path in roots.missing
        if raw in query.operand_roots
        and "/" not in raw
        and "\\" not in raw
        and not corrected_paths(path, cwd)
    ]
    if not words:
        return []
    if isinstance(args, list) and all(isinstance(item, str) for item in args):
        suggested: list[str] = []
        for item in args:
            suggested += ["-e", item] if item in words else [item]
    else:
        suggested = [part for word in words for part in ("-e", word)]
    listed = " and ".join(f'"{word}"' for word in dict.fromkeys(words))
    read, search = (
        ("were read as paths", "them as further patterns, put -e before each")
        if len(words) > 1
        else ("was read as a path", "it as another pattern, put -e before it")
    )
    return [
        f"args takes ripgrep options, and an item without a leading - is a path, so {listed} "
        f"{read}. To search {search}: args {json.dumps(suggested, ensure_ascii=False)}."
    ]


def _nothing_searched(warnings: list[str], roots: _Roots, query: SearchQuery) -> str:
    """The error when every root is missing, naming the field the missing roots came from."""
    if all(raw in query.operand_roots for raw, _ in roots.missing):
        several = len(roots.missing) > 1
        fix = (
            f"Correct the path{'s' if several else ''} in args, or remove "
            f"{'them' if several else 'it'} to search the working directory."
        )
    else:
        fix = "Correct path, or omit it to search the working directory."
    return f"{' '.join(warnings)} Nothing was searched. {fix}"


def _absolute(text: str, cwd: Path) -> Path:
    return Path(os.path.abspath(cwd / Path(text).expanduser()))


def _add(paths: list[Path], path: Path) -> None:
    if all(os.path.normcase(path) != os.path.normcase(known) for known in paths):
        paths.append(path)


def _existing_alternatives(text: str, cwd: Path) -> list[Path]:
    """Return the paths a brace or comma list names, when every one of them exists."""
    if "{" in text:
        alternatives = _expand_brace_alternations(text)
    elif "," in text:
        alternatives = [part.strip() for part in text.split(",")]
    else:
        return []
    if len(alternatives) < 2 or not all(alternatives):
        return []
    paths = [_absolute(alternative, cwd) for alternative in alternatives]
    return paths if all(os.path.lexists(path) for path in paths) else []


def _glob_root(text: str, cwd: Path, query: SearchQuery) -> Path | None:
    """Search a path written as a glob, such as src/**/*.py, as its directory and glob."""
    if query.globs or not _GLOB_CHARACTERS.search(text):
        return None
    parts = Path(text).parts
    fixed = next(index for index, part in enumerate(parts) if _GLOB_CHARACTERS.search(part))
    base = _absolute(str(Path(*parts[:fixed])) if fixed else ".", cwd)
    if not base.is_dir():
        return None
    remainder = "/".join(parts[fixed:])
    try:
        glob = "/".join(("", *base.relative_to(cwd).parts, remainder))
    except ValueError:
        glob = "/" + remainder
    query.globs.append(glob)
    query.notes.append(
        f'path "{text}" contains glob characters, so {path_label(base, cwd)} was searched with '
        f'glob "{glob}".'
    )
    return base


_GLOB_SYNTAX = re.compile(r"[*?\[\]{}]")
_REGEX_SYNTAX = re.compile(r"[.^$*+?()\[\]{}|\\]")


def _directory_globs(query: SearchQuery, roots: list[Path], cwd: Path) -> None:
    """Also match the files below a directory that a glob names exactly.

    ripgrep matches globs against files, so glob "minidb" matched nothing even
    with a directory minidb. Such a glob also selects that directory's files,
    with a note. Directory listings match globs against directories already.
    """
    if query.mode == "list_dirs":
        return
    bases = [cwd, *(root for root in roots if root.is_dir())]
    globs: list[str] = []
    for glob in query.globs:
        globs.append(glob)
        if glob.startswith("!") or _GLOB_SYNTAX.search(glob):
            continue
        name = glob.replace("\\", "/").removeprefix("./").strip("/")
        if Path(name).is_absolute() or {"", ".", ".."} & set(name.split("/")):
            continue
        if not any((base / name).is_dir() for base in bases):
            continue
        if glob.startswith("/"):
            globs.append(f"/{name}/**")
        else:
            globs.append(f"{name}/**" if "/" in name else f"**/{name}/**")
        query.notes.append(f'glob "{glob}" names a directory, so the files below it were matched.')
    query.globs = globs


def _missing_root_message(
    raw: str, root: Path, cwd: Path, query: SearchQuery, offers: Callable[[str], bool]
) -> str:
    # A Swarm Wiki page searched for one plain text reads with the lines holding it.
    patterns = query.patterns
    plain = len(patterns) == 1 and (query.literal or not _REGEX_SYNTAX.search(patterns[0]))
    page = wiki_page_call(root, cwd, offers, fields={"query": patterns[0]} if plain else None)
    message = f"Path not found: {path_label(root, cwd)}"
    suggestions = corrected_paths(root, cwd)
    if suggestions:
        message += f" (similar: {', '.join(path_label(path, cwd) for path in suggestions)})"
    elif page:
        return f"{path_label(root, cwd)} is not a file or directory. {page}"
    elif not Path(raw).expanduser().is_absolute():
        # Agents pass paths relative to another directory than the one search_files uses.
        message += f" (relative to the working directory {cwd.as_posix()})"
    return f"{message}. {page}" if page else message + "."
