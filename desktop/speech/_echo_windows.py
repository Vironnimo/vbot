"""Windows WASAPI loopback and Core Audio default-endpoint discovery."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import numpy as np

from desktop.speech._echo_reference import LoopbackBackend, LoopbackStream, ReferenceBlockCallback

if TYPE_CHECKING:
    import ctypes
logger = logging.getLogger("vbot.desktop.speech.echo")
# PyAudioWPatch links its own PortAudio copy, independent of sounddevice's. Its
# initialisation is process-global and not thread-safe, so every stage's
# monitor thread initialises, opens, closes and terminates it under this lock.
_PORTAUDIO_WPATCH_LOCK = threading.Lock()


class _WasapiLoopbackStream:
    """PyAudioWPatch loopback stream; owns its PortAudio initialisation."""

    def __init__(self, audio: Any, stream: Any, name: str) -> None:
        self._audio = audio
        self._stream = stream
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def is_active(self) -> bool:
        return bool(self._stream.is_active())

    def close(self) -> None:
        with _PORTAUDIO_WPATCH_LOCK:
            try:
                self._stream.stop_stream()
                self._stream.close()
            finally:
                # Terminating makes the next open enumerate devices afresh.
                self._audio.terminate()


class WasapiLoopbackBackend:
    """Default playback endpoint loopback on Windows (PyAudioWPatch + Core Audio).

    WASAPI loopback delivers blocks only while something plays. Its callback
    timestamps are unusable, so arrival is ``perf_counter`` at callback start.
    """

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._endpoints: _DefaultPlaybackEndpoints | None = None

    def start(self) -> None:
        self._endpoints = _DefaultPlaybackEndpoints()

    def default_endpoint(self) -> str | None:
        if self._endpoints is None:
            raise OSError("playback endpoint detection is not started")
        return self._endpoints.current()

    def open_default(self, on_block: ReferenceBlockCallback) -> LoopbackStream:
        pyaudio = importlib.import_module("pyaudiowpatch")
        with _PORTAUDIO_WPATCH_LOCK:
            audio = pyaudio.PyAudio()
            try:
                device = audio.get_default_wasapi_loopback()
                rate = int(device["defaultSampleRate"])
                channels = int(device["maxInputChannels"])
                if rate < 8000 or channels < 1:
                    raise LookupError("loopback endpoint reports no usable format")
                stream = audio.open(
                    format=pyaudio.paFloat32,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(device["index"]),
                    frames_per_buffer=rate // 100,
                    stream_callback=self._callback(pyaudio, on_block, rate, channels),
                )
            except BaseException:
                audio.terminate()
                raise
        name = str(device.get("name", "")).removesuffix(" [Loopback]")
        return _WasapiLoopbackStream(audio, stream, f"{name} ({rate} Hz, {channels} ch)")

    def stop(self) -> None:
        endpoints, self._endpoints = self._endpoints, None
        if endpoints is not None:
            endpoints.close()

    def _callback(
        self, pyaudio: Any, on_block: ReferenceBlockCallback, rate: int, channels: int
    ) -> Callable[[bytes | None, int, Any, int], tuple[None, int]]:
        clock = self._clock
        overflow = int(pyaudio.paInputOverflow)
        keep_going = int(pyaudio.paContinue)
        failed = False

        def callback(
            data: bytes | None, frame_count: int, _time_info: Any, status: int
        ) -> tuple[None, int]:
            nonlocal failed
            arrival = clock()
            try:
                if data and frame_count > 0:
                    block = np.frombuffer(data, dtype=np.float32)
                    if block.size == frame_count * channels:
                        on_block(
                            block.reshape(frame_count, channels),
                            rate,
                            arrival,
                            bool(status & overflow),
                        )
            except Exception as error:  # never let an exception stop the audio thread
                if not failed:
                    failed = True
                    logger.warning("Echo reference block was dropped: %s", type(error).__name__)
            return None, keep_going

        return callback


class _DefaultPlaybackEndpoints:
    """Identity of the default playback endpoints through Core Audio (COM, ctypes).

    Created, queried and closed on one thread. The query costs well under a
    millisecond with the cached device enumerator.
    """

    _E_NOTFOUND = 0x80070490
    _RPC_E_CHANGED_MODE = 0x80010106
    _CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    _IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    _RENDER = 0
    _ROLES = (0, 1)  # console, multimedia: PortAudio and browsers pick either

    # Bound by __init__ past its Windows check, so declared for every platform.
    _ole32: ctypes.CDLL
    _get_default: Any  # WINFUNCTYPE prototypes of the Core Audio methods
    _get_id: Any
    _release: Any
    _uninitialize: bool
    _enumerator: ctypes.c_void_p

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Core Audio is only available on Windows")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        ole32.CLSIDFromString.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        self._ole32 = ole32
        self._get_default = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._get_id = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )
        self._release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

        result = ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        if result < 0 and result & 0xFFFFFFFF != self._RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx failed (0x{result & 0xFFFFFFFF:08X})")
        self._uninitialize = result >= 0
        self._enumerator = ctypes.c_void_p()
        try:
            clsid = self._guid(self._CLSID_ENUMERATOR)
            iid = self._guid(self._IID_ENUMERATOR)
            result = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 0x17, ctypes.byref(iid), ctypes.byref(self._enumerator)
            )
            if result < 0 or not self._enumerator.value:
                raise OSError(f"device enumerator unavailable (0x{result & 0xFFFFFFFF:08X})")
        except BaseException:
            if self._uninitialize:
                ole32.CoUninitialize()
            raise

    def current(self) -> str | None:
        ids = [self._default_id(role) for role in self._ROLES]
        if not any(ids):
            return None
        return "|".join(endpoint or "" for endpoint in ids)

    def close(self) -> None:
        enumerator, self._enumerator = self._enumerator, self._ctypes.c_void_p()
        if enumerator.value:
            self._method(enumerator, 2, self._release)(enumerator)
        if self._uninitialize:
            self._uninitialize = False
            self._ole32.CoUninitialize()

    def _default_id(self, role: int) -> str | None:
        ctypes = self._ctypes
        if not self._enumerator.value:
            raise OSError("device enumerator is closed")
        device = ctypes.c_void_p()
        get_default = self._method(self._enumerator, 4, self._get_default)
        result = get_default(self._enumerator, self._RENDER, role, ctypes.byref(device))
        if result & 0xFFFFFFFF == self._E_NOTFOUND:
            return None
        if result < 0 or not device.value:
            raise OSError(f"default playback endpoint query failed (0x{result & 0xFFFFFFFF:08X})")
        try:
            text = ctypes.c_void_p()
            result = self._method(device, 5, self._get_id)(device, ctypes.byref(text))
            if result < 0 or not text.value:
                raise OSError(f"playback endpoint id unavailable (0x{result & 0xFFFFFFFF:08X})")
            try:
                return str(ctypes.wstring_at(text.value))
            finally:
                self._ole32.CoTaskMemFree(text)
        finally:
            self._method(device, 2, self._release)(device)

    def _method(self, instance: Any, index: int, prototype: Any) -> Any:
        ctypes = self._ctypes
        table = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return prototype(table[index])

    def _guid(self, text: str) -> Any:
        guid = (self._ctypes.c_ubyte * 16)()
        result = self._ole32.CLSIDFromString(text, self._ctypes.byref(guid))
        if result < 0:
            raise OSError(f"invalid GUID {text}")
        return guid


def default_loopback() -> LoopbackBackend | None:
    if sys.platform != "win32" or importlib.util.find_spec("pyaudiowpatch") is None:
        return None
    return WasapiLoopbackBackend()
