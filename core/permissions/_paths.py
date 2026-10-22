"""Path normalization and glob matching shared by every permission check.

A path is compared in two forms: its lexical form (separators, ``.``/``..``,
Windows trailing dots and spaces, ``\\\\?\\`` drive prefixes and 8.3 short names
normalized, nothing followed) and its resolved form (links and junctions followed
with ``realpath``). Paths are kept with ``/`` separators; on Windows they compare
case-insensitively through :func:`path_key`.

Network paths, device paths, alternate data streams and drive-relative paths are
recognized lexically, before anything touches the filesystem, so a UNC path never
contacts SMB.
"""

from __future__ import annotations

import functools
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

# What an unsafe path form is called in refusals.
NETWORK_PATH = "network path"
DEVICE_PATH = "device path"
ALTERNATE_DATA_STREAM = "alternate data stream"
DRIVE_RELATIVE_PATH = "drive-relative path"

_DRIVE = re.compile(r"([A-Za-z]):")
_WILDCARD = re.compile(r"[*?]")


@dataclass(frozen=True, slots=True)
class LexicalPath:
    """A lexically normalized path: its root and its segments.

    ``root`` is ``"C:/"`` or ``"/"`` for absolute paths, ``"//server/share/"`` for
    network paths and ``""`` for relative ones (whose leading ``..`` segments stay).
    ``unsafe`` names an unsafe form (:data:`NETWORK_PATH`, ...) or is ``None``.
    """

    root: str
    segments: tuple[str, ...]
    unsafe: str | None = None

    @property
    def absolute(self) -> bool:
        return bool(self.root)

    @property
    def text(self) -> str:
        if not self.root:
            return "/".join(self.segments) or "."
        return self.root + "/".join(self.segments)


@dataclass(frozen=True, slots=True)
class NormalizedPath:
    """Both forms of one access path, with ``/`` separators.

    ``resolved`` equals ``lexical`` for an unsafe form, which is never resolved.
    """

    lexical: str
    resolved: str
    unsafe: str | None = None


def path_key(text: str, *, windows: bool) -> str:
    """The comparison key of a normalized path: case-folded on Windows."""
    return text.casefold() if windows else text


def is_inside(key: str, folder_key: str) -> bool:
    """Whether the path ``key`` is ``folder_key`` or below it (both comparison keys)."""
    if key == folder_key:
        return True
    prefix = folder_key if folder_key.endswith("/") else folder_key + "/"
    return key.startswith(prefix)


def has_wildcard(text: str) -> bool:
    return _WILDCARD.search(text) is not None


def split_lexical(text: str, *, windows: bool) -> LexicalPath:
    """Normalize ``text`` lexically without touching the filesystem.

    ``.`` segments and empty segments disappear; ``..`` is applied to the segments
    before it (above the root it stays at the root; in a relative path a leading
    ``..`` stays). Windows segments lose trailing dots and spaces, as Windows does.
    """
    unsafe: str | None = None
    if text[:5].lower() == "file:":
        text, unsafe = _file_url_path(text, windows=windows)
    if windows:
        root, rest, form = _windows_root(text.replace("\\", "/"))
        unsafe = unsafe or form
        if unsafe is None and ":" in rest:
            unsafe = ALTERNATE_DATA_STREAM
    else:
        root = "/" if text.startswith("/") else ""
        rest = text
    segments = _normalize_segments(rest.split("/"), windows=windows, absolute=bool(root))
    return LexicalPath(root, segments, unsafe)


def join_lexical(base: LexicalPath, path: LexicalPath) -> LexicalPath:
    """``path`` made absolute against the absolute ``base``, ``..`` applied again."""
    if path.root == "/" and _DRIVE.match(base.root):
        # A Windows path rooted without a drive lives on the base's drive.
        root = base.root
        segments = path.segments
    elif path.absolute:
        return path
    else:
        root = base.root
        segments = base.segments + path.segments
    collapsed = _normalize_segments(list(segments), windows=False, absolute=True)
    return LexicalPath(root, collapsed, path.unsafe or base.unsafe)


