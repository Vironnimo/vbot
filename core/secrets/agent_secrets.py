"""Agent Secrets: values Agents use by reference without seeing them.

:class:`AgentSecrets` owns the Secrets of one data directory: their records in
the Secret Store's ``agent_secrets`` section, lifetimes and grants, the Secret
Sources that supply bound values, resolution of a reference for one Agent and
Session, and the set of known values that masking replaces with references.

Tools never resolve values themselves: the Tool registry substitutes references
in declared sink arguments (:meth:`AgentSecrets.substitute`), and Chat masks
every Tool result, user message and Provider request with :attr:`masker`.
"""

from __future__ import annotations

import secrets as _random
import string
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Any, Final, Literal

from core.secrets._agent_secret_model import (
    DEFAULT_SINKS,
    KIND_FIELDS,
    LIFETIMES,
    OPTIONAL_KIND_FIELDS,
    SECRET_ID_PREFIX,
    SECRET_KINDS,
    SINK_CLASSES,
    TOTP_FIELD,
    AgentSecret,
    Lifetime,
    SecretField,
    SecretKind,
    SecretRecordError,
    SinkClass,
    SourceBinding,
    check_description,
    check_sinks,
    check_value,
    decode_record,
    encode_record,
    normalize_field_name,
    normalize_secret_name,
)
from core.secrets._references import (
    EMPTY_MASKER,
    MIN_MASKED_LENGTH,
    REFERENCE_PATTERN,
    Reference,
    SecretMasker,
    find_references,
    has_reference,
    map_strings,
)
from core.secrets._totp import TotpSeedError, check_totp_seed, totp_code
from core.secrets.sources import SecretSource, SecretSourceError, SourceStatus
from core.secrets.store import AGENT_SECRETS, SecretStore, credential_name_key
from core.utils.ids import new_id
from core.utils.logging import get_logger

__all__ = [
    "LIFETIMES",
    "RecordedUse",
    "DEFAULT_PASSWORD_SYMBOLS",
    "REFERENCE_PATTERN",
    "SECRET_KINDS",
    "SINK_CLASSES",
    "AgentSecret",
    "AgentSecrets",
    "EMPTY_MASKER",
    "ChangeListener",
    "Lifetime",
    "SecretChange",
    "SecretField",
    "SecretKind",
    "SecretMasker",
    "SecretRecordError",
    "SecretUse",
    "SecretUseError",
    "SinkClass",
    "SourceBinding",
    "describe_uses",
    "find_references",
    "has_reference",
    "normalize_secret_name",
]

_LOGGER = get_logger("secrets")

SecretUseFailure = Literal[
    "unknown_secret",
    "not_granted",
    "unknown_field",
    "sink_not_allowed",
    "source_failed",
]
ChangeKind = Literal["created", "changed", "deleted"]

#: Symbols a generated password contains unless the caller names others.
DEFAULT_PASSWORD_SYMBOLS: Final = "!#$%&()*+,-./:;<=>?@[]^_{|}~"
_MIN_PASSWORD = 8
_RECENT_USES = 20
_MAX_PASSWORD = 128


class SecretUseError(Exception):
    """A reference cannot be used here; the message tells the Agent what to do."""

    def __init__(self, code: SecretUseFailure, message: str) -> None:
        super().__init__(message)
        self.code: SecretUseFailure = code


@dataclass(frozen=True)
class SecretUse:
    """One substitution: which Secret field went into which sink class."""

    name: str
    field: str
    sink: SinkClass

    @property
    def label(self) -> str:
        return f"{self.name}.{self.field}"


def describe_uses(uses: Iterable[SecretUse]) -> list[str]:
    """How a call's display names the Secrets it used (``GITHUB.password (terminal)``)."""
    return list(dict.fromkeys(f"{use.label} ({use.sink})" for use in uses))


@dataclass(frozen=True)
class RecordedUse:
    """One substitution as Settings shows it: never a value."""

    field: str
    sink: SinkClass
    agent_id: str
    session_id: str
    at: datetime


@dataclass(frozen=True)
class SecretChange:
    kind: ChangeKind
    secret: AgentSecret
    actor: str


ChangeListener = Callable[[SecretChange], None]


