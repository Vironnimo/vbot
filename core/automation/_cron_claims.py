"""Durable once-job admission claims, independent from the running scheduler."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.automation._cron_jobs import (
    CronJob,
    CronJobValidationError,
    CronStorageError,
    _parse_utc_timestamp,
)
from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger

_LOGGER = get_logger("automation.cron")


def write(directory: Path, job: CronJob, claimed_at: str) -> None:
    claim_path = path_for(directory, job.id)
    payload = {
        "job_id": job.id,
        "claimed_at": claimed_at,
        "run_at": job.run_at,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(claim_path, serialized)
    except OSError as error:
        raise CronStorageError(f"Cannot write {claim_path}: {error}") from error


def read(directory: Path, job_id: str) -> str | None:
    claim_path = path_for(directory, job_id)
    if not claim_path.exists():
        return None

    try:
        payload = json.loads(claim_path.read_text(encoding="utf-8"))
    except UnicodeError as error:
        raise CronStorageError(f"Invalid UTF-8 in once fire claim {claim_path}: {error}") from error
    except OSError as error:
        raise CronStorageError(f"Cannot read {claim_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise CronStorageError(f"Invalid once fire claim {claim_path}: {error}") from error

    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        raise CronStorageError(f"Invalid once fire claim {claim_path}: job_id mismatch")

    claimed_at = payload.get("claimed_at")
    if not isinstance(claimed_at, str):
        raise CronStorageError(f"Invalid once fire claim {claim_path}: claimed_at is required")
    try:
        _parse_utc_timestamp(claimed_at, field_name="claimed_at")
    except CronJobValidationError as error:
        raise CronStorageError(f"Invalid once fire claim {claim_path}: {error}") from error
    return claimed_at


def remove(directory: Path, job_id: str) -> None:
    claim_path = path_for(directory, job_id)
    try:
        claim_path.unlink(missing_ok=True)
    except OSError as error:
        _LOGGER.warning(
            "Cannot remove once job fire claim for job=%s: %s",
            job_id,
            error,
        )


def path_for(directory: Path, job_id: str) -> Path:
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"
