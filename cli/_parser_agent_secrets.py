"""CLI grammar for Agent Secrets, their requests and Secret Sources."""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta
from difflib import get_close_matches
from typing import Any, override

from cli._parser_common import (
    AGENT_SECRET_KINDS,
    AGENT_SECRET_SINKS,
    AGENT_SECRETS_HELP,
    AGENT_SECRETS_SOURCE_HELP,
    AREA_HELP,
    _add_command_parser,
)

_DURATION = re.compile(
    r"(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)", re.IGNORECASE
)
_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_SINK_SPELLINGS = {"env": "environment", "file": "files", "type": "typing"}

_SINK_WORDS = (
    "environment (shell environment variables), terminal (terminal input), files (file "
    "contents), typing (typed text), external_tools (arguments of MCP and Extension Tools)"
)
_VALUE_LINES = (
    "for token and text the whole input, for a login the password on the first line and "
    "an optional TOTP seed on the second"
)


def duration_argument(raw: str) -> timedelta:
    """``30m``, ``8h``, ``7d`` or ``2w``, as the ``/secret`` command takes them."""
    match = _DURATION.fullmatch(raw.strip())
    if match is None or int(match.group(1)) <= 0:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is no duration; use minutes, hours, days or weeks such as 30m, 8h, 7d or 2w"
        )
    unit = _DURATION_UNITS[match.group(2)[0].lower()]
    return timedelta(**{unit: int(match.group(1))})


