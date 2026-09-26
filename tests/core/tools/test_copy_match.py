"""Tests for applying edits whose old text copies the file with errors."""

from __future__ import annotations

import time

import pytest

from core.tools.copy_match import copy_warnings, match_copied_edit, replace_copied
from core.tools.fuzzy_match import AmbiguousFuzzyMatch, FuzzyReplacement


def _applied(found: object) -> FuzzyReplacement:
    assert isinstance(found, FuzzyReplacement), found
    assert found.strategy == "copied"
    return found


def test_misspelled_old_word_keeps_the_file_spelling_and_names_the_line() -> None:
    content = "def load():\n    return reciept_total\n"

    found = _applied(
        replace_copied(
            content,
            "def load():\n    return receipt_total",
            "def load():\n    return receipt_total + tax",
        )
    )

    assert found.new_content == "def load():\n    return reciept_total + tax\n"
    assert copy_warnings(found) == [
        "Line 2 did not match your old text exactly and was edited anyway; "
        "it read:     return reciept_total",
        'Your new text uses the file\'s spelling: "reciept_total" for "receipt_total".',
    ]


def test_misspelling_in_a_changed_line_is_placed_by_exact_context() -> None:
    content = "def compute(total, count):\n    value = total / count\n    return value\n"
    lines = [
        (" ", "def compute(total, count):"),
        (" ", "    value = total / count"),
        ("-", "    retrun value"),
        ("+", "    return value + 1"),
    ]

    found = _applied(match_copied_edit(content, lines))

    assert found.new_content == (
        "def compute(total, count):\n    value = total / count\n    return value + 1\n"
    )
    assert copy_warnings(found) == [
        "Line 3 did not match your old text exactly and was edited anyway; "
        "it read:     return value"
    ]


@pytest.mark.parametrize(
    ("content", "lines"),
    [
        # Too few correct words carry a misspelling.
        ("def f(x):\n    return value\n", [("-", "    retrun value"), ("+", "    return 1")]),
        # A short change resting on another value.
        (
            "TIMEOUT = 30\nRETRIES = 5\nMAX_SIZE = 1024\n",
            [
                (" ", "TIMEOUT = 30"),
                ("-", "RETRIES = 3"),
                ("+", "RETRIES = 4"),
                (" ", "MAX_SIZE = 1024"),
            ],
        ),
        # Another comparison is not a copy error.
        (
            "def f():\n    if count > limit and ready:\n        go()\n",
            [
                (" ", "def f():"),
                ("-", "    if count < limit and ready:"),
                ("+", "    if count < limit and ready and ok:"),
                (" ", "        go()"),
            ],
        ),
        # Lines without words never anchor a passage.
        (
            "}\nconsole.log(alpha)\n}\n",
            [
                (" ", "}"),
                ("-", "console.log(beta)"),
                ("+", "console.log(gamma)"),
                (" ", "}"),
            ],
        ),
        # Another identifier in a kept line names another place.
        (
            "def save_user(key):\n    pass\n",
            [(" ", "def load_user(key):"), ("-", "    pass"), ("+", "    return 1")],
        ),
        # Below three correct words a copy is exact up to spacing.
        ("retry()\nstop\n", [(" ", "retry();"), ("-", "stop"), ("+", "halt")]),
        # A kept line may lack or add a few words, not this many.
        (
            "def process(data):\n    pass\n",
            [
                (" ", "def process(data, strict, retries):"),
                ("-", "    pass"),
                ("+", "    return 1"),
            ],
        ),
        # Words under 4 characters, other digits, and words the file holds are not
        # misspellings.
        (
            "the cat sat on the mat\n",
            [("-", "the car sat on the mat"), ("+", "the car sat on the rug")],
        ),
        (
            "load version2 of the parser module\n",
            [("-", "load version3 of the parser module"), ("+", "load it")],
        ),
        (
            "weight = scale(input_value)\nheight = scale(input_value)\n",
            [("-", "weight = scale(input_valeu)"), ("+", "weight = 0")],
        ),
    ],
)
def test_copies_without_enough_evidence_are_refused(
    content: str, lines: list[tuple[str, str]]
) -> None:
    assert match_copied_edit(content, lines) is None


