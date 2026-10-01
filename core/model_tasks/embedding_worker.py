"""Standalone local embedding child; it never imports vBot.

It runs in the managed embedding environment (ONNX Runtime, tokenizers,
numpy, huggingface-hub, onnx). The parent owns model choice, paths, timeouts
and process lifetime, and applies every query or document prefix before a
text arrives here, so a text is embedded exactly as received. Modes:

``--verify``
    Import the runtime stack; exit status 0 proves the environment.
``--install <spec>``
    Download the pinned files of one model revision into its directory,
    check their SHA-256, derive the graph vBot loads when the spec asks for
    it, and load the model once. Progress frames
    ``{"progress": {"completed": bytes, "total": bytes}}`` and a final
    ``{"error": code}`` on failure are written to stdout, each on its own line.
``--serve``
    Answer one JSON request per stdin line on stdout:
    ``{"op": "load", "model": {...}, "threads": n}`` replies
    ``{"loaded": {"dimension": d}}``; ``{"op": "embed", "texts": [...]}``
    replies ``{"embedded": {"vectors": base64, "dimension": d, "tokens": [...]}}``
    with L2-normalized little-endian float32 vectors in input order. A text
    longer than the model's limit embeds nothing and replies
    ``{"error": "input_too_long", "index": i, "tokens": n, "limit": m}``; the
    child keeps serving. Any other failure replies
    ``{"error": "embedding_failed", "type": name}``.

Every text runs alone: dynamically quantized graphs make a vector depend on
its batch neighbours, and a single text needs no padding.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_PROGRESS_INTERVAL_S = 0.25


class InputTooLongError(Exception):
    def __init__(self, index: int, tokens: int, limit: int) -> None:
        super().__init__("input_too_long")
        self.index, self.tokens, self.limit = index, tokens, limit


class InstallError(Exception):
    """An installation failure carrying its stable code (``download_failed``,
    ``checksum_mismatch`` or ``verification_failed``)."""


def lower_priority() -> None:
    """Yield the CPU to interactive work; ONNX Runtime threads inherit this."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), _BELOW_NORMAL_PRIORITY_CLASS)
    else:
        with suppress(OSError):
            os.nice(10)


