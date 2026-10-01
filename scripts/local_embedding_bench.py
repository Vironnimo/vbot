#!/usr/bin/env python
"""Measure the local embedding engine and check its vectors against the original Models.

Runs the real vBot path: ``LocalEmbeddingExecutor`` installs the managed
environment and the pinned Model files under ``<data-dir>/embedding-engines``
(with ``--install``) and embeds through its serving child, with the Model
family's query prefix applied as vBot applies it. Reports the load time, query
latency and document throughput for passages of about 400 tokens at document
batches of 8 and 16, as JSON on stdout.

``--reference`` also embeds German and English queries and passages with the
original Model in this interpreter (transformers and torch, float32, one text
at a time) and reports the cosine similarity per text; a Model passes at a
minimum of 0.99. The reference weights are downloaded into the Hugging Face cache
unless ``--reference-dir`` holds them in a folder named after the local Model id.

Examples:
    python scripts/local_embedding_bench.py --data-dir ~/.vbot-local-embeddings --install
    python scripts/local_embedding_bench.py --data-dir ~/.vbot-local-embeddings --reference
    python scripts/local_embedding_bench.py --data-dir D:/scratch --model harrier-0.6b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Import this checkout's packages first; from a linked worktree `core` would
# otherwise resolve through the editable install to the main checkout's code.
_CHECKOUT_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[:1] != [_CHECKOUT_ROOT]:
    sys.path.insert(0, _CHECKOUT_ROOT)

from core.model_tasks.embedding_profiles import embedding_profile  # noqa: E402
from core.model_tasks.embeddings_local import (  # noqa: E402
    LocalEmbeddingExecutor,
    LocalEmbeddingModel,
    builtin_local_embedding_models,
)

JsonReport = dict[str, Any]

PARITY_THRESHOLD = 0.99
BATCH_SIZES = (8, 16)
PASSAGES = 16
QUERY_REPEATS = 10

# The original Models; Granite's weights live in the repository vBot installs from.
REFERENCES = {
    "granite-embedding-r2": ("ibm-granite/granite-embedding-311m-multilingual-r2", "cls"),
    "harrier-0.6b": ("microsoft/harrier-oss-v1-0.6b", "last"),
}

QUERIES = (
    "Wie hoch ist die Zugspitze?",
    "Welche Einstellungen haben wir gestern für die Suche geändert?",
    "How do I rotate the API key for the calendar connection?",
    "What did we decide about the release date?",
)
DOCUMENTS = (
    "Die Zugspitze ist mit 2962 Metern der höchste Berg Deutschlands.",
    "Gestern haben wir die semantische Suche über Gespräche eingerichtet und das "
    "Indexieren im Hintergrund mit lokalen Modellen getestet.",
    "To rotate a key, open Settings, choose the connection, and paste the new key; "
    "the old key stops working immediately.",
    "We agreed to ship the release on Thursday after the last review.",
    "Kurz.",
)
_PASSAGE = (
    "Gestern haben wir im Projekt die semantische Suche über Gespräche eingerichtet. "
    "The local engine embeds passages in the background while the user keeps chatting. "
    "Die Ergebnisse waren überzeugend, auch bei gemischten deutschen und englischen Texten. "
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="vBot data directory whose embedding-engines folder holds the installation",
    )
    parser.add_argument(
        "--model",
        choices=[model.id for model in builtin_local_embedding_models()],
        action="append",
        help="Model to measure (repeatable; default: every local embedding Model)",
    )
    parser.add_argument("--install", action="store_true", help="install missing Models first")
    parser.add_argument(
        "--reference", action="store_true", help="compare vectors with the original Model"
    )
    parser.add_argument(
        "--reference-dir",
        type=Path,
        help="folder with one downloaded original Model per local Model id",
    )
    parser.add_argument("--threads", type=int, default=0, help="CPU threads (0: engine default)")
    parser.add_argument(
        "--passage-repeats",
        type=int,
        default=8,
        help="repetitions of the sample paragraph per passage (8 is about 400 tokens)",
    )
    return parser


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def _install(executor: LocalEmbeddingExecutor, model: LocalEmbeddingModel) -> JsonReport:
    setup = executor.setup_for(f"local/{model.id}")
    if setup.available():
        return {"state": "ready", "seconds": 0.0}
    started = time.perf_counter()
    status = setup.install()
    last = ""
    while status["state"] == "installing":
        progress = status.get("progress")
        line = status["phase"] + (
            f" {progress['completed'] / max(progress['total'], 1):.0%}" if progress else ""
        )
        if line != last:
            _log(f"{model.id}: {line}")
            last = line
        await asyncio.sleep(1)
        status = setup.status()
    return {"state": status["state"], "error": status["error"], "seconds": _since(started)}


async def _measure(
    executor: LocalEmbeddingExecutor, model: LocalEmbeddingModel, args: argparse.Namespace
) -> JsonReport:
    options = {"threads": args.threads}
    profile = embedding_profile(model.id)

    def queries(texts: tuple[str, ...]) -> list[str]:
        return list(profile.request(list(texts), "query").inputs)

    started = time.perf_counter()
    await executor.embed(model.id, queries(QUERIES[:1]), query=True, options=options)
    load = _since(started)
    latencies = []
    for index in range(QUERY_REPEATS):
        started = time.perf_counter()
        await executor.embed(
            model.id, queries(QUERIES[index % len(QUERIES) :][:1]), query=True, options=options
        )
        latencies.append(_since(started) * 1000)
    passages = [f"{index}. " + _PASSAGE * args.passage_repeats for index in range(PASSAGES)]
    throughput: dict[str, float] = {}
    tokens: list[int] = []
    for batch in BATCH_SIZES:
        started = time.perf_counter()
        for offset in range(0, PASSAGES, batch):
            output = await executor.embed(
                model.id, passages[offset : offset + batch], query=False, options=options
            )
            tokens.extend(output.tokens)
        throughput[f"batch_{batch}"] = round(PASSAGES / _since(started), 2)
    return {
        "load_and_first_query_s": round(load, 2),
        "query_latency_ms": {
            "median": round(statistics.median(latencies), 1),
            "max": round(max(latencies), 1),
        },
        "passage_tokens": round(statistics.mean(tokens)),
        "passages_per_s": throughput,
    }


async def _parity(
    executor: LocalEmbeddingExecutor, model: LocalEmbeddingModel, reference_dir: Path | None
) -> JsonReport:
    profile = embedding_profile(model.id)
    queries = list(profile.request(list(QUERIES), "query").inputs)
    vectors = [
        *(await executor.embed(model.id, queries, query=True, options={})).vectors,
        *(await executor.embed(model.id, list(DOCUMENTS), query=False, options={})).vectors,
    ]
    reference = await asyncio.to_thread(
        _reference_vectors, model, [*queries, *DOCUMENTS], reference_dir
    )
    similarities = [round(_cosine(a, b), 4) for a, b in zip(vectors, reference, strict=True)]
    return {
        "reference": REFERENCES[model.id][0],
        "cosine": similarities,
        "minimum": min(similarities),
        "passed": min(similarities) >= PARITY_THRESHOLD,
    }


def _reference_vectors(
    model: LocalEmbeddingModel, texts: list[str], reference_dir: Path | None
) -> list[list[float]]:
    """Embed with the original Model in float32, one unpadded text at a time."""
    import torch
    from transformers import AutoModel, AutoTokenizer

    repo, pooling = REFERENCES[model.id]
    revision = model.revision if repo == model.repo else None
    source = str(reference_dir.expanduser() / model.id) if reference_dir else repo
    tokenizer = AutoTokenizer.from_pretrained(source, revision=revision)
    network = AutoModel.from_pretrained(source, revision=revision, dtype=torch.float32).eval()
    vectors = []
    with torch.inference_mode():
        for text in texts:
            hidden = network(**tokenizer(text, return_tensors="pt")).last_hidden_state[0]
            vectors.append((hidden[0] if pooling == "cls" else hidden[-1]).tolist())
    return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def _since(started: float) -> float:
    return time.perf_counter() - started


async def _run(args: argparse.Namespace) -> int:
    engines = args.data_dir.expanduser() / "embedding-engines"
    executor = LocalEmbeddingExecutor(engines_dir=engines)
    selected = args.model or [model.id for model in builtin_local_embedding_models()]
    report: JsonReport = {"engines": str(engines), "models": {}}
    passed = True
    try:
        for local_id in selected:
            model = executor.model(local_id)
            entry: JsonReport = {}
            report["models"][local_id] = entry
            if args.install:
                entry["install"] = await _install(executor, model)
            if not executor.targets.get(local_id).can_execute():
                entry["error"] = "not installed (run with --install)"
                passed = False
                continue
            _log(f"{local_id}: measuring")
            entry.update(await _measure(executor, model, args))
            if args.reference:
                _log(f"{local_id}: comparing with the reference Model")
                entry["parity"] = await _parity(executor, model, args.reference_dir)
                passed = passed and entry["parity"]["passed"]
            await executor.release_memory(f"local/{local_id}")
    finally:
        await executor.aclose()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
