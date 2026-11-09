"""Resolve file-search roots and explain missing or glob-shaped paths.

Under Permission Rules, every path is checked for reading before it is touched:
a refused root refuses the whole call (``PermissionDenied``), and suggestions
and directory globs consider only readable paths.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.permissions import (
    PermissionBoundary,
    PermissionDenied,
    PermissionGrant,
    run_permission_decision,
)
from core.tools._path_suggestions import corrected_paths, refused_note
from core.tools._permission_preflight import authorize_paths
from core.tools._search_arguments import interpret_search_call
from core.tools._search_query import (
    SearchArgumentError,
    SearchQuery,
    glob_separators,
)
from core.tools._search_results import (
    path_label,
)
from core.tools._swarm_references import wiki_page_call
from core.tools._tool_context import ToolContext, _path_argument
from core.tools.contracts import JsonObject, ToolContractError
from core.tools.edit import offered_edit_tool
from core.tools.search import _expand_brace_alternations
from core.utils.file_status import exists_strict, is_dir_strict

_GLOB_CHARACTERS = re.compile(r"[*?\[]")


@dataclass
class _Roots:
    """The roots one call searches, and the requested paths that do not exist."""

    paths: list[Path] = field(default_factory=list)
    missing: list[tuple[str, Path]] = field(default_factory=list)
    plain: list[Path] = field(default_factory=list)
    globs: list[tuple[Path, str]] = field(default_factory=list)

    def add(self, path: Path, glob: str | None = None) -> None:
        """Keep each root's selection separate from filters for the whole call."""
        _add(self.paths, path)
        if glob is None:
            _add(self.plain, path)
        elif (path, glob) not in self.globs:
            self.globs.append((path, glob))


def _resolve_roots(
    query: SearchQuery,
    cwd: Path,
    permissions: PermissionBoundary,
    grant: PermissionGrant | None = None,
    *,
    context: ToolContext,
) -> _Roots:
    """Resolve the requested roots; a missing path is reported, never replaced.

    A missing path written with glob characters, braces or commas runs as what
    it evidently means when that names existing paths: ``src/**/*.py`` searches
    ``src`` with that glob, ``{src,tests}`` and ``src,tests`` search both. A
    missing path written as ``!vendor/**`` is the excluding glob it reads as.
    """
    _local_globs(query, cwd, permissions, grant)
    roots = _Roots()
    excluded = False
    for raw in query.roots or [str(cwd)]:
        text = str(_path_argument(raw, windows=os.name == "nt"))
        if not text.strip() or text == "-":
            raise SearchArgumentError(
                f'path must name a file or directory; received "{raw}". Omit path to search '
                "the working directory."
            )
        literal_error = None
        try:
            candidate = _search_path(text, context)
        except ToolContractError as error:
            # A list of absolute Windows paths cannot be a literal filename:
            # the second drive colon is invalid there, but valid after splitting.
            literal_error = error
            candidate = None
        # Windows cannot store * or ? in names. Every other spelling gets its
        # literal approval and existence check before any list/glob interpretation.
        impossible_literal = os.name == "nt" and any(c in str(candidate) for c in "*?")
        resolved = candidate
        if candidate is not None and not impossible_literal:
            resolved = _checked(candidate, permissions, grant)
            if _exists(resolved):
                roots.add(_actual_case(resolved))
                continue
        alternatives = _existing_alternatives(text, cwd, permissions, grant, context=context)
        if alternatives:
            for path in alternatives:
                roots.add(path)
            shown = ", ".join(path_label(path, cwd) for path in alternatives)
            query.notes.append(f'path "{raw}" names several paths, so {shown} were searched.')
            continue
        if literal_error is not None:
            raise literal_error
        assert candidate is not None and resolved is not None
        if text.startswith("!") and len(text) > 1:
            query.globs.append(text)
            query.notes.append(
                f'path "{raw}" starts with !, so it was applied as an excluding glob.'
            )
            excluded = True
            continue
        # The cwd is literal filesystem context, never part of the Agent's glob.
        glob_text = (
            str(candidate.relative_to(cwd)) if candidate.is_relative_to(cwd) else str(candidate)
        )
        selected = _glob_root(glob_text, cwd, query, permissions, grant)
        if selected is not None:
            roots.add(*selected)
            continue
        roots.missing.append((raw, resolved))
    if excluded and not roots.paths and not roots.missing:
        roots.add(cwd)
    return roots


