"""Voice configuration schema: the single owner of the stored ``wakeword`` section.

The section lives in the Desktop settings store (``desktop.settings``) under the
``wakeword`` key. This module owns its complete shape:

- defaults and limits (:data:`MAX_ACTIVE_PHRASES`, the sensitivity range);
- tolerant parsing of stored data into an immutable :class:`VoiceConfig`
  snapshot (:func:`parse_voice_config`): malformed entries are dropped one by
  one, never the whole section;
- strict validation of one partial change (:func:`apply_voice_changes`,
  :func:`set_enabled`): invalid input rejects the whole change with a
  :class:`VoiceConfigError` before anything could be written;
- server-profile keys (:func:`canonical_profile_key`) and phrase action types;
- the config part of the Voice status snapshot (:func:`config_status`).

Pure: no I/O and no threading. Callers run :func:`apply_voice_changes` and
:func:`set_enabled` as the mutation of ``desktop.settings.update_section`` so
the read-modify-write is one locked transaction.

Stored shape (every field optional)::

    {
      "enabled": true,
      "microphone": {"index": 4, "name": "Studio mic", "host_api": "Windows WASAPI"},
      "echo_cancellation": true,
      "active_model_ids": ["builtin/hey_nabu", "builtin/hey_jarvis"],
      "model_sensitivities": {"builtin/hey_nabu": 0.55},
      "server_profiles": {
        "http://127.0.0.1:8421": {
          "target_agent_id": "main",
          "session_behavior": "active",
          "phrase_actions": {
            "builtin/hey_jarvis": {"type": "command", "agent_id": "coder",
                                   "session_behavior": "new"},
            "builtin/okay_nabu": {"type": "live_voice", "mode": "toggle"}
          }
        }
      }
    }

Acoustic settings (active phrases, sensitivities, microphone, echo
cancellation) are global. Agent ids are server-specific, so the default command
target and the per-phrase actions live in the profile of one server. Writers
keep unknown keys inside and outside the section.

The retired global ``model_actions`` (``{model_id: "command" | "live_voice"}``)
is carried over on read and write: each ``live_voice`` entry becomes a
``{"type": "live_voice", "mode": "start"}`` action in every stored profile
without a valid action for that model (``start`` is what it did). The key is
removed by the next write once it was carried over; while no profile exists
it stays and is carried into the first profile a write creates.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeAlias
from urllib.parse import urlsplit

MAX_ACTIVE_PHRASES = 8
MIN_SENSITIVITY = 0.05
MAX_SENSITIVITY = 0.95
DEFAULT_SENSITIVITY = 0.5
DEFAULT_MODEL_IDS = ("builtin/okay_nabu", "builtin/hey_nabu")

SESSION_BEHAVIOR_ACTIVE = "active"
SESSION_BEHAVIOR_NEW = "new"
SESSION_BEHAVIORS = (SESSION_BEHAVIOR_ACTIVE, SESSION_BEHAVIOR_NEW)
DEFAULT_SESSION_BEHAVIOR = SESSION_BEHAVIOR_ACTIVE

# What a detection of one phrase does: record and send a spoken command, or ask
# the page to start or toggle a Live voice call (nothing is recorded).
ACTION_COMMAND = "command"
ACTION_LIVE_VOICE = "live_voice"
ACTION_TYPES = (ACTION_COMMAND, ACTION_LIVE_VOICE)

LIVE_MODE_START = "start"
LIVE_MODE_TOGGLE = "toggle"
LIVE_MODES = (LIVE_MODE_START, LIVE_MODE_TOGGLE)
DEFAULT_LIVE_MODE = LIVE_MODE_TOGGLE

ERROR_VOICE_CONFIG_INVALID = "voice_config_invalid"
ERROR_NO_SERVER = "no_server"

_KEY_ENABLED = "enabled"
_KEY_MICROPHONE = "microphone"
_KEY_ECHO_CANCELLATION = "echo_cancellation"
_KEY_ACTIVE_MODEL_IDS = "active_model_ids"
_KEY_MODEL_SENSITIVITIES = "model_sensitivities"
_KEY_SERVER_PROFILES = "server_profiles"
_KEY_RETIRED_MODEL_ACTIONS = "model_actions"
_PROFILE_KEY_AGENT = "target_agent_id"
_PROFILE_KEY_SESSION_BEHAVIOR = "session_behavior"
_PROFILE_KEY_PHRASE_ACTIONS = "phrase_actions"

# Change keys accepted by apply_voice_changes. ``enabled`` has its own
# operation (set_enabled) because enabling runs readiness checks first.
_CHANGE_MICROPHONE = "microphone"
_CHANGE_ECHO_CANCELLATION = "echo_cancellation"
_CHANGE_ACTIVE_MODEL_IDS = "active_model_ids"
_CHANGE_MODEL_SENSITIVITIES = "model_sensitivities"
_CHANGE_DEFAULT_AGENT = "default_agent_id"
_CHANGE_DEFAULT_SESSION_BEHAVIOR = "default_session_behavior"
_CHANGE_PHRASE_ACTIONS = "phrase_actions"
_GLOBAL_CHANGE_KEYS = (
    _CHANGE_MICROPHONE,
    _CHANGE_ECHO_CANCELLATION,
    _CHANGE_ACTIVE_MODEL_IDS,
    _CHANGE_MODEL_SENSITIVITIES,
)
_PROFILE_CHANGE_KEYS = (
    _CHANGE_DEFAULT_AGENT,
    _CHANGE_DEFAULT_SESSION_BEHAVIOR,
    _CHANGE_PHRASE_ACTIONS,
)
_CHANGE_KEYS = frozenset((*_GLOBAL_CHANGE_KEYS, *_PROFILE_CHANGE_KEYS))


class VoiceConfigError(ValueError):
    """A rejected Voice config change with a stable code and the offending field."""

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        error_code: str = ERROR_VOICE_CONFIG_INVALID,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.error_code = error_code


@dataclass(frozen=True)
class MicrophoneSelection:
    """Stable identity of an explicitly chosen input device.

    Capture accepts the stored index only while name and host API still match,
    so a reordered device list never routes audio to a recycled index.
    """

    index: int
    name: str
    host_api: str

    def to_dict(self) -> dict[str, Any]:
        """Return the stored ``{index, name, host_api}`` form."""
        return {"index": self.index, "name": self.name, "host_api": self.host_api}


@dataclass(frozen=True)
class CommandAction:
    """Record a spoken command and send it to an Agent.

    ``None`` fields fall back to the server profile defaults; see
    :meth:`VoiceConfig.effective_action`.
    """

    agent_id: str | None = None
    session_behavior: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the public ``{type, agent_id, session_behavior}`` form."""
        return {
            "type": ACTION_COMMAND,
            "agent_id": self.agent_id,
            "session_behavior": self.session_behavior,
        }


