"""Prompt-epoch dynamic prompt blocks: their pinned texts and the changes told since.

Contributed Tool and Extension blocks, temporary Session blocks, and
``core:channels`` follow live contributor state. The first request pins their
texts, membership and layout positions (:class:`PromptBlockPin`), and later
requests reuse them; a successful Compaction starts the next epoch
with fresh texts. A change in between reaches the Model at the start of the next
Run as one ``[prompt-block-change]`` note: a System Reminder that lists the
changed entries of a catalog block, or carries the current text of any other
block. The note stores the blocks' new state next to its text, so
:func:`plan_prompt_block_change` rebuilds what the Model knows from the pin and
the Session's notes of the same epoch.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from core.chat.messages import ChatMessage, JsonObject
from core.prompts import BlockCatalog, RenderedBlock
from core.prompts.pinned_context import PINNED_DYNAMIC_BLOCKS_SLOT
from core.sessions import PROMPT_BLOCK_CHANGE_NOTE_PREFIX, is_prompt_block_change_note

if TYPE_CHECKING:
    from core.sessions import ChatSessionManager, SessionAddress

PROMPT_BLOCK_PIN_VERSION = 1
PROMPT_BLOCK_CHANGE_NOTE_VERSION = 1

_CATALOG_CHANGED = "{title} changed since your System Prompt listed them."
_SECTION_CHANGED = (
    'The "{section}" section of your System Prompt changed. Use this current version '
    "instead:\n\n{text}"
)
_SECTION_REMOVED = 'The "{section}" section of your System Prompt no longer applies.'
_SECTION_ADDED = "This section now applies in addition to your System Prompt:\n\n{text}"
_SECTION_NAME_LIMIT = 80

# Captures the dynamic blocks and their positions for a fresh epoch.
LiveBlockRenderer = Callable[[], "PromptBlockPin"]


@dataclass(frozen=True)
class PromptBlockPin:
    """The dynamic block texts every request of one prompt epoch shows, by block id.

    ``epoch`` keys the change notes of this epoch; notes of another epoch are
    ignored. Membership, text and layout order stay fixed for the whole epoch;
    additions and removals are announced in its history instead.
    """

    epoch: str
    blocks: Mapping[str, RenderedBlock]
    order: tuple[str, ...] = ()

    @classmethod
    def start(
        cls, blocks: Mapping[str, RenderedBlock], *, order: Iterable[str] = ()
    ) -> PromptBlockPin:
        """Start a new epoch that shows *blocks*."""

        return cls(epoch=uuid.uuid4().hex, blocks=dict(blocks), order=tuple(order))

    def texts(self) -> dict[str, str]:
        """Return the pinned text of every block, by block id."""

        return {block_id: block.text for block_id, block in self.blocks.items()}

    def to_payload(self) -> JsonObject:
        """Return the persisted pin value."""

        return {
            "v": PROMPT_BLOCK_PIN_VERSION,
            "epoch": self.epoch,
            "blocks": _blocks_payload(self.blocks),
            "order": list(self.order),
        }

    @classmethod
    def from_payload(cls, payload: object) -> PromptBlockPin | None:
        """Parse a persisted pin; ``None`` when it is unreadable or of another version."""

        if not isinstance(payload, dict) or payload.get("v") != PROMPT_BLOCK_PIN_VERSION:
            return None
        epoch = payload.get("epoch")
        blocks = _blocks_from_payload(payload.get("blocks"))
        if not isinstance(epoch, str) or not epoch or blocks is None:
            return None
        order = payload.get("order", [])
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            return None
        return cls(epoch=epoch, blocks=blocks, order=tuple(order))


@dataclass(frozen=True)
class PromptBlockChange:
    """One announcement of changed prompt blocks: their new state and its reminder text."""

    epoch: str
    blocks: Mapping[str, RenderedBlock]
    text: str

    def note_content(self) -> str:
        """Return the note content that persists this announcement."""

        payload: JsonObject = {
            "v": PROMPT_BLOCK_CHANGE_NOTE_VERSION,
            "epoch": self.epoch,
            "blocks": _blocks_payload(self.blocks),
            "text": self.text,
        }
        return PROMPT_BLOCK_CHANGE_NOTE_PREFIX + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )


def prompt_block_change_from_note(message: ChatMessage) -> PromptBlockChange | None:
    """Parse a ``[prompt-block-change]`` note; ``None`` for other or malformed notes."""

    if not is_prompt_block_change_note(message):
        return None
    content = cast(str, message.content)
    try:
        payload = json.loads(content.removeprefix(PROMPT_BLOCK_CHANGE_NOTE_PREFIX))
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("v") != PROMPT_BLOCK_CHANGE_NOTE_VERSION:
        return None
    epoch = payload.get("epoch")
    text = payload.get("text")
    blocks = _blocks_from_payload(payload.get("blocks"))
    if not isinstance(epoch, str) or not epoch or not isinstance(text, str) or not blocks:
        return None
    return PromptBlockChange(epoch=epoch, blocks=blocks, text=text)


def without_other_epoch_prompt_block_changes(
    messages: Iterable[ChatMessage], epoch: str
) -> list[ChatMessage]:
    """Drop prompt-block change notes that do not belong to *epoch*.

    Such notes describe another Agent's or an earlier epoch's blocks (after a
    Takeover, a cross-scope move or a lost pin); the current pin already shows
    what they told.
    """

    return [
        message
        for message in messages
        if not is_prompt_block_change_note(message)
        or getattr(prompt_block_change_from_note(message), "epoch", None) == epoch
    ]


def plan_prompt_block_change(
    pin: PromptBlockPin,
    messages: Iterable[ChatMessage],
    live: Mapping[str, RenderedBlock],
    *,
    unavailable: Collection[str] = (),
) -> PromptBlockChange | None:
    """Return the announcement that brings the Model's knowledge of *pin*'s blocks up to *live*.

    The Model knows each pinned block as pinned, or as this epoch's latest note
    in *messages* told it. A block whose *live* text differs is announced: a
    catalog block whose title and frame stayed the same by its added, changed
    and removed entries, any other block by its current text. ``None`` when no
    block changed. Missing blocks are announced as removed; render failures
    named in *unavailable* leave the previously announced state intact.
    """

    known = dict(pin.blocks)
    for message in messages:
        change = prompt_block_change_from_note(message)
        if change is not None and change.epoch == pin.epoch:
            known.update(change.blocks)
    changed: dict[str, RenderedBlock] = {}
    paragraphs: list[str] = []
    for block_id in dict.fromkeys([*live, *known]):
        if block_id in unavailable:
            continue
        before = known.get(block_id, RenderedBlock(""))
        block = live.get(block_id, RenderedBlock(""))
        if before.text == block.text:
            continue
        changed[block_id] = block
        paragraphs.append(_describe_change(before, block))
    if not changed:
        return None
    return PromptBlockChange(epoch=pin.epoch, blocks=changed, text="\n\n".join(paragraphs))


def ensure_prompt_block_pin(
    sessions: ChatSessionManager, address: SessionAddress, render: LiveBlockRenderer
) -> PromptBlockPin:
    """Return the epoch's immutable snapshot, capturing only when it has none."""

    stored = PromptBlockPin.from_payload(sessions.prompt_pin(address, PINNED_DYNAMIC_BLOCKS_SLOT))
    if stored is not None and stored.order:
        return stored
    captured = render()
    if stored is None:
        pin = captured

        def accept(current: JsonObject) -> bool:
            return PromptBlockPin.from_payload(current) is not None

    else:
        # Layout order is an optional additive pin field. Enrich an existing
        # snapshot without replacing its texts, membership or epoch identity.
        if not captured.order:
            return stored
        pin = replace(stored, order=captured.order)

        def accept(current: JsonObject) -> bool:
            other = PromptBlockPin.from_payload(current)
            return other is not None and (other.epoch != pin.epoch or bool(other.order))

    pinned = sessions.ensure_prompt_pin(
        address, PINNED_DYNAMIC_BLOCKS_SLOT, pin.to_payload(), accept
    )
    return PromptBlockPin.from_payload(pinned) or pin


