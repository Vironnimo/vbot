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
        "The scheduler runs every job in order and retries a failed job twice.\n"
        "It reports the failure to the owner after the last retry.\n"
    )
    old = (
        "The scheduler runs every job in order and retries a failed job three times.\n"
        "It reports the failure to the owner after the last retry."
    )

    found = _applied(replace_copied(content, old, old.replace("owner", "team")))

    assert found.new_content == content.replace("owner", "team")
    assert copy_warnings(found) == [
        "Line 1 did not match your old text exactly and was left as it reads: "
        "The scheduler runs every job in order and retries a failed job twice."
    ]


@pytest.mark.parametrize(
    ("held", "copied"),
    [
        ("retries a failed job twice", "retries a failed job three times"),
        ("[--overwrite|--force]", "[--overwrite [--force]]"),
        ("files may remain", "files remain"),
    ],
)
def test_text_a_changed_line_keeps_must_be_copied_up_to_misspellings(
    held: str, copied: str
) -> None:
    # The copy may hold wording its author meant to write: keeping the file's text
    # there would silently drop it.
    line = "The scheduler runs every job in order, {} and it reports the failure to the owner."
    content = line.format(held) + "\n"
    old = line.format(copied)

    assert replace_copied(content, old, old.replace("owner", "team")) is None


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
    content = (
        "    if count > limit and the queue is not empty:\n"
        "        the worker keeps running until it stops\n"
    )
    old = (
        "    if count < limit and the queue is not empty:\n"
        "        the worker keeps running until it stops"
    )

    assert replace_copied(content, old, old.replace("limit", "limit + 1")) is None
    # In a line the edit keeps, the difference only locates the change.
    far = _applied(replace_copied(content, old, old.replace("running", "running now")))
    assert far.new_content == content.replace("running", "running now")
    assert copy_warnings(far) == [
        "Line 1 did not match your old text exactly and was left as it reads: "
        "    if count > limit and the queue is not empty:"
    ]


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
    assert copy_warnings(found) == [
        "Line 1 did not match your old text exactly and was left as it reads: "
        + content.split("\n")[0]
    ]


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


def test_a_kept_line_is_never_placed_on_another_line() -> None:
    # The copy leaves out the blank line under the heading. Placed one line lower,
    # the heading would fall on that blank line and the new block after it.
    content = (
        "# Guide\n\n## Setup\n\n"
        "Install the tools with the package manager and run the setup script once.\n"
        "\n## Usage\n\nRun it.\n"
    )
    lines = [
        ("+", "Read the notes first."),
        ("+", ""),
        (" ", "## Setup"),
        ("-", "Install the tools with the package manager and run the setup script once."),
        ("+", "Install the tools with the package manager and run the setup script twice."),
    ]

    assert match_copied_edit(content, lines) is None


def test_a_copy_joined_across_a_line_break_is_refused() -> None:
    # Placed on the continuation line, the added item would take its indentation.
    content = (
        "- The first item explains the setup and then wraps onto\n"
        "  a second line of text that ends here.\n"
        "- Second item.\n"
    )
    old = "wraps onto a second line of text that ends here."

    assert replace_copied(content, old, old + "\n- Inserted item.") is None


def test_a_kept_line_that_holds_its_file_line_may_add_a_note() -> None:
    # The copy adds a remark to a short comment line; every word of the line is there.
    content = (
        "func run_checks() -> void:\n"
        "\t# Test kill\n"
        "\tenemy.take_damage(enemy.hp + 1.0)\n"
        "\tawait process_frame\n"
        "\tif is_instance_valid(enemy):\n"
        '\t\tprint("FAIL: enemy should be dead")\n'
        "\t\tquit(1)\n"
    )
    lines = [
        (" ", "\t# Test kill (the flash delays it)"),
        (" ", "\tenemy.take_damage(enemy.hp + 1.0)"),
        ("-", "\tawait process_frame"),
        ("-", "\tif is_instance_valid(enemy):"),
        ("-", '\t\tprint("FAIL: enemy should be dead")'),
        ("+", "\tif enemy.hp > 0:"),
        ("+", '\t\tprint("FAIL: enemy hp should be zero")'),
        (" ", "\t\tquit(1)"),
    ]

    found = _applied(match_copied_edit(content, lines))

    assert found.new_content == (
        "func run_checks() -> void:\n"
        "\t# Test kill\n"
        "\tenemy.take_damage(enemy.hp + 1.0)\n"
        "\tif enemy.hp > 0:\n"
        '\t\tprint("FAIL: enemy hp should be zero")\n'
        "\t\tquit(1)\n"
    )
    assert copy_warnings(found) == [
        "Line 2 did not match your old text exactly and was left as it reads: \t# Test kill"
    ]