@dataclass(frozen=True)
class LiveVoiceAction:
    """Ask the page to start a Live voice call, or toggle it (end a running call)."""

    mode: str = DEFAULT_LIVE_MODE

    def to_dict(self) -> dict[str, Any]:
        """Return the public ``{type, mode}`` form."""
        return {"type": ACTION_LIVE_VOICE, "mode": self.mode}


PhraseAction: TypeAlias = CommandAction | LiveVoiceAction


@dataclass(frozen=True)
class PhraseConfig:
    """One active wake phrase: a catalog model id and its detection sensitivity."""

    model_id: str
    sensitivity: float = DEFAULT_SENSITIVITY


def _frozen_mapping(values: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(values or {}))


@dataclass(frozen=True)
class ServerVoiceProfile:
    """Server-specific routing: the default command target and per-phrase actions."""

    default_agent_id: str | None = None
    default_session_behavior: str = DEFAULT_SESSION_BEHAVIOR
    phrase_actions: Mapping[str, PhraseAction] = field(default_factory=_frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "phrase_actions", _frozen_mapping(self.phrase_actions))


_EMPTY_PROFILE = ServerVoiceProfile()


def _default_phrases() -> tuple[PhraseConfig, ...]:
    return tuple(PhraseConfig(model_id) for model_id in DEFAULT_MODEL_IDS)


