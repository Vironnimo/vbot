"""Wakeword model catalog and shared multi-phrase detection engine.

The catalog lists curated downloads and validated Desktop-local imports. Each
descriptor carries its detector ``kind`` (``tflite_head`` today) and the ids of
catalog models it ``overlaps`` with (models trained on overlapping phrases, so
one utterance can trigger both). :class:`MultiWakewordEngine` runs every active
detector over one shared openWakeWord feature stream, confirms threshold
crossings per phrase, re-arms each phrase independently (overlapping phrases
together), and returns at most one winner per audio window.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from core.utils.file_status import is_dir_strict, is_file_strict
from core.utils.model_files import (
    ModelFile,
    ModelFilesCancelledError,
    ModelFilesError,
    PinnedModel,
    fetch_model_files,
)
from desktop.wakeword.config import (
    DEFAULT_MODEL_IDS,
    DEFAULT_SENSITIVITY,
    MAX_ACTIVE_PHRASES,
    MAX_SENSITIVITY,
    MIN_SENSITIVITY,
    PhraseConfig,
)

logger = logging.getLogger("vbot.desktop.wakeword.engine")

MAX_CUSTOM_WAKEWORD_MODEL_BYTES = 20 * 1024 * 1024

# Detector kinds the engine can host over the shared feature stream.
DETECTOR_KIND_TFLITE_HEAD = "tflite_head"

# openWakeWord heads emit raw, unsmoothed per-window scores, and the bundled heads
# differ in shape: okay_nabu sustains ~10 windows above threshold while hey_nabu
# peaks for a single one, so a fixed multi-window rule would drop genuine
# activations. A score clearly above the threshold confirms immediately; a
# marginal crossing must repeat within a short recent-score window so an
# isolated noise spike can never trigger detection on its own.
CONFIDENT_SCORE_MARGIN = 0.15
MARGINAL_CONFIRMATION_CHUNKS = 5

# A sustained head (okay_nabu) can dip below a high threshold and cross it again
# within one utterance, so re-arming on the score alone fires that utterance
# twice. After a phrase fires, it (and every phrase overlapping it) stays
# disarmed for this many 80 ms chunks (2 s), as other openWakeWord hosts do.
REFRACTORY_CHUNKS = 25

_CUSTOM_MODEL_PREFIX = "custom/"
_CUSTOM_MODEL_DIRECTORY_NAME = "wakewords"
_CUSTOM_MODEL_METADATA_SUFFIX = ".json"
_CUSTOM_MODEL_FILE_SUFFIX = ".tflite"
_CUSTOM_MODEL_LABEL_LIMIT = 80
_BUNDLED_HEY_NABU_PATH = Path(__file__).with_name("models") / "hey_nabu_v2.tflite"


@dataclass(frozen=True)
class WakewordModelDescriptor:
    """One Desktop-local wakeword model available for selection.

    ``kind`` selects the detector implementation; ``overlaps`` lists catalog
    model ids trained on overlapping phrases, so one utterance may trigger both
    when they are active together.
    """

    id: str
    label: str
    source: str
    format: str
    removable: bool
    target: str
    builtin: bool = False
    kind: str = DETECTOR_KIND_TFLITE_HEAD
    overlaps: tuple[str, ...] = ()
    installed: bool = True
    bundled: bool = False
    size_bytes: int | None = None
    language: str | None = None
    license: str | None = None
    homepage: str | None = None
    community: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the public descriptor without exposing a local filesystem path."""
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "format": self.format,
            "removable": self.removable,
            "kind": self.kind,
            "overlaps": list(self.overlaps),
            "installed": self.installed,
            "bundled": self.bundled,
            "size_bytes": self.size_bytes,
            "language": self.language,
            "license": self.license,
            "homepage": self.homepage,
            "community": self.community,
        }


@dataclass(frozen=True)
class WakewordMatch:
    """The single winning detector result for one audio window."""

    model_id: str
    score: float
    threshold: float


# Pinned assets, reviewed with the app; the page can select ids, never URLs.
_CATALOG_ENTRIES = json.loads(
    Path(__file__).with_name("models").joinpath("catalog.json").read_text(encoding="utf-8")
)
_CATALOG_BY_ID = {entry["id"]: entry for entry in _CATALOG_ENTRIES}


