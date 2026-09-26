"""Wiki copy recovery and evidence from the exact revision a mutation saved."""

import pytest

from tests.resources.extensions.test_swarm_board import board as board
from tests.resources.extensions.test_swarm_board import continuation
from tests.resources.extensions.test_swarm_wiki import create, invoke, stored

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
    assert result["data"]["content"].startswith(replacement)
    assert "revision 2" in result["data"]["shown"]


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
    assert result["data"]["content"] == "Changed"


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
    assert result["data"]["content"] == current
    assert (await stored(board, page_id))["content"] == current


@pytest.mark.asyncio
async def test_wiki_mutation_excerpt_is_bounded_revision_pinned_and_replayable(board):
    content = "\n".join(f"Row {i}: " + "x" * 400 for i in range(15))
    page_id = await create(board, content)
    edit = {
        "action": "update",
        "page_id": page_id,
        "old_text": "Row 4:",
        "new_text": "Revised row 4:",
    }
    result = await invoke(board, edit, tool_call_id="excerpt-edit")
    data = result["data"]
    assert data["revision"] == 2
    assert data["content"].startswith("Revised row 4:")
    assert len(data["content"]) <= 2000
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
    expected = content.replace("Row 4:", "Revised row 4:")
    assert more["data"]["content"] == expected[next_call["offset"] :]
    replay = await invoke(board, edit, tool_call_id="excerpt-edit")
    assert replay["data"]["content"] == data["content"]
    assert replay["data"]["revision"] == 2
    assert (await stored(board, page_id))["content"] == "Peer revision"


@pytest.mark.asyncio
async def test_wiki_deletion_and_whole_page_replacement_show_actual_saved_content(board):
    page_id = await create(board, "Before\nRemove this\nAfter")
    removed = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": "Remove this\n", "new_text": ""},
    )
    assert removed["data"]["content"] == "After"
    replaced = await invoke(
        board,
        {"action": "update", "page_id": page_id, "content": "", "expected_revision": 2},
    )
    assert replaced["data"]["content"] == ""
    assert (await stored(board, page_id))["content"] == ""
    assert (await stored(board, page_id))["revision"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["LF", "CRLF", "CR"])
async def test_wiki_excerpt_shows_a_change_late_in_a_long_line_and_end_deletion(board, newline):
    first_line = "First line must stay outside the changed excerpt." + newline
    prefix, suffix = "Before " * 500, " After" * 500
    page_id = await create(board, first_line + prefix + "OLD VALUE" + suffix)
    changed = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": "OLD VALUE", "new_text": "NEW VALUE"},
    )
    data = changed["data"]
    assert "from line 2:" in data["shown"]
    assert first_line not in data["content"]
    assert "NEW VALUE" in data["content"]
    assert len(data["content"]) <= 2000
    next_call = continuation(data["more"])
    assert next_call["revision"] == 2
    actual = first_line + prefix + "NEW VALUE" + suffix
    assert (
        data["content"] == actual[next_call["offset"] - len(data["content"]) : next_call["offset"]]
    )
    tail = await invoke(board, next_call)
    assert tail["data"]["content"] == actual[next_call["offset"] :]
    removed = await invoke(
        board,
        {"action": "update", "page_id": page_id, "old_text": suffix, "new_text": ""},
    )
    assert removed["data"]["content"].endswith("NEW VALUE")
    assert "more" not in removed["data"]
    assert "from line 2:" in removed["data"]["shown"]
    assert (await stored(board, page_id))["content"] == first_line + prefix + "NEW VALUE"
