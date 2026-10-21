"""The rule language of Permission Rules: parts, bases, rule strings and resolution.

The stored form is an object with optional parts ``read``, ``write``,
``commands``, ``web`` (bases) and ``allow``, ``deny`` (rule strings such as
``Read({workdir}/docs/**)``). One strict normalizer validates every stored form
and returns the normalized value that writers persist.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast, override
from urllib.parse import urlsplit

from core.permissions._paths import DRIVE_RELATIVE_PATH, split_lexical
from core.utils.errors import VBotError

type RuleKind = Literal["Read", "Edit", "Shell", "Web"]
type ReadBase = Literal["anywhere", "work_area"]
type WriteBase = Literal["anywhere", "work_area", "scratch"]
type CommandsBase = Literal["any", "listed"]
type WebBase = Literal["any", "listed"]

RULE_KINDS: Final[tuple[RuleKind, ...]] = ("Read", "Edit", "Shell", "Web")
PATH_RULE_KINDS: Final[frozenset[str]] = frozenset({"Read", "Edit"})
PLACEHOLDERS: Final[tuple[str, ...]] = ("workdir", "project", "scratch", "workspace", "home")
PARTS: Final[tuple[str, ...]] = ("read", "write", "commands", "web", "allow", "deny")
READ_BASES: Final[tuple[ReadBase, ...]] = ("anywhere", "work_area")
WRITE_BASES: Final[tuple[WriteBase, ...]] = ("anywhere", "work_area", "scratch")
COMMANDS_BASES: Final[tuple[CommandsBase, ...]] = ("any", "listed")
WEB_BASES: Final[tuple[WebBase, ...]] = ("any", "listed")
_BASES: Final[dict[str, tuple[str, ...]]] = {
    "read": READ_BASES,
    "write": WRITE_BASES,
    "commands": COMMANDS_BASES,
    "web": WEB_BASES,
}

_KINDS: Final[dict[str, RuleKind]] = {kind.lower(): kind for kind in RULE_KINDS}
# Kinds other harnesses use (Claude Code, OpenCode), saved as the vBot kind.
_ALIASES: Final[dict[str, RuleKind]] = {
    "write": "Edit",
    "multiedit": "Edit",
    "notebookedit": "Edit",
    "glob": "Read",
    "grep": "Read",
    "ls": "Read",
    "list": "Read",
    "bash": "Shell",
    "powershell": "Shell",
    "webfetch": "Web",
}
_RULE = re.compile(r"\s*([A-Za-z]+)\s*(?:\((.*)\))?\s*\Z", re.DOTALL)
_PLACEHOLDER_AT_START = re.compile(r"\{([^{}/\\]*)\}")
_ANY_PLACEHOLDER = re.compile(r"\{([A-Za-z_ ]+)\}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HOST_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")


class PermissionRulesError(VBotError, ValueError):
    """A stored or submitted rule set, rule string or ceiling is invalid."""


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """One normalized rule: its kind and its pattern, as in ``Read(C:/docs/**)``."""

    kind: RuleKind
    pattern: str

    @property
    def text(self) -> str:
        return f"{self.kind}({self.pattern})"

    @override
    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class PermissionRules:
    """An Agent's rule set; a part that is ``None`` is not set at this level.

    An instance, even one with no part set, means restricted; the absence of a
    rule set (``None`` where one is expected) means full access.
    """

    read: ReadBase | None = None
    write: WriteBase | None = None
    commands: CommandsBase | None = None
    web: WebBase | None = None
    allow: tuple[PermissionRule, ...] | None = None
    deny: tuple[PermissionRule, ...] | None = None


@dataclass(frozen=True, slots=True)
class RuleNormalization:
    """The outcome of normalizing one rule string for an editor or a paste import.

    ``rule`` is the normalized rule, or ``None`` with ``error`` saying why the text
    is no rule. ``note`` says how an accepted text was rewritten, such as an alias
    of another harness.
    """

    rule: PermissionRule | None
    note: str | None = None
    error: str | None = None


class _RuleProblemError(Exception):
    pass


def normalize_permission_rules(
    value: object,
    *,
    notes: list[str] | None = None,
    platform: str = sys.platform,
) -> PermissionRules:
    """Validate and normalize a stored or submitted rule set.

    Raises :class:`PermissionRulesError` naming the part, the value and what is
    accepted. Every rule string an alias or other spelling rewrote adds a sentence
    to ``notes``. ``None`` parts count as not set.
    """
    if isinstance(value, PermissionRules):
        return value
    if not isinstance(value, Mapping):
        raise PermissionRulesError(
            "permission_rules must be an object with the parts read, write, commands, "
            f"web, allow and deny; received {_kind_of(value)}."
        )
    unknown = [key for key in value if key not in PARTS]
    if unknown:
        raise PermissionRulesError(
            f"permission_rules has no part {_quote(unknown[0])}. "
            "Use read, write, commands, web, allow or deny."
        )
    bases = {part: _base(part, value.get(part)) for part in _BASES}
    lists = {
        part: _rule_list(part, value.get(part), notes=notes, windows=platform == "win32")
        for part in ("allow", "deny")
    }
    return PermissionRules(
        read=cast("ReadBase | None", bases["read"]),
        write=cast("WriteBase | None", bases["write"]),
        commands=cast("CommandsBase | None", bases["commands"]),
        web=cast("WebBase | None", bases["web"]),
        allow=lists["allow"],
        deny=lists["deny"],
    )


def normalize_permission_rule(text: object, *, platform: str = sys.platform) -> RuleNormalization:
    """Normalize one rule string, such as one pasted from another harness."""
    if not isinstance(text, str):
        return RuleNormalization(None, error=f"A rule is a string; received {_kind_of(text)}.")
    try:
        rule, note = _parse_rule(text, windows=platform == "win32")
    except _RuleProblemError as problem:
        return RuleNormalization(None, error=f"{_quote(text.strip())}: {problem}")
    return RuleNormalization(rule, note=note)


def parse_permission_rule(text: str, *, platform: str = sys.platform) -> PermissionRule:
    """The normalized rule for ``text``; raises :class:`PermissionRulesError`."""
    outcome = normalize_permission_rule(text, platform=platform)
    if outcome.rule is None:
        raise PermissionRulesError(outcome.error or f"{_quote(text)} is no rule.")
    return outcome.rule


def permission_rules_to_dict(rules: PermissionRules) -> dict[str, object]:
    """The normalized stored form: only the parts that are set, rules as strings."""
    document: dict[str, object] = {}
    for part in ("read", "write", "commands", "web"):
        base = getattr(rules, part)
        if base is not None:
            document[part] = base
    for part in ("allow", "deny"):
        listed = getattr(rules, part)
        if listed is not None:
            document[part] = [rule.text for rule in listed]
    return document


def resolve_rule_set(*levels: PermissionRules | None) -> PermissionRules | None:
    """Resolve rule sets per part; callers pass the most specific level first.

    For each part the first level that sets it wins. Without any rule set the
    result is ``None`` (full access); a level without any part set still makes
    the result restricted.
    """
    present = [level for level in levels if level is not None]
    if not present:
        return None

    def first(part: str) -> object:
        for level in present:
            value = getattr(level, part)
            if value is not None:
                return value
        return None

    return PermissionRules(
        read=cast("ReadBase | None", first("read")),
        write=cast("WriteBase | None", first("write")),
        commands=cast("CommandsBase | None", first("commands")),
        web=cast("WebBase | None", first("web")),
        allow=cast("tuple[PermissionRule, ...] | None", first("allow")),
        deny=cast("tuple[PermissionRule, ...] | None", first("deny")),
    )


def normalize_host(host: str) -> str:
    """The comparison form of a host an access names: lowercase, IDNA, no brackets."""
    text = host.strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return ipaddress.ip_address(text).compressed
    except ValueError:
        pass
    import idna

    try:
        return idna.encode(text, uts46=True).decode("ascii")
    except idna.IDNAError, UnicodeError:
        return text


def host_matches(pattern: str, host: str) -> bool:
    """Whether a normalized host pattern covers a normalized host."""
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    if _is_ip(pattern):
        return host == pattern
    return host == pattern or host.endswith("." + pattern)


def validated_base(part: str, value: object) -> str:
    """A base value that must be present (ceilings store every base)."""
    base = _base(part, value)
    if base is None:
        raise PermissionRulesError(
            f"{part} must be one of {_choices(_BASES[part])}; received nothing."
        )
    return base


def _base(part: str, value: object) -> str | None:
    if value is None:
        return None
    accepted = _BASES[part]
    if isinstance(value, str):
        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        if text in accepted:
            return text
    raise PermissionRulesError(
        f"permission_rules.{part} must be {_choices(accepted)}; received {_shown(value)}."
    )


def _rule_list(
    part: str, value: object, *, notes: list[str] | None, windows: bool
) -> tuple[PermissionRule, ...] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, Iterable) or isinstance(value, Mapping):
        raise PermissionRulesError(
            f"permission_rules.{part} must be a list of rule strings such as "
            f'"Read({{workdir}}/docs/**)"; received {_kind_of(value)}.'
        )
    rules: list[PermissionRule] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise PermissionRulesError(
                f"permission_rules.{part}[{index}] must be a rule string such as "
                f'"Shell(git status)"; received {_kind_of(item)}.'
            )
        try:
            rule, note = _parse_rule(item, windows=windows)
        except _RuleProblemError as problem:
            raise PermissionRulesError(
                f"permission_rules.{part}[{index}] {_quote(item.strip())}: {problem}"
            ) from None
        if note is not None and notes is not None:
            notes.append(note)
        if rule not in rules:
            rules.append(rule)
    return tuple(rules)


def _parse_rule(text: str, *, windows: bool) -> tuple[PermissionRule, str | None]:
    match = _RULE.match(text)
    if match is None:
        raise _RuleProblemError(
            "a rule is Kind(pattern), such as Read({workdir}/docs/**), Edit(src/**), "
            "Shell(git status) or Web(example.com)."
        )
    written_kind, inner = match.group(1), match.group(2)
    kind = _KINDS.get(written_kind.lower()) or _ALIASES.get(written_kind.lower())
    if kind is None:
        raise _RuleProblemError(f"unknown kind {written_kind}. Use Read, Edit, Shell or Web.")
    pattern_text = (inner or "").strip()
    # Another harness's spelling is reported; a normalized separator or space is not.
    foreign = written_kind.lower() not in _KINDS or not pattern_text
    if not pattern_text:
        if kind in PATH_RULE_KINDS:
            raise _RuleProblemError(
                f"{kind} needs a path pattern, such as {kind}({{workdir}}/docs/**)."
            )
        pattern_text = "*"
    if kind in PATH_RULE_KINDS:
        pattern = _path_pattern(pattern_text, windows=windows)
        foreign = foreign or (pattern_text.startswith("//") and not pattern.startswith("//"))
    elif kind == "Shell":
        pattern = _command_pattern(pattern_text)
        foreign = foreign or pattern_text.endswith(":*")
    else:
        pattern = _host_pattern(pattern_text)
        lowered = pattern_text.lower()
        foreign = foreign or lowered.startswith("domain:") or "://" in lowered
    rule = PermissionRule(kind, pattern)
    note = f"{text.strip()} was saved as {rule.text}." if foreign else None
    return rule, note


def _path_pattern(text: str, *, windows: bool) -> str:
    if _CONTROL.search(text):
        raise _RuleProblemError("a path pattern cannot contain control characters.")
    if text.startswith("//") and not text.startswith(("///", "//?/", "//./")):
        # Claude Code writes an absolute path as //path, a Windows drive as //c/.
        rest = text[2:]
        drive = re.match(r"([A-Za-z])(/|$)", rest)
        if windows and drive is not None:
            text = f"{drive.group(1).upper()}:/{rest[2:]}"
        elif not windows:
            text = "/" + rest
    lead = ""
    placeholder = _PLACEHOLDER_AT_START.match(text)
    if placeholder is not None:
        name = placeholder.group(1).strip().lower()
        if name not in PLACEHOLDERS:
            raise _RuleProblemError(_unknown_placeholder(placeholder.group(1)))
        lead = "{" + name + "}"
        text = text[placeholder.end() :]
        if text and text[0] not in "/\\":
            raise _RuleProblemError(f"{lead} must end the pattern or be followed by /.")
    elif text == "~" or text.startswith("~/") or (windows and text.startswith("~\\")):
        lead = "~"
        text = text[1:]
    stray = _ANY_PLACEHOLDER.search(text)
    if stray is not None:
        name = stray.group(1).strip().lower()
        if name in PLACEHOLDERS:
            raise _RuleProblemError(f"{{{name}}} can only start a pattern.")
        raise _RuleProblemError(_unknown_placeholder(stray.group(1)))
    if lead:
        lexical = split_lexical(text.lstrip("/\\"), windows=windows)
        if lexical.unsafe is not None:
            raise _RuleProblemError(f"{lead} must be followed by a relative path, not {text}.")
        return lead + ("/" + lexical.text if lexical.segments else "")
    lexical = split_lexical(text, windows=windows)
    if lexical.unsafe == DRIVE_RELATIVE_PATH:
        raise _RuleProblemError("a drive needs a slash after its colon, as in C:/folder.")
    return lexical.text


def _command_pattern(text: str) -> str:
    if _CONTROL.search(text.replace("\t", " ")):
        raise _RuleProblemError("a command pattern is one line without control characters.")
    command = " ".join(text.split())
    if command.endswith(":*"):
        # Claude Code's older prefix syntax ``npm run test:*``.
        command = command[:-2].rstrip() + " *"
    return command


def _host_pattern(text: str) -> str:
    host = text.strip().lower()
    if host.startswith("domain:"):
        host = host[len("domain:") :].strip()
    if "://" in host:
        host = urlsplit(host).hostname or ""
    if host == "*":
        return "*"
    wildcard = host.startswith("*.")
    body = host[2:] if wildcard else host
    body = body.rstrip(".")
    if not body:
        raise _RuleProblemError("Web needs a host, such as Web(example.com) or Web(*.example.com).")
    if "*" in body:
        raise _RuleProblemError("a * can only start a host, as in Web(*.example.com).")
    unbracketed = body[1:-1] if body.startswith("[") and body.endswith("]") else body
    if _is_ip(unbracketed):
        if wildcard:
            raise _RuleProblemError("an IP address cannot have a *. prefix.")
        return ipaddress.ip_address(unbracketed).compressed
    if "/" in body or ":" in body or _CONTROL.search(body) or " " in body:
        raise _RuleProblemError(
            "Web takes a host without scheme, port or path, such as Web(example.com)."
        )
    import idna

    try:
        encoded = idna.encode(body, uts46=True, std3_rules=True).decode("ascii")
    except idna.IDNAError, UnicodeError:
        raise _RuleProblemError(f"{body} is not a valid host name.") from None
    if len(encoded) > 253 or any(_HOST_LABEL.match(label) is None for label in encoded.split(".")):
        raise _RuleProblemError(f"{body} is not a valid host name.")
    return ("*." if wildcard else "") + encoded


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _unknown_placeholder(name: str) -> str:
    known = ", ".join("{" + placeholder + "}" for placeholder in PLACEHOLDERS)
    return f"unknown placeholder {{{name}}}. Use {known} or ~."


def _choices(values: tuple[str, ...]) -> str:
    quoted = [f'"{value}"' for value in values]
    return " or ".join(quoted) if len(quoted) <= 2 else ", ".join(quoted[:-1]) + f" or {quoted[-1]}"


def _quote(text: str) -> str:
    return f'"{text}"'


def _shown(value: object) -> str:
    return f'"{value}"' if isinstance(value, str) else _kind_of(value)


def _kind_of(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, Mapping):
        return "an object"
    if isinstance(value, (list, tuple)):
        return "a list"
    return type(value).__name__
