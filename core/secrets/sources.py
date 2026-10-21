"""Secret Sources: external places Agent Secret values come from.

vBot's own Secret Store holds the values of most Secrets. A Secret Source (a
password manager such as KeePassXC or 1Password, contributed by an Extension)
supplies the values of Secrets bound to its entries, at the moment of use. A
source never decides who may use a value: grants, lifetimes, sinks and masking
stay with :class:`core.secrets.agent_secrets.AgentSecrets`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, runtime_checkable

from core.secrets._agent_secret_model import SecretKind

SourceState = Literal["available", "locked", "unavailable"]
SourceFailure = Literal[
    "locked", "denied", "not_found", "optional_field_missing", "unavailable", "timed_out"
]

#: The optional capabilities a source can declare.
SOURCE_CAPABILITY_STORE: Final = "store"


class SecretSourceError(Exception):
    """A source could not deliver what was asked; ``code`` says why."""

    def __init__(self, code: SourceFailure, message: str) -> None:
        super().__init__(message)
        self.code: SourceFailure = code


@dataclass(frozen=True)
class SourceStatus:
    state: SourceState
    reason: str | None = None


@dataclass(frozen=True)
class SourceField:
    """One field of a source entry; ``value`` only for fields that are not concealed."""

    name: str
    concealed: bool
    value: str | None = field(default=None, repr=False)
    # A catalog can offer an optional field without proving this entry has it.
    optional: bool = False


@dataclass(frozen=True)
class SourceEntry:
    """One entry of a source as vBot may describe it; never a concealed value."""

    reference: str
    title: str
    kind: SecretKind
    fields: tuple[SourceField, ...]
    container: str | None = None


@dataclass(frozen=True)
class UnlockField:
    """One input the user gives to unlock a source (shown masked when ``concealed``)."""

    name: str
    label: str
    concealed: bool = True
    required: bool = True


@dataclass(frozen=True)
class NewSourceEntry:
    """An entry vBot asks a source to create (a generated password, a saved login)."""

    title: str
    kind: SecretKind
    values: Mapping[str, str] = field(repr=False)
    container: str | None = None


@runtime_checkable
class SecretSource(Protocol):
    """The interface an Extension implements to contribute a Secret Source.

    Every method may be called from a worker thread; none runs on the Event
    Loop. Values are fetched per call and must not be cached on disk.
    """

    @property
    def name(self) -> str:
        """Stable id (``keepassxc``); Secrets store it in their binding."""
        ...

    @property
    def label(self) -> str:
        """Human-readable label for Settings."""
        ...

    @property
    def unlock_fields(self) -> Sequence[UnlockField]:
        """Inputs :meth:`unlock` takes; empty when the source needs no unlock."""
        ...

    @property
    def capabilities(self) -> frozenset[str]:
        """Optional capabilities, such as :data:`SOURCE_CAPABILITY_STORE`."""
        ...

    def status(self) -> SourceStatus: ...

    def containers(self) -> Sequence[str]:
        """Groups, vaults or folders the user can bind as a whole."""
        ...

    def entries(self, container: str | None = None) -> Sequence[SourceEntry]: ...

    def resolve(self, reference: str, field_name: str) -> str:
        """One field's value; :class:`SecretSourceError` when it cannot be delivered."""
        ...

    def unlock(self, inputs: Mapping[str, str]) -> SourceStatus: ...

    def lock(self) -> SourceStatus: ...

    def store(self, entry: NewSourceEntry) -> str:
        """Create an entry and return its reference (only with the ``store`` capability)."""
        ...