@dataclass(frozen=True)
class VoiceConfig:
    """Immutable snapshot of the stored Voice configuration.

    ``phrases`` are the active phrases in stored order with their effective
    sensitivities; ``profiles`` are keyed by :func:`canonical_profile_key`.
    """

    enabled: bool = False
    microphone: MicrophoneSelection | None = None
    echo_cancellation: bool = True
    phrases: tuple[PhraseConfig, ...] = field(default_factory=_default_phrases)
    profiles: Mapping[str, ServerVoiceProfile] = field(default_factory=_frozen_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "phrases", tuple(self.phrases))
        object.__setattr__(self, "profiles", _frozen_mapping(self.profiles))

    @property
    def active_model_ids(self) -> tuple[str, ...]:
        """Ordered model ids of the active phrases."""
        return tuple(phrase.model_id for phrase in self.phrases)

    def phrase(self, model_id: str) -> PhraseConfig | None:
        """Return the active phrase for ``model_id``, or ``None`` when inactive."""
        return next((phrase for phrase in self.phrases if phrase.model_id == model_id), None)

    def profile_for(self, server_url: str) -> ServerVoiceProfile:
        """Return the routing profile of one server (empty defaults when none is stored)."""
        key = canonical_profile_key(server_url)
        if not key:
            return _EMPTY_PROFILE
        return self.profiles.get(key, _EMPTY_PROFILE)

    def effective_action(self, model_id: str, server_url: str) -> PhraseAction:
        """Resolve what a detection of ``model_id`` does on one server.

        A phrase without a stored action is a command. Command fields fall back
        to the profile's default Agent and Session behavior (``active`` when
        the profile stores none); the Agent stays ``None`` when neither is set.
        """
        profile = self.profile_for(server_url)
        action = profile.phrase_actions.get(model_id)
        if isinstance(action, LiveVoiceAction):
            return action
        agent_id = action.agent_id if action is not None else None
        session_behavior = action.session_behavior if action is not None else None
        return CommandAction(
            agent_id=agent_id or profile.default_agent_id,
            session_behavior=session_behavior or profile.default_session_behavior,
        )

    def listener_settings_changed(self, other: VoiceConfig) -> bool:
        """Whether switching to ``other`` needs a new listener (capture and detection).

        Action routing (phrase actions, default Agent and Session behavior) can
        change on a running listener; everything acoustic cannot.
        """
        return (
            self.microphone != other.microphone
            or self.echo_cancellation != other.echo_cancellation
            or self.phrases != other.phrases
        )


def canonical_profile_key(server_url: str) -> str:
    """Return the server-profile key for a server URL, merging loopback aliases.

    The same local server is reachable as ``localhost`` and ``127.0.0.1``;
    profiles keyed by the raw spelling would lose the stored Agents whenever the
    launch host spelling changes between the two forms. Surrounding whitespace
    and trailing slashes never form a different key. An empty URL yields ``""``.
    """
    url = (server_url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "localhost":
        return url
    try:
        port = parts.port
    except ValueError:
        return url
    port_suffix = f":{port}" if port else ""
    return f"{parts.scheme or 'http'}://127.0.0.1{port_suffix}{parts.path}"


def voice_limits() -> dict[str, Any]:
    """Return the limits the WebUI enforces (it hard-codes none of its own)."""
    return {
        "max_active_phrases": MAX_ACTIVE_PHRASES,
        "min_sensitivity": MIN_SENSITIVITY,
        "max_sensitivity": MAX_SENSITIVITY,
    }


# -- Tolerant parsing ----------------------------------------------------------


def parse_voice_config(raw: object) -> VoiceConfig:
    """Build a :class:`VoiceConfig` from the stored section, never raising.

    A missing or non-object section yields the defaults. Each malformed field
    falls back to its default on its own: malformed ``active_model_ids`` (not
    1 to :data:`MAX_ACTIVE_PHRASES` unique non-empty ids) select the default
    phrases; malformed sensitivities, profiles and phrase actions are dropped
    entry by entry. Unknown keys are ignored; the retired ``model_actions`` is
    carried over into the stored profiles first (see the module docstring).
    """
    section = _writable_section(raw)
    enabled = section.get(_KEY_ENABLED)
    echo_cancellation = section.get(_KEY_ECHO_CANCELLATION)
    model_ids = _parse_active_model_ids(section.get(_KEY_ACTIVE_MODEL_IDS))
    sensitivities = _parse_sensitivities(section.get(_KEY_MODEL_SENSITIVITIES))
    return VoiceConfig(
        enabled=enabled if isinstance(enabled, bool) else False,
        microphone=_parse_microphone(section.get(_KEY_MICROPHONE)),
        echo_cancellation=echo_cancellation if isinstance(echo_cancellation, bool) else True,
        phrases=tuple(
            PhraseConfig(model_id, sensitivities.get(model_id, DEFAULT_SENSITIVITY))
            for model_id in (model_ids or DEFAULT_MODEL_IDS)
        ),
        profiles=_parse_profiles(section.get(_KEY_SERVER_PROFILES)),
    )


def _parse_microphone(value: object) -> MicrophoneSelection | None:
    if not isinstance(value, Mapping):
        return None
    index = value.get("index")
    name = value.get("name")
    host_api = value.get("host_api")
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or index < 0
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(host_api, str)
    ):
        return None
    return MicrophoneSelection(index=index, name=name.strip(), host_api=host_api.strip())


