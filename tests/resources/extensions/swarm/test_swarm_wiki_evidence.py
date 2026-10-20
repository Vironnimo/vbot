"""Wiki copy recovery and evidence from the exact revision a mutation saved."""

import pytest

from tests.resources.extensions.swarm.swarm_test_support import (
    continuation,
    create,
    invoke,
    stored,
)

_PROSE = (
    "| A7 | The saved document must contain the complete previous or next version, "
    "never a partial document after a failed write. | Storage test |"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["**", "*", "__", "_"])
async def test_wiki_emphasis_copy_replaces_the_complete_intended_row(board, marker):
    current = _PROSE.replace(
        "never a partial document", f"{marker}never a partial document{marker}"
    )
    old = _PROSE.replace("never", f"{marker}never{marker}")
    replacement = "| A7 | Inject a deterministic write failure and verify complete bytes. |"
    page_id = await create(board, f"Before\n{current}\nAfter")

    result = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": old, "new_text": replacement},
    )

    assert result["ok"], result
    assert (await stored(board, page_id))["content"] == f"Before\n{replacement}\nAfter"
    assert "Markdown emphasis" in result["data"]["note"]
    assert current in result["data"]["note"]
    assert result["data"]["status"] == "Replaced line 2."
    assert result["data"]["content"] == f"Before\n{replacement}\nAfter"


@pytest.mark.asyncio
async def test_wiki_emphasis_copy_preserves_formatting_outside_the_requested_edit(board):
    current = (
        _PROSE.replace("never a partial document", "**never a partial document**")
        + " `write_file()`"
    )
    old = _PROSE.replace("never", "**never**") + " `write_file()`"
    page_id = await create(board, f"Before\n{current}\nAfter")
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "old_text": old,
            "new_text": old.replace("saved document", "exported document"),
        },
    )
    assert result["ok"], result
    assert (await stored(board, page_id))["content"] == (
        "Before\n" + current.replace("saved document", "exported document") + "\nAfter"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "old"),
    [
        ("**Read file** now", "Read **file** now"),
        (_PROSE.replace("A7", "B8"), _PROSE),
        (_PROSE.replace("never", "`**never**`"), _PROSE.replace("never", "`never`")),
        # An unclosed backtick string stays literal, in linear time.
        (_PROSE.replace("never", "**never**") + " " + "`" * 5000, _PROSE + " " + "`" * 5000),
        (
            _PROSE + " [details](https://example.test/**path**)",
            _PROSE + " [details](https://example.test/path)",
        ),
        ("```text\n" + _PROSE.replace("never", "**never**") + "\n```", _PROSE),
        (
            "```text\n```still-code\n" + _PROSE.replace("never", "**never**") + "\n```",
            _PROSE,
        ),
        (
            "- ```text\n  " + _PROSE.replace("never", "**never**") + "\n  ```",
            "  " + _PROSE,
        ),
        (
            "1. ~~~text\n   " + _PROSE.replace("never", "**never**") + "\n   ~~~",
            "   " + _PROSE,
        ),
        ("    " + _PROSE.replace("never", "**never**"), "    " + _PROSE),
    ],
)
async def test_wiki_emphasis_copy_does_not_guess_short_changed_or_protected_text(
    board, current, old
):
    page_id = await create(board, current)
    result = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": old, "new_text": "Changed"},
    )
    assert not result["ok"], result
    saved = await stored(board, page_id)
    assert saved["content"] == current
    assert saved["revision"] == 1


@pytest.mark.asyncio
async def test_wiki_emphasis_copy_resumes_after_a_valid_fence_closer(board):
    current = _PROSE.replace("never", "**never**")
    page_id = await create(board, "```text\nLiteral code\n```   \n" + current)
    result = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": _PROSE, "new_text": "Changed"},
    )
    assert result["ok"], result
    assert (await stored(board, page_id))["content"].endswith("\nChanged")


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["-  ", ">  ", "1.  "])
async def test_wiki_emphasis_copy_after_a_long_non_fence_container_line(board, container):
    prefix = container * 2000 + "not a fence\n"
    current = _PROSE.replace("never", "**never**")
    page_id = await create(board, prefix + current)
    result = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": _PROSE, "new_text": "Changed"},
    )
    assert result["ok"], result
    saved = await stored(board, page_id)
    assert saved["content"] == prefix + "Changed"
    assert saved["revision"] == 2
    assert result["data"]["content"].endswith("not a fence\nChanged")