def _missing_warnings(
    query: SearchQuery,
    roots: _Roots,
    cwd: Path,
    offers: Callable[[str], bool],
    permissions: PermissionBoundary,
    args: object = None,
    grant: PermissionGrant | None = None,
) -> list[str]:
    """Name each missing root, and how args operands that were meant otherwise work.

    *args* is the call's args as sent, for a suggestion that keeps its other items.
    """

    def readable(path: Path) -> bool:
        return (grant or permissions).check_path(path, "read") is None or (
            grant is not None and any(grant.search_read_allowed(path, root) for root in roots.paths)
        )

    warnings = [
        _missing_root_message(raw, path, cwd, query, offers, permissions, readable)
        for raw, path in roots.missing
    ]
    missing_operands = [raw for raw, _ in roots.missing if raw in query.operand_roots]
    if missing_operands and query.searches_contents and not query.pattern_operand:
        warnings.extend(_operand_pattern_warning(roots, query, cwd, args, permissions, readable))
    if (
        missing_operands
        and isinstance(args, list)
        and any(
            isinstance(item, str) and re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*", item)
            for item in args
        )
    ):
        warnings.append(
            "-r and -R mean recursive search here and take no value; replacement is not "
            "available. The following operand was read as a path."
        )
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
    roots: _Roots,
    query: SearchQuery,
    cwd: Path,
    args: object,
    permissions: PermissionBoundary,
    readable: Callable[[Path], bool],
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
        and not corrected_paths(path, cwd, permissions=permissions, readable=readable)
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


def _local_globs(
    query: SearchQuery,
    cwd: Path,
    permissions: PermissionBoundary,
    grant: PermissionGrant | None = None,
) -> None:
    """Read globs written as absolute paths as the globs they select.

    ripgrep matches globs against paths below the searched directory, so an
    absolute glob matched nothing. One whose fixed directory lies in the
    working directory is anchored there (``/src/*.py``); one outside it, in a
    call without path, searches that directory with the rest of the glob.
    """
    globs: list[str] = []
    outside: Path | None = None
    missing: PermissionDenied | None = None
    for glob in query.globs:
        negated = glob.startswith("!")
        try:
            split = _absolute_glob(
                glob_separators(glob[1:] if negated else glob),
                cwd,
                permissions,
                grant,
                required=not negated and not query.roots,
            )
        except PermissionDenied as error:
            if error.refusal.reason != "not_permitted":
                raise
            missing = missing or error
            continue
        if split is None:
            globs.append(glob)
            continue
        base, remainder = split
        if base.is_relative_to(cwd):
            globs.append(
                ("!" if negated else "") + "/".join(("", *base.relative_to(cwd).parts, remainder))
            )
        elif not negated and not query.roots and outside in (None, base):
            outside = base
            globs.append(f"/{remainder}")
            query.notes.append(
                f'glob "{glob}" is an absolute path, so {path_label(base, cwd)} was searched '
                f'with glob "/{remainder}".'
            )
        else:
            globs.append(glob)
    if missing is not None:
        raise missing
    query.globs = globs
    if outside is not None:
        query.roots = [str(outside)]


def _absolute_glob(
    glob: str,
    cwd: Path,
    permissions: PermissionBoundary,
    grant: PermissionGrant | None = None,
    *,
    required: bool = False,
) -> tuple[Path, str] | None:
    """Split an absolute glob into its existing fixed directory and the glob below it.

    Off Windows, ``/src/*.py`` stays ripgrep's glob anchored at the searched
    directory when that directory holds ``src``.
    """
    path = Path(glob)
    if not path.is_absolute():
        return None
    parts = path.parts
    fixed = next(
        (index for index, part in enumerate(parts) if _GLOB_SYNTAX.search(part)),
        len(parts) - 1,
    )
    if fixed < 1:
        return None
    base = Path(*parts[:fixed])
    anchored = cwd.joinpath(*parts[1:fixed])
    if os.name != "nt" and _readable_dir(anchored, permissions):
        return None
    if required:
        _checked(base, permissions, grant)
    if not _readable_dir(base, permissions, grant):
        return None
    return base, "/".join(parts[fixed:])


def _readable_dir(
    path: Path, permissions: PermissionBoundary, grant: PermissionGrant | None = None
) -> bool:
    return (grant or permissions).check_path(path, "read") is None and is_dir_strict(path)


def _absolute(text: str, cwd: Path) -> Path:
    return Path(os.path.abspath(cwd / Path(text).expanduser()))


def _search_path(text: str, context: ToolContext) -> Path:
    """Use the shared file-Tool resolver before interpreting glob punctuation."""
    path = context.path_target(text, access="read")
    if os.name == "nt":
        spelling = str(path).replace("\\", "/")
        if spelling[:8].casefold() == "//?/unc/":
            path = Path("//" + spelling[8:])
        elif re.match(r"^//\?/[A-Za-z]:/", spelling):
            path = Path(spelling[4:])
        elif spelling.startswith("//?/"):
            raise SearchArgumentError(
                f'Nothing was searched. Device path "{text}" is not a local file path. '
                "Send a drive-letter path or a UNC file path."
            )
    return Path(os.path.abspath(path))


