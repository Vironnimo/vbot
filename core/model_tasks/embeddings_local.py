"""Local embedding engines: model catalog, installation and the serving child.

Two pinned ONNX models run on this machine through ONNX Runtime in a managed
environment (``<data-dir>/embedding-engines/onnx``), never in the server.
Each model's files live under ``<data-dir>/embedding-engines/models/<id>/
<revision>`` with their own receipt. One standalone child process
(:mod:`core.model_tasks.embedding_worker`) serves the loaded model at low
process priority with a capped thread count.

The executor runs one exchange with the child at a time. Document batches are
split into single-text turns and a waiting query always takes the next turn,
so a search never waits behind more than one document. Purpose prefixes are
applied by the caller before texts arrive here.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import os
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Lock, Timer
from typing import Any, Protocol

from core.model_tasks.constants import TASK_TEXT_EMBEDDING
from core.model_tasks.local_setup import LocalSetup, environment_error
from core.model_tasks.local_targets import LocalTaskTargetDescriptor, LocalTaskTargetRegistry
from core.model_tasks.model_files import ModelFile, PinnedModel
from core.model_tasks.options import TaskModelOptionField
from core.utils.errors import VBotError
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("embeddings.local")
_WORKER = Path(__file__).with_name("embedding_worker.py")
# Bump when the worker changes how a pinned model turns text into a vector.
LOCAL_EMBEDDING_ENGINE_VERSION = 1
THREADS_OPTION = "threads"
_EXCHANGE_TIMEOUT_S = 300.0


class LocalEmbeddingError(VBotError):
    """A local embedding model cannot run or failed."""


class LocalEmbeddingUnavailableError(LocalEmbeddingError):
    """The model is not installed, still installing, or the executor closed."""


class LocalEmbeddingInputTooLongError(LocalEmbeddingError):
    """One text exceeds the local model's input limit; nothing was embedded."""

    def __init__(self, index: int, tokens: int, limit: int) -> None:
        super().__init__(
            f"Text {index} has {tokens} tokens; the local embedding model accepts at most "
            f"{limit} tokens per text."
        )
        self.index, self.tokens, self.limit = index, tokens, limit


@dataclass(frozen=True)
class LocalEmbeddingModel:
    """One pinned model revision and how the worker turns its output into vectors.

    ``graph`` is the ONNX file the worker loads; ``derive`` optionally names a
    pinned source graph it is written from (see the worker's ``derive_graph``).
    ``pooling`` is ``cls`` (first token of ``last_hidden_state``) or
    ``pooled`` (the graph already outputs one vector). ``max_input_tokens`` is
    this engine's limit per text: attention memory grows with the square of
    the length, so it stays far below the Model's own context.
    """

    id: str
    label: str
    repo: str
    revision: str
    license: str
    dimension: int
    graph: str
    output: str
    pooling: str
    tokenizer: str
    files: tuple[ModelFile, ...]
    max_input_tokens: int
    document_batch_size: int
    derive: Mapping[str, Any] | None = None

    @property
    def pinned(self) -> PinnedModel:
        return PinnedModel(self.repo, self.revision, self.files)

    @property
    def download_bytes(self) -> int:
        return self.pinned.download_bytes

    def identity(self) -> dict[str, Any]:
        """The facts that determine which vector a text gets."""
        return {
            "engine_version": LOCAL_EMBEDDING_ENGINE_VERSION,
            "repo": self.repo,
            "revision": self.revision,
            "graph": self.graph,
            "derive": dict(self.derive or {}),
            "output": self.output,
            "pooling": self.pooling,
            "tokenizer": self.tokenizer,
        }