@pytest.mark.asyncio
async def test_wiki_emphasis_copy_bounds_success_and_failure_evidence(board):
    prefix = " ".join(f"word{number}" for number in range(400)) + " "
    old = prefix + _PROSE
    current = prefix + _PROSE.replace("never", "**never**")
    page_id = await create(board, current)
    result = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": old, "new_text": "Changed"},
    )
    assert result["ok"], result
    assert "Earlier content (excerpt):" in result["data"]["note"]
    assert "**never**" in result["data"]["note"]
    assert len(result["data"]["note"]) < 450
    assert (await stored(board, page_id))["content"] == "Changed"

    for content in ("```text\n" + current + "\n```", current + "\n" + current):
        page_id = await create(board, content)
        result = await invoke(
            board,
            {"action": "update", "page_id": page_id, "old_text": old, "new_text": "Changed"},
        )
        assert not result["ok"], result
        assert "[passage shortened]" in result["error"]["message"]
        assert len(result["error"]["message"]) < 1000
        assert (await stored(board, page_id))["content"] == content
        assert (await stored(board, page_id))["revision"] == 1


@pytest.mark.asyncio
async def test_wiki_emphasis_copy_rejects_two_visible_matches_without_changes(board):
    first = _PROSE.replace("never", "**never**")
    second = _PROSE.replace("never a partial document", "**never a partial document**")
    old = _PROSE.replace("a partial", "**a partial**")
    page_id = await create(board, first + "\n" + second)
    result = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": old, "new_text": "Changed"},
    )
    assert result["error"]["code"] == "wiki_edit_conflict"
    assert "resembles 2 passages" in result["error"]["message"]
    assert (await stored(board, page_id))["content"] == first + "\n" + second
    assert (await stored(board, page_id))["revision"] == 1


@pytest.mark.asyncio
async def test_wiki_emphasis_copy_without_a_prose_change_keeps_the_existing_revision(board):
    current = _PROSE.replace("never a partial document", "**never a partial document**")
    old = _PROSE.replace("never", "**never**")
    page_id = await create(board, current)
    result = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": old, "new_text": old},
    )
    assert result["ok"], result
    assert result["data"]["revision"] == 1
    assert result["data"]["status"] == (
        "Nothing changed: the page already holds this change, at line 1."
    )
    assert (await stored(board, page_id))["content"] == current


@pytest.mark.asyncio
async def test_wiki_seam_is_bounded_revision_pinned_and_replayable(board):
    rows = [f"Row {i:02d}: " + "x" * 200 for i in range(60)]
    page_id = await create(board, "\n".join(rows))
    edit = {
        "action": "update",
        "page_id": page_id,
        "edits": [
            {"old_text": f"Row {i:02d}:", "new_text": f"Revised row {i:02d}:"}
            for i in range(5, 60, 6)
        ],
    }
    result = await invoke(board, edit, tool_call_id="seam-edit")
    data = result["data"]
    assert data["revision"] == 2
    # Each edit shows the line before, its line and two after; lines between are named.
    shown = data["content"].split("\n")
    assert shown[:6] == [
        rows[4],
        rows[5].replace("Row", "Revised row"),
        rows[6],
        rows[7],
        "[lines 9–10 unchanged]",
        rows[10],
    ]
    assert len(data["content"]) <= 4100
    assert shown[-1].startswith("[lines ") and shown[-1].endswith("–60 not shown]")
    next_call = continuation(data["more"])
    assert next_call["revision"] == 2
    await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "content": "Peer revision",
            "expected_revision": 2,
        },
        peer=1,
    )
    more = await invoke(board, next_call)
    saved = "\n".join(rows)
    for item in edit["edits"]:
        saved = saved.replace(item["old_text"], item["new_text"])
    assert more["data"]["content"] == saved[next_call["offset"] :]
    first_cut = int(shown[-1].removeprefix("[lines ").split("–")[0])
    assert more["data"]["content"].startswith(saved.split("\n")[first_cut - 1])
    replay = await invoke(board, edit, tool_call_id="seam-edit")
    assert (replay["data"]["content"], replay["data"]["revision"]) == (data["content"], 2)
    assert (await stored(board, page_id))["content"] == "Peer revision"


