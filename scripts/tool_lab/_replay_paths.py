"""Resolve Session file identities and rewrite paths into the replay workspace."""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.tools._patch_syntax import _HEADER, _MOVE_TO, _clean_path
from core.tools._read_arguments import normalize_read_arguments
from core.tools.apply_patch import patch_targets
from scripts.tool_lab._replay_evidence import HistoryCall, result_labels

# Stands for the replay root in rewritten absolute paths; ``run`` substitutes it.
ROOT_TOKEN = "{replay-root}"
_FILE_TOOLS = ("read", "apply_patch", "edit", "write")
_DRIVE = re.compile(r"[A-Za-z]:/")


def _normal(name: str) -> str | None:
    """A path spelling with ``/`` separators and no ``.``/``..`` steps, or None if unusable."""
    text = name.strip().replace("\\", "/")
    if not text or "\x00" in text or text.startswith(("~", "//")):
        return None
    absolute = bool(_DRIVE.match(text)) or text.startswith("/")
    text = posixpath.normpath(text)
    if text in {".", "/"} or _DRIVE.fullmatch(text + "/"):
        return None
    if not absolute and (text == ".." or text.startswith("../") or ":" in text):
        return None
    return text


def _is_absolute(normal: str) -> bool:
    return bool(_DRIVE.match(normal)) or normal.startswith("/")


@dataclass(frozen=True, slots=True)
class SessionPaths:
    """Path keys and replay locations of one Session's file names."""

    cwd: str | None

    def key(self, name: str) -> str | None:
        normal = _normal(name)
        if normal is None:
            return None
        if _is_absolute(normal):
            return normal.lower()
        return f"{self.cwd}/{normal.lower()}" if self.cwd else f"./{normal.lower()}"

    def workspace(self, name: str) -> str | None:
        """Where the replay keeps the file, relative to its root (``cwd/...`` or ``abs/...``)."""
        normal = _normal(name)
        if normal is None:
            return None
        if not _is_absolute(normal):
            return f"cwd/{normal}"
        if self.cwd and normal.lower().startswith(self.cwd + "/"):
            return f"cwd/{normal[len(self.cwd) + 1 :]}"
        if _DRIVE.match(normal):
            return f"abs/{normal[0].lower()}/{normal[3:]}"
        return f"abs/_{normal}"

    def label_resolver(self, keys: dict[str, str | None]) -> Callable[[str], str | None]:
        values = {key for key in keys.values() if key is not None}

        def resolve(label: str) -> str | None:
            key = self.key(label)
            if key in values:
                return key
            normal = _normal(label)
            if normal is None:
                return None
            lower = normal.lower()
            matches = {
                value
                for value in values
                if value.endswith("/" + lower) or lower.endswith("/" + value.removeprefix("./"))
            }
            return matches.pop() if len(matches) == 1 else None

        return resolve

    def replacements(
        self, keys: dict[str, str | None], workspace: dict[str, str]
    ) -> dict[str, str]:
        replacements: dict[str, str] = {}
        for name, key in keys.items():
            normal = _normal(name)
            if key is None or normal is None or not _is_absolute(normal):
                continue
            location = workspace[key]
            if "\\" in name and "/" not in name:
                replacements[name] = ROOT_TOKEN + "\\" + location.replace("/", "\\")
            else:
                replacements[name] = f"{ROOT_TOKEN}/{location}"
        return replacements

    def rewritten(self, arguments: dict[str, Any], expected: set[str]) -> bool:
        """Whether every file the rewritten call names is one of the replay's files."""
        found = set()
        for name in call_paths("apply_patch", arguments):
            if name.startswith(ROOT_TOKEN):
                found.add(name[len(ROOT_TOKEN) + 1 :].replace("\\", "/"))
            else:
                normal = _normal(name)
                if normal is None or _is_absolute(normal):
                    return False
                found.add(f"cwd/{normal}")
        return {path.lower() for path in found} == {path.lower() for path in expected}