def test_other_differences_need_twelve_correct_words_and_the_file_keeps_them() -> None:
    content = (
        "The scheduler runs every job in order, and it retries a failed job twice before "
        "it reports the failure to the owner. Jobs never overlap.\n"
    )
    old = (
        "The scheduler runs every job in order and it retries a failed job twice before "
        "it reports the failure to the owner. Jobs never overlap."
    )

    found = _applied(replace_copied(content, old, old.replace("never", "may")))

    assert found.new_content == content.replace("never", "may")
    assert copy_warnings(found) == [
        f"Line 1 did not match your old text exactly and was edited anyway; it read: "
        f"{content.rstrip()}"
    ]


def test_a_rewrite_may_differ_where_it_discards_the_old_text() -> None:
    content = (
        "    message = 'Continue work that does not depend on the result, or finish the "
        "current Run now.'\n"
    )
    old = (
        "    message = 'Continue work that does not depend on the result, or finish your "
        "current Run now.'"
    )

    found = _applied(replace_copied(content, old, "    message = 'Wait for the result.'"))

    assert found.new_content == "    message = 'Wait for the result.'\n"


def test_a_change_next_to_another_difference_is_refused() -> None:
    content = "    if count > limit and the queue is not empty and the worker is still running:\n"
    old = "    if count < limit and the queue is not empty and the worker is still running:"

    assert replace_copied(content, old, old.replace("limit", "limit + 1")) is None
    far = _applied(replace_copied(content, old, old.replace("running", "running now")))
    assert far.new_content == content.replace("running", "running now")


@pytest.mark.parametrize(
    ("content", "context", "expected"),
    [
        # A stale or misremembered kept line may add or lack a few words.
        (
            "def process(data):\n    value = “x”\n    return True\n",
            "def process(data, strict):",
            "def process(data):\n    value = “y”\n    return True\n",
        ),
        (
            "def process(data, strict):\n    value = “x”\n    return True\n",
            "def process(data):",
            "def process(data, strict):\n    value = “y”\n    return True\n",
        ),
    ],
)
def test_kept_lines_may_lack_or_add_a_few_words(content: str, context: str, expected: str) -> None:
    lines = [
        (" ", context),
        ("-", '    value = "x"'),
        ("+", '    value = "y"'),
        (" ", "    return True"),
    ]

    found = _applied(match_copied_edit(content, lines))

    assert found.new_content == expected
    assert copy_warnings(found) == []


def test_exact_end_lines_place_a_passage_around_similar_lines() -> None:
    content = (
        "def handle(request):\n    audit(request)\n    save_to_database(request)\n"
        "    return response_ok\n"
    )
    lines = [
        (" ", "def handle(request):"),
        (" ", "    audit(request, strict)"),
        (" ", "    save_to_db(request)"),
        ("+", "    notify(request)"),
        (" ", "    return response_ok"),
    ]

    found = _applied(match_copied_edit(content, lines))

    assert found.new_content == (
        "def handle(request):\n    audit(request)\n    save_to_database(request)\n"
        "    notify(request)\n    return response_ok\n"
    )


@pytest.mark.parametrize("elsewhere", ["", "load_user = cached(save_user)\n"])
def test_another_identifier_names_another_place_however_long_the_copy(elsewhere: str) -> None:
    content = (
        f"{elsewhere}def save_user(key, db):\n    value = db.get(key)\n"
        "    if value is None:\n        return default_value\n    return value\n"
    )
    lines = [
        (" ", "def load_user(key, db):"),
        (" ", "    value = db.get(key)"),
        (" ", "    if value is None:"),
        ("-", "        return default_value"),
        ("+", "        raise KeyError(key)"),
        (" ", "    return value"),
    ]

    assert match_copied_edit(content, lines) is None


def test_a_passage_copied_up_to_misspellings_wins_over_looser_ones() -> None:
    block = (
        "def handler_{0}(request, context):\n    value = request.get(field_name)\n"
        "    if value is None:\n        return None\n    return context.process(value)\n"
    )
    content = block.format(1) + block.format(2)
    lines = [
        (" ", "def handler_2(request, context):"),
        (" ", "    value = request.get(field_name)"),
        (" ", "    if value is None:"),
        ("-", "        retrun None"),
        ("+", "        raise KeyError(field_name)"),
        (" ", "    return context.process(value)"),
    ]

    found = _applied(match_copied_edit(content, lines))

    assert found.new_content == block.format(1) + block.format(2).replace(
        "return None", "raise KeyError(field_name)"
    )


