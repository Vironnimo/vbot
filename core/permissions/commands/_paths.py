"""Lexical path resolution for command analysis: no filesystem access.

Every function here works on text only. It turns a word the command line names
into an absolute, lexically normalized ``Path`` of the host against a known
working directory, or says why that is impossible (``Unresolvable``) or that
the word names no file (``None``: devices such as ``NUL`` or ``/dev/null``,
PowerShell drives such as ``Env:``). Link resolution and the final comparison
belong to the Permission Boundary.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

WINDOWS = sys.platform == "win32"

GLOB_CHARACTERS = frozenset("*?[")
_BASH_GLOB_CHARACTERS = frozenset("*?[{")
_PROGRAM_SUFFIXES = (".exe", ".com", ".cmd", ".bat")
_PROVIDER_PREFIX = re.compile(r"^(?:microsoft\.powershell\.core\\)?filesystem::", re.IGNORECASE)
_DRIVE = re.compile(r"^([A-Za-z]):")
_PSDRIVE = re.compile(r"^([A-Za-z][\w.-]+):(?!//)")
_WINDOWS_DEVICES = re.compile(
    r"^(?:nul|con|prn|aux|com[0-9]|lpt[0-9]|conin\$|conout\$)(?::|\.[^\\/]*)?$", re.I
)
_POSIX_DEVICES = re.compile(
    r"^/(?:dev/(?:null|zero|full|random|urandom|tty|stdin|stdout|stderr|fd/\d+)|proc/self/fd/\d+)$"
)
# PowerShell drives that hold no files: writing to Env:\X sets a variable.
_NON_FILE_DRIVES = frozenset(
    {"env", "variable", "function", "alias", "hklm", "hkcu", "cert", "wsman", "registry"}
)
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True, slots=True)
class Unresolvable:
    """A word that names a file this analysis cannot place; ``reason`` says why."""

    reason: str


def program_name(text: str) -> str:
    """The canonical program name: no directory, no ``.exe``/``.com``/``.cmd``/``.bat``."""
    name = re.split(r"[\\/]", text.rstrip("\\/"))[-1] or text
    lowered = name.lower()
    for suffix in _PROGRAM_SUFFIXES:
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def drive_of(path: Path | None) -> str | None:
    """The upper-case drive letter of a Windows path, else None."""
    if path is None:
        return None
    match = _DRIVE.match(str(path))
    return match.group(1).upper() if match else None


def is_url(text: str) -> bool:
    return bool(_URL.match(text))


def file_url_path(text: str) -> str | None:
    """The path a ``file:`` URL names, else None."""
    if not text.lower().startswith("file:"):
        return None
    rest = text[5:]
    if rest.startswith("///"):
        rest = rest[3:] if WINDOWS and _DRIVE.match(rest[3:]) else rest[2:]
    elif rest.startswith("//"):
        rest = "\\\\" + rest[2:] if WINDOWS else rest[1:]
    return rest or None


def resolve(
    text: str,
    cwd: Path | None,
    home: Path,
    *,
    tilde: bool = True,
    msys: bool = False,
    drives: Mapping[str, Path] | None = None,
) -> Path | Unresolvable | None:
    """The absolute path *text* names against *cwd*, lexically.

    ``msys`` reads POSIX forms as Git Bash on Windows does (``/c/x`` is
    ``C:\\x``). ``drives`` holds the folders a script last used on other
    drives, for drive-relative forms (``D:x``).
    """
    if not text:
        return None
    if WINDOWS:
        return _resolve_windows(text, cwd, home, tilde=tilde, msys=msys, drives=drives or {})
    return _resolve_posix(text, cwd, home, tilde=tilde)


def _resolve_posix(
    text: str, cwd: Path | None, home: Path, *, tilde: bool
) -> Path | Unresolvable | None:
    if _POSIX_DEVICES.match(text) or text == "-":
        return None
    if tilde and text.startswith("~"):
        if text == "~" or text.startswith("~/"):
            return Path(posixpath.normpath(str(home) + text[1:]))
        return Unresolvable("names another user's home directory")
    if text.startswith("/"):
        return Path(posixpath.normpath(text))
    if cwd is None:
        return Unresolvable("is relative to a folder this analysis cannot determine")
    return Path(posixpath.normpath(posixpath.join(str(cwd), text)))


def _resolve_windows(
    text: str,
    cwd: Path | None,
    home: Path,
    *,
    tilde: bool,
    msys: bool,
    drives: Mapping[str, Path],
) -> Path | Unresolvable | None:
    if text == "-":
        return None
    text = _PROVIDER_PREFIX.sub("", text)
    if msys and text.startswith("/"):
        if _POSIX_DEVICES.match(text):
            return None
        mounted = re.match(r"^/([A-Za-z])(?:/|$)", text)
        if mounted is None:
            return Unresolvable(
                "is a POSIX path, which bash on Windows maps to a folder"
                " this analysis does not know"
            )
        text = f"{mounted.group(1)}:\\{text[3:]}"
    text = text.replace("/", "\\")
    if text.startswith(("\\\\.\\", "\\??\\")) or text.lower().startswith("\\\\?\\globalroot"):
        return Path(text)
    if text.lower().startswith("\\\\?\\unc\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    if _WINDOWS_DEVICES.match(text):
        return None
    if tilde and text.startswith("~"):
        if text == "~" or text.startswith("~\\"):
            return Path(ntpath.normpath(str(home) + text[1:]))
        return Unresolvable("names another user's home directory")
    if text.startswith("\\\\"):
        return Path(ntpath.normpath(text))
    drive = _DRIVE.match(text)
    if drive is not None:
        letter = drive.group(1).upper()
        rest = text[2:]
        if rest.startswith("\\"):
            return Path(ntpath.normpath(f"{letter}:{rest}"))
        base = cwd if drive_of(cwd) == letter else drives.get(letter)
        if base is None:
            return Unresolvable(
                f"is relative to the current folder of drive {letter}:,"
                " which this analysis cannot determine"
            )
        return Path(ntpath.normpath(ntpath.join(str(base), rest)))
    psdrive = _PSDRIVE.match(text)
    if psdrive is not None:
        if psdrive.group(1).lower() in _NON_FILE_DRIVES:
            return None
        return Unresolvable(
            f"is on the PowerShell drive {psdrive.group(1)}:,"
            " which this analysis does not map to a folder"
        )
    if text.startswith("\\"):
        if cwd is None:
            return Unresolvable("is relative to the root of a drive this analysis cannot determine")
        anchor = ntpath.splitroot(str(cwd))[0] or str(cwd)[:2]
        return Path(ntpath.normpath(anchor + text))
    if cwd is None:
        return Unresolvable("is relative to a folder this analysis cannot determine")
    return Path(ntpath.normpath(ntpath.join(str(cwd), text)))


def without_long_prefix(text: str) -> str:
    """*text* without the long-path prefix (backslash, backslash, ``?``, backslash).

    The ``?`` of the prefix is no wildcard.
    """
    if not WINDOWS or text[:4] not in ("\\\\?\\", "//?/"):
        return text
    return "\\\\" + text[8:] if text[4:8].lower() in ("unc\\", "unc/") else text[4:]


def has_glob(text: str, *, brace: bool = False) -> bool:
    characters = _BASH_GLOB_CHARACTERS if brace else GLOB_CHARACTERS
    return any(character in characters for character in text)


def glob_prefix(text: str, *, brace: bool = False) -> str | Unresolvable:
    """The literal folder a glob starts in (``src/*.py`` -> ``src``; ``*.txt`` -> ``.``)."""
    characters = _BASH_GLOB_CHARACTERS if brace else GLOB_CHARACTERS
    first = next(index for index, character in enumerate(text) if character in characters)
    if ".." in re.split(r"[\\/]", text[first:]):
        return Unresolvable("climbs out of a folder a wildcard names")
    prefix = text[:first]
    cut = max(prefix.rfind("/"), prefix.rfind("\\") if WINDOWS else -1)
    if cut < 0:
        drive = _DRIVE.match(prefix) if WINDOWS else None
        return prefix[:2] if drive is not None else "."
    return prefix[: cut + 1] if cut == 0 or (WINDOWS and prefix[cut - 1] == ":") else prefix[:cut]


def is_clearly_path(text: str, *, windows_switches: bool) -> bool:
    """Whether a word of a command without a path spec is clearly a path (tier B).

    Absolute, drive-letter, UNC and home paths, and relative paths that climb
    out with ``..``. URLs are not paths. ``windows_switches``: the word belongs
    to a Windows program, where ``/all`` is a switch, so a lone ``/word`` is no
    path.
    """
    if not text or "\n" in text or text.startswith("-") or is_url(text):
        return False
    if text == "~" or text.startswith(("~/", "~\\")):
        return True
    if WINDOWS and (_DRIVE.match(text) or text.startswith(("\\\\", "//", "\\"))):
        return True
    if text.startswith("/"):
        if not windows_switches:
            return True
        return bool(re.search(r"[\\/]", text[1:])) or text.startswith("//")
    return climbs_out(text)


def climbs_out(text: str) -> bool:
    """Whether a relative path leaves its starting folder through ``..``."""
    depth = 0
    for segment in re.split(r"[\\/]" if WINDOWS else r"/", text):
        if segment == "..":
            depth -= 1
            if depth < 0:
                return True
        elif segment not in ("", "."):
            depth += 1
    return False
