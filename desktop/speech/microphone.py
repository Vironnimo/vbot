"""The Desktop's shared microphone: the stored choice, the device list, and captures.

Every Desktop speech feature (Voice wake phrases and commands, dictation)
records through the microphone this module describes. It owns:

- the stored ``microphone`` settings section (tolerant parsing into an
  immutable :class:`MicrophoneSettings`, strict validation of one partial
  change in :func:`apply_microphone_changes`);
- the named, idempotent migration :func:`migrate_wakeword_microphone`, which
  moves the choice out of the ``wakeword`` section where earlier versions
  stored it;
- :class:`MicrophoneService`, the process-wide owner the features share: the
  current settings with change listeners, the device list, the echo stage
  pool, and new :class:`~desktop.speech.capture.AudioCapture` instances built
  from the current settings.

Stored shape (every field optional)::

    {
      "device": {"index": 4, "name": "Studio mic", "host_api": "Windows WASAPI"},
      "echo_cancellation": true
    }

``device`` ``null`` (or missing) is the system default input. Writers keep
unknown keys.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from desktop import settings as desktop_settings

if TYPE_CHECKING:
    from desktop.speech.capture import AudioCapture, CaptureStatus, EchoStageFactory, EchoStagePool

logger = logging.getLogger("vbot.desktop.speech.microphone")

ERROR_MICROPHONE_CONFIG_INVALID = "microphone_config_invalid"

_KEY_DEVICE = "device"
_KEY_ECHO_CANCELLATION = "echo_cancellation"
_CHANGE_KEYS = frozenset((_KEY_DEVICE, _KEY_ECHO_CANCELLATION))
# Where versions before the shared microphone stored the choice.
_WAKEWORD_MICROPHONE_KEY = "microphone"
_WAKEWORD_ECHO_KEY = "echo_cancellation"

_DEFAULT_RECONNECT_INTERVAL_SECONDS = 30.0


class MicrophoneConfigError(ValueError):
    """An invalid microphone change; ``field`` names the offending key."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.error_code = ERROR_MICROPHONE_CONFIG_INVALID


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
class MicrophoneSettings:
    """The shared microphone choice: input device and echo cancellation."""

    device: MicrophoneSelection | None = None
    echo_cancellation: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return the ``{device, echo_cancellation}`` form (stored and reported)."""
        return {
            _KEY_DEVICE: self.device.to_dict() if self.device is not None else None,
            _KEY_ECHO_CANCELLATION: self.echo_cancellation,
        }


def parse_microphone_selection(value: object) -> MicrophoneSelection | None:
    """Return the device of a stored descriptor, or ``None`` when malformed."""
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


def parse_microphone_settings(raw: object) -> MicrophoneSettings:
    """Build :class:`MicrophoneSettings` from the stored section, never raising.

    Each malformed field falls back to its default on its own: the system
    default input and echo cancellation on.
    """
    section = raw if isinstance(raw, Mapping) else {}
    echo_cancellation = section.get(_KEY_ECHO_CANCELLATION)
    return MicrophoneSettings(
        device=parse_microphone_selection(section.get(_KEY_DEVICE)),
        echo_cancellation=echo_cancellation if isinstance(echo_cancellation, bool) else True,
    )


def apply_microphone_changes(raw: object, changes: object) -> dict[str, Any]:
    """Validate one partial change and return the new stored section.

    ``changes`` keys: ``device`` (a ``{index, name, host_api}`` descriptor or
    ``null`` for the system default) and ``echo_cancellation`` (bool). Any
    invalid part rejects the whole change with :class:`MicrophoneConfigError`;
    ``raw`` is never mutated and unknown stored keys are kept.
    """
    if not isinstance(changes, Mapping):
        raise MicrophoneConfigError("Microphone changes must be an object")
    for key in changes:
        if key not in _CHANGE_KEYS:
            raise MicrophoneConfigError(f"Unknown microphone setting: {key}", field=str(key))
    section = dict(raw) if isinstance(raw, Mapping) else {}
    if _KEY_DEVICE in changes:
        value = changes[_KEY_DEVICE]
        device = parse_microphone_selection(value) if value is not None else None
        if value is not None and device is None:
            raise MicrophoneConfigError(
                "The microphone must be a device descriptor or null", field=_KEY_DEVICE
            )
        section[_KEY_DEVICE] = device.to_dict() if device is not None else None
    if _KEY_ECHO_CANCELLATION in changes:
        value = changes[_KEY_ECHO_CANCELLATION]
        if not isinstance(value, bool):
            raise MicrophoneConfigError(
                "Echo cancellation must be true or false", field=_KEY_ECHO_CANCELLATION
            )
        section[_KEY_ECHO_CANCELLATION] = value
    return section


def migrate_wakeword_microphone(path: Path | None) -> bool:
    """Move the microphone choice out of the ``wakeword`` section (idempotent).

    Versions before the shared microphone stored ``microphone`` and
    ``echo_cancellation`` inside ``wakeword``. When the ``microphone`` section
    does not exist yet, both values move into it (``microphone`` becomes
    ``device``); in every case the old keys leave ``wakeword``. One settings
    transaction, so a crash never leaves the choice in both places. Returns
    whether the file changed; an unreadable settings file is left alone.
    """

    keys = (desktop_settings.MICROPHONE_KEY, desktop_settings.WAKEWORD_KEY)

    def mutate(sections: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        microphone, wakeword = sections[keys[0]], sections[keys[1]]
        if _WAKEWORD_MICROPHONE_KEY not in wakeword and _WAKEWORD_ECHO_KEY not in wakeword:
            return sections
        old_device = wakeword.pop(_WAKEWORD_MICROPHONE_KEY, None)
        old_echo = wakeword.pop(_WAKEWORD_ECHO_KEY, None)
        if not microphone:
            device = parse_microphone_selection(old_device)
            microphone = {_KEY_DEVICE: device.to_dict() if device is not None else None}
            if isinstance(old_echo, bool):
                microphone[_KEY_ECHO_CANCELLATION] = old_echo
        return {keys[0]: microphone, keys[1]: wakeword}

    try:
        before = desktop_settings.read_settings(path)
        desktop_settings.update_sections(keys, mutate, path)
    except (OSError, ValueError) as exc:
        logger.warning("Microphone settings could not be migrated: %s", exc)
        return False
    migrated = before != desktop_settings.read_settings(path)
    if migrated:
        logger.info("Moved the microphone choice out of the wakeword settings")
    return migrated


MicrophoneListener = Callable[[MicrophoneSettings], None]


class MicrophoneService:
    """The process-wide microphone every Desktop speech feature records through.

    Construction runs :func:`migrate_wakeword_microphone` and reads the
    settings, so build it before any consumer reads its own section. Listeners
    added with :meth:`add_listener` hear every effective change after it was
    persisted, on the caller's thread, and must not block. ``audio_backend``
    (the sounddevice module), ``echo_stage_factory``, ``echo_stage_wait`` and
    ``reconnect_interval`` are test seams; ``None`` uses the real one or the
    capture default.
    """

    def __init__(
        self,
        *,
        settings_path: Path | None,
        audio_backend: Any = None,
        echo_stage_factory: EchoStageFactory | None = None,
        echo_stage_wait: float | None = None,
        reconnect_interval: float = _DEFAULT_RECONNECT_INTERVAL_SECONDS,
    ) -> None:
        self._settings_path = settings_path
        self._audio_backend = audio_backend
        self._echo_stage_factory = echo_stage_factory
        self._echo_stage_wait = echo_stage_wait
        self._reconnect_interval = reconnect_interval
        self._mutation_lock = threading.Lock()
        self._lock = threading.Lock()
        self._listeners: list[MicrophoneListener] = []
        self._echo_stages: EchoStagePool | None = None
        migrate_wakeword_microphone(settings_path)
        self._settings = parse_microphone_settings(
            desktop_settings.read_section(desktop_settings.MICROPHONE_KEY, settings_path)
        )

    @property
    def settings(self) -> MicrophoneSettings:
        """The current microphone settings."""
        with self._lock:
            return self._settings

    def status(self) -> dict[str, Any]:
        """Return ``{device, echo_cancellation}`` for the WebUI."""
        return self.settings.to_dict()

    def add_listener(self, listener: MicrophoneListener) -> None:
        """Hear every effective settings change (after it was persisted)."""
        with self._lock:
            self._listeners.append(listener)

    def update(self, changes: object) -> dict[str, Any]:
        """Validate, persist and apply one partial change; returns the status.

        Raises :class:`MicrophoneConfigError` for an invalid change, before
        anything is written.
        """
        with self._mutation_lock:
            section = desktop_settings.update_section(
                desktop_settings.MICROPHONE_KEY,
                lambda raw: apply_microphone_changes(raw, changes),
                self._settings_path,
            )
            updated = parse_microphone_settings(section)
            with self._lock:
                previous, self._settings = self._settings, updated
                listeners = list(self._listeners)
            if updated != previous:
                logger.info(
                    "Microphone changed (device=%s, echo_cancellation=%s)",
                    updated.device.name if updated.device is not None else "system default",
                    updated.echo_cancellation,
                )
                for listener in listeners:
                    try:
                        listener(updated)
                    except Exception:
                        logger.exception("Microphone change listener failed")
        return updated.to_dict()

    def list_devices(self) -> list[dict[str, Any]]:
        """Return the shared-mode input devices and whether speech input can use them.

        PortAudio keeps the device list of its last initialization, so the list
        is refreshed first; the refresh is skipped while an input stream is
        open (it would invalidate the stream).
        """
        from desktop.speech._microphones import list_microphones

        self.refresh_devices()
        return list_microphones(self._audio_backend)

    def refresh_devices(self) -> bool:
        """Re-initialize PortAudio unless an input stream is open; ``True`` when done."""
        from desktop.speech._microphones import refresh_microphone_devices

        return refresh_microphone_devices(self._audio_backend)

    def prepare_echo(self) -> None:
        """Start creating the echo canceller in the background when it is enabled."""
        if self.settings.echo_cancellation:
            self._echo_stage_pool().prepare()

    def create_capture(
        self,
        *,
        on_status: Callable[[CaptureStatus], None],
        stop_event: threading.Event,
    ) -> AudioCapture:
        """Return a new, not yet started capture of the current microphone settings.

        ``stop_event`` ends it; see :class:`~desktop.speech.capture.AudioCapture`.
        """
        from desktop.speech.capture import ECHO_STAGE_WAIT_SECONDS, AudioCapture

        settings = self.settings
        echo_stages = self._echo_stage_pool()
        if settings.echo_cancellation:
            echo_stages.prepare()  # the canceller loads while the consumer gets ready
        return AudioCapture(
            microphone=settings.device,
            echo_cancellation=settings.echo_cancellation,
            echo_stages=echo_stages,
            on_status=on_status,
            stop_event=stop_event,
            backend=self._audio_backend,
            reconnect_interval=self._reconnect_interval,
            echo_stage_wait=(
                ECHO_STAGE_WAIT_SECONDS if self._echo_stage_wait is None else self._echo_stage_wait
            ),
        )

    def _echo_stage_pool(self) -> EchoStagePool:
        """The process-wide echo stage pool, created on first use."""
        from desktop.speech.capture import EchoStagePool

        with self._lock:
            if self._echo_stages is None:
                self._echo_stages = EchoStagePool(self._echo_stage_factory or _create_echo_stage)
            return self._echo_stages


def _create_echo_stage() -> Any:
    # Loads the WebRTC library; only reached when echo cancellation is enabled.
    from desktop.speech.echo import create_echo_stage

    return create_echo_stage()
