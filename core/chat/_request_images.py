"""Run-local request image admission and prompt-epoch retirement."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from core.chat.errors import ImageBudgetExceededError
from core.chat.messages import (
    JsonObject,
)
from core.providers.adapter import (
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
)

# Compaction starts a new epoch without it.
PINNED_IMAGE_RETIREMENT_SLOT = "pinned_image_retirement"
# Delivery evidence survives Compaction, which resets retirement but retains images.
PINNED_IMAGE_DELIVERY_SLOT = "pinned_image_delivery"
_IMAGE_BUDGET_NOTE = (
    "[This image was supplied in an earlier Model request and has now been omitted "
    "to make room for more images. Its file path remains available. "
    "Read the file again if you need to inspect it.]"
)


# (role, message identity, block index): where an image sits in every request.
_ImageAddress = tuple[str, str, int]
# The address plus a payload digest: which exact pixels a response acknowledged.
_ImageKey = tuple[str, str, int, str]


@dataclass(frozen=True)
class _RequestImage:
    message_index: int
    field: str
    block_index: int
    address: _ImageAddress | None
    key: _ImageKey | None
    size: int


def _request_images(messages: list[JsonObject]) -> list[_RequestImage]:
    images: list[_RequestImage] = []
    for message_index, message in enumerate(messages):
        role = message.get("role")
        if role not in {"user", "tool"}:
            continue
        field = TOOL_RESULT_CONTENT_BLOCKS_FIELD if role == "tool" else "content"
        content = message.get(field)
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if (
                isinstance(block, dict)
                and block.get("type") == "media"
                and str(block.get("media_type", "")).startswith("image/")
                and isinstance(block.get("base64"), str)
            ):
                identity = message.get("id") or message.get("tool_call_id")
                address: _ImageAddress | None = None
                key: _ImageKey | None = None
                if isinstance(identity, str) and identity:
                    address = (str(role), identity, block_index)
                    # Hashes retain no pixels. Changed hook content or format conversion
                    # at the same address must be delivered before it becomes eligible.
                    digest = hashlib.sha256(block["media_type"].encode())
                    digest.update(block["base64"].encode())
                    key = (*address, digest.hexdigest())
                images.append(
                    _RequestImage(
                        message_index, field, block_index, address, key, len(block["base64"])
                    )
                )
    return images


def _retirement_addresses(pin: JsonObject | None) -> set[_ImageAddress]:
    entries = pin.get("images") if isinstance(pin, dict) else None
    if not isinstance(entries, list):
        return set()
    return {
        (entry[0], entry[1], entry[2])
        for entry in entries
        if isinstance(entry, list)
        and len(entry) == 3
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
        and isinstance(entry[2], int)
    }


def _delivery_keys(pin: JsonObject | None) -> set[_ImageKey]:
    entries = pin.get("images") if isinstance(pin, dict) else None
    if not isinstance(entries, list):
        return set()
    return {
        (entry[0], entry[1], entry[2], entry[3])
        for entry in entries
        if isinstance(entry, list)
        and len(entry) == 4
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
        and isinstance(entry[2], int)
        and not isinstance(entry[2], bool)
        and isinstance(entry[3], str)
    }


def retain_delivery_pin(pin: JsonObject | None, messages: list[JsonObject]) -> JsonObject | None:
    """Keep evidence only for messages retained in the active request."""
    identities = {
        (message.get("role"), message.get("id") or message.get("tool_call_id"))
        for message in messages
        if message.get("role") in {"user", "tool"}
        and isinstance(message.get("id") or message.get("tool_call_id"), str)
    }
    kept = sorted(key for key in _delivery_keys(pin) if key[:2] in identities)
    return {"images": [list(key) for key in kept]} if kept else None


@dataclass
class RequestImageBudget:
    """Which images a Session's requests still carry, shared by rebuilds and fallback.

    Every image stays in every request until a Provider limit forces room: a
    documented image count (``image_limit``) or a rejected body size. Then the
    oldest delivered images are retired until the request holds at most half
    that limit, so retirements stay rare and the request prefix before the
    oldest retired image stays cached. Retired images render as a fixed note.

    The retired addresses persist in the Session's prompt epoch
    (``PINNED_IMAGE_RETIREMENT_SLOT``), so later Runs render the same notes,
    and Compaction starts over. Exact image keys acknowledged by a successful
    response persist separately, including across Compaction; an image without
    that evidence stays fresh (``record_delivered``).

    Projection is pure unless remember=True is used on the live request view.
    """

    _delivered: set[_ImageKey] = field(default_factory=set)
    _retired: set[_ImageAddress] = field(default_factory=set)
    _restored: bool = False
    _unsaved: bool = False
    _unsaved_delivery: bool = False

    @property
    def restored(self) -> bool:
        return self._restored

    def restore(
        self,
        retirement_pin: JsonObject | None,
        delivery_pin: JsonObject | None,
    ) -> None:
        """Restore retired addresses and exact acknowledged image keys."""
        self._restored = True
        self._retired |= _retirement_addresses(retirement_pin)
        self._delivered.update(_delivery_keys(delivery_pin))

    def take_unsaved_pin(self) -> JsonObject | None:
        """Return the pin value once new retirements need persisting, else ``None``."""
        if not self._unsaved:
            return None
        self._unsaved = False
        return {"images": [list(address) for address in sorted(self._retired)]}

    def take_unsaved_delivery_pin(self) -> JsonObject | None:
        """Return acknowledged keys when new delivery evidence needs persisting."""
        if not self._unsaved_delivery:
            return None
        self._unsaved_delivery = False
        return {"images": [list(key) for key in sorted(self._delivered)]}

    def record_delivered(self, messages: list[JsonObject]) -> None:
        delivered = {image.key for image in _request_images(messages) if image.key}
        retained = retain_delivery_pin({"images": [list(key) for key in self._delivered]}, messages)
        # A hook or conversion can change pixels at one address. Only its latest
        # delivered payload needs evidence, so repeated changes cannot grow the pin.
        addresses = {key[:3] for key in delivered}
        prior = {key for key in _delivery_keys(retained) if key[:3] not in addresses}
        updated = prior | delivered
        if updated != self._delivered:
            self._delivered = updated
            self._unsaved_delivery = True

    def project(
        self,
        messages: list[JsonObject],
        *,
        image_limit: int | None = None,
        remember: bool = False,
    ) -> list[JsonObject]:
        """Apply retirements and keep the request within *image_limit* images."""
        return self._project(messages, image_limit=image_limit, remember=remember)

    def shrink(
        self,
        messages: list[JsonObject],
        *,
        max_bytes: int | None,
        image_limit: int | None = None,
    ) -> list[JsonObject]:
        """Retire images after a body-size rejection; unchanged when none can go.

        Retires the oldest delivered images until the image data takes at most
        half of *max_bytes*, or half of its current size when the limit is
        unknown, and always at least one.
        """
        return self._project(
            messages, image_limit=image_limit, remember=True, shrink=True, max_bytes=max_bytes
        )

    def _project(
        self,
        messages: list[JsonObject],
        *,
        image_limit: int | None,
        remember: bool,
        shrink: bool = False,
        max_bytes: int | None = None,
    ) -> list[JsonObject]:
        images = _request_images(messages)
        active = [image for image in images if image.address not in self._retired]
        # Oldest first: retiring the oldest keeps the longest cached prefix intact.
        delivered = [image for image in active if image.key in self._delivered]
        retiring: list[_RequestImage] = []
        if image_limit is not None and len(active) > image_limit:
            fresh = len(active) - len(delivered)
            if fresh > image_limit:
                raise ImageBudgetExceededError(fresh, image_limit)
            retiring = delivered[: len(active) - max(image_limit // 2, fresh)]
        if shrink:
            image_bytes = sum(image.size for image in active) - sum(
                image.size for image in retiring
            )
            target = (max_bytes if max_bytes is not None else image_bytes) // 2
            for count, image in enumerate(delivered[len(retiring) :]):
                if count and image_bytes <= target:
                    break
                retiring.append(image)
                image_bytes -= image.size
        retired = self._retired | {image.address for image in retiring if image.address}
        if remember and len(retired) > len(self._retired):
            self._retired = retired
            self._unsaved = True
        result = list(messages)
        copied: set[int] = set()
        for image in images:
            if image.address not in retired:
                continue
            index, field_name = image.message_index, image.field
            if index not in copied:
                result[index] = dict(messages[index])
                copied.add(index)
            if result[index][field_name] is messages[index][field_name]:
                result[index][field_name] = list(messages[index][field_name])
            result[index][field_name][image.block_index] = {
                "type": "text",
                "text": _IMAGE_BUDGET_NOTE,
            }
        return result


def limit_request_images(
    messages: list[JsonObject],
    *,
    budget: RequestImageBudget | None = None,
    image_limit: int | None = None,
    remember: bool = False,
) -> list[JsonObject]:
    """Apply the Session's image retirements and the route's image-count limit."""
    return (budget if budget is not None else RequestImageBudget()).project(
        messages, image_limit=image_limit, remember=remember
    )