@pytest.mark.asyncio
async def test_wiki_seam_shows_the_changed_passage_edges_and_the_lines_after_it(board):
    old_rows = [f"| {i} | old |" for i in range(10)]
    new_rows = [f"| {i} | new |" for i in range(10)]
    content = "\n".join(["# Table", *old_rows, "Closing line one", "Closing line two"])
    page_id = await create(board, content)
    # The edit replaces eight rows with ten, leaving the last two old rows below the seam.
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "old_text": "\n".join(old_rows[:8]),
            "new_text": "\n".join(new_rows),
        },
    )
    data = result["data"]
    assert data["status"] == "Replaced lines 2–9 with lines 2–11."
    assert data["shown"] == "The saved page around the change, lines 1–13 of 15:"
    assert data["content"] == "\n".join(
        ["# Table", new_rows[0], "[lines 3–10 as in your new_text]", new_rows[9], *old_rows[8:]]
    )


@pytest.mark.asyncio
async def test_wiki_seam_shows_lines_the_page_holds_other_than_sent(board):
    page_id = await create(board, "\n".join(f"Line {n}" for n in range(1, 11)))
    # The kept line 4 was copied with an error; the page keeps its own wording.
    result = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "old_text": "Line 3\nLine 4 typo\nLine 5\nLine 6\nLine 7",
            "new_text": "Line 3!\nLine 4 typo\nLine 5\nLine 6\nLine 7!",
        },
    )
    data = result["data"]
    assert data["status"] == "Replaced lines 3–7."
    assert data["content"] == (
        "Line 2\nLine 3!\nLine 4\n[lines 5–6 as in your new_text]\nLine 7!\nLine 8\nLine 9"
    )


@pytest.mark.asyncio
async def test_wiki_deletion_and_whole_page_replacement_report_what_was_saved(board):
    page_id = await create(board, "Before\nRemove this\nAfter")
    removed = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": "Remove this\n", "new_text": ""},
    )
    assert removed["data"]["status"] == "Removed line 2."
    # The seam starts one line before the removed lines.
    assert removed["data"]["content"] == "Before\nAfter"
    replaced = await invoke(
        board,
        {
            "action": "update",
            "page_id": page_id,
            "content": "A\nB\nC\nD\nE",
            "expected_revision": 2,
        },
    )
    assert replaced["data"]["status"] == (
        "Replaced the whole content. Saved as sent: 5 lines, 9 characters."
    )
    assert "content" not in replaced["data"] and "more" not in replaced["data"]
    shortened = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": "B\nC\nD", "new_text": "B\nD"},
    )
    assert shortened["data"]["status"] == "Replaced lines 2–4 with lines 2–3."
    assert shortened["data"]["content"] == "B\nD\nE"
    emptied = await invoke(
        board,
        {"action": "update", "page_id": page_id, "content": "", "expected_revision": 4},
    )
    assert emptied["data"]["status"] == "Replaced the whole content. The page is now empty."
    assert "content" not in emptied["data"]
    assert (await stored(board, page_id))["content"] == ""
    assert (await stored(board, page_id))["revision"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["LF", "CRLF", "CR"])
async def test_wiki_seam_shows_a_change_late_in_a_long_line_and_end_deletion(board, newline):
    first_line = "First line, the line before the change."
    prefix, suffix = "Before " * 500, " After" * 500
    page_id = await create(board, first_line + newline + prefix + "OLD VALUE" + suffix)
    changed = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": "OLD VALUE", "new_text": "NEW VALUE"},
    )
    data = changed["data"]
    assert data["status"] == "Replaced line 2."
    # A long line keeps 150 characters on each side of the changed text.
    line = prefix + "NEW VALUE" + suffix
    start, end = len(prefix) - 150, len(prefix) + len("NEW VALUE") + 150
    assert data["content"] == (
        f"{first_line}\n[{start} characters not shown]{line[start:end]}"
        f"[{len(line) - end} characters not shown]"
    )
    assert "more" not in data
    removed = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": suffix, "new_text": ""},
    )
    kept = prefix + "NEW VALUE"
    assert removed["data"]["content"] == (
        f"{first_line}\n[{len(kept) - 300} characters not shown]{kept[-300:]}"
    )
    assert "more" not in removed["data"]
    assert (await stored(board, page_id))["content"] == first_line + newline + kept
