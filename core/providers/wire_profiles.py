"""Resolve wire profiles for (Provider, Connection, Model id).

``WireProfiles`` owns the bundled wire profile files, Custom Provider wire
blocks and the optional observation source, and resolves one immutable
:class:`~core.providers.wire_profile.WireProfile` per target. See the module
docstring of :mod:`core.providers.wire_profile` for the layer order.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from core.providers._wire_profile_files import (
    PROFILE_SCHEMA,
    RuleMatcher,
    WireIssueReport,
    WireModelEntry,
    WireProfileFile,
    WireRule,
    child_node,
    is_opaque,
    load_wire_profile_files,
    parse_wire_profile_block,
)
from core.providers._wire_protocol_defaults import PROTOCOL_DEFAULTS
from core.providers.reasoning import normalize_thinking_effort
from core.providers.reasoning_dialects import dialect_carriers
from core.providers.wire_observations import (
    EXCLUSIVE_GROUP_SEPARATOR,
    ObservedFacts,
    WireObservations,
)
from core.providers.wire_profile import (
    PROTOCOLS,
    Admission,
    BudgetRule,
    MediaRules,
    ParameterRule,
    ProfileStatus,
    Protocol,
    ReasoningWire,
    ReplayRules,
    RequestRules,
    ResponseRules,
    Verification,
    WireProfile,
)
from core.utils.config import VBOT_ROOT
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.models.models import Model

_LOGGER = get_logger("providers.wire_profiles")

ModelResolver = Callable[[str, str], "Model | None"]
"""``(provider_id, model_id) -> Model | None`` from the live Model DB."""

ProtocolSupport = Callable[[str], Sequence[Protocol] | None]
"""``provider_id -> protocols its Adapter speaks`` (first = default), or ``None``."""

LAYER_PROTOCOL = "protocol"
LAYER_FILE_DEFAULTS = "defaults"
LAYER_FILE_PROTOCOL = "protocols"
LAYER_FILE_CONNECTION = "connections"
LAYER_CATALOG = "catalog"
LAYER_CATALOG_HINT = "catalog_hint"
LAYER_RULE = "rule"
LAYER_OBSERVED = "observed"
LAYER_MODEL = "model"
LAYER_MODEL_CONNECTION = "model_connection"
_GENERIC_LAYERS = frozenset(
    {LAYER_PROTOCOL, LAYER_FILE_DEFAULTS, LAYER_FILE_PROTOCOL, LAYER_FILE_CONNECTION}
)

_CATALOG_HINT_PROTOCOLS: Mapping[str, Protocol] = MappingProxyType(
    {
        "@ai-sdk/anthropic": "messages",
        "@ai-sdk/google": "gemini",
        "@ai-sdk/openai": "responses",
        "@ai-sdk/openai-compatible": "chat_completions",
    }
)
"""Catalog protocol hint (``metadata.<provider>.npm``, the Model's own AI SDK
package) → the wire protocol that package speaks. Any other package names no
protocol vBot can derive and is ignored."""


@dataclass(frozen=True)
class WireBinding:
    """One Adapter's view of wire profiles and learned facts for its Connection."""

    provider_id: str
    connection_id: str
    profiles: WireProfiles
    observations: WireObservations | None = None

    def profile(self, model_id: str) -> WireProfile:
        return self.profiles.resolve(self.provider_id, self.connection_id, model_id)

    def observe_reasoning_field(self, model_id: str, field: str) -> None:
        if self.observations is not None:
            self.observations.record_reasoning_field(
                self.provider_id, self.connection_id, model_id, field
            )

    def observe_reasoning_returned(self, model_id: str) -> None:
        if self.observations is not None:
            self.observations.record_reasoning_returned(
                self.provider_id, self.connection_id, model_id
            )

    def observe_rejected_parameter(self, model_id: str, parameter: str) -> None:
        if self.observations is not None:
            self.observations.record_rejected_parameter(
                self.provider_id, self.connection_id, model_id, parameter
            )

    def observe_exclusive_parameters(self, model_id: str, group: Sequence[str]) -> None:
        if self.observations is not None:
            self.observations.record_exclusive_parameters(
                self.provider_id, self.connection_id, model_id, group
            )

    def observe_rejected_effort(self, model_id: str, effort: str) -> None:
        if self.observations is not None:
            self.observations.record_rejected_effort(
                self.provider_id, self.connection_id, model_id, effort
            )


