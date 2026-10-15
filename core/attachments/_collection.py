"""Quiescent lifecycle reclamation of blobs whose canonical owners are gone."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from core.attachments.attachments import AttachmentError, AttachmentNotFoundError
from core.database import DatabaseError
from core.utils.file_status import is_link_status, stat_or_none
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.attachments.attachments import AttachmentStore
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("attachments")


def collect_session_orphans(store: AttachmentStore, sessions: ChatSessionManager) -> int:
    """No Run, producer or queued admission may overlap this lifecycle pass."""
    try:
        candidates = sessions.unreferenced_attachment_ids()
        if not candidates:
            return 0
        root = store._attachments_dir
        status = stat_or_none(root, follow_symlinks=False)
        if status is not None and (
            is_link_status(status)
            or not stat.S_ISDIR(status.st_mode)
            or root.resolve() != store._data_dir.resolve() / "artifacts" / "attachments"
        ):
            raise AttachmentError("Attachment collection requires its canonical directory")
    except (AttachmentError, DatabaseError, OSError) as exc:
        _LOGGER.warning("Attachment collection deferred (reason=%s)", type(exc).__name__)
        return 0

    removed = 0
    forgotten: list[str] = []
    for attachment_id in candidates:
        try:
            try:
                record = store._get_record(attachment_id, require_blob=False)
            except AttachmentNotFoundError:
                # No valid record is not authority to remove a nameless blob or
                # invalid reservation. A later pass can retry a repaired record.
                forgotten.append(attachment_id)
                continue
            blob = store._blob_path(record.id, record.media_type)
            sidecar = store._sidecar_path(record.id)
            statuses = [stat_or_none(path, follow_symlinks=False) for path in (blob, sidecar)]
            if statuses[1] is None or any(
                value is not None and (is_link_status(value) or not stat.S_ISREG(value.st_mode))
                for value in statuses
            ):
                continue
            # Remove only the pair the validated record owns. A failed second
            # unlink leaves the ledger candidate for a retry, never a fresh id.
            blob.unlink(missing_ok=True)
            sidecar.unlink()
            forgotten.append(record.id)
            removed += 1
        except (AttachmentError, OSError) as exc:
            _LOGGER.warning(
                "Attachment collection deferred (attachment=%s reason=%s)",
                attachment_id,
                type(exc).__name__,
            )
    if forgotten:
        try:
            sessions.forget_collected_attachment_ids(forgotten)
        except DatabaseError as exc:
            # The next startup can observe already removed pairs; this is only
            # ledger compaction, not authority to delete anything else.
            _LOGGER.warning("Attachment collection ledger retained (reason=%s)", type(exc).__name__)
        if removed:
            _LOGGER.info("Unreferenced attachments removed (count=%d)", removed)
    return removed