def builtin_local_embedding_models() -> tuple[LocalEmbeddingModel, ...]:
    return (
        LocalEmbeddingModel(
            id="granite-embedding-r2",
            label="Granite Embedding 311M Multilingual R2",
            repo="ibm-granite/granite-embedding-311m-multilingual-r2",
            revision="44399559930365213510b1ee2eb15ded83374f0e",
            license="Apache-2.0",
            dimension=768,
            # IBM's own int8 export (dynamic activation quantization).
            graph="onnx/model_quint8_avx2.onnx",
            output="last_hidden_state",
            pooling="cls",
            tokenizer="tokenizer.json",
            files=(
                ModelFile(
                    "onnx/model_quint8_avx2.onnx",
                    "f1fdd44e7e1ac51f12ab7957c7bd092e064d596c288513bf9d326842f669edee",
                    313_421_909,
                ),
                ModelFile(
                    "tokenizer.json",
                    "0087c868b33bad550a78a08d19798cfd7f713cde4f020803b8f51f405503e15f",
                    33_384_821,
                ),
            ),
            max_input_tokens=2048,
            document_batch_size=8,
        ),
        LocalEmbeddingModel(
            id="harrier-0.6b",
            label="Harrier OSS v1 0.6B",
            repo="onnx-community/harrier-oss-v1-0.6b-ONNX",
            revision="e4daffa011e666dcd2ff2a3c6c05084090ac314d",
            license="MIT",
            dimension=1024,
            # The 8-bit export, run with int8 activations (``accuracy_level`` 4).
            graph="onnx/model_quantized.vbot.onnx",
            output="sentence_embedding",
            pooling="pooled",
            tokenizer="tokenizer.json",
            files=(
                ModelFile(
                    "onnx/model_quantized.onnx",
                    "3c7c03247bf681338ea079ca3875fa48d6b339bacd3abb6b7ce81afa5a357861",
                    393_942,
                ),
                ModelFile(
                    "onnx/model_quantized.onnx_data",
                    "95e9e447398ba3ef59d54888c820ee8c1b80e4373c56cc5d09b2358ee25d893c",
                    706_117_632,
                ),
                ModelFile(
                    "tokenizer.json",
                    "2320ebaed2032c36f5c539cb8531d0642a10024e164f1d81ebbe163b259e3ba5",
                    9_117_473,
                ),
            ),
            max_input_tokens=2048,
            document_batch_size=8,
            derive={
                "source": "onnx/model_quantized.onnx",
                "target": "onnx/model_quantized.vbot.onnx",
                "accuracy_level": 4,
            },
        ),
    )


