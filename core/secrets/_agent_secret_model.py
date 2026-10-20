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
    #: Agents a Secret is granted to, by Agent id.
    agents: tuple[str, ...] = ()
    #: Sessions a Secret was passed to explicitly (Sub-Agent pass-through).
    sessions: tuple[str, ...] = ()
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

    def granted_to(self, agent_id: str, session_id: str, *, passed_only: bool = False) -> bool:
        """Whether the Agent of ``session_id`` may use this Secret there.

        ``passed_only`` (a Sub-Agent Session) leaves out the grants to the Agent.
        """
        if session_id in self.sessions:
            return True
        if self.lifetime in ("session", "once"):
            # Records written before Session Secrets kept their Agent have none.
            return self.session_id == session_id and (not self.agents or agent_id in self.agents)
        return not passed_only and agent_id in self.agents


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
