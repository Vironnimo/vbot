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


def test_exact_match_preserves_crlf_in_replacement() -> None:
    result = replace_fuzzy("alpha\r\nbeta\r\n", "beta", "one\ntwo", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "alpha\r\none\r\ntwo\r\n"
    assert result.strategy == "exact"


def test_exact_match_preserves_cr_only_endings() -> None:
    # Classic-Mac CR-only files must keep their CR endings in the replacement
    # instead of mixing in LF (the old detection misread CR-only as LF).
    result = replace_fuzzy("alpha\rbeta\r", "beta", "one\ntwo", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "alpha\rone\rtwo\r"
    assert result.strategy == "exact"


def test_unicode_line_separator_is_line_content_not_a_line_ending() -> None:
    # Like read and search_files, U+2028 is an ordinary in-line character.
    result = replace_fuzzy("alpha\u2028beta\u2028", "beta", "one\ntwo", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "alpha\u2028one\ntwo\u2028"
    assert result.strategy == "exact"
    assert result.first_changed_line == 1


def test_lf_locator_does_not_match_across_unicode_line_separators() -> None:
    content = "alpha\u2028beta\u2028gamma\u2028"

    assert replace_fuzzy(content, "alpha\nbeta", "one\ntwo", replace_all=False) is None


def test_cr_only_line_numbers() -> None:
    # Line numbers count CR-only breaks, not just LF.
    result = replace_fuzzy("one\rtwo\rthree\r", "two", "x", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.first_changed_line == 2


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


def test_reindent_converts_spaces_to_the_files_tabs() -> None:
    content = 'func greet() string {\n\tif polite {\n\t\treturn "Hello"\n\t}\n\treturn "Hi"\n}\n'

    result = replace_fuzzy(
        content,
        '    if polite {\n        return "Hello"\n    }',
        '    if polite {\n        if loud {\n            return "HELLO"\n        }\n'
        '        return "Hello"\n    }',
        replace_all=False,
    )

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == (
        'func greet() string {\n\tif polite {\n\t\tif loud {\n\t\t\treturn "HELLO"\n'
        '\t\t}\n\t\treturn "Hello"\n\t}\n\treturn "Hi"\n}\n'
    )


def test_reindent_converts_tabs_to_the_files_spaces() -> None:
    content = "def f():\n    if ready:\n        go()\n"

    result = replace_fuzzy(
        content,
        "\tif ready:\n\t\tgo()",
        "\tif ready:\n\t\tif fast:\n\t\t\trun()",
        replace_all=False,
    )

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "def f():\n    if ready:\n        if fast:\n            run()\n"


def test_reindent_scales_a_new_deeper_line_to_the_files_level_width() -> None:
    content = "def f():\n    a = 1\n    b = 2\n"

    result = replace_fuzzy(
        content, "  a = 1\n  b = 2", "  a = 1\n  if a:\n    b = 3", replace_all=False
    )

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == "def f():\n    a = 1\n    if a:\n        b = 3\n"


def test_reindent_restores_a_dropped_outer_level_in_tabs() -> None:
    content = "class A:\n\tdef f(self):\n\t\treturn 1\n"

    result = replace_fuzzy(
        content,
        "def f(self):\n    return 1",
        "def f(self):\n    if self:\n        return 2\n    return 1",
        replace_all=False,
    )

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == (
        "class A:\n\tdef f(self):\n\t\tif self:\n\t\t\treturn 2\n\t\treturn 1\n"
    )


def test_reindent_converts_levels_despite_an_aligned_line() -> None:
    content = "func f() {\n\tcall(a,\n\t     b)\n}\n"

    result = replace_fuzzy(
        content,
        "    call(a,\n         b)",
        "    call(a,\n         b)\n    if ok {\n        done()\n    }",
        replace_all=False,
    )

    assert isinstance(result, FuzzyReplacement)
    assert result.new_content == (
        "func f() {\n\tcall(a,\n\t     b)\n\tif ok {\n\t\tdone()\n\t}\n}\n"
    )


def test_line_trimmed_preserves_crlf_endings() -> None:
    # A whitespace-tolerant line match (2-space args vs a 4-space CRLF file) must
    # not mangle the file's CRLF endings.
    content = "def f():\r\n    a = 1\r\n    b = 2\r\n"

    result = replace_fuzzy(content, "  a = 1\n  b = 2", "  a = 1\n  c = 3", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "line_trimmed"
    assert result.new_content == "def f():\r\n    a = 1\r\n    c = 3\r\n"


def test_line_trimmed_reindents_without_normalizing_explicit_unicode_separator() -> None:
    content = "def f():\n    alpha\n    beta\n"

    result = replace_fuzzy(
        content,
        "  alpha\n  beta",
        "  alpha\u2028  BETA",
        replace_all=False,
    )

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "line_trimmed"
    # The separator stays literal line content, so only the line start is reindented.
    assert result.new_content == "def f():\n    alpha\u2028  BETA\n"


def test_line_trimmed_does_not_match_genuinely_different_text() -> None:
    # Same shape, different content — must not fuzzily replace the wrong block.
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


def test_whitespace_normalized_matches_internal_space_and_tab_runs() -> None:
    content = "def f():\n    value  =\t1\n"

    result = replace_fuzzy(content, "  value = 1", "  value = 2", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "whitespace_normalized"
    assert result.new_content == "def f():\n    value = 2\n"
    assert result.first_changed_line == 2


def test_whitespace_normalized_preserves_crlf_endings() -> None:
    content = "alpha  = 1\r\nbeta\r\n"

    result = replace_fuzzy(content, "alpha = 1", "one = 1\ntwo = 2", replace_all=False)

    assert isinstance(result, FuzzyReplacement)
    assert result.strategy == "whitespace_normalized"
    assert result.new_content == "one = 1\r\ntwo = 2\r\nbeta\r\n"


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


@pytest.mark.parametrize(
    ("content", "old_string", "lines"),
    [
        # Occurrences overlap at the shared middle line.
        ("x\n}\n}\n}\ny\n", "}\n}", [2, 3]),
        # A partial occurrence must not shadow a later whole-line one.
        ("a {\n}\n}\nb {\n  c {\n    }\n}\n}\n", "}\n}", [2, 7]),
    ],
)
def test_overlapping_whole_line_occurrences_are_ambiguous(
    content: str, old_string: str, lines: list[int]
) -> None:
    result = replace_fuzzy(
        content, old_string, "}\nnew\n}", replace_all=False, whole_lines=True, typographic=True
    )

    assert isinstance(result, AmbiguousFuzzyMatch)
    assert result.line_numbers == lines


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