def default_threads() -> int:
    """All physical cores but one, at least one."""
    try:
        import psutil  # type: ignore[import-untyped]

        physical: int | None = psutil.cpu_count(logical=False)
    except Exception:
        physical = None
    if not physical:
        physical = max(1, (os.cpu_count() or 2) // 2)
    return max(1, physical - 1)


def _model_spec(model: LocalEmbeddingModel, directory: Path) -> dict[str, Any]:
    return {
        "graph": str(directory / model.graph),
        "tokenizer": str(directory / model.tokenizer),
        "output": model.output,
        "pooling": model.pooling,
        "max_tokens": model.max_input_tokens,
        "dimension": model.dimension,
    }


class LocalEmbeddingSetup(LocalSetup):
    """Install the shared ONNX environment and one model's pinned files."""

    def __init__(
        self,
        model: LocalEmbeddingModel,
        *,
        environment: Path | None,
        models_dir: Path | None,
        install_lock: asyncio.Lock | None = None,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            name=model.id,
            directory=environment,
            install_lock=install_lock,
            subject="Local embedding",
            logger=_LOGGER,
            model=model.pinned,
            models_dir=models_dir,
        )
        self.embedding_model = model
        self._on_ready = on_ready

    def _environment_error(self) -> str:
        if self.model_directory is None:
            return "environment_missing"
        return super()._environment_error()

    def _model_error(self) -> str:
        if error := super()._model_error():
            return error
        assert self.model_directory is not None
        try:
            if not (self.model_directory / self.embedding_model.graph).is_file():
                return "model_incomplete"
        except OSError:
            return "environment_unreadable"
        return ""

    async def _perform(self) -> None:
        assert self.directory is not None and self.model_directory is not None
        model = self.embedding_model
        if environment_error(self.directory, self.python):
            if await self._install_recipe("local-embeddings", "onnx") is None:
                return
            if await self._command([str(self.python), "-I", "-B", str(_WORKER), "--verify"]) != 0:
                self._fail("verification_failed")
                return
            self._write_marker(self.directory / "verified.json")
            _LOGGER.info("Local embedding environment installed")
        if not await self._fetch_model():
            return
        self._phase = "verifying"
        self._progress = None
        spec = {
            "directory": str(self.model_directory),
            "derive": dict(model.derive) if model.derive else None,
            "model": _model_spec(model, self.model_directory),
            "threads": default_threads(),
        }
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        code = await self._command(
            [str(self.python), "-I", "-B", str(_WORKER), "--prepare", json.dumps(spec)],
            environment=environment,
        )
        if code != 0:
            self._fail("verification_failed")
            return
        self._publish_model()
        self._state = "ready"
        _LOGGER.info(
            "Local embedding model installed (model=%s revision=%s)",
            model.id,
            model.revision[:12],
        )
        if self._on_ready is not None:
            self._on_ready()


class _Child(Protocol):
    def embed(self, texts: list[str]) -> tuple[list[list[float]], list[int]]: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


class _WorkerProcess:
    """The serving child: one blocking request/reply exchange at a time."""

    def __init__(self, python: Path, spec: Mapping[str, Any], threads: int) -> None:
        from core.utils.processes import subprocess_creation_flags

        self._process = subprocess.Popen(
            [str(python), "-I", "-B", str(_WORKER), "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=subprocess_creation_flags(),
            start_new_session=os.name != "nt",
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "TOKENIZERS_PARALLELISM": "false",
                "HF_HUB_OFFLINE": "1",
            },
        )
        try:
            self._exchange({"op": "load", "model": dict(spec), "threads": threads}, "loaded")
        except BaseException:
            self.close()
            raise

    def embed(self, texts: list[str]) -> tuple[list[list[float]], list[int]]:
        reply = self._exchange({"op": "embed", "texts": texts}, "embedded")
        values = array.array("f")
        values.frombytes(base64.b64decode(reply["vectors"]))
        if sys.byteorder != "little":
            values.byteswap()
        dimension = int(reply["dimension"])
        tokens = [int(count) for count in reply["tokens"]]
        if dimension <= 0 or len(values) != dimension * len(texts) or len(tokens) != len(texts):
            raise LocalEmbeddingError("The local embedding engine returned a malformed reply")
        vectors = [values[i * dimension : (i + 1) * dimension].tolist() for i in range(len(texts))]
        return vectors, tokens

    def _exchange(self, request: Mapping[str, Any], answer: str) -> Any:
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        # A hung child is killed; its closed pipe then ends this exchange.
        timer = Timer(_EXCHANGE_TIMEOUT_S, self.kill)
        timer.daemon = True
        timer.start()
        try:
            # ASCII JSON: the isolated child ignores PYTHONUTF8 and reads stdin in
            # the console code page, which would garble non-ASCII text.
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        finally:
            timer.cancel()
        if not line:
            raise LocalEmbeddingError("The local embedding engine stopped")
        reply = json.loads(line)
        if reply.get("error") == "input_too_long":
            raise LocalEmbeddingInputTooLongError(
                int(reply["index"]), int(reply["tokens"]), int(reply["limit"])
            )
        if reply.get("error"):
            raise LocalEmbeddingError(
                f"The local embedding engine failed ({reply.get('type') or reply['error']})"
            )
        return reply[answer]

    def kill(self) -> None:
        """Kill the child's process tree; a blocked exchange then ends."""
        from core.utils.processes import kill_process_tree

        process = self._process
        if process.poll() is None:
            try:
                kill_process_tree(process)
            except (OSError, ProcessLookupError):
                with suppress(OSError):
                    process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)

    def close(self) -> None:
        self.kill()
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                with suppress(OSError):
                    stream.close()


ChildFactory = Callable[[Path, Mapping[str, Any], int], _Child]


class _Turns:
    """Grant one exchange at a time; waiting queries go before waiting documents."""

    def __init__(self) -> None:
        self._busy = False
        self._queries: deque[asyncio.Future[None]] = deque()
        self._documents: deque[asyncio.Future[None]] = deque()

    async def acquire(self, *, query: bool) -> None:
        if not self._busy and not self._queries and (query or not self._documents):
            self._busy = True
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiters = self._queries if query else self._documents
        waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                # The turn was handed over just as the caller gave up.
                self.release()
            else:
                with suppress(ValueError):
                    waiters.remove(waiter)
            raise

    def release(self) -> None:
        for waiters in (self._queries, self._documents):
            while waiters:
                waiter = waiters.popleft()
                if not waiter.done():
                    waiter.set_result(None)
                    return
        self._busy = False


@dataclass(frozen=True)
class LocalEmbeddingOutput:
    vectors: tuple[list[float], ...]
    tokens: tuple[int, ...]