def _actual_case(path: Path) -> Path:
    # realpath asks Windows for stored spelling; preserve explicit link identity.
    if os.name == "nt":
        actual = Path(os.path.realpath(path))
        if os.path.normcase(actual) == os.path.normcase(path):
            return actual
    return path


def _checked(
    path: Path, permissions: PermissionBoundary, grant: PermissionGrant | None = None
) -> Path:
    """Return ``path``, or refuse the call when the rules refuse reading it."""
    refusal = (grant or permissions).check_path(path, "read")
    if refusal is not None:
        raise PermissionDenied(refusal)
    return path


def _exists(path: Path) -> bool:
    try:
        return exists_strict(path, follow_symlinks=False)
    except OSError as error:
        # Windows rejects glob characters as a literal path before interpretation.
        if getattr(error, "winerror", None) == 123 and _GLOB_CHARACTERS.search(str(path)):
            return False
        raise


def _add(paths: list[Path], path: Path) -> None:
    if all(os.path.normcase(path) != os.path.normcase(known) for known in paths):
        paths.append(path)


def _existing_alternatives(
    text: str,
    cwd: Path,
    permissions: PermissionBoundary,
    grant: PermissionGrant | None = None,
    *,
    context: ToolContext,
) -> list[Path]:
    """Return the paths a brace or comma list names, when every one of them exists."""
    if "{" in text:
        alternatives = _expand_brace_alternations(text)
    elif "," in text:
        alternatives = [part.strip() for part in text.split(",")]
    else:
        return []
    if len(alternatives) < 2 or not all(alternatives):
        return []
    paths = [_search_path(alternative, context) for alternative in alternatives]
    missing = None
    for path in paths:
        refusal = (grant or permissions).check_path(path, "read")
        if refusal is not None:
            if refusal.reason != "not_permitted":
                raise PermissionDenied(refusal)
            missing = missing or refusal
    if missing is not None:
        raise PermissionDenied(missing)
    return paths if all(_exists(path) for path in paths) else []


def _glob_root(
    text: str,
    cwd: Path,
    query: SearchQuery,
    permissions: PermissionBoundary,
    grant: PermissionGrant | None = None,
) -> tuple[Path, str] | None:
    """Search a path written as a glob, such as src/**/*.py, as its directory and glob."""
    if any(not glob.startswith("!") for glob in query.globs) or not _GLOB_CHARACTERS.search(text):
        return None
    parts = Path(text).parts
    fixed = next(index for index, part in enumerate(parts) if _GLOB_CHARACTERS.search(part))
    if fixed == 0 and Path(text).is_absolute():
        raise SearchArgumentError(
            f'Nothing was searched. Invalid path "{text}". '
            "Send a local path without a device prefix."
        )
    while fixed < len(parts):
        if os.name == "nt" and any(char in parts[fixed] for char in "*?"):
            break
        literal = _checked(_absolute(str(Path(*parts[: fixed + 1])), cwd), permissions, grant)
        if not is_dir_strict(literal):
            break
        fixed = next(
            (
                index
                for index in range(fixed + 1, len(parts))
                if _GLOB_CHARACTERS.search(parts[index])
            ),
            len(parts),
        )
    if fixed == len(parts):
        return None
    base = _checked(_absolute(str(Path(*parts[:fixed])) if fixed else ".", cwd), permissions, grant)
    if not is_dir_strict(base):
        return None
    remainder = "/".join(parts[fixed:])
    try:
        prefix = [
            re.sub(r"[\[\]{}*?]", lambda match: f"[{match[0]}]", part)
            for part in base.relative_to(cwd).parts
        ]
        glob = "/".join(("", *prefix, remainder))
    except ValueError:
        glob = "/" + remainder
    query.notes.append(
        f'path "{text}" contains glob characters, so {path_label(base, cwd)} was searched with '
        f'glob "{glob}".'
    )
    return base, glob


_GLOB_SYNTAX = re.compile(r"[*?\[\]{}]")
_REGEX_SYNTAX = re.compile(r"[.^$*+?()\[\]{}|\\]")


def split_glob_lists(query: SearchQuery) -> list[str] | None:
    """Repair separate wildcard filters only after the written selection was empty.

    Braces and character classes own their separators. Literal spaces/commas
    keep their meaning whenever the original glob selects a file.
    """
    changed = False
    globs: list[str] = []
    for glob in query.globs:
        pieces: list[str] = []
        start = depth = 0
        escaped = False
        for index, char in enumerate(glob):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth = max(0, depth - 1)
            elif not depth and (char == "," or char.isspace()):
                if glob[start:index].strip():
                    pieces.append(glob[start:index].strip())
                start = index + 1
        if glob[start:].strip():
            pieces.append(glob[start:].strip())
        if (
            len(pieces) > 1
            and all(
                _GLOB_CHARACTERS.search(piece)
                and not piece.startswith("!")
                and not Path(piece).is_absolute()
                for piece in pieces
            )
            and not glob.startswith("!")
        ):
            globs.extend(pieces)
            changed = True
        else:
            globs.append(glob)
    return globs if changed else None