def _describe_change(before: RenderedBlock, after: RenderedBlock) -> str:
    text = after.text.strip()
    # A block that starts or stops showing is told whole: its entries alone
    # would leave out the rest of its text.
    if not before.text.strip():
        return _SECTION_ADDED.format(text=text)
    section = _section_name(before.text)
    if not text:
        return _SECTION_REMOVED.format(section=section)
    old, new = before.catalog, after.catalog
    if old is not None and new is not None and (old.title, old.frame) == (new.title, new.frame):
        described = _describe_entries(new.title, old.entries, new.entries)
        if described is not None:
            return described
    return _SECTION_CHANGED.format(section=section, text=text)


def _describe_entries(
    title: str,
    before: tuple[tuple[str, str], ...],
    after: tuple[tuple[str, str], ...],
) -> str | None:
    old = dict(before)
    new = dict(after)
    groups = (
        ("Added:", [line for key, line in after if key not in old]),
        ("Changed to:", [line for key, line in after if key in old and old[key] != line]),
        ("Removed:", [line for key, line in before if key not in new]),
    )
    if not any(lines for _label, lines in groups):
        return None
    parts = [_CATALOG_CHANGED.format(title=title)]
    parts.extend("\n".join([label, *lines]) for label, lines in groups if lines)
    return "\n".join(parts)


def _section_name(text: str) -> str:
    """Name a block by its Markdown heading, else by the start of its first line."""

    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line.startswith("#"):
        return first_line.lstrip("#").strip()
    if len(first_line) > _SECTION_NAME_LIMIT:
        return first_line[: _SECTION_NAME_LIMIT - 3].rstrip() + "..."
    return first_line


def _blocks_payload(blocks: Mapping[str, RenderedBlock]) -> JsonObject:
    payload: JsonObject = {}
    for block_id, block in blocks.items():
        value: JsonObject = {"text": block.text}
        if block.catalog is not None:
            # A list keeps the entry order: the Session store sorts object keys.
            value["catalog"] = {
                "title": block.catalog.title,
                "frame": block.catalog.frame,
                "entries": [[key, line] for key, line in block.catalog.entries],
            }
        payload[block_id] = value
    return payload


def _blocks_from_payload(payload: object) -> dict[str, RenderedBlock] | None:
    if not isinstance(payload, dict):
        return None
    blocks: dict[str, RenderedBlock] = {}
    for block_id, value in payload.items():
        block = _block_from_payload(value)
        if not isinstance(block_id, str) or block is None:
            return None
        blocks[block_id] = block
    return blocks


def _block_from_payload(value: Any) -> RenderedBlock | None:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return None
    catalog = value.get("catalog")
    if catalog is None:
        return RenderedBlock(value["text"])
    if not isinstance(catalog, dict):
        return None
    title = catalog.get("title")
    frame = catalog.get("frame")
    entries = catalog.get("entries")
    if not isinstance(title, str) or not isinstance(frame, str) or not isinstance(entries, list):
        return None
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(part, str) for part in entry)
        ):
            return None
        pairs.append((entry[0], entry[1]))
    return RenderedBlock(
        value["text"], BlockCatalog(title=title, entries=tuple(pairs), frame=frame)
    )
