"""Agent Secret records: names, kinds, fields, lifetimes, grants and their codec.

A record is one sealed JSON object in the Secret Store's ``agent_secrets``
section, keyed by its Secret id. It holds metadata and the field values of a
Secret kept in the store; a Secret bound to a Secret Source holds only the
source reference. Unknown record fields are preserved on rewrite.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, Literal, cast

from core.secrets._totp import TotpSeedError, check_totp_seed
from core.sessions import SessionAddress
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp

SecretKind = Literal["token", "login", "text"]
SinkClass = Literal["environment", "terminal", "files", "typing", "external_tools"]
Lifetime = Literal["permanent", "session", "until", "once"]

SECRET_KINDS: Final[tuple[SecretKind, ...]] = ("token", "login", "text")
SINK_CLASSES: Final[tuple[SinkClass, ...]] = (
    "environment",
    "terminal",
    "files",
    "typing",
    "external_tools",
)
LIFETIMES: Final[tuple[Lifetime, ...]] = ("permanent", "session", "until", "once")

#: A Secret name: an environment-variable style name, so the same name works in
#: ``{{secret:NAME}}`` and as a shell variable.
SECRET_NAME_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
#: A field name of a Secret.
FIELD_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: A Secret id (the store entry name).
SECRET_ID_PREFIX: Final = "sec"

#: The field of a one-value Secret.
VALUE_FIELD: Final = "value"
#: A login's TOTP seed; resolving it yields the current code.
TOTP_FIELD: Final = "totp"

RECORD_VERSION: Final = 1
_MAX_DESCRIPTION = 500
_MAX_VALUE = 64 * 1024

#: The fields each kind starts with, and whether each is concealed.
KIND_FIELDS: Final[Mapping[SecretKind, tuple[tuple[str, bool], ...]]] = {
    "token": ((VALUE_FIELD, True),),
    "login": (("username", False), ("password", True), ("url", False), (TOTP_FIELD, True)),
    "text": ((VALUE_FIELD, True),),
}
#: Fields of a kind that may be left out.
OPTIONAL_KIND_FIELDS: Final[Mapping[SecretKind, frozenset[str]]] = {
    "token": frozenset(),
    "login": frozenset({"url", TOTP_FIELD, "username"}),
    "text": frozenset(),
}
#: The sink classes a new Secret of each kind allows.
DEFAULT_SINKS: Final[Mapping[SecretKind, frozenset[SinkClass]]] = {
    "token": frozenset({"environment", "files"}),
    "login": frozenset({"environment", "terminal", "typing"}),
    "text": frozenset({"environment", "terminal", "files", "typing"}),
}

_KNOWN_RECORD_FIELDS = frozenset(
    {
        "v",
        "name",
        "kind",
        "description",
        "fields",
        "lifetime",
        "session_id",
        "expires_at",
        "agents",
        "sessions",
        "session_grants",
        "sinks",
        "source",
        "created_at",
        "created_by",
    }
)


class SecretRecordError(ValueError):
    """A Secret record or a requested change is invalid; nothing was stored."""


@dataclass(frozen=True)
class SecretField:
    """One field of a Secret. ``value`` is ``None`` for a field a source supplies."""

    name: str
    concealed: bool
    value: str | None = field(default=None, repr=False)
    #: The source's own field name, for a Secret bound to a Secret Source.
    source_field: str | None = None


@dataclass(frozen=True)
class SourceBinding:
    """Where a Secret's values come from when not from the store itself."""

    source: str
    reference: str


@dataclass(frozen=True)
class SessionGrant:
    """A pass-through path rooted in one explicit Agent grant."""

    agent: str
    path: tuple[SessionAddress, ...]


def agent_address(address: SessionAddress) -> str:
    from core.projects.address import format_agent_address

    return format_agent_address(address.agent_id, address.project_id)


def session_address(agent: str, session_id: str) -> SessionAddress:
    from core.projects.address import InvalidAgentAddressError, parse_agent_address

    try:
        agent_id, project_id = parse_agent_address(agent)
    except InvalidAgentAddressError as error:
        raise SecretRecordError(str(error)) from None
    return SessionAddress(project_id, agent_id, session_id)


