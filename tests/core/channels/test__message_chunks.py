"""Shared outbound Channel text splitting."""

from __future__ import annotations

import pytest

from core.channels._message_chunks import split_message


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_prefers_paragraph_then_line_then_word_boundaries() -> None:
    paragraphs = "First paragraph here.\n\nSecond paragraph that is longer."
    assert split_message(paragraphs, 40) == [
        "First paragraph here.",
        "Second paragraph that is longer.",
    ]
    lines = "alpha beta gamma\ndelta epsilon zeta"
    assert split_message(lines, 25) == ["alpha beta gamma", "delta epsilon zeta"]
    assert split_message("one two three four five", 10) == ["one two", "three four", "five"]


def test_code_fence_is_closed_and_reopened_with_its_info_string() -> None:
    code = "\n".join(f"value_{index} = {index}" for index in range(12))
    message = f"Here is the script:\n\n```python\n{code}\n```\n\nDone."

    chunks = split_message(message, 80)

    assert len(chunks) > 2
    assert all(len(chunk) <= 80 for chunk in chunks)
    for chunk in chunks[1:-1]:
        assert chunk.startswith("```python\n")
        assert chunk.endswith("\n```")
    assert chunks[0].endswith("\n```")
    assert chunks[-1].endswith("Done.")
    # Every chunk renders balanced fences, and no code line is lost or cut.
    for chunk in chunks:
        assert sum(line.startswith("```") for line in chunk.split("\n")) % 2 == 0
    emitted = [line for chunk in chunks for line in chunk.split("\n")]
    assert [line for line in emitted if line.startswith("value_")] == code.split("\n")


def test_tilde_fences_keep_their_marker_length() -> None:
    message = "~~~~ js\n" + "\n".join(f"let v{index} = {index};" for index in range(8)) + "\n~~~~"

    chunks = split_message(message, 50)

    assert all(len(chunk) <= 50 for chunk in chunks)
    assert all(chunk.startswith("~~~~ js\n") for chunk in chunks)
    assert all(chunk.endswith("\n~~~~") for chunk in chunks)


def test_plain_text_mode_never_adds_fence_markers() -> None:
    message = "```\n" + "\n".join(["line of code"] * 10) + "\n```"

    chunks = split_message(message, 40, code_fences=False)

    assert all(len(chunk) <= 40 for chunk in chunks)
    assert sum(chunk.count("```") for chunk in chunks) == 2


def test_long_unbroken_token_is_hard_cut_at_the_limit() -> None:
    token = "x" * 25
    assert split_message(f"short {token}", 10) == ["short", "x" * 10, "x" * 10, "x" * 5]


@pytest.mark.parametrize(
    ("message", "limit", "expected"),
    [
        (
            "\U0001f468\u200d\U0001f469\u200d\U0001f467" * 2,
            7,
            ["\U0001f468\u200d\U0001f469\u200d\U0001f467"] * 2,
        ),
        (
            "\U0001f1e9\U0001f1ea\U0001f1eb\U0001f1f7",
            3,
            ["\U0001f1e9\U0001f1ea", "\U0001f1eb\U0001f1f7"],
        ),
        ("e\u0301" * 3, 3, ["e\u0301", "e\u0301", "e\u0301"]),
        (
            "\U0001f44d\U0001f3fd" * 3,
            5,
            ["\U0001f44d\U0001f3fd\U0001f44d\U0001f3fd", "\U0001f44d\U0001f3fd"],
        ),
    ],
)
def test_hard_cuts_do_not_split_grapheme_clusters(
    message: str, limit: int, expected: list[str]
) -> None:
    assert split_message(message, limit) == expected


def test_limit_uses_the_supplied_length_measure() -> None:
    message = "\U0001f600" * 8

    chunks = split_message(message, 4, measure=_utf16)

    assert chunks == ["\U0001f600\U0001f600"] * 4
    fenced = "```\n" + "\U0001f600 " * 20 + "\n```"
    assert all(_utf16(chunk) <= 20 for chunk in split_message(fenced, 20, measure=_utf16))


def test_rejects_non_positive_limit_and_skips_empty_input() -> None:
    with pytest.raises(ValueError):
        split_message("hello", 0)
    assert split_message("", 10) == []
