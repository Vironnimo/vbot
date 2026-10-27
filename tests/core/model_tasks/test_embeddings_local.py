"""Local embedding engine contracts with a fake serving child; no downloads, no ML stack."""

from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import os
import sys
import threading
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.model_tasks import _worker_process, embedding_worker, embeddings_local, local_setup
from core.model_tasks.embeddings_local import (
    LocalEmbeddingError,
    LocalEmbeddingExecutor,
    LocalEmbeddingInputTooLongError,
    LocalEmbeddingModel,
    LocalEmbeddingUnavailableError,
    _WorkerProcess,
    builtin_local_embedding_models,
    default_threads,
)
from core.model_tasks.model_files import ModelFilesError

GRANITE, HARRIER = builtin_local_embedding_models()


def install_fake(engines: Path, model: LocalEmbeddingModel) -> None:
    """Leave the files a completed setup leaves: environment, model and receipts."""
    environment = engines / "onnx"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    (environment / "verified.json").write_text("{}\n", encoding="utf-8")
    directory = engines / "models" / model.id / model.revision
    (directory / model.graph).parent.mkdir(parents=True, exist_ok=True)
    (directory / model.graph).touch()
    (directory / "verified.json").write_text("{}\n", encoding="utf-8")


class FakeChild:
    """Records loads and requests; a text's vector is ``[len(text), 1.0]``."""

    events: list[Any] = []
    gate: dict[str, threading.Event] = {}
    fail: set[str] = set()

    def __init__(self, python: Path, spec: Mapping[str, Any], threads: int) -> None:
        self.closed = False
        FakeChild.events.append(("load", Path(spec["graph"]).name, threads))

    def embed(self, texts: list[str]) -> tuple[list[list[float]], list[int]]:
        FakeChild.events.append(list(texts))
        for text in texts:
            if (gate := FakeChild.gate.get(text)) is not None:
                gate.set()
                assert FakeChild.gate[f"{text}:proceed"].wait(5)
        for index, text in enumerate(texts):
            if text.startswith("long"):
                raise LocalEmbeddingInputTooLongError(index, 3000, 2048)
            if text in FakeChild.fail:
                raise RuntimeError("child crashed")
        return [[float(len(text)), 1.0] for text in texts], [len(text) for text in texts]

    def kill(self) -> None:
        FakeChild.events.append("kill")

    def close(self) -> None:
        self.closed = True
        FakeChild.events.append("close")


@pytest.fixture(autouse=True)
def _reset_fake_child() -> None:
    FakeChild.events = []
    FakeChild.gate = {}
    FakeChild.fail = set()


@asynccontextmanager
async def running(engines: Path) -> AsyncIterator[LocalEmbeddingExecutor]:
    executor = LocalEmbeddingExecutor(engines_dir=engines, child_factory=FakeChild)
    try:
        yield executor
    finally:
        await executor.aclose()


@pytest.mark.asyncio
async def test_documents_yield_to_a_waiting_query_after_each_text(tmp_path: Path) -> None:
    async with running(tmp_path) as executor:
        install_fake(tmp_path, GRANITE)
        started, proceed = threading.Event(), threading.Event()
        FakeChild.gate = {"doc one": started, "doc one:proceed": proceed}

        documents = asyncio.create_task(
            executor.embed(GRANITE.id, ["doc one", "doc two", "doc three"], query=False, options={})
        )
        assert await asyncio.to_thread(started.wait, 5)
        query = asyncio.create_task(executor.embed(GRANITE.id, ["q?"], query=True, options={}))
        await asyncio.sleep(0)
        assert executor.memory_status()["models"][0]["busy"]
        proceed.set()

        embedded, asked = await documents, await query

        # The query runs as soon as the running document finishes, before the rest.
        assert FakeChild.events[1:] == [["doc one"], ["q?"], ["doc two"], ["doc three"]]
        assert FakeChild.events[0][:2] == ("load", "model_quint8_avx2.onnx")
        assert embedded.vectors == ([7.0, 1.0], [7.0, 1.0], [9.0, 1.0])
        assert (embedded.tokens, asked.tokens) == ((7, 7, 9), (2,))


