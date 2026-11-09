"""Host paths exposed by Docker run/create bind mounts, without container paths."""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.permissions.commands import _paths
from core.permissions.commands._state import Context
from core.permissions.commands._syntax import RawCommand, Word

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis


_FLAGS = frozenset(
    {
        "--detach",
        "--disable-content-trust",
        "--help",
        "--init",
        "--interactive",
        "--no-healthcheck",
        "--oom-kill-disable",
        "--privileged",
        "--publish-all",
        "--quiet",
        "--read-only",
        "--rm",
        "--sig-proxy",
        "--tty",
        "--use-api-socket",
    }
)
_SHORT_FLAGS = frozenset("ditPq")
_SHORT_VOLUME = re.compile(r"^-[ditPq]*v(.*)$")


def handle_docker(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    """Inspect mount options before the image; retain tier B for other arguments."""
    location = analysis.first(context, raw)
    texts = [analysis.value(context, word, location).text for word in arguments]
    start = 1 if texts[:1] in (["run"], ["create"]) else 0
    if texts[:2] in (["container", "run"], ["container", "create"]):
        start = 2
    consumed: set[int] = set()
    index = start
    while start and index < len(arguments):
        text = texts[index]
        if text is None or not text.startswith("-") or text == "--":
            break  # The image and its argv are outside Docker's option parser.
        word = arguments[index]
        index += 1
        option, equals, attached = text.partition("=")
        volume = _SHORT_VOLUME.fullmatch(text)
        if volume:
            option, attached = "-v", volume[1].removeprefix("=")
            equals = "=" if attached else ""
        if option in ("-v", "--volume", "--mount"):
            value = (
                word.part(attached)
                if equals
                else (arguments[index] if index < len(arguments) else None)
            )
            consumed.add(index - 1)
            if not equals and value is not None:
                consumed.add(index)
                index += 1
            if value is not None:
                _mount(analysis, context, raw, value, structured=option == "--mount")
            continue
        if (
            equals
            or option in _FLAGS
            or (
                option.startswith("-")
                and not option.startswith("--")
                and set(option[1:]) <= _SHORT_FLAGS
            )
        ):
            continue
        # Other run/create options take a value. Attached short-option values
        # consume only this word, such as -p8080:80 or -eKEY=value.
        if not option.startswith("--") and len(option) > 2:
            continue
        if index < len(arguments) and not (texts[index] or "").startswith("-"):
            index += 1
    analysis.tier_b(
        context,
        raw,
        [word for index, word in enumerate(arguments) if index not in consumed],
        mentions=True,
    )
    if fed:
        analysis.pipeline_targets(context, raw, "read")


def _mount(
    analysis: Analysis, context: Context, raw: RawCommand, word: Word, *, structured: bool
) -> None:
    text = analysis.literal_of(context, raw, word)
    if text is None:
        analysis.unresolve("path_write", word.source, "the mount source is computed when it runs")
        return
    readonly = False
    creates_source = not structured
    if structured:
        try:
            fields = next(csv.reader([text], strict=True))
        except csv.Error, StopIteration:
            analysis.unresolve("path_write", word.source, "the mount source cannot be resolved")
            return
        options: dict[str, str] = {}
        for field in fields:
            key, _, value = field.partition("=")
            key = key.lower()
            # Docker processes these aliases in order; a later alias replaces
            # the earlier value even when its spelling differs.
            key = {"src": "source", "ro": "readonly"}.get(key, key)
            options[key] = value
        if options.get("type", "").lower() != "bind":
            return  # Named volumes and tmpfs do not name a host filesystem path.
        source = options.get("source")
        if not source:
            return
        readonly = "readonly" in options and options["readonly"].lower() in ("", "true", "t", "1")
        readonly = readonly and options.get("bind-recursive") != "writable"
        creates_source = "bind-create-src" in options and options["bind-create-src"].lower() in (
            "",
            "true",
            "t",
            "1",
        )
    else:
        # A Windows drive colon is part of the source, not the mount separator.
        begin = 2 if re.match(r"^[A-Za-z]:", text) else 0
        separator = text.find(":", begin)
        if separator < 0:
            return  # An anonymous volume names only a container destination.
        source = text[:separator]
        flags = text.rsplit(":", 1)[-1].split(",")
        readonly = "ro" in flags and not ({"rw", "z", "Z"} & set(flags))
        if not (
            re.search(r"[\\/]", source) or re.match(r"^[A-Za-z]:", source) or source in (".", "..")
        ):
            return  # Docker volume name, not a bind source.
    target = word.part(source)
    if _paths.WINDOWS and source.startswith(("/", "\\")) and not source.startswith(("//", "\\\\")):
        # Docker on Windows can address a Linux daemon: /data is not proof of
        # a Windows host path. Keep denies/protected-location checks as a mention.
        # This is a source, never a /switch; the equivalent Windows spelling
        # lets even a one-segment source retain deny-only mention checks.
        target = word.part(source.replace("/", "\\"))
        analysis.access(context, raw, "read", target, glob=False, tier="b", mention=True)
    else:
        if readonly and creates_source:
            # -v creates a missing host directory, even with :ro. --mount does
            # so only with bind-create-src. Creation does not write its subtree.
            analysis.access(context, raw, "write", target, glob=False, tier="a")
        analysis.access(
            context,
            raw,
            "read" if readonly else "write",
            target,
            glob=False,
            tier="a",
            subtree=True,
        )
