"""Discovery and parsing of plaintext credential files and recorded import sources."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.secrets.store import (
    CREDENTIAL_NAME_PATTERN,
    CREDENTIALS,
    ENTRY_NAME_PATTERN,
    OAUTH_TOKENS,
    QUARANTINE_NAME,
    SECRET_STORE_FILE_NAME,
    Section,
    credential_name_key,
)
from core.utils.file_status import is_link_status, stat_or_none

ENV_FILE_PATH = ".env"

OAUTH_DIRECTORY = "oauth"

OAUTH_TOKEN_PATTERN = "oauth/*.json"

#: The plaintext credential files of a data directory before the Secret Store.
PLAINTEXT_CREDENTIAL_PATTERNS: tuple[str, ...] = (ENV_FILE_PATH, OAUTH_TOKEN_PATTERN)

# The file stem of a token file: ``<provider>-<connection>[--<account>]``.
_TOKEN_STEM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_OAUTH_PREFIX = "oauth/"

_OAUTH_SUFFIX = ".json"

_EXTENDED_PATH_PREFIX = "\\\\?\\"


@dataclass
class _Source:
    """One plaintext credential file as it reads now."""

    path: str
    #: Why vBot never deletes the file (it is reached through a link), or ``None``.
    link: str | None = None
    content: bytes | None = field(default=None, repr=False)
    entries: dict[str, str] = field(default_factory=dict, repr=False)
    #: ``<file>`` or ``<file>:<line>`` left out, with the reason.
    problems: dict[str, str] = field(default_factory=dict)

    @property
    def section(self) -> Section:
        return _section_of(self.path)


def plaintext_credential_files(data_dir: str | Path) -> tuple[str, ...]:
    """Every plaintext credential file of ``data_dir`` as a relative POSIX path.

    A file reached through a link counts too. Raises ``OSError`` when the data
    directory cannot be listed.
    """
    return tuple(_find_plaintext(Path(data_dir)))


def quarantined_plaintext_files(data_dir: str | Path) -> tuple[str, ...]:
    """Plaintext credential copies in the JSON document quarantine, relative to ``data_dir``.

    An older vBot kept OAuth token files in data snapshots, so its set
    restores moved them to quarantine. Raises ``OSError`` when the quarantine
    cannot be listed.
    """
    from core.database import DOCUMENT_QUARANTINE_NAME, quarantine_batches

    root = Path(data_dir)
    found: list[str] = []
    for batch in quarantine_batches(root, DOCUMENT_QUARANTINE_NAME):
        prefix = batch.relative_to(root).as_posix()
        found.extend(f"{prefix}/{path}" for path in _find_plaintext(batch))
    return tuple(found)


def quarantined_secret_stores(data_dir: str | Path) -> tuple[str, ...]:
    """Secret Store documents in quarantine, newest batch first, relative to ``data_dir``.

    ``reset`` moves a store there, and an older vBot's set restore moved the
    live store there when its snapshot held none. Raises ``OSError`` when the
    quarantine cannot be listed.
    """
    from core.database import DOCUMENT_QUARANTINE_NAME, quarantine_batches

    root = Path(data_dir)
    found: list[tuple[str, str]] = []
    for name in (DOCUMENT_QUARANTINE_NAME, QUARANTINE_NAME):
        for batch in quarantine_batches(root, name):
            document = batch / SECRET_STORE_FILE_NAME
            status = stat_or_none(document, follow_symlinks=False)
            if status is not None and stat.S_ISREG(status.st_mode):
                found.append((batch.name, document.relative_to(root).as_posix()))
    return tuple(path for _batch, path in sorted(found, reverse=True))


def _find_plaintext(root: Path) -> dict[str, str | None]:
    """Every plaintext credential file below ``root``, by relative path, in order.

    The value says why vBot never deletes the file (it is reached through a
    symbolic link or junction), or is ``None``. Raises ``OSError`` when a
    directory cannot be listed.
    """
    found: dict[str, str | None] = {}
    env = root / ENV_FILE_PATH
    status = stat_or_none(env, follow_symlinks=False)
    if status is not None:
        if is_link_status(status):
            found[ENV_FILE_PATH] = _link_reason(env)
        elif stat.S_ISREG(status.st_mode):
            found[ENV_FILE_PATH] = None
    directory = root / OAUTH_DIRECTORY
    status = stat_or_none(directory, follow_symlinks=False)
    if status is None:
        return found
    directory_link = _link_reason(directory) if is_link_status(status) else None
    try:
        with os.scandir(directory) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if entry.name.endswith(_OAUTH_SUFFIX) and not entry.name.startswith(".")
            )
    except FileNotFoundError, NotADirectoryError:
        return found
    for name in names:
        entry_path = directory / name
        status = stat_or_none(entry_path, follow_symlinks=False)
        if status is None:
            continue
        path = f"{_OAUTH_PREFIX}{name}"
        if is_link_status(status):
            found[path] = _link_reason(entry_path)
        elif stat.S_ISREG(status.st_mode):
            found[path] = (
                None
                if directory_link is None
                else f"lies in the oauth directory, which {directory_link}"
            )
    return found


def _link_reason(path: Path) -> str:
    try:
        target = os.readlink(path)
    except OSError, ValueError:
        return "is a link"
    # Windows reports the extended-length form of a junction or symbolic link target.
    return f"is a link to {target.removeprefix(_EXTENDED_PATH_PREFIX)}"


def _inspect(root: Path, path: str, link: str | None) -> _Source | None:
    """Read and parse one plaintext file; ``None`` when it is gone.

    Raises ``OSError`` when the file cannot be read.
    """
    target = root.joinpath(*path.split("/"))
    source = _Source(path, link=link)
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        if stat_or_none(target, follow_symlinks=False) is None:
            return None
        source.problems[path] = "its link points to nothing"
        return source
    source.content = content
    if path == ENV_FILE_PATH:
        parsed = _parse_env(content)
        if parsed is None:
            source.problems[path] = "not valid UTF-8"
            return source
        source.entries, skipped = parsed
        source.problems.update({f"{path}:{line}": reason for line, reason in skipped.items()})
        return source
    token_id, problem = _parse_token(path, content)
    if token_id is None:
        source.problems[path] = problem or "cannot be imported"
    else:
        source.entries[token_id] = content.decode("utf-8")
    return source


def _inspect_for_report(root: Path, path: str, link: str | None) -> _Source | None:
    """:func:`_inspect` for reports: a file that cannot be read is a problem."""
    try:
        return _inspect(root, path, link)
    except OSError as exc:
        return _Source(path, link=link, problems={path: f"cannot be read: {exc}"})


def _recorded_sources(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The record's plaintext source files as ``{path: {"fingerprint", "entries"}}``.

    A source of the first record format holds only its content fingerprint
    and records no entries: none of its values counts as put there by the
    import. A path that names no plaintext credential file is left out.
    """
    sources = record.get("sources")
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(sources, dict):
        return result
    for path, value in sources.items():
        if not _is_plaintext_path(path):
            continue
        if isinstance(value, str):
            result[path] = {"fingerprint": value, "entries": {}}
        elif isinstance(value, dict) and isinstance(value.get("fingerprint"), str):
            entries = value.get("entries")
            result[path] = {
                "fingerprint": value["fingerprint"],
                "entries": {
                    name: digest
                    for name, digest in (entries if isinstance(entries, dict) else {}).items()
                    if isinstance(name, str) and isinstance(digest, str)
                },
            }
    return result


