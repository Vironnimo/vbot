"""Terminal file references follow the text submitted to the server."""

from __future__ import annotations

from typing import Any, cast

import pytest

from cli.tui.mentions import extract_mentions, format_mention, open_quoted_mention, resolve_mentions
from cli.tui.state import Conversation


@pytest.mark.parametrize(
    "path",
    ["src/Übersicht.md", "notes/my file.md", 'notes/a"b.md', "notes/a@b.md", "notes/a\\b.txt"],
)
def test_mention_tokens_round_trip_listed_names(path: str) -> None:
    assert extract_mentions("Read " + format_mention(path)) == [path]


@pytest.mark.asyncio
async def test_submitted_tokens_resolve_index_ignored_files_and_quoted_paths() -> None:
    calls: list[dict[str, Any]] = []

    class Listings:
        async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(params)
            if "directory" not in params:
                return {"files": ["new.py", "docs/Übersicht.md", 'notes/a"b.md']}
            return {"entries": [{"name": "ignored.txt", "kind": "file", "ignored": True}]}

    text = (
        'user@example.com @new.py. @docs\\Übersicht.md @"notes/a\\"b.md" '
        '@secret/ignored.txt @../outside.txt @"C:/outside.txt" @nobody'
    )
    assert await resolve_mentions(
        cast(Any, Listings()), {"agent_id": "main", "session_id": "s"}, text
    ) == ["new.py", "docs/Übersicht.md", 'notes/a"b.md', "secret/ignored.txt"]
    assert all(
        ".." not in call.get("directory", "") and ":" not in call.get("directory", "")
        for call in calls
    )
    view = Conversation("main", mentions=["old.py"])
    assert "file_mentions" not in view.submission(text)
    assert extract_mentions("No files remain") == []
    assert open_quoted_mention('Read @"notes/my file') == (5, "notes/my file")
    assert open_quoted_mention('Read @"notes/my file"') is None