def call_paths(name: str, arguments: Any) -> list[str]:
    """The file names a read or file-edit call gives, as spelled."""
    if name == "read":
        try:
            normalized = normalize_read_arguments(arguments)
        except Exception:
            return []
        path = normalized.get("path") if isinstance(normalized, dict) else None
        return [path] if isinstance(path, str) else []
    try:
        return patch_targets(arguments)
    except Exception:
        return _header_paths(arguments)


def _header_paths(value: Any) -> list[str]:
    """File names in V4A header lines, for patches the parser refuses."""
    if isinstance(value, dict):
        return [path for item in value.values() for path in _header_paths(item)]
    if isinstance(value, list):
        return [path for item in value for path in _header_paths(item)]
    if not isinstance(value, str):
        return []
    paths: list[str] = []
    for line in value.split("\n"):
        line = line.rstrip("\r")
        if header := _HEADER.fullmatch(line):
            source, _arrow, destination = _clean_path(header[2]).partition(" -> ")
            paths.extend(_clean_path(part) for part in (source, destination) if part.strip())
        elif move := _MOVE_TO.fullmatch(line):
            paths.append(_clean_path(move[1]))
    return [path for path in paths if path]


def infer_cwd(calls: list[HistoryCall]) -> str | None:
    """The Session's working directory, when absolute and relative spellings reveal it.

    Strong evidence: a result labels a file relative to the working directory
    that the call named absolutely, or the reverse. Otherwise at least two
    relative names with a folder must each end an absolute name the same way.
    """
    strong: Counter[str] = Counter()
    absolute: set[str] = set()
    relative: set[str] = set()
    for call in calls:
        if call.name not in _FILE_TOOLS:
            continue
        names = [n for n in map(_normal, call_paths(call.name, call.arguments())) if n]
        for normal in names:
            (absolute if _is_absolute(normal) else relative).add(normal.lower())
        if call.name != "apply_patch":
            continue
        for label in filter(None, map(_normal, result_labels(call.envelope()))):
            for normal in names:
                first, second = normal.lower(), label.lower()
                if _is_absolute(second) and not _is_absolute(first):
                    first, second = second, first
                mixed = _is_absolute(first) and not _is_absolute(second)
                if mixed and first.endswith("/" + second):
                    strong[first[: -len(second) - 1]] += 1
    if strong:
        return strong.most_common(1)[0][0]
    weak: Counter[str] = Counter()
    for name in relative:
        if "/" not in name:
            continue
        for full in absolute:
            if full.endswith("/" + name):
                weak[full[: -len(name) - 1]] += 1
    if weak:
        best, votes = weak.most_common(1)[0]
        if votes >= 2:
            return best
    return None


def rewrite_paths(value: Any, replacements: dict[str, str]) -> Any:
    """Replace file names in path fields and patch header lines; content stays literal."""
    if not replacements:
        return value
    if isinstance(value, dict):
        return {key: rewrite_paths(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(item, replacements) for item in value]
    if not isinstance(value, str):
        return value
    if value.strip() in replacements:
        return value.replace(value.strip(), replacements[value.strip()])
    lines = value.split("\n")
    changed = False
    ordered = sorted(replacements.items(), key=lambda item: -len(item[0]))
    for index, line in enumerate(lines):
        bare = line.rstrip("\r")
        if not (
            _HEADER.fullmatch(bare)
            or _MOVE_TO.fullmatch(bare)
            or bare.startswith(("--- ", "+++ ", "diff --git ", "rename from ", "rename to "))
        ):
            continue
        new = bare
        for old, replacement in ordered:
            pattern = rf"(?<![^\s:'\"`>]){re.escape(old)}(?=$|[\s'\"`]|\s*->)"
            new = re.sub(pattern, _literal(replacement), new)
        if new != bare:
            lines[index] = new + line[len(bare) :]
            changed = True
    return "\n".join(lines) if changed else value


def _literal(text: str) -> Callable[[re.Match[str]], str]:
    """A ``re.sub`` replacement that inserts ``text`` as it is, backslashes included."""
    return lambda _match: text
