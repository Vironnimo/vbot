"""Local speech worker protocol, audio decoding and native engine adapters."""

from __future__ import annotations

import base64
import gc
import importlib
import json
import os
import re
import signal
import subprocess
import tempfile
import wave
from collections.abc import Callable, Container, Generator, Iterator, Mapping, Sequence
from contextlib import closing, nullcontext, suppress
from pathlib import Path
from threading import Lock, Timer
from typing import Any, cast

from core.model_tasks._speech_context import (
    _AUDIO,
    _CANCELLATION,
    _PROGRESS,
    _SAMPLE_RATE,
    LocalSpeechError,
    LocalSpeechExecutionError,
)
from core.model_tasks.speech_input import iter_decoded_frames
from core.model_tasks.speech_setup import LocalSpeechSetup
from core.model_tasks.speech_types import (
    SpeechAudioChunk,
    SpeechSynthesisResult,
    SpeechTranscriptionResult,
)

_CHUNK_SAMPLES = 30 * _SAMPLE_RATE
_LOAD_DEADLINE_S = 900.0
_INFERENCE_DEADLINE_S = 600.0
_SYNTHESIS_DEADLINE_S = 1800.0


class _WaitingWorkers:
    """Speech worker children the parent is waiting on, so closing can end them.

    While a child loads, transcribes or synthesizes, its executor thread blocks
    until the child answers; only ending the child releases it before that.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._workers: set[_WorkerProcess] = set()
        self._closed = False

    def add(self, worker: _WorkerProcess) -> None:
        with self._lock:
            if not self._closed:
                self._workers.add(worker)
                return
        worker.end("closed")

    def discard(self, worker: _WorkerProcess) -> None:
        with self._lock:
            self._workers.discard(worker)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers, self._workers = self._workers, set()
        for worker in workers:
            worker.end("closed")


class _WorkerProcess:
    """A ``speech_worker.py`` child the parent talks to in JSON lines.

    The child is ended with its process tree when a request passes its deadline,
    a cancellation's grace runs out (``abort``) or the executor closes; the
    request waiting for it then fails.
    """

    _closed_message: str  # The error of a request whose child closing ended.

    def __init__(self, python: Path, arguments: Sequence[str], waiting: _WaitingWorkers) -> None:
        from core.utils.processes import subprocess_creation_flags

        self._waiting = waiting
        self._ending = Lock()
        self._ended = ""  # Why the parent ended the child, if it did.
        self._process = subprocess.Popen(
            [
                str(python),
                "-I",
                "-B",
                str(Path(__file__).with_name("speech_worker.py")),
                *arguments,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=subprocess_creation_flags(),
            start_new_session=os.name != "nt",
            env={**os.environ, "PYTHONUTF8": "1", "TOKENIZERS_PARALLELISM": "false"},
        )

    def abort(self) -> None:
        """End the child from another thread; the running request fails."""
        self.end("aborted")

    def end(self, reason: str) -> None:
        """Kill the child's process tree; the first reason names the failure."""
        with self._ending:
            self._ended = self._ended or reason
        _kill_speech_process(self._process)

    def _exchange(
        self,
        request: Mapping[str, Any],
        answer: str,
        deadline: float,
        phases: Container[str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """Send one request line and return the child's ``answer`` field.

        Progress forwards the child's phases (only *phases* when given). The child
        is ended once *deadline* seconds pass without that answer.
        """
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        timer = Timer(deadline, self.end, ("deadline",))
        timer.daemon = True
        self._waiting.add(self)
        timer.start()
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            while line := process.stdout.readline(4096):
                event = json.loads(line)
                if event.get("error"):
                    raise RuntimeError(event["error"])
                if on_event is not None:
                    on_event(event)
                if (
                    (phase := event.get("phase"))
                    and (phases is None or phase in phases)
                    and (progress := _PROGRESS.get()) is not None
                ):
                    progress.update(phase)
                if (payload := event.get(answer)) is not None:
                    return payload
        except OSError, ValueError:
            if not self._ended:
                raise  # A broken pipe or line of a child ended here is reported below.
        finally:
            timer.cancel()
            self._waiting.discard(self)
        if self._ended == "deadline":
            raise TimeoutError(f"Speech worker did not answer within {deadline:.0f} seconds")
        if self._ended == "closed":
            raise LocalSpeechError(self._closed_message)
        raise RuntimeError("Speech worker exited")

    def _load(self, options: Mapping[str, Any]) -> None:
        """Have the child load its model; a child that fails to is closed."""
        try:
            cancellation = _CANCELLATION.get()
            with cancellation.running(self) if cancellation is not None else nullcontext():
                self._exchange({"load": True, "options": dict(options)}, "loaded", _LOAD_DEADLINE_S)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        _close_speech_process(self._process)


class _TtsEngine(_WorkerProcess):
    """A cached SDK process with fixed entry point and parent-owned output paths.

    Construction returns once the child reports the model loaded, so the
    executor's load boundary (logs, progress, preloading) covers the real load.
    """

    _closed_message = "Local speech synthesis is closed. Restart the vBot server before retrying."

    def __init__(
        self, setup: LocalSpeechSetup, waiting: _WaitingWorkers, options: Mapping[str, Any]
    ) -> None:
        super().__init__(setup.python, [setup.engine], waiting)
        self._load(options)

    def synthesize(self, text: str, options: Mapping[str, Any]) -> SpeechSynthesisResult:
        with tempfile.TemporaryDirectory(prefix="vbot-tts-") as directory:
            output = Path(directory) / "speech.wav"
            callback = _AUDIO.get()
            next_chunk = 0
            total_bytes = 0

            def chunk_ready(event: dict[str, Any]) -> None:
                nonlocal next_chunk, total_bytes
                if "audio_chunk" not in event:
                    return
                index = event["audio_chunk"]
                if callback is None or type(index) is not int or index != next_chunk:
                    raise ValueError("Invalid speech audio chunk")
                path = output.with_name(f"chunk-{index}.wav")
                size = path.stat().st_size
                total_bytes += size
                if not size > 44 or total_bytes > 64 * 1024 * 1024:
                    raise ValueError("Invalid speech audio chunk size")
                with wave.open(str(path), "rb") as wav:
                    if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                        raise ValueError("Invalid speech audio chunk format")
                    chunk = SpeechAudioChunk(wav.readframes(wav.getnframes()), wav.getframerate())
                path.unlink()
                callback(chunk)
                next_chunk += 1

            try:
                self._exchange(
                    {
                        "text": text,
                        "options": dict(options),
                        "output": str(output),
                        "stream_audio": callback is not None,
                    },
                    "done",
                    _SYNTHESIS_DEADLINE_S,
                    ("loading", "synthesizing"),
                    chunk_ready,
                )
            except BaseException:
                # A failed/cancelled playback consumer can leave the child still
                # generating into its WAV. Reap it before Windows removes files.
                self.close()
                raise
            if not 44 < output.stat().st_size <= 64 * 1024 * 1024:
                raise ValueError("Invalid output size")
            return SpeechSynthesisResult(output.read_bytes(), "audio/wav", "wav")


class _ManagedSttEngine(_WorkerProcess):
    """Keep the optional ML stack in a managed child interpreter.

    Construction returns once the child reports the model loaded, so the
    executor's load boundary (logs, progress, preloading) covers the real load.
    """

    _closed_message = "Local speech recognition is closed. Restart the vBot server before retrying."

    def __init__(
        self,
        setup: LocalSpeechSetup,
        app_root: Path,
        engine: str,
        waiting: _WaitingWorkers,
        options: Mapping[str, Any],
    ) -> None:
        super().__init__(setup.python, ["--stt", engine, str(app_root)], waiting)
        self._load(options)

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        payload = self._exchange(
            {
                "samples": base64.b64encode(samples.astype("<f4").tobytes()).decode("ascii"),
                "options": dict(options),
            },
            "result",
            _INFERENCE_DEADLINE_S,
        )
        return SpeechTranscriptionResult(
            text=payload["text"],
            language=payload.get("language"),
            segments=tuple(payload.get("segments", ())),
            usage=payload.get("usage"),
        )


def _kill_speech_process(process: subprocess.Popen[str]) -> None:
    """Kill a speech child's whole process tree; its pipes stay with their owner."""
    from core.utils.processes import windows_taskkill_tree

    if process.poll() is None:
        if os.name == "nt":
            if not windows_taskkill_tree(process.pid):
                with suppress(ProcessLookupError):
                    process.kill()
        else:
            with suppress(ProcessLookupError):
                cast(Any, os).killpg(process.pid, cast(Any, signal).SIGKILL)
        process.wait()


def _close_speech_process(process: subprocess.Popen[str]) -> None:
    _kill_speech_process(process)
    if process.stdin:
        process.stdin.close()
    if process.stdout:
        process.stdout.close()


def _audio_chunks(
    audio: bytes, *, check_cancel: Callable[[], None] | None = None
) -> Generator[tuple[int, Any]]:
    """Decode original audio once into bounded mono 16 kHz inference chunks."""
    import numpy as np

    pending = np.empty(_CHUNK_SAMPLES, dtype=np.float32)
    filled = 0
    offset = 0
    with closing(
        iter_decoded_frames(
            audio, sample_rate=_SAMPLE_RATE, sample_format="fltp", check_cancel=check_cancel
        )
    ) as frames:
        for frame in frames:
            samples = frame.to_ndarray().reshape(-1)
            while len(samples):
                if check_cancel is not None:
                    check_cancel()
                count = min(len(samples), _CHUNK_SAMPLES - filled)
                pending[filled : filled + count] = samples[:count]
                filled += count
                samples = samples[count:]
                if filled < _CHUNK_SAMPLES:
                    continue
                # Prefer a quiet boundary in the final second. Every sample is
                # retained exactly once, including speech with no useful pause.
                window = 320
                tail = pending[_CHUNK_SAMPLES - _SAMPLE_RATE : _CHUNK_SAMPLES].reshape(-1, window)
                quietest = int(np.argmin(np.mean(tail * tail, axis=1)))
                cut = _CHUNK_SAMPLES - _SAMPLE_RATE + (quietest + 1) * window
                yield offset, pending[:cut]
                offset += cut
                # Yielded arrays must remain stable. Copy only the unconsumed
                # tail into a fresh bounded buffer, never the growing prefix.
                remainder = pending[cut:filled]
                pending = np.empty(_CHUNK_SAMPLES, dtype=np.float32)
                filled = len(remainder)
                pending[:filled] = remainder
        if filled:
            yield offset, pending[:filled]


class _TransformersEngine:
    """Shared model loading; model-specific preparation and decoding stay below."""

    model_class = ""

    def __init__(self, options: Mapping[str, Any]) -> None:
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")

        self._torch = torch
        self._model: Any = None
        self._processor: Any = None
        device = options.get("device") or "auto"
        if device == "auto":
            device = (
                "cuda"
                if torch.cuda.is_available()
                else ("mps" if torch.backends.mps.is_available() else "cpu")
            )
        if device == "cuda" and not torch.cuda.is_available():
            raise LocalSpeechExecutionError(
                "The selected GPU is unavailable to PyTorch. "
                "Select CPU or install a GPU-enabled PyTorch build."
            )
        if device == "mps" and not torch.backends.mps.is_available():
            raise LocalSpeechExecutionError(
                "The selected Apple GPU is unavailable to PyTorch. "
                "Select CPU or a supported device in the speech-to-text options."
            )
        precision = options.get("dtype") or "auto"
        if precision == "auto":
            precision = "float32"
            if device == "cuda":
                precision = "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
        # The executor supplies the installed model's directory unless the binding names one.
        path = Path((options.get("model_path") or "").strip()).expanduser()
        if not str(path) or str(path) == "." or not path.is_dir():
            raise LocalSpeechExecutionError(
                "The model directory does not exist on the vBot server. "
                "Correct Model directory in the speech-to-text options or leave it empty "
                "to use the installed model."
            )
        source = str(path)
        load_options = {
            "local_files_only": True,
            "trust_remote_code": False,
        }
        try:
            if (progress := _PROGRESS.get()) is not None:
                progress.update("loading")
            self._processor = transformers.AutoProcessor.from_pretrained(source, **load_options)
            self._model = (
                getattr(transformers, self.model_class)
                .from_pretrained(source, dtype=getattr(torch, precision), **load_options)
                .to(device)
                .eval()
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._model = None
        self._processor = None
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
        if self._torch.backends.mps.is_available():
            self._torch.mps.empty_cache()


class _QwenEngine(_TransformersEngine):
    model_class = "AutoModelForMultimodalLM"

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        language = (options.get("language") or "").strip() or None
        if language == "auto":
            language = None
        inputs = self._processor.apply_transcription_request(
            audio=samples,
            language=language,
            prompt=options.get("prompt") or None,
            audio_kwargs={"sampling_rate": _SAMPLE_RATE},
        ).to(self._model.device, self._model.dtype)
        with self._torch.inference_mode():
            output = self._model.generate(**inputs, max_new_tokens=1024, do_sample=False)
        generated = output[:, inputs["input_ids"].shape[1] :]
        if generated.shape[1] >= 1024:
            raise LocalSpeechExecutionError(
                "The transcription reached the model output limit. Retry with a shorter recording."
            )
        parsed = self._processor.decode(generated, return_format="parsed")[0]
        return SpeechTranscriptionResult(
            text=parsed["transcription"], language=language or parsed.get("language")
        )


class _ParakeetEngine(_TransformersEngine):
    model_class = "AutoModelForTDT"

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        inputs = self._processor(samples, sampling_rate=_SAMPLE_RATE, return_tensors="pt").to(
            self._model.device, self._model.dtype
        )
        with self._torch.inference_mode():
            output = self._model.generate(**inputs, return_dict_in_generate=True)
        texts = self._processor.decode(output.sequences, skip_special_tokens=True)
        return SpeechTranscriptionResult(text=texts[0])


class _NemotronEngine(_TransformersEngine):
    model_class = "AutoModelForRNNT"

    def transcribe(self, samples: Any, options: Mapping[str, Any]) -> SpeechTranscriptionResult:
        language = (options.get("language") or "").strip() or "auto"
        inputs = self._processor(
            samples, sampling_rate=_SAMPLE_RATE, language=language, return_tensors="pt"
        ).to(self._model.device, self._model.dtype)
        # Compute features once, then let native streaming generation retain the
        # encoder/decoder caches across fixed-size mel chunks within this recording.
        lookahead = 6
        self._processor.set_num_lookahead_tokens(lookahead)
        first = self._processor.num_mel_frames_first_audio_chunk
        subsequent = self._processor.num_mel_frames_per_audio_chunk
        frames = int(inputs["attention_mask"].sum().item())
        features = inputs["input_features"][:, :frames]
        chunk_count = 1 + max(0, frames - first + subsequent - 1) // subsequent

        def chunks() -> Iterator[Any]:
            start = 0
            for index in range(chunk_count):
                size = first if index == 0 else subsequent
                chunk = features[:, start : start + size]
                # The final short chunk must be padded, never discarded. Native
                # streaming rejects any chunk that does not have its exact size.
                yield self._torch.nn.functional.pad(chunk, (0, 0, 0, size - chunk.shape[1]))
                start += size

        # RNNT emits blanks as well as text; bound by the maximum emissions per
        # encoder frame so the final audio cannot be truncated by a text-token cap.
        limit = chunk_count * (lookahead + 1) * self._model.max_symbols_per_step + 1
        with self._torch.inference_mode():
            output = self._model.generate(
                input_features=chunks(),
                prompt_ids=inputs["prompt_ids"],
                num_lookahead_tokens=lookahead,
                max_new_tokens=limit,
                return_dict_in_generate=True,
            )
        text = self._processor.decode(output.sequences, skip_special_tokens=True)[0]
        raw = self._processor.decode(output.sequences, skip_special_tokens=False)[0]
        detected = re.findall(r"<([a-z]{2,3}-[A-Z]{2})>", raw)
        return SpeechTranscriptionResult(
            text=text,
            language=language if language != "auto" else (detected[-1] if detected else None),
        )
