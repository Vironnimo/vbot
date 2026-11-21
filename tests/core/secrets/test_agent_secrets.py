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

    with pytest.raises(SecretRecordError) as existing:
        _login(secrets)
    with pytest.raises(SecretRecordError) as reserved:
        secrets.create(name="gh", kind="token", values={"value": TOKEN}, actor="user")
    assert str(existing.value) == str(reserved.value)


@pytest.mark.parametrize(
    ("agent_id", "session_id", "text", "sink", "code"),
    [
        ("main", "s1", "{{secret:GITHUB.password}}", "files", "sink_not_allowed"),
        ("other", "s1", "{{secret:GITHUB.password}}", "terminal", "unknown_secret"),
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
    address = SessionAddress(None, "main", "s1")
    assert secrets.masker_for(address).mask("temporary-value") == "{{secret:TEMP}}"
    clock.now += timedelta(hours=2)
    assert secrets.masker_for(address).mask("temporary-value {{secret:TEMP}} otp-12345678") == (
        "[redacted] [redacted] [redacted]"
    )
    assert [item.name for item in secrets.usable(SessionAddress(None, "main", "s1"))] == ["API"]
    assert secrets.sweep() == 1
    assert secrets.masker.mask("temporary-value") == "[redacted]"
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

    # A later forbidden grant must not leave an earlier grant from the batch.
    helper = SessionAddress(None, "helper", "child")
    owner = SessionAddress(None, "main", "s1")
    local = secrets.create(
        name="LOCAL", kind="token", values={"value": TOKEN}, actor="user", agents=["helper"]
    )
    with pytest.raises(SecretRecordError):
        secrets.pass_to_session(helper, ["LOCAL", "GITHUB"], child=owner)
    assert secrets.lookup(owner, "LOCAL", passed_only=True) is None

    assert refused.value.code == "unknown_secret"
    assert unpassed.value.code == "unknown_secret"
    # A permanent Secret given in a Session stays usable there.
    assert secrets.usable(SessionAddress(None, "main", "other-child"), passed_only=True) == [
        requested
    ]
    assert [
        item.name
        for item in secrets.usable(SessionAddress(None, "helper", "child"), passed_only=True)
    ] == ["GITHUB"]
    secrets.remove_session(SessionAddress(None, "helper", "child"))
    assert secrets.usable(helper) == [local]
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
    masker = secrets.masker_for(SessionAddress(None, "main", "s1"))
    assert masker.project_prepared("287082", "{{secret:GITHUB.totp}}") == "{{secret:GITHUB.totp}}"
    clock.now += timedelta(seconds=30)
    assert masker.project_prepared("287082", "{{secret:GITHUB.totp}}") == "[redacted]"


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
    address = SessionAddress(None, "main", "s1")
    assert secrets.masker.mask("printed from-the-vault-123") == "printed [redacted]"
    assert secrets.masker_for(address).mask("from-the-vault-123") == "{{secret:DB.password}}"
    assert secrets.masker_for(address, passed_only=True).mask("from-the-vault-123") == "[redacted]"
    assert secrets.masker_for(address).catalog_source_label("db") == source.label
    source.values["Password"] = "rotated-vault-password"
    secrets.substitute(address, "{{secret:DB.password}}", "terminal")
    assert secrets.masker_for(address).mask("from-the-vault-123 rotated-vault-password") == (
        "[redacted] {{secret:DB.password}}"
    )
    for short in ("123", "456"):
        source.values["Password"] = short
        secrets.substitute(address, "{{secret:DB.password}}", "terminal")
        assert (
            secrets.masker_for(address).project_prepared(short, "{{secret:DB}}") == "{{secret:DB}}"
        )
    assert secrets.masker_for(address).project_prepared("123", "{{secret:DB}}") == "[redacted]"
    source.locked = True
    with pytest.raises(SecretUseError) as raised:
        secrets.substitute(SessionAddress(None, "main", "s1"), "{{secret:DB.password}}", "terminal")
    assert raised.value.code == "source_failed"
    source.locked = False
    secrets.replace_sources([])
    assert source.lock_calls == 1
    assert secrets.masker_for(address).catalog_source_label("db") == "vault"
    assert secrets.masker_for(address).project_prepared("456", "{{secret:DB}}") == "[redacted]"
    with pytest.raises(SecretUseError):
        secrets.substitute(SessionAddress(None, "main", "s1"), "{{secret:DB.password}}", "terminal")


def test_masking_replaces_known_values_and_their_encodings_with_references(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    _login(secrets)
    secrets.create(name="short", kind="token", values={"value": "abc"}, actor="user")
    secrets.set_system_values({"OPENAI_API_KEY": TOKEN})
    address = SessionAddress(None, "main", "s1")
    masker = secrets.masker_for(address)
    encoded = base64.b64encode(b"user:" + PASSWORD.encode()).decode()
    untouched = {"text": "nothing to see"}

    # A visible field, such as the username, stays readable.
    assert masker.mask(f"julian: pw {PASSWORD}, key {TOKEN}") == (
        "julian: pw {{secret:GITHUB.password}}, key [redacted]"
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

    [login] = [item for item in secrets.all() if item.name == "GITHUB"]
    # Unchanged records from a reload or metadata write are still active values.
    secrets.reload()
    secrets.update(login.id, description="new description", actor="user")
    secrets.update(login.id, concealed={"password": False}, actor="user")
    assert secrets.masker.mask(PASSWORD) == PASSWORD
    secrets.update(login.id, concealed={"password": True}, actor="user")
    secrets.update(login.id, values={"password": "rotated-credential"}, actor="user")
    assert secrets.masker_for(address).mask(PASSWORD) == "[redacted]"
    secrets.update(
        login.id, values={"password": PASSWORD}, concealed={"password": False}, actor="user"
    )
    assert secrets.masker.mask(PASSWORD) == PASSWORD
    duplicate = secrets.create(
        name="SHARED", kind="token", values={"value": PASSWORD}, actor="user"
    )
    assert secrets.masker_for(address).mask(PASSWORD) == "[redacted]"
    secrets.update(duplicate.id, concealed={"value": False}, actor="user")
    secrets.set_system_values({"SYSTEM_TOKEN": PASSWORD})
    assert secrets.masker.mask(PASSWORD) == "[redacted]"
    assert secrets.masker.mask(TOKEN) == "[redacted]"  # the replaced system credential


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
        secrets.remember_value("runtime-value-123")
        return (
            secrets.masker_for(SessionAddress(None, "main", "s1")).mask(PASSWORD),
            [item.name for item in secrets.all()],
        )

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
    assert secrets.masker.mask(f"{TOKEN} runtime-value-123") == "[redacted] [redacted]"


def test_stored_references_are_projected_for_the_receiving_session(tmp_path: Path) -> None:
    secrets = _secrets(tmp_path)
    address = SessionAddress(None, "main", "conversation")
    assert secrets.masker_for(address).mask("{{secret:NEVER_CREATED}}") == "[redacted]"
    _login(secrets)
    owned = secrets.masker_for(address)
    child = secrets.masker_for(address, passed_only=True)
    ref = "{{ secret:github.Password }}"
    text = f"Before {ref} after"
    assert owned.allows_name("github") and owned.allows_reference("github", "PASSWORD")
    assert not owned.allows_reference("github", "missing")
    assert not child.allows_name("GITHUB")
    catalog = owned.catalog_record("github")
    assert catalog is not None and [(field.name, field.value) for field in catalog.fields] == [
        ("username", "julian"),
        ("password", None),
    ]
    assert child.catalog_record("github") is None
    assert owned.allows_environment_name("github_password") and not child.allows_environment_name(
        "GITHUB_PASSWORD"
    )
    assert owned.mask(text) == text and not owned.contains(ref)
    assert child.contains(ref) and child.mask(text) == "Before [redacted] after"
    for masker in (child, secrets.masker):
        assert masker.mask("{{secret:NEVER_CREATED}} {{!secret:DOC_EXAMPLE}}") == (
            "[redacted] {{!secret:DOC_EXAMPLE}}"
        )
        assert masker.mask_value({ref: {"value": PASSWORD}}, include_keys=True) == {
            "[redacted]": {"value": "[redacted]"}
        }
        assert masker.mask_growing("Before {{ secret:GITH") == "Before "
        assert masker.stream_boundary(text + "Z" * 200, text.index("github") + 4) == len("Before ")
        assert (
            masker.mask_slice(text, text.index("github"), text.index("Password") + 3)
            == "[redacted]"
        )
        assert masker.mask_slice("start" + PASSWORD + "end", 7, 11) == "[redacted]"
        assert masker.mask_slice(text, 0, 3) == "Bef"
        project = masker.slice_projector(text)
        assert [project(*window) for window in [(0, 1), (8, 15), (10, 12), (10, 10), (-6, -1)]] == [
            "B",
            "[redacted]",
            "[redacted]",
            "",
            " afte",
        ]
    assert owned.mask_slice(text, text.index("github"), text.index("Password") + 3) == ref
    assert owned.mask_growing(PASSWORD) == owned.mask(PASSWORD)
    assert child.mask_growing(PASSWORD) == "[redacted]"
    long_value = PASSWORD + "\n" + "0123456789" * 120 + "-suffix"
    streaming = SecretMasker({PASSWORD: ref, long_value: ref})
    assert streaming.mask_growing("hi " + PASSWORD[:2]) == "hi " + PASSWORD[:2]
    assert streaming.mask_growing("hi " + PASSWORD[:3]) == "hi "
    assert streaming.mask_growing("hi " + long_value[:-2]) == "hi " + ref
    assert streaming.mask_growing("hi " + long_value) == "hi " + ref

    def refusal() -> tuple[str, str]:
        with pytest.raises(SecretUseError) as caught:
            secrets.substitute(address, "{{secret:GUESS}}", "files")
        return caught.value.code, str(caught.value)

    absent = refusal()
    secrets.create(
        name="GUESS", kind="token", actor="user", values={"value": TOKEN}, agents=["other"]
    )
    assert refusal() == absent
    for number, literal in enumerate(
        ("suffix}}after", "before{{secret:", "{{secret:GUESS}}tail", "redacted")
    ):
        secrets.create(
            name=f"RAW_{number}",
            kind="text",
            actor="user",
            values={"value": literal},
            agents=["main"],
        )
    masker = secrets.masker_for(address)
    for overlap in ("{{secret:suffix}}after", "before{{secret:GUESS}}"):
        assert masker.mask(overlap) == masker.mask_slice(overlap, 3, 6) == "[redacted]"
    assert masker.mask("{{secret:GUESS}}tail") == "{{secret:RAW_2}}"
    assert masker.mask("[redacted]") == "[redacted]"


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
    assert secrets.masker_for(descendant, passed_only=True).mask(TOKEN) == "{{secret:API}}"
    secrets = _secrets(tmp_path)
    assert secrets.lookup(child, "API", passed_only=True) is not None
    assert secrets.lookup(descendant, "API", passed_only=True) is not None
    if lifetime == "permanent":
        secrets.update(secret.id, actor="user", agents=[])
        assert secrets.usable(owner) == []
    else:
        secrets.remove_session(owner)
    assert secrets.usable(child, passed_only=True) == []
    assert secrets.masker_for(descendant, passed_only=True).mask(TOKEN + " {{secret:API}}") == (
        "[redacted] [redacted]"
    )
    assert _secrets(tmp_path).usable(descendant, passed_only=True) == []


def test_capture_api_keys_is_atomic_idempotent_and_never_replaces(
    tmp_path: Path, monkeypatch
) -> None:
    secrets = _secrets(tmp_path)
    address = SessionAddress(None, "main", "s1")
    secrets.create(
        name="API_KEY",
        kind="token",
        values={"value": TOKEN},
        actor="user",
        lifetime="session",
        session_id="s1",
        agents=["main"],
    )
    other = "different-value-123456789"
    secrets.capture_api_keys(address, [("API_KEY", other), ("API_KEY", other), ("API_KEY", TOKEN)])
    assert [item.name for item in secrets.usable(address)] == ["API_KEY", "API_KEY_2"]
    assert secrets.masker_for(address).mask(other) == "{{secret:API_KEY_2}}"
    secrets.capture_api_keys(address, [("API_KEY", other)])
    unrelated = SessionAddress(None, "other", "s2")
    secrets.capture_api_keys(unrelated, [("API_KEY", other)])
    assert not secrets.usable(unrelated)
    assert len(secrets.all()) == 2
    assert other not in (tmp_path / "data" / "secrets.json").read_text(encoding="utf-8")

    def fail(_change):
        raise OSError("store unavailable")

    monkeypatch.setattr(secrets._store, "update", fail)
    with pytest.raises(OSError):
        secrets.capture_api_keys(
            address, [("ONE", "new-secret-value-123"), ("TWO", "another-secret-value-456")]
        )
    assert len(secrets.all()) == 2
    assert secrets.masker.mask("new-secret-value-123") == "new-secret-value-123"


def test_tool_capture_grants_only_the_session_that_independently_read_the_value(
    tmp_path: Path,
) -> None:
    secrets = _secrets(tmp_path)
    first = SessionAddress(None, "main", "one")
    second = SessionAddress("project", "helper", "two")
    candidates = [("API_KEY", TOKEN)]
    references = secrets.capture_api_keys(first, candidates, source="tool")
    assert references == {TOKEN: "{{secret:API_KEY}}"}
    assert not secrets.usable(second, passed_only=True)

    other_references = secrets.capture_api_keys(second, candidates, source="tool", passed_only=True)
    assert other_references == {TOKEN: "{{secret:API_KEY}}"}
    assert secrets.masker_for(first).mask(TOKEN) == "{{secret:API_KEY}}"
    assert secrets.masker_for(second, passed_only=True).mask(TOKEN) == "{{secret:API_KEY}}"
    assert secrets.capture_api_keys(first, candidates, source="tool") == references
    assert len(secrets.all()) == 2
    assert (
        secrets.substitute(second, other_references[TOKEN], "files", passed_only=True)[0] == TOKEN
    )
    secrets.remove_session(first)
    assert not secrets.usable(first)
    assert (
        secrets.substitute(second, other_references[TOKEN], "files", passed_only=True)[0] == TOKEN
    )
    reopened = _secrets(tmp_path)
    assert (
        reopened.substitute(second, other_references[TOKEN], "files", passed_only=True)[0] == TOKEN
    )
    [second_secret] = reopened.usable(second, passed_only=True)
    reopened.update(second_secret.id, actor="user", sinks=[])
    third = SessionAddress(None, "main", "three")
    assert reopened.capture_api_keys(third, candidates, source="tool") == {}
    assert not reopened.usable(third)
