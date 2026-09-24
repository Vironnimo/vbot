"""All Python benchmarks, in run order."""

from __future__ import annotations

from collections import Counter

from scripts.perf_bench_suite import bench_chat, bench_provider, bench_server, bench_sessions
from scripts.perf_bench_suite.runner import Benchmark

BENCHMARKS: tuple[Benchmark, ...] = (
    *bench_sessions.BENCHMARKS,
    *bench_chat.BENCHMARKS,
    *bench_provider.BENCHMARKS,
    *bench_server.BENCHMARKS,
)

_duplicates = sorted(
    name for name, count in Counter(b.name for b in BENCHMARKS).items() if count > 1
)
if _duplicates:
    raise RuntimeError(f"duplicate benchmark names: {', '.join(_duplicates)}")
