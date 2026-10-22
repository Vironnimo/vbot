"""Tests for the ``/secret`` grammar (``core.secrets.commands``)."""

from __future__ import annotations

import pytest

from core.secrets.commands import SecretCommandError, parse_secret_command


@pytest.mark.parametrize(
    ("arguments", "name", "value", "lifetime"),
    [
        # Without an option no lifetime is chosen: a replaced value keeps its own.
        ("GITHUB ghp_abc def", "GITHUB", "ghp_abc def", None),
        # Any whitespace ends the name and each option, line breaks included.
        ("GITHUB\n--session ghp_abc def", "GITHUB", "ghp_abc def", "session"),
        ("GITHUB\t--once\nghp_abc", "GITHUB", "ghp_abc", "once"),
        ("GITHUB --for\n2h\n  ghp_abc", "GITHUB", "ghp_abc", "until"),
        ("GITHUB -- --starts-with-dashes", "GITHUB", "--starts-with-dashes", None),
    ],
)
def test_secret_command_reads_name_options_and_value(
    arguments: str, name: str, value: str, lifetime: str | None
) -> None:
    parsed = parse_secret_command(arguments)

    assert (parsed.name, parsed.value, parsed.lifetime) == (name, value, lifetime)


@pytest.mark.parametrize(
    "arguments",
    ["GH --hunter2-very-secret", "GH --for hunter2-very-secret", "GH --once --hunter2-very-secret"],
)
def test_secret_command_errors_never_repeat_what_may_be_the_value(arguments: str) -> None:
    with pytest.raises(SecretCommandError) as raised:
        parse_secret_command(arguments)

    assert "hunter2" not in str(raised.value)
