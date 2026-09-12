"""Shared Runtime configuration and catalog workers."""

from core.utils.workers import BoundedWorkerPool

_RUNTIME_WORKERS = BoundedWorkerPool(name="runtime", max_workers=2)
