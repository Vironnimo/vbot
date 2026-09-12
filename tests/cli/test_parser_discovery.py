"""Command aliases and recovery preserve dispatch and exact target arguments."""

import pytest

from cli.parser import AREA_ALIASES, parse_args


@pytest.mark.parametrize("alias,canonical", AREA_ALIASES.items())
def test_collection_aliases_dispatch_identically(alias, canonical):
    target = ["list", "--host", "192.0.2.10", "--port", "9000"]
    if canonical == "session":
        target.insert(1, "test-agent")
    assert vars(parse_args([alias, *target])) == vars(parse_args([canonical, *target]))


@pytest.mark.parametrize("command", ["connect", "connect-status", "disconnect"])
def test_oauth_target_is_positional_and_connection_optional(command):
    args = parse_args(["providers", command, "openai", "--account", "work"])
    assert (args.area, args.command, args.provider, args.connection, args.account) == (
        "provider",
        command,
        "openai",
        None,
        "work",
    )


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["provder", "list"], "provider"),
        (["server", "restat"], "restart"),
        (["--server", "192.0.2.10", "--restart"], "vbot server restart"),
    ],
)
def test_invalid_commands_suggest_without_dispatch(tokens, expected, capsys):
    with pytest.raises(SystemExit) as failure:
        parse_args(tokens)
    assert failure.value.code == 2
    error = capsys.readouterr().err
    assert expected in error
    assert "--help" in error


def test_options_cannot_be_abbreviated():
    with pytest.raises(SystemExit) as failure:
        parse_args(["server", "restart", "--po", "9000"])
    assert failure.value.code == 2