def lexical_text(text: str, *, windows: bool) -> str:
    """The lexical form of an absolute path given by the caller (a folder of the Run)."""
    return split_lexical(text, windows=windows).text


def normalize_access_path(
    path: str | os.PathLike[str],
    *,
    base: str,
    home: str,
    windows: bool,
    follow_missing: bool = True,
) -> NormalizedPath:
    """Normalize one path an access names, relative paths against ``base``.

    ``base`` and ``home`` are absolute lexical paths. ``~`` and ``~/...`` expand to
    ``home``; placeholders do not (a Tool argument ``{workdir}`` is a file name).
    An unsafe form is returned unresolved; every other path is also resolved
    through links and junctions on its longest existing prefix. Without
    ``follow_missing``, a path that names no existing entry stays unresolved:
    reading it reaches nothing a link could lead to, and the cheap existence
    check spares a ``realpath`` per word.
    """
    text = os.fspath(path)
    if "\x00" in text:
        return NormalizedPath(text, text, DEVICE_PATH)
    if text == "~" or text.startswith(("~/", "~\\" if windows else "~/")):
        text = home + text[1:]
    base_path = split_lexical(base, windows=windows)
    lexical = join_lexical(base_path, split_lexical(text, windows=windows))
    if lexical.unsafe is not None:
        return NormalizedPath(lexical.text, lexical.text, lexical.unsafe)
    if windows:
        lexical = _expand_short_names(lexical)
        # Win32 applies ``..`` lexically before the filesystem sees the path.
        resolved_input = lexical.text
    elif text[:5].lower() == "file:":
        resolved_input = lexical.text
    else:
        # POSIX follows a link before ``..`` climbs out of it.
        resolved_input = text if text.startswith("/") else base.rstrip("/") + "/" + text
    if not follow_missing and not os.path.lexists(resolved_input):
        return NormalizedPath(lexical.text, lexical.text)
    return NormalizedPath(lexical.text, resolve_text(resolved_input, windows=windows))


def resolve_text(text: str, *, windows: bool) -> str:
    """The resolved form of an absolute path: links and junctions followed.

    The longest existing prefix is resolved; the rest is kept as written. A prefix
    that cannot be checked stays as written too, which is what the filesystem
    itself would reach.
    """
    resolved = os.path.realpath(text)
    return split_lexical(resolved, windows=windows).text


def compile_pattern(pattern_key: str) -> re.Pattern[str]:
    """Compile one absolute, normalized pattern key into a regular expression.

    ``*`` matches within one segment, ``**`` as a whole segment any depth, ``?``
    one character. A pattern covers the entries it names and everything below them.
    """
    out: list[str] = []
    index = 0
    text = pattern_key
    covers_below = True
    while index < len(text):
        if text.startswith("/**/", index):
            out.append("/(?:.*/)?")
            index += 4
        elif text.startswith("/**", index) and index + 3 == len(text):
            out.append("(?:/.*)?")
            index += 3
            covers_below = False
        elif text.startswith("**", index):
            # ``**`` inside a segment, or right after a root that ends with "/".
            at_segment_start = index == 0 or text[index - 1] == "/"
            at_segment_end = index + 2 == len(text) or text[index + 2] == "/"
            if at_segment_start and at_segment_end:
                out.append(".*")
                covers_below = index + 2 != len(text)
            else:
                out.append("[^/]*")
            index += 2
        elif text[index] == "*":
            out.append("[^/]*")
            index += 1
        elif text[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(text[index]))
            index += 1
    if covers_below:
        out.append("(?:.*)?" if text.endswith("/") else "(?:/.*)?")
    return re.compile("".join(out))


def literal_prefix(lexical: LexicalPath) -> LexicalPath:
    """The pattern's segments before the first one with a wildcard."""
    segments: list[str] = []
    for segment in lexical.segments:
        if has_wildcard(segment):
            break
        segments.append(segment)
    return LexicalPath(lexical.root, tuple(segments), lexical.unsafe)


