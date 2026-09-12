"""Shared bounded workers for Session RPC reads and Agent rename migrations."""

from core.utils.workers import BoundedWorkerPool

SESSION_RPC_WORKER_LIMIT = 4
_SESSION_RPC_WORKERS = BoundedWorkerPool(name="session-rpc", max_workers=SESSION_RPC_WORKER_LIMIT)
