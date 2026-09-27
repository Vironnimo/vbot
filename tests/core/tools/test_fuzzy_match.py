"""Tests for the edit tool's fuzzy find-and-replace strategies."""

from __future__ import annotations

import pytest

from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    replace_fuzzy,
)


def test_exact_match_replaces_and_reports_strategy() -> None:
    result = replace_fuzzy("a = 1\nb = 2\n", "a = 1", "a = 9", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "a = 9\nb = 2\n"
    assert result.strategy == "exact"
    assert result.replacements == 1
    assert result.first_changed_line == 1


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
def test_exact_match_keeps_the_files_line_endings_and_counts_them(ending: str) -> None:
    # Classic-Mac CR-only files keep their CR endings instead of mixing in LF.
    result = replace_fuzzy(f"alpha{ending}beta{ending}", "beta", "one\ntwo", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == f"alpha{ending}one{ending}two{ending}"
    assert result.strategy == "exact"
    assert result.first_changed_line == 2


def test_unicode_line_separator_is_line_content_not_a_line_ending() -> None:
    # Like read and search_files, U+2028 is an ordinary in-line character.
    result = replace_fuzzy("alpha\u2028beta\u2028", "beta", "one\ntwo", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "alpha\u2028one\ntwo\u2028"
    assert result.strategy == "exact"
    assert result.first_changed_line == 1
    # An LF locator does not match across the separators.
    content = "alpha\u2028beta\u2028gamma\u2028"
    assert replace_fuzzy(content, "alpha\nbeta", "one\ntwo", replace_all=False) is None


@pytest.mark.parametrize(
    ("content", "old_string", "new_string", "expected"),
    [
        # Curly quotes sent for straight ones (built from code points to keep the source ASCII).
        ('msg = "hello"\n', f"msg = {chr(0x201C)}hello{chr(0x201D)}", 'msg = "hi"', 'msg = "hi"\n'),
        # A non-breaking space sent for a regular one.
        ("a = 1\n", f"a{chr(0xA0)}= 1", "a = 2", "a = 2\n"),
        # An LF locator in a CRLF file keeps the file's endings.
        ("alpha\r\nbeta\r\ngamma\r\n", "alpha\nbeta", "one\ntwo", "one\r\ntwo\r\ngamma\r\n"),
    ],
)
def test_normalized_matches_typography_and_line_endings(
    content: str, old_string: str, new_string: str, expected: str
) -> None:
    result = replace_fuzzy(content, old_string, new_string, replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "normalized"
    assert result.new_content == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("def f():\n    a = 1\n    b = 2\n", "def f():\n    a = 1\n    c = 3\n"),
        # A whitespace-tolerant line match must not mangle CRLF endings.
        ("def f():\r\n    a = 1\r\n    b = 2\r\n", "def f():\r\n    a = 1\r\n    c = 3\r\n"),
    ],
)
def test_line_trimmed_matches_different_indent_and_reindents(content: str, expected: str) -> None:
    # The file body is 4-space indented; the model sent 2 spaces. The replacement
    # is re-indented to the file's 4-space style.
    result = replace_fuzzy(content, "  a = 1\n  b = 2", "  a = 1\n  c = 3", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "line_trimmed"
    assert result.new_content == expected
    assert result.first_changed_line == 2


@pytest.mark.parametrize(
    ("content", "old_string", "new_string", "expected"),
    [
        # Spaces sent for the file's tabs.
        (
            'func greet() string {\n\tif polite {\n\t\treturn "Hello"\n\t}\n\treturn "Hi"\n}\n',
            '    if polite {\n        return "Hello"\n    }',
            '    if polite {\n        if loud {\n            return "HELLO"\n        }\n'
            '        return "Hello"\n    }',
            'func greet() string {\n\tif polite {\n\t\tif loud {\n\t\t\treturn "HELLO"\n'
            '\t\t}\n\t\treturn "Hello"\n\t}\n\treturn "Hi"\n}\n',
        ),
        # Tabs sent for the file's spaces.
        (
            "def f():\n    if ready:\n        go()\n",
            "\tif ready:\n\t\tgo()",
            "\tif ready:\n\t\tif fast:\n\t\t\trun()",
            "def f():\n    if ready:\n        if fast:\n            run()\n",
        ),
        # A new deeper line is scaled to the file's level width.
        (
            "def f():\n    a = 1\n    b = 2\n",
            "  a = 1\n  b = 2",
            "  a = 1\n  if a:\n    b = 3",
            "def f():\n    a = 1\n    if a:\n        b = 3\n",
        ),
        # A dropped outer level is restored in tabs.
        (
            "class A:\n\tdef f(self):\n\t\treturn 1\n",
            "def f(self):\n    return 1",
            "def f(self):\n    if self:\n        return 2\n    return 1",
            "class A:\n\tdef f(self):\n\t\tif self:\n\t\t\treturn 2\n\t\treturn 1\n",
        ),
        # Levels are converted despite an aligned continuation line.
        (
            "func f() {\n\tcall(a,\n\t     b)\n}\n",
            "    call(a,\n         b)",
            "    call(a,\n         b)\n    if ok {\n        done()\n    }",
            "func f() {\n\tcall(a,\n\t     b)\n\tif ok {\n\t\tdone()\n\t}\n}\n",
        ),
        # An explicit Unicode separator stays literal line content; only line starts move.
        (
            "def f():\n    alpha\n    beta\n",
            "  alpha\n  beta",
            "  alpha\u2028  BETA",
            "def f():\n    alpha\u2028  BETA\n",
        ),
    ],
)
def test_reindent_writes_new_lines_in_the_files_indentation(
    content: str, old_string: str, new_string: str, expected: str
) -> None:
    result = replace_fuzzy(content, old_string, new_string, replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "line_trimmed"
    assert result.new_content == expected


def test_line_trimmed_does_not_match_genuinely_different_text() -> None:
    # Same shape, different content: must not fuzzily replace the wrong block.
    content = "def f():\n    a = 1\n    b = 2\n"

    assert (
        replace_fuzzy(
            content,
            "release production artifacts\nnotify every customer",
            "x",
            replace_all=False,
        )
        is None
    )


@pytest.mark.parametrize(
    ("content", "old_string", "new_string", "expected", "line"),
    [
        (
            "def f():\n    value  =\t1\n",
            "  value = 1",
            "  value = 2",
            "def f():\n    value = 2\n",
            2,
        ),
        (
            "alpha  = 1\r\nbeta\r\n",
            "alpha = 1",
            "one = 1\ntwo = 2",
            "one = 1\r\ntwo = 2\r\nbeta\r\n",
            1,
        ),
    ],
)
def test_whitespace_normalized_matches_internal_space_and_tab_runs(
    content: str, old_string: str, new_string: str, expected: str, line: int
) -> None:
    result = replace_fuzzy(content, old_string, new_string, replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "whitespace_normalized"
    assert result.new_content == expected
    assert result.first_changed_line == line


def test_whitespace_normalized_keeps_ambiguity_and_replace_all_semantics() -> None:
    content = "value  = 1\nvalue\t= 1\n"

    ambiguous = replace_fuzzy(content, "value = 1", "value = 2", replace_all=False)
    replaced = replace_fuzzy(content, "value = 1", "value = 2", replace_all=True)

    assert isinstance(ambiguous, AmbiguousFuzzyMatch)
    assert ambiguous.occurrences == 2
    assert ambiguous.line_numbers == [1, 2]
    assert isinstance(replaced, FuzzyReplacement)
    assert replaced.strategy == "whitespace_normalized"
    assert replaced.new_content == "value = 2\nvalue = 2\n"
    assert replaced.replacements == 2


def test_repeated_text_is_ambiguous_unless_every_occurrence_is_replaced() -> None:
    ambiguous = replace_fuzzy("x\ny\nx\n", "x", "z", replace_all=False)
    replaced = replace_fuzzy("x\ny\nx\n", "x", "z", replace_all=True)

    assert isinstance(ambiguous, AmbiguousFuzzyMatch)
    assert ambiguous.occurrences == 2
    assert ambiguous.line_numbers == [1, 3]
    assert isinstance(replaced, FuzzyReplacement)
    assert replaced.new_content == "z\ny\nz\n"
    assert replaced.replacements == 2
    assert replaced.first_changed_line == 1


def test_no_match_returns_none() -> None:
    assert replace_fuzzy("hello\nworld\n", "missing", "x", replace_all=False) is None


def test_closest_candidates_rank_same_sized_raw_block() -> None:
    content = (
        "def unrelated():\n    return None\n\ndef deploy():\n    timeout = 30\n    retries = 5\n"
    )

    candidates = find_closest_candidates(
        content,
        "def deploy():\n    timeout = 20\n    retries = 5",
    )

    assert candidates
    assert candidates[0].line_number == 4
    assert candidates[0].text == "def deploy():\n    timeout = 30\n    retries = 5"
    assert candidates[0].truncated is False


def test_closest_candidates_suppress_weak_noise() -> None:
    assert find_closest_candidates("alpha\nbeta\ngamma\n", "totally unrelated locator") == []


def test_closest_candidates_bound_count_and_raw_excerpt() -> None:
    content = "\n".join(f"target value {index}" for index in range(10))

    candidates = find_closest_candidates(content, "target value x")

    assert len(candidates) == 3
    assert all(len(candidate.text) <= 1_200 for candidate in candidates)


def test_closest_candidates_return_bounded_prefix_for_long_raw_line() -> None:
    raw_line = "target = " + "x" * 2_000

    candidates = find_closest_candidates(raw_line, "target = " + "x" * 1_999 + "y")

    assert len(candidates) == 1
    assert candidates[0].text == raw_line[:1_200]
    assert candidates[0].truncated is True


def test_exact_wins_over_looser_strategies() -> None:
    # A clean exact match must be used as-is, never escalated to a fuzzy one.
    result = replace_fuzzy("  value = 1\n", "  value = 1", "  value = 2", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "exact"
    assert result.new_content == "  value = 2\n"


@pytest.mark.parametrize(
    ("content", "old_string"),
    [
        ('message = "finish the current Run now"\n', 'message = "finish your current Run now"'),
        (
            "start\nalpha\nthe current Run\nomega\nend\n",
            "start\nalpha\nyour current Run\nomega\nend",
        ),
    ],
)
def test_word_differences_are_left_to_copy_match(content: str, old_string: str) -> None:
    # Every strategy here is precise; a copy with other words is copy_match's concern.
    assert replace_fuzzy(content, old_string, "x", replace_all=False, typographic=True) is None


def test_replace_all_replaces_leftmost_non_overlapping_occurrences() -> None:
    result = replace_fuzzy("aaaa", "aa", "b", replace_all=True)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "bb"
    assert result.replacements == 2


def test_replace_all_does_not_use_approximate_strategies() -> None:
    content = 'message = "finish the current Run now"\n'

    result = replace_fuzzy(
        content,
        'message = "finish your current Run now"',
        'message = "wait now"',
        replace_all=True,
    )

    assert result is None
