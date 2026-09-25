"""Validated Skill Policy — the central disable/share control plane for Skills.

The Skills domain owns ``<data_dir>/skills/policy.json``: a versioned JSON
document that disables Skills by name across every origin and marks an Identity
Agent's private Skills as shared with specific other Identity Agents. A missing
file means an empty policy. A malformed file yields diagnostics plus an empty
effective policy instead of breaking startup; the manager surfaces the
diagnostics, and mutations refuse to overwrite it. Another ``format_version`` is
invalid; data from before persistence Generation 1 is converted, not migrated.

Entries this vBot cannot use (a Skill name that is not trigger-safe, a receiver
that is not an Identity Agent id) are warnings: the effective policy leaves them
out, and mutations write every stored entry back except the one they change.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    child_path,
    validate_json_file,
    validate_string_list,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    JsonDocumentWriteError,
    json_document,
    validate_format_version,
    write_json_document,
)
from core.skills.skill_validator import SKILL_NAME_TRIGGER_PATTERN
from core.utils.errors import VBotError
from core.utils.logging import get_logger

POLICY_FORMAT_VERSION = 1
_SKILLS_DIRNAME = "skills"
_POLICY_FILENAME = "policy.json"
# ``shared`` is keyed by data (owner ids, Skill names), so only the root has fields.
POLICY_SHAPE = json_document({"disabled", "shared"})

_LOGGER = get_logger("skills")


class _PolicyStorage(Protocol):
    """The one Storage surface the policy service needs (avoids an import cycle)."""

    @property
    def data_dir(self) -> Path: ...


class SkillPolicyError(VBotError):
    """Raised when the Skill Policy cannot be persisted."""


@dataclass(frozen=True)
class SkillPolicy:
    """The validated, in-memory form of the Skill Policy document."""

    disabled: frozenset[str] = frozenset()
    # Owner Identity Agent id -> {shared Skill name -> receiver Identity Agent
    # ids}. Entries are kept as written (stale owners/names included); staleness
    # is resolved where the receiving registries are built, which knows the live
    # Agent roster.
    shared: Mapping[str, Mapping[str, frozenset[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class _StoredPolicy:
    """The modeled fields of the policy document exactly as stored.

    Mutations change one entry here and write the rest back unchanged, including
    entries the effective :class:`SkillPolicy` leaves out.
    """

    disabled: tuple[str, ...] = ()
    shared: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)

    @classmethod
    def from_document(cls, data: Mapping[str, Any]) -> _StoredPolicy:
        """Read the stored lists of a document that passed validation."""
        return cls(
            disabled=tuple(data.get("disabled") or ()),
            shared={
                str(owner_id): {
                    str(skill_name): tuple(receivers or ())
                    for skill_name, receivers in (skills or {}).items()
                }
                for owner_id, skills in (data.get("shared") or {}).items()
            },
        )

    def effective(self) -> SkillPolicy:
        """Return the policy this vBot applies: usable names and receivers only."""
        from core.settings import is_valid_agent_id

        shared: dict[str, dict[str, frozenset[str]]] = {}
        for owner_id, stored_skills in sorted(self.shared.items()):
            skills: dict[str, frozenset[str]] = {}
            for skill_name, stored_receivers in sorted(stored_skills.items()):
                receivers = frozenset(
                    receiver for receiver in stored_receivers if is_valid_agent_id(receiver)
                )
                if _is_usable_skill_name(skill_name) and receivers:
                    skills[skill_name] = receivers
            if skills:
                shared[owner_id] = skills
        return SkillPolicy(
            disabled=frozenset(name for name in self.disabled if _is_usable_skill_name(name)),
            shared=shared,
        )

    def to_document(self) -> dict[str, Any]:
        """Return the stored lists in the document's canonical (sorted) order."""
        return {
            "disabled": sorted(self.disabled),
            "shared": {
                owner_id: {
                    skill_name: sorted(receivers) for skill_name, receivers in skills.items()
                }
                for owner_id, skills in self.shared.items()
            },
        }


