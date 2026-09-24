"""The ``[[perf ...]]`` directive round-trips and rejects scripting mistakes."""

import pytest

from scripts.perf_load_suite.directive import (
    DirectiveError,
    PerfDirective,
    find_directive,
    parse_directive_body,
)


def test_directive_parses_every_field():
    directive = find_directive(
        "prefix [[perf id=L10-s003-t1 steps=4 tokens=400 rate=80 think_ms=600 "
        "tools=read,search_files,bash calls=2]] suffix"
    )

    assert directive == PerfDirective(
        tag="L10-s003-t1",
        steps=4,
        tokens=400,
        rate=80.0,
        think_ms=600,
        tools=("read", "search_files", "bash"),
        calls=2,
    )
    assert directive.tool_rounds == 3
    assert directive.scripted_text_ms() == pytest.approx(600 + 400 / 80 * 1000)


@pytest.mark.parametrize(
    "directive",
    [
        PerfDirective(tag="L1-s000-t1", steps=4, tokens=400, rate=80, think_ms=600),
        PerfDirective(tag="x", steps=2, tools=("bash",), calls=3, rate=12.5),
        PerfDirective(tag="L1-s000-w1", warmup_tokens=20_000, rate=0),
    ],
)
def test_rendered_directive_parses_back_to_itself(directive):
    assert find_directive(f"hello {directive.render()} world") == directive


def test_warmup_directive_streams_unpaced_history():
    directive = find_directive("[[perf id=w warmup_tokens=20000 rate=0]]")

    assert directive is not None
    assert directive.is_warmup
    assert directive.text_tokens == 20_000
    assert directive.scripted_text_ms() == 0


def test_text_without_directive_is_not_scripted():
    assert find_directive("plain text [[other id=x]]") is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("steps=2", "requires id"),
        ("id=x unknown=1", "unknown directive key"),
        ("id=x id=y", "duplicate"),
        ("id=x steps", "key=value"),
        ("id=x steps=two", "integer"),
        ("id=x rate=fast", "number"),
        ("id=x steps=0", "at least 1"),
        ("id=x calls=9", "calls"),
        ("id=x rate=-1", "negative"),
        ("id=bad/tag", "invalid directive id"),
        ("id=x tools=,", "Tool names"),
        ("id=x warmup_tokens=10 steps=2", "warmup"),
    ],
)
def test_malformed_directive_is_rejected(body, message):
    with pytest.raises(DirectiveError, match=message):
        parse_directive_body(body)