@pytest.mark.asyncio
async def test_over_length_text_is_reported_with_its_request_index(tmp_path: Path) -> None:
    async with running(tmp_path) as executor:
        install_fake(tmp_path, GRANITE)

        with pytest.raises(LocalEmbeddingInputTooLongError) as error:
            await executor.embed(GRANITE.id, ["a", "b", "long text"], query=False, options={})

        assert (error.value.index, error.value.tokens, error.value.limit) == (2, 3000, 2048)
        # An over-length text is the caller's problem; the loaded model stays.
        assert "close" not in FakeChild.events
        assert executor.memory_status()["models"][0]["loaded"]


@pytest.mark.asyncio
async def test_failed_child_is_discarded_and_the_next_request_loads_a_new_one(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as executor:
        install_fake(tmp_path, GRANITE)
        FakeChild.fail = {"crash"}

        with pytest.raises(LocalEmbeddingError, match="failed"):
            await executor.embed(GRANITE.id, ["crash"], query=True, options={})
        await executor.embed(GRANITE.id, ["fine"], query=True, options={"threads": 3})

        loads = [event for event in FakeChild.events if isinstance(event, tuple)]
        assert "close" in FakeChild.events
        assert [threads for _load, _graph, threads in loads][1] == 3
        assert len(loads) == 2


@pytest.mark.asyncio
async def test_uninstalled_model_never_starts_a_child(tmp_path: Path) -> None:
    async with running(tmp_path) as executor:
        assert not executor.targets.get(GRANITE.id).can_execute()

        with pytest.raises(LocalEmbeddingUnavailableError, match="Install it in Settings"):
            await executor.embed(GRANITE.id, ["text"], query=True, options={})

        assert FakeChild.events == []


@pytest.mark.asyncio
async def test_memory_release_stops_only_an_idle_loaded_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(tmp_path) as executor:
        install_fake(tmp_path, GRANITE)
        install_fake(tmp_path, HARRIER)
        await executor.embed(GRANITE.id, ["one"], query=True, options={})
        # Loading another model replaces the child: one local embedding model in memory.
        await executor.embed(HARRIER.id, ["two"], query=True, options={})

        assert [
            (model["target"], model["loaded"]) for model in executor.memory_status()["models"]
        ] == [
            ("local/granite-embedding-r2", False),
            ("local/harrier-0.6b", True),
        ]
        assert (await executor.release_memory("local/granite-embedding-r2"))["released"] is False
        released = await executor.release_memory("local/harrier-0.6b")
        assert released["released"] is True
        assert not any(model["loaded"] for model in released["models"])
        assert FakeChild.events.count("close") == 2
        with pytest.raises(ValueError):
            await executor.release_memory("local/unknown")

        # A different model's admitted turn may still precede an unload in the
        # worker queue. Recheck the target when unloading, not only on admission.
        await executor.embed(GRANITE.id, ["warm"], query=True, options={})
        started, proceed = threading.Event(), threading.Event()
        embed_turn = executor._embed_turn

        def delayed_turn(model, key, texts):
            started.set()
            assert proceed.wait(timeout=2)
            return embed_turn(model, key, texts)

        monkeypatch.setattr(executor, "_embed_turn", delayed_turn)
        other = asyncio.create_task(executor.embed(HARRIER.id, ["next"], query=True, options={}))
        assert await asyncio.to_thread(started.wait, 2)
        release = asyncio.create_task(executor.release_memory("local/granite-embedding-r2"))
        try:
            await asyncio.sleep(0)
        finally:
            proceed.set()
        await other
        result = await release

        assert result["released"] is False
        assert [model["target"] for model in result["models"] if model["loaded"]] == [
            "local/harrier-0.6b"
        ]


class _Commands:
    """Fake setup commands: creates the environment and the prepared graph."""

    def __init__(self, setup: Any, exit_code: int = 0) -> None:
        self.setup = setup
        self.exit_code = exit_code
        self.calls: list[list[str]] = []
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, arguments: Any, **_kwargs: Any) -> int:
        self.calls.append(list(arguments))
        if "venv" in arguments:
            self.setup.python.parent.mkdir(parents=True, exist_ok=True)
            self.setup.python.touch()
        if "--prepare" not in arguments:
            return 0
        self.seen.append(self.setup.status())
        if self.exit_code == 0:
            graph = Path(json.loads(arguments[-1])["model"]["graph"])
            graph.parent.mkdir(parents=True, exist_ok=True)
            graph.touch()
        return self.exit_code


