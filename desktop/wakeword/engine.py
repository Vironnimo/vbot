"""Wakeword model catalog, engine abstraction, and implementations."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

logger = logging.getLogger("vbot.desktop.wakeword.engine")

DEFAULT_WAKEWORD_MODEL_ID = "builtin/hey_jarvis"
DEFAULT_WAKEWORD_SENSITIVITY = 0.5
MIN_WAKEWORD_SENSITIVITY = 0.05
MAX_WAKEWORD_SENSITIVITY = 0.95
MAX_CUSTOM_WAKEWORD_MODEL_BYTES = 20 * 1024 * 1024

_WAKEWORD_VAD_THRESHOLD = 0.3
_CUSTOM_MODEL_PREFIX = "custom/"
_CUSTOM_MODEL_DIRECTORY_NAME = "wakewords"
_CUSTOM_MODEL_METADATA_SUFFIX = ".json"
_CUSTOM_MODEL_FILE_SUFFIX = ".onnx"
_CUSTOM_MODEL_LABEL_LIMIT = 80


@dataclass(frozen=True)
class WakewordModelDescriptor:
    """One Desktop-local wakeword model available for selection."""

    id: str
    label: str
    source: str
    format: str
    removable: bool
    target: str

    def to_dict(self) -> dict[str, Any]:
        """Return the public descriptor without exposing a local filesystem path."""
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "format": self.format,
            "removable": self.removable,
        }


_BUILTIN_MODELS = (
    WakewordModelDescriptor(
        id="builtin/hey_jarvis",
        label="Hey Jarvis",
        source="built_in",
        format="onnx",
        removable=False,
        target="hey_jarvis",
    ),
    WakewordModelDescriptor(
        id="builtin/hey_mycroft",
        label="Hey Mycroft",
        source="built_in",
        format="onnx",
        removable=False,
        target="hey_mycroft",
    ),
    WakewordModelDescriptor(
        id="builtin/hey_rhasspy",
        label="Hey Rhasspy",
        source="built_in",
        format="onnx",
        removable=False,
        target="hey_rhasspy",
    ),
    WakewordModelDescriptor(
        id="builtin/alexa",
        label="Alexa",
        source="built_in",
        format="onnx",
        removable=False,
        target="alexa",
    ),
)
_BUILTIN_MODELS_BY_ID = {descriptor.id: descriptor for descriptor in _BUILTIN_MODELS}


class WakewordModelError(ValueError):
    """A stable, user-actionable wakeword model catalog failure."""

    def __init__(self, message: str, *, error_code: str = "wakeword_model_invalid") -> None:
        super().__init__(message)
        self.error_code = error_code


class WakewordModelCatalog:
    """Own built-in selection and durable Desktop-local ONNX model imports."""

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

    def import_model(self, filename: str, content: bytes) -> WakewordModelDescriptor:
        """Validate and persist one user-supplied ONNX wakeword model."""
        original_name = _safe_original_filename(filename)
        if Path(original_name).suffix.casefold() != _CUSTOM_MODEL_FILE_SUFFIX:
            raise WakewordModelError("Wakeword model must be an ONNX file")
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
                "format": "onnx",
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
            format="onnx",
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
            format="onnx",
            removable=True,
            target=str(model_path),
        )

    def _metadata_path(self, model_token: str) -> Path:
        return self._model_directory / f"{model_token}{_CUSTOM_MODEL_METADATA_SUFFIX}"


class WakewordEngine(Protocol):
    """Abstract interface for wakeword detection engines."""

    def start(self) -> None:
        """Initialize the engine and begin audio capture."""
        ...

    def stop(self) -> None:
        """Stop audio capture and release resources."""
        ...

    def detect(self, audio_chunk: bytes) -> float:
        """Return detection score 0.0-1.0 for an audio chunk."""
        ...


class MockWakewordEngine:
    """Configurable mock engine for UI testing without real microphone."""

    def __init__(self, score_sequence: list[float] | None = None) -> None:
        self._score_sequence = score_sequence or [0.0]
        self._index = 0
        self._running = False

    def start(self) -> None:
        self._running = True
        self._index = 0

    def stop(self) -> None:
        self._running = False

    def detect(self, audio_chunk: bytes) -> float:
        """Return the next score from the configured sequence."""
        if not self._running:
            return 0.0
        score = self._score_sequence[self._index % len(self._score_sequence)]
        self._index += 1
        return score

    def set_score_sequence(self, sequence: list[float]) -> None:
        """Replace the score sequence and reset the index."""
        self._score_sequence = list(sequence)
        self._index = 0


class OpenWakeWordEngine:
    """Wakeword detection via openWakeWord with one active ONNX model."""

    def __init__(
        self,
        model_target: str = "hey_jarvis",
        sensitivity: float = DEFAULT_WAKEWORD_SENSITIVITY,
    ) -> None:
        self._model_target = model_target
        normalized_sensitivity = max(
            MIN_WAKEWORD_SENSITIVITY,
            min(MAX_WAKEWORD_SENSITIVITY, float(sensitivity)),
        )
        self._threshold = 1.0 - normalized_sensitivity
        self._model: Any = None

    @property
    def threshold(self) -> float:
        """Score threshold above which a detection is triggered."""
        return self._threshold

    def start(self) -> None:
        """Load the configured built-in name or imported ONNX path."""
        self._model = _create_openwakeword_model(
            self._model_target,
            vad_threshold=_WAKEWORD_VAD_THRESHOLD,
        )

    def stop(self) -> None:
        """Release the openWakeWord model."""
        self._model = None

    def detect(self, audio_chunk: bytes) -> float:
        """Run inference and return the strongest score from the single model."""
        if self._model is None:
            return 0.0
        import numpy as np

        audio_array = np.frombuffer(audio_chunk, dtype=np.int16)
        prediction = self._model.predict(audio_array)
        scores = [float(score) for score in prediction.values()]
        score = max(scores, default=0.0)
        return max(0.0, min(1.0, score))


def _create_openwakeword_model(model_target: str, *, vad_threshold: float) -> Any:
    from openwakeword.model import Model  # type: ignore[import-untyped]

    return Model(
        wakeword_models=[model_target],
        inference_framework="onnx",
        vad_threshold=vad_threshold,
    )


def _validate_custom_model(model_path: Path) -> None:
    try:
        model = _create_openwakeword_model(str(model_path), vad_threshold=0.0)
    except Exception as exc:
        raise WakewordModelError(
            "The selected file is not a compatible ONNX wakeword model"
        ) from exc
    if len(getattr(model, "models", {})) != 1:
        raise WakewordModelError("Wakeword model must contain exactly one detector")


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