def _parse_active_model_ids(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ACTIVE_PHRASES:
        return None
    model_ids = tuple(_non_empty_string(model_id) for model_id in value)
    if any(model_id is None for model_id in model_ids):
        return None
    if len(set(model_ids)) != len(model_ids):
        return None
    return tuple(model_id for model_id in model_ids if model_id is not None)


def _parse_sensitivities(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    sensitivities: dict[str, float] = {}
    for raw_model_id, raw_sensitivity in value.items():
        model_id = _non_empty_string(raw_model_id)
        sensitivity = _sensitivity_value(raw_sensitivity)
        if model_id is not None and sensitivity is not None:
            sensitivities[model_id] = sensitivity
    return sensitivities


def _parse_profiles(value: object) -> dict[str, ServerVoiceProfile]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: _parse_profile(value[stored_key])
        for key, stored_key in _stored_profile_keys(value).items()
    }


def _stored_profile_keys(profiles: Mapping[Any, Any]) -> dict[str, Any]:
    """Map each canonical profile key to the stored key that serves it.

    An entry stored under the exact canonical spelling wins over alias
    spellings (``localhost``, a trailing slash); otherwise the first alias does.
    Non-object entries never serve a key.
    """
    selected: dict[str, Any] = {}
    for stored_key, profile in profiles.items():
        if not isinstance(stored_key, str) or not isinstance(profile, Mapping):
            continue
        key = canonical_profile_key(stored_key)
        if not key:
            continue
        if key not in selected or (stored_key == key and selected[key] != key):
            selected[key] = stored_key
    return selected


def _parse_profile(profile: Mapping[str, Any]) -> ServerVoiceProfile:
    session_behavior = profile.get(_PROFILE_KEY_SESSION_BEHAVIOR)
    return ServerVoiceProfile(
        default_agent_id=_non_empty_string(profile.get(_PROFILE_KEY_AGENT)),
        default_session_behavior=(
            session_behavior
            if isinstance(session_behavior, str) and session_behavior in SESSION_BEHAVIORS
            else DEFAULT_SESSION_BEHAVIOR
        ),
        phrase_actions=_parse_phrase_actions(profile.get(_PROFILE_KEY_PHRASE_ACTIONS)),
    )


def _parse_phrase_actions(value: object) -> dict[str, PhraseAction]:
    if not isinstance(value, Mapping):
        return {}
    actions: dict[str, PhraseAction] = {}
    for raw_model_id, raw_action in value.items():
        model_id = _non_empty_string(raw_model_id)
        action = _parse_action(raw_action)
        if model_id is not None and action is not None:
            actions[model_id] = action
    return actions


def _parse_action(value: object) -> PhraseAction | None:
    """Parse one stored action; any malformed field drops the whole entry."""
    if not isinstance(value, Mapping):
        return None
    action_type = value.get("type")
    if action_type == ACTION_COMMAND:
        agent_id = value.get("agent_id")
        session_behavior = value.get("session_behavior")
        if agent_id is not None and _non_empty_string(agent_id) is None:
            return None
        if session_behavior is not None and session_behavior not in SESSION_BEHAVIORS:
            return None
        return CommandAction(
            agent_id=_non_empty_string(agent_id),
            session_behavior=session_behavior if isinstance(session_behavior, str) else None,
        )
    if action_type == ACTION_LIVE_VOICE:
        mode = value.get("mode")
        if mode is None:
            return LiveVoiceAction()
        if isinstance(mode, str) and mode in LIVE_MODES:
            return LiveVoiceAction(mode=mode)
    return None


# -- Strict change validation --------------------------------------------------


def apply_voice_changes(
    raw: object,
    changes: object,
    *,
    server_url: str,
    known_model_ids: Callable[[str], bool],
) -> dict[str, Any]:
    """Validate one partial Voice change and return the new stored section.

    ``changes`` keys: ``microphone`` (descriptor or ``null``),
    ``echo_cancellation`` (bool), ``active_model_ids`` (1 to
    :data:`MAX_ACTIVE_PHRASES` unique ids; an id that is not active yet must
    be known, while an active one whose model is gone may stay, so the other
    phrases stay editable), ``model_sensitivities``
    (merged; each known id maps to a number within the sensitivity range),
    ``default_agent_id`` (non-empty id or ``null``), ``default_session_behavior``
    (``active`` | ``new``) and ``phrase_actions`` (merged per known id; a
    ``null`` value removes the entry). The last three belong to the profile of
    ``server_url`` and require one (``error_code`` ``no_server``).

    ``known_model_ids`` answers whether a model id names an installed catalog
    model. Any invalid part rejects the whole change with
    :class:`VoiceConfigError` naming the offending field; ``raw`` is never
    mutated. The returned section keeps unknown keys and carries the retired
    ``model_actions`` over.
    """
    if not isinstance(changes, Mapping):
        raise VoiceConfigError("Voice config changes must be an object")
    for change_key in changes:
        if change_key not in _CHANGE_KEYS:
            raise VoiceConfigError(
                f"Unknown Voice setting: {change_key}",
                field=str(change_key),
            )

    section = _writable_section(raw)
    if _CHANGE_MICROPHONE in changes:
        microphone = _validated_microphone(changes[_CHANGE_MICROPHONE])
        section[_KEY_MICROPHONE] = microphone.to_dict() if microphone is not None else None
    if _CHANGE_ECHO_CANCELLATION in changes:
        section[_KEY_ECHO_CANCELLATION] = _validated_bool(
            changes[_CHANGE_ECHO_CANCELLATION], _CHANGE_ECHO_CANCELLATION
        )
    if _CHANGE_ACTIVE_MODEL_IDS in changes:
        active = frozenset(parse_voice_config(section).active_model_ids)
        section[_KEY_ACTIVE_MODEL_IDS] = _validated_active_model_ids(
            changes[_CHANGE_ACTIVE_MODEL_IDS],
            lambda model_id: model_id in active or known_model_ids(model_id),
        )
    if _CHANGE_MODEL_SENSITIVITIES in changes:
        sensitivities = _validated_sensitivities(
            changes[_CHANGE_MODEL_SENSITIVITIES], known_model_ids
        )
        stored = section.get(_KEY_MODEL_SENSITIVITIES)
        merged = dict(stored) if isinstance(stored, dict) else {}
        merged.update(sensitivities)
        section[_KEY_MODEL_SENSITIVITIES] = merged

    profile_changes = [key for key in _PROFILE_CHANGE_KEYS if key in changes]
    if profile_changes:
        profile_key = canonical_profile_key(server_url)
        if not profile_key:
            raise VoiceConfigError(
                "Voice Agent settings require a connected server",
                field=profile_changes[0],
                error_code=ERROR_NO_SERVER,
            )
        profile = _writable_profile(section, profile_key)
        if _CHANGE_DEFAULT_AGENT in changes:
            profile[_PROFILE_KEY_AGENT] = _validated_agent_id(
                changes[_CHANGE_DEFAULT_AGENT], _CHANGE_DEFAULT_AGENT, nullable=True
            )
        if _CHANGE_DEFAULT_SESSION_BEHAVIOR in changes:
            profile[_PROFILE_KEY_SESSION_BEHAVIOR] = _validated_session_behavior(
                changes[_CHANGE_DEFAULT_SESSION_BEHAVIOR],
                _CHANGE_DEFAULT_SESSION_BEHAVIOR,
                nullable=False,
            )
        if _CHANGE_PHRASE_ACTIONS in changes:
            updates = _validated_phrase_actions(changes[_CHANGE_PHRASE_ACTIONS], known_model_ids)
            stored_actions = profile.get(_PROFILE_KEY_PHRASE_ACTIONS)
            actions = dict(stored_actions) if isinstance(stored_actions, dict) else {}
            for model_id, action in updates.items():
                if action is None:
                    actions.pop(model_id, None)
                else:
                    actions[model_id] = action
            profile[_PROFILE_KEY_PHRASE_ACTIONS] = actions
    return section


def set_enabled(raw: object, enabled: object) -> dict[str, Any]:
    """Return the stored section with ``enabled`` replaced (strict bool)."""
    section = _writable_section(raw)
    section[_KEY_ENABLED] = _validated_bool(enabled, _KEY_ENABLED)
    return section


def forget_model(raw: object, model_id: str) -> dict[str, Any]:
    """Return the stored section without the per-phrase settings of ``model_id``.

    Removes its sensitivity and its action in every server profile, for a
    model that was deleted from the catalog. Malformed containers are left as
    they are; ``active_model_ids`` is not touched.
    """
    section = _writable_section(raw)
    sensitivities = section.get(_KEY_MODEL_SENSITIVITIES)
    if isinstance(sensitivities, dict):
        sensitivities.pop(model_id, None)
    retired_actions = section.get(_KEY_RETIRED_MODEL_ACTIONS)
    if isinstance(retired_actions, dict):
        for key in [key for key in retired_actions if _non_empty_string(key) == model_id]:
            del retired_actions[key]
    profiles = section.get(_KEY_SERVER_PROFILES)
    if isinstance(profiles, dict):
        for profile in profiles.values():
            actions = (
                profile.get(_PROFILE_KEY_PHRASE_ACTIONS) if isinstance(profile, dict) else None
            )
            if isinstance(actions, dict):
                actions.pop(model_id, None)
    return section


def _writable_section(raw: object) -> dict[str, Any]:
    section = copy.deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
    _carry_over_retired_actions(section)
    return section


def _carry_over_retired_actions(section: dict[str, Any]) -> None:
    """Move the retired global ``model_actions`` into the stored profiles.

    Every ``live_voice`` entry becomes a Live voice start action in each
    profile that has no entry for that model; ``command`` entries were the
    default and need nothing. The key is kept while it still has something to
    carry and no profile exists to receive it.
    """
    if _KEY_RETIRED_MODEL_ACTIONS not in section:
        return
    retired = section[_KEY_RETIRED_MODEL_ACTIONS]
    live_model_ids: list[str] = []
    if isinstance(retired, Mapping):
        for raw_model_id, action in retired.items():
            model_id = _non_empty_string(raw_model_id)
            if model_id is not None and action == ACTION_LIVE_VOICE:
                live_model_ids.append(model_id)
    stored_profiles = section.get(_KEY_SERVER_PROFILES)
    profiles: dict[Any, Any] = stored_profiles if isinstance(stored_profiles, dict) else {}
    stored_keys = list(_stored_profile_keys(profiles).values())
    if live_model_ids and not stored_keys:
        return
    for stored_key in stored_keys:
        stored_actions = profiles[stored_key].get(_PROFILE_KEY_PHRASE_ACTIONS)
        actions = dict(stored_actions) if isinstance(stored_actions, Mapping) else {}
        present = {_non_empty_string(model_id) for model_id in actions}
        missing = [model_id for model_id in live_model_ids if model_id not in present]
        if not missing:
            continue
        for model_id in missing:
            actions[model_id] = {"type": ACTION_LIVE_VOICE, "mode": LIVE_MODE_START}
        profiles[stored_key] = {**profiles[stored_key], _PROFILE_KEY_PHRASE_ACTIONS: actions}
    del section[_KEY_RETIRED_MODEL_ACTIONS]


def _writable_profile(section: dict[str, Any], profile_key: str) -> dict[str, Any]:
    """Return the stored profile dict for ``profile_key``, created on demand.

    A profile stored under an alias spelling moves to the canonical key so the
    written values and every later read address the same entry. A new profile
    receives the retired ``model_actions`` that waited for one.
    """
    stored_profiles = section.get(_KEY_SERVER_PROFILES)
    profiles = stored_profiles if isinstance(stored_profiles, dict) else {}
    section[_KEY_SERVER_PROFILES] = profiles
    stored_key = _stored_profile_keys(profiles).get(profile_key)
    profile = dict(profiles[stored_key]) if stored_key is not None else {}
    if stored_key is not None and stored_key != profile_key:
        del profiles[stored_key]
    profiles[profile_key] = profile
    _carry_over_retired_actions(section)
    written: dict[str, Any] = profiles[profile_key]
    return written


def _validated_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise VoiceConfigError(
            f"Voice setting {field_name} must be true or false", field=field_name
        )
    return value


def _validated_microphone(value: object) -> MicrophoneSelection | None:
    if value is None:
        return None
    microphone = _parse_microphone(value)
    if microphone is None:
        raise VoiceConfigError(
            "Voice microphone must be a device descriptor or null",
            field=_CHANGE_MICROPHONE,
        )
    return microphone


def _validated_active_model_ids(value: object, known_model_ids: Callable[[str], bool]) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ACTIVE_PHRASES:
        raise VoiceConfigError(
            f"Choose between 1 and {MAX_ACTIVE_PHRASES} wake phrases",
            field=_CHANGE_ACTIVE_MODEL_IDS,
        )
    model_ids = [
        _validated_model_id(model_id, _CHANGE_ACTIVE_MODEL_IDS, known_model_ids)
        for model_id in value
    ]
    if len(set(model_ids)) != len(model_ids):
        raise VoiceConfigError("Active wake phrases must be unique", field=_CHANGE_ACTIVE_MODEL_IDS)
    return model_ids


def _validated_sensitivities(
    value: object, known_model_ids: Callable[[str], bool]
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise VoiceConfigError(
            "Voice model sensitivities must be an object",
            field=_CHANGE_MODEL_SENSITIVITIES,
        )
    sensitivities: dict[str, float] = {}
    for raw_model_id, raw_sensitivity in value.items():
        model_id = _validated_model_id(raw_model_id, _CHANGE_MODEL_SENSITIVITIES, known_model_ids)
        sensitivity = _sensitivity_value(raw_sensitivity)
        if sensitivity is None:
            raise VoiceConfigError(
                f"Voice sensitivity must be a number between {MIN_SENSITIVITY} "
                f"and {MAX_SENSITIVITY}",
                field=_CHANGE_MODEL_SENSITIVITIES,
            )
        sensitivities[model_id] = sensitivity
    return sensitivities


def _validated_phrase_actions(
    value: object, known_model_ids: Callable[[str], bool]
) -> dict[str, dict[str, Any] | None]:
    if not isinstance(value, Mapping):
        raise VoiceConfigError("Phrase actions must be an object", field=_CHANGE_PHRASE_ACTIONS)
    updates: dict[str, dict[str, Any] | None] = {}
    for raw_model_id, raw_action in value.items():
        if raw_action is None:
            # Removal needs no catalog entry: it also cleans up deleted imports.
            model_id = _non_empty_string(raw_model_id)
            if model_id is None:
                raise VoiceConfigError(
                    "Wake phrase model ids must be non-empty strings",
                    field=_CHANGE_PHRASE_ACTIONS,
                )
            updates[model_id] = None
            continue
        model_id = _validated_model_id(raw_model_id, _CHANGE_PHRASE_ACTIONS, known_model_ids)
        updates[model_id] = _validated_action(raw_action)
    return updates


def _validated_action(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VoiceConfigError(
            "A phrase action must be an object or null", field=_CHANGE_PHRASE_ACTIONS
        )
    action_type = value.get("type")
    if action_type == ACTION_COMMAND:
        _reject_unknown_action_keys(value, {"type", "agent_id", "session_behavior"})
        action: dict[str, Any] = {"type": ACTION_COMMAND}
        agent_id = _validated_agent_id(value.get("agent_id"), _CHANGE_PHRASE_ACTIONS, nullable=True)
        session_behavior = _validated_session_behavior(
            value.get("session_behavior"), _CHANGE_PHRASE_ACTIONS, nullable=True
        )
        if agent_id is not None:
            action["agent_id"] = agent_id
        if session_behavior is not None:
            action["session_behavior"] = session_behavior
        return action
    if action_type == ACTION_LIVE_VOICE:
        _reject_unknown_action_keys(value, {"type", "mode"})
        mode = value.get("mode")
        if mode is None:
            mode = DEFAULT_LIVE_MODE
        if not isinstance(mode, str) or mode not in LIVE_MODES:
            raise VoiceConfigError(
                "Live voice mode must be one of: " + ", ".join(LIVE_MODES),
                field=_CHANGE_PHRASE_ACTIONS,
            )
        return {"type": ACTION_LIVE_VOICE, "mode": mode}
    raise VoiceConfigError(
        "Phrase action type must be one of: " + ", ".join(ACTION_TYPES),
        field=_CHANGE_PHRASE_ACTIONS,
    )


def _reject_unknown_action_keys(action: Mapping[Any, Any], allowed: set[str]) -> None:
    for key in action:
        if key not in allowed:
            raise VoiceConfigError(
                f"Unknown phrase action field: {key}", field=_CHANGE_PHRASE_ACTIONS
            )


def _validated_model_id(
    value: object, field_name: str, known_model_ids: Callable[[str], bool]
) -> str:
    model_id = _non_empty_string(value)
    if model_id is None:
        raise VoiceConfigError("Wake phrase model ids must be non-empty strings", field=field_name)
    if not known_model_ids(model_id):
        raise VoiceConfigError(f"Wakeword model is not available: {model_id}", field=field_name)
    return model_id


def _validated_agent_id(value: object, field_name: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    agent_id = _non_empty_string(value)
    if agent_id is None:
        raise VoiceConfigError("Voice Agent must be a non-empty id or null", field=field_name)
    return agent_id


def _validated_session_behavior(value: object, field_name: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in SESSION_BEHAVIORS:
        raise VoiceConfigError(
            "Voice Session behavior must be one of: " + ", ".join(SESSION_BEHAVIORS),
            field=field_name,
        )
    return value


def _non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _sensitivity_value(value: object) -> float | None:
    """Return a sensitivity within the supported range, else ``None``.

    The range check runs before float conversion, so huge integers and NaN are
    rejected instead of overflowing or slipping through comparisons.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not MIN_SENSITIVITY <= value <= MAX_SENSITIVITY:
        return None
    return float(value)


# -- Status projection ---------------------------------------------------------


def config_status(
    config: VoiceConfig,
    server_url: str,
    labels: Mapping[str, str],
) -> dict[str, Any]:
    """Return the config part of the Voice status snapshot for one server.

    ``labels`` maps model ids to catalog labels (the id is shown when a label
    is missing). Runtime fields - listener state, echo cancellation state,
    per-phrase problems, recording, commands, calibration - are added by the
    owner of the runtime state.
    """
    profile = config.profile_for(server_url)
    return {
        "enabled": config.enabled,
        "microphone": config.microphone.to_dict() if config.microphone is not None else None,
        "echo_cancellation": {"enabled": config.echo_cancellation},
        "default_agent_id": profile.default_agent_id,
        "default_session_behavior": profile.default_session_behavior,
        "phrases": [
            {
                "model_id": phrase.model_id,
                "label": labels.get(phrase.model_id, phrase.model_id),
                "sensitivity": phrase.sensitivity,
                "action": profile.phrase_actions.get(phrase.model_id, CommandAction()).to_dict(),
                "effective": config.effective_action(phrase.model_id, server_url).to_dict(),
            }
            for phrase in config.phrases
        ],
        "limits": voice_limits(),
    }
