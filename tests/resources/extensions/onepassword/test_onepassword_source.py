"""1Password Secret Source: settings, token handling, vaults and items, field reads.

The 1Password CLI is replaced by an in-memory fake that answers like ``op``
with ``--format json`` and authenticates by the service account token in the
environment of each call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from core.secrets.sources import SecretSourceError
from resources.extensions.onepassword._cli import (
    CommandNotStartedError,
    CommandResult,
    CommandTimeoutError,
)
from resources.extensions.onepassword._source import (
    LISTING_SECONDS,
    TOKEN_ENV_KEY,
    OnePasswordSource,
)
from tests.resources.extensions.bundled_test_support import load_bundled

TOKEN = "ops_eyJzaWduSW5BZGRyZXNzIjoibXkuMXBhc3N3b3JkLmNvbSJ9"
OTP_URI = "otpauth://totp/GitHub:alice?secret=JBSWY3DPEHPK3PXP&issuer=GitHub"
VAULTS = [{"id": "v1private", "name": "Private"}, {"id": "v2work", "name": "Work"}]
ITEMS: dict[str, list[dict[str, Any]]] = {
    "v1private": [
        {
            "id": "i1login",
            "title": "GitHub",
            "category": "LOGIN",
            "additional_information": "alice",
            "urls": [
                {"href": "https://old.example"},
                {"primary": True, "href": "https://github.com"},
            ],
        },
        {"id": "i2note", "title": "Recovery codes", "category": "SECURE_NOTE"},
    ],
    "v2work": [
        {"id": "i3api", "title": "Deploy key", "category": "API_CREDENTIAL"},
        {"id": "i4pass", "title": "Wifi", "category": "PASSWORD"},
        {"id": "i5card", "title": "Card", "category": "CREDIT_CARD"},
    ],
}
DETAILS: dict[str, dict[str, Any]] = {
    "i1login": {
        "category": "LOGIN",
        "id": "i1login",
        "title": "GitHub",
        "urls": [{"primary": True, "href": "https://github.com"}],
        "fields": [
            {"id": "username", "type": "STRING", "purpose": "USERNAME", "value": "alice"},
            {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "value": "gh-pass-1"},
            {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "label": "notesPlain"},
            {"id": "TOTP_x1", "type": "OTP", "label": "one-time password", "value": OTP_URI},
            {"id": "k3x9", "type": "CONCEALED", "label": "API Host", "value": "host-secret-1"},
        ],
    },
    "i3api": {
        "category": "API_CREDENTIAL",
        "id": "i3api",
        "title": "Deploy key",
        "fields": [{"id": "credential", "type": "CONCEALED", "value": "api-credential-1"}],
    },
}


class FakeOp:
    """``op`` for one account; the token in the environment decides access."""

    def __init__(self) -> None:
        self.tokens = {TOKEN}
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.raises: Exception | None = None
        self.stderr: str | None = None

    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float
    ) -> CommandResult:
        self.calls.append((list(argv), dict(env)))
        if self.raises is not None:
            raise self.raises
        token = env.get(TOKEN_ENV_KEY, "")
        if token not in self.tokens:
            return _error(f"authentication failed for token {token}: Unauthorized (401)")
        if self.stderr is not None:
            return _error(self.stderr)
        command = argv[1:3]
        if command == ["vault", "list"]:
            return CommandResult(0, json.dumps(VAULTS), "")
        vault = argv[argv.index("--vault") + 1]
        if command == ["item", "list"]:
            return CommandResult(0, json.dumps(ITEMS[vault]), "")
        if command == ["item", "get"] and "--reveal" in argv and argv[3] in DETAILS:
            return CommandResult(0, json.dumps(DETAILS[argv[3]]), "")
        return _error(f'"{argv[3]}" isn\'t an item in the "{vault}" vault.')

    def commands(self) -> list[str]:
        return [" ".join(argv[1:3]) for argv, _env in self.calls]


def _error(message: str) -> CommandResult:
    return CommandResult(1, "", f"[ERROR] 2026/10/21 09:30:00 {message}\n")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def op_path(tmp_path: Path) -> str:
    path = tmp_path / "op.exe"
    path.write_bytes(b"")
    return str(path)


def _source(
    op_path: str,
    op: FakeOp,
    *,
    saved: str = "",
    clock: Clock | None = None,
) -> OnePasswordSource:
    credentials = {TOKEN_ENV_KEY: saved}
    return OnePasswordSource(
        lambda: {"cli_path": op_path},
        lambda key: credentials.get(key, ""),
        logger=logging.getLogger("tests.onepassword"),
        runner=op,
        which=lambda _name: None,
        locations=(),
        clock=clock or Clock(),
    )


def test_registers_the_source_with_a_secret_token_setting(op_path: str) -> None:
    config: dict[str, Any] = {}
    credentials: dict[str, str] = {}
    registry = load_bundled(
        "onepassword",
        config_provider=lambda _name: config,
        credential_resolver=lambda key: credentials.get(key, ""),
    )
    record = next(item for item in registry.records() if item.name == "onepassword")
    assert record.status == "loaded" and record.capability_errors == []
    assert record.manifest is not None and record.manifest.display_name == "1Password"
    schema = record.declarations.settings_schema
    assert schema is not None
    fields = {field.key: field for field in schema}
    assert (fields["token"].type, fields["token"].env_key) == ("secret", TOKEN_ENV_KEY)
    assert fields["cli_path"].type == "text"
    [source] = registry.secret_sources()
    assert (source.name, source.label, source.capabilities) == (
        "onepassword",
        "1Password",
        frozenset(),
    )

    config["cli_path"] = op_path
    assert source.status().state == "locked"
    assert source.unlock_fields[0].required is True

    credentials[TOKEN_ENV_KEY] = TOKEN
    assert source.status().state == "available"
    assert source.unlock_fields[0].required is False


def test_status_is_unavailable_without_op(tmp_path: Path) -> None:
    op = FakeOp()
    found = _source(str(tmp_path / "missing" / "op.exe"), op, saved=TOKEN)

    status = found.status()

    assert status.state == "unavailable" and status.reason
    with pytest.raises(SecretSourceError) as raised:
        found.resolve("v1private/i1login", "password")
    assert raised.value.code == "unavailable"
    assert op.calls == []


def test_an_unlock_token_reaches_op_only_through_its_environment(
    op_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OP_CONNECT_HOST", "http://connect.example")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "connect-token-1")
    op = FakeOp()
    source = _source(op_path, op)
    assert source.status().state == "locked"

    assert source.unlock({"token": TOKEN}).state == "available"
    assert source.resolve("v1private/i1login", "password") == "gh-pass-1"

    for argv, env in op.calls:
        assert not any(TOKEN in item for item in argv)
        assert env[TOKEN_ENV_KEY] == TOKEN and env["OP_CACHE"] == "false"
        assert "OP_CONNECT_HOST" not in env and "OP_CONNECT_TOKEN" not in env
    assert op.commands() == ["vault list", "item get"]


def test_lock_forgets_an_unlock_token_and_pauses_a_saved_one(op_path: str) -> None:
    op = FakeOp()
    given = _source(op_path, op)
    given.unlock({"token": TOKEN})
    saved = _source(op_path, op, saved=TOKEN)
    assert saved.status().state == "available"

    for source in (given, saved):
        assert source.lock().state == "locked"
        calls = len(op.calls)
        with pytest.raises(SecretSourceError) as raised:
            source.resolve("v1private/i1login", "password")
        assert raised.value.code == "locked"
        assert len(op.calls) == calls

    with pytest.raises(SecretSourceError) as raised:
        given.unlock({})
    assert raised.value.code == "denied"
    assert saved.unlock({}).state == "available"
    assert op.calls[-1][1][TOKEN_ENV_KEY] == TOKEN


def test_a_refused_token_fails_unlock_and_locks_a_saved_token(
    op_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    op = FakeOp()
    op.tokens = set()
    source = _source(op_path, op)

    with pytest.raises(SecretSourceError) as raised:
        source.unlock({"token": TOKEN})

    assert raised.value.code == "denied"
    saved = _source(op_path, op, saved=TOKEN)
    with pytest.raises(SecretSourceError):
        saved.containers()
    status = saved.status()
    assert status.state == "locked" and status.reason
    messages = [str(raised.value), *(record.getMessage() for record in caplog.records)]
    assert all(TOKEN not in text and "ops_" not in text for text in messages)


def test_vaults_and_items_become_containers_and_entries(op_path: str) -> None:
    clock = Clock()
    op = FakeOp()
    source = _source(op_path, op, saved=TOKEN, clock=clock)

    assert source.containers() == ["Private", "Work"]
    entries = {entry.reference: entry for entry in source.entries()}
    login = entries["v1private/i1login"]
    assert (login.title, login.kind, login.container) == ("GitHub", "login", "Private")
    assert [item.name for item in login.fields if not item.optional] == ["password"]
    assert [(item.name, item.concealed, item.value) for item in login.fields] == [
        ("username", False, "alice"),
        ("password", True, None),
        ("url", False, "https://github.com"),
        ("totp", True, None),
        ("notes", True, None),
    ]
    kinds = {reference: entry.kind for reference, entry in entries.items()}
    assert kinds == {
        "v1private/i1login": "login",
        "v1private/i2note": "text",
        "v2work/i3api": "token",
        "v2work/i4pass": "token",
        "v2work/i5card": "text",
    }
    assert [field.name for field in entries["v2work/i3api"].fields][0] == "credential"
    assert [entry.reference for entry in source.entries("Work")] == [
        "v2work/i3api",
        "v2work/i4pass",
        "v2work/i5card",
    ]
    assert op.commands() == ["vault list", "item list", "item list"]

    clock.now += LISTING_SECONDS + 1
    source.entries("Work")
    assert op.commands()[-2:] == ["vault list", "item list"]
    with pytest.raises(SecretSourceError) as raised:
        source.entries("Shared")
    assert raised.value.code == "not_found"


@pytest.mark.parametrize(
    ("reference", "field_name", "value"),
    [
        ("v1private/i1login", "username", "alice"),
        ("v1private/i1login", "password", "gh-pass-1"),
        ("v1private/i1login", "url", "https://github.com"),
        ("v1private/i1login", "totp", OTP_URI),
        ("v1private/i1login", "api host", "host-secret-1"),
        ("v1private/i1login", "k3x9", "host-secret-1"),
        ("v2work/i3api", "credential", "api-credential-1"),
    ],
)
def test_resolve_finds_the_field_by_purpose_type_id_or_label(
    op_path: str, reference: str, field_name: str, value: str
) -> None:
    op = FakeOp()
    source = _source(op_path, op, saved=TOKEN)

    assert source.resolve(reference, field_name) == value
    argv, _env = op.calls[-1]
    vault_id, item_id = reference.split("/")
    assert argv[1:4] == ["item", "get", item_id]
    assert argv[argv.index("--vault") + 1] == vault_id
    assert {"--reveal", "json"} <= set(argv)


@pytest.mark.parametrize(
    ("reference", "field_name", "stderr", "code"),
    [
        pytest.param("v1private/gone", "password", None, "not_found", id="missing-item"),
        pytest.param("v1private/i1login", "pin", None, "not_found", id="missing-field"),
        pytest.param(
            "v1private/i1login", "notes", None, "optional_field_missing", id="empty-value"
        ),
        pytest.param("not-a-reference", "password", None, "not_found", id="bad-reference"),
        pytest.param(
            "v1private/i1login",
            "password",
            "Too many requests (429): rate limit exceeded",
            "unavailable",
            id="rate-limit",
        ),
        pytest.param(
            "v1private/i1login",
            "password",
            "Get https://my.1password.com: dial tcp: no such host",
            "unavailable",
            id="offline",
        ),
    ],
)
def test_resolve_failures_carry_a_code_and_no_value(
    op_path: str,
    caplog: pytest.LogCaptureFixture,
    reference: str,
    field_name: str,
    stderr: str | None,
    code: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    op = FakeOp()
    op.stderr = None if stderr is None else f"{stderr} (token {TOKEN})"
    source = _source(op_path, op, saved=TOKEN)

    with pytest.raises(SecretSourceError) as raised:
        source.resolve(reference, field_name)

    assert raised.value.code == code
    assert source.status().state == "available"
    for text in [str(raised.value), *(record.getMessage() for record in caplog.records)]:
        assert TOKEN not in text and "gh-pass-1" not in text


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        pytest.param(CommandTimeoutError(), "timed_out", id="timeout"),
        pytest.param(CommandNotStartedError("Access is denied"), "unavailable", id="not-started"),
    ],
)
def test_op_failures_become_source_errors(op_path: str, failure: Exception, code: str) -> None:
    op = FakeOp()
    op.raises = failure
    source = _source(op_path, op, saved=TOKEN)

    with pytest.raises(SecretSourceError) as raised:
        source.resolve("v1private/i1login", "password")

    assert raised.value.code == code
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
