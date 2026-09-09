"""Local synthesis contracts, independent of SDK installs and model downloads."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from core.model_tasks import speech_worker
from core.model_tasks.constants import TASK_TEXT_TO_SPEECH
from core.model_tasks.local_targets import LocalTaskTargetDescriptor
from core.model_tasks.options import TaskModelOptionField
from core.model_tasks.speech_local import (
    _PROGRESS,
    LocalSpeechError,
    LocalSpeechExecutor,
    SpeechEngineDefinition,
    _TtsEngine,
)
from core.model_tasks.speech_setup import LocalSpeechSetup
from core.model_tasks.speech_types import SpeechProgress, SpeechSynthesisResult


@pytest.mark.asyncio
async def test_installations_share_a_queue_and_cancel_waiting_jobs(tmp_path, monkeypatch):
    executor = LocalSpeechExecutor(engines_dir=tmp_path)
    first, second = executor.tts_setups.values()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block():
        entered.set()
        await release.wait()

    next_install = AsyncMock()
    monkeypatch.setattr(first, "_run_install", block)
    monkeypatch.setattr(second, "_run_install", next_install)
    first.install()
    await entered.wait()
    second.install()
    await asyncio.sleep(0)
    assert second.status()["phase"] == "queued"
    next_install.assert_not_called()
    await second.aclose()
    assert second.status()["error"] == "interrupted"
    release.set()
    await executor.aclose()


@pytest.mark.asyncio
async def test_tts_catalog_is_lazy_and_each_setup_has_its_own_availability(tmp_path):
    executor = LocalSpeechExecutor(engines_dir=tmp_path)
    try:
        for name in ("qwen3-tts", "chatterbox"):
            target = executor._definitions[name].descriptor
            assert target.task_types == (TASK_TEXT_TO_SPEECH,)
            assert not target.can_execute()
        setup = executor.setup_for("local/qwen3-tts")
        setup.python.parent.mkdir(parents=True)
        setup.python.touch()
        (setup.directory / "verified.json").write_text(setup._recipe_key())
        assert executor._definitions["qwen3-tts"].descriptor.can_execute()
        assert not executor._definitions["chatterbox"].descriptor.can_execute()
        setup._state = "installing"
        assert not executor._definitions["qwen3-tts"].descriptor.can_execute()
        with pytest.raises(ValueError):
            executor.setup_for("local/../../escape")
        assert executor.setup_for("local/parakeet") is executor.setup
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_synthesis_substitution_cache_voice_changes_and_stt_switch():
    events = []

    class Engine:
        def synthesize(self, text, options):
            events.append((text, options["voice"]))
            assert _PROGRESS.get().snapshot()["phase"] == "synthesizing"
            return SpeechSynthesisResult(b"audio", "audio/wav", "wav")

        def close(self):
            events.append("close")

    factory = Mock(side_effect=lambda _options: Engine())
    entry = SpeechEngineDefinition(
        LocalTaskTargetDescriptor(
            id="custom",
            label="Custom",
            task_types=(TASK_TEXT_TO_SPEECH,),
            availability=lambda: True,
            option_fields=(TaskModelOptionField("voice", "text", "Voice", default="one"),),
        ),
        factory,
        (),
    )
    executor = LocalSpeechExecutor(engines=[entry])
    try:
        for voice in ("one", "two"):
            result = await executor.synthesize(
                "custom", "hello", options={"voice": voice}, progress=SpeechProgress()
            )
            assert result.audio == b"audio"
        assert factory.call_count == 1
        with pytest.raises(LocalSpeechError):
            await executor.transcribe(
                "custom", b"audio", filename="a.wav", media_type="audio/wav", options={}
            )
        with pytest.raises(LocalSpeechError):
            await executor.synthesize("custom", "x" * 5001, options={})
        await executor.release_memory("local/custom")
        assert events == [("hello", "one"), ("hello", "two"), "close"]
        assert _PROGRESS.get() is None
    finally:
        await executor.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["qwen3-tts", "chatterbox"])
async def test_managed_setup_never_installs_sdk_in_server_and_verifies_before_ready(
    tmp_path, monkeypatch, engine
):
    setup = LocalSpeechSetup(engine=engine, directory=tmp_path / engine)
    commands = []

    async def command(arguments, **kwargs):
        commands.append(list(arguments))
        if "venv" in arguments:
            setup.python.parent.mkdir(parents=True)
            setup.python.touch()
        return 0

    monkeypatch.setattr(setup, "_command", command)
    monkeypatch.setattr("core.model_tasks.speech_setup.shutil.which", lambda _name: None)
    setup.install()
    await setup._task
    assert setup.status()["state"] == "ready" and setup.available()
    host_installs = [cmd for cmd in commands if cmd[:3] == [sys.executable, "-m", "pip"]]
    assert len(host_installs) == 1
    assert "uv==0.12.11" in host_installs[0]
    assert commands[-1][0] == str(setup.python) and "--verify" in commands[-1]
    for cmd in commands:
        if "install" in cmd and "uv" in cmd:
            assert cmd[cmd.index("--python") + 1] == str(setup.python)
    if engine == "chatterbox":
        assert any("5de7a54" in " ".join(cmd) and "--no-deps" in cmd for cmd in commands)
    # Stale recipes and failed verification must not publish availability.
    (setup.directory / "verified.json").write_text("stale")
    fresh = LocalSpeechSetup(engine=engine, directory=setup.directory)
    monkeypatch.setattr(fresh, "_command", AsyncMock(return_value=1))
    fresh.install()
    await fresh._task
    assert fresh.status()["state"] == "failed" and not fresh.available()
    await setup.aclose()
    await fresh.aclose()


@pytest.mark.parametrize("engine", ["qwen3-tts", "chatterbox"])
def test_worker_uses_complete_cache_and_keeps_sdk_loading_offline(monkeypatch, engine):
    calls = []
    cls = SimpleNamespace(
        from_local=Mock(return_value="model"), from_pretrained=Mock(return_value="model")
    )
    torch = SimpleNamespace(
        float32="float32",
        cuda=SimpleNamespace(is_available=lambda: False),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )
    constants = SimpleNamespace(HF_HUB_OFFLINE=False)
    tokenizer = SimpleNamespace()
    modules = {
        "torch": torch,
        "huggingface_hub.utils.tqdm": SimpleNamespace(tqdm=object),
        "huggingface_hub.constants": constants,
        "chatterbox.models.tokenizers.tokenizer": tokenizer,
    }
    snapshot = Mock(return_value="cached")
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setattr(speech_worker.importlib, "import_module", modules.__getitem__)
    monkeypatch.setattr(speech_worker, "resolve_snapshot", snapshot)
    monkeypatch.setattr(speech_worker, "sdk", lambda _name: cls)
    assert speech_worker.load(engine, {}, calls.append) == "model"
    assert constants.HF_HUB_OFFLINE
    if engine == "chatterbox":
        cls.from_local.assert_called_once_with("cached", device="cpu", t3_model="v3")
        assert "t3_mtl23ls_v3.safetensors" in snapshot.call_args.args[1]
        assert Path(
            tokenizer.hf_hub_download(
                repo_id="ResembleAI/chatterbox", filename="Cangjie5_TC.json", cache_dir="ignored"
            )
        ) == Path("cached/Cangjie5_TC.json")
        with pytest.raises(ValueError):
            tokenizer.hf_hub_download(repo_id="other", filename="other")
    else:
        assert cls.from_pretrained.call_args.kwargs["local_files_only"] is True
        assert "speech_tokenizer/model.safetensors" in snapshot.call_args.args[1]
    assert calls == ["checking_model", "loading"]


def test_disabled_download_bars_still_report_real_transfer(monkeypatch):
    phases = []

    class DisabledBar:
        def __init__(self, **kwargs):
            pass

        def update(self, n):
            return None

    bars = SimpleNamespace(tqdm=DisabledBar)

    def snapshot(*args, **kwargs):
        bar = bars.tqdm(unit="B", disable=True)
        bar.update(0)
        bar.update(128)
        bars.tqdm(unit="files", disable=True).update(1)
        return "cached"

    modules = {
        "torch": SimpleNamespace(),
        "huggingface_hub.utils.tqdm": bars,
        "huggingface_hub.constants": SimpleNamespace(),
        "chatterbox.models.tokenizers.tokenizer": SimpleNamespace(),
    }
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setattr(speech_worker.importlib, "import_module", modules.__getitem__)
    monkeypatch.setattr(speech_worker, "resolve_snapshot", snapshot)
    monkeypatch.setattr(speech_worker, "sdk", lambda _name: SimpleNamespace(from_local=Mock()))
    speech_worker.load("chatterbox", {"device": "cpu"}, phases.append)
    assert phases == ["checking_model", "downloading", "loading"]


@pytest.mark.parametrize(
    "initial", ["complete", "missing", "partial", "partial_error", "empty_file"]
)
def test_snapshot_reuses_local_files_and_finishes_only_missing_revision(
    tmp_path, monkeypatch, initial
):
    source = tmp_path / ("a" * 40)
    source.mkdir()
    files = ["config.json", "model.safetensors", "tokenizer/config.json"]
    sentinel = b"existing weights"
    if initial != "missing":
        (source / "model.safetensors").write_bytes(sentinel)
    if initial in ("complete", "empty_file"):
        for name in files:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"asset")
    if initial == "empty_file":
        (source / "config.json").write_bytes(b"")

    class MissingError(Exception):
        pass

    calls = []

    def download(repo, **kwargs):
        calls.append(kwargs)
        assert kwargs["allow_patterns"] == files
        if kwargs.get("local_files_only"):
            if initial == "partial_error" and len(calls) == 1:
                error = MissingError()
                error.snapshot_path = str(source)
                raise error
            if initial == "missing" and len(calls) == 1:
                raise MissingError()
            return str(source)
        assert kwargs["allow_patterns"] == files
        assert kwargs.get("revision") == (source.name if initial != "missing" else None)
        for name in files:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or not path.stat().st_size:
                path.write_bytes(b"downloaded")
        return str(source)

    modules = {
        "huggingface_hub": SimpleNamespace(snapshot_download=download),
        "huggingface_hub.errors": SimpleNamespace(LocalEntryNotFoundError=MissingError),
    }
    monkeypatch.setattr(speech_worker.importlib, "import_module", modules.__getitem__)
    for _ in range(2):
        assert speech_worker.resolve_snapshot("owner/model", files, object) == str(source)
    assert sum(not call.get("local_files_only") for call in calls) == (
        0 if initial == "complete" else 1
    )
    if initial != "missing":
        assert (source / "model.safetensors").read_bytes() == sentinel


def test_incomplete_download_and_unrelated_cache_errors_are_not_treated_as_ready(
    tmp_path, monkeypatch
):
    class MissingError(Exception):
        pass

    download = Mock(return_value=str(tmp_path))
    modules = {
        "huggingface_hub": SimpleNamespace(snapshot_download=download),
        "huggingface_hub.errors": SimpleNamespace(LocalEntryNotFoundError=MissingError),
    }
    monkeypatch.setattr(speech_worker.importlib, "import_module", modules.__getitem__)
    with pytest.raises(OSError):
        speech_worker.resolve_snapshot("owner/model", ["required.file"], object)
    assert download.call_count == 2
    download.reset_mock(side_effect=True)
    download.side_effect = PermissionError()
    with pytest.raises(PermissionError):
        speech_worker.resolve_snapshot("owner/model", ["required.file"], object)
    download.assert_called_once_with(
        "owner/model", local_files_only=True, allow_patterns=["required.file"]
    )


@pytest.mark.parametrize("engine", ["qwen3-tts", "chatterbox"])
def test_worker_generates_playable_wav_and_keeps_every_text_chunk(tmp_path, engine):
    text = "Dies ist ein Satz. " * 35
    pieces = speech_worker.chunks(text)
    assert " ".join(pieces).split() == text.split()
    assert max(map(len, pieces)) <= 240
    samples = np.zeros(240, dtype=np.float32)
    model = SimpleNamespace(
        generate_custom_voice=Mock(return_value=([samples], 24000)),
        generate=Mock(
            return_value=SimpleNamespace(
                detach=lambda: SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: samples))
            )
        ),
        sr=24000,
    )
    output = tmp_path / "voice.wav"
    speech_worker.generate(model, engine, text, {"language": "de"}, str(output))
    with wave.open(str(output)) as audio:
        assert audio.getframerate() == 24000 and audio.getnchannels() == 1
        assert audio.getnframes() == 240 * len(pieces)
    calls = (
        model.generate_custom_voice.call_args_list
        if engine == "qwen3-tts"
        else model.generate.call_args_list
    )
    assert len(calls) == len(pieces)


def test_process_adapter_keeps_audio_and_text_off_arguments_and_cleans_up(tmp_path, monkeypatch):
    from core.model_tasks import speech_local

    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 100)
    kill_tree = Mock(return_value=True)
    monkeypatch.setattr("core.tools.process_manager.windows_taskkill_tree", kill_tree)
    if sys.platform != "win32":
        monkeypatch.setattr("os.killpg", kill_tree)
    process = Mock()
    process.stdout.readline.side_effect = ['{"phase":"loading"}\n', '{"done":true}\n']
    process.poll.return_value = None

    def write(line):
        request = json.loads(line)
        Path(request["output"]).write_bytes(output.getvalue())

    process.stdin.write.side_effect = write
    popen = Mock(return_value=process)
    monkeypatch.setattr(speech_local.subprocess, "Popen", popen)
    setup = LocalSpeechSetup(engine="qwen3-tts", directory=tmp_path)
    engine = _TtsEngine(setup, {})
    token = _PROGRESS.set(SpeechProgress())
    try:
        result = engine.synthesize("private test text", {})
        assert result.audio == output.getvalue()
        assert "private test text" not in str(popen.call_args)
        assert _PROGRESS.get().snapshot()["phase"] == "loading"
        assert not Path(json.loads(process.stdin.write.call_args.args[0])["output"]).exists()
    finally:
        _PROGRESS.reset(token)
        engine.close()
    assert kill_tree.call_count == 1
    process.wait.assert_called_once()