@dataclass(frozen=True)
class AgentSecret:
    """One Secret: what an Agent may use by name without seeing its value."""

    id: str
    name: str
    kind: SecretKind
    fields: tuple[SecretField, ...]
    lifetime: Lifetime
    created_at: datetime
    created_by: str
    description: str = ""
    #: The Session a ``session`` or ``once`` Secret belongs to.
    session_id: str | None = None
    expires_at: datetime | None = None
    #: Qualified Agent addresses (a bare id denotes an Identity Agent).
    agents: tuple[str, ...] = ()
    #: Sessions a Secret was passed to explicitly (Sub-Agent pass-through).
    sessions: tuple[str, ...] = ()
    session_grants: tuple[SessionGrant, ...] = ()
    sinks: frozenset[SinkClass] = frozenset()
    source: SourceBinding | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def field_named(self, name: str) -> SecretField | None:
        return next((item for item in self.fields if item.name == name), None)

    @property
    def single_value(self) -> bool:
        """Whether the Secret has one field, so ``{{secret:NAME}}`` names it."""
        return len(self.fields) == 1

    def default_field(self) -> SecretField | None:
        """The field ``{{secret:NAME}}`` without a field part refers to."""
        if self.single_value:
            return self.fields[0]
        return self.field_named("password") or self.field_named(VALUE_FIELD)

    def reference(self, field_name: str | None = None) -> str:
        """The reference text for this Secret or one of its fields."""
        default = self.default_field()
        if field_name is None or (self.single_value and default and default.name == field_name):
            return f"{{{{secret:{self.name}}}}}"
        return f"{{{{secret:{self.name}.{field_name}}}}}"

    def environment_names(self) -> dict[str, str]:
        """Shell variable name for each field: ``NAME`` or ``NAME_<FIELD>``."""
        if self.single_value:
            return {self.fields[0].name: self.name}
        return {item.name: f"{self.name}_{item.name.upper()}" for item in self.fields}

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def grant_for(
        self, address: SessionAddress, *, passed_only: bool = False
    ) -> SessionGrant | None:
        """The authority for this exact Session, including the root of a passed grant."""
        agent = agent_address(address)
        local = self.lifetime in ("session", "once")
        if agent in self.agents and (
            self.session_id == address.session_id if local else not passed_only
        ):
            return SessionGrant(agent, (address,))
        for grant in self.session_grants:
            if grant.path[-1] != address or grant.agent not in self.agents:
                continue
            if local and grant.path[0] != session_address(grant.agent, self.session_id or ""):
                continue
            return grant
        return None

    def granted_to(self, address: SessionAddress, *, passed_only: bool = False) -> bool:
        return self.grant_for(address, passed_only=passed_only) is not None


def normalize_secret_name(raw: str) -> str:
    """``api-key`` -> ``API_KEY``; raises :class:`SecretRecordError` when unusable."""
    text = re.sub(r"[\s.\-/:]+", "_", str(raw).strip()).upper()
    text = re.sub(r"[^A-Z0-9_]", "", text).strip("_")
    if not text or not text[0].isalpha():
        raise SecretRecordError(
            f"Secret names start with a letter and use letters, digits and underscores: {raw!r}."
        )
    text = text[:64]
    if SECRET_NAME_PATTERN.fullmatch(text) is None:
        raise SecretRecordError(f"Not a usable Secret name: {raw!r}.")
    return text


def normalize_field_name(raw: str) -> str:
    text = re.sub(r"[\s.\-]+", "_", str(raw).strip()).lower()
    if FIELD_NAME_PATTERN.fullmatch(text) is None:
        raise SecretRecordError(
            f"Field names start with a letter and use letters, digits and underscores: {raw!r}."
        )
    return text


