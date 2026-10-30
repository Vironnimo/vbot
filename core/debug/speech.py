"""Bounded, opt-in speech diagnostics: original upload bytes and their outcome.

The Debug owner's ordered worker serializes publication, retention and reads.
Audio never enters the Provider trace JSON format or its independent limit.
"""

from __future__ import annotations

import json
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from core.debug.store import _TRACE_THREAD
from core.storage.layout import DataDirectoryLayout
from core.utils.atomic import atomic_write_text, write_new_bytes
from core.utils.file_status import is_link_status
from core.utils.ids import is_safe_id, write_id_file
from core.utils.logging import get_logger
from core.utils.surrogates import without_lone_surrogates

_LOGGER = get_logger("debug.speech")


def _is_recording_id(value: object) -> bool:
    return isinstance(value, str) and value.startswith("stt_") and is_safe_id(value)


def _audio_media_type(value: str) -> str:
    media_type = value.split(";", 1)[0].strip().lower()
    media_type = {"video/webm": "audio/webm", "video/mp4": "audio/mp4"}.get(media_type, media_type)
    # Failed STT inputs can be arbitrary uploads. Never serve supplied HTML or
    # other active content as a same-origin document from the diagnostic URL.
    if not media_type.startswith("audio/") or not _MEDIA_TYPE.fullmatch(media_type):
        return "application/octet-stream"
    return media_type


_MEDIA_TYPE = re.compile(r"[a-z0-9.+-]+/[a-z0-9.+-]+", re.IGNORECASE)
_MAX_PENDING = 4


class SpeechDebugDisabledError(PermissionError):
    """Inspection is unavailable while Debug capture is disabled."""


@dataclass(frozen=True)
class SpeechDebugCapture:
    """One enabled-at-admission capture, finalized without delaying transcription."""

    store: SpeechDebugStore
    audio: bytes
    media_type: str
    timestamp: str
    started: float

    def finish(self, text: str | None, error: BaseException | None) -> None:
        metadata = {
            "timestamp": self.timestamp,
            "media_type": self.media_type,
            "size_bytes": len(self.audio),
            "duration_ms": round((time.monotonic() - self.started) * 1000),
            "text": text,
            "error": (
                {"type": type(error).__name__, "message": str(error)} if error is not None else None
            ),
        }
        try:
            if not _TRACE_THREAD.hand_off(
                partial(self.store._save_safely, self.audio, metadata), limit=_MAX_PENDING
            ):
                _LOGGER.warning(
                    "Speech debug capture dropped: earlier captures are still being written"
                )
        except Exception:
            _LOGGER.warning("Speech debug capture could not be queued", exc_info=True)


