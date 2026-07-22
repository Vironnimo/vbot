"""Wakeword model catalog and shared TFLite inference engine."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from desktop.settings import DEFAULT_WAKEWORD_MODEL_IDS

logger = logging.getLogger("vbot.desktop.wakeword.engine")

DEFAULT_WAKEWORD_SENSITIVITY = 0.5
MIN_WAKEWORD_SENSITIVITY = 0.05
MAX_WAKEWORD_SENSITIVITY = 0.95
MAX_ACTIVE_WAKEWORD_MODELS = 2
MAX_CUSTOM_WAKEWORD_MODEL_BYTES = 20 * 1024 * 1024

_CUSTOM_MODEL_PREFIX = "custom/"
_CUSTOM_MODEL_DIRECTORY_NAME = "wakewords"
_CUSTOM_MODEL_METADATA_SUFFIX = ".json"
_CUSTOM_MODEL_FILE_SUFFIX = ".tflite"
_CUSTOM_MODEL_LABEL_LIMIT = 80
_BUNDLED_HEY_NABU_PATH = Path(__file__).with_name("models") / "hey_nabu_v2.tflite"


@dataclass(frozen=True)
class WakewordModelDescriptor:
    """One Desktop-local TFLite wakeword model available for selection."""

    id: str
    label: str
    source: str
    format: str
    removable: bool
    target: str
    builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the public descriptor without exposing a local filesystem path."""
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "format": self.format,
            "removable": self.removable,
        }


@dataclass(frozen=True)
class WakewordMatch:
    """The single winning detector result for one audio window."""

    model_id: str
    score: float
    threshold: float


_BUILTIN_MODELS = (
    WakewordModelDescriptor(
        id="builtin/okay_nabu",
        label="Okay Nabu",
        source="built_in",
        format="tflite",
        removable=False,
        target="okay_nabu",
        builtin=True,
    ),
    WakewordModelDescriptor(
        id="builtin/hey_nabu",
        label="Hey Nabu",
        source="built_in",
        format="tflite",
        removable=False,
        target=str(_BUNDLED_HEY_NABU_PATH),
    ),
    WakewordModelDescriptor(
        id="builtin/hey_jarvis",
        label="Hey Jarvis",
        source="built_in",
        format="tflite",
        removable=False,
        target="hey_jarvis",
        builtin=True,
    ),
    WakewordModelDescriptor(
        id="builtin/hey_mycroft",
        label="Hey Mycroft",
        source="built_in",
        format="tflite",
        removable=False,
        target="hey_mycroft",
        builtin=True,
    ),
    WakewordModelDescriptor(
        id="builtin/hey_rhasspy",
        label="Hey Rhasspy",
        source="built_in",
        format="tflite",
        removable=False,
        target="hey_rhasspy",
        builtin=True,
    ),
    WakewordModelDescriptor(
        id="builtin/alexa",
        label="Alexa",
        source="built_in",
        format="tflite",
        removable=False,
        target="alexa",
        builtin=True,
    ),
)
_BUILTIN_MODELS_BY_ID = {descriptor.id: descriptor for descriptor in _BUILTIN_MODELS}


class WakewordModelError(ValueError):
    """A stable, user-actionable wakeword model catalog failure."""

    def __init__(self, message: str, *, error_code: str = "wakeword_model_invalid") -> None:
        super().__init__(message)
        self.error_code = error_code


