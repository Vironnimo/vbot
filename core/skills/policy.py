"""Validated Skill Policy — the central disable/share control plane for Skills.

The Skills domain owns ``<data_dir>/skills/policy.json``: a versioned JSON
document that turns off individual Skill packages (one source and name each, so
a same-named package elsewhere stays on) and marks an Identity Agent's private
Skills as shared with specific other Identity Agents. A missing
file means an empty policy. A malformed file yields diagnostics plus an empty
effective policy instead of breaking startup; the manager surfaces the
diagnostics, and mutations refuse to overwrite it. Another ``format_version`` is
invalid; data from before persistence Generation 1 is refused, not migrated.

Entries this vBot cannot use (a Skill name that is not trigger-safe, an Agent id
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
    json_object,
    validate_format_version,
    write_json_document,
)
from core.skills.skill_validator import SKILL_NAME_TRIGGER_PATTERN
from core.utils.errors import VBotError
from core.utils.log_conditions import LoggedConditions
from core.utils.logging import get_logger

POLICY_FORMAT_VERSION = 1
_SKILLS_DIRNAME = "skills"
_POLICY_FILENAME = "policy.json"
# Where a turned-off package is loaded from. ``home`` is the user's global Skill
# home and ``bundled`` the Skills shipped with vBot; ``extension``, ``folder`` and
# ``agent`` packages also name their Extension, configured skill folder or owning
# Identity Agent. Project Skills are turned off in their Project instead.
SKILL_PACKAGE_SOURCES = ("home", "folder", "extension", "bundled", "agent")
# ``disabled_packages`` groups names by source; the three sources with several
# roots are maps keyed by the Extension name, the folder or the Agent id.
_LISTED_SOURCES = {"home": "home", "bundled": "bundled"}
_MAPPED_SOURCES = {"extensions": "extension", "folders": "folder", "agents": "agent"}
_DISABLED_FIELDS = frozenset({*_LISTED_SOURCES, *_MAPPED_SOURCES})
# ``shared`` is keyed by data (owner ids, Skill names), as are the maps of
# ``disabled_packages``; only the root and ``disabled_packages`` have fields.
POLICY_SHAPE = json_document(
    {"disabled_packages", "shared"},
    {"disabled_packages": json_object(_DISABLED_FIELDS)},
)

_LOGGER = get_logger("skills")


class _PolicyStorage(Protocol):
    """The one Storage surface the policy service needs (avoids an import cycle)."""

    @property
    def data_dir(self) -> Path: ...


class SkillPolicyError(VBotError):
    """Raised when the Skill Policy cannot be persisted."""


@dataclass(frozen=True, order=True)
class SkillPackageRef:
    """One Skill package: the source it loads from, that source's root, and its name.

    ``location`` names the root where a source has several: the Extension name
    for ``extension``, the configured folder (as written in ``skill_directories``)
    for ``folder`` and the owning Identity Agent id for ``agent``. It is ``None``
    for ``home`` and ``bundled``.
    """

    source: str
    name: str
    location: str | None = None


@dataclass(frozen=True)
class SkillPolicy:
    """The validated, in-memory form of the Skill Policy document."""

    disabled_packages: frozenset[SkillPackageRef] = frozenset()
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

    disabled_packages: frozenset[SkillPackageRef] = frozenset()
    shared: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)

    @classmethod
    def from_document(cls, data: Mapping[str, Any]) -> _StoredPolicy:
        """Read the stored lists of a document that passed validation."""
        return cls(
            disabled_packages=_stored_package_refs(data.get("disabled_packages") or {}),
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
            disabled_packages=frozenset(
                ref for ref in self.disabled_packages if _is_usable_package_ref(ref)
            ),
            shared=shared,
        )

    def to_document(self) -> dict[str, Any]:
        """Return the stored lists in the document's canonical (sorted) order."""
        disabled: dict[str, Any] = {field_name: [] for field_name in _LISTED_SOURCES}
        disabled.update({field_name: {} for field_name in _MAPPED_SOURCES})
        listed = {source: field_name for field_name, source in _LISTED_SOURCES.items()}
        mapped = {source: field_name for field_name, source in _MAPPED_SOURCES.items()}
        for ref in sorted(self.disabled_packages):
            if ref.source in listed:
                disabled[listed[ref.source]].append(ref.name)
            elif ref.source in mapped:
                disabled[mapped[ref.source]].setdefault(ref.location, []).append(ref.name)
        return {
            "disabled_packages": disabled,
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

    _validate_disabled_packages(diagnostics, data.get("disabled_packages", {}))
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


def _validate_disabled_packages(diagnostics: list[JsonDiagnostic], disabled: Any) -> None:
    """Validate ``disabled_packages``: name lists per source, mapped where needed."""
    from core.settings import is_valid_agent_id

    path = "$.disabled_packages"
    if disabled is None:
        return
    if not isinstance(disabled, dict):
        add_error(diagnostics, path, "must be an object keyed by package source")
        return
    for field_name in _LISTED_SOURCES:
        _validate_name_list(diagnostics, child_path(path, field_name), disabled.get(field_name))
    for field_name, source in _MAPPED_SOURCES.items():
        field_path = child_path(path, field_name)
        roots = disabled.get(field_name)
        if roots is None:
            continue
        if not isinstance(roots, dict):
            add_error(diagnostics, field_path, f"must be an object keyed by {source}")
            continue
        for location, names in sorted(roots.items()):
            location_path = child_path(field_path, str(location))
            if source == "agent" and not is_valid_agent_id(location):
                diagnostics.append(
                    JsonDiagnostic(
                        severity="warning",
                        path=location_path,
                        message=f"ignoring invalid agent id: {location!r}",
                    )
                )
            _validate_name_list(diagnostics, location_path, names)
    warn_unknown_keys(diagnostics, path, disabled, _DISABLED_FIELDS, "package source")


def _validate_name_list(diagnostics: list[JsonDiagnostic], path: str, names: Any) -> None:
    if names is None:
        return
    validate_string_list(diagnostics, path, names)
    if isinstance(names, list):
        for index, name in enumerate(names):
            _warn_unusable_skill_name(diagnostics, f"{path}[{index}]", name)


def _stored_package_refs(disabled: Mapping[str, Any]) -> frozenset[SkillPackageRef]:
    """Read the stored refs of a validated ``disabled_packages`` object."""
    refs: set[SkillPackageRef] = set()
    for field_name, source in _LISTED_SOURCES.items():
        refs.update(SkillPackageRef(source, str(name)) for name in disabled.get(field_name) or ())
    for field_name, source in _MAPPED_SOURCES.items():
        for location, names in (disabled.get(field_name) or {}).items():
            refs.update(SkillPackageRef(source, str(name), str(location)) for name in names or ())
    return frozenset(refs)


def _is_usable_package_ref(ref: SkillPackageRef) -> bool:
    from core.settings import is_valid_agent_id

    if not _is_usable_skill_name(ref.name):
        return False
    return ref.source != "agent" or is_valid_agent_id(ref.location)


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


def _file_state(path: Path) -> tuple[int, int] | None:
    """The modification time and size that tell one version of a file from the next."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


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
        # Registries re-read the policy on every rebuild: an unusable file is
        # logged once per file state, and again when it becomes valid.
        self._conditions = LoggedConditions(limit=4)

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

    def set_package_disabled(self, ref: SkillPackageRef, *, disabled: bool) -> SkillPolicy:
        """Turn one Skill package off (``disabled``) or on again."""
        from core.settings import is_valid_agent_id

        self._validate_skill_name(ref.name)
        if ref.source not in SKILL_PACKAGE_SOURCES:
            raise SkillPolicyError(f"Unknown Skill package source: {ref.source!r}")
        if (ref.source in ("home", "bundled")) != (ref.location is None):
            raise SkillPolicyError(f"Skill package source {ref.source!r} has the wrong location")
        if ref.source == "agent" and not is_valid_agent_id(ref.location):
            raise SkillPolicyError("A private Skill package must name a valid Identity Agent id")
        with self._lock:
            stored = self._read_stored()
            refs = stored.disabled_packages - {ref}
            if disabled:
                refs = refs | {ref}
            return self._write_policy(
                _StoredPolicy(disabled_packages=refs, shared=stored.shared),
                operation="disable" if disabled else "enable",
                target=f"{ref.source}:{ref.location or ''}/{ref.name}",
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
                _StoredPolicy(disabled_packages=stored.disabled_packages, shared=per_owner),
                operation="share" if shared else "unshare",
                target=f"{owner_id}/{name}",
            )

    def move_shared(self, owner_id: str, name: str, target: str) -> SkillPolicy | None:
        """Move the shares of an owner's Skill ``name`` to its Skill ``target``.

        For a Skill merged into another one: ``target`` is then shared with every
        receiver of either Skill, and ``name`` with none. Returns the new policy,
        or ``None`` when ``name`` was not shared and nothing was written.
        """
        self._validate_skill_name(name)
        self._validate_skill_name(target)
        if name == target:
            raise SkillPolicyError("A Skill's shares cannot move to the Skill itself")
        with self._lock:
            stored = self._read_stored()
            owner_skills = dict(stored.shared.get(owner_id, {}))
            moved = owner_skills.pop(name, None)
            if moved is None:
                return None
            owner_skills[target] = tuple(sorted({*owner_skills.get(target, ()), *moved}))
            per_owner = {owner: dict(skills) for owner, skills in stored.shared.items()}
            per_owner[owner_id] = owner_skills
            return self._write_policy(
                _StoredPolicy(disabled_packages=stored.disabled_packages, shared=per_owner),
                operation="move_shares",
                target=f"{owner_id}/{name}->{target}",
            )

    @staticmethod
    def _validate_skill_name(name: str) -> None:
        if not isinstance(name, str) or not SKILL_NAME_TRIGGER_PATTERN.fullmatch(name):
            raise SkillPolicyError("Skill policy requires a trigger-safe Skill name")

    def _read_policy(self) -> tuple[SkillPolicy, list[str]]:
        """Return the effective policy and the diagnostics of the file."""
        path = self.policy_path
        if not path.is_file():
            self._log_policy_usable(path)
            return SkillPolicy(), []
        state = _file_state(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            if self._conditions.started(path, state):
                _LOGGER.warning("Ignored unreadable skill policy (path=%s): %s", path, error)
            return SkillPolicy(), [f"Cannot read skill policy {path}: {error}"]
        diagnostics = _validate_policy_document(data)
        messages = [
            f"{diagnostic.severity} {diagnostic.path}: {diagnostic.message}"
            for diagnostic in diagnostics
        ]
        if any(diagnostic.severity == "error" for diagnostic in diagnostics):
            if self._conditions.started(path, state):
                _LOGGER.warning(
                    "Ignored invalid skill policy (path=%s): %s", path, "; ".join(messages)
                )
            return SkillPolicy(), messages
        self._log_policy_usable(path)
        return _StoredPolicy.from_document(data).effective(), messages

    def _log_policy_usable(self, path: Path) -> None:
        if self._conditions.ended(path):
            _LOGGER.info("Skill policy became usable again (path=%s)", path)

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
            "Applied skill policy change (operation=%s target=%s disabled=%d shared_owners=%d)",
            operation,
            target,
            len(policy.disabled_packages),
            len(policy.shared),
        )
        return policy