@pytest.mark.parametrize(
    ("kept", "between"),
    [
        # The copy leaves out the blank line under the rule: placed one line lower,
        # the rule would fall on the blank line and the new heading after the rule.
        ("---", ""),
        # A heading on another heading that shares only its markup.
        ("## Further bugs", "## Notes"),
    ],
)
def test_a_kept_line_is_not_placed_on_a_line_it_does_not_hold(kept: str, between: str) -> None:
    content = (
        f"# Report\n\n{kept}\n{between}\n"
        "No further flows could be tested because the login blocks every page.\n"
    )
    lines = [
        ("+", "## Navigation bug"),
        ("+", ""),
        (" ", kept),
        ("-", "No further flows could be tested because the login blocks every page."),
        ("+", "Flow one is blocked by the login."),
    ]

    assert match_copied_edit(content, lines) is None


def test_words_moved_between_kept_lines_of_the_passage_do_not_matter() -> None:
    # The copy wraps a comment differently; both comment lines stay as the file has them.
    content = (
        "/**\n"
        " * Build the response and classify the reading into its\n"
        " * severity band, then persist the sample to the\n"
        " * cross-restart store. The call is fire-and-forget so\n"
        " * the response is not blocked on disk writes.\n"
        " */\n"
        "export function buildResponse(reading) {\n"
        "  return classify(reading);\n"
        "}\n"
    )
    lines = [
        (" ", " * cross-restart store. The call is fire-and-forget so the"),
        (" ", " * response is not blocked on disk writes."),
        (" ", " */"),
        (" ", "export function buildResponse(reading) {"),
        ("-", "  return classify(reading);"),
        ("+", "  return classify(reading, band);"),
        (" ", "}"),
    ]

    found = _applied(match_copied_edit(content, lines))

    assert found.new_content == content.replace("classify(reading)", "classify(reading, band)")
    assert copy_warnings(found) == [
        "Lines 4, 5 did not match your old text exactly and were left as they read:\n"
        "4:  * cross-restart store. The call is fire-and-forget so\n"
        "5:  * the response is not blocked on disk writes."
    ]


@pytest.mark.parametrize(
    ("content", "lines"),
    [
        # A kept first line whose copy starts with the end of the line above.
        (
            "## Known issues\n"
            "- The shell tool splits arguments at spaces unless the value is quoted, see\n"
            "  the quoting section for the details of this behaviour.\n"
            "- Second rule\n",
            [
                (" ", "see the quoting section for the details of this behaviour."),
                ("+", "- A new rule about quoting."),
            ],
        ),
        # A written line whose copy ends with the first word of the next line: the
        # rewrite may drop that word or write it twice.
        (
            "alpha beta gamma delta epsilon zeta so\n"
            "the response stays unblocked for callers here.\n",
            [
                ("-", "alpha beta gamma delta epsilon zeta so the"),
                ("+", "one two three four five six seven"),
                (" ", "response stays unblocked for callers here."),
            ],
        ),
        # A kept line whose copy holds the first word of the written line below: the
        # kept line stays as the file has it, so the rewrite would drop that word.
        (
            "alpha beta gamma delta epsilon zeta so\n"
            "the response stays unblocked for callers here.\n",
            [
                (" ", "alpha beta gamma delta epsilon zeta so the"),
                ("-", "response stays unblocked for callers here."),
                ("+", "one two three four five six seven"),
            ],
        ),
    ],
)
def test_words_that_continue_another_line_are_refused(
    content: str, lines: list[tuple[str, str]]
) -> None:
    assert match_copied_edit(content, lines) is None