@dataclass(frozen=True)
class _CacheEntry:
    model: Model | None
    files_generation: int
    observations_generation: int
    profile: WireProfile


class WireProfiles:
    """Resolve and cache wire profiles for every Provider of one Runtime."""

    def __init__(
        self,
        *,
        files: Mapping[str, WireProfileFile],
        protocol_support: ProtocolSupport,
        model_resolver: ModelResolver,
        report: WireIssueReport,
        observations: WireObservations | None = None,
    ) -> None:
        self._files: Mapping[str, WireProfileFile] = MappingProxyType(dict(files))
        self._protocol_support = protocol_support
        self._model_resolver = model_resolver
        self._report = report
        self._observations = observations
        self._files_generation = 0
        self._cache: dict[tuple[str, str, str], _CacheEntry] = {}
        self._lock = threading.Lock()

    def replace_files(self, files: Mapping[str, WireProfileFile]) -> None:
        """Swap the profile data (bundled files plus Custom Provider blocks)."""

        with self._lock:
            self._files = MappingProxyType(dict(files))
            self._files_generation += 1
            self._cache.clear()

    def set_observations(self, observations: WireObservations | None) -> None:
        with self._lock:
            self._observations = observations
            self._cache.clear()

    @property
    def files(self) -> Mapping[str, WireProfileFile]:
        """The current profile data by Provider id (bundled files plus Custom Provider blocks)."""
        return self._files

    def file_for(self, provider_id: str) -> WireProfileFile | None:
        return self._files.get(provider_id)

    def resolve(self, provider_id: str, connection_id: str, model_id: str) -> WireProfile:
        """Return the wire profile for one Model id on one local Connection id.

        ``model_id`` may carry a ``::`` suffix; it is resolved by its bare wire id.
        """

        bare_id = model_id.split("::", 1)[0]
        model = self._model_resolver(provider_id, bare_id)
        observations = self._observations
        observations_generation = observations.generation if observations is not None else 0
        key = (provider_id, connection_id, bare_id)
        cached = self._cache.get(key)
        if (
            cached is not None
            and cached.model is model
            and cached.files_generation == self._files_generation
            and cached.observations_generation == observations_generation
        ):
            return cached.profile
        profile = _Resolution(
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=bare_id,
            model=model,
            file=self._files.get(provider_id),
            protocols=self._protocol_support(provider_id),
            observed=(
                observations.facts_for(provider_id, connection_id, bare_id)
                if observations is not None
                else None
            ),
            report=self._report,
        ).resolve()
        self._cache[key] = _CacheEntry(
            model=model,
            files_generation=self._files_generation,
            observations_generation=observations_generation,
            profile=profile,
        )
        return profile

    def status(
        self, provider_id: str, connection_id: str, model_id: str
    ) -> tuple[ProfileStatus, Verification | None]:
        """Return ``(status, verification)`` of one target without resolving its profile.

        Equals the ``status`` and ``verification`` of :meth:`resolve`: they
        depend only on the Model entry and the matching rules, never on learned
        observations, so a listing of every catalog Model stays cheap.
        """

        bare_id = model_id.split("::", 1)[0]
        file = self._files.get(provider_id)
        resolution = _Resolution(
            provider_id=provider_id,
            connection_id=connection_id,
            model_id=bare_id,
            model=self._model_resolver(provider_id, bare_id),
            file=file,
            protocols=None,
            observed=None,
            report=self._report,
        )
        entry = file.models.get(bare_id) if file is not None else None
        return resolution._status(entry, resolution._matching_rules())

    def bind(self, provider_id: str, connection_id: str) -> WireBinding:
        """Return the profile and observation view of one Connection (for Adapters)."""

        return WireBinding(
            provider_id=provider_id,
            connection_id=connection_id,
            profiles=self,
            observations=self._observations,
        )