class WakewordModelError(ValueError):
    """A stable, user-actionable wakeword model catalog failure."""

    def __init__(self, message: str, *, error_code: str = "wakeword_model_invalid") -> None:
        super().__init__(message)
        self.error_code = error_code


class WakewordModelCatalog:
    """Own pinned catalog downloads and durable Desktop-local TFLite imports."""

    def __init__(
        self, settings_file: Path | None = None, *, versions_directory: Path | None = None
    ) -> None:
        if settings_file is None:
            from desktop.settings import settings_path

            settings_file = settings_path()
        self._model_directory = settings_file.parent / _CUSTOM_MODEL_DIRECTORY_NAME
        self._download_lock = threading.Lock()
        self._versions_directory = versions_directory

    def adopt_existing_models(self, model_ids: Iterable[str]) -> None:
        """Preserve selected legacy heads locally before their old runtime is retired."""
        for model_id in model_ids:
            if model_id not in _CATALOG_BY_ID or not model_id.startswith("builtin/"):
                continue
            try:
                with self._download_lock:
                    self._install_model(
                        model_id, lambda _done: None, threading.Event(), offline=True
                    )
            except WakewordModelError:
                logger.info("Selected wakeword needs a download (model=%s)", model_id)

    def model_labels(self) -> dict[str, str]:
        """Return selectable ids and labels without inspecting catalog binaries."""
        labels = {entry["id"]: entry["label"] for entry in _CATALOG_ENTRIES}
        labels.update((model.id, model.label) for model in self._custom_models())
        return labels

    def list_models(self) -> list[WakewordModelDescriptor]:
        """Return catalog entries followed by valid imported models."""
        custom_models = sorted(
            self._custom_models(), key=lambda model: (model.label.casefold(), model.id)
        )
        return [*(self._catalog_descriptor(entry) for entry in _CATALOG_ENTRIES), *custom_models]

    def resolve(self, model_id: str) -> WakewordModelDescriptor:
        """Resolve a public model id to its executable target."""
        entry = _CATALOG_BY_ID.get(model_id)
        if entry is not None:
            return self._catalog_descriptor(entry)
        custom = next((model for model in self._custom_models() if model.id == model_id), None)
        if custom is not None:
            return custom
        raise WakewordModelError(
            f"Wakeword model is not available: {model_id}",
            error_code="wakeword_model_unavailable",
        )

    def _catalog_descriptor(self, entry: dict[str, Any]) -> WakewordModelDescriptor:
        path = (
            _BUNDLED_HEY_NABU_PATH
            if entry["bundled"]
            else self._model_directory / "catalog" / f"{entry['sha256']}.tflite"
        )
        return WakewordModelDescriptor(
            id=entry["id"],
            label=entry["label"],
            source="catalog",
            format="tflite",
            removable=False,
            target=str(path),
            overlaps=tuple(entry["overlaps"]),
            installed=is_file_strict(path) and _model_digest(path) == entry["sha256"],
            bundled=entry["bundled"],
            size_bytes=entry["size_bytes"],
            language=entry["language"],
            license=entry["license"],
            homepage=entry["homepage"],
            community=entry["community"],
        )

    def install_model(
        self,
        model_id: str,
        *,
        progress: Callable[[int], None] = lambda _done: None,
        cancelled: threading.Event | None = None,
    ) -> WakewordModelDescriptor:
        """Fetch through the shared model downloader, then validate and select locally."""
        with self._download_lock:
            return self._install_model(model_id, progress, cancelled or threading.Event())

    def _install_model(
        self,
        model_id: str,
        progress: Callable[[int], None],
        cancelled: threading.Event,
        *,
        offline: bool = False,
    ) -> WakewordModelDescriptor:
        descriptor = self.resolve(model_id)
        if descriptor.installed or descriptor.source == "imported":
            return descriptor
        if descriptor.bundled:
            raise WakewordModelError(
                "The bundled wakeword is missing", error_code="wakeword_model_unavailable"
            )
        entry = _CATALOG_BY_ID[model_id]
        target = Path(descriptor.target)
        filename = unquote(urlsplit(entry["url"]).path.rsplit("/", 1)[-1])
        staging = target.parent / ".downloads" / entry["sha256"]
        model = PinnedModel(
            repo=model_id,
            revision=entry["sha256"],
            files=(ModelFile(filename, entry["sha256"], entry["size_bytes"], url=entry["url"]),),
        )
        try:
            previous = (
                tuple(
                    self._versions_directory.glob(
                        "*/runtime/Lib/site-packages/pyopen_wakeword/models"
                    )
                )
                if self._versions_directory is not None
                else ()
            )
            fetch_model_files(
                model,
                staging,
                progress=progress,
                cancelled=cancelled,
                reuse=(*_legacy_model_directories(model_id), *previous),
                client_factory=_download_client,
                offline=offline,
            )
            candidate = staging / filename
            if not offline:
                _validate_custom_model(candidate)
            if cancelled.is_set():
                raise ModelFilesCancelledError
            candidate.replace(target)
        except ModelFilesCancelledError as exc:
            raise WakewordModelError(
                "Wakeword download cancelled", error_code="wakeword_model_download_cancelled"
            ) from exc
        except ModelFilesError as exc:
            code = (
                "wakeword_model_insufficient_space"
                if exc.code == "insufficient_space"
                else "wakeword_model_download_failed"
            )
            raise WakewordModelError(
                "Wakeword model could not be downloaded", error_code=code
            ) from exc
        except WakewordModelError:
            raise
        except Exception as exc:
            raise WakewordModelError(
                "Wakeword model could not be downloaded. Try again.",
                error_code="wakeword_model_download_failed",
            ) from exc
        # The model is installed. A busy staging directory can be reused later.
        _remove_file(staging / f"{filename}.part")
        try:
            staging.rmdir()
        except OSError:
            logger.debug("Wakeword staging directory cleanup deferred (model=%s)", model_id)
        logger.info("Wakeword model installed (model=%s)", model_id)
        return replace(descriptor, installed=True)

    def create_engine(
        self,
        phrases: Sequence[PhraseConfig],
        *,
        score_listener: Callable[[dict[str, float]], None] | None = None,
    ) -> MultiWakewordEngine:
        """Create one shared-feature detector for 1 to ``MAX_ACTIVE_PHRASES`` phrases.

        ``phrases`` are :class:`PhraseConfig` entries with unique model ids.
        ``score_listener`` receives every window's raw per-phrase scores.
        """
        configured = tuple(phrases)
        if not all(isinstance(phrase, PhraseConfig) for phrase in configured):
            raise WakewordModelError("Wake phrases must be phrase configurations")
        if not 1 <= len(configured) <= MAX_ACTIVE_PHRASES:
            raise WakewordModelError(f"Choose between 1 and {MAX_ACTIVE_PHRASES} wake phrases")
        model_ids = tuple(phrase.model_id for phrase in configured)
        if len(set(model_ids)) != len(model_ids):
            raise WakewordModelError("Active wakeword models must be unique")
        descriptors = tuple(self.resolve(model_id) for model_id in model_ids)
        return MultiWakewordEngine(
            descriptors,
            {phrase.model_id: phrase.sensitivity for phrase in configured},
            score_listener=score_listener,
        )

    def import_model(self, filename: str, content: bytes) -> WakewordModelDescriptor:
        """Validate and persist one user-supplied TFLite wakeword model."""
        original_name = _safe_original_filename(filename)
        if Path(original_name).suffix.casefold() != _CUSTOM_MODEL_FILE_SUFFIX:
            raise WakewordModelError("Wakeword model must be a TFLite file")
        if not content:
            raise WakewordModelError("Wakeword model file is empty")
        if len(content) > MAX_CUSTOM_WAKEWORD_MODEL_BYTES:
            raise WakewordModelError(
                f"Wakeword model exceeds {MAX_CUSTOM_WAKEWORD_MODEL_BYTES} bytes"
            )

        self._model_directory.mkdir(parents=True, exist_ok=True)
        model_token = uuid4().hex
        model_id = f"{_CUSTOM_MODEL_PREFIX}{model_token}"
        model_path = self._model_directory / f"{model_token}{_CUSTOM_MODEL_FILE_SUFFIX}"
        metadata_path = self._metadata_path(model_token)

        try:
            # Leaving the block removes the temporary file unless it became the model.
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=self._model_directory,
                delete_on_close=False,
                prefix=f".{model_token}.",
                suffix=_CUSTOM_MODEL_FILE_SUFFIX,
            ) as temporary_file:
                temporary_file.write(content)
                temporary_file.close()
                temporary_path = Path(temporary_file.name)
                _validate_custom_model(temporary_path)
                temporary_path.replace(model_path)
            metadata = {
                "id": model_id,
                "label": _display_label(original_name),
                "filename": model_path.name,
                "format": "tflite",
                "source": "imported",
            }
            _write_json_atomic(metadata_path, metadata)
        except WakewordModelError:
            _remove_file(model_path)
            _remove_file(metadata_path)
            raise
        except Exception as exc:
            _remove_file(model_path)
            _remove_file(metadata_path)
            raise WakewordModelError("Wakeword model could not be imported") from exc

        return WakewordModelDescriptor(
            id=model_id,
            label=str(metadata["label"]),
            source="imported",
            format="tflite",
            removable=True,
            target=str(model_path),
        )

    def delete_model(self, model_id: str) -> None:
        """Permanently remove one imported model and its metadata."""
        descriptor = self.resolve(model_id)
        if not descriptor.removable:
            raise WakewordModelError(
                "Built-in wakeword models cannot be removed",
                error_code="wakeword_model_delete_failed",
            )
        model_token = model_id.removeprefix(_CUSTOM_MODEL_PREFIX)
        model_path = self._model_directory / f"{model_token}{_CUSTOM_MODEL_FILE_SUFFIX}"
        metadata_path = self._metadata_path(model_token)
        try:
            model_path.unlink()
            metadata_path.unlink(missing_ok=True)
        except OSError as exc:
            raise WakewordModelError(
                "Wakeword model could not be removed",
                error_code="wakeword_model_delete_failed",
            ) from exc

    def _custom_models(self) -> list[WakewordModelDescriptor]:
        if not is_dir_strict(self._model_directory):
            return []
        models: list[WakewordModelDescriptor] = []
        for metadata_path in self._model_directory.glob(f"*{_CUSTOM_MODEL_METADATA_SUFFIX}"):
            descriptor = self._read_custom_descriptor(metadata_path)
            if descriptor is not None:
                models.append(descriptor)
        return models

    def _read_custom_descriptor(self, metadata_path: Path) -> WakewordModelDescriptor | None:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except OSError, UnicodeError, json.JSONDecodeError:
            logger.warning("Ignoring unreadable wakeword model metadata: %s", metadata_path.name)
            return None
        if not isinstance(metadata, dict):
            return None
        model_id = metadata.get("id")
        label = metadata.get("label")
        filename = metadata.get("filename")
        if (
            not isinstance(model_id, str)
            or not isinstance(label, str)
            or not isinstance(filename, str)
        ):
            return None
        model_token = model_id.removeprefix(_CUSTOM_MODEL_PREFIX)
        expected_filename = f"{model_token}{_CUSTOM_MODEL_FILE_SUFFIX}"
        if (
            not model_id.startswith(_CUSTOM_MODEL_PREFIX)
            or len(model_token) != 32
            or any(character not in "0123456789abcdef" for character in model_token)
            or filename != expected_filename
            or metadata.get("format") != "tflite"
            or metadata_path.name != f"{model_token}{_CUSTOM_MODEL_METADATA_SUFFIX}"
        ):
            return None
        model_path = self._model_directory / expected_filename
        if not is_file_strict(model_path):
            return None
        return WakewordModelDescriptor(
            id=model_id,
            label=label,
            source="imported",
            format="tflite",
            removable=True,
            target=str(model_path),
        )

    def _metadata_path(self, model_token: str) -> Path:
        return self._model_directory / f"{model_token}{_CUSTOM_MODEL_METADATA_SUFFIX}"


