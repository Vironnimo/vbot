"""The terminal equivalent of WebUI file-mention token and listing resolution."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import TYPE_CHECKING, Any

from cli.tui.transport import ClientError

if TYPE_CHECKING:
    from cli.tui.transport import TerminalClient


def _bare(character: str) -> bool:
    return character in "_./\\-" or unicodedata.category(character)[0] in "LMN"


def format_mention(path: str) -> str:
    if path and all(_bare(character) for character in path):
        return "@" + path
    return '@"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unescape(text: str) -> str:
    return re.sub(r'\\(["\\])', r"\1", text)


def open_quoted_mention(text: str) -> tuple[int, str] | None:
    start = text.rfind('@"')
    if start < 0 or (start and (_bare(text[start - 1]) or text[start - 1] == "@")):
        return None
    inner = text[start + 2 :]
    index = 0
    while index < len(inner):
        if inner[index] == "\\":
            index += 2
            continue
        if inner[index] in {'"', "\n"}:
            return None
        index += 1
    return start, _unescape(inner)


def extract_mentions(text: str) -> list[str]:
    result = []
    index = 0
    while index < len(text):
        if text[index] != "@" or (index and (_bare(text[index - 1]) or text[index - 1] == "@")):
            index += 1
            continue
        start = index + 1
        if start < len(text) and text[start] == '"':
            end = start + 1
            while end < len(text):
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == '"':
                    break
                end += 1
            if end >= len(text):
                index += 1
                continue
            token = _unescape(text[start + 1 : end])
            index = end + 1
        else:
            end = start
            while end < len(text) and _bare(text[end]):
                end += 1
            token = text[start:end]
            index = max(end, start)
        if token and token not in result:
            result.append(token)
    return result


def _variants(token: str) -> list[str]:
    normalized = token.replace("\\", "/")
    return list(dict.fromkeys(filter(None, (token, normalized, normalized.rstrip(".,;:!?)")))))


def _outside(path: str) -> bool:
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:", path)) or ".." in path.split("/")


def retained_mentions(text: str, selected: list[str]) -> list[str]:
    candidates = {path for token in extract_mentions(text) for path in _variants(token)}
    return [path for path in selected if path in candidates]


async def resolve_mentions(client: TerminalClient, params: dict[str, Any], text: str) -> list[str]:
    """Only tokens confirmed by the server's index/direct entries become snapshots."""
    tokens = extract_mentions(text)
    if not tokens:
        return []
    listing = await client.rpc("files.list", params)
    files = set(listing.get("files", []))
    resolved = {}
    unresolved = []
    for token in tokens:
        match = next((path for path in _variants(token) if path in files), None)
        if match:
            resolved[token] = match
        else:
            unresolved.append(token)
    directories: dict[str, set[str]] = {}
    for token in unresolved:
        for path in _variants(token):
            directory = path.rpartition("/")[0]
            if not _outside(path) and directory not in directories and len(directories) < 20:
                directories[directory] = set()

    async def entries(directory: str) -> None:
        try:
            response = await client.rpc("files.list", {**params, "directory": directory})
        except ClientError:
            return
        directories[directory] = {
            entry["name"] for entry in response.get("entries", []) if entry.get("kind") == "file"
        }

    await asyncio.gather(*(entries(directory) for directory in directories))
    for token in unresolved:
        for path in _variants(token):
            directory, _, name = path.rpartition("/")
            if not _outside(path) and name in directories.get(directory, set()):
                resolved[token] = path
                break
    return list(dict.fromkeys(resolved[token] for token in tokens if token in resolved))
