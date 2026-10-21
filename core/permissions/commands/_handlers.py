"""Programs whose arguments need more than a ``ProgramSpec``: find, tar, dd, unzip,
curl and wget, icacls and attrib, git, python, and programs that reach other hosts.

Each handler gets the program's arguments after its name (and, for programs
with ``GLOBAL_OPTIONS``, after those) and records accesses, URLs and hosts on
the ``Analysis``. ``program_outputs`` is the fallback for programs without a
spec: their ``OUTPUT_OPTIONS`` are writes, every other word tier B.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from core.permissions.commands import _git_files, _paths
from core.permissions.commands._specs import (
    ATTACHED_OUTPUT_OPTIONS,
    OUTPUT_OPTIONS,
    POSIX_SHELLS,
    PROGRAMS,
    VENV_MODULES,
    AccessKind,
)
from core.permissions.commands._state import Context, relative_to
from core.permissions.commands._syntax import RawCommand, Word

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis

# (analysis, context, raw, program name, arguments, fed by xargs)
type Handler = Callable[[Analysis, Context, RawCommand, str, Sequence[Word], bool], None]


def _handle_find(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    location = analysis.first(context, raw)
    texts = [analysis.value(context, word, location).text for word in arguments]
    roots: list[Word] = []
    index = 0
    while (
        index < len(arguments) and texts[index] is not None and texts[index] in ("-H", "-L", "-P")
    ):
        index += 1
    while index < len(arguments):
        text = texts[index]
        if text is not None and (text.startswith("-") or text in ("(", "!", ")")):
            break
        roots.append(arguments[index])
        index += 1
    roots = roots or [Word.literal(".", source=raw.source)]
    writes = False
    expression = list(zip(arguments[index:], texts[index:], strict=True))
    position = 0
    while position < len(expression):
        word, text = expression[position]
        position += 1
        if text == "-delete":
            writes = True
        elif text in ("-fprint", "-fprint0", "-fprintf", "-fls"):
            if position < len(expression):
                analysis.access(
                    context, raw, "write", expression[position][0], glob=False, tier="a"
                )
        elif text in ("-exec", "-execdir", "-ok", "-okdir"):
            inner: list[Word] = []
            while position < len(expression) and expression[position][1] not in (";", "+"):
                inner.append(expression[position][0])
                position += 1
            position += 1
            if inner:
                inner_name = analysis.value(context, inner[0], location).text
                if inner_name is None:
                    analysis.unresolve(
                        "command", raw.source, "find -exec runs a command computed when it runs"
                    )
                    continue
                program = _paths.program_name(inner_name).lower()
                spec = PROGRAMS.get(program)
                analysis.emit(
                    context,
                    _paths.program_name(inner_name),
                    [w for w in inner[1:] if analysis.value(context, w, location).text != "{}"],
                    raw,
                )
                if spec is None or "write" in (spec.operands, spec.last) or program in POSIX_SHELLS:
                    writes = True
                others = [w for w in inner[1:] if analysis.value(context, w, location).text != "{}"]
                analysis.tier_b(context, raw, others)
        del word
    found = []
    for root in roots:
        found += analysis.access(context, raw, "read", root, glob=True, tier="a", subtree=True)
        if writes:
            analysis.access(context, raw, "write", root, glob=True, tier="a", subtree=True)
    if raw.pipeline is not None:
        listing = analysis.resolve_listing(context, raw, roots, recursive=True)
        context.listings[(raw.pipeline.id, raw.pipeline.index)] = listing
    del found


def _handle_tar(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    location = analysis.first(context, raw)
    archive: Word | None = None
    directory: Word | None = None
    mode = "list"
    operands: list[Word] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        text = analysis.value(context, word, location).text
        index += 1
        if text is None:
            operands.append(word)
            continue
        letters = ""
        if text.startswith("--"):
            option, _, attached = text.partition("=")
            if option in ("--file", "--directory"):
                value = (
                    Word.literal(attached, source=word.source)
                    if attached
                    else (arguments[index] if index < len(arguments) else None)
                )
                if not attached:
                    index += 1
                if option == "--file":
                    archive = value
                else:
                    directory = value
            elif option in ("--extract", "--get"):
                mode = "extract"
            elif option in ("--create", "--append", "--update", "--concatenate"):
                mode = "create"
            continue
        if text.startswith("-") or (index == 1 and re.match(r"^[A-Za-z]+$", text)):
            letters = text.lstrip("-")
            for position, letter in enumerate(letters):
                if letter == "x":
                    mode = "extract"
                elif letter in "cru":
                    mode = "create"
                elif letter in "fC":
                    rest = letters[position + 1 :]
                    value = (
                        Word.literal(rest, source=word.source)
                        if rest
                        else (arguments[index] if index < len(arguments) else None)
                    )
                    if not rest:
                        index += 1
                    if letter == "f":
                        archive = value
                    else:
                        directory = value
                    break
            continue
        operands.append(word)
    target = directory or Word.literal(".", source=raw.source)
    if mode == "extract":
        if archive is not None:
            analysis.access(context, raw, "read", archive, glob=False, tier="a")
        analysis.access(context, raw, "write", target, glob=False, tier="a", subtree=True)
    elif mode == "create":
        if archive is not None:
            analysis.access(context, raw, "write", archive, glob=False, tier="a")
        for operand in operands:
            analysis.access(
                context,
                raw,
                "read",
                relative_to(operand, directory),
                glob=True,
                tier="a",
                subtree=True,
            )
    elif archive is not None:
        analysis.access(context, raw, "read", archive, glob=False, tier="a")


def _handle_dd(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    location = analysis.first(context, raw)
    for word in arguments:
        text = analysis.value(context, word, location).text
        if text is None:
            if word.source.startswith(("of=", "if=")):
                analysis.unresolve(
                    "path_write" if word.source.startswith("of=") else "path_read",
                    word.source,
                    "the path is computed when it runs",
                )
            continue
        key, equals, value = text.partition("=")
        if equals and key in ("if", "of"):
            analysis.access(
                context,
                raw,
                "read" if key == "if" else "write",
                Word.literal(value, source=word.source),
                glob=False,
                tier="a",
            )


def _handle_unzip(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    location = analysis.first(context, raw)
    texts = [analysis.value(context, word, location).text for word in arguments]
    destination: Word | None = None
    listing_only = False
    archive: Word | None = None
    index = 0
    while index < len(arguments):
        text = texts[index]
        if text == "-d" and index + 1 < len(arguments):
            destination = arguments[index + 1]
            index += 2
            continue
        if text is not None and text.startswith("-"):
            listing_only = (
                listing_only
                or any(letter in text for letter in "lvtZ")
                and not text.startswith("--")
            )
            if text == "-x":
                break
        elif archive is None:
            archive = arguments[index]
        index += 1
    if archive is not None:
        analysis.access(context, raw, "read", archive, glob=True, tier="a")
    if not listing_only:
        analysis.access(
            context,
            raw,
            "write",
            destination or Word.literal(".", source=raw.source),
            glob=False,
            tier="a",
            subtree=True,
        )


def _handle_download(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    """curl and wget: where downloads land and which files they upload."""
    location = analysis.first(context, raw)
    program = name.lower()
    writes = _CURL_WRITES if program == "curl" else _WGET_WRITES
    reads = _CURL_READS if program == "curl" else _WGET_READS
    remote_name = False
    values_seen: set[int] = set()
    index = 0
    while index < len(arguments):
        word = arguments[index]
        text = analysis.value(context, word, location).text
        index += 1
        if text is None or not text.startswith("-"):
            continue
        option, equals, attached = text.partition("=")
        if program == "curl" and not option.startswith("--") and len(option) > 2:
            option, attached, equals = option[:2], option[2:], "="
        if option in ("-O", "--remote-name", "--remote-name-all", "-J"):
            remote_name = True
            continue
        kind: AccessKind | None = (
            "write" if option in writes else "read" if option in reads else None
        )
        if option in _DOWNLOAD_VALUES or kind is not None:
            value = (
                Word.literal(attached, source=word.source)
                if equals
                else (arguments[index] if index < len(arguments) else None)
            )
            if not equals:
                index += 1
            if value is not None and not equals:
                values_seen.add(id(value))
            if kind is None or value is None:
                continue
            text_value = analysis.value(context, value, location).text
            if (
                text_value is not None
                and program == "curl"
                and option in ("-d", "--data", "--data-binary", "--data-urlencode", "-F", "--form")
            ):
                if "@" not in text_value:
                    continue
                text_value = text_value.split("@", 1)[1].split(";", 1)[0]
                value = Word.literal(text_value, source=value.source)
            analysis.access(context, raw, kind, value, glob=False, tier="a")
    if program == "wget" and not any(
        (analysis.value(context, word, location).text or "").startswith(
            ("-O", "--output-document", "-P", "--directory-prefix")
        )
        for word in arguments
    ):
        remote_name = True
    if remote_name:
        analysis.access(
            context, raw, "write", Word.literal(".", source=raw.source), glob=False, tier="a"
        )
    operands = [
        word
        for word in arguments
        if not (analysis.value(context, word, location).text or "-").startswith("-")
        and id(word) not in values_seen
    ]
    for word in operands:
        text = analysis.value(context, word, location).text
        if text is not None:
            add_web_target(analysis, text, word.source)
    analysis.tier_b(context, raw, operands)


def add_web_target(analysis: Analysis, text: str, source: str) -> None:
    """A web program's target without scheme (``curl evil.com/x``) reaches ``http://`` it."""
    if "://" in text or not _SCHEMELESS.match(text):
        return
    analysis.add_url("http://" + text, source)