class _Fetch:
    """Fake model download: reports half the bytes, then all, or fails with *error*."""

    def __init__(self, setup: Any, error: str = "") -> None:
        self.setup = setup
        self.error = error
        self.models: list[Any] = []
        self.seen: list[dict[str, Any]] = []

    def __call__(
        self, model: Any, directory: Path, *, progress: Any, cancelled: Any, reuse: Any = ()
    ) -> None:
        self.models.append(model)
        progress(model.download_bytes // 2)
        self.seen.append(self.setup.status())
        if self.error:
            raise ModelFilesError(self.error)
        progress(model.download_bytes)


@pytest.mark.asyncio
async def test_setup_installs_the_environment_once_then_each_pinned_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(tmp_path) as executor:
        ready: list[str] = []
        executor.add_ready_listener(ready.append)
        harrier = executor.setup_for("local/harrier-0.6b")
        stale = tmp_path / "models" / HARRIER.id / "old-revision"
        stale.mkdir(parents=True)
        total = HARRIER.download_bytes
        commands = _Commands(harrier)
        fetch = _Fetch(harrier)
        monkeypatch.setattr(harrier, "_command", commands)
        monkeypatch.setattr(harrier, "_packaged", lambda: True)
        monkeypatch.setattr(local_setup, "fetch_model_files", fetch)
        # A completed environment based on a Python other than the server's, as
        # left behind when vBot's Python changed: its recipe names no Python.
        assert harrier.directory is not None
        harrier.python.parent.mkdir(parents=True)
        harrier.python.touch()
        base = tmp_path / "base-python"
        base.mkdir()
        config = f"home = {base}\nversion_info = 3.1.4\n"
        (harrier.directory / "pyvenv.cfg").write_text(config, encoding="utf-8")
        (harrier.directory / "verified.json").write_text("{}\n", encoding="utf-8")
        assert harrier.status() == {
            "state": "missing",
            "phase": "checking",
            "error": "python_changed",
        }
        removals: list[tuple[Path, int]] = []
        rmtree = local_setup.shutil.rmtree

        def remove(path: Path, *args: Any, **kwargs: Any) -> None:
            removals.append((Path(path), threading.get_ident()))
            rmtree(path, *args, **kwargs)

        monkeypatch.setattr(local_setup.shutil, "rmtree", remove)

        harrier.install()
        assert harrier._task is not None
        await harrier._task

        # The environment was removed off the Event Loop, then created again on a
        # uv-managed Python of the server's version: uv does not see the server's
        # interpreter as its parent. The Model's superseded revision was removed
        # off the Event Loop too.
        for removed in (harrier.directory, stale):
            threads = [thread for path, thread in removals if path == removed]
            assert threads and threading.get_ident() not in threads
        assert not (harrier.directory / "pyvenv.cfg").exists()
        venv = next(call for call in commands.calls if "venv" in call)
        server = f"{sys.version_info.major}.{sys.version_info.minor}"
        uv = [sys.executable, "-c", local_setup._UV]
        assert venv == [*uv, "venv", "--python", server, "--managed-python", str(harrier.directory)]

        assert harrier.status()["state"] == "ready" and harrier.available()
        assert harrier.activity() == {"state": "completed"}
        assert executor.targets.get(HARRIER.id).can_execute()
        assert ready == ["local/harrier-0.6b"]
        assert fetch.models == [HARRIER.pinned]
        assert fetch.seen == [
            {
                "state": "installing",
                "phase": "downloading",
                "error": "",
                "progress": {"completed": total // 2, "total": total},
            }
        ]
        assert commands.seen == [{"state": "installing", "phase": "verifying", "error": ""}]
        # The pinned recipe goes into the managed environment only, never the server.
        installs = [call for call in commands.calls if "install" in call and "pip" in call]
        assert installs and all(
            call[:3] == [sys.executable, "-c", local_setup._UV]
            and call[call.index("--python") + 1] == str(harrier.python)
            for call in installs
        )
        recipe = harrier._config()["tool"]["vbot"]["local-embeddings"]["onnx"]
        assert installs[-1][-len(recipe["packages"]) :] == recipe["packages"]
        assert "--verify" in commands.calls[-2]
        assert json.loads(commands.calls[-1][-1])["derive"]["accuracy_level"] == 4
        assert not stale.exists()

        granite = executor.setup_for("local/granite-embedding-r2")
        granite_commands = _Commands(granite)
        monkeypatch.setattr(granite, "_command", granite_commands)
        granite.install()
        assert granite._task is not None
        await granite._task

        assert granite.available()
        assert [call[-2] for call in granite_commands.calls] == ["--prepare"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fetch_error", "prepare_exit", "phase", "error"),
    [
        pytest.param("checksum_mismatch", 0, "downloading", "checksum_mismatch", id="download"),
        pytest.param("", 1, "verifying", "verification_failed", id="unloadable"),
    ],
)
async def test_failed_model_install_publishes_no_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch_error: str,
    prepare_exit: int,
    phase: str,
    error: str,
) -> None:
    async with running(tmp_path) as executor:
        install_fake(tmp_path, GRANITE)
        setup = executor.setup_for("local/granite-embedding-r2")
        assert setup.model_directory is not None
        monkeypatch.setattr(setup, "_command", _Commands(setup, exit_code=prepare_exit))
        monkeypatch.setattr(local_setup, "fetch_model_files", _Fetch(setup, fetch_error))
        # Reinstalling a model withdraws its earlier receipt first.
        (setup.model_directory / "verified.json").unlink()
        setup.install()
        assert setup._task is not None
        await setup._task

        assert setup.status() == {"state": "failed", "phase": phase, "error": error}
        assert not (setup.model_directory / "verified.json").exists()
        assert not setup.available()


class _WorkerModel:
    """The child's model, replaced: a text's vector is ``[len(text), 0.5, 0.25]``."""

    def __init__(self, spec: dict[str, Any], threads: int) -> None:
        if spec.get("graph") == "unloadable":
            raise RuntimeError("bad graph")
        self.dimension = 3

    def embed(self, texts: list[str]) -> tuple[list[Any], list[int]]:
        for index, text in enumerate(texts):
            if text == "long":
                raise embedding_worker.InputTooLongError(index, 3000, 2048)
            if text == "boom":
                raise RuntimeError("private detail")
        return [array.array("f", [len(text), 0.5, 0.25]) for text in texts], [7] * len(texts)


def test_worker_wire_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = [
        {"op": "embed", "texts": ["x"]},
        {"op": "load", "model": {"graph": "g"}, "threads": 2},
        {"op": "embed", "texts": ["ab", "Grüße"]},
        {"op": "embed", "texts": ["ok", "long"]},
        {"op": "embed", "texts": ["fine"]},
        {"op": "embed", "texts": ["boom"]},
    ]
    wire = io.StringIO()
    monkeypatch.setattr(embedding_worker, "Model", _WorkerModel)
    monkeypatch.setattr(embedding_worker, "lower_priority", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(r) + "\n" for r in requests)))
    monkeypatch.setattr(sys, "stdout", wire)

    assert embedding_worker.serve() == 0

    replies = wire.getvalue().splitlines()
    assert [json.loads(line) for line in replies[:2]] == [
        {"error": "embedding_failed", "type": "RuntimeError"},
        {"loaded": {"dimension": 3}},
    ]
    assert json.loads(replies[3]) == {
        "error": "input_too_long",
        "index": 1,
        "tokens": 3000,
        "limit": 2048,
    }
    # The parent decodes exactly these frames; failures name no private detail.
    sent = io.StringIO()
    process = SimpleNamespace(
        stdin=sent, stdout=io.StringIO("\n".join(replies[1:]) + "\n"), poll=lambda: 0
    )
    popen = Mock(return_value=process)
    monkeypatch.setattr(_worker_process.subprocess, "Popen", popen)
    parent = _WorkerProcess(Path("worker-python"), {"graph": "g"}, 2)
    try:
        vectors, tokens = parent.embed(["ab", "Grüße"])
        assert (vectors, tokens) == ([[2.0, 0.5, 0.25], [5.0, 0.5, 0.25]], [7, 7])
        with pytest.raises(LocalEmbeddingInputTooLongError) as too_long:
            parent.embed(["ok", "long"])
        assert (too_long.value.index, too_long.value.tokens, too_long.value.limit) == (
            1,
            3000,
            2048,
        )
        assert parent.embed(["fine"]) == ([[4.0, 0.5, 0.25]], [7])
        with pytest.raises(LocalEmbeddingError, match=r"\(RuntimeError\)$"):
            parent.embed(["boom"])
        # Requests are ASCII lines: the isolated child reads stdin in the console code page.
        assert sent.getvalue().isascii()
        assert [json.loads(line) for line in sent.getvalue().splitlines()[:2]] == requests[1:3]
        assert popen.call_args.args[0] == [
            "worker-python",
            "-I",
            "-B",
            str(embeddings_local._WORKER),
            "--serve",
        ]
        assert popen.call_args.kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    finally:
        parent.close()
    assert process.stdin.closed and process.stdout.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["loading", "embedding"])
