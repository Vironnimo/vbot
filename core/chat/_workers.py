"""Shared bounded Chat transformation workers."""

from __future__ import annotations

from core.utils.workers import BoundedWorkerPool

CHAT_TRANSFORM_WORKER_LIMIT = 4


_CHAT_TRANSFORM_WORKERS = BoundedWorkerPool(
    name="chat-transform",
    max_workers=CHAT_TRANSFORM_WORKER_LIMIT,
)
