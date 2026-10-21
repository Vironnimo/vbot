"""KeePassXC Secret Source: settings, lock state, entries, field reads and new entries.

keepassxc-cli is replaced by an in-memory fake that checks the master password
on stdin the way the real CLI does: a wrong password exits with 1 and, with
``-q``, prints nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from core.secrets.sources import NewSourceEntry, SecretSourceError
from resources.extensions.keepassxc._cli import (
    CommandNotStartedError,
    CommandResult,
    CommandTimeoutError,
)
from resources.extensions.keepassxc._source import LISTING_SECONDS, KeePassXCSource
from tests.resources.extensions.bundled_test_support import load_bundled

MASTER = "correct horse battery"
OTP_URI = "otpauth://totp/Example:alice?secret=JBSWY3DPEHPK3PXP&issuer=Example"


class FakeKeePassXC:
    """keepassxc-cli over one in-memory database."""

    def __init__(self, *, password: str = MASTER, key_file: str | None = None) -> None:
        self.password = password
        self.key_file = key_file
        self.entries: dict[str, dict[str, str]] = {
            "Root Entry": {"UserName": "root", "Password": "root-pass-1"},
            "Work/Mail": {
                "UserName": "alice",
                "Password": "mail-pass-1",
                "URL": "https://mail.example",
                "Notes": "line one\nline two",
                "otp": OTP_URI,
                "API Key": "api-key-1",
            },
            "Work/Sub/Legacy": {"Password": "", "TOTP Seed": "JBSWY3DPEHPK3PXP"},
        }
        self.calls: list[tuple[list[str], str]] = []
        self.raises: Exception | None = None

    def __call__(self, argv: Sequence[str], *, stdin: str, timeout: float) -> CommandResult:
        self.calls.append((list(argv), stdin))
        if self.raises is not None:
            raise self.raises
        command, separator = argv[1], list(argv).index("--")
        options, positional = list(argv[2:separator]), list(argv[separator + 2 :])
        lines = stdin.split("\n")
        given = "" if "--no-password" in options else lines.pop(0)
        key = options[options.index("-k") + 1] if "-k" in options else None
        if given != self.password or key != self.key_file:
            return CommandResult(1, "", "")
        if command == "ls":
            return CommandResult(0, "Root Entry\nWork/\n", "")
        if command == "search":
            if not self.entries:
                return CommandResult(1, "", "No results for that search term.\n")
            return CommandResult(0, "".join(f"/{path}\n" for path in self.entries), "")
        if command == "show":
            attribute = options[options.index("-a") + 1].lower()
            entry = self.entries.get(positional[0])
            found = {name.lower(): value for name, value in (entry or {}).items()}
            if attribute not in found:
                return CommandResult(1, "", "Eintrag nicht gefunden.\n")
            return CommandResult(0, found[attribute] + "\n", "")
        if command == "add" and positional[0] not in self.entries:
            entry = {}
            for option in options:
                if option.startswith("--username="):
                    entry["UserName"] = option.removeprefix("--username=")
                if option.startswith("--url="):
                    entry["URL"] = option.removeprefix("--url=")
            if "--password-prompt" in options:
                entry["Password"] = lines[0]
            self.entries[positional[0]] = entry
            return CommandResult(0, "", "")
        return CommandResult(1, "", "Fehler.\n")

    def commands(self) -> list[str]:
        return [argv[1] for argv, _stdin in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def files(tmp_path: Path) -> dict[str, str]:
    for name in ("Passwords.kdbx", "Other.kdbx", "Passwords.keyx", "keepassxc-cli.exe"):
        (tmp_path / name).write_bytes(b"")
    return {
        "database": str(tmp_path / "Passwords.kdbx"),
        "other": str(tmp_path / "Other.kdbx"),
        "key_file": str(tmp_path / "Passwords.keyx"),
        "cli_path": str(tmp_path / "keepassxc-cli.exe"),
    }


def _source(
    config: dict[str, Any], cli: FakeKeePassXC, clock: Clock | None = None
) -> KeePassXCSource:
    return KeePassXCSource(
        lambda: dict(config),
        logger=logging.getLogger("tests.keepassxc"),
        runner=cli,
        which=lambda _name: None,
        locations=(),
        clock=clock or Clock(),
    )


def _unlocked(
    files: dict[str, str], clock: Clock | None = None
) -> tuple[KeePassXCSource, FakeKeePassXC, dict[str, Any]]:
    config: dict[str, Any] = {"database": files["database"], "cli_path": files["cli_path"]}
    cli = FakeKeePassXC()
    source = _source(config, cli, clock)
    source.unlock({"password": MASTER})
    return source, cli, config


def test_registers_the_source_with_live_settings(files: dict[str, str]) -> None:
    config: dict[str, Any] = {}
    registry = load_bundled("keepassxc", config_provider=lambda _name: config)
    record = next(item for item in registry.records() if item.name == "keepassxc")
    assert record.status == "loaded" and record.capability_errors == []
    assert record.manifest is not None and record.manifest.display_name == "KeePassXC"
    schema = record.declarations.settings_schema
    assert schema is not None
    assert {(field.key, field.type) for field in schema} == {
        ("database", "text"),
        ("key_file", "text"),
        ("cli_path", "text"),
    }
    [source] = registry.secret_sources()
    assert (source.name, source.label) == ("keepassxc", "KeePassXC")
    assert "store" in source.capabilities
    assert source.status().state == "unavailable"

    config.update(database=files["database"], cli_path=files["cli_path"])

    assert source.status().state == "locked"
    assert [(item.name, item.concealed, item.required) for item in source.unlock_fields] == [
        ("password", True, True)
    ]
    config["key_file"] = files["key_file"]
    assert source.unlock_fields[0].required is False


@pytest.mark.parametrize(
    ("settings", "missing"),
    [
        pytest.param({"database": "database"}, None, id="no-cli"),
        pytest.param({"database": "database", "cli_path": "missing"}, "missing", id="cli-path"),
        pytest.param({"cli_path": "cli_path"}, None, id="no-database"),
        pytest.param({"database": "missing", "cli_path": "cli_path"}, "missing", id="database"),
        pytest.param(
            {"database": "database", "cli_path": "cli_path", "key_file": "missing"},
            "missing",
            id="key-file",
        ),
    ],
)
def test_status_is_unavailable_with_a_reason_until_cli_and_files_exist(
    files: dict[str, str], tmp_path: Path, settings: dict[str, str], missing: str | None
) -> None:
    paths = {**files, "missing": str(tmp_path / "missing.file")}
    config = {key: paths[name] for key, name in settings.items()}
    cli = FakeKeePassXC()
    source = _source(config, cli)

    status = source.status()

    assert status.state == "unavailable" and status.reason
    if missing is not None:
        assert paths[missing] in status.reason
    with pytest.raises(SecretSourceError) as raised:
        source.unlock({"password": MASTER})
    assert raised.value.code == "unavailable"
    assert cli.calls == []


def test_cli_is_found_on_path_or_in_the_installation_folder(
    files: dict[str, str], tmp_path: Path
) -> None:
    installed = tmp_path / "KeePassXC" / "keepassxc-cli.exe"
    installed.parent.mkdir()
    installed.write_bytes(b"")
    config = {"database": files["database"]}
    on_path = KeePassXCSource(
        lambda: config,
        logger=logging.getLogger("tests.keepassxc"),
        runner=FakeKeePassXC(),
        which={"keepassxc.cli": files["cli_path"]}.get,
        locations=(),
    )
    in_folder = KeePassXCSource(
        lambda: config,
        logger=logging.getLogger("tests.keepassxc"),
        runner=FakeKeePassXC(),
        which=lambda _name: None,
        locations=(str(installed),),
    )

    assert on_path.status().state == "locked"
    assert in_folder.status().state == "locked"


def test_unlock_checks_the_master_password_given_on_stdin(files: dict[str, str]) -> None:
    config = {"database": files["database"], "cli_path": files["cli_path"]}
    cli = FakeKeePassXC()
    source = _source(config, cli)

    with pytest.raises(SecretSourceError) as raised:
        source.unlock({"password": "wrong password"})
    assert raised.value.code == "denied"
    assert source.status().state == "locked"

    assert source.unlock({"password": MASTER}).state == "available"
    assert source.status().state == "available"
    for argv, stdin in cli.calls:
        assert argv[-1] == files["database"] and argv[-2] == "--"
        assert not any(MASTER in item or "wrong password" in item for item in argv)
        assert stdin.split("\n")[0] in (MASTER, "wrong password")


def test_a_key_file_database_unlocks_without_a_password(files: dict[str, str]) -> None:
    config = {
        "database": files["database"],
        "cli_path": files["cli_path"],
        "key_file": files["key_file"],
    }
    cli = FakeKeePassXC(password="", key_file=files["key_file"])
    source = _source(config, cli)

    assert source.unlock({}).state == "available"
    argv, stdin = cli.calls[-1]
    assert "--no-password" in argv and stdin == ""
    assert argv[argv.index("-k") + 1] == files["key_file"]
    without_key = _source({**config, "key_file": ""}, cli)
    with pytest.raises(SecretSourceError) as raised:
        without_key.unlock({})
    assert raised.value.code == "denied"


def test_entries_are_listed_by_group_and_served_from_one_listing(files: dict[str, str]) -> None:
    clock = Clock()
    source, cli, _config = _unlocked(files, clock)
    cli.calls.clear()

    assert source.containers() == ["Work", "Work/Sub"]
    entries = {entry.reference: entry for entry in source.entries()}
    assert list(entries) == ["Root Entry", "Work/Mail", "Work/Sub/Legacy"]
    mail = entries["Work/Mail"]
    assert (mail.title, mail.container, mail.kind) == ("Mail", "Work", "login")
    assert entries["Root Entry"].container is None
    assert [item.name for item in mail.fields if not item.optional] == ["password"]
    assert [(item.name, item.concealed, item.value) for item in mail.fields] == [
        ("username", False, None),
        ("password", True, None),
        ("url", False, None),
        ("notes", True, None),
        ("totp", True, None),
    ]
    assert [entry.reference for entry in source.entries("Work")] == ["Work/Mail"]
    assert cli.commands() == ["search"]

    clock.now += LISTING_SECONDS + 1
    source.entries()
    assert cli.commands() == ["search", "search"]
    with pytest.raises(SecretSourceError) as raised:
        source.entries("Private")
    assert raised.value.code == "not_found"


def test_an_empty_database_lists_no_entries(files: dict[str, str]) -> None:
    source, cli, _config = _unlocked(files)
    cli.entries.clear()

    assert source.entries() == []
    assert source.containers() == []


@pytest.mark.parametrize(
    ("field_name", "attribute", "value"),
    [
        ("username", "UserName", "alice"),
        ("password", "Password", "mail-pass-1"),
        ("url", "URL", "https://mail.example"),
        ("notes", "Notes", "line one\nline two"),
        ("totp", "otp", OTP_URI),
        ("API Key", "API Key", "api-key-1"),
    ],
)
def test_resolve_reads_the_field_from_its_attribute(
    files: dict[str, str], field_name: str, attribute: str, value: str
) -> None:
    source, cli, _config = _unlocked(files)

    assert source.resolve("Work/Mail", field_name) == value
    argv, stdin = cli.calls[-1]
    assert argv[1] == "show" and "-s" in argv
    assert argv[argv.index("-a") + 1] == attribute
    assert argv[-1] == "Work/Mail"
    assert value not in argv and stdin == f"{MASTER}\n"


@pytest.mark.parametrize("empty_modern_field", [False, True])
def test_totp_falls_back_to_the_legacy_seed_attribute(
    files: dict[str, str], empty_modern_field: bool
) -> None:
    source, cli, _config = _unlocked(files)

    if empty_modern_field:
        cli.entries["Work/Sub/Legacy"]["otp"] = ""
    assert source.resolve("Work/Sub/Legacy", "totp") == "JBSWY3DPEHPK3PXP"
    assert [argv[argv.index("-a") + 1] for argv, _ in cli.calls[-2:]] == ["otp", "TOTP Seed"]


@pytest.mark.parametrize(
    ("reference", "field_name"),
    [
        pytest.param("Work/Gone", "password", id="missing-entry"),
        pytest.param("Root Entry", "totp", id="missing-field"),
        pytest.param("Work/Sub/Legacy", "password", id="empty-value"),
    ],
)
def test_resolve_reports_a_missing_entry_field_or_value(
    files: dict[str, str], reference: str, field_name: str
) -> None:
    source, _cli, _config = _unlocked(files)

    with pytest.raises(SecretSourceError) as raised:
        source.resolve(reference, field_name)

    assert raised.value.code == ("optional_field_missing" if field_name == "totp" else "not_found")
    assert source.status().state == "available"


def test_a_database_that_stops_opening_locks_the_source(files: dict[str, str]) -> None:
    source, cli, _config = _unlocked(files)
    cli.password = "changed elsewhere"

    with pytest.raises(SecretSourceError) as raised:
        source.resolve("Work/Mail", "password")

    assert raised.value.code == "locked"
    status = source.status()
    assert status.state == "locked" and status.reason
    calls = len(cli.calls)
    with pytest.raises(SecretSourceError):
        source.resolve("Work/Mail", "password")
    assert len(cli.calls) == calls


def test_changed_database_settings_lock_the_source(files: dict[str, str]) -> None:
    source, cli, config = _unlocked(files)
    config["database"] = files["other"]
    calls = len(cli.calls)

    status = source.status()

    assert status.state == "locked" and status.reason
    with pytest.raises(SecretSourceError) as raised:
        source.resolve("Work/Mail", "password")
    assert raised.value.code == "locked"
    assert len(cli.calls) == calls


def test_lock_forgets_the_master_password(files: dict[str, str]) -> None:
    source, cli, _config = _unlocked(files)
    calls = len(cli.calls)

    assert source.lock().state == "locked"

    for call in (lambda: source.resolve("Work/Mail", "password"), source.entries):
        with pytest.raises(SecretSourceError) as raised:
            call()
        assert raised.value.code == "locked"
    assert len(cli.calls) == calls
    with pytest.raises(SecretSourceError):
        source.unlock({"password": "wrong password"})
    assert source.status().state == "locked"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        pytest.param(CommandTimeoutError(), "timed_out", id="timeout"),
        pytest.param(CommandNotStartedError("Access is denied"), "unavailable", id="not-started"),
    ],
)
def test_cli_failures_become_source_errors_without_values(
    files: dict[str, str], caplog: pytest.LogCaptureFixture, failure: Exception, code: str
) -> None:
    caplog.set_level(logging.DEBUG)
    source, cli, _config = _unlocked(files)
    cli.raises = failure

    with pytest.raises(SecretSourceError) as raised:
        source.resolve("Work/Mail", "password")

    assert raised.value.code == code
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    for text in [str(raised.value), *(record.getMessage() for record in caplog.records)]:
        assert MASTER not in text and "mail-pass-1" not in text


def test_store_creates_an_entry_with_its_password_on_stdin(files: dict[str, str]) -> None:
    source, cli, _config = _unlocked(files)
    new = NewSourceEntry(
        title="Shop",
        kind="login",
        values={"username": "bob", "password": "new-pass-1", "url": "https://shop.example"},
        container="Work",
    )

    assert source.store(new) == "Work/Shop"

    argv, stdin = cli.calls[-1]
    assert argv[1] == "add" and argv[-1] == "Work/Shop"
    assert {"--username=bob", "--url=https://shop.example", "--password-prompt"} <= set(argv)
    assert not any("new-pass-1" in item for item in argv)
    assert stdin == f"{MASTER}\nnew-pass-1\n"
    assert source.resolve("Work/Shop", "password") == "new-pass-1"
    assert "Work/Shop" in [entry.reference for entry in source.entries("Work")]


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(NewSourceEntry("Mail", "login", {"password": "x"}, "Work"), id="exists"),
        pytest.param(NewSourceEntry("Note", "text", {"notes": "x"}), id="unsupported-field"),
        pytest.param(NewSourceEntry("Two", "login", {"password": "a\nb"}), id="line-break"),
        pytest.param(NewSourceEntry("a/b", "login", {"password": "x"}), id="slash-in-title"),
    ],
)
def test_store_refuses_entries_it_cannot_create_safely(
    files: dict[str, str], entry: NewSourceEntry
) -> None:
    source, cli, _config = _unlocked(files)

    with pytest.raises(SecretSourceError):
        source.store(entry)

    assert "add" not in cli.commands()