def check_value(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SecretRecordError(f"The field {field_name} needs a value.")
    if len(value) > _MAX_VALUE:
        raise SecretRecordError(f"The field {field_name} is longer than {_MAX_VALUE} characters.")
    if "\x00" in value:
        raise SecretRecordError(f"The field {field_name} contains a NUL character.")
    return value


def check_description(description: str) -> str:
    text = str(description or "").strip()
    if len(text) > _MAX_DESCRIPTION:
        raise SecretRecordError(f"Descriptions have at most {_MAX_DESCRIPTION} characters.")
    return text


def kind_fields(kind: SecretKind) -> tuple[tuple[str, bool], ...]:
    return KIND_FIELDS[kind]


def check_sinks(sinks: Iterable[str]) -> frozenset[SinkClass]:
    chosen = set()
    for sink in sinks:
        if sink not in SINK_CLASSES:
            raise SecretRecordError(
                f"Unknown sink class {sink!r}; use one of {', '.join(SINK_CLASSES)}."
            )
        chosen.add(cast(SinkClass, sink))
    return frozenset(chosen)


def encode_record(secret: AgentSecret) -> str:
    """The record text sealed under the Secret's id."""
    data: dict[str, Any] = dict(secret.extra)
    data.update(
        {
            "v": RECORD_VERSION,
            "name": secret.name,
            "kind": secret.kind,
            "description": secret.description,
            "fields": {item.name: _encode_field(item) for item in secret.fields},
            "lifetime": secret.lifetime,
            "agents": list(secret.agents),
            "sessions": list(secret.sessions),
            "session_grants": [
                {
                    "agent": grant.agent,
                    "path": [
                        {
                            "project_id": address.project_id,
                            "agent_id": address.agent_id,
                            "session_id": address.session_id,
                        }
                        for address in grant.path
                    ],
                }
                for grant in secret.session_grants
            ],
            "sinks": sorted(secret.sinks),
            "created_at": format_canonical_timestamp(secret.created_at),
            "created_by": secret.created_by,
        }
    )
    data.pop("session_id", None)
    data.pop("expires_at", None)
    data.pop("source", None)
    if secret.session_id is not None:
        data["session_id"] = secret.session_id
    if secret.expires_at is not None:
        data["expires_at"] = format_canonical_timestamp(secret.expires_at)
    if secret.source is not None:
        data["source"] = {"source": secret.source.source, "reference": secret.source.reference}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode_field(item: SecretField) -> dict[str, Any]:
    encoded: dict[str, Any] = {"concealed": item.concealed}
    if item.value is not None:
        encoded["value"] = item.value
    if item.source_field is not None:
        encoded["source_field"] = item.source_field
    return encoded


def decode_record(secret_id: str, text: str) -> AgentSecret:
    """Parse one sealed record; :class:`SecretRecordError` names what is wrong."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SecretRecordError(f"Secret {secret_id} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise SecretRecordError(f"Secret {secret_id} is not an object")
    name = data.get("name")
    kind = data.get("kind")
    lifetime = data.get("lifetime")
    if not isinstance(name, str) or SECRET_NAME_PATTERN.fullmatch(name) is None:
        raise SecretRecordError(f"Secret {secret_id} has no valid name")
    if kind not in SECRET_KINDS:
        raise SecretRecordError(f"Secret {secret_id} has an unknown kind")
    if lifetime not in LIFETIMES:
        raise SecretRecordError(f"Secret {secret_id} has an unknown lifetime")
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise SecretRecordError(f"Secret {secret_id} has no fields")
    fields: list[SecretField] = []
    for field_name, raw in raw_fields.items():
        if not isinstance(field_name, str) or FIELD_NAME_PATTERN.fullmatch(field_name) is None:
            raise SecretRecordError(f"Secret {secret_id} has an invalid field name")
        if not isinstance(raw, dict):
            raise SecretRecordError(f"Secret {secret_id} has an invalid field")
        value = raw.get("value")
        source_field = raw.get("source_field")
        fields.append(
            SecretField(
                name=field_name,
                concealed=bool(raw.get("concealed", True)),
                value=value if isinstance(value, str) else None,
                source_field=source_field if isinstance(source_field, str) else None,
            )
        )
    source = data.get("source")
    binding = None
    if isinstance(source, dict):
        if not isinstance(source.get("source"), str) or not isinstance(
            source.get("reference"), str
        ):
            raise SecretRecordError(f"Secret {secret_id} has an invalid source binding")
        binding = SourceBinding(source=source["source"], reference=source["reference"])
    try:
        created_at = parse_timestamp(str(data.get("created_at")))
        expires_raw = data.get("expires_at")
        expires_at = parse_timestamp(expires_raw) if isinstance(expires_raw, str) else None
    except ValueError as exc:
        raise SecretRecordError(f"Secret {secret_id} has an invalid timestamp") from exc
    sinks = [item for item in _strings(data.get("sinks")) if item in SINK_CLASSES]
    session_id = data.get("session_id")
    return AgentSecret(
        id=secret_id,
        name=name,
        kind=cast(SecretKind, kind),
        description=str(data.get("description") or ""),
        fields=tuple(fields),
        lifetime=cast(Lifetime, lifetime),
        session_id=session_id if isinstance(session_id, str) else None,
        expires_at=expires_at,
        agents=tuple(_strings(data.get("agents"))),
        sessions=tuple(_strings(data.get("sessions"))),
        session_grants=_session_grants(data.get("session_grants", [])),
        sinks=frozenset(cast(Sequence[SinkClass], sinks)),
        source=binding,
        created_at=created_at,
        created_by=str(data.get("created_by") or "user"),
        extra={key: value for key, value in data.items() if key not in _KNOWN_RECORD_FIELDS},
    )


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def with_changes(secret: AgentSecret, **changes: Any) -> AgentSecret:
    return replace(secret, **changes)


def _session_grants(raw: Any) -> tuple[SessionGrant, ...]:
    if not isinstance(raw, list):
        raise SecretRecordError("Invalid Session grant evidence")
    grants = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("agent"), str):
            raise SecretRecordError("Invalid Session grant owner")
        path = item.get("path")
        if not isinstance(path, list) or not path:
            raise SecretRecordError("Invalid Session grant path")
        addresses = []
        for address in path:
            if (
                not isinstance(address, dict)
                or not isinstance(address.get("agent_id"), str)
                or not isinstance(address.get("session_id"), str)
                or address.get("project_id") is not None
                and not isinstance(address["project_id"], str)
            ):
                raise SecretRecordError("Invalid Session grant address")
            addresses.append(
                SessionAddress(
                    address.get("project_id"), address["agent_id"], address["session_id"]
                )
            )
        grants.append(SessionGrant(item["agent"], tuple(addresses)))
    return tuple(grants)


def _check_lifetime(
    lifetime: Lifetime, session_id: str | None, expires_at: datetime | None, now: datetime
) -> None:
    if lifetime in ("session", "once") and not session_id:
        raise SecretRecordError(f"A {lifetime} Secret needs its Session.")
    if lifetime == "until" and expires_at is None:
        raise SecretRecordError("A time-limited Secret needs an expiry time.")
    if expires_at is not None and expires_at <= now:
        raise SecretRecordError("The expiry time has already passed.")


def with_given_lifetime(
    secret: AgentSecret,
    lifetime: Lifetime,
    address: SessionAddress,
    expires_at: datetime | None,
    now: datetime,
) -> AgentSecret:
    """Apply the user's replacement scope, retaining only grants it still permits."""
    _check_lifetime(lifetime, address.session_id, expires_at, now)
    if lifetime == "permanent" and expires_at is not None:
        lifetime = "until"
    agent = agent_address(address)
    local = lifetime in ("session", "once")
    agents = (agent,) if local else tuple(dict.fromkeys((*secret.agents, agent)))
    grants = (
        ()
        if local
        else tuple(
            dict.fromkeys(
                (
                    *secret.session_grants,
                    SessionGrant(agent, (address,)),
                )
            )
        )
    )
    return replace(
        secret,
        lifetime=lifetime,
        expires_at=expires_at,
        session_id=address.session_id if local else None,
        agents=agents,
        session_grants=grants,
        sessions=tuple(dict.fromkeys(grant.path[-1].session_id for grant in grants)),
    )


def _build_fields(
    kind: SecretKind,
    values: Mapping[str, str],
    concealed: Mapping[str, bool],
    source_fields: Mapping[str, str] | None,
    *,
    bound: bool,
) -> tuple[SecretField, ...]:
    """The fields of a new Secret: stored values, or the source fields of a bound one."""
    defaults = dict(KIND_FIELDS[kind])
    if bound:
        if any(values.values()):
            raise SecretRecordError("A Secret from a source takes all its values from the source.")
        mapped = {normalize_field_name(key): value for key, value in (source_fields or {}).items()}
        if not mapped:
            raise SecretRecordError("A Secret from a source needs the source fields it uses.")
        return tuple(
            SecretField(
                name=name,
                concealed=bool(concealed.get(name, defaults.get(name, True))),
                source_field=source_field,
            )
            for name, source_field in mapped.items()
        )
    given = {normalize_field_name(key): value for key, value in values.items() if value != ""}
    for name, _hidden in KIND_FIELDS[kind]:
        if name not in given and name not in OPTIONAL_KIND_FIELDS[kind]:
            raise SecretRecordError(f"A {kind} Secret needs its {name}.")
    names = [name for name in defaults if name in given]
    names += [name for name in given if name not in defaults]
    if not names:
        raise SecretRecordError("A Secret needs at least one field.")
    fields: list[SecretField] = []
    for name in names:
        value = _checked_value(name, given[name])
        hidden = bool(concealed.get(name, defaults.get(name, True)))
        fields.append(SecretField(name=name, concealed=hidden, value=value))
    return tuple(fields)


def _checked_value(name: str, value: str) -> str:
    checked = check_value(value, field_name=name)
    if name == TOTP_FIELD:
        try:
            check_totp_seed(checked)
        except TotpSeedError as error:
            raise SecretRecordError(f"The totp field is not a TOTP seed: {error}.") from None
    return checked


def _with_values(secret: AgentSecret, values: Mapping[str, str]) -> tuple[SecretField, ...]:
    updates = {normalize_field_name(key): value for key, value in values.items()}
    fields = []
    for item in secret.fields:
        if item.name in updates:
            fields.append(replace(item, value=_checked_value(item.name, updates.pop(item.name))))
        else:
            fields.append(item)
    defaults = dict(KIND_FIELDS[secret.kind])
    for name, value in updates.items():
        fields.append(
            SecretField(
                name=name,
                concealed=defaults.get(name, True),
                value=_checked_value(name, value),
            )
        )
    return tuple(fields)
