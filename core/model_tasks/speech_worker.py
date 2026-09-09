"""Standalone TTS child entry point; deliberately imports no vBot modules.

The parent owns serialization, paths, timeouts and process lifetime. Libraries
write diagnostics to stderr; stdout carries only bounded JSON control frames.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import sys
import wave
from pathlib import Path
from typing import Any


def resolve_snapshot(repo: str, files: list[str], tqdm_class: Any) -> str:
    """Reuse complete local files without HTTP; finish only missing downloads.

    Kept in this standalone loader so the server's STT adapters and isolated
    TTS SDK processes share one cache policy without importing vBot in the child.
    Hub's local snapshot lookup alone does not prove that a download is complete.
    """
    hub = importlib.import_module("huggingface_hub")
    missing = importlib.import_module("huggingface_hub.errors").LocalEntryNotFoundError
    try:
        cached = hub.snapshot_download(repo, local_files_only=True, allow_patterns=files)
    except missing as error:
        # Newer Hub versions expose the partial snapshot on this error; older
        # SDK stacks return its directory and leave completeness to the caller.
        cached = getattr(error, "snapshot_path", None)

    def complete(source: str) -> bool:
        return all(
            (Path(source) / name).is_file() and (Path(source) / name).stat().st_size > 0
            for name in files
        )

    if cached and complete(cached):
        return str(cached)
    # A partial snapshot must finish its existing revision, not combine it with
    # whatever the remote main branch happens to point at today.
    options = {"revision": Path(cached).name} if cached else {}
    source = hub.snapshot_download(repo, allow_patterns=files, tqdm_class=tqdm_class, **options)
    if not complete(source):
        raise OSError("Incomplete speech model download")
    return str(source)


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
    hub_progress: Any = importlib.import_module("huggingface_hub.utils.tqdm")
    tqdm = hub_progress.tqdm

    class DownloadProgress(tqdm):  # type: ignore[valid-type,misc]
        def __init__(self, *args, **kwargs):
            self._bytes = kwargs.get("unit") == "B"
            kwargs["file"] = io.StringIO()
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            if n > 0 and self._bytes:
                progress("downloading")
            return super().update(n)

    # Transformers 4.x requires Hub < 1. Its snapshot bar counts files;
    # the actual byte-transfer bars are created through this module instead.
    # This override is confined to the dedicated child, never the vBot server.
    hub_progress.tqdm = DownloadProgress

    progress("checking_model")
    if engine == "qwen3-tts":
        source = resolve_snapshot(
            options.get("model", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"),
            [
                "config.json",
                "generation_config.json",
                "merges.txt",
                "model.safetensors",
                "preprocessor_config.json",
                "tokenizer_config.json",
                "vocab.json",
                "speech_tokenizer/config.json",
                "speech_tokenizer/configuration.json",
                "speech_tokenizer/model.safetensors",
                "speech_tokenizer/preprocessor_config.json",
            ],
            DownloadProgress,
        )
    else:
        source = resolve_snapshot(
            "ResembleAI/chatterbox",
            [
                "ve.pt",
                "t3_mtl23ls_v3.safetensors",
                "s3gen.pt",
                "grapheme_mtl_merged_expanded_v1.json",
                "conds.pt",
                "Cangjie5_TC.json",
            ],
            DownloadProgress,
        )
    # SDK internals may probe the Hub even when given local paths. All required
    # files now exist, so keep this dedicated child offline for loading/inference.
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


def generate(model: Any, engine: str, text: str, options: dict[str, Any], output: str) -> None:
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
            wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def main() -> None:
    wire = sys.stdout
    sys.stdout = sys.stderr
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
            if not 0 < len(request["text"]) <= 5000:
                raise ValueError("Invalid text length")
            if model is None:
                model = load(engine, request["options"], progress)
            progress("synthesizing")
            generate(model, engine, request["text"], request["options"], request["output"])
            emit({"done": True})
        except Exception as error:
            emit({"error": type(error).__name__})
            return


if __name__ == "__main__":
    main()