def time_argument(raw: str) -> datetime:
    """An ISO 8601 time; without an offset, in this computer's time zone."""
    text = raw.strip()
    if _DATE_ONLY.fullmatch(text):
        raise argparse.ArgumentTypeError(
            f"{text} has no time of day; use {text}T00:00 for its start or {text}T23:59 for its end"
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is no ISO 8601 time; use for example 2026-11-01T18:00 or "
            "2026-11-01T18:00+01:00"
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


class _ListArgument(argparse.Action):
    """Collect values given with spaces, commas or a repeated flag; none gives []."""

    def __init__(self, *args: Any, choices_hint: tuple[str, ...] = (), **kwargs: Any) -> None:
        self._choices_hint = choices_hint
        super().__init__(*args, **kwargs)

    @override
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        collected = list(getattr(namespace, self.dest, None) or [])
        for value in values or ():
            for item in str(value).split(","):
                item = item.strip()
                if not item:
                    continue
                if self._choices_hint:
                    item = self._known(item)
                if item not in collected:
                    collected.append(item)
        setattr(namespace, self.dest, collected)

    def _known(self, item: str) -> str:
        spelled = item.lower().replace("-", "_")
        spelled = _SINK_SPELLINGS.get(spelled, spelled)
        if spelled in self._choices_hint:
            return spelled
        matches = get_close_matches(spelled, self._choices_hint, n=1)
        hint = f" Did you mean {matches[0]}?" if matches else ""
        raise argparse.ArgumentError(
            self, f"unknown use {item!r}.{hint} Use: {', '.join(self._choices_hint)}"
        )


def _add_agents(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument(
        "--agent",
        dest="agents",
        action=_ListArgument,
        nargs="*",
        metavar="<agent-id>",
        help=help_text,
    )


def _add_sinks(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument(
        "--sink",
        dest="sinks",
        action=_ListArgument,
        nargs="*",
        metavar="<use>",
        choices_hint=AGENT_SECRET_SINKS,
        help=help_text,
    )


def _add_user(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--user", dest="user", metavar="<username>", help=help_text)
    parser.add_argument("--username", dest="user", help=argparse.SUPPRESS)


def _add_expiry(
    parser: argparse.ArgumentParser, *, omit: str, permanent: str | None = None
) -> None:
    expiry = parser.add_mutually_exclusive_group()
    expiry.add_argument(
        "--for",
        dest="duration",
        type=duration_argument,
        metavar="<duration>",
        help=f"Delete the Secret after this time, such as 30m, 8h, 7d or 2w. {omit}",
    )
    expiry.add_argument(
        "--until",
        type=time_argument,
        metavar="<time>",
        help=(
            "Delete the Secret at this time, ISO 8601 such as 2026-11-01T18:00; without an "
            f"offset, in this computer's time zone. {omit}"
        ),
    )
    if permanent is not None:
        expiry.add_argument("--permanent", action="store_true", help=permanent)


def _add_value_stdin(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--value-stdin", action="store_true", help=help_text)


def _add_agent_secrets_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    area_parser = subparsers.add_parser(
        "agent-secrets",
        help=AREA_HELP["agent-secrets"],
        description=(
            f"{AREA_HELP['agent-secrets']}. Agents write {{{{secret:NAME}}}} where a value "
            "belongs, and vBot inserts it in the places the Secret allows. Credentials vBot "
            "itself uses, such as a token a Skill requires, are vbot data-store secrets."
        ),
    )
    commands = area_parser.add_subparsers(dest="command", required=True)

    _add_command_parser(
        commands, "list", AGENT_SECRETS_HELP["list"], example="agent-secrets list"
    ).epilog = (
        "Each Secret shows its name and id (either one names it in the other commands), "
        "its fields with the values Agents can see, the Agents granted it and where vBot "
        "inserts it (sinks)."
    )

    add = _add_command_parser(
        commands,
        "add",
        AGENT_SECRETS_HELP["add"],
        example="agent-secrets add GITHUB_TOKEN --agent main",
    )
    add.add_argument(
        "name",
        metavar="<NAME>",
        help=(
            "The name Agents write in {{secret:NAME}}, such as GITHUB_TOKEN: letters, digits "
            "and underscores; lower case and dashes become upper case and underscores"
        ),
    )
    add.add_argument(
        "--kind",
        type=str.lower,
        choices=AGENT_SECRET_KINDS,
        help=(
            "What the Secret holds: token for one key or code, login for a username and "
            "password with an optional TOTP seed, text for several lines such as an SSH key. "
            "Omit for token, or for login when --user is given."
        ),
    )
    _add_user(
        add,
        "The login's username, which Agents can see; implies --kind login. Omit for a "
        "login without username.",
    )
    _add_agents(
        add,
        "Agents that can use the Secret, by id as vbot agent list shows it; separate several "
        "with spaces or commas. Omit to grant it to the Agent whose Run runs this command, "
        "outside a Run to no Agent; --agent without an id grants it to none.",
    )
    _add_sinks(
        add,
        f"Where vBot inserts the value for Agents: {_SINK_WORDS}; separate several with "
        "spaces or commas. Omit for the kind's default: token environment and files, login "
        "environment, terminal and typing, text all but external_tools.",
    )
    add.add_argument(
        "--description",
        metavar="<text>",
        help=(
            "What the Secret is for, shown to Agents with its name; one line, at most 500 "
            "characters. Omit for none."
        ),
    )
    _add_expiry(add, omit="Omit to keep it until it is deleted.")
    _add_value_stdin(
        add,
        f"Read the value from piped stdin: {_VALUE_LINES}. Omit in a terminal, where a "
        "hidden prompt asks for it.",
    )
    add.epilog = (
        "The value never goes on the command line, where shell history and process lists "
        "keep it. The output shows the stored Secret without its concealed values. Exit 0 "
        "when stored, 1 when refused, 2 for an invalid call."
    )

    update = _add_command_parser(
        commands,
        "update",
        AGENT_SECRETS_HELP["update"],
        example="agent-secrets update GITHUB_TOKEN --agent main coder",
    )
    _add_secret_target(update)
    _add_agents(
        update,
        "Agents that can use the Secret, by id; replaces the whole list, so name every Agent "
        "to keep (separate with spaces or commas). --agent without an id grants it to none. "
        "Omit to keep the granted Agents.",
    )
    _add_sinks(
        update,
        f"Where vBot inserts the value: {', '.join(AGENT_SECRET_SINKS)}; replaces the whole "
        "list, so name every use to keep. --sink without a use allows it nowhere. Omit to "
        "keep the allowed uses.",
    )
    update.add_argument(
        "--description",
        metavar="<text>",
        help="Replace the description, one line. Omit to keep it.",
    )
    _add_expiry(
        update,
        omit="Omit to keep the expiry.",
        permanent=(
            "Keep the Secret until it is deleted: removes its expiry, and a Session's Secret "
            "becomes one that --agent grants to Agents. Omit to keep the lifetime."
        ),
    )
    _add_user(update, "Replace the login's username. Omit to keep it.")
    _add_value_stdin(
        update,
        "Replace the value: from a hidden prompt in a terminal, else from piped stdin "
        f"({_VALUE_LINES}); an empty prompt or line keeps that field. Omit to keep the value.",
    )
    update.epilog = (
        "Inside an Agent's Run, update does not grant the Secret to more Agents or allow it "
        "in more places; the user makes that change. The output shows the Secret as it is now."
    )

    delete = _add_command_parser(
        commands, "delete", AGENT_SECRETS_HELP["delete"], example="agent-secrets delete GITHUB"
    )
    _add_secret_target(delete)
    delete.epilog = (
        "Deleting cannot be undone. A Secret from a Secret Source loses only its binding; the "
        "entry stays in the source. Exit 0 also when no Secret has this name."
    )

    reveal = _add_command_parser(
        commands,
        "reveal",
        AGENT_SECRETS_HELP["reveal"],
        example="agent-secrets reveal GITHUB --field password",
    )
    _add_secret_target(reveal)
    reveal.add_argument(
        "--field",
        metavar="<field>",
        help=(
            "The field to print, as list shows the fields, such as username, password or "
            "totp (its stored seed, not a code). Omit for a login's password, else its value."
        ),
    )
    reveal.epilog = (
        "Prints only the value. Refused inside an Agent's Run, where vBot shows Secret values "
        "only as their references."
    )

    answer = _add_command_parser(
        commands,
        "answer",
        AGENT_SECRETS_HELP["answer"],
        example="agent-secrets answer <request-id>",
    )
    _add_request_target(answer)
    _add_user(answer, "The login's username, which Agents can see. Omit for none.")
    _add_expiry(
        answer,
        omit="Omit for the lifetime the request suggests, as list shows it.",
        permanent=(
            "Keep the Secret until it is deleted, granted to the requesting Agent. Omit for "
            "the lifetime the request suggests."
        ),
    )
    _add_value_stdin(
        answer,
        f"Read the value from piped stdin: {_VALUE_LINES}. Omit in a terminal, where a "
        "hidden prompt asks for it.",
    )
    answer.epilog = (
        "When the request replaces an existing Secret, only its value changes. Refused inside "
        "an Agent's Run: requests are the user's to answer."
    )

    decline = _add_command_parser(
        commands,
        "decline",
        AGENT_SECRETS_HELP["decline"],
        example="agent-secrets decline <request-id>",
    )
    _add_request_target(decline)
    decline.epilog = "Refused inside an Agent's Run: requests are the user's to answer."

    source = commands.add_parser(
        "source", help=AGENT_SECRETS_HELP["source"], description=AGENT_SECRETS_HELP["source"]
    )
    source_commands = source.add_subparsers(dest="source_command", required=True)
    _add_command_parser(
        source_commands,
        "list",
        AGENT_SECRETS_SOURCE_HELP["list"],
        example="agent-secrets source list",
    )
    unlock = _add_command_parser(
        source_commands,
        "unlock",
        AGENT_SECRETS_SOURCE_HELP["unlock"],
        example="agent-secrets source unlock <source>",
    )
    _add_source_target(unlock)
    _add_value_stdin(
        unlock,
        "Read the unlock fields from piped stdin, one line per field in the order source list "
        "shows them. Omit in a terminal, where hidden prompts ask for them.",
    )
    unlock.epilog = "Refused inside an Agent's Run: only the user unlocks a Secret Source."
    lock = _add_command_parser(
        source_commands,
        "lock",
        AGENT_SECRETS_SOURCE_HELP["lock"],
        example="agent-secrets source lock <source>",
    )
    _add_source_target(lock)
    entries = _add_command_parser(
        source_commands,
        "entries",
        AGENT_SECRETS_SOURCE_HELP["entries"],
        example="agent-secrets source entries <source>",
    )
    _add_source_target(entries)
    entries.add_argument(
        "--container",
        metavar="<name>",
        help="Only the entries of this folder or vault, as the output names them. Omit for all.",
    )
    entries.add_argument(
        "--query",
        metavar="<text>",
        help="Only the entries whose title contains this text, ignoring case. Omit for all.",
    )


def _add_secret_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "secret",
        metavar="<secret>",
        help="The Secret's name as list shows it, such as GITHUB, or its id",
    )


def _add_request_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "request",
        metavar="<request-id>",
        help="The request's id as list shows it, or the name of the Secret it asks for",
    )


def _add_source_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "source", metavar="<source>", help="The Secret Source's name as source list shows it"
    )