def patterns_below(pattern_key: str, folder_key: str) -> set[str]:
    """What of the pattern ``pattern_key`` can match below ``folder_key``, relative to it.

    Both are comparison keys. Each result is a relative pattern in the same
    syntax that also covers everything below what it names; together they cover
    at least every match below the folder. ``**`` stands for the whole folder.
    """
    found = _relative(tuple(pattern_key.rstrip("/").split("/")), folder_key.rstrip("/").split("/"))
    return {pattern.removesuffix("/**") for pattern in found}


def spelled_like(relative: str, pattern_key: str, written: str) -> str:
    """``relative``, a :func:`patterns_below` result for ``pattern_key``, spelled as ``written``.

    ``written`` is the pattern the key was made from. Keys fold case on
    Windows, and the Agent should read a rule's part as the rule writes it, with
    its closing ``/**``. A result that is not the tail of the key keeps the
    key's spelling.
    """
    tail = relative.split("/")
    core = pattern_key.rstrip("/").removesuffix("/**").split("/")
    text = written.replace("\\", "/").rstrip("/")
    segments = text.removesuffix("/**").split("/")
    if relative == "**" or core[-len(tail) :] != tail or len(segments) < len(tail):
        return relative
    spelled = "/".join(segments[-len(tail) :])
    return f"{spelled}/**" if text.endswith("/**") else spelled


def reaches(folder_key: str, reach: tuple[str, ...], pattern_key: str) -> bool:
    """Whether an access to ``reach`` below ``folder_key`` can touch what ``pattern_key`` covers.

    Both cover an entry and everything below it only where they end in ``**``;
    a pattern key always covers what lies below its matches.
    """
    access = (*folder_key.rstrip("/").split("/"), *reach)
    covered = (*pattern_key.rstrip("/").split("/"), "**")
    return _segments_meet(tuple(map(_segment, access)), tuple(map(_segment, covered)))


def _segment(text: str) -> str:
    return "**" if text == "**" else text.replace("**", "*")


