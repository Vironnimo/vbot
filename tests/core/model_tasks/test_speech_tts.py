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

from core.model_tasks import local_setup, speech_worker
from core.model_tasks.constants import TASK_TEXT_TO_SPEECH
from core.model_tasks.local_targets import LocalTaskTargetDescriptor
from core.model_tasks.model_files import ModelFilesError, PinnedModel
from core.model_tasks.options import TaskModelOptionField
from core.model_tasks.speech_local import (
    _PROGRESS,
    LocalSpeechError,
    LocalSpeechExecutor,
    SpeechEngineDefinition,
    _TtsEngine,
)
from core.model_tasks.speech_models import SPEECH_MODELS
from core.model_tasks.speech_setup import LocalSpeechSetup
from core.model_tasks.speech_types import SpeechProgress, SpeechSynthesisResult


@pytest.mark.asyncio
async def test_installations_share_a_queue_and_cancel_waiting_jobs(tmp_path, monkeypatch):
    executor = LocalSpeechExecutor(engines_dir=tmp_path)
    first, second = (
        executor.setup_for(target) for target in ("local/qwen3-tts-1.7b", "local/chatterbox")
    )
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


class _Fetch:
    """Fake model download: reports half the bytes, then all, or fails with *error*."""

    def __init__(self) -> None:
        self.error = ""
        self.models: list[PinnedModel] = []
        self.seen: list[dict] = []
        self.setup: LocalSpeechSetup | None = None

    def __call__(self, model, directory, *, progress, cancelled, reuse=()) -> None:
        self.models.append(model)
        directory.mkdir(parents=True, exist_ok=True)
        progress(model.download_bytes // 2)
        if self.setup is not None:
            self.seen.append(self.setup.status())
        if self.error:
            raise ModelFilesError(self.error)
        progress(model.download_bytes)


@pytest.mark.asyncio
async def test_tts_sizes_share_their_environment_and_install_their_own_model(tmp_path, monkeypatch):
    executor = LocalSpeechExecutor(engines_dir=tmp_path)
    try:
        for name in ("qwen3-tts-1.7b", "qwen3-tts-0.6b", "chatterbox"):
            target = executor._definitions[name].descriptor
            assert target.task_types == (TASK_TEXT_TO_SPEECH,)
            assert not target.can_execute()
        large = executor.setup_for("local/qwen3-tts-1.7b")
        small = executor.setup_for("local/qwen3-tts-0.6b")
        commands = []

        async def command(arguments, **kwargs):
            commands.append(list(arguments))
            if "venv" in arguments:
                large.python.parent.mkdir(parents=True)
                large.python.touch()
            return 0

        fetch = _Fetch()
        fetch.setup = large
        for setup in (large, small):
            monkeypatch.setattr(setup, "_command", command)
            monkeypatch.setattr(setup, "_packaged", lambda: True)
        monkeypatch.setattr(local_setup, "fetch_model_files", fetch)
        assert large.status()["error"] == "python_missing"

        large.install()
        await large._task

        status = large.status()
        assert status["state"] == "ready" and not status["error"] and "progress" not in status
        assert executor._definitions["qwen3-tts-1.7b"].descriptor.can_execute()
        model = SPEECH_MODELS["qwen3-tts-1.7b"]
        total = model.download_bytes
        assert fetch.models == [model]
        assert fetch.seen == [
            {
                "state": "installing",
                "phase": "downloading",
                "error": "",
                "progress": {"completed": total // 2, "total": total},
            }
        ]
        receipt = json.loads((large.model_directory / "verified.json").read_text())
        assert receipt == {"repo": model.repo, "revision": model.revision}
        # The other size reuses the environment and reports only its missing model.
        assert small.status()["error"] == "model_missing"
        assert not executor._definitions["qwen3-tts-0.6b"].descriptor.can_execute()
        environment_commands = len(commands)
        fetch.error = "checksum_mismatch"
        fetch.setup = small
        small.install()
        await small._task

        assert len(commands) == environment_commands
        assert small.status() == {
            "state": "failed",
            "phase": "downloading",
            "error": "checksum_mismatch",
        }
        assert not (small.model_directory / "verified.json").exists()
        assert not executor._definitions["qwen3-tts-0.6b"].descriptor.can_execute()
        assert executor._definitions["qwen3-tts-1.7b"].descriptor.can_execute()
        large._state = "installing"
        assert not executor._definitions["qwen3-tts-1.7b"].descriptor.can_execute()
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
@pytest.mark.parametrize("packaged", [False, True])
async def test_managed_setup_never_installs_sdk_in_server_and_verifies_before_ready(
    tmp_path, monkeypatch, engine, packaged
):
    setup = LocalSpeechSetup(engine=engine, directory=tmp_path / engine)
    monkeypatch.setattr(setup, "_packaged", lambda: packaged)
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
    assert len(host_installs) == (0 if packaged else 1)
    if host_installs:
        assert "uv==0.12.11" in host_installs[0]
    assert any(cmd[:3] == [sys.executable, "-m", "uv"] for cmd in commands)
    assert commands[-1][:3] == [str(setup.python), "-I", "-B"]
    assert "--verify" in commands[-1]
    for cmd in commands:
        if "install" in cmd and "uv" in cmd:
            assert cmd[cmd.index("--python") + 1] == str(setup.python)
    recipe = setup._config()["tool"]["vbot"]["local-tts"][engine]
    package_stage = next(
        cmd for cmd in commands if "--only-binary=:all:" in cmd and recipe["packages"][0] in cmd
    )
    if engine == "chatterbox":
        reinstall = package_stage.index("--reinstall-package")
        assert package_stage[reinstall : reinstall + 2] == ["--reinstall-package", "chatterbox-tts"]
        assert reinstall < package_stage.index(recipe["packages"][0])
        source_stage = next(cmd for cmd in commands if recipe["source"] in cmd)
        assert "--no-deps" in source_stage
        assert source_stage[source_stage.index("--reinstall-package") + 1] == "chatterbox-tts"
    else:
        assert "--reinstall-package" not in package_stage
    # Incomplete setups and failed verification must not publish availability.
    (setup.directory / "verified.json").unlink()
    fresh = LocalSpeechSetup(engine=engine, directory=setup.directory)
    monkeypatch.setattr(fresh, "_command", AsyncMock(return_value=1))
    fresh.install()
    await fresh._task
    assert fresh.status()["state"] == "failed" and not fresh.available()
    await setup.aclose()
    await fresh.aclose()


@pytest.mark.parametrize("engine", ["qwen3-tts", "chatterbox"])
def test_worker_loads_the_installed_model_offline(monkeypatch, engine):
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
        "huggingface_hub.constants": constants,
        "chatterbox.models.tokenizers.tokenizer": tokenizer,
    }
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setattr(speech_worker.importlib, "import_module", modules.__getitem__)
    monkeypatch.setattr(speech_worker, "sdk", lambda _name: cls)
    assert speech_worker.load(engine, {"model_path": "installed"}, calls.append) == "model"
    assert constants.HF_HUB_OFFLINE
    if engine == "chatterbox":
        cls.from_local.assert_called_once_with("installed", device="cpu", t3_model="v3")
        assert Path(
            tokenizer.hf_hub_download(
                repo_id="ResembleAI/chatterbox", filename="Cangjie5_TC.json", cache_dir="ignored"
            )
        ) == Path("installed/Cangjie5_TC.json")
        with pytest.raises(ValueError):
            tokenizer.hf_hub_download(repo_id="other", filename="other")
    else:
        assert cls.from_pretrained.call_args.args == ("installed",)
        assert cls.from_pretrained.call_args.kwargs["local_files_only"] is True
    assert calls == ["loading"]


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
    monkeypatch.setattr("core.utils.processes.windows_taskkill_tree", kill_tree)
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
    engine = _TtsEngine(setup, {"model_path": "installed"})
    token = _PROGRESS.set(SpeechProgress())
    try:
        result = engine.synthesize("private test text", {})
        assert result.audio == output.getvalue()
        assert "private test text" not in str(popen.call_args)
        request = json.loads(process.stdin.write.call_args.args[0])
        # The installed model reaches the worker with every request.
        assert request["options"]["model_path"] == "installed"
        assert _PROGRESS.get().snapshot()["phase"] == "loading"
        assert not Path(json.loads(process.stdin.write.call_args.args[0])["output"]).exists()
    finally:
        _PROGRESS.reset(token)
        engine.close()
    assert popen.call_args.args[0][:3] == [str(setup.python), "-I", "-B"]
    assert kill_tree.call_count == 1
    process.wait.assert_called_once()
