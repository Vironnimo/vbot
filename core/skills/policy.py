"""Validated Skill Policy — the central disable/share control plane for Skills.

The Skills domain owns ``<data_dir>/skills/policy.json``: a versioned JSON
document that disables Skills by name across every origin and marks an Identity
Agent's private Skills as shared with specific other Identity Agents. A missing
file means an empty policy. A malformed file yields diagnostics plus an empty
effective policy instead of breaking startup; the manager surfaces the
diagnostics, and mutations refuse to overwrite it. Another ``format_version`` is
invalid; data from before persistence Generation 1 is converted, not migrated.
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
    disabled = data.get("disabled", [])
    if disabled is not None:
        validate_string_list(diagnostics, "$.disabled", disabled)
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
                validate_string_list(
                    diagnostics,
                    child_path(owner_path, str(skill_name)),
                    receivers,
                )
    warn_unknown_keys(diagnostics, "$", data, POLICY_SHAPE.fields, "key")
    return diagnostics


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
            policy, _ = self._read_policy(strict=True)
            names = set(policy.disabled)
            if disabled:
                names.add(name)
            else:
                names.discard(name)
            return self._write_policy(
                SkillPolicy(disabled=frozenset(names), shared=policy.shared),
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
            policy, _ = self._read_policy(strict=True)
            per_owner: dict[str, dict[str, frozenset[str]]] = {
                owner: dict(skills) for owner, skills in policy.shared.items()
            }
            owner_skills = per_owner.get(owner_id, {})
            if shared:
                owner_skills[name] = frozenset(receivers or [])
                per_owner[owner_id] = owner_skills
            else:
                owner_skills.pop(name, None)
                if owner_skills:
                    per_owner[owner_id] = owner_skills
                else:
                    per_owner.pop(owner_id, None)
            return self._write_policy(
                SkillPolicy(
                    disabled=policy.disabled,
                    shared={owner: dict(skills) for owner, skills in sorted(per_owner.items())},
                ),
                operation="share" if shared else "unshare",
                target=f"{owner_id}/{name}",
            )

    @staticmethod
    def _validate_skill_name(name: str) -> None:
        if not isinstance(name, str) or not SKILL_NAME_TRIGGER_PATTERN.fullmatch(name):
            raise SkillPolicyError("Skill policy requires a trigger-safe Skill name")

    def _read_policy(self, *, strict: bool = False) -> tuple[SkillPolicy, list[str]]:
        path = self.policy_path
        if not path.is_file():
            return SkillPolicy(), []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            message = f"Cannot read skill policy {path}: {error}"
            if strict:
                raise SkillPolicyError(message) from error
            _LOGGER.warning(message)
            return SkillPolicy(), [message]
        diagnostics = _validate_policy_document(data)
        if any(diagnostic.severity == "error" for diagnostic in diagnostics):
            messages = [
                f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
                for diagnostic in diagnostics
            ]
            if strict:
                raise SkillPolicyError(
                    f"Cannot update invalid skill policy {path}: {'; '.join(messages)}"
                )
            _LOGGER.warning("Ignoring invalid skill policy %s: %s", path, "; ".join(messages))
            return SkillPolicy(), messages
        policy = self._build_effective_policy(data, diagnostics)
        return policy, [
            f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
            for diagnostic in diagnostics
        ]

    @staticmethod
    def _build_effective_policy(
        data: Mapping[str, Any], diagnostics: list[JsonDiagnostic]
    ) -> SkillPolicy:
        """Project a valid document into its effective policy, dropping bad names."""
        from core.settings import is_valid_agent_id

        def usable_name(name: Any, path: str) -> bool:
            if isinstance(name, str) and SKILL_NAME_TRIGGER_PATTERN.fullmatch(name):
                return True
            diagnostics.append(
                JsonDiagnostic(
                    severity="warning",
                    path=path,
                    message=f"ignoring unusable skill name: {name!r}",
                )
            )
            return False

        disabled = frozenset(
            name
            for index, name in enumerate(data.get("disabled") or [])
            if usable_name(name, f"$.disabled[{index}]")
        )
        shared: dict[str, dict[str, frozenset[str]]] = {}
        raw_shared = data.get("shared") or {}
        for owner_id, raw_skills in sorted(raw_shared.items()):
            path = child_path("$.shared", str(owner_id))
            skills: dict[str, frozenset[str]] = {}
            for index, (skill_name, raw_receivers) in enumerate(sorted((raw_skills or {}).items())):
                if not usable_name(skill_name, f"{path}[{index}]"):
                    continue
                receivers = frozenset(
                    receiver for receiver in raw_receivers or [] if is_valid_agent_id(receiver)
                )
                if receivers:
                    skills[str(skill_name)] = receivers
            if skills:
                shared[str(owner_id)] = skills
        return SkillPolicy(disabled=disabled, shared=shared)

    def _write_policy(self, policy: SkillPolicy, *, operation: str, target: str) -> SkillPolicy:
        document = {
            "disabled": sorted(policy.disabled),
            "shared": {
                owner_id: {
                    skill_name: sorted(receivers)
                    for skill_name, receivers in sorted(skills.items())
                }
                for owner_id, skills in sorted(policy.shared.items())
            },
        }
        try:
            write_json_document(
                self.policy_path, document, POLICY_FORMAT, data_dir=self._storage.data_dir
            )
        except JsonDocumentWriteError as error:
            raise SkillPolicyError(str(error)) from error
        except OSError as error:
            raise SkillPolicyError(f"Cannot write skill policy: {error}") from error
        _LOGGER.info(
            "Skill policy %s applied for %s (%d disabled, %d shared owners)",
            operation,
            target,
            len(policy.disabled),
            len(policy.shared),
        )
        return policy