def _parse_env(content: bytes) -> tuple[dict[str, str], dict[int, str]] | None:
    """The credentials of a ``.env`` file and the lines left out, by line number.

    ``None`` when the file is not UTF-8, which vBot never read either. Names
    take their credential spelling (uppercase on Windows, where a later line
    wins over another spelling as it did in the process environment).
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    values: dict[str, str] = {}
    skipped: dict[int, str] = {}
    for line, name, value in _parse_env_lines(text):
        if CREDENTIAL_NAME_PATTERN.fullmatch(name) is None:
            skipped[line] = "not a credential name"
        else:
            values[credential_name_key(name)] = value
    # Blank placeholders (the old .env template) hold no credential.
    return {name: value for name, value in values.items() if value}, skipped


def _parse_env_lines(text: str) -> Iterator[tuple[int, str, str]]:
    """Parse ``KEY=VALUE`` lines exactly as vBot read ``.env`` before the Secret Store.

    Yields the line number, key and value of each assignment. CR, LF and CRLF
    end a line; Unicode line separators are part of a value. Blank lines,
    comments and lines without ``=`` are ignored, values may hold further
    ``=``, and one pair of matching outer quotes is removed. There is no
    expansion, escape decoding or command substitution. A later line wins.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            yield number, key, value


def _parse_token(path: str, content: bytes) -> tuple[str | None, str | None]:
    # The token document format belongs to the Provider domain; imported at call
    # time, since the Provider token store builds on this package.
    from core.providers.token_store import validate_oauth_token_data

    stem = path.removeprefix(_OAUTH_PREFIX).removesuffix(_OAUTH_SUFFIX)
    if _TOKEN_STEM_PATTERN.fullmatch(stem) is None or ENTRY_NAME_PATTERN.fullmatch(stem) is None:
        return None, "not an OAuth token file name"
    try:
        data = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None, "not valid JSON"
    errors = [item for item in validate_oauth_token_data(data) if item.severity == "error"]
    if errors:
        return None, "does not load as an OAuth token: " + "; ".join(
            f"{item.path}: {item.message}" for item in errors
        )
    return stem, None


def _section_of(path: str) -> Section:
    return CREDENTIALS if path == ENV_FILE_PATH else OAUTH_TOKENS


def _label(section: Section, name: str) -> str:
    """How reports name an entry: a credential by name, a token as ``oauth/<token id>``."""
    return name if section == CREDENTIALS else f"{_OAUTH_PREFIX}{name}"


def _is_plaintext_path(path: object) -> bool:
    """Whether a recorded source names a plaintext credential file; never another file."""
    if path == ENV_FILE_PATH:
        return True
    if not isinstance(path, str):
        return False
    if not path.startswith(_OAUTH_PREFIX) or not path.endswith(_OAUTH_SUFFIX):
        return False
    stem = path.removeprefix(_OAUTH_PREFIX).removesuffix(_OAUTH_SUFFIX)
    return _TOKEN_STEM_PATTERN.fullmatch(stem) is not None
