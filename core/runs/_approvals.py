"""Process-local operator approvals for live Runs."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from core.runs.run import ApprovalAnswer, Run, RunStatus, is_unattended_run_kind
from core.utils.ids import new_id

_LOGGER = logging.getLogger("vbot.runs")


def _rejection_covers(rejected: str, requested: str) -> bool:
    """Compare access identities; a declined read also excludes changes below its scope."""
    if rejected == requested:
        return True
    try:
        old, new = json.loads(rejected), json.loads(requested)
    except ValueError, TypeError:
        return False
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if old.get("kind") == new.get("kind") == "host":
        return (
            old.get("computer_id") == new.get("computer_id")
            and old.get("access") == new.get("access")
            and bool(set(old.get("host_aliases", ())) & set(new.get("host_aliases", ())))
        )
    if old.get("kind") != "path" or new.get("kind") != "path":
        return False
    if old.get("computer_id") != new.get("computer_id"):
        return False
    if old.get("access") != new.get("access") and not (
        old.get("access") == "read" and new.get("access") == "write"
    ):
        return False

    def includes(scope: dict[str, Any], target: object) -> bool:
        root = scope.get("target")
        if not isinstance(root, str) or not isinstance(target, str):
            return False
        if root == target:
            return True
        prefix = root if root.endswith("/") else root + "/"
        if not target.startswith(prefix):
            return False
        parts = target[len(prefix) :].split("/")
        if any(part in scope.get("pruned", ()) for part in parts):
            return False
        pattern = scope.get("glob")
        if isinstance(pattern, str) and pattern:
            candidates = [parts[:length] for length in range(1, len(parts) + 1)]
            if not scope.get("subtree"):
                candidates = candidates[-1:]
            return any(PurePosixPath("/".join(part)).full_match(pattern) for part in candidates)
        return bool(scope.get("subtree"))

    # A later broad access must not cover a previously declined child either.
    return includes(old, new.get("target")) or includes(new, old.get("target"))


def _is_rejected(rejected: set[str], keys: Sequence[str]) -> bool:
    return any(_rejection_covers(old, new) for old in rejected for new in keys)


@dataclass(slots=True)
class _Pending:
    request: dict[str, Any]
    future: asyncio.Future[ApprovalAnswer]
    checks: tuple[str, ...]
    rejected: tuple[str, ...]
    recheck: Callable[[], Awaitable[bool]] | None
    deciding: bool = False

    @property
    def session(self) -> tuple[str | None, str, str]:
        return (self.request["project_id"], self.request["agent_id"], self.request["session_id"])


class RunApprovals:
    """Own live waiters, their Run-local rejections and the accessor projection."""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}
        self._rejected: dict[str, set[str]] = {}
        self._revision = 0
        self._grant_revision = 0
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []

    def snapshot(self) -> dict[str, Any]:
        return {
            "revision": self._revision,
            "requests": copy.deepcopy([p.request for p in self._pending.values()]),
        }

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    def _changed(self) -> None:
        self._revision += 1
        for callback in tuple(self._callbacks):
            try:
                callback(self.snapshot())
            except Exception:
                _LOGGER.warning("Approval observer failed", exc_info=True)

    async def request(
        self,
        run: Run,
        payload: dict[str, Any],
        keys: Sequence[str],
        rejected_keys: Sequence[str],
        recheck: Callable[[], Awaitable[bool]] | None = None,
    ) -> ApprovalAnswer:
        if (
            run.status != RunStatus.RUNNING
            or run.cancel_requested
            or is_unattended_run_kind(run.run_kind)
        ):
            return False
        rejected = self._rejected.setdefault(run.id, set())
        # A sibling can save a grant while this call prepares its access, or
        # during the read below. Publish only if a current decision is needed.
        if recheck is not None:
            while True:
                revision = self._grant_revision
                permitted = await recheck()
                if (
                    run.status != RunStatus.RUNNING
                    or run.cancel_requested
                    or _is_rejected(rejected, keys)
                ):
                    return False
                if revision == self._grant_revision:
                    if permitted:
                        return "permitted"
                    break
        if _is_rejected(rejected, keys):
            return False
        request_id = new_id("approval")
        request = {
            **copy.deepcopy(payload),
            "id": request_id,
            "run_id": run.id,
            "agent_id": run.agent_id,
            "project_id": run.project_id,
            "session_id": run.session_id,
        }
        future: asyncio.Future[ApprovalAnswer] = asyncio.get_running_loop().create_future()
        pending = _Pending(request, future, tuple(keys), tuple(rejected_keys), recheck)
        self._pending[request_id] = pending
        run.set_approval_pending(request_id, future)
        self._changed()
        try:
            answer = await future
            if (
                _is_rejected(rejected, keys)
                or run.cancel_requested
                or run.status != RunStatus.RUNNING
            ):
                return False
            return answer
        finally:
            run.set_approval_pending(request_id, None)
            if self._pending.pop(request_id, None) is not None:
                self._changed()

    async def _check_rules(self, pending: _Pending) -> None:
        if pending.recheck is None or pending.deciding or pending.future.done():
            return
        try:
            permitted = await pending.recheck()
        except Exception as error:
            if not pending.future.done() and not pending.deciding:
                pending.future.set_exception(error)
            return
        if permitted and not pending.deciding and not pending.future.done():
            pending.future.set_result("permitted")

    async def remember(
        self,
        request_id: str,
        save: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        folder_read: bool,
    ) -> bool:
        pending = self._pending.get(request_id)
        if pending is None or pending.deciding or pending.future.done():
            return False
        # Claim the decision before leaving the event loop to persist it. A
        # second client cannot reject or remember another scope in the meantime.
        pending.deciding = True
        try:
            await save(pending.request)
        finally:
            pending.deciding = False
        self._grant_revision += 1
        self.resolve(request_id, "allow", folder_read=folder_read)
        await asyncio.gather(
            *(
                self._check_rules(other)
                for other in tuple(self._pending.values())
                if other.recheck is not None and other.session == pending.session
            )
        )
        return True

    def resolve(
        self, request_id: str, decision: Literal["allow", "reject"], *, folder_read: bool = False
    ) -> bool:
        if decision not in ("allow", "reject"):
            raise ValueError("decision must be allow or reject")
        pending = self._pending.get(request_id)
        if pending is None or pending.deciding or pending.future.done():
            return False
        del self._pending[request_id]
        request, future, rejected_keys = pending.request, pending.future, pending.rejected
        if decision == "reject":
            rejected = self._rejected.setdefault(request["run_id"], set())
            rejected.update(rejected_keys)
            # Settle already-pending calls covered by the same refusal as well.
            # They are independent invocations but cannot ask for declined access.
            for other_id, other in tuple(self._pending.items()):
                other_future = other.future
                if other.request["run_id"] == request["run_id"] and _is_rejected(
                    rejected, other.checks
                ):
                    self._pending.pop(other_id)
                    if not other_future.done():
                        other_future.set_result(False)
        if not future.done():
            future.set_result("permitted" if folder_read else decision == "allow")
        self._changed()
        _LOGGER.info(
            "Approval decided (request=%s run=%s decision=%s actor=user)",
            request_id,
            request["run_id"],
            decision,
        )
        return True

    def finish(self, run: Run) -> None:
        self._rejected.pop(run.id, None)
        for request_id, pending in tuple(self._pending.items()):
            future = pending.future
            if pending.request["run_id"] == run.id:
                self._pending.pop(request_id)
                if not future.done():
                    future.cancel()
                self._changed()
