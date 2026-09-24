"""Internal parts of the concurrent-Agent load harness (``scripts/perf_load.py``).

The harness starts its own vBot server and a scripted OpenAI-compatible fake
Provider, drives many Sessions concurrently through the public RPC/SSE
surface, and reports where vBot itself spends time.
"""
