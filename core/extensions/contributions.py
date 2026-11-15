"""Public declarations for Extension presentation, independent of any UI framework."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any

EXTENSION_FEATURES = frozenset(
    {
        "ui.pages",
        "ui.contributions",
        "ui.preferences",
        "services",
        "observations",
        "providers",
        "channels",
        "desktop.manifest",
    }
)
UI_SURFACES = frozenset(
    {
        "shell.status",
        "shell.sidebar",
        "chat.header",
        "chat.composer.actions",
        "chat.composer.context",
        "chat.composer.footer",
        "chat.composer.footer.start",
        "chat.composer.footer.end",
        "chat.timeline",
        "chat.tool-result",
        "settings.sections",
    }
)
UI_CONTEXT_FIELDS = frozenset(
    {
        "agent_id",
        "session_id",
        "project_id",
        "run_id",
        "run_active",
        "has_draft",
        "has_session",
        "settings_section",
        "tool_name",
        "tool_status",
        "tool_call_id",
        "connected",
        "view",
        "computer_id",
        "context_tokens",
        "context_window",
        "session_cost",
        "output_speed",
    }
)
PAGE_ICONS = frozenset({"network", "panel", "grid", "sparkles", "factory"})
_ID = re.compile(r"[a-z][a-z0-9_-]{0,79}")
_PATH = re.compile(r"[MmLlHhVvCcSsQqTtAaZz0-9.eE,+\s-]+")


@dataclass(frozen=True)
class ContributionDeclaration:
    """One registration-owned contribution; specs contain only detached JSON values."""

    id: str
    spec: dict[str, Any]


def validate_icon(icon: object) -> str | dict[str, Any]:
    """Accept a known icon or bounded SVG path geometry, never markup or URLs."""
    if isinstance(icon, str) and icon in PAGE_ICONS:
        return icon
    if not isinstance(icon, dict) or set(icon) - {"paths", "view_box"}:
        raise ValueError("icon must be a known icon name or path geometry")
    paths = icon.get("paths")
    view_box = icon.get("view_box", "0 0 16 16")
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 32
        or any(
            not isinstance(path, str) or not 0 < len(path) <= 8192 or not _PATH.fullmatch(path)
            for path in paths
        )
    ):
        raise ValueError("icon paths must contain bounded SVG path data")
    if not isinstance(view_box, str) or len(view_box) > 120:
        raise ValueError("icon view_box must contain four finite numbers")
    try:
        values = [float(value) for value in view_box.split()]
    except ValueError as exc:
        raise ValueError("icon view_box must contain four finite numbers") from exc
    if (
        len(values) != 4
        or not all(math.isfinite(value) for value in values)
        or min(values[2:]) <= 0
    ):
        raise ValueError("icon view_box must contain four finite numbers with positive size")
    return {"paths": list(paths), "view_box": view_box}


def _text(value: object, field: str, *, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be non-empty text up to {maximum} characters")
    return value


def _binding(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"context"}
        and isinstance(value["context"], str)
        and value["context"] in UI_CONTEXT_FIELDS
    )


def parse_contribution(contribution_id: str, **fields: Any) -> ContributionDeclaration:
    """Validate a declarative contribution before publishing its registration."""
    if not isinstance(contribution_id, str) or not _ID.fullmatch(contribution_id):
        raise ValueError("contribution id must be a lowercase local identifier")
    allowed = {
        "surface",
        "title",
        "kind",
        "order",
        "when",
        "replaces",
        "text",
        "icon",
        "action",
        "source",
        "page",
        "operations",
        "tone",
        "description",
    }
    if set(fields) - allowed:
        raise ValueError(f"unknown contribution fields: {', '.join(sorted(set(fields) - allowed))}")
    spec = copy.deepcopy(fields)
    if spec.get("surface") not in UI_SURFACES:
        raise ValueError("unknown contribution surface")
    _text(spec.get("title"), "title", maximum=120)
    if spec.get("kind") not in {"status", "action", "view"}:
        raise ValueError("contribution kind must be status, action or view")
    order = spec.setdefault("order", 0)
    if isinstance(order, bool) or not isinstance(order, (int, float)) or not math.isfinite(order):
        raise ValueError("contribution order must be finite")
    when = spec.setdefault("when", {})
    if not isinstance(when, dict) or set(when) - UI_CONTEXT_FIELDS:
        raise ValueError("contribution when uses unsupported context fields")
    if any(
        value is not None and not isinstance(value, (str, bool, int, float))
        for value in when.values()
    ):
        raise ValueError("contribution when values must be scalars")
    if "replaces" in spec:
        _text(spec["replaces"], "replaces", maximum=240)
    if "text" in spec and not _binding(spec["text"]):
        _text(spec["text"], "text")
    if "icon" in spec:
        spec["icon"] = validate_icon(spec["icon"])
    if "description" in spec:
        _text(spec["description"], "description")
    if "tone" in spec and spec["tone"] not in {"muted", "info", "success", "warning", "danger"}:
        raise ValueError("unsupported contribution tone")
    operations = spec.get("operations", ())
    if not isinstance(operations, (list, tuple)) or any(
        not isinstance(op, str) or not op for op in operations
    ):
        raise ValueError("contribution operations must be operation names")
    operation_names = set(operations)
    for key in ("action", "source"):
        value = spec.get(key)
        if value is None:
            continue
        permitted = (
            {"operation", "params", "resource"}
            if key == "source"
            else {"operation", "command", "params"}
        )
        if not isinstance(value, dict) or set(value) - permitted:
            raise ValueError(f"invalid contribution {key}")
        names = [name for name in ("operation", "command") if name in value]
        if len(names) != 1 or (key == "source" and names != ["operation"]):
            raise ValueError(f"contribution {key} requires one operation or command")
        _text(value[names[0]], f"{key}.{names[0]}", maximum=160)
        if "operation" in value:
            operation_names.add(value["operation"])
        if not isinstance(value.get("params", {}), dict):
            raise ValueError(f"contribution {key}.params must be an object")
        if "resource" in value:
            _text(value["resource"], "source.resource", maximum=160)
    if spec["kind"] == "view":
        if not isinstance(spec.get("page"), str) or not _ID.fullmatch(spec["page"]):
            raise ValueError("view contribution requires a local page id")
    elif "page" in spec:
        raise ValueError("only view contributions can name a page")
    if spec["kind"] == "action" and "action" not in spec and "source" not in spec:
        raise ValueError("action contribution requires an action or source")
    spec["operations"] = sorted(operation_names)
    try:
        encoded = json.dumps(spec, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("contribution must contain finite UTF-8 JSON values") from exc
    if len(encoded) > 64 * 1024:
        raise ValueError("contribution exceeds 64 KiB")
    return ContributionDeclaration(contribution_id, spec)