def _directory_globs(
    query: SearchQuery,
    roots: list[Path],
    cwd: Path,
    permissions: PermissionBoundary,
    grant: PermissionGrant | None = None,
) -> None:
    """Also match the files below a directory that a glob names exactly.

    ripgrep matches globs against files, so glob "minidb" matched nothing even
    with a directory minidb. Such a glob also selects that directory's files,
    with a note. Directory listings match globs against directories already.
    """
    if query.mode == "list_dirs":
        return
    readable = permissions.check_path(cwd, "read") is None
    bases = [*([cwd] if readable else []), *(root for root in roots if root.is_dir())]
    globs: list[str] = []
    for glob in query.globs:
        globs.append(glob)
        if glob.startswith("!") or _GLOB_SYNTAX.search(glob):
            continue
        name = glob.replace("\\", "/").removeprefix("./").strip("/")
        if Path(name).is_absolute() or {"", ".", ".."} & set(name.split("/")):
            continue
        if not any(
            (
                (grant or permissions).check_path(base / name, "read") is None
                or (grant is not None and grant.search_read_allowed(base / name, base))
            )
            and is_dir_strict(base / name)
            for base in bases
        ):
            continue
        if glob.startswith("/"):
            globs.append(f"/{name}/**")
        else:
            globs.append(f"{name}/**" if "/" in name else f"**/{name}/**")
        query.notes.append(f'glob "{glob}" names a directory, so the files below it were matched.')
    query.globs = globs


def _missing_root_message(
    raw: str,
    root: Path,
    cwd: Path,
    query: SearchQuery,
    offers: Callable[[str], bool],
    permissions: PermissionBoundary,
    readable: Callable[[Path], bool],
) -> str:
    # A Swarm Wiki page searched for one plain text reads with the lines holding it.
    patterns = query.patterns
    plain = len(patterns) == 1 and (query.literal or not _REGEX_SYNTAX.search(patterns[0]))
    page = wiki_page_call(root, cwd, offers, fields={"query": patterns[0]} if plain else None)
    message = f"Path not found: {path_label(root, cwd)}"
    suggestions = corrected_paths(root, cwd, permissions=permissions, readable=readable)
    refused = refused_note(root, cwd, permissions, also=bool(suggestions))
    if suggestions:
        message += f" (similar: {', '.join(path_label(path, cwd) for path in suggestions)})"
    elif page:
        return f"{path_label(root, cwd)} is not a file or directory.{refused} {page}"
    elif not Path(raw).expanduser().is_absolute():
        # Agents pass paths relative to another directory than the one search_files uses.
        message += f" (relative to the working directory {cwd.as_posix()})"
    return f"{message}.{refused} {page}" if page else f"{message}.{refused}"


async def _preflight_search(context: ToolContext, arguments: JsonObject) -> None:
    if not context.permissions.restricted:
        return
    try:
        query = interpret_search_call(arguments, edit_tool=offered_edit_tool(context))
    except SearchArgumentError, ToolContractError:
        return
    if query.mode in {"help", "reference"}:
        return
    cwd = Path(os.path.abspath(context.effective_cwd.expanduser()))
    flags = [
        value
        for item in query.rg_args
        for name, sep, value in [item.partition("=")]
        if sep and name in {"--file", "--ignore-file"}
    ]
    checked: set[Path] = set()

    def prepare() -> list[Path]:
        paths: list[Path] = []
        requested: list[str | None] = list(query.roots) if query.roots else [None]
        for raw in requested:
            candidate = replace(
                query, roots=[raw] if raw is not None else [], globs=list(query.globs), notes=[]
            )
            try:
                resolved = _resolve_roots(
                    candidate, cwd, context.permissions, context.permission_access, context=context
                )
                paths.extend(resolved.paths)
            except PermissionDenied as error:
                if error.refusal.reason != "not_permitted":
                    raise
                paths.append(Path(error.refusal.target))
        return list(dict.fromkeys(path for path in paths if path not in checked))

    while True:
        try:
            roots = await run_permission_decision(prepare)
        except SearchArgumentError, ToolContractError, OSError:
            # The handler owns argument and filesystem failure envelopes. No
            # approval is useful for a call that cannot reach the search phase.
            return
        if not roots and not flags:
            return
        await authorize_paths(
            context,
            [
                *((path, "read", {"filtered": True}) for path in roots),
                *((path, "read", {}) for path in flags),
            ],
            arguments=arguments,
        )
        checked.update(roots)
        flags = []
