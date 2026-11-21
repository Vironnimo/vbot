"""Keep recognized API keys from group attachments out of storage and Runs."""

from collections.abc import Callable
from pathlib import Path

from core.attachments import AttachmentRecord, AttachmentStore, sniff_media_type
from core.chat.content_blocks import ContentBlock, FileBlock, MediaBlock, TextBlock
from core.secrets.detection import contains_potential_api_key, detect_api_keys
from core.utils.workers import BoundedWorkerPool

_ATTACHMENT_WORKERS = BoundedWorkerPool(name="channel-attachments", max_workers=2)


class GroupAttachmentKeyError(ValueError):
    def __init__(self, message_id: str | None) -> None:
        super().__init__("Group attachment contains an API key")
        self.message_id = message_id


def _has_key(text: str) -> bool:
    return contains_potential_api_key(text) and bool(detect_api_keys(text))


async def store_inbound_attachment(
    store: AttachmentStore, filename: str, data: bytes, *, group: bool, message_id: str | None
) -> AttachmentRecord:
    if not group:
        return await store.store_async(filename, data)

    def checked_store() -> AttachmentRecord:
        store.ensure_within_limit(len(data))
        if _has_key(filename) or (
            sniff_media_type(data, filename).startswith("text/") and _has_key(data.decode("utf-8"))
        ):
            raise GroupAttachmentKeyError(message_id)
        return store.store(filename, data)

    return await _ATTACHMENT_WORKERS.run(checked_store)


async def group_content_has_key(
    blocks: list[ContentBlock], store: AttachmentStore | None, secret_command: Callable[[str], bool]
) -> bool:
    """Also check blocks from adapters that return existing attachments by reference."""

    def file_has_key(block: FileBlock) -> bool:
        if store is None:
            raise ValueError("Cannot inspect the group attachment without its store")
        record = store.get(block.attachment_id)
        with Path(record.file_path).open(encoding="utf-8") as source:
            return any(_has_key(line) for line in source)

    for block in blocks:
        if isinstance(block, TextBlock) and (_has_key(block.text) or secret_command(block.text)):
            return True
        if isinstance(block, (MediaBlock, FileBlock)) and _has_key(block.filename):
            return True
        if (
            isinstance(block, FileBlock)
            and block.media_type.startswith("text/")
            and await _ATTACHMENT_WORKERS.run(file_has_key, block)
        ):
            return True
    return False
