"""Validated Skill Policy — the central disable/share control plane for Skills.

The Skills domain owns ``<data_dir>/skills/policy.json``: a versioned JSON
document that disables Skills by name across every origin and marks an Identity
Agent's private Skills as shared with all other Identity Agents. A missing file
means an empty policy. A malformed file yields diagnostics plus an empty
effective policy instead of breaking startup; the manager surfaces the
diagnostics. There is deliberately no legacy compatibility or auto-migration —
an unsupported schema version is simply invalid.
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
    add_error,
    child_path,
    validate_required_fields,
    validate_string_list,
    warn_unknown_keys,
)
from core.skills.skill_validator import SKILL_NAME_TRIGGER_PATTERN
from core.utils.atomic import atomic_write_text
from core.utils.errors import VBotError
from core.utils.logging import get_logger

POLICY_SCHEMA_VERSION = 1
_SKILLS_DIRNAME = "skills"
_POLICY_FILENAME = "policy.json"
_POLICY_KEYS = frozenset({"version", "disabled", "shared"})

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
    # Owner Identity Agent id -> shared private Skill names. Entries are kept as
    # written (stale owners/names included); staleness is resolved where the
    # receiving registries are built, which knows the live Agent roster.
    shared: Mapping[str, frozenset[str]] = field(default_factory=dict)


def _validate_policy_document(data: Any) -> list[JsonDiagnostic]:
    """Validate one decoded policy document, transport-neutral."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        add_error(diagnostics, "$", "must be a JSON object")
        return diagnostics
    validate_required_fields(diagnostics, "$", data, frozenset({"version"}))
    version = data.get("version")
    if version != POLICY_SCHEMA_VERSION or isinstance(version, bool):
        add_error(diagnostics, "$.version", f"must be {POLICY_SCHEMA_VERSION}")
    disabled = data.get("disabled", [])
    if disabled is not None:
        validate_string_list(diagnostics, "$.disabled", disabled)
    shared = data.get("shared", {})
    if shared is not None and not isinstance(shared, dict):
        add_error(diagnostics, "$.shared", "must be an object keyed by owner agent id")
    else:
        for owner_id, names in sorted((shared or {}).items()):
            validate_string_list(diagnostics, child_path("$.shared", str(owner_id)), names)
    warn_unknown_keys(diagnostics, "$", data, _POLICY_KEYS, "key")
    return diagnostics


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
        with self._lock:
            policy = self.load()
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

    def set_shared(self, owner_id: str, name: str, *, shared: bool) -> SkillPolicy:
        """Share or unshare one of an owner's private Skills with all other Agents."""
        with self._lock:
            policy = self.load()
            per_owner: dict[str, frozenset[str]] = {
                owner: frozenset(names) for owner, names in policy.shared.items()
            }
            names = set(per_owner.get(owner_id, frozenset()))
            if shared:
                names.add(name)
            else:
                names.discard(name)
            if names:
                per_owner[owner_id] = frozenset(names)
            else:
                per_owner.pop(owner_id, None)
            return self._write_policy(
                SkillPolicy(
                    disabled=policy.disabled,
                    shared={owner: per_owner[owner] for owner in sorted(per_owner)},
                ),
                operation="share" if shared else "unshare",
                target=f"{owner_id}/{name}",
            )

    def _read_policy(self) -> tuple[SkillPolicy, list[str]]:
        path = self.policy_path
        if not path.is_file():
            return SkillPolicy(), []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            message = f"Cannot read skill policy {path}: {error}"
            _LOGGER.warning(message)
            return SkillPolicy(), [message]
        diagnostics = _validate_policy_document(data)
        if any(diagnostic.severity == "error" for diagnostic in diagnostics):
            messages = [
                f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
                for diagnostic in diagnostics
            ]
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

        def usable_name(name: Any, path: str) -> bool:
            if isinstance(name, str) and SKILL_NAME_TRIGGER_PATTERN.match(name):
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
        shared: dict[str, frozenset[str]] = {}
        raw_shared = data.get("shared") or {}
        for owner_id, raw_names in sorted(raw_shared.items()):
            path = child_path("$.shared", str(owner_id))
            names = frozenset(
                name
                for index, name in enumerate(raw_names or [])
                if usable_name(name, f"{path}[{index}]")
            )
            if names:
                shared[str(owner_id)] = names
        return SkillPolicy(disabled=disabled, shared=shared)

    def _write_policy(self, policy: SkillPolicy, *, operation: str, target: str) -> SkillPolicy:
        document = {
            "version": POLICY_SCHEMA_VERSION,
            "disabled": sorted(policy.disabled),
            "shared": {
                owner_id: sorted(names) for owner_id, names in sorted(policy.shared.items())
            },
        }
        try:
            atomic_write_text(
                self.policy_path,
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                data_dir=self._storage.data_dir,
            )
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