@functools.cache
def _segments_meet(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Whether two segment patterns (``*``, ``?`` within, ``**`` across) share a path."""
    if not first or not second:
        rest = first or second
        return all(segment == "**" for segment in rest)
    if first[0] == "**" and (
        _segments_meet(first[1:], second) or _segments_meet(first, second[1:])
    ):
        return True
    if second[0] == "**" and (
        _segments_meet(first, second[1:]) or _segments_meet(first[1:], second)
    ):
        return True
    if "**" in (first[0], second[0]):
        return False
    return _segment_meets(first[0], second[0]) and _segments_meet(first[1:], second[1:])


@functools.cache
def _segment_meets(first: str, second: str) -> bool:
    """Whether two one-segment wildcard patterns match a common name."""

    @functools.cache
    def meet(i: int, j: int) -> bool:
        if i == len(first) and j == len(second):
            return True
        if i < len(first) and first[i] == "*" and meet(i + 1, j):
            return True
        if j < len(second) and second[j] == "*" and meet(i, j + 1):
            return True
        if i == len(first) or j == len(second):
            return False
        if first[i] == "*":
            return meet(i, j + 1)
        if second[j] == "*":
            return meet(i + 1, j)
        return (first[i] == second[j] or "?" in (first[i], second[j])) and meet(i + 1, j + 1)

    return meet(0, 0)


def pruned_away(pattern_key: str, folder_key: str, pruned: frozenset[str]) -> bool:
    """Whether every match of the pattern lies in a folder the access leaves out by name.

    True when a literal segment of the pattern that lies below the folder in
    every match is a pruned name: one at or past the folder's depth with no
    ``**`` above that depth before it.
    """
    if not pruned:
        return False
    depth = len(folder_key.rstrip("/").split("/"))
    for index, segment in enumerate(pattern_key.rstrip("/").split("/")):
        if "**" in segment:
            if index < depth:
                return False  # where the rest lies is not fixed
            continue
        if index >= depth and not has_wildcard(segment) and segment in pruned:
            return True
    return False


def _relative(pattern: tuple[str, ...], folder: list[str]) -> set[str]:
    for index, segment in enumerate(folder):
        if index == len(pattern):
            return {"**"}
        if pattern[index] == "**":
            rest = pattern[index + 1 :]
            # ``**`` absorbs some of the folder's segments, or all of them and more.
            found = {"/".join(("**", *rest))}
            for skip in range(index, len(folder)):
                found |= _relative(rest, folder[skip:])
            return found
        if compile_pattern(pattern[index]).fullmatch(segment) is None:
            return set()
    rest = pattern[len(folder) :]
    return {"/".join(rest)} if rest else {"**"}


def _file_url_path(text: str, *, windows: bool) -> tuple[str, str | None]:
    parts = urlsplit(text)
    host = parts.netloc
    path = unquote(parts.path)
    if windows and re.match(r"/[A-Za-z]:", path):
        path = path[1:]
    if host and host.lower() != "localhost":
        return f"//{host}{path}", NETWORK_PATH
    return path, None


def _windows_root(text: str) -> tuple[str, str, str | None]:
    """Split a ``/``-separated Windows path into root, rest and an unsafe form."""
    if text.startswith(("//?/", "//./")):
        rest = text[4:]
        if text[2] == "?" and re.match(r"[A-Za-z]:(/|$)", rest):
            text = rest
        elif text[2] == "?" and rest[:4].lower() == "unc/":
            server, share, remainder = _server_share(rest[4:])
            return f"//{server}/{share}/", remainder, NETWORK_PATH
        else:
            return "//./", rest, DEVICE_PATH
    elif text.startswith("/??/"):
        return "//./", text[4:], DEVICE_PATH
    elif text.startswith("//"):
        server, share, remainder = _server_share(text[2:])
        return f"//{server}/{share}/", remainder, NETWORK_PATH
    drive = _DRIVE.match(text)
    if drive is not None:
        rest = text[2:]
        if not rest.startswith("/"):
            return f"{drive.group(1).upper()}:/", rest, DRIVE_RELATIVE_PATH
        return f"{drive.group(1).upper()}:/", rest, None
    if text.startswith("/"):
        return "/", text, None
    return "", text, None


def _server_share(text: str) -> tuple[str, str, str]:
    parts = text.lstrip("/").split("/", 2)
    server = parts[0] if parts else ""
    share = parts[1] if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""
    return server, share, rest


def _normalize_segments(raw: list[str], *, windows: bool, absolute: bool) -> tuple[str, ...]:
    segments: list[str] = []
    for segment in raw:
        if windows and segment not in (".", ".."):
            segment = segment.rstrip(" .")
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments and segments[-1] != "..":
                segments.pop()
            elif not absolute:
                segments.append(segment)
            continue
        segments.append(segment)
    return tuple(segments)


def _expand_short_names(lexical: LexicalPath) -> LexicalPath:
    """Expand Windows 8.3 short names on the longest existing prefix."""
    if not any("~" in segment for segment in lexical.segments):
        return lexical
    for end in range(len(lexical.segments), 0, -1):
        if not any("~" in segment for segment in lexical.segments[:end]):
            break
        prefix = lexical.root + "/".join(lexical.segments[:end])
        expanded = _long_path_name(prefix)
        if expanded is not None:
            head = split_lexical(expanded, windows=True)
            if head.root.casefold() != lexical.root.casefold() or head.unsafe is not None:
                return lexical
            return LexicalPath(lexical.root, head.segments + lexical.segments[end:])
    return lexical


def _long_path_name(path: str) -> str | None:
    # An ``if`` block, not an early return: mypy for other platforms then skips
    # the name defined only on Windows.
    if sys.platform == "win32":
        import ctypes

        function = _get_long_path_name()
        size = function(path, None, 0)
        if size != 0:
            buffer = ctypes.create_unicode_buffer(size)
            if function(path, buffer, size) != 0:
                return buffer.value
    return None


if sys.platform == "win32":

    @functools.cache
    def _get_long_path_name() -> Callable[..., int]:
        """kernel32's ``GetLongPathNameW``, loaded on first use and kept."""
        import ctypes
        from ctypes import wintypes

        function = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        function.restype = wintypes.DWORD
        return function