class AgentSecrets:
    """The Agent Secrets of one data directory; one instance per process.

    Reads are served from an in-memory copy, so the hot paths (masking,
    listing) do no I/O. Every write goes through the Secret Store and its lock;
    callers run writes and source calls off the Event Loop.
    """

    def __init__(
        self,
        store: SecretStore,
        *,
        clock: Callable[[], datetime],
        credential_names: Callable[[], Iterable[str]] = tuple,
    ) -> None:
        self._store = store
        self._clock = clock
        self._credential_names = credential_names
        self._lock = RLock()
        self._records: dict[str, AgentSecret] = {}
        self._sources: dict[str, SecretSource] = {}
        # Values known only in memory: system credentials and values a source
        # delivered in this process, each with the reference that masks it.
        self._runtime_values: dict[str, str] = {}
        self._system_values: dict[str, str] = {}
        self._masker: SecretMasker | None = None
        self._listeners: list[ChangeListener] = []
        self._loaded = False
        # The latest uses of each Secret since this process started, newest last.
        self._uses: dict[str, deque[RecordedUse]] = {}

    # -- loading and listeners -------------------------------------------------

    def reload(self) -> None:
        """Read every record from the store (blocking); damaged records are skipped."""
        entries = self._store.entries(AGENT_SECRETS)
        records: dict[str, AgentSecret] = {}
        for secret_id, text in entries.items():
            try:
                records[secret_id] = decode_record(secret_id, text)
            except SecretRecordError as error:
                _LOGGER.warning("Agent Secret %s is unreadable and ignored: %s", secret_id, error)
        with self._lock:
            self._records = records
            self._masker = None
            self._loaded = True
        self.sweep()

    def now(self) -> datetime:
        """The time lifetimes are measured against."""
        return self._clock()

    def add_change_listener(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def _notify(self, change: SecretChange) -> None:
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:
                _LOGGER.exception("Agent Secret change listener failed")

    # -- sources ---------------------------------------------------------------

    def replace_sources(self, sources: Iterable[SecretSource]) -> None:
        """Offer exactly ``sources``; a source that goes away is locked first."""
        offered = {source.name: source for source in sources}
        with self._lock:
            removed = [
                source for name, source in self._sources.items() if offered.get(name) is not source
            ]
            self._sources = offered
        for source in removed:
            try:
                source.lock()
            except Exception:
                _LOGGER.warning("Secret Source %s did not lock", source.name, exc_info=True)

    def sources(self) -> list[SecretSource]:
        with self._lock:
            return list(self._sources.values())

    def source(self, name: str) -> SecretSource | None:
        with self._lock:
            return self._sources.get(name)

    def source_status(self, name: str) -> SourceStatus:
        source = self.source(name)
        if source is None:
            return SourceStatus("unavailable", "its Extension is not enabled")
        try:
            return source.status()
        except Exception as error:  # a source's own failure is reported, not raised
            return SourceStatus("unavailable", str(error) or type(error).__name__)

    # -- queries ---------------------------------------------------------------

    def all(self) -> list[AgentSecret]:
        """Every live Secret, in name order."""
        now = self._clock()
        with self._lock:
            return sorted(
                (item for item in self._records.values() if not item.expired(now)),
                key=lambda item: (item.name, item.id),
            )

    def get(self, secret_id: str) -> AgentSecret | None:
        with self._lock:
            secret = self._records.get(secret_id)
        return None if secret is None or secret.expired(self._clock()) else secret

    def usable(
        self, agent_id: str, session_id: str, *, passed_only: bool = False
    ) -> list[AgentSecret]:
        """The Secrets this Agent may use in this Session, one per name.

        A Secret bound to or passed into the Session wins over one granted to
        the Agent under the same name. A Sub-Agent Session (``passed_only``)
        has only the Secrets bound to or passed into it.
        """
        chosen: dict[str, AgentSecret] = {}
        for secret in self.all():
            if not secret.granted_to(agent_id, session_id, passed_only=passed_only):
                continue
            current = chosen.get(secret.name)
            if current is None or _session_bound(secret, session_id) > _session_bound(
                current, session_id
            ):
                chosen[secret.name] = secret
        return [chosen[name] for name in sorted(chosen)]

    def lookup(
        self, agent_id: str, session_id: str, name: str, *, passed_only: bool = False
    ) -> AgentSecret | None:
        usable = self.usable(agent_id, session_id, passed_only=passed_only)
        return next((item for item in usable if item.name == name), None)

    def describe(self, secret: AgentSecret) -> dict[str, Any]:
        """What an Agent or the WebUI may know about a Secret: never a concealed value."""
        described: dict[str, Any] = {
            "id": secret.id,
            "name": secret.name,
            "kind": secret.kind,
            "reference": secret.reference(),
            "fields": [
                {
                    "name": item.name,
                    "concealed": item.concealed,
                    "reference": secret.reference(item.name),
                    **({} if item.concealed or item.value is None else {"value": item.value}),
                }
                for item in secret.fields
            ],
            "environment": secret.environment_names(),
            "lifetime": secret.lifetime,
            "sinks": sorted(secret.sinks),
            "agents": list(secret.agents),
            "created_at": secret.created_at.isoformat(),
            "created_by": secret.created_by,
        }
        if secret.description:
            described["description"] = secret.description
        if secret.session_id is not None:
            described["session_id"] = secret.session_id
        if secret.expires_at is not None:
            described["expires_at"] = secret.expires_at.isoformat()
        if secret.source is not None:
            described["source"] = {
                "source": secret.source.source,
                "reference": secret.source.reference,
                "state": self.source_status(secret.source.source).state,
            }
        return described

    # -- writes ----------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        kind: SecretKind,
        values: Mapping[str, str],
        actor: str,
        lifetime: Lifetime = "permanent",
        session_id: str | None = None,
        expires_at: datetime | None = None,
        agents: Sequence[str] = (),
        description: str = "",
        concealed: Mapping[str, bool] | None = None,
        sinks: Iterable[str] | None = None,
        source: SourceBinding | None = None,
        source_fields: Mapping[str, str] | None = None,
    ) -> AgentSecret:
        """Store a new Secret (blocking); :class:`SecretRecordError` when invalid."""
        secret_name = normalize_secret_name(name)
        fields = _build_fields(
            kind, values, concealed or {}, source_fields, bound=source is not None
        )
        now = self._clock()
        _check_lifetime(lifetime, session_id, expires_at, now)
        if lifetime == "permanent" and expires_at is not None:
            lifetime = "until"
        secret = AgentSecret(
            id="",
            name=secret_name,
            kind=kind,
            description=check_description(description),
            fields=fields,
            lifetime=lifetime,
            session_id=session_id if lifetime in ("session", "once") else None,
            expires_at=expires_at,
            agents=tuple(dict.fromkeys(agents)) if lifetime in ("permanent", "until") else (),
            # The Session it was given in keeps it, a Sub-Agent Session too.
            sessions=(session_id,) if session_id and lifetime in ("permanent", "until") else (),
            sinks=DEFAULT_SINKS[kind] if sinks is None else check_sinks(sinks),
            source=source,
            created_at=now,
            created_by=actor,
        )
        with self._lock:
            self._check_name_free(secret)
            secret = replace(
                secret, id=new_id(SECRET_ID_PREFIX, claim=lambda item: item not in self._records)
            )
            self._write(secret)
        _LOGGER.info(
            "Agent Secret %s created (kind=%s lifetime=%s actor=%s)",
            secret.name,
            secret.kind,
            secret.lifetime,
            actor,
        )
        self._notify(SecretChange("created", secret, actor))
        return secret

    def update(
        self,
        secret_id: str,
        *,
        actor: str,
        values: Mapping[str, str] | None = None,
        description: str | None = None,
        concealed: Mapping[str, bool] | None = None,
        agents: Sequence[str] | None = None,
        sinks: Iterable[str] | None = None,
        expires_at: datetime | None = None,
        clear_expiry: bool = False,
        permanent: bool = False,
    ) -> AgentSecret:
        """Change a Secret (blocking); only the given parts change."""
        with self._lock:
            current = self._require(secret_id)
            secret = current
            if values:
                if secret.source is not None:
                    raise SecretRecordError(
                        f"{secret.name} takes its values from {secret.source.source}; "
                        "change them there."
                    )
                secret = replace(secret, fields=_with_values(secret, values))
            if concealed:
                secret = replace(
                    secret,
                    fields=tuple(
                        replace(item, concealed=bool(concealed.get(item.name, item.concealed)))
                        for item in secret.fields
                    ),
                )
            if description is not None:
                secret = replace(secret, description=check_description(description))
            if sinks is not None:
                secret = replace(secret, sinks=check_sinks(sinks))
            if permanent and secret.lifetime != "permanent":
                secret = replace(secret, lifetime="permanent", expires_at=None, session_id=None)
            if clear_expiry and secret.lifetime == "until":
                secret = replace(secret, lifetime="permanent", expires_at=None)
            if expires_at is not None:
                _check_lifetime("until", None, expires_at, self._clock())
                secret = replace(
                    secret,
                    expires_at=expires_at,
                    lifetime=secret.lifetime if secret.lifetime != "permanent" else "until",
                )
            if agents is not None:
                if secret.lifetime not in ("permanent", "until"):
                    raise SecretRecordError(
                        f"{secret.name} belongs to one Session; make it permanent to grant it."
                    )
                secret = replace(secret, agents=tuple(dict.fromkeys(agents)))
            if secret == current:
                return current
            if secret.lifetime != current.lifetime:
                self._check_name_free(secret)
            self._write(secret)
        _LOGGER.info("Agent Secret %s changed (actor=%s)", secret.name, actor)
        self._notify(SecretChange("changed", secret, actor))
        return secret

    def delete(self, secret_id: str, *, actor: str) -> AgentSecret:
        with self._lock:
            secret = self._require(secret_id)
            self._remove(secret)
        _LOGGER.info("Agent Secret %s deleted (actor=%s)", secret.name, actor)
        self._notify(SecretChange("deleted", secret, actor))
        return secret

    def passable(
        self, agent_id: str, session_id: str, names: Sequence[str], *, passed_only: bool = False
    ) -> list[AgentSecret]:
        """The caller's Secrets ``names`` stand for; :class:`SecretUseError` names the others."""
        granted: list[AgentSecret] = []
        missing = []
        for raw in names:
            try:
                name = normalize_secret_name(raw)
            except SecretRecordError:
                missing.append(str(raw))
                continue
            secret = self.lookup(agent_id, session_id, name, passed_only=passed_only)
            if secret is None:
                missing.append(name)
            elif secret not in granted:
                granted.append(secret)
        if missing:
            raise SecretUseError(
                "unknown_secret",
                f"You cannot pass {', '.join(missing)}: "
                f"{'it is' if len(missing) == 1 else 'they are'} not among your Secrets. "
                f"Your Secrets: {self._names_text(agent_id, session_id, passed_only)}.",
            )
        return granted

    def pass_to_session(
        self,
        agent_id: str,
        session_id: str,
        names: Sequence[str],
        child_session_id: str,
        *,
        passed_only: bool = False,
    ) -> list[AgentSecret]:
        """Grant named Secrets the caller can use to a Sub-Agent's Session (blocking)."""
        granted = self.passable(agent_id, session_id, names, passed_only=passed_only)
        changed: list[AgentSecret] = []
        with self._lock:
            for secret in granted:
                current = self._records.get(secret.id, secret)
                if child_session_id in current.sessions:
                    continue
                updated = replace(current, sessions=(*current.sessions, child_session_id))
                self._write(updated)
                changed.append(updated)
        for secret in changed:
            self._notify(SecretChange("changed", secret, f"session:{session_id}"))
        return granted

    def remove_session(self, session_id: str) -> None:
        """Forget a Session (blocking): its own Secrets go, passed grants are withdrawn."""
        with self._lock:
            affected = [
                item
                for item in self._records.values()
                if item.session_id == session_id or session_id in item.sessions
            ]
            for secret in affected:
                if secret.session_id == session_id and secret.lifetime in ("session", "once"):
                    self._remove(secret)
                else:
                    self._write(
                        replace(
                            secret,
                            sessions=tuple(item for item in secret.sessions if item != session_id),
                        )
                    )
        if affected:
            _LOGGER.info("Agent Secrets of a removed Session withdrawn (count=%d)", len(affected))

    def sweep(self) -> int:
        """Remove expired Secrets (blocking); how many were removed."""
        now = self._clock()
        with self._lock:
            expired = [item for item in self._records.values() if item.expired(now)]
            for secret in expired:
                self._remove(secret)
        for secret in expired:
            _LOGGER.info("Agent Secret %s expired", secret.name)
            self._notify(SecretChange("deleted", secret, "expiry"))
        return len(expired)

    def generate_password(self, length: int = 24, symbols: str = DEFAULT_PASSWORD_SYMBOLS) -> str:
        """A random password of letters, digits and ``symbols``, with each group at least once."""
        if not _MIN_PASSWORD <= length <= _MAX_PASSWORD:
            raise SecretRecordError(
                f"Generated passwords have {_MIN_PASSWORD} to {_MAX_PASSWORD} characters."
            )
        chosen_symbols = "".join(
            dict.fromkeys(char for char in symbols if char.isascii() and char.isprintable())
        ).replace(" ", "")
        groups = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
        if chosen_symbols:
            groups.append(chosen_symbols)
        alphabet = "".join(groups)
        while True:
            password = "".join(_random.choice(alphabet) for _ in range(length))
            if all(any(char in group for char in password) for group in groups):
                return password

    # -- use -------------------------------------------------------------------

    def resolve(
        self,
        agent_id: str,
        session_id: str,
        reference: Reference,
        sink: SinkClass,
        *,
        passed_only: bool = False,
    ) -> tuple[str, SecretUse]:
        """The value a reference stands for in this sink (blocking for source-bound Secrets)."""
        secret = self.lookup(agent_id, session_id, reference.name, passed_only=passed_only)
        if secret is None:
            raise self._unknown(agent_id, session_id, reference, passed_only)
        target = secret.field_named(reference.field) if reference.field else secret.default_field()
        if target is None:
            available = ", ".join(secret.reference(item.name) for item in secret.fields)
            raise SecretUseError(
                "unknown_field",
                f"{secret.name} has no field {reference.field or '(default)'}; use {available}.",
            )
        if sink not in secret.sinks:
            allowed = ", ".join(sorted(secret.sinks)) or "none"
            raise SecretUseError(
                "sink_not_allowed",
                f"{secret.name} may not be used in {_SINK_WORDS[sink]} (allowed: {allowed}). "
                "Tell the user if this use is needed; they can allow it in Settings -> Secrets.",
            )
        value = self.field_value(secret, target)
        self._record_use(secret, target.name, sink, agent_id, session_id)
        if secret.lifetime == "once":
            with self._lock:
                if secret.id in self._records:
                    self._remove(secret)
            self._notify(SecretChange("deleted", secret, "used once"))
        return value, SecretUse(secret.name, target.name, sink)

    def field_value(self, secret: AgentSecret, target: SecretField) -> str:
        """The value of one field: stored, from its source, or the current TOTP code."""
        if secret.source is not None:
            value = self._source_value(secret, target)
        elif target.value is None:
            raise SecretUseError("unknown_field", f"{secret.name}.{target.name} has no value.")
        else:
            value = target.value
        if target.name == TOTP_FIELD:
            try:
                return totp_code(value, self._clock())
            except TotpSeedError as error:
                raise SecretUseError(
                    "source_failed", f"{secret.name}.totp is not a usable TOTP seed: {error}."
                ) from None
        return value

    def substitute(
        self,
        agent_id: str,
        session_id: str,
        value: Any,
        sink: SinkClass,
        *,
        passed_only: bool = False,
    ) -> tuple[Any, list[SecretUse]]:
        """``value`` (JSON-like) with references replaced; raises :class:`SecretUseError`."""
        uses: list[SecretUse] = []
        # One value per reference and call, so a ``once`` Secret named twice
        # in the same call is resolved (and consumed) once.
        resolved_values: dict[tuple[str, str | None], tuple[str, SecretUse]] = {}

        def replace_text(text: str) -> str:
            references = find_references(text)
            if not references:
                return text
            parts: list[str] = []
            position = 0
            for reference in references:
                key = (reference.name, reference.field)
                if key not in resolved_values:
                    resolved_values[key] = self.resolve(
                        agent_id, session_id, reference, sink, passed_only=passed_only
                    )
                resolved, use = resolved_values[key]
                parts.append(text[position : reference.start])
                parts.append(resolved)
                position = reference.end
                if use not in uses:
                    uses.append(use)
            parts.append(text[position:])
            return "".join(parts)

        return map_strings(value, replace_text), uses

    def environment_names(
        self, agent_id: str, session_id: str, *, passed_only: bool = False
    ) -> frozenset[str]:
        """Every name ``env_keys`` accepts for this Agent: Secret names and field variables."""
        names: set[str] = set()
        for secret in self.usable(agent_id, session_id, passed_only=passed_only):
            names.add(secret.name)
            names.update(secret.environment_names().values())
        return frozenset(names)

    def environment(
        self, agent_id: str, session_id: str, name: str, *, passed_only: bool = False
    ) -> tuple[dict[str, str], list[SecretUse]] | None:
        """Shell variables for a name in ``env_keys``; ``None`` if no Secret has it.

        A Secret name gives every field (``NAME`` or ``NAME_<FIELD>``); one
        field's variable name (``GITHUB_PASSWORD``) gives only that field.
        """
        wanted = name.upper()
        for secret in self.usable(agent_id, session_id, passed_only=passed_only):
            variables = secret.environment_names()
            if secret.name == wanted:
                fields = list(secret.fields)
            else:
                fields = [item for item in secret.fields if variables[item.name] == wanted]
                if not fields:
                    continue
            if "environment" not in secret.sinks:
                raise SecretUseError(
                    "sink_not_allowed",
                    f"{secret.name} may not be used as a shell environment variable. "
                    "Tell the user if this use is needed; they can allow it in "
                    "Settings -> Secrets.",
                )
            values = {variables[item.name]: self.field_value(secret, item) for item in fields}
            uses = [SecretUse(secret.name, item.name, "environment") for item in fields]
            for item in fields:
                self._record_use(secret, item.name, "environment", agent_id, session_id)
            return values, uses
        return None

    def recent_uses(self, secret_id: str) -> list[RecordedUse]:
        """The latest uses of a Secret since this process started, newest first."""
        with self._lock:
            return list(reversed(self._uses.get(secret_id, ())))

    def _record_use(
        self, secret: AgentSecret, field_name: str, sink: SinkClass, agent_id: str, session_id: str
    ) -> None:
        use = RecordedUse(field_name, sink, agent_id, session_id, self._clock())
        with self._lock:
            self._uses.setdefault(secret.id, deque(maxlen=_RECENT_USES)).append(use)
        _LOGGER.debug(
            "Agent Secret %s.%s used (sink=%s agent=%s)", secret.name, field_name, sink, agent_id
        )

    # -- masking ---------------------------------------------------------------

    @property
    def masker(self) -> SecretMasker:
        """Masks every value vBot knows; rebuilt only after a change."""
        with self._lock:
            if self._masker is None:
                known: dict[str, str] = {}
                for name, value in self._system_values.items():
                    known.setdefault(value, f"{{{{secret:{name}}}}}")
                known.update(self._runtime_values)
                for secret in self._records.values():
                    for item in secret.fields:
                        if item.value is not None:
                            known[item.value] = secret.reference(item.name)
                self._masker = SecretMasker(known) if known else EMPTY_MASKER
            return self._masker

    def set_system_values(self, values: Mapping[str, str]) -> None:
        """Replace the system credential values masked as ``{{secret:<ENV_NAME>}}``."""
        with self._lock:
            self._system_values = {
                credential_name_key(name).upper(): value
                for name, value in values.items()
                if isinstance(value, str) and len(value) >= MIN_MASKED_LENGTH
            }
            self._masker = None

    def remember_value(self, value: str, reference: str) -> None:
        """Mask a value that reached a process from outside the store (a source, the shell)."""
        if len(value) < MIN_MASKED_LENGTH:
            return
        with self._lock:
            if self._runtime_values.get(value) == reference:
                return
            self._runtime_values[value] = reference
            self._masker = None

    # -- internals -------------------------------------------------------------

    def _source_value(self, secret: AgentSecret, target: SecretField) -> str:
        assert secret.source is not None
        source = self.source(secret.source.source)
        if source is None:
            raise SecretUseError(
                "source_failed",
                f"{secret.name} comes from {secret.source.source}, which is not available. "
                "Tell the user to enable it in Settings -> Secrets.",
            )
        try:
            value = source.resolve(secret.source.reference, target.source_field or target.name)
        except SecretSourceError as error:
            hint = (
                "Tell the user to unlock it in Settings -> Secrets."
                if error.code == "locked"
                else "Tell the user what failed."
            )
            raise SecretUseError(
                "source_failed",
                f"{source.label} did not deliver {secret.name}.{target.name}: {error} {hint}",
            ) from None
        self.remember_value(value, secret.reference(target.name))
        return value

    def _unknown(
        self, agent_id: str, session_id: str, reference: Reference, passed_only: bool
    ) -> SecretUseError:
        exists = any(item.name == reference.name for item in self.all())
        names = self._names_text(agent_id, session_id, passed_only)
        if exists and passed_only:
            return SecretUseError(
                "not_granted",
                f"{reference.name} was not passed to you. Your Secrets: {names}. "
                "If you need it, say so in your report; your Parent Agent can pass it.",
            )
        if exists:
            return SecretUseError(
                "not_granted",
                f"{reference.name} is not granted to you. Your Secrets: {names}. "
                "Ask the user to grant it in Settings -> Secrets if you need it.",
            )
        system = {credential_name_key(item).upper() for item in self._credential_names()}
        if reference.name in system:
            return SecretUseError(
                "not_granted",
                f"{reference.name} is a vBot credential, not a Secret you can use here. "
                f"Your Secrets: {names}.",
            )
        return SecretUseError(
            "unknown_secret",
            f"No Secret named {reference.name}. Your Secrets: {names}. "
            "Use the secrets Tool to request one from the user.",
        )

    def _names_text(self, agent_id: str, session_id: str, passed_only: bool) -> str:
        usable = self.usable(agent_id, session_id, passed_only=passed_only)
        names = [item.name for item in usable]
        return ", ".join(names) if names else "none"

    def _require(self, secret_id: str) -> AgentSecret:
        secret = self._records.get(secret_id)
        if secret is None:
            raise SecretRecordError(f"No Secret with id {secret_id}.")
        return secret

    def _check_name_free(self, secret: AgentSecret) -> None:
        system = {credential_name_key(item).upper() for item in self._credential_names()}
        if secret.name in system:
            raise SecretRecordError(
                f"{secret.name} is already a vBot credential name; choose another name."
            )
        now = self._clock()
        for other in self._records.values():
            if other.id == secret.id or other.name != secret.name or other.expired(now):
                continue
            same_session = secret.session_id is not None and other.session_id == secret.session_id
            both_agent_wide = secret.session_id is None and other.session_id is None
            if same_session or both_agent_wide:
                raise SecretRecordError(f"A Secret named {secret.name} already exists.")

    def _write(self, secret: AgentSecret) -> None:
        text = encode_record(secret)
        self._store.update(lambda change: change.set(AGENT_SECRETS, secret.id, text))
        self._records[secret.id] = secret
        self._masker = None

    def _remove(self, secret: AgentSecret) -> None:
        self._store.update(lambda change: change.remove(AGENT_SECRETS, secret.id))
        self._records.pop(secret.id, None)
        self._masker = None