class SpeechDebugStore:
    """Server-owned speech diagnostics under artifacts/debug/speech.

    A capture must be enabled both at admission and publication. Turning Debug
    off leaves retained diagnostics intact, but never records an in-flight take
    after the switch. Settings reads and every filesystem operation run off-loop.
    """

    def __init__(
        self,
        data_dir: str | Path,
        settings: Callable[[], Mapping[str, Any]],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        debug_dir = DataDirectoryLayout(data_dir).debug
        self._directory = debug_dir / "speech"
        self._settings = settings
        self._clock = clock

    def begin(self, audio: bytes, media_type: str) -> SpeechDebugCapture | None:
        """Snapshot admission policy on the caller's worker; never fail transcription."""
        try:
            if not audio or not self._settings().get("enabled", False):
                return None
            media_type = _audio_media_type(media_type)
            return SpeechDebugCapture(
                self, audio, media_type, self._clock().isoformat(), time.monotonic()
            )
        except Exception:
            _LOGGER.warning("Speech debug capture could not be initialized", exc_info=True)
            return None

    async def list_async(self) -> list[dict[str, Any]]:
        return await _TRACE_THREAD.call_async(self._inspect)

    async def audio_async(self, recording_id: str) -> tuple[bytes, str]:
        self._validate_id(recording_id)
        return await _TRACE_THREAD.call_async(partial(self._audio, recording_id))

    async def clear_async(self) -> None:
        await _TRACE_THREAD.call_async(self._clear)

    async def prune_async(self) -> None:
        await _TRACE_THREAD.call_async(self._prune)

    def _check_directory(self) -> bool:
        try:
            status = self._directory.lstat()
        except FileNotFoundError:
            return False
        if is_link_status(status) or not stat.S_ISDIR(status.st_mode):
            raise ValueError("Speech debug directory must be a regular directory")
        return True

    @staticmethod
    def _validate_id(recording_id: str) -> None:
        if not isinstance(recording_id, str) or not _is_recording_id(recording_id):
            raise ValueError("Invalid speech recording id")

    @staticmethod
    def _regular_file(path: Path) -> None:
        status = path.lstat()
        if is_link_status(status) or not stat.S_ISREG(status.st_mode):
            raise ValueError("Speech debug entry must be a regular file")

    def _metadata(self, recording_id: str) -> dict[str, Any]:
        self._validate_id(recording_id)
        path = self._directory / f"{recording_id}.json"
        self._regular_file(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("format_version") != 1
            or data.get("recording_id") != recording_id
            or not isinstance(data.get("timestamp"), str)
            or not isinstance(data.get("media_type"), str)
            or _MEDIA_TYPE.fullmatch(data["media_type"]) is None
            or type(data.get("size_bytes")) is not int
            or data["size_bytes"] < 1
            or not (data.get("text") is None or isinstance(data["text"], str))
            or not (data.get("error") is None or isinstance(data["error"], dict))
        ):
            raise ValueError("Invalid speech debug metadata")
        return data

    def _list(self) -> list[dict[str, Any]]:
        if not self._check_directory():
            return []
        records = [
            self._metadata(path.stem)
            for path in self._directory.iterdir()
            if path.suffix == ".json" and _is_recording_id(path.stem) and path.lstat().st_size > 0
        ]
        return sorted(records, key=lambda record: record["timestamp"], reverse=True)

    def _ensure_enabled(self) -> None:
        if not self._settings().get("enabled", False):
            raise SpeechDebugDisabledError("Debug mode is not enabled")

    def _inspect(self) -> list[dict[str, Any]]:
        self._ensure_enabled()
        return self._list()

    def _audio(self, recording_id: str) -> tuple[bytes, str]:
        self._ensure_enabled()
        if not self._check_directory():
            raise FileNotFoundError("Speech recording not found")
        metadata = self._metadata(recording_id)
        path = self._directory / f"{recording_id}.audio"
        self._regular_file(path)
        audio = path.read_bytes()
        if len(audio) != metadata["size_bytes"]:
            raise ValueError("Speech recording size does not match its metadata")
        return audio, _audio_media_type(metadata["media_type"])

    def _save_safely(self, audio: bytes, metadata: dict[str, Any]) -> None:
        path = None
        audio_created = False
        published = False
        try:
            if not self._settings().get("enabled", False):
                return
            self._directory.mkdir(parents=True, exist_ok=True)
            self._check_directory()
            # Validate existing records before publishing or pruning anything.
            self._list()
            while True:
                # The sidecar owns identity even if its blob is missing. An
                # empty reserved sidecar is an unpublished, recoverable take.
                path = write_id_file(self._directory, "stt", ".json", b"")
                try:
                    write_new_bytes(path.with_suffix(".audio"), audio)
                except FileExistsError:
                    path.unlink()
                    continue
                audio_created = True
                break
            atomic_write_text(
                path,
                json.dumps(
                    without_lone_surrogates(
                        {"format_version": 1, "recording_id": path.stem, **metadata}
                    ),
                    ensure_ascii=False,
                ),
            )
            published = True
            self._prune()
        except Exception:
            try:
                if path is not None and not published:
                    path.unlink(missing_ok=True)
                    if audio_created:
                        path.with_suffix(".audio").unlink(missing_ok=True)
            except OSError:
                _LOGGER.warning(
                    "Incomplete speech debug capture could not be removed", exc_info=True
                )
            _LOGGER.warning("Speech debug capture could not be saved", exc_info=True)

    def _prune(self) -> None:
        records = self._list()
        limit = self._settings().get("speech_recording_limit", 10)
        for record in records[limit:]:
            recording_id = record["recording_id"]
            (self._directory / f"{recording_id}.json").unlink(missing_ok=True)
            (self._directory / f"{recording_id}.audio").unlink(missing_ok=True)
        # A crash between audio creation and metadata publication leaves only
        # an unlisted blob. No in-process writer can overlap this ordered sweep.
        retained = {record["recording_id"] for record in records[:limit]}
        if self._check_directory():
            for path in self._directory.iterdir():
                if (
                    path.suffix in {".audio", ".json"}
                    and _is_recording_id(path.stem)
                    and path.stem not in retained
                    and not stat.S_ISDIR(path.lstat().st_mode)
                ):
                    path.unlink(missing_ok=True)

    def _clear(self) -> None:
        if not self._check_directory():
            return
        for path in self._directory.iterdir():
            if (
                path.suffix in {".json", ".audio"}
                and _is_recording_id(path.stem)
                and not stat.S_ISDIR(path.lstat().st_mode)
            ):
                path.unlink(missing_ok=True)