_reported_issues: set[str] = set()


def log_wire_profile_issue(message: str) -> None:
    """Log a wire profile data issue once per process."""

    if message in _reported_issues:
        return
    _reported_issues.add(message)
    _LOGGER.warning("Wire profile data issue: %s", message)


@functools.cache
def bundled_wire_profile_files(resources_dir: Path | None = None) -> Mapping[str, WireProfileFile]:
    """The wire profile files shipped in ``<resources>/wire`` (each directory loaded once).

    ``resources_dir`` defaults to this installation's ``resources`` directory.
    """

    return _load_bundled_files((resources_dir or VBOT_ROOT / "resources").resolve())


@functools.cache
def _load_bundled_files(resources_dir: Path) -> Mapping[str, WireProfileFile]:
    return MappingProxyType(load_wire_profile_files(resources_dir, report=log_wire_profile_issue))


def custom_provider_wire_file(
    provider_id: str,
    provider: Mapping[str, Any],
    *,
    report: WireIssueReport,
    source: str | None = None,
) -> WireProfileFile | None:
    """Parse the ``wire`` block of one Custom Provider Settings record.

    The block is that Provider's wire profile file: the body of a bundled file
    without ``format_version``. It may only name the Provider's one Connection
    and the protocols its Adapter speaks. ``None`` when the record has no block
    or the block is not an object (reported); every other invalid entry is
    reported and omitted on its own.
    """

    raw = provider.get("wire")
    if raw is None:
        return None
    # Imported here: Adapters import this module, and Settings import Providers lazily.
    from core.providers.adapter_types import ADAPTER_TYPES
    from core.settings.normalizers import CUSTOM_PROVIDER_CONNECTION_ID

    adapter_class = ADAPTER_TYPES.get(str(provider.get("adapter")))
    return parse_wire_profile_block(
        provider_id,
        raw,
        source=source or f"settings.json:providers.custom.{provider_id}.wire",
        report=report,
        connection_ids=(CUSTOM_PROVIDER_CONNECTION_ID,),
        protocols=adapter_class.WIRE_PROTOCOLS if adapter_class is not None else None,
    )


def wire_profile_files(
    custom_providers: Mapping[str, Mapping[str, Any]] | None = None,
    resources_dir: Path | None = None,
) -> Mapping[str, WireProfileFile]:
    """The bundled files (of ``resources_dir``) plus every Custom Provider's wire block.

    A Custom Provider's block is its only file; without a block it resolves
    from the protocol defaults and its catalog.
    """

    files = dict(bundled_wire_profile_files(resources_dir))
    for provider_id, provider in (custom_providers or {}).items():
        files.pop(provider_id, None)
        parsed = custom_provider_wire_file(provider_id, provider, report=log_wire_profile_issue)
        if parsed is not None:
            files[provider_id] = parsed
    return MappingProxyType(files)


def standalone_wire_binding(
    *,
    provider_id: str,
    connection_id: str,
    protocols: Sequence[Protocol],
    model_lookup: Callable[[str], Model | None] | None,
    files: Mapping[str, WireProfileFile] | None = None,
) -> WireBinding:
    """Profile lookup for an Adapter built outside a Runtime (tools, tests).

    Uses ``files`` (the bundled files when omitted) and the Adapter's own Model
    lookup. Learned facts live in memory for the Adapter's lifetime only.
    """

    def resolve_model(_provider_id: str, model_id: str) -> Model | None:
        return model_lookup(model_id) if model_lookup is not None else None

    profiles = WireProfiles(
        files=bundled_wire_profile_files() if files is None else files,
        protocol_support=lambda _provider_id: protocols,
        model_resolver=resolve_model,
        report=log_wire_profile_issue,
        observations=WireObservations(None, save_delay=None),
    )
    return profiles.bind(provider_id, connection_id)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


# Structured reasoning carriers: opaque state that replay fidelity decides, never readable text.
_STRUCTURED_REASONING_FIELDS = frozenset({"reasoning_details", "encrypted_content"})