@pytest.mark.parametrize(
    ("held", "copied", "old_word", "new_word"),
    [
        # A hyphen for an em dash.
        (
            "- `profiles` \u2014 map keyed by the normalized server base URL for each user.",
            "- `profiles` - map keyed by the normalized server base URL for each user.",
            "normalized",
            "canonical",
        ),
        # A backslash only the file holds.
        (
            'The note asks „what ran there\\" and names the model fields in the data.',
            'The note asks „what ran there" and names the model fields in the data.',
            "data.",
            "measurement rows.",
        ),
        # A backslash only the copy holds.
        (
            '  "_comment": "Hand layer for the provider that expires 2026-08-31 and goes",',
            '  \\"_comment\\": \\"Hand layer for the provider that expires 2026-08-31 and goes\\",',
            "2026-08-31",
            "2026-09-30",
        ),
    ],
)
def test_differences_that_never_matter_keep_the_file_text(
    held: str, copied: str, old_word: str, new_word: str
) -> None:
    content = f"# Notes\n{held}\n"

    found = _applied(replace_copied(content, copied, copied.replace(old_word, new_word)))

    assert found.new_content == f"# Notes\n{held.replace(old_word, new_word)}\n"
    assert copy_warnings(found) == [
        f"Line 2 did not match your old text exactly and was edited anyway; it read: {held}"
    ]


@pytest.mark.parametrize(
    ("held", "copied"),
    [
        # Inside a word, at different places on the two sides.
        (
            "Die Fenster (Neustart, P1) entste\u200bhen **in** dieser Woche nach dem Umbau.",
            "Die Fenster (Neustart, P1) entst\u200behen **in** dieser Woche nach dem Umbau.",
        ),
        # Only the file holds them, between words and on both sides of the change.
        (
            "Die Fenster (Neustart, P1) entstehen **in** dieser \u200dWoche\u2060 nach dem Umbau.",
            "Die Fenster (Neustart, P1) entstehen **in** dieser Woche nach dem Umbau.",
        ),
        # Only the copy holds them, at its start and end.
        (
            "Die Fenster (Neustart, P1) entstehen **in** dieser Woche nach dem Umbau.",
            "\ufeffDie Fenster (Neustart, P1) entstehen **in** dieser Woche nach dem Umbau.\u200c",
        ),
    ],
)
def test_zero_width_characters_are_invisible_to_the_copy(held: str, copied: str) -> None:
    content = f"# Notes\n{held}\n"

    found = _applied(replace_copied(content, copied, copied.replace("Woche", "Stunde")))

    assert found.new_content == content.replace("Woche", "Stunde")
    assert copy_warnings(found) == []


def test_invisible_characters_written_beside_a_change_are_refused() -> None:
    held = "Die Fenster (Neustart, P1) entstehen **in** dieser Woche nach dem Umbau."
    content = f"# Notes\n{held}\n"

    # Kept text brings the file's invisible characters there, so the caller's have no place.
    assert replace_copied(content, held, held.replace("Woche", "\u200bStunde")) is None


@pytest.mark.parametrize(
    ("held", "copied"),
    [
        # Markup the copy lacks, a list marker and a table cell border it adds.
        (
            "confirm the server predates the patch** (check the start time)",
            "confirm the server predates the patch (check the start time)",
        ),
        (
            "Worker states: off, starting, listening and sending",
            "- Worker states: off, starting, listening and sending",
        ),
        (
            "the run stays open after the last control run.",
            "the run stays open after the last control run. |",
        ),
    ],
)
def test_markup_a_changed_line_keeps_must_match_the_file(held: str, copied: str) -> None:
    line = "{} Then the owner decides what comes next for the whole team."
    content = line.format(held) + "\n"
    old = line.format(copied)

    assert replace_copied(content, old, old.replace("owner", "lead")) is None