class WakewordEngine(Protocol):
    """Abstract interface for wakeword detection engines."""

    def start(self) -> None:
        """Initialize the engine."""
        ...

    def stop(self) -> None:
        """Release native inference resources."""
        ...

    def reset(self) -> None:
        """Forget audio and detection history after a capture gap without reloading models."""
        ...

    def detect(self, audio_chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
        """Return at most one winning wakeword match for an audio chunk."""
        ...


class MockWakewordEngine:
    """Configurable structured-match engine for UI and worker tests."""

    def __init__(
        self,
        score_sequence: list[float] | None = None,
        *,
        model_id: str = DEFAULT_MODEL_IDS[0],
        sensitivity: float = DEFAULT_SENSITIVITY,
        score_listener: Callable[[dict[str, float]], None] | None = None,
    ) -> None:
        self._score_sequence = score_sequence or [0.0]
        self._model_id = model_id
        self._threshold = _threshold_for_sensitivity(sensitivity)
        self._score_listener = score_listener
        self._index = 0
        self._running = False

    def start(self) -> None:
        self._running = True
        self._index = 0

    def stop(self) -> None:
        self._running = False

    def reset(self) -> None:
        self._index = 0

    def detect(self, audio_chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
        """Return a match when the next configured score reaches the threshold."""
        if not self._running:
            return None
        score = _clamp_score(self._score_sequence[self._index % len(self._score_sequence)])
        self._index += 1
        if self._score_listener is not None:
            self._score_listener({self._model_id: score})
        if score < self._threshold:
            return None
        return WakewordMatch(self._model_id, score, self._threshold)

    def set_score_sequence(self, sequence: list[float]) -> None:
        """Replace the score sequence and reset the index."""
        self._score_sequence = list(sequence)
        self._index = 0


class MultiWakewordEngine:
    """Run several wakeword detectors over one shared feature stream.

    Each phrase confirms its own threshold crossings and keeps its own arm
    state: a phrase that fired re-arms only once its raw score falls below its
    threshold after :data:`REFRACTORY_CHUNKS` chunks have passed, while the
    other phrases stay armed. Active phrases whose models
    ``overlap`` share one arm state instead, because one utterance drives both
    detectors in consecutive windows: after either fires, neither fires again
    until a window in which all of them are below their thresholds. Among
    armed, confirmed phrases of one window the highest score-to-threshold
    ratio wins, so independently tuned phrases stay comparable.
    """

    def __init__(
        self,
        descriptors: tuple[WakewordModelDescriptor, ...],
        model_sensitivities: Mapping[str, float],
        *,
        score_listener: Callable[[dict[str, float]], None] | None = None,
    ) -> None:
        if not descriptors:
            raise WakewordModelError("At least one wakeword model is required")
        self._descriptors = descriptors
        self._thresholds = {
            descriptor.id: _threshold_for_sensitivity(
                model_sensitivities.get(descriptor.id, DEFAULT_SENSITIVITY)
            )
            for descriptor in descriptors
        }
        self._models: list[tuple[WakewordModelDescriptor, Any]] = []
        self._features: Any = None
        self._arm_groups = _arm_groups(descriptors)
        self._armed: dict[str, bool] = {}
        self._chunk = 0
        self._rearm_at: dict[str, int] = {}
        self._arm_all()
        self._score_listener = score_listener
        self._recent_scores: dict[str, deque[float]] = {}
        self._reset_recent_scores()

    @property
    def active_model_ids(self) -> tuple[str, ...]:
        """Ordered public model IDs served by this engine."""
        return tuple(descriptor.id for descriptor in self._descriptors)

    @property
    def thresholds(self) -> dict[str, float]:
        """Return an isolated per-model threshold map."""
        return dict(self._thresholds)

    def start(self) -> None:
        """Load all detector heads and their one shared feature extractor."""
        self.stop()
        features: Any = None
        models: list[tuple[WakewordModelDescriptor, Any]] = []
        try:
            features = _create_feature_stream()
            for descriptor in self._descriptors:
                models.append((descriptor, _create_detector(descriptor)))
        except Exception:
            _close_models(models)
            if features is not None:
                features.close()
            raise
        self._features = features
        self._models = models
        self._arm_all()
        self._reset_recent_scores()

    def stop(self) -> None:
        """Release all native TensorFlow Lite resources."""
        models = self._models
        features = self._features
        self._models = []
        self._features = None
        self._arm_all()
        self._reset_recent_scores()
        try:
            _close_models(models)
        finally:
            if features is not None:
                features.close()

    def detect(self, audio_chunk: bytes, *, speech_present: bool = True) -> WakewordMatch | None:
        """Return the strongest confirmed, armed phrase match for one chunk.

        ``speech_present=False`` zeroes this chunk's scores before they can enter
        confirmation or reach the score listener, so idle room noise cannot
        accumulate toward a detection and calibration sees the same gated scores.
        The caller decides it; the detection loop's speech gate asks whether the
        chunks 4 to 6 before this one carried speech, as upstream openWakeWord's
        VAD threshold does. Re-arming uses the raw scores: a gate that closes
        during a short pause while the score is still high must not count as
        the phrase having ended, or the same utterance fires twice.
        """
        if self._features is None or not self._models:
            return None
        self._chunk += 1
        embeddings = self._features.process(audio_chunk)
        best_match: WakewordMatch | None = None
        best_ratio = 0.0
        scores: dict[str, float] = {}
        groups_below_threshold = dict.fromkeys(self._armed, True)
        for descriptor, model in self._models:
            raw_score = max(map(_clamp_score, model.scores(embeddings)), default=0.0)
            threshold = self._thresholds[descriptor.id]
            group = self._arm_groups[descriptor.id]
            if raw_score >= threshold:
                groups_below_threshold[group] = False
            score = raw_score if speech_present else 0.0
            scores[descriptor.id] = score
            self._recent_scores[descriptor.id].append(score)
            if score < threshold:
                continue
            if not self._armed[group]:
                # Still above threshold since this phrase (or an overlapping one) fired.
                continue
            if not score_is_confirmed(self._recent_scores[descriptor.id], score, threshold):
                continue
            ratio = score / threshold
            if best_match is None or ratio > best_ratio:
                best_match = WakewordMatch(descriptor.id, score, threshold)
                best_ratio = ratio
        if self._score_listener is not None:
            self._score_listener(scores)
        for group, below_threshold in groups_below_threshold.items():
            if (
                not self._armed[group]
                and below_threshold
                and self._chunk >= self._rearm_at.get(group, 0)
            ):
                self._armed[group] = True
                for model_id, arm_group in self._arm_groups.items():
                    if arm_group == group:
                        self._recent_scores[model_id].clear()
        if best_match is not None:
            group = self._arm_groups[best_match.model_id]
            self._armed[group] = False
            self._rearm_at[group] = self._chunk + REFRACTORY_CHUNKS
        return best_match

    def reset(self) -> None:
        """Start a fresh audio stream, retaining the loaded native models."""
        if self._features is not None:
            self._features.reset()
        for _descriptor, model in self._models:
            model.reset()
        self._arm_all()
        self._reset_recent_scores()

    def _arm_all(self) -> None:
        self._armed = dict.fromkeys(self._arm_groups.values(), True)
        self._rearm_at = {}

    def _reset_recent_scores(self) -> None:
        self._recent_scores = {
            descriptor.id: deque(
                (0.0 for _ in range(MARGINAL_CONFIRMATION_CHUNKS)),
                maxlen=MARGINAL_CONFIRMATION_CHUNKS,
            )
            for descriptor in self._descriptors
        }


def _arm_groups(descriptors: tuple[WakewordModelDescriptor, ...]) -> dict[str, str]:
    """Map each active model id to the id that represents its arm group.

    Models join one group when either lists the other in ``overlaps``
    (transitively); models without an active overlap form their own group.
    """
    active_ids = [descriptor.id for descriptor in descriptors]
    group_of = {model_id: model_id for model_id in active_ids}

    def root(model_id: str) -> str:
        while group_of[model_id] != model_id:
            model_id = group_of[model_id]
        return model_id

    for descriptor in descriptors:
        for overlapping_id in descriptor.overlaps:
            if overlapping_id in group_of:
                first, second = sorted((root(descriptor.id), root(overlapping_id)))
                group_of[second] = first
    return {model_id: root(model_id) for model_id in active_ids}


def _create_detector(descriptor: WakewordModelDescriptor) -> Any:
    """Load the detector implementation that serves one descriptor's kind."""
    if descriptor.kind == DETECTOR_KIND_TFLITE_HEAD:
        return _create_phrase_head(descriptor)
    raise WakewordModelError(
        f"Wakeword detector kind is not supported: {descriptor.kind}",
        error_code="wakeword_model_unavailable",
    )


def _create_feature_stream() -> Any:
    from desktop.wakeword._openwakeword import FeatureStream

    return FeatureStream()


def _create_phrase_head(descriptor: WakewordModelDescriptor) -> Any:
    if descriptor.source == "catalog" and not descriptor.installed:
        raise WakewordModelError(
            f"Wakeword model needs a verified download: {descriptor.label}",
            error_code="wakeword_model_unavailable",
        )
    from desktop.wakeword._openwakeword import PhraseHead, builtin_model_path

    try:
        if descriptor.builtin:
            return PhraseHead(builtin_model_path(descriptor.target))
        return PhraseHead(Path(descriptor.target))
    except (OSError, RuntimeError, ValueError) as exc:
        raise WakewordModelError(
            f"Wakeword model is not loadable: {descriptor.label}",
            error_code="wakeword_model_unavailable",
        ) from exc


def _validate_custom_model(model_path: Path) -> None:
    descriptor = WakewordModelDescriptor(
        id="custom/validation",
        label=model_path.stem,
        source="imported",
        format="tflite",
        removable=True,
        target=str(model_path),
    )
    try:
        model = _create_phrase_head(descriptor)
    except Exception as exc:
        raise WakewordModelError(
            "The selected file is not a compatible TFLite wakeword model"
        ) from exc
    model.close()


def _threshold_for_sensitivity(sensitivity: float) -> float:
    normalized_sensitivity = max(MIN_SENSITIVITY, min(MAX_SENSITIVITY, float(sensitivity)))
    return 1.0 - normalized_sensitivity


def score_is_confirmed(recent_scores: Iterable[float], score: float, threshold: float) -> bool:
    """Whether a crossing confirms, given the recent window including this score.

    Detection and calibration use the same rule: one confident score or two
    crossings within ``MARGINAL_CONFIRMATION_CHUNKS``.
    """
    if score >= threshold + CONFIDENT_SCORE_MARGIN:
        return True
    crossings = sum(1 for recent_score in recent_scores if recent_score >= threshold)
    return crossings >= 2


def _clamp_score(score: float) -> float:
    value = float(score)
    return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0


def _close_models(models: list[tuple[WakewordModelDescriptor, Any]]) -> None:
    for _descriptor, model in reversed(models):
        try:
            model.close()
        except Exception:
            logger.warning("Failed to close wakeword model", exc_info=True)


def _safe_original_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise WakewordModelError("Wakeword model filename is required")
    normalized = filename.strip().replace("\\", "/")
    original_name = normalized.rsplit("/", 1)[-1]
    if original_name in {"", ".", ".."}:
        raise WakewordModelError("Wakeword model filename is invalid")
    return original_name


def _display_label(filename: str) -> str:
    stem = Path(filename).stem
    label = " ".join(stem.replace("_", " ").replace("-", " ").split()).strip()
    if not label:
        raise WakewordModelError("Wakeword model filename must contain a name")
    return label[:_CUSTOM_MODEL_LABEL_LIMIT]


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    # Leaving the block removes the temporary file unless it replaced *path*.
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete_on_close=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as temporary_file:
        json.dump(value, temporary_file, indent=2, sort_keys=True)
        temporary_file.write("\n")
        temporary_file.close()
        Path(temporary_file.name).replace(path)


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to clean up wakeword model file: %s", os.fspath(path))


def _model_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _legacy_model_directories(model_id: str) -> tuple[Path, ...]:
    """Let the shared downloader adopt a matching head from an older runtime."""
    if not model_id.startswith("builtin/"):
        return ()
    from importlib.util import find_spec

    try:
        spec = find_spec("pyopen_wakeword")
        return (
            tuple(Path(location) / "models" for location in (spec.submodule_search_locations or ()))
            if spec
            else ()
        )
    except ImportError, ValueError:
        return ()


def _download_client() -> Any:
    import httpx

    from desktop._http import shared_ssl_context

    return httpx.Client(
        trust_env=False, verify=shared_ssl_context(), timeout=15, follow_redirects=False
    )