class _Resolution:
    def __init__(
        self,
        *,
        provider_id: str,
        connection_id: str,
        model_id: str,
        model: Model | None,
        file: WireProfileFile | None,
        protocols: Sequence[Protocol] | None,
        observed: ObservedFacts | None,
        report: WireIssueReport,
    ) -> None:
        self.provider_id = provider_id
        self.connection_id = connection_id
        self.model_id = model_id
        self.model = model
        self.file = file
        self.protocols = tuple(protocols) if protocols else ("chat_completions",)
        self.observed = observed
        self.report = report
        self.values: dict[str, Any] = {}
        self.provenance: dict[str, str] = {}

    def resolve(self) -> WireProfile:
        file = self.file
        entry = file.models.get(self.model_id) if file is not None else None
        rules = self._matching_rules()
        protocol, protocol_layer = self._protocol(entry, rules)
        self.provenance["protocol"] = protocol_layer

        self._apply(PROTOCOL_DEFAULTS[protocol], LAYER_PROTOCOL)
        if file is not None:
            self._apply(file.defaults, LAYER_FILE_DEFAULTS)
            self._apply(file.protocols.get(protocol, _EMPTY), LAYER_FILE_PROTOCOL)
            self._apply(file.connections.get(self.connection_id, _EMPTY), LAYER_FILE_CONNECTION)
        self._apply_catalog(protocol)
        for rule in rules:
            self._apply(rule.values, f"{LAYER_RULE}[{rule.index}]")
        if self.observed is not None and not self.observed.is_empty():
            self._apply_observed(self.observed)
        if entry is not None:
            self._apply(entry.values, LAYER_MODEL)
            self._apply(entry.connections.get(self.connection_id, _EMPTY), LAYER_MODEL_CONNECTION)

        status, verification = self._status(entry, rules)
        return WireProfile(
            provider_id=self.provider_id,
            connection_id=self.connection_id,
            model_id=self.model_id,
            protocol=protocol,
            status=status,
            verification=verification,
            admission=_build_admission(self.values.get("admission", {})),
            request=_build_request(self.values.get("request", {})),
            reasoning=_build_reasoning(self.values.get("reasoning", {})),
            response=_build_response(self.values.get("response", {})),
            replay=_build_replay(self.values.get("replay", {})),
            media=_build_media(self.values.get("media", {})),
            known_model=self.model is not None,
            provenance=MappingProxyType(dict(self.provenance)),
        )

    # -- protocol -------------------------------------------------------------

    def _protocol(
        self, entry: WireModelEntry | None, rules: Sequence[WireRule]
    ) -> tuple[Protocol, str]:
        candidates: list[tuple[Any, str]] = []
        if entry is not None:
            candidates.append(
                (
                    entry.connections.get(self.connection_id, _EMPTY).get("protocol"),
                    LAYER_MODEL_CONNECTION,
                )
            )
            candidates.append((entry.values.get("protocol"), LAYER_MODEL))
        for rule in reversed(rules):
            candidates.append((rule.values.get("protocol"), f"{LAYER_RULE}[{rule.index}]"))
        hinted = self._hinted_protocol()
        if hinted is not None:
            candidates.append((hinted, LAYER_CATALOG_HINT))
        if self.file is not None:
            candidates.append(
                (
                    self.file.connections.get(self.connection_id, _EMPTY).get("protocol"),
                    LAYER_FILE_CONNECTION,
                )
            )
            candidates.append((self.file.defaults.get("protocol"), LAYER_FILE_DEFAULTS))
        for value, layer in candidates:
            if value is None:
                continue
            if value in self.protocols:
                return cast(Protocol, value), layer
            self.report(
                f"wire profile {self.provider_id}:{self.connection_id}/{self.model_id}: "
                f"protocol {value!r} from {layer} is not spoken by the Provider's Adapter "
                f"({', '.join(self.protocols)}), ignoring it"
            )
        return self.protocols[0], LAYER_PROTOCOL

    def _hinted_protocol(self) -> Protocol | None:
        """The protocol the catalog's AI SDK package names, if the Adapter speaks it.

        A hint is a catalog fact, not a curated choice: a package the Adapter
        cannot speak is skipped silently instead of reported as a data issue.
        """

        npm = _catalog_hint(self.provider_id, self.model, "npm")
        protocol = _CATALOG_HINT_PROTOCOLS.get(npm) if isinstance(npm, str) else None
        return protocol if protocol in self.protocols else None

    # -- rules ----------------------------------------------------------------

    def _matching_rules(self) -> tuple[WireRule, ...]:
        if self.file is None:
            return ()
        return tuple(rule for rule in self.file.rules if self._matches(rule.when))

    def _matches(self, when: RuleMatcher) -> bool:
        model = self.model
        model_id = self.model_id
        if when.connections is not None and self.connection_id not in when.connections:
            return False
        if when.ids is not None and model_id not in when.ids:
            return False
        if when.prefix is not None and not model_id.startswith(when.prefix):
            return False
        if when.suffix is not None and not model_id.endswith(when.suffix):
            return False
        if when.unknown is not None and when.unknown != (model is None):
            return False
        if when.family is not None and (model is None or model.family not in when.family):
            return False
        if when.control is not None:
            control = model.capabilities.reasoning.control if model is not None else None
            if (control or "none") not in when.control:
                return False
        if when.npm is not None and _catalog_hint(self.provider_id, model, "npm") not in when.npm:
            return False
        return not when.metadata or self._metadata_matches(when.metadata)

    def _metadata_matches(self, expected: Mapping[str, Any]) -> bool:
        facts = _provider_metadata(self.provider_id, self.model)
        for key, wanted in expected.items():
            actual = facts.get(key)
            if isinstance(wanted, Mapping) and set(wanted) == {"contains"}:
                if not isinstance(actual, list | tuple) or wanted["contains"] not in actual:
                    return False
            elif _thaw(actual) != _thaw(wanted):
                return False
        return True

    # -- layers ---------------------------------------------------------------

    def _apply(self, partial: Mapping[str, Any], layer: str) -> None:
        # The protocol was decided first (``_protocol``); layers only shape fields.
        fields = {key: value for key, value in partial.items() if key != "protocol"}
        _merge_into(self.values, fields, PROFILE_SCHEMA, "", layer, self.provenance)

    def _apply_catalog(self, protocol: Protocol) -> None:
        model = self.model
        if model is None:
            return
        reasoning = model.capabilities.reasoning
        partial: dict[str, Any] = {}
        reasoning_values: dict[str, Any] = {}
        if isinstance(reasoning.supported, bool):
            reasoning_values["supported"] = reasoning.supported
        if isinstance(reasoning.control, str):
            reasoning_values["control"] = reasoning.control
        levels = tuple(
            level for raw in reasoning.levels or () if (level := normalize_thinking_effort(raw))
        )
        if levels:
            reasoning_values["catalog_levels"] = levels
        budget_max = reasoning.budget_max
        if isinstance(budget_max, int) and not isinstance(budget_max, bool) and budget_max > 0:
            reasoning_values["budget_max"] = budget_max
        if reasoning.mandatory:
            reasoning_values["mandatory"] = True
        if reasoning_values:
            partial["reasoning"] = reasoning_values

        current_fields = tuple(self.values.get("response", {}).get("reasoning_fields", ()))
        # models.dev ``interleaved.field`` (an OpenAI-compatible Chat setting):
        # the Model answers with readable reasoning in this field and takes it
        # back there in history. A structured carrier is opaque state, which
        # replay fidelity decides, never a readable field.
        interleaved = _catalog_hint(self.provider_id, model, "interleaved_field")
        if (
            protocol == "chat_completions"
            and isinstance(interleaved, str)
            and interleaved
            and interleaved not in _STRUCTURED_REASONING_FIELDS
        ):
            partial["response"] = {
                "reasoning_fields": (interleaved, *(f for f in current_fields if f != interleaved))
            }
            partial["replay"] = {"history_field": interleaved}
        _merge_into(self.values, partial, _CATALOG_SCHEMA, "", LAYER_CATALOG, self.provenance)

    def _apply_observed(self, facts: ObservedFacts) -> None:
        """Translate learned facts into profile values (below every Model entry)."""

        partial: dict[str, Any] = {}
        field = facts.reasoning_field
        if field:
            current = tuple(self.values.get("response", {}).get("reasoning_fields", ()))
            partial["response"] = {
                "reasoning_fields": (field, *(item for item in current if item != field))
            }
            # Echo the observed field back only where the wire opts in and the
            # carrier is a generic default; catalog hints and rules know better.
            replay = self.values.get("replay", {})
            if (
                replay.get("echo_response_field") is True
                and replay.get("history_field") is not None
                and self.provenance.get("replay.history_field") in _GENERIC_LAYERS
            ):
                partial["replay"] = {"history_field": field}
        if facts.reasoning_returned and self.values.get("reasoning", {}).get("supported") is None:
            # A Model the catalog does not know returned reasoning: it reasons.
            partial["reasoning"] = {"supported": True}
        if facts.rejected_parameters:
            partial["request"] = {
                "parameters": {name: {"mode": "drop"} for name in facts.rejected_parameters}
            }
        if facts.exclusive_parameters:
            # Learned groups join the configured ones instead of replacing them.
            configured = tuple(self.values.get("request", {}).get("exclusive_parameters", ()))
            learned = tuple(
                tuple(name.split(EXCLUSIVE_GROUP_SEPARATOR)) for name in facts.exclusive_parameters
            )
            partial.setdefault("request", {})["exclusive_parameters"] = tuple(
                dict.fromkeys((*(tuple(group) for group in configured), *learned))
            )
        rejected = facts.rejected_efforts
        narrowed_map: dict[str, str] | None = None
        if rejected:
            reasoning = _build_reasoning(self.values.get("reasoning", {}))
            learned = partial.setdefault("reasoning", {})
            if "none" in rejected and (
                "none" not in reasoning.ladder or dialect_carriers(reasoning.dialect).off_switch
            ):
                # A rejected explicit off that no ladder rung spells (the
                # dialect's off switch, or a ``none`` outside the ladder):
                # Agent effort ``none`` leaves reasoning to the Provider.
                learned["off"] = "omit"
            if any(level in rejected for level in reasoning.ladder) or any(
                level in rejected for level in reasoning.effort_map.values()
            ):
                narrowed_map = {
                    effort: level
                    for effort, level in reasoning.effort_map.items()
                    if level not in rejected
                }
                learned["levels"] = tuple(
                    level for level in reasoning.ladder if level not in rejected
                )
                learned["effort_map"] = narrowed_map
        _merge_into(self.values, partial, PROFILE_SCHEMA, "", LAYER_OBSERVED, self.provenance)
        if narrowed_map is not None:
            # A rejected mapping must disappear, not merge with the earlier map.
            self.values["reasoning"]["effort_map"] = narrowed_map

    # -- status ---------------------------------------------------------------

    def _status(
        self, entry: WireModelEntry | None, rules: Sequence[WireRule]
    ) -> tuple[ProfileStatus, Any]:
        if entry is not None:
            verification = entry.verification
            if verification is not None and (
                not verification.connections or self.connection_id in verification.connections
            ):
                return "verified", verification
            return "configured", None
        if any(rule.when.ids is not None for rule in rules):
            return "configured", None
        return "inferred", None