async def test_unresponsive_worker_is_ended_and_the_next_request_loads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    armed: Future[Any] = Future()
    timers: list[Any] = []
    processes: list[Any] = []
    stopped = threading.Event()

    class Timer:
        def __init__(self, interval, function, args=()):
            self.interval, self.function, self.args = interval, function, args
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            assert not self.cancelled
            self.function(*self.args)

    def launch(*_args, **_kwargs):
        first = not processes
        replies = iter([] if first and phase == "loading" else ['{"loaded":true}\n'])
        vector = base64.b64encode(array.array("f", [1.0, 0.5]).tobytes()).decode("ascii")

        def readline(_limit):
            if (reply := next(replies, None)) is not None:
                return reply
            if first:
                armed.set_result(timers[-1])
                assert stopped.wait(5)
                return ""
            return json.dumps({"embedded": {"vectors": vector, "dimension": 2, "tokens": [7]}})

        process = Mock()
        process.stdout.readline.side_effect = readline
        process.poll.side_effect = lambda: 0 if first and stopped.is_set() else None
        processes.append(process)
        return process

    kill_tree = Mock(side_effect=lambda _process: stopped.set())
    monkeypatch.setattr(_worker_process, "Timer", Timer)
    monkeypatch.setattr(_worker_process.subprocess, "Popen", launch)
    monkeypatch.setattr("core.utils.processes.kill_process_tree", kill_tree)
    executor = LocalEmbeddingExecutor(engines_dir=tmp_path)
    install_fake(tmp_path, GRANITE)
    try:
        request = asyncio.create_task(executor.embed(GRANITE.id, ["first"], query=True, options={}))
        timer = await asyncio.wrap_future(armed)
        assert timer.interval == embeddings_local._EXCHANGE_TIMEOUT_S
        await asyncio.to_thread(timer.fire)
        with pytest.raises(LocalEmbeddingError):
            await request
        assert not executor.memory_status()["models"][0]["loaded"]
        kill_tree.assert_called_once_with(processes[0])
        processes[0].wait.assert_called_once()
        processes[0].stdin.close.assert_called_once()
        processes[0].stdout.close.assert_called_once()
        assert (await executor.embed(GRANITE.id, ["next"], query=True, options={})).tokens == (7,)
        assert len(processes) == 2
        assert executor.memory_status()["models"][0]["loaded"]
        assert all(timer.cancelled for timer in timers)
    finally:
        stopped.set()
        await executor.aclose()


@pytest.mark.parametrize(
    ("physical", "usable", "threads"),
    [(6, 12, 5), (6, 3, 2), (1, 1, 1), (None, 8, 3)],
    ids=["all-cpus", "affinity", "single-core", "unknown-physical-cores"],
)
def test_automatic_threads_keep_one_usable_core_free(
    monkeypatch: pytest.MonkeyPatch, physical: int | None, usable: int, threads: int
) -> None:
    psutil = SimpleNamespace(cpu_count=lambda logical=True: usable if logical else physical)
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(os, "cpu_count", lambda: usable)
    # The serving child inherits the server's CPU affinity.
    monkeypatch.setattr(os, "process_cpu_count", lambda: usable)

    assert default_threads() == threads