class Model:
    """One loaded ONNX graph with its tokenizer; texts run one at a time."""

    def __init__(self, spec: dict[str, Any], threads: int) -> None:
        self._numpy = importlib.import_module("numpy")
        runtime = importlib.import_module("onnxruntime")
        tokenizers = importlib.import_module("tokenizers")
        options = runtime.SessionOptions()
        options.intra_op_num_threads = max(1, int(threads))
        options.inter_op_num_threads = 1
        options.execution_mode = runtime.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        # Idle threads must not spin: the child shares the CPU with the user.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._session = runtime.InferenceSession(
            spec["graph"], options, providers=["CPUExecutionProvider"]
        )
        self._inputs = {item.name for item in self._session.get_inputs()}
        tokenizer = tokenizers.Tokenizer.from_file(spec["tokenizer"])
        # A model's own configuration may truncate or pad; vBot reports
        # over-length input instead and never pads a single text.
        tokenizer.no_truncation()
        tokenizer.no_padding()
        self._tokenizer = tokenizer
        self._output = spec["output"]
        self._pooling = spec["pooling"]
        self._limit = int(spec["max_tokens"])
        self.dimension = len(self.embed(["vBot"])[0][0])
        expected = spec.get("dimension")
        if expected and self.dimension != expected:
            raise ValueError("Unexpected embedding dimension")

    def embed(self, texts: list[str]) -> tuple[list[Any], list[int]]:
        np = self._numpy
        encodings = self._tokenizer.encode_batch(texts)
        counts = [len(encoding.ids) for encoding in encodings]
        for index, count in enumerate(counts):
            if count > self._limit:
                raise InputTooLongError(index, count, self._limit)
        vectors = []
        for encoding in encodings:
            ids = np.asarray([encoding.ids], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
            if "token_type_ids" in self._inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            output = self._session.run(
                [self._output], {k: v for k, v in feeds.items() if k in self._inputs}
            )
            values = np.asarray(output[0], dtype=np.float32)
            vector = values[0, 0, :] if self._pooling == "cls" else values[0]
            norm = float(np.linalg.norm(vector))
            if not np.isfinite(norm) or norm == 0.0:
                raise ValueError("Degenerate embedding")
            vectors.append((vector / norm).astype("<f4"))
        return vectors, counts


def serve() -> int:
    wire = sys.stdout
    # Libraries write diagnostics to stdout; only control frames may use it.
    sys.stdout = sys.stderr
    lower_priority()
    model: Model | None = None

    def send(frame: dict[str, Any]) -> None:
        wire.write(json.dumps(frame, separators=(",", ":")) + "\n")
        wire.flush()

    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("op") == "load":
                model = Model(request["model"], request.get("threads", 1))
                send({"loaded": {"dimension": model.dimension}})
            elif request.get("op") == "embed":
                if model is None:
                    raise RuntimeError("No model loaded")
                vectors, counts = model.embed([str(text) for text in request["texts"]])
                payload = b"".join(vector.tobytes() for vector in vectors)
                send(
                    {
                        "embedded": {
                            "vectors": base64.b64encode(payload).decode("ascii"),
                            "dimension": model.dimension,
                            "tokens": counts,
                        }
                    }
                )
            else:
                raise ValueError("Unknown operation")
        except InputTooLongError as error:
            send(
                {
                    "error": "input_too_long",
                    "index": error.index,
                    "tokens": error.tokens,
                    "limit": error.limit,
                }
            )
        except Exception as error:
            send({"error": "embedding_failed", "type": type(error).__name__})
    return 0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def install(spec: dict[str, Any]) -> int:
    def send(frame: dict[str, Any]) -> None:
        # A leading newline keeps the frame on its own line even after a
        # library wrote a partial line to the shared output.
        sys.stdout.write("\n" + json.dumps(frame, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    lower_priority()
    try:
        directory = Path(spec["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        files = spec["files"]
        total = sum(int(item["size"]) for item in files)
        done = 0
        reported = 0.0

        def report(current: int, force: bool = False) -> None:
            nonlocal reported
            now = time.monotonic()
            if force or now - reported >= _PROGRESS_INTERVAL_S:
                reported = now
                send({"progress": {"completed": min(done + current, total), "total": total}})

        report(0, force=True)
        for item in files:
            target = directory / item["path"]
            if not (target.is_file() and target.stat().st_size == int(item["size"])) or (
                sha256(target) != item["sha256"]
            ):
                download(spec, item, target, report)
            done += int(item["size"])
            report(0, force=True)
        if derive := spec.get("derive"):
            derive_graph(directory, derive)
        try:
            Model(spec["model"], spec.get("threads", 1))
        except Exception as error:
            raise InstallError("verification_failed") from error
    except InstallError as error:
        send({"error": str(error)})
        return 1
    except Exception:
        send({"error": "download_failed"})
        return 1
    return 0


def download(spec: dict[str, Any], item: dict[str, Any], target: Path, report: Any) -> None:
    hub = importlib.import_module("huggingface_hub")
    tqdm = importlib.import_module("huggingface_hub.utils.tqdm").tqdm
    size = int(item["size"])
    # Xet downloads count network bytes and written bytes on separate bars;
    # the furthest of them is this file's progress.
    furthest = 0

    class Progress(tqdm):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._bytes = kwargs.get("unit") == "B"
            kwargs["file"] = io.StringIO()
            # The hub leaves `disable` to TTY detection, which turns off a bar
            # writing to a string, and a bar that is off never counts.
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)

        def update(self, n: float | None = 1) -> bool | None:
            nonlocal furthest
            result: bool | None = super().update(n)
            if self._bytes:
                furthest = min(size, max(furthest, int(self.n)))
                report(furthest)
            return result

    try:
        hub.hf_hub_download(
            spec["repo"],
            item["path"],
            revision=spec["revision"],
            local_dir=spec["directory"],
            force_download=target.exists(),
            tqdm_class=Progress,
        )
    except Exception as error:
        raise InstallError("download_failed") from error
    if sha256(target) != item["sha256"]:
        target.unlink(missing_ok=True)
        raise InstallError("checksum_mismatch")


def derive_graph(directory: Path, derive: dict[str, Any]) -> None:
    """Write the loaded graph: the source graph with a fixed compute setting.

    ``accuracy_level`` 4 lets ONNX Runtime's CPU kernels multiply 8-bit
    ``MatMulNBits`` weights with int8 activations instead of dequantizing the
    whole weight set on every run. External weights stay shared by reference.
    """
    onnx = importlib.import_module("onnx")
    source = directory / derive["source"]
    target = directory / derive["target"]
    graph = onnx.load(str(source), load_external_data=False)
    for node in graph.graph.node:
        if node.op_type == "MatMulNBits":
            for attribute in [item for item in node.attribute if item.name == "accuracy_level"]:
                node.attribute.remove(attribute)
            node.attribute.append(
                onnx.helper.make_attribute("accuracy_level", int(derive["accuracy_level"]))
            )
    temporary = target.with_suffix(".tmp")
    onnx.save(graph, str(temporary))
    os.replace(temporary, target)


def verify() -> int:
    for name in ("numpy", "onnxruntime", "tokenizers", "huggingface_hub", "onnx"):
        importlib.import_module(name)
    runtime = importlib.import_module("onnxruntime")
    return 0 if "CPUExecutionProvider" in runtime.get_available_providers() else 1


def main(arguments: list[str]) -> int:
    if arguments[:1] == ["--verify"]:
        return verify()
    if arguments[:1] == ["--install"] and len(arguments) == 2:
        return install(json.loads(arguments[1]))
    if arguments[:1] == ["--serve"]:
        return serve()
    sys.stderr.write("usage: embedding_worker.py --verify | --install <spec> | --serve\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