class LocalEmbeddingExecutor:
    """Own the local embedding catalog, installations and the serving child."""

    def __init__(
        self,
        *,
        engines_dir: Path | None,
        models: Sequence[LocalEmbeddingModel] | None = None,
        child_factory: ChildFactory | None = None,
    ) -> None:
        install_lock = asyncio.Lock()
        self._models = {model.id: model for model in models or builtin_local_embedding_models()}
        self._setups = {
            model.id: LocalEmbeddingSetup(
                model,
                environment=engines_dir / "onnx" if engines_dir else None,
                models_dir=engines_dir / "models" if engines_dir else None,
                install_lock=install_lock,
                on_ready=partial(self._installed, model.id),
            )
            for model in self._models.values()
        }
        self._ready_listeners: list[Callable[[str], None]] = []
        self.targets = LocalTaskTargetRegistry(
            [self._descriptor(model) for model in self._models.values()]
        )
        self._child_factory: ChildFactory = child_factory or _WorkerProcess
        self._workers = BoundedWorkerPool(name="embedding-local", max_workers=1)
        self._turns = _Turns()
        self._child: _Child | None = None
        self._child_key: tuple[str, int] | None = None
        self._child_lock = Lock()
        self._pending = dict.fromkeys(self._models, 0)
        self._closed = False

    def _descriptor(self, model: LocalEmbeddingModel) -> LocalTaskTargetDescriptor:
        return LocalTaskTargetDescriptor(
            id=model.id,
            label=model.label,
            task_types=(TASK_TEXT_EMBEDDING,),
            availability=partial(self._can_execute, model.id),
            metadata={
                "license": model.license,
                "download_bytes": model.download_bytes,
                "dimension": model.dimension,
                "max_input_tokens": model.max_input_tokens,
            },
            option_fields=(
                TaskModelOptionField(
                    THREADS_OPTION,
                    "number",
                    "CPU threads",
                    min_value=1,
                    max_value=256,
                    step=1,
                    description="How many CPU threads the model may use. Leave empty to use "
                    "all physical cores but one; fewer threads keep more of the computer "
                    "free while indexing runs.",
                    placeholder=f"Automatic ({default_threads()})",
                ),
            ),
        )

    def _can_execute(self, local_id: str) -> bool:
        return not self._closed and self._setups[local_id].available()

    def add_ready_listener(self, listener: Callable[[str], None]) -> None:
        """Call *listener* with the target id whenever a model finishes installing."""
        self._ready_listeners.append(listener)

    def _installed(self, local_id: str) -> None:
        for listener in list(self._ready_listeners):
            try:
                listener(f"local/{local_id}")
            except Exception:
                _LOGGER.warning("Local embedding ready listener failed", exc_info=True)

    def check_available(self, local_id: str) -> None:
        """Raise :class:`LocalEmbeddingUnavailableError` unless the model can run now."""
        model = self.model(local_id)
        if not self._can_execute(local_id):
            raise LocalEmbeddingUnavailableError(
                f"The local embedding model {model.label} is not installed or is still "
                "installing. Install it in Settings, then retry."
            )

    def setup_for(self, target: str) -> LocalEmbeddingSetup:
        local_id = target.removeprefix("local/")
        if not target.startswith("local/") or local_id not in self._setups:
            raise ValueError("Unknown local embedding target")
        return self._setups[local_id]

    def model(self, local_id: str) -> LocalEmbeddingModel:
        try:
            return self._models[local_id]
        except KeyError:
            raise LocalEmbeddingUnavailableError(
                f"Unknown local embedding model: {local_id}"
            ) from None

    async def embed(
        self,
        local_id: str,
        texts: Sequence[str],
        *,
        query: bool,
        options: Mapping[str, Any],
    ) -> LocalEmbeddingOutput:
        """Embed *texts* exactly as given; documents yield to waiting queries per text."""
        model = self.model(local_id)
        self.check_available(local_id)
        requested = options.get(THREADS_OPTION) or 0
        threads = int(requested) if isinstance(requested, int | float) and requested > 0 else 0
        key = (local_id, threads or default_threads())
        turns = [list(texts)] if query else [[text] for text in texts]
        vectors: list[list[float]] = []
        tokens: list[int] = []
        self._pending[local_id] += 1
        try:
            for turn in turns:
                await self._turns.acquire(query=query)
                try:
                    turn_vectors, turn_tokens = await self._workers.run(
                        self._embed_turn, model, key, turn
                    )
                except LocalEmbeddingInputTooLongError as error:
                    raise LocalEmbeddingInputTooLongError(
                        len(vectors) + error.index, error.tokens, error.limit
                    ) from error
                finally:
                    self._turns.release()
                vectors.extend(turn_vectors)
                tokens.extend(turn_tokens)
        finally:
            self._pending[local_id] -= 1
        return LocalEmbeddingOutput(tuple(vectors), tuple(tokens))

    def document_batch_size(self, local_id: str) -> int:
        return self.model(local_id).document_batch_size

    def _embed_turn(
        self, model: LocalEmbeddingModel, key: tuple[str, int], texts: list[str]
    ) -> tuple[list[list[float]], list[int]]:
        """Run in the worker thread: (re)load the child if needed, then embed."""
        if self._closed:
            raise LocalEmbeddingUnavailableError("Local embedding is shutting down")
        child = self._loaded_child(model, key)
        try:
            return child.embed(texts)
        except LocalEmbeddingInputTooLongError:
            raise
        except Exception as error:
            self._unload()
            _LOGGER.warning(
                "Local embedding failed (model=%s, error_type=%s)",
                model.id,
                type(error).__name__,
            )
            if isinstance(error, LocalEmbeddingError):
                raise
            raise LocalEmbeddingError("The local embedding engine failed") from error

    def _loaded_child(self, model: LocalEmbeddingModel, key: tuple[str, int]) -> _Child:
        if self._child is not None and self._child_key == key:
            return self._child
        self._unload()
        setup = self._setups[model.id]
        assert setup.model_directory is not None
        try:
            child = self._child_factory(
                setup.python, _model_spec(model, setup.model_directory), key[1]
            )
        except LocalEmbeddingError:
            raise
        except Exception as error:
            _LOGGER.warning(
                "Local embedding model failed to load (model=%s, error_type=%s)",
                model.id,
                type(error).__name__,
            )
            raise LocalEmbeddingError("The local embedding model failed to load") from error
        with self._child_lock:
            self._child, self._child_key = child, key
        if self._closed:
            self._unload()
            raise LocalEmbeddingUnavailableError("Local embedding is shutting down")
        _LOGGER.info("Loaded local embedding model (model=%s threads=%d)", model.id, key[1])
        return child

    def _unload(self) -> None:
        with self._child_lock:
            child, self._child, self._child_key = self._child, None, None
        if child is not None:
            child.close()

    def _kill_child(self) -> None:
        with self._child_lock:
            child = self._child
        if child is not None:
            child.kill()

    def memory_status(self) -> dict[str, Any]:
        """Which model is loaded; reads state only, never starts a child."""
        loaded = self._child_key[0] if self._child_key is not None else None
        return {
            "models": [
                {
                    "target": f"local/{model.id}",
                    "label": model.label,
                    "loaded": loaded == model.id,
                    "busy": self._pending[model.id] > 0 or self._closed,
                }
                for model in self._models.values()
            ]
        }

    async def release_memory(self, target: str) -> dict[str, Any]:
        """Stop the child of one idle model; the next request loads it again."""
        local_id = target.removeprefix("local/")
        if not target.startswith("local/") or local_id not in self._models:
            raise ValueError("Unknown local embedding target")
        loaded = self._child_key is not None and self._child_key[0] == local_id
        if self._pending[local_id] or self._closed or not loaded:
            return {**self.memory_status(), "released": False}
        self._pending[local_id] += 1
        try:
            await self._workers.run(self._unload)
            _LOGGER.info("Unloaded local embedding model (target=%s)", target)
        finally:
            self._pending[local_id] -= 1
        return {**self.memory_status(), "released": True}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for setup in self._setups.values():
            setup.close()
        self._kill_child()
        self._workers.shutdown(wait=False)

    async def aclose(self) -> None:
        for setup in self._setups.values():
            await setup.aclose()
        if self._closed:
            return
        self._closed = True
        # Killing the child ends an exchange in progress; the worker thread
        # then settles and the pipes close in order.
        await asyncio.to_thread(self._kill_child)
        try:
            await self._workers.run(self._unload)
        finally:
            self._workers.shutdown(wait=False)