_EMPTY: Mapping[str, Any] = MappingProxyType({})

# The catalog layer also writes ``reasoning.catalog_levels``, which no file may set.
_CATALOG_SCHEMA: dict[str, Any] = {
    **PROFILE_SCHEMA,
    "reasoning": {
        **PROFILE_SCHEMA["reasoning"],
        "catalog_levels": PROFILE_SCHEMA["reasoning"]["floor"],
    },
}


def _merge_into(
    target: dict[str, Any],
    partial: Mapping[str, Any],
    schema: Any,
    prefix: str,
    layer: str,
    provenance: dict[str, str],
) -> None:
    for key, value in partial.items():
        node = child_node(schema, key)
        path = f"{prefix}{key}"
        if node is None or is_opaque(node):
            target[key] = value
            provenance[path] = layer
            continue
        child = target.get(key)
        if not isinstance(child, dict):
            child = {}
            target[key] = child
        _merge_into(child, value, node, f"{path}.", layer, provenance)


def _provider_metadata(provider_id: str, model: Model | None) -> Mapping[str, Any]:
    if model is None:
        return _EMPTY
    facts = model.metadata.get(provider_id.replace("-", "_"))
    return facts if isinstance(facts, Mapping) else _EMPTY


def _catalog_hint(provider_id: str, model: Model | None, key: str) -> Any:
    return _provider_metadata(provider_id, model).get(key)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Building the frozen profile
