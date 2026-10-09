"""Standalone speech child entry point; imports vBot only for managed STT engines.

The parent owns serialization, paths, timeouts and process lifetime, and
installs every model before it starts a child: the first request carries the
model's directory as the ``model_path`` option, and the child never contacts
the Hub. A ``load`` request only loads the model and answers ``loaded``.
Libraries write diagnostics to stderr; stdout carries only bounded JSON
control frames.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any


def sdk(engine: str) -> Any:
    if engine == "qwen3-tts":
        return importlib.import_module("qwen_tts").Qwen3TTSModel
    if engine == "chatterbox":
        return importlib.import_module("chatterbox.mtl_tts").ChatterboxMultilingualTTS
    raise ValueError("Unknown engine")


def verify(engine: str, device: str) -> None:
    torch = importlib.import_module("torch")
    importlib.import_module("torchaudio")
    cls = sdk(engine)
    if engine == "chatterbox":
        import inspect

        assert "t3_model" in inspect.signature(cls.from_local).parameters
    if device == "cuda":
        x = torch.ones((16, 16), device="cuda")
        assert (x @ x).sum().item() == 4096


def load(engine: str, options: dict[str, Any], progress: Any) -> Any:
    torch = importlib.import_module("torch")
    device = options.get("device", "auto")
    if device == "auto":
        device = (
            "cuda"
            if torch.cuda.is_available()
            else ("mps" if torch.backends.mps.is_available() else "cpu")
        )
    source = options["model_path"]
    # SDK internals may probe the Hub even when given local paths. The installed
    # model holds every required file, so keep this child offline.
    os.environ["HF_HUB_OFFLINE"] = "1"
    constants: Any = importlib.import_module("huggingface_hub.constants")
    constants.HF_HUB_OFFLINE = True
    cls = sdk(engine)
    progress("loading")
    if engine == "qwen3-tts":
        dtype = torch.float32 if device == "cpu" else torch.float16
        if device == "cuda" and torch.cuda.is_bf16_supported():
            dtype = torch.bfloat16
        return cls.from_pretrained(
            source,
            device_map=device,
            dtype=dtype,
            attn_implementation="sdpa",
            local_files_only=True,
        )
    # The pinned SDK otherwise ignores its local mapping and starts another Hub
    # lookup in a nested cache. Resolve this one tokenizer asset from our snapshot.
    tokenizer: Any = importlib.import_module("chatterbox.models.tokenizers.tokenizer")

    def tokenizer_asset(*, repo_id: str, filename: str, **_kwargs: Any) -> str:
        if repo_id != "ResembleAI/chatterbox" or filename != "Cangjie5_TC.json":
            raise ValueError("Unexpected Chatterbox tokenizer asset")
        return str(Path(source) / filename)

    tokenizer.hf_hub_download = tokenizer_asset
    return cls.from_local(source, device=device, t3_model="v3")


def chunks(text: str) -> list[str]:
    """Bound model context by natural sentence/word boundaries, keeping all text."""
    result: list[str] = []
    remaining = text.strip()
    while len(remaining) > 240:
        ends = [match.end() for match in re.finditer(r"[.!?。！？]\s+", remaining[:241])]
        cut = ends[-1] if ends else remaining.rfind(" ", 0, 240)
        if cut < 1:
            cut = 240
        result.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        result.append(remaining)
    return result


def generate(
    model: Any,
    engine: str,
    text: str,
    options: dict[str, Any],
    output: str,
    chunk_ready: Callable[[int], None] | None = None,
) -> None:
    np = importlib.import_module("numpy")
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        for index, part in enumerate(chunks(text)):
            if engine == "qwen3-tts":
                waves, rate = model.generate_custom_voice(
                    text=part,
                    language=options.get("language", "Auto"),
                    speaker=options.get("voice", "Ryan"),
                    instruct=options.get("instructions", ""),
                    max_new_tokens=2048,
                )
                samples = np.asarray(waves[0]).reshape(-1)
            else:
                audio = model.generate(
                    part,
                    language_id=options.get("language", "en"),
                    exaggeration=options.get("exaggeration", 0.5),
                    cfg_weight=options.get("cfg_weight", 0.5),
                )
                samples = audio.detach().cpu().numpy().reshape(-1)
                rate = model.sr
            if not len(samples) or not np.isfinite(samples).all():
                raise ValueError("Invalid audio")
            if index == 0:
                wav.setframerate(rate)
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            wav.writeframes(pcm)
            if chunk_ready is not None:
                chunk_path = Path(output).with_name(f"chunk-{index}.wav")
                with wave.open(str(chunk_path), "wb") as chunk:
                    chunk.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                    chunk.writeframes(pcm)
                chunk_ready(index)


def main() -> None:
    wire = sys.stdout
    sys.stdout = sys.stderr
    if len(sys.argv) > 1 and sys.argv[1] == "--verify-stt":
        verify_stt(sys.argv[2])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--stt":
        run_stt(wire, sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--verify":
        verify(sys.argv[2], sys.argv[3])
        return
    engine = sys.argv[1]
    model = None

    def emit(event: dict[str, Any]) -> None:
        wire.write(json.dumps(event) + "\n")
        wire.flush()

    def progress(phase: str) -> None:
        emit({"phase": phase})

    for line in sys.stdin:
        try:
            request = json.loads(line)
            if model is None:
                model = load(engine, request["options"], progress)
            if request.get("load"):
                emit({"loaded": True})
                continue
            if not 0 < len(request["text"]) <= 5000:
                raise ValueError("Invalid text length")
            progress("synthesizing")
            generate(
                model,
                engine,
                request["text"],
                request["options"],
                request["output"],
                (lambda index: emit({"audio_chunk": index}))
                if request.get("stream_audio")
                else None,
            )
            emit({"done": True})
        except Exception as error:
            emit({"error": type(error).__name__})
            return


def _load_stt_source(app_root: str, module: str = "speech_local") -> Any:
    root = str(Path(app_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(f"core.model_tasks.{module}")


def verify_stt(app_root: str) -> None:
    setup = _load_stt_source(app_root, "speech_setup")
    assert setup._dependencies_available()
    importlib.import_module("av")


class _WireProgress:
    """Forward the in-process loader's phases to the parent as control frames."""

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    def update(self, phase: str) -> None:
        self._emit({"phase": phase})


def run_stt(wire: Any, engine_id: str, app_root: str) -> None:
    """Serve one STT engine: ``load`` requests answer ``loaded``, samples a result."""
    local = _load_stt_source(app_root)
    np = importlib.import_module("numpy")
    definition = next(
        item for item in local.builtin_speech_engines() if item.descriptor.id == engine_id
    )
    model = None

    def emit(event: dict[str, Any]) -> None:
        wire.write(json.dumps(event) + "\n")
        wire.flush()

    local._PROGRESS.set(_WireProgress(emit))
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if model is None:
                model = definition.create(request["options"])
            if request.get("load"):
                emit({"loaded": True})
                continue
            samples = np.frombuffer(
                base64.b64decode(request["samples"], validate=True), dtype="<f4"
            )
            emit({"phase": "transcribing"})
            result = model.transcribe(samples, request["options"])
            emit({"result": result.to_dict()})
        except Exception as error:
            emit({"error": type(error).__name__})
            return


if __name__ == "__main__":
    main()