# host[:port][/path], with a dot, a port, or localhost: what curl and Invoke-WebRequest fetch.
_SCHEMELESS = re.compile(
    r"^(?:[^@/\\\s]+@)?(?:\[[0-9A-Fa-f:.]+\]|localhost|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"|[A-Za-z0-9-]+(?=:\d))(?::\d{1,5})?(?:[/?#]\S*)?$",
    re.IGNORECASE,
)


_CURL_WRITES = frozenset(
    {
        "-o",
        "--output",
        "-c",
        "--cookie-jar",
        "-D",
        "--dump-header",
        "--trace",
        "--trace-ascii",
        "--output-dir",
        "--stderr",
        "--etag-save",
        "--hsts",
        "--alt-svc",
    }
)
_CURL_READS = frozenset(
    {
        "-T",
        "--upload-file",
        "-K",
        "--config",
        "-d",
        "--data",
        "--data-binary",
        "--data-urlencode",
        "-F",
        "--form",
        "--cacert",
        "--cert",
        "--key",
        "--netrc-file",
        "-b",
        "--cookie",
    }
)
_WGET_WRITES = frozenset(
    {
        "-O",
        "--output-document",
        "-P",
        "--directory-prefix",
        "-o",
        "--output-file",
        "-a",
        "--append-output",
        "--save-cookies",
    }
)
_WGET_READS = frozenset(
    {"-i", "--input-file", "--load-cookies", "--post-file", "--body-file", "--ca-certificate"}
)
_DOWNLOAD_VALUES = frozenset(
    {
        "-H",
        "--header",
        "-X",
        "--request",
        "-u",
        "--user",
        "-A",
        "--user-agent",
        "-e",
        "--referer",
        "-m",
        "--max-time",
        "-x",
        "--proxy",
        "-w",
        "--write-out",
        "-r",
        "--range",
        "--connect-timeout",
        "--retry",
        "-U",
        "--proxy-user",
        "--resolve",
        "-t",
        "--tries",
        "-T",
        "--timeout",
        "--password",
        "-Q",
        "--quota",
        "-l",
        "--level",
    }
)


def _handle_acl(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    """icacls and attrib: the files they change; switches, their values and flags are no paths."""
    location = analysis.first(context, raw)
    icacls = name.lower() == "icacls"
    texts = [analysis.value(context, word, location).text for word in arguments]
    recursive = any((text or "").lower() in ("/t", "/s") for text in texts)
    index = 0
    files = 0
    while index < len(arguments):
        word, text = arguments[index], texts[index]
        index += 1
        lowered = (text or "").lower()
        if icacls and lowered in ("/save", "/restore") and index < len(arguments):
            kind: AccessKind = "write" if lowered == "/save" else "read"
            analysis.access(context, raw, kind, arguments[index], glob=False, tier="a")
            index += 1
            continue
        if icacls and lowered.split(":", 1)[0] in _ICACLS_VALUES:
            index += 1
            continue
        if text is not None and (
            text.startswith(("/", "+")) or (text.startswith("-") and not icacls)
        ):
            continue
        if icacls and files:
            continue  # icacls names one file or folder; later words are values
        files += 1
        analysis.access(context, raw, "write", word, glob=True, tier="a", subtree=recursive)


_ICACLS_VALUES = frozenset(
    {"/grant", "/deny", "/remove", "/setintegritylevel", "/setowner", "/findsid", "/substitute"}
)


# git -------------------------------------------------------------------------

# Subcommands that change the repository they work in (its worktree or .git).
_GIT_WRITES = frozenset(
    {
        "add", "am", "apply", "bisect", "checkout", "checkout-index", "cherry-pick", "clean",
        "clone", "commit", "fetch", "filter-branch", "format-patch", "gc", "init", "maintenance",
        "merge", "mv", "notes", "prune", "pull", "read-tree", "rebase", "repack", "replace",
        "reset", "restore", "revert", "rm", "sparse-checkout", "stash", "submodule", "switch",
        "update-index", "worktree",
    }
)  # fmt: skip
_GIT_REMOTES = frozenset({"clone", "fetch", "pull", "push", "ls-remote", "remote", "submodule"})
_GIT_CLONE_VALUES = frozenset(
    {
        "-b", "--branch", "-o", "--origin", "--depth", "--reference", "--reference-if-able",
        "-c", "--config", "--template", "--separate-git-dir", "-u", "--upload-pack", "-j",
        "--jobs", "--filter", "--shallow-since", "--shallow-exclude", "--bundle-uri",
        "--server-option", "--ref-format",
    }
)  # fmt: skip
_GIT_INIT_VALUES = frozenset(
    {
        "--template",
        "--separate-git-dir",
        "-b",
        "--initial-branch",
        "--object-format",
        "--ref-format",
    }
)
_GIT_OUTPUTS = frozenset({"-o", "--output", "--output-directory"})
# [user@]host:path, as git, scp and rsync name a remote.
_SCP_TARGET = re.compile(r"^(?:[^@/\\\s:]+@)?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9][\w.-]*):(?!//)")
_NETWORK_URL = re.compile(
    r"^(?:ssh|git|git\+ssh|ssh\+git|ftp|ftps|sftp|rsync|telnet)://(?:[^@/]+@)?"
    r"(\[[0-9A-Fa-f:.]+\]|[^:/?#]+)",
    re.IGNORECASE,
)


def handle_git(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    """git after its global options: what it changes and reads, clone targets, remote hosts."""
    location = analysis.first(context, raw)
    texts = [analysis.value(context, word, location).text for word in arguments]
    subcommand = (texts[0] or "") if texts else ""
    rest = list(zip(arguments[1:], texts[1:], strict=True))
    operands = _operands(rest, _GIT_CLONE_VALUES | _GIT_INIT_VALUES | _GIT_OUTPUTS)
    consumed: set[int] = set()
    if subcommand in _GIT_REMOTES:
        for word, text in operands:
            if text is not None and add_network_target(analysis, text, word.source):
                consumed.add(id(word))
    if subcommand == "clone":
        remote = operands[0] if operands else None
        targets = operands[1:]
        if targets:
            analysis.access(
                context, raw, "write", targets[0][0], glob=False, tier="a", subtree=True
            )
            consumed.add(id(targets[0][0]))
        else:
            folder = _repository_name(remote[1] if remote is not None else None)
            analysis.access(
                context,
                raw,
                "write",
                Word.literal(folder or ".", source=raw.source),
                glob=False,
                tier="a",
                subtree=folder is not None,
            )
        if remote is not None and remote[1] is not None and _is_local(remote[1]):
            analysis.access(context, raw, "read", remote[0], glob=False, tier="a", subtree=True)
            consumed.add(id(remote[0]))
    elif subcommand == "init":
        target = operands[0][0] if operands else Word.literal(".", source=raw.source)
        analysis.access(context, raw, "write", target, glob=False, tier="a")
        consumed.update(id(word) for word, _ in operands[:1])
    elif subcommand == "worktree" and operands and operands[0][1] == "add" and len(operands) > 1:
        analysis.access(context, raw, "write", operands[1][0], glob=False, tier="a", subtree=True)
        consumed.add(id(operands[1][0]))
    else:
        if subcommand in _GIT_WRITES:
            consumed |= _git_files.changed_files(analysis, context, raw, subcommand, rest)
        consumed |= _git_files.read_files(analysis, context, raw, subcommand, rest)
    consumed |= _output_values(analysis, context, raw, rest, _GIT_OUTPUTS)
    analysis.tier_b(context, raw, [word for word in arguments[1:] if id(word) not in consumed])


def git_writes(arguments: Sequence[str | None]) -> bool:
    """Whether the git subcommand after the global options changes its repository."""
    return bool(arguments) and arguments[0] in _GIT_WRITES


def _repository_name(remote: str | None) -> str | None:
    """The folder ``git clone <remote>`` creates (``https://x/y.git`` -> ``y``)."""
    if not remote:
        return None
    name = re.split(r"[\\/:]", remote.rstrip("\\/"))[-1]
    name = name.removesuffix(".git")
    return name if name and name not in (".", "..") else None


def _is_local(text: str) -> bool:
    if "://" in text:
        return False
    return _SCP_TARGET.match(text) is None or re.match(r"^[A-Za-z]:", text) is not None


# Programs that reach other hosts ----------------------------------------------

_SSH_VALUES = frozenset(
    {
        "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o",
        "-p", "-P", "-Q", "-R", "-S", "-W", "-w", "-B",
    }
)  # fmt: skip
_SSH_READS = frozenset({"-F", "-i"})


def handle_remote_shell(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    """ssh, sftp, telnet, mosh: the host is the first operand; key and config files are reads."""
    location = analysis.first(context, raw)
    index = 0
    consumed: set[int] = set()
    while index < len(arguments):
        word = arguments[index]
        text = analysis.value(context, word, location).text
        index += 1
        if text is None:
            continue
        if text.startswith("-") and len(text) >= 2:
            option = text[:2]
            if option in _SSH_VALUES:
                value = (
                    Word.literal(text[2:], source=word.source)
                    if len(text) > 2
                    else (arguments[index] if index < len(arguments) else None)
                )
                if len(text) == 2:
                    index += 1
                if value is not None:
                    consumed.add(id(value))
                    if option in _SSH_READS:
                        analysis.access(context, raw, "read", value, glob=False, tier="a")
                    elif option == "-J":
                        for hop in (analysis.value(context, value, location).text or "").split(","):
                            add_network_target(analysis, hop, word.source, bare=True)
            continue
        add_network_target(analysis, text, word.source, bare=True)
        consumed.add(id(word))
        break
    analysis.tier_b(context, raw, [word for word in arguments if id(word) not in consumed])


def add_network_target(analysis: Analysis, text: str, source: str, *, bare: bool = False) -> bool:
    """Record the host *text* reaches (``user@host:path``, ``ssh://host``; with *bare* a
    plain ``[user@]host[:port]``); False when it names no remote host."""
    match = _NETWORK_URL.match(text) or _SCP_TARGET.match(text)
    if match is not None and not re.match(r"^[A-Za-z]:[\\/]", text):
        analysis.add_host(match.group(1), source)
        return True
    if bare:
        host = text.rsplit("@", 1)[-1]
        host = re.sub(r":\d+$", "", host) if not host.startswith("[") else host
        if re.match(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9][\w.-]*)$", host):
            analysis.add_host(host, source)
            return True
    return False


# Programs that write where an option says -----------------------------------


def handle_python(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    fed: bool,
) -> None:
    """python: the script it runs is a read; ``-m venv`` and ``-m pip`` write where they say."""
    location = analysis.first(context, raw)
    index = 0
    while index < len(arguments):
        word = arguments[index]
        text = analysis.value(context, word, location).text
        if text is None:
            break
        if text in ("-X", "-W", "--check-hash-based-pycs"):
            index += 2
            continue
        if text == "-c":
            analysis.tier_b(context, raw, arguments[index + 2 :])
            return
        if text.startswith("-m"):
            module_word = (
                Word.literal(text[2:], source=word.source)
                if len(text) > 2
                else (arguments[index + 1] if index + 1 < len(arguments) else None)
            )
            rest = arguments[index + (1 if len(text) > 2 else 2) :]
            module = (
                None if module_word is None else analysis.value(context, module_word, location).text
            )
            if module in VENV_MODULES:
                operands = _operands(
                    [(item, analysis.value(context, item, location).text) for item in rest],
                    frozenset({"-p", "--python", "--prompt", "--upgrade-deps"}),
                )
                for operand, _ in operands:
                    analysis.access(
                        context, raw, "write", operand, glob=False, tier="a", subtree=True
                    )
                return
            if module is not None:
                program_outputs(analysis, context, raw, module.lower(), rest)
                return
            analysis.tier_b(context, raw, rest)
            return
        if text.startswith("-") and text != "-":
            index += 1
            continue
        if text != "-":
            analysis.access(context, raw, "read", word, glob=False, tier="a")
        analysis.tier_b(context, raw, arguments[index + 1 :])
        return
    analysis.tier_b(context, raw, arguments[index:])


def program_outputs(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
) -> None:
    """A program without a spec: its ``OUTPUT_OPTIONS`` are writes, the rest tier B."""
    options = OUTPUT_OPTIONS.get(name, frozenset())
    location = analysis.first(context, raw)
    pairs = [(word, analysis.value(context, word, location).text) for word in arguments]
    consumed = _output_values(analysis, context, raw, pairs, options) if options else set()
    analysis.tier_b(context, raw, [word for word in arguments if id(word) not in consumed])


def _output_values(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    pairs: Sequence[tuple[Word, str | None]],
    options: frozenset[str],
) -> set[int]:
    """Record the values of *options* as writes; the ids of the words they consumed."""
    consumed: set[int] = set()
    index = 0
    while index < len(pairs):
        word, text = pairs[index]
        index += 1
        if text is None:
            continue
        if text == "--":
            break
        option, equals, attached = text.partition("=")
        value: Word | None = None
        if option in options:
            if equals:
                value = Word.literal(attached, source=word.source)
            elif index < len(pairs):
                value = pairs[index][0]
                consumed.add(id(value))
                index += 1
        elif text[:2] in options and text[:2] in ATTACHED_OUTPUT_OPTIONS and len(text) > 2:
            value = Word.literal(text[2:], source=word.source)
        if value is not None:
            consumed.add(id(word))
            # Many name a folder the program fills (pip --target, dotnet -o).
            analysis.access(context, raw, "write", value, glob=False, tier="a", subtree=True)
    return consumed


def _operands(
    pairs: Sequence[tuple[Word, str | None]], values: frozenset[str]
) -> list[tuple[Word, str | None]]:
    """The operands among *pairs*, skipping options and the values of *values*."""
    operands: list[tuple[Word, str | None]] = []
    index = 0
    ended = False
    while index < len(pairs):
        word, text = pairs[index]
        index += 1
        if not ended and text == "--":
            ended = True
            continue
        if not ended and text is not None and text.startswith("-") and text != "-":
            if "=" not in text and text in values:
                index += 1
            continue
        operands.append((word, text))
    return operands


HANDLERS: Mapping[str, Handler] = {
    "icacls": _handle_acl,
    "attrib": _handle_acl,
    "find": _handle_find,
    "tar": _handle_tar,
    "bsdtar": _handle_tar,
    "dd": _handle_dd,
    "unzip": _handle_unzip,
    "curl": _handle_download,
    "wget": _handle_download,
    "git": handle_git,
    "ssh": handle_remote_shell,
    "sftp": handle_remote_shell,
    "mosh": handle_remote_shell,
    "telnet": handle_remote_shell,
}
# Python interpreters, handled by handle_python.
PYTHONS = re.compile(r"^(?:python[\d.]*w?|py|pythonw)$", re.IGNORECASE)