class WakewordModelCatalog:
    """Own built-in selection and durable Desktop-local TFLite imports."""

    def __init__(self, settings_file: Path | None = None) -> None:
        if settings_file is None:
            from desktop.settings import settings_path

            settings_file = settings_path()
        self._model_directory = settings_file.parent / _CUSTOM_MODEL_DIRECTORY_NAME

    def list_models(self) -> list[WakewordModelDescriptor]:
        """Return curated built-ins followed by valid imported models."""
        custom_models = sorted(
            self._custom_models(), key=lambda model: (model.label.casefold(), model.id)
        )
        return [*_BUILTIN_MODELS, *custom_models]

    def resolve(self, model_id: str) -> WakewordModelDescriptor:
        """Resolve a public model id to its executable target."""
        builtin = _BUILTIN_MODELS_BY_ID.get(model_id)
        if builtin is not None:
            return builtin
        custom = next((model for model in self._custom_models() if model.id == model_id), None)
        if custom is not None:
            return custom
        raise WakewordModelError(
            f"Wakeword model is not available: {model_id}",
            error_code="wakeword_model_unavailable",
        )

    def create_engine(
        self,
        active_model_ids: list[str] | tuple[str, ...],
        model_sensitivities: dict[str, float] | None = None,
    ) -> MultiWakewordEngine:
        """Create one shared-feature detector for the active catalog entries."""
        model_ids = tuple(active_model_ids)
        if not 1 <= len(model_ids) <= MAX_ACTIVE_WAKEWORD_MODELS:
            raise WakewordModelError(
                f"Choose between 1 and {MAX_ACTIVE_WAKEWORD_MODELS} wakeword models"
            )
        if len(set(model_ids)) != len(model_ids):
            raise WakewordModelError("Active wakeword models must be unique")
        descriptors = tuple(self.resolve(model_id) for model_id in model_ids)
        return MultiWakewordEngine(descriptors, model_sensitivities or {})

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
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=self._model_directory,
                delete=False,
                prefix=f".{model_token}.",
                suffix=_CUSTOM_MODEL_FILE_SUFFIX,
            ) as temporary_file:
                temporary_file.write(content)
                temporary_path = Path(temporary_file.name)
            _validate_custom_model(temporary_path)
            temporary_path.replace(model_path)
            temporary_path = None
            metadata = {
                "id": model_id,
                "label": _display_label(original_name),
                "filename": model_path.name,
                "format": "tflite",
                "source": "imported",
            }
            _write_json_atomic(metadata_path, metadata)
        except WakewordModelError:
            _remove_file(temporary_path)
            _remove_file(model_path)
            _remove_file(metadata_path)
            raise
        except Exception as exc:
            _remove_file(temporary_path)
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
            raise WakewordModelError("Built-in wakeword models cannot be removed")
        model_token = model_id.removeprefix(_CUSTOM_MODEL_PREFIX)
        model_path = self._model_directory / f"{model_token}{_CUSTOM_MODEL_FILE_SUFFIX}"
        metadata_path = self._metadata_path(model_token)
        try:
            model_path.unlink()
            metadata_path.unlink(missing_ok=True)
        except OSError as exc:
            raise WakewordModelError("Wakeword model could not be removed") from exc

    def _custom_models(self) -> list[WakewordModelDescriptor]:
        if not self._model_directory.is_dir():
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
        except (OSError, json.JSONDecodeError):
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
        if not model_path.is_file():
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

    def detect(self, audio_chunk: bytes) -> WakewordMatch | None:
        """Return at most one winning wakeword match for an audio chunk."""
        ...


class MockWakewordEngine:
    """Configurable structured-match engine for UI and worker tests."""

    def __init__(
        self,
        score_sequence: list[float] | None = None,
        *,
        model_id: str = DEFAULT_WAKEWORD_MODEL_IDS[0],
        sensitivity: float = DEFAULT_WAKEWORD_SENSITIVITY,
    ) -> None:
        self._score_sequence = score_sequence or [0.0]
        self._model_id = model_id
        self._threshold = _threshold_for_sensitivity(sensitivity)
        self._index = 0
        self._running = False

    def start(self) -> None:
        self._running = True
        self._index = 0

    def stop(self) -> None:
        self._running = False

    def detect(self, audio_chunk: bytes) -> WakewordMatch | None:
        """Return a match when the next configured score reaches the threshold."""
        if not self._running:
            return None
        score = _clamp_score(self._score_sequence[self._index % len(self._score_sequence)])
        self._index += 1
        if score < self._threshold:
            return None
        return WakewordMatch(self._model_id, score, self._threshold)

    def set_score_sequence(self, sequence: list[float]) -> None:
        """Replace the score sequence and reset the index."""
        self._score_sequence = list(sequence)
        self._index = 0