# ---------------------------------------------------------------------------


def _frozen(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping or {}))


def _build_admission(values: Mapping[str, Any]) -> Admission:
    return Admission(**{key: values[key] for key in ("state", "message") if key in values})


def _build_request(values: Mapping[str, Any]) -> RequestRules:
    kwargs: dict[str, Any] = {
        key: values[key]
        for key in (
            "output_limit_field",
            "output_limit_default",
            "output_limit_cap",
            "output_limit_collapse",
            "allowed_parameters",
            "tool_schema",
            "tool_call_ids",
            "list_announced_tools",
            "prompt_cache",
        )
        if key in values
    }
    kwargs["parameters"] = MappingProxyType(
        {name: ParameterRule(**rule) for name, rule in (values.get("parameters") or {}).items()}
    )
    kwargs["exclusive_parameters"] = tuple(
        tuple(group) for group in values.get("exclusive_parameters") or ()
    )
    for key in ("body_defaults", "extra_body", "extra_headers", "options"):
        kwargs[key] = _frozen(values.get(key))
    return RequestRules(**kwargs)


def _build_reasoning(values: Mapping[str, Any]) -> ReasoningWire:
    kwargs: dict[str, Any] = {
        key: values[key]
        for key in (
            "dialect",
            "supported",
            "control",
            "catalog_levels",
            "levels",
            "floor",
            "snap",
            "off",
            "unset",
            "mandatory",
            "budget_max",
        )
        if key in values
    }
    kwargs["effort_map"] = _frozen(values.get("effort_map"))
    kwargs["options"] = _frozen(values.get("options"))
    if "budget" in values:
        kwargs["budget"] = BudgetRule(**values["budget"])
    return ReasoningWire(**kwargs)