_SINK_WORDS: Final[Mapping[SinkClass, str]] = {
    "environment": "shell environment variables",
    "terminal": "terminal input",
    "files": "file contents",
    "typing": "typed text",
    "external_tools": "arguments sent to external Tools (MCP, Extensions)",
}


def _session_bound(secret: AgentSecret, session_id: str) -> int:
    return 1 if secret.session_id == session_id or session_id in secret.sessions else 0


def _check_lifetime(
    lifetime: Lifetime, session_id: str | None, expires_at: datetime | None, now: datetime
) -> None:
    if lifetime in ("session", "once") and not session_id:
        raise SecretRecordError(f"A {lifetime} Secret needs its Session.")
    if lifetime == "until" and expires_at is None:
        raise SecretRecordError("A time-limited Secret needs an expiry time.")
    if expires_at is not None and expires_at <= now:
        raise SecretRecordError("The expiry time has already passed.")


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
        value = check_value(given[name], field_name=name)
        if name == TOTP_FIELD:
            try:
                check_totp_seed(value)
            except TotpSeedError as error:
                raise SecretRecordError(f"The totp field is not a TOTP seed: {error}.") from None
        hidden = bool(concealed.get(name, defaults.get(name, True)))
        fields.append(SecretField(name=name, concealed=hidden, value=value))
    return tuple(fields)


def _with_values(secret: AgentSecret, values: Mapping[str, str]) -> tuple[SecretField, ...]:
    updates = {normalize_field_name(key): value for key, value in values.items()}
    fields = []
    for item in secret.fields:
        if item.name in updates:
            value = check_value(updates.pop(item.name), field_name=item.name)
            if item.name == TOTP_FIELD:
                check_totp_seed(value)
            fields.append(replace(item, value=value))
        else:
            fields.append(item)
    for name, value in updates.items():
        fields.append(
            SecretField(name=name, concealed=True, value=check_value(value, field_name=name))
        )
    return tuple(fields)
