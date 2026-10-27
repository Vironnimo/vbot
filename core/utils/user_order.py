"""Pure contracts shared by user-ordered Agent and Project collections.

Collection owners retain document formats, error types and persistence lifecycle,
including whether a read repairs or materializes its order.
"""

from collections.abc import Callable, Sequence
from typing import Any, Literal

from core.config_validation import (
    JsonDiagnostic,
    add_error,
    validate_positive_integer,
    validate_required_fields,
    validate_string_list,
)


def validate_order_fields(
    diagnostics: list[JsonDiagnostic],
    data: dict[str, Any],
    *,
    ids_field: str,
    valid_id: Callable[[str], bool],
    item_label: str,
) -> None:
    """Validate the revision and unique ids after the owner's format check."""
    validate_required_fields(diagnostics, "$", data, frozenset({"revision", ids_field}))
    if "revision" in data:
        validate_positive_integer(diagnostics, "$.revision", data["revision"], required=True)
    if ids_field not in data:
        return
    ids = data[ids_field]
    path = f"$.{ids_field}"
    validate_string_list(diagnostics, path, ids)
    if isinstance(ids, list):
        seen: set[str] = set()
        for index, item_id in enumerate(ids):
            if not isinstance(item_id, str):
                continue
            if not valid_id(item_id):
                add_error(diagnostics, f"{path}[{index}]", f"must be a valid {item_label} id")
            if item_id in seen:
                add_error(diagnostics, f"{path}[{index}]", "must be unique")
            seen.add(item_id)


def apply_order[T](
    items: Sequence[T], ids: Sequence[str], *, item_id: Callable[[T], str]
) -> tuple[T, ...]:
    """Apply a valid stored order, retaining unlisted items in their input order."""
    by_id = {item_id(item): item for item in items}
    ordered = [by_id[value] for value in ids if value in by_id]
    placed = {item_id(item) for item in ordered}
    ordered.extend(item for item in items if item_id(item) not in placed)
    return tuple(ordered)


def validated_reorder_request(
    ids: Any,
    expected_revision: Any,
    *,
    ids_field: str,
    valid_id: Callable[[str], bool] | None = None,
    item_label: str = "",
) -> list[str]:
    """Check request shape, leaving the owner's identity policy optional."""
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")
    if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
        raise ValueError(f"{ids_field} must be a list of strings")
    if valid_id is not None and any(not valid_id(value) for value in ids):
        raise ValueError(f"{ids_field} must contain only valid {item_label} ids")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{ids_field} must not contain duplicates")
    return ids


def reorder_state(
    requested: Sequence[str],
    current: Sequence[str],
    *,
    expected_revision: int,
    current_revision: int,
    repair_required: bool = False,
) -> Literal["membership_conflict", "revision_conflict", "unchanged", "changed"]:
    """Require exact membership; an unrepaired no-op needs no current revision."""
    if len(requested) != len(current) or set(requested) != set(current):
        return "membership_conflict"
    if tuple(requested) == tuple(current) and not repair_required:
        return "unchanged"
    if expected_revision != current_revision:
        return "revision_conflict"
    return "changed"
