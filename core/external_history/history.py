"""Read-only source discovery shared by the product importer and reading Skill."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from core.external_history.databases import list_database, read_database
from core.external_history.errors import ExternalHistoryError
from core.external_history.files import MAX_SOURCE_BYTES, read_session
from core.external_history.records import SOURCES, ExternalSession
from core.utils.file_status import is_dir_strict, is_file_strict, is_link_status


def source_root(source: str, path: str | None) -> Path:
    if source not in SOURCES:
        raise ExternalHistoryError("Unknown source. Choose " + ", ".join(SOURCES) + ".")
    if path:
        return Path(path).expanduser().absolute()
    defaults = {
        "codex": ("CODEX_HOME", Path.home() / ".codex"),
        "claude-code": ("CLAUDE_CONFIG_DIR", Path.home() / ".claude"),
        "hermes": ("HERMES_HOME", Path.home() / ".hermes"),
        "openclaw": ("OPENCLAW_STATE_DIR", Path.home() / ".openclaw"),
        "opencode": (
            "OPENCODE_DB",
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode",
        ),
    }
    variable, default = defaults[source]
    return Path(os.environ.get(variable) or default).expanduser().absolute()


def _walk(root: Path, suffixes: set[str]) -> list[Path]:
    if is_file_strict(root):
        return [root] if root.suffix.lower() in suffixes else []
    if not is_dir_strict(root):
        raise ExternalHistoryError(
            f"Source not found: {root.as_posix()}. Set the source path to an existing data "
            f"directory or exported transcript."
        )
    found = []
    for current, directories, files in os.walk(root, onerror=_walk_error, followlinks=False):
        directories[:] = [
            name for name in directories if not is_link_status((Path(current) / name).lstat())
        ]
        for name in files:
            path = Path(current) / name
            if path.suffix.lower() in suffixes and not is_link_status(path.lstat()):
                found.append(path)
    return found


def _walk_error(error: OSError) -> None:
    raise error


def _existing_trees(root: Path, names: tuple[str, ...]) -> list[Path]:
    return [root / name for name in names if is_dir_strict(root / name)]


def _transcripts(source: str, root: Path) -> list[Path]:
    if is_file_strict(root):
        return [root]
    if not is_dir_strict(root):
        raise ExternalHistoryError(
            f"Source not found: {root.as_posix()}. Pass --path with its data directory or "
            f"an exported file."
        )
    trees = {
        "codex": ("sessions", "archived_sessions"),
        "claude-code": ("projects",),
        "openclaw": ("agents",),
        "hermes": (),
        "opencode": (),
    }[source]
    selected: list[Path] = []
    for tree in _existing_trees(root, trees):
        for file in _walk(tree, {".jsonl", ".sqlite"}):
            if (
                source != "openclaw"
                or file.name == "openclaw-agent.sqlite"
                or "sessions" in file.parts
                or "transcripts" in file.parts
            ):
                selected.append(file)
    # App homes contain plugins, credentials and caches. Never recursively scan
    # them as an untyped export directory. Explicit nested export folders work.
    for item in root.iterdir():
        if is_link_status(item.lstat()) or not is_file_strict(item):
            continue
        if (
            source == "hermes"
            and item.name == "state.db"
            or source == "opencode"
            and item.name.startswith("opencode")
            and item.suffix == ".db"
            or item.suffix in {".jsonl", ".json"}
            and item.name
            not in {
                "settings.json",
                "config.json",
                "auth.json",
                "history.jsonl",
                "sessions.json",
                "package.json",
                "package-lock.json",
            }
        ):
            selected.append(item)
    return sorted(set(selected), key=lambda p: (p.stat().st_mtime_ns, p.as_posix()), reverse=True)


def _memory_files(source: str, root: Path) -> list[Path]:
    if is_file_strict(root):
        if root.suffix.lower() != ".md":
            raise ExternalHistoryError("Choose a Markdown Memory file.")
        return [root]
    if not is_dir_strict(root):
        raise ExternalHistoryError(
            f"Source not found: {root.as_posix()}. Pass --path with an existing Memory "
            f"file or directory."
        )
    names = ("memory", "memories", "memories_v2", "agent-memory", "agent-memory-local")
    roots = _existing_trees(root, names)
    if root.name in names:
        roots.append(root)
    if source == "claude-code" and is_dir_strict(root / "projects"):
        for project in (root / "projects").iterdir():
            if not is_link_status(project.lstat()) and is_dir_strict(project):
                roots.extend(_existing_trees(project, names))
    if source == "openclaw":
        for workspace in root.glob("workspace*"):
            if not is_link_status(workspace.lstat()) and is_dir_strict(workspace):
                roots.extend(_existing_trees(workspace, names))
                roots.extend(
                    p for p in (workspace / "MEMORY.md", workspace / "USER.md") if is_file_strict(p)
                )
    files = [p for tree in roots for p in _walk(tree, {".md"})]
    # A selected folder can contain Memory documents directly. No traversal of
    # unrelated Skills, plugins or repository documentation.
    files.extend(
        p for p in root.glob("*.md") if not is_link_status(p.lstat()) and is_file_strict(p)
    )
    return sorted(set(files))


def _content_page(
    rows: list[dict[str, Any]], offset: int, limit: int, char_offset: int
) -> dict[str, Any]:
    if char_offset < 0:
        raise ExternalHistoryError("Use char_offset >= 0.")
    budget = 6000
    items: list[dict[str, Any]] = []
    next_offset = None
    next_char_offset = 0
    for index in range(offset, min(offset + limit, len(rows))):
        row = rows[index]
        start = char_offset if index == offset else 0
        content = row["content"]
        if start > len(content):
            raise ExternalHistoryError(
                "The character offset is past the end of this entry. Read it from offset 0."
            )
        # Reserve indentation too: newline-heavy source output still fits the CLI.
        end = start
        cost = 4
        while end < len(content) and cost + (5 if content[end] == "\n" else 1) <= budget:
            cost += 5 if content[end] == "\n" else 1
            end += 1
        chunk = content[start:end]
        items.append({**row, "content": chunk, "char_offset": start, "characters": len(content)})
        budget -= cost
        if start + len(chunk) < len(content):
            next_offset, next_char_offset = index, start + len(chunk)
            break
        next_offset = index + 1 if index + 1 < len(rows) else None
        if budget < 10:
            break
    return {
        "total": len(rows),
        "items": items,
        "next_offset": next_offset,
        "next_char_offset": next_char_offset,
    }


class ExternalHistory:
    """Stateless file readers. Every path belongs to the server's filesystem."""

    def sources(self) -> list[dict[str, str]]:
        return [
            {"id": key, "name": name, "path": source_root(key, None).as_posix()}
            for key, name in SOURCES.items()
        ]

    def list(
        self,
        source: str,
        *,
        path: str | None = None,
        offset: int = 0,
        limit: int = 50,
        query: str = "",
    ) -> dict[str, Any]:
        root = source_root(source, path)
        self._page(offset, limit)
        rows: list[dict[str, Any]] = []
        problems: list[str] = []
        try:
            for candidate in _transcripts(source, root):
                try:
                    if candidate.suffix in {".db", ".sqlite"}:
                        rows.extend(list_database(source, candidate))
                    else:
                        rows.append(read_session(source, candidate).summary())
                except (ExternalHistoryError, OSError, sqlite3.Error, ValueError) as exc:
                    problems.append(f"{candidate.as_posix()}: {exc}")
        except OSError as exc:
            raise ExternalHistoryError(
                f"Cannot read source {root.as_posix()}: {exc.strerror}."
            ) from exc
        if query:
            needle = query.casefold()
            rows = [
                row
                for row in rows
                if needle
                in (str(row["title"]) + " " + str(row["cwd"]) + " " + str(row["id"])).casefold()
            ]
        rows.sort(key=lambda row: (row["updated_at"], row["id"], row["path"]), reverse=True)
        return {
            "source": source,
            "path": root.as_posix(),
            "total": len(rows),
            "items": rows[offset : offset + limit],
            "next_offset": offset + limit if len(rows) > offset + limit else None,
            "warnings": problems,
        }

    def read(self, source: str, session_id: str, *, path: str | None = None) -> ExternalSession:
        root = source_root(source, path)
        try:
            if is_file_strict(root):
                result = (
                    read_database(source, root, session_id)
                    if root.suffix in {".db", ".sqlite"}
                    else read_session(source, root)
                )
                if result.id != session_id:
                    raise ExternalHistoryError(
                        f"The selected file contains Session {result.id!r}, not "
                        f"{session_id!r}. Nothing was imported."
                    )
                return result
            matches: list[dict[str, Any]] = []
            for candidate in _transcripts(source, root):
                if candidate.suffix in {".db", ".sqlite"}:
                    matches.extend(
                        row for row in list_database(source, candidate) if row["id"] == session_id
                    )
                else:
                    candidate_session = read_session(source, candidate)
                    if candidate_session.id == session_id:
                        matches.append(candidate_session.summary())
            if len(matches) != 1:
                raise ExternalHistoryError(
                    f"Session {session_id!r} has {len(matches)} matches. List sessions and "
                    f"pass the exact source file path."
                )
            return self.read(source, session_id, path=matches[0]["path"])
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ExternalHistoryError(
                f"Cannot read the selected {SOURCES[source]} Session: {exc}. Nothing was imported."
            ) from exc

    def show(
        self,
        source: str,
        session_id: str,
        *,
        path: str | None = None,
        offset: int = 0,
        limit: int = 30,
        query: str = "",
        char_offset: int = 0,
    ) -> dict[str, Any]:
        self._page(offset, limit)
        session = self.read(source, session_id, path=path)
        entries: list[dict[str, Any]] = [
            {
                "ordinal": i,
                "role": e.role,
                "kind": e.kind,
                "content": e.content,
                "timestamp": e.timestamp,
            }
            for i, e in enumerate(session.entries)
        ]
        if query:
            entries = [
                entry for entry in entries if query.casefold() in entry["content"].casefold()
            ]
        return {**session.summary(), **_content_page(entries, offset, limit, char_offset)}

    def read_memory(self, source: str, path: str) -> dict[str, Any]:
        item = source_root(source, path)
        try:
            if not is_file_strict(item) or item.suffix.lower() != ".md":
                raise ExternalHistoryError(
                    "Choose one Markdown Memory file from external.memories."
                )
            if item.stat().st_size > MAX_SOURCE_BYTES:
                raise ExternalHistoryError(f"Memory file exceeds 256 MiB: {item.as_posix()}.")
            content = item.read_text(encoding="utf-8-sig")
            return {
                "path": item.as_posix(),
                "name": item.name,
                "content": content,
                "characters": len(content),
                "scope": "user" if item.name.upper() == "USER.MD" else "agent",
                "digest": hashlib.sha256(content.encode()).hexdigest(),
            }
        except (OSError, UnicodeError) as exc:
            raise ExternalHistoryError(f"Cannot read Memory at {item.as_posix()}: {exc}.") from exc

    def memories(
        self,
        source: str,
        *,
        path: str | None = None,
        offset: int = 0,
        limit: int = 50,
        char_offset: int = 0,
    ) -> dict[str, Any]:
        self._page(offset, limit)
        root = source_root(source, path)
        try:
            rows = [
                self.read_memory(source, item.as_posix()) for item in _memory_files(source, root)
            ]
            return {
                "source": source,
                "path": root.as_posix(),
                **_content_page(rows, offset, limit, char_offset),
            }
        except OSError as exc:
            raise ExternalHistoryError(f"Cannot read Memory at {root.as_posix()}: {exc}.") from exc

    @staticmethod
    def _page(offset: int, limit: int) -> None:
        if offset < 0 or not 1 <= limit <= 100:
            raise ExternalHistoryError("Use offset >= 0 and limit between 1 and 100.")


def import_metadata(session: ExternalSession) -> dict[str, Any]:
    digest = hashlib.sha256(
        json.dumps(
            [
                {key: value for key, value in entry.to_dict().items() if key != "timestamp"}
                for entry in session.entries
            ],
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "source": session.source,
        "session_id": session.id,
        "path": session.path,
        "cwd": session.cwd,
        "digest": digest,
    }


def memory_entries(content: str) -> list[str]:
    """Offer paragraphs for review; preserve their wording instead of summarizing."""
    entries = []
    for paragraph in re.split(r"\n\s*\n|\n(?=\s*[-*] )|\n§\n", content.strip()):
        value = paragraph.strip()
        if value.startswith(("- ", "* ")):
            value = value[2:]
        if value:
            entries.append(value)
    return entries