def validate_skill_policy_file(policy_path: str | Path) -> JsonValidationReport:
    """Validate the optional persisted ``skills/policy.json`` without consuming it."""
    return validate_json_file(policy_path, _validate_policy_document, missing_ok=True)


def _validate_policy_document(data: Any) -> list[JsonDiagnostic]:
    """Validate one decoded policy document, transport-neutral."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        add_error(diagnostics, "$", "must be a JSON object")
        return diagnostics
    if not validate_format_version(diagnostics, data, POLICY_FORMAT_VERSION):
        return diagnostics
    from core.settings import is_valid_agent_id

    disabled = data.get("disabled", [])
    if disabled is not None:
        validate_string_list(diagnostics, "$.disabled", disabled)
        if isinstance(disabled, list):
            for index, name in enumerate(disabled):
                _warn_unusable_skill_name(diagnostics, f"$.disabled[{index}]", name)
    shared = data.get("shared", {})
    if shared is not None and not isinstance(shared, dict):
        add_error(diagnostics, "$.shared", "must be an object keyed by owner agent id")
    else:
        for owner_id, skills in sorted((shared or {}).items()):
            owner_path = child_path("$.shared", str(owner_id))
            if not isinstance(skills, dict):
                add_error(diagnostics, owner_path, "must be an object keyed by skill name")
                continue
            for skill_name, receivers in sorted(skills.items()):
                skill_path = child_path(owner_path, str(skill_name))
                _warn_unusable_skill_name(diagnostics, skill_path, skill_name)
                validate_string_list(diagnostics, skill_path, receivers)
                if not isinstance(receivers, list):
                    continue
                for index, receiver in enumerate(receivers):
                    if isinstance(receiver, str) and not is_valid_agent_id(receiver):
                        diagnostics.append(
                            JsonDiagnostic(
                                severity="warning",
                                path=f"{skill_path}[{index}]",
                                message=f"ignoring invalid receiver agent id: {receiver!r}",
                            )
                        )
    warn_unknown_keys(diagnostics, "$", data, POLICY_SHAPE.fields, "key")
    return diagnostics


def _warn_unusable_skill_name(diagnostics: list[JsonDiagnostic], path: str, name: Any) -> None:
    if isinstance(name, str) and not _is_usable_skill_name(name):
        diagnostics.append(
            JsonDiagnostic(
                severity="warning",
                path=path,
                message=f"ignoring unusable skill name: {name!r}",
            )
        )


def _is_usable_skill_name(name: Any) -> bool:
    return isinstance(name, str) and SKILL_NAME_TRIGGER_PATTERN.fullmatch(name) is not None


POLICY_FORMAT = JsonDocumentFormat(
    name="Skill policy",
    version=POLICY_FORMAT_VERSION,
    shape=POLICY_SHAPE,
    validate=_validate_policy_document,
    sort_keys=True,
)


class SkillPolicyService:
    """Load, validate, and mutate ``<data_dir>/skills/policy.json``.

    Reads-modify-write cycles are serialized through one process-local lock and
    persisted with exactly one atomic replace, matching the Settings transaction
    pattern. The scanner only considers subdirectories containing ``SKILL.md``,
    so the policy file inside ``<data_dir>/skills`` never pollutes the pool.
    """

    def __init__(self, storage: _PolicyStorage) -> None:
        self._storage = storage
        self._lock = threading.RLock()

    @property
    def policy_path(self) -> Path:
        """Return the canonical policy file location."""
        return self._storage.data_dir / _SKILLS_DIRNAME / _POLICY_FILENAME

    def load(self) -> SkillPolicy:
        """Return the current effective policy (empty when missing or invalid)."""
        policy, _ = self._read_policy()
        return policy

    def validation_diagnostics(self) -> list[str]:
        """Return human-readable diagnostics from the most recent load."""
        _, diagnostics = self._read_policy()
        return diagnostics

    def set_disabled(self, name: str, *, disabled: bool) -> SkillPolicy:
        """Add or remove one Skill name from the global disable switch."""
        self._validate_skill_name(name)
        with self._lock:
            stored = self._read_stored()
            names = tuple(entry for entry in stored.disabled if entry != name)
            if disabled:
                names = (*names, name)
            return self._write_policy(
                _StoredPolicy(disabled=names, shared=stored.shared),
                operation="disable" if disabled else "enable",
                target=name,
            )

    def set_shared(
        self,
        owner_id: str,
        name: str,
        *,
        shared: bool,
        receivers: list[str] | None = None,
    ) -> SkillPolicy:
        """Share or unshare one of an owner's private Skills with specific Agents.

        When ``shared`` is True, ``receivers`` must list at least one Identity
        Agent id; the skill becomes visible to exactly those agents. When False,
        the entry is removed entirely.
        """
        from core.settings import is_valid_agent_id

        self._validate_skill_name(name)
        if not is_valid_agent_id(owner_id):
            raise SkillPolicyError("Skill owner must be a valid Identity Agent id")
        if shared and (
            not receivers
            or any(
                not is_valid_agent_id(receiver) or receiver == owner_id for receiver in receivers
            )
        ):
            raise SkillPolicyError("Sharing requires valid receiver Agent ids other than the owner")
        with self._lock:
            stored = self._read_stored()
            per_owner: dict[str, dict[str, tuple[str, ...]]] = {
                owner: dict(skills) for owner, skills in stored.shared.items()
            }
            owner_skills = per_owner.get(owner_id, {})
            if shared:
                owner_skills[name] = tuple(sorted(set(receivers or [])))
                per_owner[owner_id] = owner_skills
            elif name in owner_skills:
                del owner_skills[name]
                if not owner_skills:
                    del per_owner[owner_id]
            return self._write_policy(
                _StoredPolicy(disabled=stored.disabled, shared=per_owner),
                operation="share" if shared else "unshare",
                target=f"{owner_id}/{name}",
            )

    @staticmethod
    def _validate_skill_name(name: str) -> None:
        if not isinstance(name, str) or not SKILL_NAME_TRIGGER_PATTERN.fullmatch(name):
            raise SkillPolicyError("Skill policy requires a trigger-safe Skill name")

    def _read_policy(self) -> tuple[SkillPolicy, list[str]]:
        """Return the effective policy and the diagnostics of the file."""
        path = self.policy_path
        if not path.is_file():
            return SkillPolicy(), []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            message = f"Cannot read skill policy {path}: {error}"
            _LOGGER.warning(message)
            return SkillPolicy(), [message]
        diagnostics = _validate_policy_document(data)
        messages = [
            f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
            for diagnostic in diagnostics
        ]
        if any(diagnostic.severity == "error" for diagnostic in diagnostics):
            _LOGGER.warning("Ignoring invalid skill policy %s: %s", path, "; ".join(messages))
            return SkillPolicy(), messages
        return _StoredPolicy.from_document(data).effective(), messages

    def _read_stored(self) -> _StoredPolicy:
        """Return the stored policy for a mutation; refuse a file that fails to load."""
        path = self.policy_path
        if not path.is_file():
            return _StoredPolicy()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SkillPolicyError(f"Cannot read skill policy {path}: {error}") from error
        errors = [
            f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
            for diagnostic in _validate_policy_document(data)
            if diagnostic.severity == "error"
        ]
        if errors:
            raise SkillPolicyError(
                f"Cannot update invalid skill policy {path}: {'; '.join(errors)}"
            )
        return _StoredPolicy.from_document(data)

    def _write_policy(self, stored: _StoredPolicy, *, operation: str, target: str) -> SkillPolicy:
        try:
            write_json_document(
                self.policy_path,
                stored.to_document(),
                POLICY_FORMAT,
                data_dir=self._storage.data_dir,
            )
        except JsonDocumentWriteError as error:
            raise SkillPolicyError(str(error)) from error
        except OSError as error:
            raise SkillPolicyError(f"Cannot write skill policy: {error}") from error
        policy = stored.effective()
        _LOGGER.info(
            "Skill policy %s applied for %s (%d disabled, %d shared owners)",
            operation,
            target,
            len(policy.disabled),
            len(policy.shared),
        )
        return policy
