"""Tests for the edit tool's fuzzy find-and-replace strategies."""

from __future__ import annotations

from core.tools.fuzzy_match import AmbiguousFuzzyMatch, FuzzyReplacement, replace_fuzzy


def test_exact_match_replaces_and_reports_strategy() -> None:
    result = replace_fuzzy("a = 1\nb = 2\n", "a = 1", "a = 9", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "a = 9\nb = 2\n"
    assert result.strategy == "exact"
    assert result.replacements == 1
    assert result.first_changed_line == 1


def test_exact_match_preserves_crlf_in_replacement() -> None:
    result = replace_fuzzy("alpha\r\nbeta\r\n", "beta", "one\ntwo", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "alpha\r\none\r\ntwo\r\n"
    assert result.strategy == "exact"


def test_normalized_matches_smart_quotes() -> None:
    # File has straight quotes; the model sent curly ones (built via code points
    # so the source stays pure ASCII).
    curly = chr(0x201C) + "hello" + chr(0x201D)
    result = replace_fuzzy('msg = "hello"\n', f"msg = {curly}", 'msg = "hi"', replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "normalized"
    assert result.new_content == 'msg = "hi"\n'


def test_normalized_matches_non_breaking_space() -> None:
    # File uses a regular space; the model sent a non-breaking space (U+00A0).
    nbsp = chr(0xA0)
    result = replace_fuzzy("a = 1\n", f"a{nbsp}= 1", "a = 2", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "normalized"
    assert result.new_content == "a = 2\n"


def test_normalized_matches_across_crlf_and_preserves_endings() -> None:
    content = "alpha\r\nbeta\r\ngamma\r\n"

    result = replace_fuzzy(content, "alpha\nbeta", "one\ntwo", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "normalized"
    assert result.new_content == "one\r\ntwo\r\ngamma\r\n"


def test_line_trimmed_matches_different_indent_and_reindents() -> None:
    # File body is 4-space indented; the model sent 2-space indentation. The
    # replacement must be re-indented to the file's actual 4-space style.
    content = "def f():\n    a = 1\n    b = 2\n"

    result = replace_fuzzy(content, "  a = 1\n  b = 2", "  a = 1\n  c = 3", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "line_trimmed"
    assert result.new_content == "def f():\n    a = 1\n    c = 3\n"
    assert result.first_changed_line == 2


def test_line_trimmed_preserves_crlf_endings() -> None:
    # A whitespace-tolerant line match (2-space args vs a 4-space CRLF file) must
    # not mangle the file's CRLF endings.
    content = "def f():\r\n    a = 1\r\n    b = 2\r\n"

    result = replace_fuzzy(content, "  a = 1\n  b = 2", "  a = 1\n  c = 3", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "line_trimmed"
    assert result.new_content == "def f():\r\n    a = 1\r\n    c = 3\r\n"


def test_line_trimmed_does_not_match_genuinely_different_text() -> None:
    # Same shape, different content — must not fuzzily replace the wrong block.
    content = "def f():\n    a = 1\n    b = 2\n"

    assert replace_fuzzy(content, "  a = 9\n  b = 9", "x", replace_all=False) is None


def test_ambiguous_returns_match_with_line_numbers() -> None:
    result = replace_fuzzy("x\ny\nx\n", "x", "z", replace_all=False)

    assert isinstance(result, AmbiguousFuzzyMatch)
    assert result.occurrences == 2
    assert result.line_numbers == [1, 3]


def test_replace_all_replaces_every_occurrence() -> None:
    result = replace_fuzzy("x\ny\nx\n", "x", "z", replace_all=True)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "z\ny\nz\n"
    assert result.replacements == 2
    assert result.first_changed_line == 1


def test_no_match_returns_none() -> None:
    assert replace_fuzzy("hello\nworld\n", "missing", "x", replace_all=False) is None


def test_exact_wins_over_looser_strategies() -> None:
    # A clean exact match must be used as-is, never escalated to a fuzzy one.
    result = replace_fuzzy("  value = 1\n", "  value = 1", "  value = 2", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "exact"
    assert result.new_content == "  value = 2\n"
