"""Collect an accepted Provider video job after the foreground call stops waiting."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import sqlite3
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.chat.wire_shaping import quote_external_value
from core.database import DatabaseUnavailableError
from core.model_tasks.artifacts import (
    BeforeOutputWrite,
    GeneratedMediaArtifact,
    OutputWriteError,
    persist_artifacts,
    persist_guarded_artifact,
)
from core.model_tasks.model_tasks import TaskModelTargetRef, parse_task_model_target_id
from core.model_tasks.video_providers import (
    ProviderVideoClient,
    VideoJobEndedError,
    VideoJobUncollectableError,
)
from core.model_tasks.video_types import VideoGenerationResult
from core.providers.errors import ProviderContentRefusedError, ProviderError
from core.runs import RunAdmissionBlockedError, RunNotFoundError
from core.sessions.video_jobs import VideoJob
from core.utils.atomic import atomic_write_bytes, write_new_bytes
from core.utils.errors import VBotError
from core.utils.file_status import is_link_status
from core.utils.logging import get_logger
from core.utils.workers import cancel_and_wait, settle_before_cancelling

if TYPE_CHECKING:
    from core.runtime.runtime import Runtime
    from core.sessions import SessionAddress

_LOGGER = get_logger("video.recovery")


@dataclass(frozen=True)
class VideoDelivery:
    generation: str
    run_id: str
    call_id: str
    root: Path


class VideoDestinationChangedError(PermissionError):
    """The original destination or authority no longer permits publishing the video."""


def video_extension(media_type: str) -> str:
    return "webm" if media_type.partition(";")[0].strip().lower() == "video/webm" else "mp4"


class VideoRecovery:
    def __init__(
        self, runtime: Runtime, authorize: Callable[[VideoJob, Path], Awaitable[None]]
    ) -> None:
        self.runtime = runtime
        self._authorize = authorize
        self._worker: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._collections: dict[str, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(4)

    def start(self) -> None:
        if self._closing:
            return
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._watch(), name="video-job-recovery")

    async def close(self) -> None:
        self._closing = True
        if self._worker is not None:
            await cancel_and_wait(self._worker, *self._collections.values())

    async def pin(self, target: TaskModelTargetRef) -> TaskModelTargetRef:
        sessions = self.runtime.chat_sessions
        account = await sessions.run_async(
            self.runtime.provider_credentials.resolve_account_id,
            target.provider_id,
            target.local_connection_id,
            target.account_id or None,
        )
        return parse_task_model_target_id(
            f"{target.provider_id}/{target.model_id}::{target.local_connection_id}:{account}"
        )

    async def accepted(
        self,
        identifier: str,
        delivery: VideoDelivery,
        directory: Path,
        target: str,
        job_id: str,
        origin: str,
    ) -> None:
        sessions = self.runtime.chat_sessions
        await settle_before_cancelling(
            sessions.run_async(
                sessions.video_jobs.record,
                identifier,
                delivery.generation,
                delivery.run_id,
                delivery.call_id,
                job_id,
                target,
                origin,
                str(directory),
                str(delivery.root),
            )
        )
        self.start()

    def cancel_at_provider(self, run_id: str) -> bool:
        return self.runtime.chat_run_manager.get(run_id).cancel_reason not in {"shutdown", "update"}

    async def forget(self, identifier: str) -> None:
        sessions = self.runtime.chat_sessions
        await settle_before_cancelling(sessions.run_async(sessions.video_jobs.forget, identifier))

    async def is_recorded(self, identifier: str) -> bool:
        sessions = self.runtime.chat_sessions
        try:
            return bool(await sessions.run_async(sessions.video_jobs.pending, identifier))
        except DatabaseUnavailableError, sqlite3.Error, OSError:
            return False

    async def save(
        self,
        identifier: str,
        directory: Path,
        result: VideoGenerationResult,
        guard: BeforeOutputWrite,
    ) -> GeneratedMediaArtifact:
        digest = await persist_artifacts(_digest, result.data)
        path = directory / f"{identifier}.{video_extension(result.media_type)}"
        sessions = self.runtime.chat_sessions
        try:
            await sessions.run_async(
                sessions.video_jobs.ready, identifier, result.media_type, digest
            )
        except DatabaseUnavailableError, sqlite3.Error, OSError:
            _LOGGER.exception("Could not record video checksum (receipt=%s)", identifier)
        try:
            await persist_guarded_artifact(path, guard, _save_once, path, result.data, digest)
        except VideoDestinationChangedError:
            raise
        except (OSError, VBotError, ValueError) as error:
            raise OutputWriteError(directory, str(error)) from error
        return GeneratedMediaArtifact(
            identifier, path.name, result.media_type, len(result.data), path
        )

    async def _watch(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                jobs = await self.runtime.chat_sessions.run_async(
                    self.runtime.chat_sessions.video_jobs.pending
                )
                if not jobs and not self._wake.is_set():
                    return
                for job in jobs:
                    if job.state == "running":
                        continue
                    if job.id not in self._collections:
                        task = asyncio.create_task(self._try_collect(job), name="video-job-collect")
                        self._collections[job.id] = task
            except Exception:
                _LOGGER.exception("Video job recovery pass failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=60)

    async def _try_collect(self, job: VideoJob) -> None:
        try:
            try:
                await self._collect(job)
            except VideoDestinationChangedError:
                await self._deliver(
                    job,
                    "The destination or its permissions changed. "
                    f"Provider job ID: {quote_external_value(job.job_id)}",
                )
        except RunAdmissionBlockedError:
            await self.runtime.chat_sessions.run_async(
                self.runtime.chat_sessions.video_jobs.forget, job.id
            )
        except (OSError, VBotError, ValueError) as error:
            _LOGGER.debug(
                "Collection of accepted video deferred (receipt=%s error=%s)",
                job.id,
                type(error).__name__,
            )
        except Exception:
            _LOGGER.exception("Collection of accepted video failed (receipt=%s)", job.id)
        finally:
            self._collections.pop(job.id, None)

    async def _collect(self, job: VideoJob) -> None:
        sessions = self.runtime.chat_sessions
        if job.state in {"finished", "stopped"}:
            await sessions.run_async(sessions.video_jobs.forget, job.id)
            return
        directory = Path(job.directory)
        if (
            not directory.is_absolute()
            or not job.id.startswith("vid_")
            or not job.id.removeprefix("vid_").isalnum()
        ):
            raise ValueError("Invalid video recovery destination")

        async def guard(path: Path) -> None:
            await self._check_origin(job)
            await self._authorize(job, path)

        path = directory / f"{job.id}.{video_extension(job.media_type or 'video/mp4')}"
        await guard(path)
        already_saved = (
            job.digest is not None
            and job.media_type is not None
            and await persist_artifacts(_existing_video, path, job.digest)
        )
        if not already_saved:
            target = parse_task_model_target_id(job.target)
            client = await ProviderVideoClient.from_runtime_async(self.runtime, target)
            try:
                async with self._slots:
                    await guard(path)
                    result = await client.collect_existing(job.job_id, job.origin)
            except VideoJobUncollectableError:
                await self._deliver(
                    job,
                    "The Provider returned a video vBot could not read. "
                    f"Provider job ID: {quote_external_value(job.job_id)}",
                )
                return
            except ProviderError as error:
                if isinstance(
                    error, (VideoJobEndedError, ProviderContentRefusedError)
                ) or error.status_code in {404, 410}:
                    await self._deliver(job, "The Provider no longer has a video to collect.")
                    return
                raise
            if result is None:
                return
            artifact = await self.save(job.id, directory, result, guard)
            path = artifact.file_path
        await guard(path)
        await self._deliver(
            job, f"Video saved to {quote_external_value(path.as_posix())}", path=path
        )

    async def _check_origin(self, job: VideoJob) -> None:
        sessions = self.runtime.chat_sessions
        current = await sessions.run_async(sessions.video_jobs.pending, job.id)
        if (
            not current
            or current[0].state in {"running", "finished", "stopped"}
            or current[0].address != job.address
        ):
            raise ValueError("Original video call is no longer awaiting delivery")
        if job.owner is not None:
            self.runtime._validate_owned_completion(job.address, job.owner)
        if current[0].state == "pending":
            with suppress(RunNotFoundError):
                if self.runtime.chat_run_manager.get(job.run_id).cancel_requested:
                    raise ValueError("Original video Run is stopping")

    async def _deliver(self, job: VideoJob, message: str, *, path: Path | None = None) -> None:
        async def recheck(_address: SessionAddress, body: str) -> str:
            try:
                await self._check_origin(job)
            except ValueError:
                # A request boundary may have committed the first notice before its
                # Run was interrupted. That receipt no longer exposes a fresh path.
                return f"Video job {job.id}: This result is no longer pending."
            if path is not None:
                try:
                    await self._authorize(job, path)
                except VideoDestinationChangedError:
                    return f"Video job {job.id}: The destination or its permissions changed."
            return body

        await self._check_origin(job)
        await self.runtime.trigger_service.submit_completion(
            job.address,
            notice_id=f"video:{job.id}",
            origin_run_id=job.run_id,
            expected_session_generation_id=job.generation,
            body=f"Video job {job.id}: {message}",
            execution_owner=job.owner,
            filter_body=recheck,
        )
        # Shutdown can withdraw an unclaimed notice. Only canonical history, as
        # observed by the next pass, acknowledges and removes the receipt.


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _existing_video(path: Path, digest: str) -> bool:
    try:
        entry = path.lstat()
        if is_link_status(entry) or not stat.S_ISREG(entry.st_mode):
            raise VideoDestinationChangedError("The video destination is no longer a regular file")
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == digest
    except FileNotFoundError:
        return False


def _save_once(path: Path, data: bytes, digest: str) -> None:
    try:
        try:
            atomic_write_bytes(path, data, replace_existing=False)
        except OSError as error:
            if error.errno not in {errno.ENOTSUP, errno.EPERM} and getattr(
                error, "winerror", None
            ) not in {1, 50}:
                raise
            # Some removable/network filesystems have no hard links. The receipt
            # and digest distinguish complete published output on the next pass.
            write_new_bytes(path, data)
    except FileExistsError:
        if not _existing_video(path, digest):
            raise VideoDestinationChangedError(
                "The previously chosen video filename is occupied"
            ) from None
