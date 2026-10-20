"""Agent Secrets: storage, grants, lifetimes, substitution, sources and masking."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from core.secrets.agent_secrets import (
    AgentSecrets,
    SecretRecordError,
    SecretUse,
    SecretUseError,
    SourceBinding,
)
from core.secrets.sources import SecretSourceError, SourceEntry, SourceStatus, UnlockField
from tests.core.secrets.secrets_test_support import data_directory, open_store

PASSWORD = "hunter2-very-secret"
TOKEN = "sk-abcdef1234567890"
# RFC 6238 test seed ("12345678901234567890" in Base32).
TOTP_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeSource:
    """An in-memory password manager with one login entry."""

    name = "vault"
    label = "Vault"
    unlock_fields = (UnlockField("password", "Password"),)
    capabilities = frozenset[str]()

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)
        self.locked = False
        self.lock_calls = 0

    def status(self) -> SourceStatus:
        return SourceStatus("locked" if self.locked else "available")

    def containers(self) -> list[str]:
        return []

    def entries(self, container: str | None = None) -> list[SourceEntry]:
        return []

    def resolve(self, reference: str, field_name: str) -> str:
        if self.locked:
            raise SecretSourceError("locked", "the vault is locked")
        return self.values[field_name]

    def unlock(self, inputs: Mapping[str, str]) -> SourceStatus:
        self.locked = False
        return self.status()

    def lock(self) -> SourceStatus:
        self.lock_calls += 1
        self.locked = True
        return self.status()

    def store(self, entry: object) -> str:
        raise SecretSourceError("unavailable", "read-only")


def _secrets(tmp_path: Path, clock: Clock | None = None) -> AgentSecrets:
    secrets = AgentSecrets(open_store(tmp_path, data_directory(tmp_path)), clock=clock or Clock())
    secrets.reload()
    return secrets


def _login(secrets: AgentSecrets, **options: object) -> None:
    secrets.create(
        name="github",
        kind="login",
        values={"username": "julian", "password": PASSWORD},
        actor="user",
        agents=["main"],
        **options,  # type: ignore[arg-type]
    )


def test_secrets_persist_sealed_and_are_described_without_concealed_values(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets, description="GitHub account")

    reopened = _secrets(tmp_path)
    (secret,) = reopened.usable("main", "s1")
    described = reopened.describe(secret)

    assert secret.name == "GITHUB"
    assert reopened.substitute("main", "s1", "{{secret:github.password}}", "terminal")[0] == (
        PASSWORD
    )
    assert PASSWORD not in json.dumps(described)
    assert {item["name"]: item.get("value") for item in described["fields"]} == {
        "username": "julian",
        "password": None,
    }
    assert PASSWORD not in (tmp_path / "data" / "secrets.json").read_text(encoding="utf-8")
    assert open_store(tmp_path).status().agent_secrets == 1


def test_names_are_unique_and_never_take_a_system_credential_name(tmp_path: Path) -> None:
    store = open_store(tmp_path, data_directory(tmp_path))
    secrets = AgentSecrets(store, clock=Clock(), credential_names=lambda: ["GH"])
    secrets.reload()
    _login(secrets)

    with pytest.raises(SecretRecordError):
        _login(secrets)
    with pytest.raises(SecretRecordError):
        secrets.create(name="gh", kind="token", values={"value": TOKEN}, actor="user")


@pytest.mark.parametrize(
    ("agent_id", "session_id", "text", "sink", "code"),
    [
        ("main", "s1", "{{secret:GITHUB.password}}", "files", "sink_not_allowed"),
        ("other", "s1", "{{secret:GITHUB.password}}", "terminal", "not_granted"),
        ("main", "s1", "{{secret:GITLAB}}", "terminal", "unknown_secret"),
        ("main", "s1", "{{secret:GITHUB.pin}}", "terminal", "unknown_field"),
    ],
)
def test_substitution_refuses_what_the_caller_may_not_use(
    tmp_path: Path, agent_id: str, session_id: str, text: str, sink: str, code: str
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets)

    with pytest.raises(SecretUseError) as raised:
        secrets.substitute(agent_id, session_id, text, sink)  # type: ignore[arg-type]

    assert raised.value.code == code
    assert PASSWORD not in str(raised.value)


def test_substitution_replaces_references_in_nested_values_and_reports_uses(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets)

    value, uses = secrets.substitute(
        "main",
        "s1",
        {"lines": ["{{secret:GITHUB.username}}:{{ secret:github.password }}", "plain"]},
        "terminal",
    )

    assert value == {"lines": [f"julian:{PASSWORD}", "plain"]}
    assert uses == [
        SecretUse("GITHUB", "username", "terminal"),
        SecretUse("GITHUB", "password", "terminal"),
    ]


def test_lifetimes_bound_where_and_how_long_a_secret_is_usable(tmp_path: Path) -> None:
    clock = Clock()
    secrets = _secrets(tmp_path, clock)
    secrets.create(
        name="code",
        kind="token",
        values={"value": "otp-12345678"},
        actor="user",
        lifetime="once",
        session_id="s1",
    )
    secrets.create(
        name="api",
        kind="token",
        values={"value": TOKEN},
        actor="user",
        lifetime="session",
        session_id="s1",
    )
    secrets.create(
        name="temp",
        kind="token",
        values={"value": "temporary-value"},
        actor="user",
        agents=["main"],
        expires_at=clock.now + timedelta(hours=1),
    )

    # A once Secret named twice in one call is used once, then it is gone.
    assert secrets.substitute("main", "s1", "{{secret:CODE}} {{secret:CODE}}", "files")[0] == (
        "otp-12345678 otp-12345678"
    )
    with pytest.raises(SecretUseError):
        secrets.substitute("main", "s1", "{{secret:CODE}}", "files")
    # A Session Secret exists only in its Session.
    assert [item.name for item in secrets.usable("main", "s2")] == ["TEMP"]
    clock.now += timedelta(hours=2)
    assert [item.name for item in secrets.usable("main", "s1")] == ["API"]
    assert secrets.sweep() == 1
    secrets.remove_session("s1")
    assert [item.name for item in _secrets(tmp_path, clock).all()] == []


def test_a_parent_passes_only_its_own_secrets_and_the_grant_ends_with_the_child(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets)

    secrets.pass_to_session("main", "s1", ["GITHUB"], child_session_id="child")
    with pytest.raises(SecretUseError):
        secrets.pass_to_session("helper", "child", ["GITLAB"], child_session_id="grandchild")

    assert [item.name for item in secrets.usable("helper", "child")] == ["GITHUB"]
    secrets.remove_session("child")
    assert secrets.usable("helper", "child") == []
    assert [item.name for item in secrets.usable("main", "s1")] == ["GITHUB"]


def test_environment_gives_every_field_for_a_name_or_one_field_for_its_variable(
    tmp_path: Path,
) -> None:
    clock = Clock()
    clock.now = datetime.fromtimestamp(59, UTC)
    secrets = _secrets(tmp_path, clock)
    _login(secrets)
    secrets.update(
        secrets.usable("main", "s1")[0].id,
        actor="user",
        values={"totp": TOTP_SEED},
    )

    everything = secrets.environment("main", "s1", "GITHUB")
    one = secrets.environment("main", "s1", "github_password")

    assert everything is not None and everything[0] == {
        "GITHUB_USERNAME": "julian",
        "GITHUB_PASSWORD": PASSWORD,
        "GITHUB_TOTP": "287082",
    }
    assert one is not None and one[0] == {"GITHUB_PASSWORD": PASSWORD}
    assert secrets.environment("main", "s1", "PATH") is None
    assert "GITHUB_PASSWORD" in secrets.environment_names("main", "s1")


def test_source_bound_secrets_resolve_at_use_and_stay_masked(tmp_path: Path) -> None:
    secrets = _secrets(tmp_path)
    source = FakeSource({"UserName": "admin", "Password": "from-the-vault-123"})
    secrets.replace_sources([source])
    secrets.create(
        name="db",
        kind="login",
        values={},
        actor="user",
        agents=["main"],
        source=SourceBinding("vault", "entry-1"),
        source_fields={"username": "UserName", "password": "Password"},
    )

    assert secrets.substitute("main", "s1", "{{secret:DB.password}}", "terminal")[0] == (
        "from-the-vault-123"
    )
    assert secrets.masker.mask("printed from-the-vault-123") == "printed {{secret:DB.password}}"
    source.locked = True
    with pytest.raises(SecretUseError) as raised:
        secrets.substitute("main", "s1", "{{secret:DB.password}}", "terminal")
    assert raised.value.code == "source_failed"
    source.locked = False
    secrets.replace_sources([])
    assert source.lock_calls == 1
    with pytest.raises(SecretUseError):
        secrets.substitute("main", "s1", "{{secret:DB.password}}", "terminal")


def test_masking_replaces_known_values_and_their_encodings_with_references(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets)
    secrets.create(name="short", kind="token", values={"value": "abc"}, actor="user")
    secrets.set_system_values({"OPENAI_API_KEY": TOKEN})
    masker = secrets.masker
    encoded = base64.b64encode(b"user:" + PASSWORD.encode()).decode()
    untouched = {"text": "nothing to see"}

    assert masker.mask(f"pw {PASSWORD}, key {TOKEN}") == (
        "pw {{secret:GITHUB.password}}, key {{secret:OPENAI_API_KEY}}"
    )
    assert "{{secret:GITHUB.password}}" in masker.mask(json.dumps({"p": PASSWORD + '"'}))
    assert masker.mask(quote(PASSWORD + "/", safe="")) == "{{secret:GITHUB.password}}%2F"
    assert "{{secret:GITHUB.password}}" in masker.mask(encoded)
    # Too short to recognize safely.
    assert masker.mask("abc") == "abc"
    assert masker.mask_value(untouched) is untouched