@pytest.mark.parametrize(
    ("held", "copied", "old_part", "new_part"),
    [
        # The copy escapes quotes the file does not: its new quotes would carry the escape.
        (
            '  "_comment": "Hand layer for the provider that expires soon and goes",',
            '  \\"_comment\\": \\"Hand layer for the provider that expires soon and goes\\",',
            "expires soon",
            'expires \\"soon\\"',
        ),
        # The file escapes quotes the copy does not: new quotes would lack the escape.
        (
            'The note asks „what ran there\\" and names the model fields in the data.',
            'The note asks „what ran there" and names the model fields in the data.',
            "the data.",
            'the "data".',
        ),
        # A backslash only the file holds would escape the new text beside it.
        (
            'The rule says „write\\" and names the model fields in the data table.',
            'The rule says „write" and names the model fields in the data table.',
            'write"',
            "write'",
        ),
    ],
)
def test_text_written_where_the_copy_escapes_differently_is_refused(
    held: str, copied: str, old_part: str, new_part: str
) -> None:
    content = f"# Notes\n{held}\n"

    assert replace_copied(content, copied, copied.replace(old_part, new_part)) is None


def test_a_new_line_may_hold_a_sign_the_file_escapes_on_another_line() -> None:
    held = 'The note asks „what ran there\\" and names the model fields in the data.'
    copied = 'The note asks „what ran there" and names the model fields in the data.'
    added = 'The next note asks „what failed" and names the rows it read.'
    content = f"# Notes\n{held}\n"

    found = _applied(replace_copied(content, copied, f"{copied}\n{added}"))

    assert found.new_content == f"# Notes\n{held}\n{added}\n"
    assert copy_warnings(found) == [
        f"Line 2 did not match your old text exactly and was left as it reads: {held}"
    ]


def test_overlapping_candidates_place_one_passage() -> None:
    tail = "epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon"
    content = f"Intro gamma delta gamma delta {tail} end.\n"
    old = "gamma delta " + tail.replace("theta", "thta").replace("zeta", "zed")

    found = _applied(
        replace_copied(content, old, old.replace("epsilon zed eta thta iota kappa", "a new text"))
    )

    assert found.new_content == (
        "Intro gamma delta gamma delta a new text lambda mu nu xi omicron pi rho sigma tau "
        "upsilon end.\n"
    )


def test_a_long_differing_line_is_shown_from_its_first_difference() -> None:
    words = " ".join(f"word{n}" for n in range(60))
    content = f"Intro {words} and the scheduler retries twice here.\n"
    old = f"Intro {words} and the schedluer retries twice here."

    found = _applied(replace_copied(content, old, old.replace("twice", "once")))

    assert copy_warnings(found)[0] == (
        "Line 1 did not match your old text exactly and was edited anyway; it read: "
        "...word54 word55 word56 word57 word58 word59 and the scheduler retries twice here."
    )


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
    kept = FuzzyReplacement(
        "", 1, 1, 1, "copied", ((0, 0),), ((0, 0),), kept_differed=((2, "a"), (4, "b"))
    )
    assert copy_warnings(kept) == [
        "Lines 2, 4 did not match your old text exactly and were left as they read:\n2: a\n4: b"
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


def test_a_long_fragment_in_repetitive_lines_is_searched_quickly() -> None:
    vocabulary = [
        "the",
        "a",
        "skill",
        "step",
        "run",
        "check",
        "file",
        "agent",
        "tool",
        "when",
        "then",
        "and",
        "or",
        "with",
        "for",
    ]
    lines = [
        f"- item{number}: "
        + " ".join(vocabulary[(number * 7 + index * 3) % len(vocabulary)] for index in range(120))
        for number in range(100)
    ]
    content = "\n".join(lines) + "\n"
    old = " ".join(lines[50].split()[4:44]).replace("skill", "skil", 1)

    started = time.perf_counter()
    found = replace_copied(content, old, old + " more")
    elapsed = time.perf_counter() - started

    # Every line holds the same words in turn, so the copy resembles many places.
    assert isinstance(found, AmbiguousFuzzyMatch)
    assert elapsed < 2.0
