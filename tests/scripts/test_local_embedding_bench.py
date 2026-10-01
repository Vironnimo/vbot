"""Smoke test of the local embedding benchmark CLI with a fake serving child."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from core.model_tasks.embeddings_local import LocalEmbeddingExecutor

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "scripts" / "local_embedding_bench.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("local_embedding_bench", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BENCH = _load_module()


class _Child:
    def __init__(self, python: Path, spec: Mapping[str, Any], threads: int) -> None:
        pass

    def embed(self, texts: list[str]) -> tuple[list[list[float]], list[int]]:
        return [[1.0, 0.0] for _ in texts], [400 for _ in texts]

    def kill(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_bench_reports_measurements_for_an_installed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engines = tmp_path / "embedding-engines"
    python = engines / "onnx" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    (engines / "onnx" / "verified.json").write_text("{}", encoding="utf-8")
    model = BENCH.builtin_local_embedding_models()[0]
    directory = engines / "models" / model.id / model.revision
    (directory / model.graph).parent.mkdir(parents=True)
    (directory / model.graph).touch()
    (directory / "verified.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        BENCH, "LocalEmbeddingExecutor", partial(LocalEmbeddingExecutor, child_factory=_Child)
    )

    code = BENCH.main(["--data-dir", str(tmp_path)])

    report = json.loads(capsys.readouterr().out)["models"]
    assert code == 1  # the other Model is not installed
    assert report[model.id]["passage_tokens"] == 400
    assert set(report[model.id]["passages_per_s"]) == {"batch_8", "batch_16"}
    assert report["harrier-0.6b"] == {"error": "not installed (run with --install)"}