def _build_response(values: Mapping[str, Any]) -> ResponseRules:
    return ResponseRules(
        reasoning_fields=tuple(values.get("reasoning_fields", ())),
        options=_frozen(values.get("options")),
    )


def _build_replay(values: Mapping[str, Any]) -> ReplayRules:
    return ReplayRules(
        **{
            key: values[key]
            for key in (
                "scope",
                "fidelity",
                "history_field",
                "echo_response_field",
                "echo_empty_on_tool_calls",
                "strip_when_off",
            )
            if key in values
        }
    )


def _build_media(values: Mapping[str, Any]) -> MediaRules:
    kwargs: dict[str, Any] = {
        key: values[key] for key in ("image_max_bytes", "request_max_bytes") if key in values
    }
    if "types" in values:
        kwargs["types"] = frozenset(values["types"])
    return MediaRules(**kwargs)


__all__ = [
    "bundled_wire_profile_files",
    "custom_provider_wire_file",
    "log_wire_profile_issue",
    "standalone_wire_binding",
    "wire_profile_files",
    "LAYER_CATALOG",
    "LAYER_CATALOG_HINT",
    "LAYER_FILE_CONNECTION",
    "LAYER_FILE_DEFAULTS",
    "LAYER_FILE_PROTOCOL",
    "LAYER_MODEL",
    "LAYER_MODEL_CONNECTION",
    "LAYER_OBSERVED",
    "LAYER_PROTOCOL",
    "LAYER_RULE",
    "PROTOCOLS",
    "ModelResolver",
    "WireBinding",
    "ProtocolSupport",
    "WireProfiles",
]
