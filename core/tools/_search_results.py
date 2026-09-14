"""Logical search pagination and readable, bounded source evidence."""

from __future__ import annotations

import base64
import contextlib
import json
from collections import deque
from collections.abc import Generator
from pathlib import Path
from typing import Any

from core.tools._search_options import SearchOptions
from core.tools.search import MAX_OUTPUT_BYTES, display_search_path


def decode_bytes(value: dict[str, str]) -> bytes:
    return value["text"].encode("utf-8") if "text" in value else base64.b64decode(value["bytes"])


def path_label(path: Path, cwd: Path) -> str:
    value = display_search_path(path, cwd=cwd)
    return json.dumps(value, ensure_ascii=False) if any(c in value for c in "\n\r\t") else value


def excerpt(raw: bytes, position: int = 0) -> str:
    if len(raw) <= 4096:
        return raw.decode("utf-8", errors="backslashreplace").rstrip("\r\n\x00")
    start = max(position - 1024, 0)
    end = min(start + 3072, len(raw))
    prefix = f"[... {start} source bytes omitted ...] " if start else ""
    suffix = f" [... {len(raw) - end} source bytes omitted ...]" if end < len(raw) else ""
    return (
        prefix
        + raw[start:end].decode("utf-8", errors="backslashreplace").rstrip("\r\n\x00")
        + suffix
    )


class ResultPage:
    def __init__(self, offset: int, limit: int):
        self.offset, self.limit = offset, limit
        self.observed = 0
        self.returned = 0
        self.lines: list[str] = []
        self.bytes = 0
        self.more = False
        self.byte_limited = False
        self.matched = False
        self.binary_files = 0

    def add(self, lines: list[str]) -> bool:
        self.observed += 1
        if self.observed <= self.offset:
            return False
        if self.returned >= self.limit:
            self.more = True
            return False
        size = sum(len(line.encode("utf-8")) + 1 for line in lines)
        # Context may fill the byte budget, but must never hide the match or
        # return a continuation that makes no progress at the same offset.
        while len(lines) > 1 and self.bytes + size > MAX_OUTPUT_BYTES - 2048:
            size -= len(lines.pop(0).encode("utf-8")) + 1
            self.byte_limited = True
        if self.bytes + size > MAX_OUTPUT_BYTES - 2048:
            if self.returned == 0:
                raise RuntimeError("One result path exceeds the output byte budget.")
            self.more = self.byte_limited = True
            return False
        self.lines.extend(lines)
        self.bytes += size
        self.returned += 1
        return True

    def context(self, lines: list[str]) -> None:
        for line in lines:
            size = len(line.encode("utf-8")) + 1
            if self.bytes + size > MAX_OUTPUT_BYTES - 2048:
                self.byte_limited = True
                return
            self.lines.append(line)
            self.bytes += size

    def data(self, *, complete: bool, warnings: list[str], quiet: bool = False) -> dict[str, Any]:
        content = "\n".join(self.lines)
        data: dict[str, Any] = {"content": content, "complete": complete}
        if quiet:
            data["matched"] = True if self.matched else False if complete else None
            data["content"] = (
                "Match found."
                if self.matched
                else "No matches."
                if complete
                else "Search incomplete; no match has been established."
            )
        elif not content:
            data["content"] = (
                f"No results at offset {self.offset}; observed {self.observed} results."
                if self.offset and self.observed
                else "No results."
                if complete
                else "No results established; search incomplete."
            )
        if self.more:
            data["next_offset"] = self.offset + self.returned
            data["continuation"] = (
                "Repeat the same search with next_offset as offset. This repeats a"
                " live query; changes to files can change page boundaries."
            )
        if self.byte_limited:
            data["content"] += "\n[Output byte limit reached; some context may be omitted.]"
        if self.binary_files:
            warnings = [
                *warnings,
                f"Skipped {self.binary_files} binary files; use --text to search their contents.",
            ]
        if warnings:
            data["warnings"] = [
                w.encode("utf-8")[:512].decode("utf-8", errors="ignore") for w in warnings[:12]
            ]
        return data


def render_events(
    events: Generator[dict[str, Any], None, None],
    page: ResultPage,
    options: SearchOptions,
    cwd: Path,
) -> None:
    before, after = options.context
    previous: deque[tuple[int, str]] = deque(maxlen=before)
    selected_until = -1
    emitted: set[int] = set()
    output = options.get("output")
    path = Path()
    snapshot = (0, 0, 0, 0, False, False)
    any_match = False
    with contextlib.closing(events):
        for event in events:
            kind, data = event["type"], event["data"]
            if kind == "row":
                count = data["count"]
                page.matched = page.matched or (output != "without" and count != 0)
                label = path_label(Path(data["path"]), cwd)
                if not options.enabled("quiet"):
                    page.add([label if count is None else f"{label}:{count}"])
                if page.more or (page.matched and options.enabled("quiet")):
                    return
            elif kind == "begin":
                raw_path = decode_bytes(data["path"])
                path = Path(raw_path.decode("utf-8"))
                previous.clear()
                emitted.clear()
                selected_until = -1
                any_match = False
                snapshot = (
                    page.observed,
                    page.returned,
                    len(page.lines),
                    page.bytes,
                    page.more,
                    page.byte_limited,
                )
            elif kind in {"match", "context"}:
                number = data.get("line_number") or 1
                raw = decode_bytes(data["lines"])
                submatches = data.get("submatches", [])
                coordinate = path_label(path, cwd)
                source = excerpt(raw, submatches[0]["start"] if submatches else 0)
                text = f"{coordinate}:{number}{':' if kind == 'match' else '-'}{source}"
                if kind == "match":
                    any_match = True
                    if not output and not options.enabled("quiet"):
                        if options.enabled("only"):
                            for match in submatches:
                                start = match["start"]
                                delimiter = b"\0" if options.enabled("null_data") else b"\n"
                                line = number + raw[:start].count(delimiter)
                                column = start - raw.rfind(delimiter, 0, start)
                                page.add(
                                    [
                                        f"{coordinate}:{line}:{column}:{excerpt(decode_bytes(match['match']))}"
                                    ]
                                )
                        else:
                            context = [
                                line
                                for n, line in previous
                                if n >= number - before and n not in emitted
                            ]
                            if page.add([*context, text]):
                                emitted.update(n for n, _ in previous)
                                emitted.add(number)
                                selected_until = (
                                    number + raw.count(b"\n") - int(raw.endswith(b"\n")) + after
                                )
                    previous.append((number, text))
                else:
                    if number <= selected_until and number not in emitted:
                        page.context([text])
                        emitted.add(number)
                    previous.append((number, text))
            elif kind == "end":
                binary = data.get("binary_offset") is not None
                if binary and options.get("binary", "skip") == "skip":
                    (
                        page.observed,
                        page.returned,
                        length,
                        page.bytes,
                        page.more,
                        page.byte_limited,
                    ) = snapshot
                    del page.lines[length:]
                    page.binary_files += 1
                else:
                    page.matched = page.matched or any_match
                    stats = data["stats"]
                    if output in {"files", "without"} and (any_match == (output == "files")):
                        page.add([path_label(path, cwd)])
                    if output in {"lines", "counts"}:
                        count = stats["matched_lines" if output == "lines" else "matches"]
                        if count or options.enabled("zero"):
                            page.add([f"{path_label(path, cwd)}:{count}"])
                if page.more or (page.matched and options.enabled("quiet")):
                    return
