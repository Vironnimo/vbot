"""openWakeWord inference over the TensorFlow Lite C library that pyopen-wakeword ships.

:class:`FeatureStream` turns 16 kHz PCM16 into openWakeWord embeddings
(melspectrogram, then embedding model) and :class:`PhraseHead` scores one wake
phrase from them. Both reproduce pyopen-wakeword's streaming windows exactly,
including its 8 s zero prefill, so scores stay comparable with the tuned
thresholds. pyopen-wakeword supplies only the native library and the built-in
models.

vBot creates the interpreters itself because pyopen-wakeword creates them with
default options: TensorFlow Lite's reference kernels and its own worker thread
pools, whose idle workers cost more CPU between the 80 ms chunks than the
inference. Here every interpreter runs on the calling thread, with the XNNPACK
delegate when the library provides it, else with the built-in kernels.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("vbot.desktop.wakeword.openwakeword")

_CHUNK_SAMPLES = 1280  # the stride between mel windows: 80 ms at 16 kHz
_PREFILL_SAMPLES = 8 * 16000  # the stream starts as if 8 s of silence preceded it
_MAX_SAMPLES = 10 * 16000
_MEL_WINDOW_SAMPLES = 1760
_MEL_BANDS = 32
_MAX_MELS = 10 * 97
_EMBEDDING_WINDOW_MELS = 76  # 775 ms
_EMBEDDING_STEP_MELS = 8
_EMBEDDING_FEATURES = 96
_MAX_EMBEDDINGS = 10 * 8

_FLOAT32_BYTES = 4
_OK = 0


class _XnnPackOptions(ctypes.Structure):
    """``TfLiteXNNPackDelegateOptions`` with room for every release's fields.

    Only ``num_threads`` (the first field in every release) is written; the
    rest keeps the library's defaults, whatever their layout.
    """

    _fields_ = [
        ("num_threads", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("_reserved", ctypes.c_uint64 * 62),
    ]


class _TfLite:
    """The TensorFlow Lite C API functions this module calls."""

    def __init__(self, path: Path) -> None:
        mode = 0 if sys.platform == "win32" else ctypes.RTLD_GLOBAL
        lib = ctypes.CDLL(os.fspath(path), mode=mode)
        pointer, int32 = ctypes.c_void_p, ctypes.c_int32
        _declare(lib.TfLiteModelCreate, [ctypes.c_char_p, ctypes.c_size_t], pointer)
        _declare(lib.TfLiteModelDelete, [pointer], None)
        _declare(lib.TfLiteInterpreterOptionsCreate, [], pointer)
        _declare(lib.TfLiteInterpreterOptionsDelete, [pointer], None)
        _declare(lib.TfLiteInterpreterOptionsSetNumThreads, [pointer, int32], None)
        _declare(lib.TfLiteInterpreterOptionsAddDelegate, [pointer, pointer], None)
        _declare(lib.TfLiteInterpreterCreate, [pointer, pointer], pointer)
        _declare(lib.TfLiteInterpreterDelete, [pointer], None)
        _declare(
            lib.TfLiteInterpreterResizeInputTensor,
            [pointer, int32, ctypes.POINTER(int32), int32],
            ctypes.c_int,
        )
        _declare(lib.TfLiteInterpreterAllocateTensors, [pointer], ctypes.c_int)
        _declare(lib.TfLiteInterpreterInvoke, [pointer], ctypes.c_int)
        _declare(lib.TfLiteInterpreterGetInputTensor, [pointer, int32], pointer)
        _declare(lib.TfLiteInterpreterGetOutputTensor, [pointer, int32], pointer)
        _declare(lib.TfLiteTensorNumDims, [pointer], int32)
        _declare(lib.TfLiteTensorDim, [pointer, int32], int32)
        _declare(lib.TfLiteTensorByteSize, [pointer], ctypes.c_size_t)
        _declare(lib.TfLiteTensorCopyFromBuffer, [pointer, pointer, ctypes.c_size_t], ctypes.c_int)
        _declare(lib.TfLiteTensorCopyToBuffer, [pointer, pointer, ctypes.c_size_t], ctypes.c_int)
        self.lib = lib
        self.xnnpack = all(
            hasattr(lib, name)
            for name in (
                "TfLiteXNNPackDelegateOptionsDefault",
                "TfLiteXNNPackDelegateCreate",
                "TfLiteXNNPackDelegateDelete",
            )
        )
        if self.xnnpack:
            _declare(lib.TfLiteXNNPackDelegateOptionsDefault, [], _XnnPackOptions)
            _declare(lib.TfLiteXNNPackDelegateCreate, [ctypes.POINTER(_XnnPackOptions)], pointer)
            _declare(lib.TfLiteXNNPackDelegateDelete, [pointer], None)

    def create_xnnpack_delegate(self) -> int | None:
        """A single-threaded XNNPACK delegate, or ``None`` when the library has none."""
        if not self.xnnpack:
            return None
        options = self.lib.TfLiteXNNPackDelegateOptionsDefault()
        options.num_threads = 1
        delegate: int | None = self.lib.TfLiteXNNPackDelegateCreate(ctypes.byref(options))
        return delegate or None


def _declare(function: Any, argtypes: list[Any], restype: Any) -> None:
    function.argtypes = argtypes
    function.restype = restype


def _package_directory() -> Path:
    import pyopen_wakeword  # type: ignore[import-untyped]

    return Path(pyopen_wakeword.__file__).resolve().parent


@functools.cache
def _tflite() -> _TfLite:
    library = next(iter(sorted((_package_directory() / "lib").glob("*tensorflowlite_c.*"))), None)
    if library is None:
        raise RuntimeError("pyopen-wakeword ships no TensorFlow Lite library for this platform")
    return _TfLite(library)


def builtin_model_path(name: str) -> Path:
    """Path of one of pyopen-wakeword's built-in phrase models."""
    from pyopen_wakeword import Model

    return _package_directory() / "models" / f"{Model(name).value}.tflite"