class MultiWakewordEngine:
    """Run multiple pyopen-wakeword TFLite heads over one feature stream."""

    def __init__(
        self,
        descriptors: tuple[WakewordModelDescriptor, ...],
        model_sensitivities: dict[str, float],
    ) -> None:
        if not descriptors:
            raise WakewordModelError("At least one wakeword model is required")
        self._descriptors = descriptors
        self._thresholds = {
            descriptor.id: _threshold_for_sensitivity(
                model_sensitivities.get(descriptor.id, DEFAULT_WAKEWORD_SENSITIVITY)
            )
            for descriptor in descriptors
        }
        self._models: list[tuple[WakewordModelDescriptor, Any]] = []
        self._features: Any = None
        self._armed = True

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
            features = _create_pyopenwakeword_features()
            for descriptor in self._descriptors:
                models.append((descriptor, _create_pyopenwakeword_model(descriptor)))
        except Exception:
            _close_models(models)
            if features is not None:
                features.close()
            raise
        self._features = features
        self._models = models
        self._armed = True

    def stop(self) -> None:
        """Release all native TensorFlow Lite resources."""
        models = self._models
        features = self._features
        self._models = []
        self._features = None
        self._armed = True
        try:
            _close_models(models)
        finally:
            if features is not None:
                features.close()

    def detect(self, audio_chunk: bytes) -> WakewordMatch | None:
        """Return the strongest threshold-normalized model match for one chunk."""
        if self._features is None or not self._models:
            return None
        feature_batches = list(self._features.process_streaming(audio_chunk))
        best_match: WakewordMatch | None = None
        best_ratio = 0.0
        all_below_threshold = True
        for descriptor, model in self._models:
            score = max(
                (
                    _clamp_score(score)
                    for features in feature_batches
                    for score in model.process_streaming(features)
                ),
                default=0.0,
            )
            threshold = self._thresholds[descriptor.id]
            if score < threshold:
                continue
            all_below_threshold = False
            ratio = score / threshold
            if best_match is None or ratio > best_ratio:
                best_match = WakewordMatch(descriptor.id, score, threshold)
                best_ratio = ratio
        if not self._armed:
            if all_below_threshold:
                self._armed = True
            return None
        if best_match is not None:
            self._armed = False
        return best_match


def _create_pyopenwakeword_features() -> Any:
    from pyopen_wakeword import OpenWakeWordFeatures  # type: ignore[import-untyped]

    return OpenWakeWordFeatures.from_builtin()


def _create_pyopenwakeword_model(descriptor: WakewordModelDescriptor) -> Any:
    from pyopen_wakeword import Model, OpenWakeWord  # type: ignore[import-untyped]

    try:
        if descriptor.builtin:
            return OpenWakeWord.from_builtin(Model(descriptor.target))
        return OpenWakeWord.from_model(descriptor.target)
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
        model = _create_pyopenwakeword_model(descriptor)
    except Exception as exc:
        raise WakewordModelError(
            "The selected file is not a compatible TFLite wakeword model"
        ) from exc
    model.close()


def _threshold_for_sensitivity(sensitivity: float) -> float:
    normalized_sensitivity = max(
        MIN_WAKEWORD_SENSITIVITY,
        min(MAX_WAKEWORD_SENSITIVITY, float(sensitivity)),
    )
    return 1.0 - normalized_sensitivity


def _clamp_score(score: float) -> float:
    return max(0.0, min(1.0, float(score)))


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
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            json.dump(value, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(path)
    finally:
        _remove_file(temporary_path)


def _remove_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to clean up wakeword model file: %s", os.fspath(path))
