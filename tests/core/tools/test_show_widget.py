"""The show_widget built-in Tool: what it stores, shows and tells the Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ReplySurface
from core.tools.show_widget import (
    MAX_WIDGET_CODE_BYTES,
    REFUSAL_PREFIX,
    SHOW_WIDGET_TOOL_NAME,
    register_show_widget_tool,
)
from core.tools.tools import ToolRegistry
from tests.core.tools.image_test_support import make_context
from tests.core.tools.tools_test_support import dispatch_as_executor


class _Payloads:
    """Stands in for the Session that keeps a call's result payloads."""

    def __init__(self) -> None:
        self.stored: list[tuple[str, Any]] = []

    def __call__(self, _call_id: str, tool_name: str, payload: Any) -> str:
        self.stored.append((tool_name, payload))
        return "res_widget"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_show_widget_tool(registry)
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "elsewhere"),
    [(None, None), (ReplySurface.webui(), None), (ReplySurface.tui(), "the terminal")]
    + [
        (
            ReplySurface.channel(
                platform="telegram", platform_display_name="Telegram", channel_id="c"
            ),
            "Telegram",
        )
    ],
    ids=["no-surface", "webui", "tui", "channel"],
)
async def test_a_widget_is_kept_with_its_result_and_shown_after_the_call(
    tmp_path: Path, surface: ReplySurface | None, elsewhere: str | None
) -> None:
    registry = _registry()
    payloads = _Payloads()
    context = make_context(
        tmp_path, SHOW_WIDGET_TOOL_NAME, result_payload_hook=payloads, reply_surface=surface
    )
    # Spellings from other harnesses work; the title becomes one short line.
    arguments = {"name": "  Text\n spacing ", "html": "<input type=range name=gap>"}

    result = await dispatch_as_executor(registry, context, arguments)

    assert result["ok"] is True
    assert payloads.stored == [
        (
            SHOW_WIDGET_TOOL_NAME,
            {"title": "Text spacing", "widget_code": "<input type=range name=gap>"},
        )
    ]
    content = result["data"]["content"]
    assert '"Text spacing"' in content
    if elsewhere is None:
        assert "do not show" not in content
    else:
        assert f"using {elsewhere}, where widgets do not show" in content
    display = registry.display_for_call(
        SHOW_WIDGET_TOOL_NAME,
        {"title": "Text spacing", "widget_code": "<input type=range name=gap>"},
        context=context,
        result=result,
    )
    assert display["widget_reference"] == {"id": "res_widget", "title": "Text spacing"}
    # The HTML shows as a detail block, not as a raw argument.
    assert "widget_code" not in str(display.get("arguments", {}))
    assert display["details"] == [
        {
            "type": "text",
            "label": "content",
            "source": {"from": "arguments", "path": ["widget_code"]},
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"title": " ", "widget_code": "<p>x</p>"}, "title is empty"),
        ({"title": "T", "widget_code": "  \n"}, "widget_code is empty"),
        (
            {"title": "T", "widget_code": "x" * (MAX_WIDGET_CODE_BYTES + 1)},
            "has 1025 KiB; the limit is 1024 KiB",
        ),
    ],
    ids=["empty-title", "empty-code", "oversized-code"],
)
async def test_an_unusable_widget_is_refused_before_anything_is_kept(
    tmp_path: Path, arguments: dict[str, Any], named: str
) -> None:
    payloads = _Payloads()
    context = make_context(tmp_path, SHOW_WIDGET_TOOL_NAME, result_payload_hook=payloads)

    result = await dispatch_as_executor(_registry(), context, arguments)

    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"].startswith(REFUSAL_PREFIX)
    assert named in result["error"]["message"]
    assert payloads.stored == []


@pytest.mark.asyncio
async def test_outside_a_session_nothing_is_shown(tmp_path: Path) -> None:
    context = make_context(tmp_path, SHOW_WIDGET_TOOL_NAME)

    result = await dispatch_as_executor(
        _registry(), context, {"title": "T", "widget_code": "<p>x</p>"}
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "session_required"
    assert result["error"]["message"].startswith("Nothing was shown.")