class _Interpreter:
    """One model on one interpreter, invoked on the calling thread."""

    _xnnpack_fallback_logged = False

    def __init__(self, tflite: _TfLite, model_path: Path, input_shape: tuple[int, ...] | None):
        self._tflite = tflite
        self._interpreter: int | None = None
        self._delegate: int | None = None
        self._model: int | None = None
        # The model keeps pointing into this buffer for its whole lifetime.
        self._model_data = Path(model_path).read_bytes()
        lib = tflite.lib
        self._model = lib.TfLiteModelCreate(self._model_data, len(self._model_data))
        if not self._model:
            raise RuntimeError(f"TensorFlow Lite cannot read {Path(model_path).name}")
        try:
            self._create(input_shape)
            self.input = lib.TfLiteInterpreterGetInputTensor(self._interpreter, 0)
            self.output = lib.TfLiteInterpreterGetOutputTensor(self._interpreter, 0)
            if not self.input or not self.output:
                raise RuntimeError(f"{Path(model_path).name} has no input or output tensor")
            self.input_bytes = int(lib.TfLiteTensorByteSize(self.input))
            self.output_bytes = int(lib.TfLiteTensorByteSize(self.output))
        except BaseException:
            self.close()
            raise

    def input_shape(self) -> tuple[int, ...]:
        lib = self._tflite.lib
        return tuple(
            int(lib.TfLiteTensorDim(self.input, index))
            for index in range(int(lib.TfLiteTensorNumDims(self.input)))
        )

    def run(self, values: np.ndarray) -> np.ndarray:
        """Invoke the model on one contiguous float32 input; return the flat float32 output."""
        lib = self._tflite.lib
        values = np.ascontiguousarray(values, dtype=np.float32)
        if values.nbytes != self.input_bytes:
            raise RuntimeError("wakeword model input has an unexpected size")
        output = np.empty(self.output_bytes // _FLOAT32_BYTES, dtype=np.float32)
        if (
            lib.TfLiteTensorCopyFromBuffer(self.input, values.ctypes.data, values.nbytes) != _OK
            or lib.TfLiteInterpreterInvoke(self._interpreter) != _OK
            or lib.TfLiteTensorCopyToBuffer(self.output, output.ctypes.data, output.nbytes) != _OK
        ):
            raise RuntimeError("TensorFlow Lite could not run a wakeword model")
        return output

    def close(self) -> None:
        lib = self._tflite.lib
        interpreter, self._interpreter = self._interpreter, None
        delegate, self._delegate = self._delegate, None
        model, self._model = self._model, None
        # The interpreter uses both the delegate and the model, so it goes first.
        if interpreter:
            lib.TfLiteInterpreterDelete(interpreter)
        if delegate:
            lib.TfLiteXNNPackDelegateDelete(delegate)
        if model:
            lib.TfLiteModelDelete(model)

    def _create(self, input_shape: tuple[int, ...] | None) -> None:
        delegate = self._tflite.create_xnnpack_delegate()
        if delegate is not None:
            self._interpreter = self._try_create(input_shape, delegate)
            if self._interpreter:
                self._delegate = delegate
                return
            self._tflite.lib.TfLiteXNNPackDelegateDelete(delegate)
        if not _Interpreter._xnnpack_fallback_logged:
            _Interpreter._xnnpack_fallback_logged = True
            reason = "unavailable" if delegate is None else "rejected by the model"
            logger.info("Wakeword models run without XNNPACK (%s)", reason)
        self._interpreter = self._try_create(input_shape, None)
        if not self._interpreter:
            raise RuntimeError("TensorFlow Lite cannot prepare the wakeword model")

    def _try_create(self, input_shape: tuple[int, ...] | None, delegate: int | None) -> int | None:
        lib = self._tflite.lib
        options = lib.TfLiteInterpreterOptionsCreate()
        if not options:
            return None
        try:
            lib.TfLiteInterpreterOptionsSetNumThreads(options, 1)
            if delegate is not None:
                lib.TfLiteInterpreterOptionsAddDelegate(options, delegate)
            interpreter: int | None = lib.TfLiteInterpreterCreate(self._model, options)
        finally:
            lib.TfLiteInterpreterOptionsDelete(options)
        if not interpreter:
            return None
        if input_shape is not None:
            dims = (ctypes.c_int32 * len(input_shape))(*input_shape)
            if (
                lib.TfLiteInterpreterResizeInputTensor(interpreter, 0, dims, len(input_shape))
                != _OK
            ):
                lib.TfLiteInterpreterDelete(interpreter)
                return None
        if lib.TfLiteInterpreterAllocateTensors(interpreter) != _OK:
            lib.TfLiteInterpreterDelete(interpreter)
            return None
        return interpreter


class FeatureStream:
    """Streaming openWakeWord features shared by every active phrase."""

    def __init__(self) -> None:
        tflite = _tflite()
        models = _package_directory() / "models"
        self._mel = _Interpreter(tflite, models / "melspectrogram.tflite", (1, _MEL_WINDOW_SAMPLES))
        try:
            self._embedding = _Interpreter(
                tflite,
                models / "embedding_model.tflite",
                (1, _EMBEDDING_WINDOW_MELS, _MEL_BANDS, 1),
            )
        except BaseException:
            self._mel.close()
            raise
        self._audio = np.zeros(_MAX_SAMPLES, dtype=np.float32)
        self._new_samples = _PREFILL_SAMPLES
        self._mels = np.zeros((_MAX_MELS, _MEL_BANDS), dtype=np.float32)
        self._new_mels = 0

    def process(self, chunk: bytes) -> list[np.ndarray]:
        """Consume PCM16 audio; return the embeddings (96 floats each) it completed."""
        samples = np.frombuffer(chunk, dtype=np.int16)
        count = len(samples)
        if count == 0:
            return []
        audio = self._audio
        audio[:-count] = audio[count:]
        audio[-count:] = samples
        self._new_samples = min(len(audio), self._new_samples + count)
        embeddings: list[np.ndarray] = []
        while self._new_samples >= _MEL_WINDOW_SAMPLES:
            start = len(audio) - self._new_samples
            window = audio[start : start + _MEL_WINDOW_SAMPLES]
            self._new_samples = max(0, self._new_samples - _CHUNK_SAMPLES)
            mels = (self._mel.run(window) / 10 + 2).reshape(-1, _MEL_BANDS)
            self._append_mels(mels, embeddings)
        return embeddings

    def close(self) -> None:
        self._mel.close()
        self._embedding.close()

    def _append_mels(self, mels: np.ndarray, embeddings: list[np.ndarray]) -> None:
        count = len(mels)
        self._mels[:-count] = self._mels[count:]
        self._mels[-count:] = mels
        self._new_mels = min(len(self._mels), self._new_mels + count)
        while self._new_mels >= _EMBEDDING_WINDOW_MELS:
            start = len(self._mels) - self._new_mels
            window = self._mels[start : start + _EMBEDDING_WINDOW_MELS]
            self._new_mels = max(0, self._new_mels - _EMBEDDING_STEP_MELS)
            embeddings.extend(self._embedding.run(window).reshape(-1, _EMBEDDING_FEATURES))


class PhraseHead:
    """One wake phrase's classifier over the most recent embeddings."""

    def __init__(self, model_path: Path) -> None:
        self._head = _Interpreter(_tflite(), model_path, None)
        try:
            shape = self._head.input_shape()
            if (
                len(shape) != 3
                or shape[0] != 1
                or shape[2] != _EMBEDDING_FEATURES
                or not 1 <= shape[1] <= _MAX_EMBEDDINGS
                or self._head.input_bytes != shape[1] * _EMBEDDING_FEATURES * _FLOAT32_BYTES
                or self._head.output_bytes != _FLOAT32_BYTES
            ):
                raise RuntimeError("not an openWakeWord phrase model")
        except BaseException:
            self._head.close()
            raise
        self._windows = shape[1]
        self._embeddings = np.zeros((_MAX_EMBEDDINGS, _EMBEDDING_FEATURES), dtype=np.float32)
        self._new_embeddings = 0

    def scores(self, embeddings: list[np.ndarray]) -> list[float]:
        """Score each window the new embeddings complete, oldest first."""
        scores: list[float] = []
        history = self._embeddings
        for embedding in embeddings:
            history[:-1] = history[1:]
            history[-1] = embedding
            self._new_embeddings = min(len(history), self._new_embeddings + 1)
            while self._new_embeddings >= self._windows:
                start = len(history) - self._new_embeddings
                window = history[start : start + self._windows]
                self._new_embeddings = max(0, self._new_embeddings - 1)
                scores.append(float(self._head.run(window)[0]))
        return scores

    def close(self) -> None:
        self._head.close()