def test_a_change_that_does_not_fit_the_best_passage_is_not_applied_elsewhere() -> None:
    content = (
        "start of the block here\nalpha beta gamma\ndelta epsilon zeta eta theta\n"
        "end of the block here\n"
        "start of the block here\nomega beta gamma delta\nepsilon zeta eta theta\n"
        "end of the block here\n"
    )
    lines = [
        (" ", "start of the block here"),
        ("-", "alpha beta gamma delta"),
        ("-", "epsilon zeta eta theta"),
        ("+", "alpha beta gamma delta"),
        ("+", "EPS zeta eta theta"),
        (" ", "end of the block here"),
    ]

    # The first block holds every word, but its line break sits next to the change.
    assert match_copied_edit(content, lines) is None


def test_several_qualifying_passages_are_ambiguous() -> None:
    line = "grand_total = reciept_total + shipping_cost + handling_fee"
    content = f"{line}\nprint(grand_total)\n{line}\n"

    found = replace_copied(
        content,
        line.replace("reciept", "receipt"),
        "grand_total = receipt_total + shipping_cost",
    )

    assert isinstance(found, AmbiguousFuzzyMatch)
    assert found.occurrences == 2
    assert found.line_numbers == [1, 3]


def test_part_of_a_line_is_matched_as_a_fragment() -> None:
    content = "    result = compute_total(alpha, beta, gamma) + offset_value  # keep\n"

    found = _applied(
        replace_copied(content, "compute_totl(alpha, beta, gamma)", "compute_total(alpha, beta)")
    )

    assert found.new_content == "    result = compute_total(alpha, beta) + offset_value  # keep\n"


def test_line_endings_and_indentation_follow_the_file() -> None:
    content = "class Account:\r\n    def run(self):\r\n        return reciept_total\r\n"

    found = _applied(
        replace_copied(
            content,
            "def run(self):\n    return receipt_total",
            "def run(self):\n    return receipt_total + tax\n    # done",
        )
    )

    assert found.new_content == (
        "class Account:\r\n    def run(self):\r\n        return reciept_total + tax\r\n"
        "        # done\r\n"
    )


def test_removing_whole_lines_removes_their_line_breaks() -> None:
    content = "keep one\nremove this reciept line now\nkeep two\n"

    found = _applied(replace_copied(content, "remove this receipt line now\n", ""))

    assert found.new_content == "keep one\nkeep two\n"


def test_emptying_a_line_without_its_break_keeps_the_line() -> None:
    content = "keep one\nremove this reciept line now\nkeep two\n"

    found = _applied(replace_copied(content, "remove this receipt line now", ""))

    assert found.new_content == "keep one\n\nkeep two\n"


def test_at_eof_requires_the_passage_to_end_the_file() -> None:
    content = "alpha beta gamma reciept\nlast line here\n"
    lines = [("-", "alpha beta gamma receipt"), ("+", "alpha beta gamma receipt delta")]

    assert match_copied_edit(content, lines, at_eof=True) is None
    assert _applied(match_copied_edit(content, lines)).new_content == (
        "alpha beta gamma reciept delta\nlast line here\n"
    )


def test_many_differing_lines_are_listed_briefly() -> None:
    found = FuzzyReplacement(
        "",
        1,
        1,
        1,
        "copied",
        ((0, 0),),
        ((0, 0),),
        differed=tuple((number, f"line {number}") for number in range(1, 6)),
    )

    assert copy_warnings(found, line_shift=10) == [
        "Lines 11, 12, 13, 14, 15 did not match your old text exactly and were edited anyway; "
        "they read:\n11: line 1\n12: line 2\n13: line 3\n(2 more)"
    ]


def test_a_large_file_is_searched_quickly() -> None:
    blocks = [
        f"def handler_{index}(request, context):\n"
        f"    value = request.get('field_{index % 50}')\n"
        "    if value is None:\n"
        "        return None\n"
        "    return context.process(value)\n"
        for index in range(4000)
    ]
    content = "".join(blocks)
    lines = [
        (" ", "def handler_3999(request, context):"),
        (" ", "    value = request.get('field_49')"),
        (" ", "    if value is None:"),
        ("-", "        retrun None"),
        ("+", "        raise ValueError('field_49')"),
        (" ", "    return context.process(value)"),
    ]

    started = time.perf_counter()
    found = _applied(match_copied_edit(content, lines))
    elapsed = time.perf_counter() - started

    assert found.new_content.endswith(
        "    if value is None:\n        raise ValueError('field_49')\n"
        "    return context.process(value)\n"
    )
    assert elapsed < 2.0
