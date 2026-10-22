"""Agent Secrets: storage, grants, lifetimes, substitution, sources and masking."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest

from core.secrets.agent_secrets import (
    AgentSecrets,
    Lifetime,
    SecretMasker,
    SecretRecordError,
    SecretUse,
    SecretUseError,
    SourceBinding,
)
from core.secrets.sources import SecretSourceError, SourceEntry, SourceStatus, UnlockField
from core.sessions import SessionAddress
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
        self.optional_fields: set[str] = set()
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
        if field_name not in self.values:
            raise SecretSourceError(
                "optional_field_missing" if field_name in self.optional_fields else "not_found",
                f"No {field_name} field",
            )
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
    (secret,) = reopened.usable(SessionAddress(None, "main", "s1"))
    described = reopened.describe(secret)

    assert secret.name == "GITHUB"
    assert reopened.substitute(
        SessionAddress(None, "main", "s1"), "{{secret:github.password}}", "terminal"
    )[0] == (PASSWORD)
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
        secrets.substitute(SessionAddress(None, agent_id, session_id), text, sink)  # type: ignore[arg-type]

    assert raised.value.code == code
    assert PASSWORD not in str(raised.value)


def test_substitution_replaces_references_in_nested_values_and_reports_uses(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets)

    value, uses = secrets.substitute(
        SessionAddress(None, "main", "s1"),
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
    changes: list[str] = []
    secrets.add_change_listener(lambda change: changes.append(change.kind))
    secrets.create(
        name="code",
        kind="token",
        values={"value": "otp-12345678"},
        actor="user",
        lifetime="once",
        session_id="s1",
        agents=["main"],
    )
    secrets.create(
        name="api",
        kind="token",
        values={"value": TOKEN},
        actor="user",
        lifetime="session",
        session_id="s1",
        agents=["main"],
    )
    secrets.create(
        name="temp",
        kind="token",
        values={"value": "temporary-value"},
        actor="user",
        agents=["main"],
        expires_at=clock.now + timedelta(hours=1),
    )

    # A call whose other arguments fail does not consume a once Secret;
    # consume ends it when the call runs.
    _, uses = secrets.substitute(
        SessionAddress(None, "main", "s1"), "{{secret:CODE}}", "files", consume=False
    )
    secrets.consume(SessionAddress(None, "main", "s1"), uses)
    with pytest.raises(SecretUseError):
        secrets.substitute(SessionAddress(None, "main", "s1"), "{{secret:CODE}}", "files")
    secrets.create(
        name="code",
        kind="token",
        values={"value": "otp-12345678"},
        actor="user",
        lifetime="once",
        session_id="s1",
        agents=["main"],
    )
    # A once Secret named twice in one call is used once, then it is gone,
    # through the environment too.
    assert secrets.substitute(
        SessionAddress(None, "main", "s1"), "{{secret:CODE}} {{secret:CODE}}", "files"
    )[0] == ("otp-12345678 otp-12345678")
    with pytest.raises(SecretUseError):
        secrets.substitute(SessionAddress(None, "main", "s1"), "{{secret:CODE}}", "files")
    secrets.create(
        name="code",
        kind="token",
        values={"value": "otp-12345678"},
        actor="user",
        lifetime="once",
        session_id="s1",
        agents=["main"],
    )
    assert secrets.environment(SessionAddress(None, "main", "s1"), "CODE") is not None
    assert secrets.lookup(SessionAddress(None, "main", "s1"), "CODE") is None
    # A Session Secret exists only in its Session, and only for its Agent.
    assert [item.name for item in secrets.usable(SessionAddress(None, "main", "s2"))] == ["TEMP"]
    assert secrets.usable(SessionAddress(None, "other", "s1")) == []
    clock.now += timedelta(hours=2)
    assert [item.name for item in secrets.usable(SessionAddress(None, "main", "s1"))] == ["API"]
    assert secrets.sweep() == 1
    assert secrets.masker.mask("temporary-value") == "{{secret:TEMP}}"
    changes.clear()
    secrets.remove_session(SessionAddress(None, "main", "s1"))
    assert [item.name for item in _secrets(tmp_path, clock).all()] == []
    assert changes == ["deleted"]


def test_a_parent_passes_only_its_own_secrets_and_the_grant_ends_with_the_child(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets)

    secrets.pass_to_session(
        SessionAddress(None, "main", "s1"),
        ["GITHUB"],
        child=SessionAddress(None, "helper", "child"),
    )
    with pytest.raises(SecretUseError):
        secrets.pass_to_session(
            SessionAddress(None, "helper", "child"),
            ["GITLAB"],
            child=SessionAddress(None, "helper", "grandchild"),
        )
    # A Sub-Agent Session of the same Agent has none of the Agent's Secrets
    # unless they were passed, and passes on only what it has.
    with pytest.raises(SecretUseError) as refused:
        secrets.pass_to_session(
            SessionAddress(None, "main", "other-child"),
            ["GITHUB"],
            child=SessionAddress(None, "helper", "grandchild"),
            passed_only=True,
        )
    with pytest.raises(SecretUseError) as unpassed:
        secrets.substitute(
            SessionAddress(None, "main", "other-child"),
            "{{secret:GITHUB.password}}",
            "typing",
            passed_only=True,
        )
    requested = secrets.create(
        name="stripe",
        kind="token",
        values={"value": TOKEN},
        actor="user",
        session_id="other-child",
        agents=["main"],
    )

    assert refused.value.code == "unknown_secret"
    assert unpassed.value.code == "not_granted"
    assert "not passed to you" in str(unpassed.value)
    # A permanent Secret given in a Session stays usable there.
    assert secrets.usable(SessionAddress(None, "main", "other-child"), passed_only=True) == [
        requested
    ]
    assert [
        item.name
        for item in secrets.usable(SessionAddress(None, "helper", "child"), passed_only=True)
    ] == ["GITHUB"]
    secrets.remove_session(SessionAddress(None, "helper", "child"))
    assert secrets.usable(SessionAddress(None, "helper", "child")) == []
    assert [item.name for item in secrets.usable(SessionAddress(None, "main", "s1"))] == [
        "GITHUB",
        "STRIPE",
    ]


def test_environment_gives_every_field_for_a_name_or_one_field_for_its_variable(
    tmp_path: Path,
) -> None:
    clock = Clock()
    clock.now = datetime.fromtimestamp(59, UTC)
    secrets = _secrets(tmp_path, clock)
    _login(secrets)
    secret_id = secrets.usable(SessionAddress(None, "main", "s1"))[0].id
    # A field added later is checked like one given at creation.
    with pytest.raises(SecretRecordError):
        secrets.update(secret_id, actor="user", values={"totp": "not a seed!"})
    secrets.update(secret_id, actor="user", values={"totp": TOTP_SEED})

    everything = secrets.environment(SessionAddress(None, "main", "s1"), "GITHUB")
    one = secrets.environment(SessionAddress(None, "main", "s1"), "github_password")

    assert everything is not None and everything[0] == {
        "GITHUB_USERNAME": "julian",
        "GITHUB_PASSWORD": PASSWORD,
        "GITHUB_TOTP": "287082",
    }
    assert one is not None and one[0] == {"GITHUB_PASSWORD": PASSWORD}
    assert secrets.environment(SessionAddress(None, "main", "s1"), "PATH") is None
    assert "GITHUB_PASSWORD" in secrets.environment_names(SessionAddress(None, "main", "s1"))


def test_source_environment_skips_only_absent_optional_fields(tmp_path: Path) -> None:
    secrets = _secrets(tmp_path)
    source = FakeSource({"username": "admin", "password": PASSWORD})
    source.optional_fields = {"totp", "notes"}
    secrets.replace_sources([source])
    secrets.create(
        name="db",
        kind="login",
        values={},
        actor="user",
        agents=["main"],
        source=SourceBinding("vault", "entry-1"),
        source_fields={name: name for name in ("username", "password", "totp", "notes")},
    )
    address = SessionAddress(None, "main", "s1")
    result = secrets.environment(address, "DB")
    assert result is not None and result[0] == {"DB_USERNAME": "admin", "DB_PASSWORD": PASSWORD}
    assert [use.field for use in result[1]] == ["username", "password"]
    for value in ("DB_TOTP", "DB_NOTES"):
        with pytest.raises(SecretUseError) as missing:
            secrets.environment(address, value)
        assert missing.value.code == "optional_field_missing"
    with pytest.raises(SecretUseError):
        secrets.substitute(address, "{{secret:DB.totp}}", "terminal")
    del source.values["password"]
    with pytest.raises(SecretUseError) as mandatory:
        secrets.environment(address, "DB")
    assert mandatory.value.code == "source_failed"
    source.locked = True
    with pytest.raises(SecretUseError) as locked:
        secrets.environment(address, "DB")
    assert locked.value.code == "source_failed"


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

    assert secrets.substitute(
        SessionAddress(None, "main", "s1"), "{{secret:DB.password}}", "terminal"
    )[0] == ("from-the-vault-123")
    assert secrets.masker.mask("printed from-the-vault-123") == "printed {{secret:DB.password}}"
    source.locked = True
    with pytest.raises(SecretUseError) as raised:
        secrets.substitute(SessionAddress(None, "main", "s1"), "{{secret:DB.password}}", "terminal")
    assert raised.value.code == "source_failed"
    source.locked = False
    secrets.replace_sources([])
    assert source.lock_calls == 1
    with pytest.raises(SecretUseError):
        secrets.substitute(SessionAddress(None, "main", "s1"), "{{secret:DB.password}}", "terminal")


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

    # A visible field, such as the username, stays readable.
    assert masker.mask(f"julian: pw {PASSWORD}, key {TOKEN}") == (
        "julian: pw {{secret:GITHUB.password}}, key {{secret:OPENAI_API_KEY}}"
    )
    assert "{{secret:GITHUB.password}}" in masker.mask(json.dumps({"p": PASSWORD + '"'}))
    assert masker.mask(quote(PASSWORD + "/", safe="")) == "{{secret:GITHUB.password}}%2F"
    assert "{{secret:GITHUB.password}}" in masker.mask(encoded)
    # Partial, lowercase and form percent-encoding, and GNU base64's wrapped lines.
    special = "p@ss w/rd*(x)!"
    long_value = "k" * 40 + "-" + "z" * 40
    masker = SecretMasker({special: "{{secret:S}}", long_value: "{{secret:L}}"})
    for text in (
        quote(special, safe="!~*'()"),
        quote(special, safe=""),
        quote(special, safe="").lower(),
        quote_plus(special, safe=""),
    ):
        assert masker.mask(text) == "{{secret:S}}", text
    wrapped = base64.encodebytes(long_value.encode()).decode()
    assert wrapped.count("\n") == 2
    assert set(masker.mask(wrapped).split()) == {"{{secret:L}}"}
    # Too short to recognize safely.
    assert masker.mask("abc") == "abc"
    assert masker.mask_value(untouched) is untouched


def test_reads_and_masking_never_wait_for_a_store_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_store(tmp_path, data_directory(tmp_path))
    secrets = AgentSecrets(store, clock=Clock())
    secrets.reload()
    _login(secrets)
    writing, release = threading.Event(), threading.Event()
    update = store.update

    def slow_update(change: Any) -> None:
        writing.set()
        release.wait(5)
        update(change)

    monkeypatch.setattr(store, "update", slow_update)

    def read() -> tuple[str, list[str]]:
        secrets.remember_value("runtime-value-123", "{{secret:RUNTIME}}")
        return secrets.masker.mask(PASSWORD), [item.name for item in secrets.all()]

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = pool.submit(
            secrets.create, name="api", kind="token", values={"value": TOKEN}, actor="user"
        )
        assert writing.wait(5)
        # The write holds its lock across the store write; the Event Loop's
        # reads and masking do not wait for it.
        masked, names = pool.submit(read).result(timeout=2)
        release.set()
        created.result(timeout=5)

    assert (masked, names) == ("{{secret:GITHUB.password}}", ["GITHUB"])
    assert secrets.masker.mask(f"{TOKEN} runtime-value-123") == (
        "{{secret:API}} {{secret:RUNTIME}}"
    )


@pytest.mark.parametrize("lifetime", ["permanent", "session", "once"])
def test_grants_use_full_addresses_and_revocation_reaches_descendants(
    tmp_path: Path, lifetime: Lifetime
) -> None:
    secrets = _secrets(tmp_path)
    owner = SessionAddress("alpha", "main", "same")
    child = SessionAddress("beta", "helper", "same")
    descendant = SessionAddress(None, "helper", "descendant")
    secret = secrets.create(
        name="API",
        kind="token",
        values={"value": TOKEN},
        actor="user",
        agents=["main@alpha"],
        session_id="same",
        lifetime=lifetime,
    )
    for outsider in [
        SessionAddress(None, "main", "same"),
        SessionAddress("beta", "main", "same"),
        SessionAddress("alpha", "other", "same"),
    ]:
        assert secrets.usable(outsider) == []
        secrets.remove_session(outsider)
        assert secrets.lookup(owner, "API") is not None
    secrets.pass_to_session(owner, ["API"], child)
    secrets.pass_to_session(child, ["API"], descendant, passed_only=True)
    secrets = _secrets(tmp_path)
    assert secrets.lookup(child, "API", passed_only=True) is not None
    assert secrets.lookup(descendant, "API", passed_only=True) is not None
    if lifetime == "permanent":
        secrets.update(secret.id, actor="user", agents=[])
        assert secrets.usable(owner) == []
    else:
        secrets.remove_session(owner)
    assert secrets.usable(child, passed_only=True) == []
    assert _secrets(tmp_path).usable(descendant, passed_only=True) == []
