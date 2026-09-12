"""Tests for chat integration images."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from core.chat import wire_shaping
from core.chat._request_history import (
    _restore_in_run_tool_result_content,
)
from core.chat._run_state import (
    RequestBuildInputs,
)
from core.chat.content_blocks import ContentBlock, MediaBlock
from core.chat.errors import ImageBudgetExceededError
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    TOOL_RESULT_CONTENT_BLOCKS_FIELD,
)
from core.providers.errors import ProviderRequestTooLargeError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.runtime import Runtime
from core.tools import read_media_artifact, tool_success
from core.utils.config import Config
from tests.core.chat.chat_integration_test_support import (
    FakeAdapter,
    JsonObject,
)
from tests.core.chat.chat_integration_test_support import (
    resources_dir as resources_dir,
)
from tests.core.chat.chat_loop_support import session_address

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,groups",
    [
        ("user", [4]),
        ("user", [51]),
        ("tool", [4]),
        ("tool", [1, 1, 1, 1]),
        ("tool", [51]),
        ("tool", [17, 17, 17]),
    ],
)
async def test_fresh_images_are_delivered_together_or_fail_explicitly(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    groups: list[int],
) -> None:
    count = sum(groups)
    responses: list[JsonObject] = [{"content": "done", "tool_calls": None}]
    if source == "tool":
        responses.insert(
            0,
            {
                "content": None,
                "tool_calls": [
                    {"id": f"capture-{i}", "name": "capture_images", "arguments": {"count": size}}
                    for i, size in enumerate(groups)
                ],
            },
        )
    adapter = FakeAdapter(responses)
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)
    runtime.start()
    try:
        runtime.agents.create("coder", "Coder", model="fake-provider/fake-model-vision")
        record = runtime.attachment_store.store("fixture.png", _PNG_BYTES)

        def capture(_context: Any, arguments: JsonObject) -> JsonObject:
            return tool_success(
                {"count": arguments["count"]},
                artifacts=[
                    read_media_artifact(
                        attachment_id=record.id,
                        filename=record.filename,
                        media_type=record.media_type,
                    )
                    for _ in range(arguments["count"])
                ],
            )

        runtime.tools.register(
            "capture_images",
            "Test image source.",
            {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            },
            capture,
        )
        content: str | list[ContentBlock] = "inspect the fixture images"
        if source == "user":
            content = [
                MediaBlock("media", record.id, record.filename, record.media_type)
                for _ in range(count)
            ]
        if count > 50:
            with pytest.raises(ImageBudgetExceededError) as failure:
                await runtime.chat_loop.send("coder", content, session_id="image-budget")
            assert failure.value.count == count
            assert len(adapter.requests) == (0 if source == "user" else 1)
        else:
            await runtime.chat_loop.send("coder", content, session_id="image-budget")
            messages = adapter.requests[-1].messages
            parts = (
                _tool_result_content_parts(messages)
                if source == "tool"
                else [
                    part
                    for message in messages
                    if message.get("role") == "user" and isinstance(message.get("content"), list)
                    for part in message["content"]
                ]
            )
            assert sum(part.get("type") == "media" for part in parts) == count
        persisted = runtime.chat_sessions.get(session_address("coder", "image-budget")).load()
        assert "base64" not in json.dumps([message.to_dict() for message in persisted])
        assert Path(record.file_path).read_bytes() == _PNG_BYTES
        if source == "tool":
            results = [message for message in persisted if message.role == "tool"]
            assert [message.tool_call_id for message in results] == [
                f"capture-{i}" for i in range(len(groups))
            ]
            artifact_count = 0
            for message in results:
                assert isinstance(message.content, str)
                artifact_count += len(json.loads(message.content)["artifacts"])
            assert artifact_count == count
        if count > 50:
            assert any(message.role == "error" for message in persisted)
            assert persisted[-1].to_dict()["status"] == "failed"
    finally:
        runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "tool", "text"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_provider_body_overflow_preserves_fresh_inputs_and_stops_without_retry(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    streaming: bool,
) -> None:
    rejected: list[list[JsonObject]] = []

    class LimitedAdapter(FakeAdapter):
        async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
            if source == "tool" and not self.requests:
                return await super().send(messages, model_id=model_id, **kwargs)
            rejected.append(messages)
            raise ProviderRequestTooLargeError(1001, 1000)

        async def stream(
            self, messages: list[dict], *, model_id: str, **kwargs: Any
        ) -> AsyncIterator[dict]:
            response = await self.send(messages, model_id=model_id, **kwargs)
            for call in response.get("tool_calls") or []:
                yield {
                    "type": "tool_call_delta",
                    "id": call["id"],
                    "name_delta": call["name"],
                    "arguments_delta": json.dumps(call["arguments"]),
                }
            yield {"type": "finish", "reason": "tool_calls"}

    adapter = LimitedAdapter(
        {
            "content": None,
            "tool_calls": [
                {"id": "read-image", "name": "read", "arguments": {"path": "frame.png"}}
            ],
        }
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)
    runtime.start()
    try:
        agent = runtime.agents.create("coder", "Coder", model="fake-provider/fake-model-vision")
        image_path = Path(agent.workspace) / "frame.png"
        image_path.write_bytes(_PNG_BYTES)
        content: str | list[ContentBlock] = "inspect frame.png"
        if source == "user":
            record = runtime.attachment_store.store("frame.png", _PNG_BYTES)
            content = [MediaBlock("media", record.id, record.filename, record.media_type)]
        loop = runtime.streaming_chat_loop if streaming else runtime.chat_loop
        with pytest.raises(ProviderRequestTooLargeError) as failure:
            await loop.send("coder", content, session_id="too-large")
        assert failure.value.size_bytes == 1001
        assert failure.value.max_bytes == 1000
        assert len(rejected) == 1
        assert len(adapter.requests) == (1 if source == "tool" else 0)
        if source != "text":
            assert '"type": "media"' in json.dumps(rejected[0])
        assert "omitted" not in json.dumps(rejected[0])
        persisted = runtime.chat_sessions.get(session_address("coder", "too-large")).load()
        assert persisted[-1].status == "failed"
        assert persisted[-1].iteration_count == (1 if source == "tool" else 0)
        assert sum(message.role == "tool" for message in persisted) == (
            1 if source == "tool" else 0
        )
        assert "base64" not in json.dumps([message.to_dict() for message in persisted])
        assert image_path.read_bytes() == _PNG_BYTES
    finally:
        runtime.stop()


def _tool_result_content_parts(messages: list[JsonObject]) -> list[JsonObject]:
    """Return every Run-local rich content part across Tool Results."""
    return [
        part
        for message in messages
        if message.get("role") == "tool"
        and isinstance(message.get(TOOL_RESULT_CONTENT_BLOCKS_FIELD), list)
        for part in message[TOOL_RESULT_CONTENT_BLOCKS_FIELD]
        if isinstance(part, dict)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_format", ["PNG", "BMP", "TIFF", "AVIF"])
async def test_read_image_returns_run_local_base64_in_tool_result_for_vision_model(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_format: str,
) -> None:
    class DeletingAdapter(FakeAdapter):
        async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
            if len(self.requests) == 1:
                assert Path(agent.workspace).joinpath("diagram.png").read_bytes() == original_bytes
                Path(agent.workspace).joinpath("diagram.png").unlink()
            return await super().send(messages, model_id=model_id, **kwargs)

    adapter = DeletingAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_read", "name": "read", "arguments": {"path": "diagram.png"}}
                ],
            },
            {
                "content": None,
                "tool_calls": [
                    {"id": "read_text", "name": "read", "arguments": {"path": "notes.txt"}}
                ],
            },
            {"content": "I can see the diagram.", "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create(
            "coder", "Coder Agent", model="fake-provider/fake-model-vision"
        )
        buffer = io.BytesIO()
        Image.new("RGB", (16, 12), "blue").save(buffer, format=source_format)
        original_bytes = buffer.getvalue()
        Path(agent.workspace).joinpath("diagram.png").write_bytes(original_bytes)

        Path(agent.workspace).joinpath("notes.txt").write_text("test", encoding="utf-8")
        stored_before = set((tmp_path / "data" / "artifacts" / "attachments").rglob("*"))
        assistant = await runtime.chat_loop.send(
            "coder", "Look at diagram.png", session_id="session-one"
        )

        assert assistant.content == "I can see the diagram."

        # The follow-up provider request carries the image on its correlated
        # Tool Result, without fabricating another user turn.
        tool_result_parts = _tool_result_content_parts(adapter.requests[1].messages)
        media_parts = [part for part in tool_result_parts if part.get("type") == "media"]
        assert len(media_parts) == 1
        assert media_parts[0]["media_type"] == "image/png"
        if source_format == "PNG":
            assert base64.b64decode(media_parts[0]["base64"]) == original_bytes
        later_parts = _tool_result_content_parts(adapter.requests[2].messages)
        assert [part for part in later_parts if part.get("type") == "media"] == media_parts
        assert set((tmp_path / "data" / "artifacts" / "attachments").rglob("*")) == stored_before
        assert runtime.chat_runs is not None
        assert "base64" not in json.dumps(
            [event.payload for run in runtime.chat_runs._runs.values() for event in run.events]
        )
        with Image.open(io.BytesIO(base64.b64decode(media_parts[0]["base64"]))) as delivered:
            assert delivered.size == (16, 12)

        # The canonical Session persists only the original user turn and the
        # compact Tool envelope; request-only base64 never reaches history.
        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
            "run_summary",
        ]
        assert all(
            not isinstance(message.content, list) for message in messages if message.role == "user"
        )
        persisted = json.dumps([message.to_dict() for message in messages])
        assert "base64" not in persisted

        assert isinstance(adapter.response, list)
        adapter.response.append({"content": "The image is no longer active.", "tool_calls": None})
        await runtime.chat_loop.send(
            "coder",
            "Continue without reopening it.",
            session_id="session-one",
        )
        next_run_messages = adapter.requests[3].messages
        assert all(TOOL_RESULT_CONTENT_BLOCKS_FIELD not in message for message in next_run_messages)
        assert "base64" not in json.dumps(next_run_messages)
    finally:
        runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_format", ["PNG", "TIFF"])
async def test_rereading_overwritten_image_delivers_each_calls_own_pixels(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_format: str,
) -> None:
    colors = [(255, 0, 0), (0, 0, 255), (0, 255, 0)]
    frames = []
    for color in colors:
        buffer = io.BytesIO()
        Image.new("RGB", (16, 12), color).save(buffer, format=source_format)
        frames.append(buffer.getvalue())

    class OverwritingAdapter(FakeAdapter):
        async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
            step = len(self.requests)
            results = [
                message
                for message in messages
                if message.get("role") == "tool" and TOOL_RESULT_CONTENT_BLOCKS_FIELD in message
            ]
            assert [message["tool_call_id"] for message in results] == [
                f"read-{index}" for index in range(step)
            ]
            for index, result in enumerate(results):
                media = [
                    part
                    for part in result[TOOL_RESULT_CONTENT_BLOCKS_FIELD]
                    if part.get("type") == "media"
                ]
                assert len(media) == 1
                raw = base64.b64decode(media[0]["base64"])
                if source_format == "PNG":
                    assert raw == frames[index]
                with Image.open(io.BytesIO(raw)) as delivered:
                    assert delivered.size == (16, 12)
                    assert delivered.getpixel((0, 0)) == colors[index]
            if step < len(frames):
                # Every Tool call reads the exact same path, as in a render loop.
                image_path.write_bytes(frames[step])
            else:
                image_path.unlink()
            return await super().send(messages, model_id=model_id, **kwargs)

    adapter = OverwritingAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": f"read-{index}", "name": "read", "arguments": {"path": "front.png"}}
                ],
            }
            for index in range(len(frames))
        ]
        + [{"content": "done", "tool_calls": None}]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)
    runtime.start()
    try:
        agent = runtime.agents.create("coder", "Coder", model="fake-provider/fake-model-vision")
        image_path = Path(agent.workspace) / "front.png"
        stored_before = set((tmp_path / "data" / "artifacts" / "attachments").rglob("*"))
        await runtime.chat_loop.send("coder", "Inspect each render", session_id="reread")
        assert len(adapter.requests) == len(frames) + 1
        persisted = runtime.chat_sessions.get(session_address("coder", "reread")).load()
        assert "base64" not in json.dumps([message.to_dict() for message in persisted])
        assert set((tmp_path / "data" / "artifacts" / "attachments").rglob("*")) == stored_before
    finally:
        runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "budget_kind,streaming",
    [("none", False), ("harness", False), ("provider", False), ("provider", True)],
)
async def test_long_mixed_image_run_keeps_images_and_can_reopen_originals(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_kind: str,
    streaming: bool,
) -> None:
    tight_budget = budget_kind == "harness"
    provider_pressure = budget_kind == "provider"
    # Isolate byte pressure from token Compaction with a realistic large Context.
    model_file = resources_dir / "models" / "fake-provider.json"
    catalog = json.loads(model_file.read_text(encoding="utf-8"))
    catalog["models"]["fake-model-vision"]["context_window"] = 1_000_000
    model_file.write_text(json.dumps(catalog), encoding="utf-8")
    rejected_sizes: list[int] = []
    frames = [_PNG_BYTES + bytes([index]) * 1_400_000 for index in range(14)]
    if tight_budget:
        # Exercise repeated eviction/reopening without a 150 MiB fixture per request.
        monkeypatch.setattr(
            wire_shaping, "REQUEST_IMAGE_BYTES_LIMIT", len(base64.b64encode(frames[0])) * 4
        )
    responses: list[JsonObject] = [
        {
            "content": f"inspection-{index}",
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "name": "read" if index % 2 == 0 else "mcp_capture",
                    "arguments": {"path": f"frame-{index}.png"}
                    if index % 2 == 0
                    else {"index": index},
                }
            ],
        }
        for index in range(14)
    ]
    responses.extend(
        [
            {
                "content": "compare original",
                "tool_calls": [
                    {
                        "id": "reopen",
                        "name": "read",
                        "arguments": {"path": "frame-0.png"},
                    }
                ],
            },
            {"content": "done", "tool_calls": None},
        ]
    )
    rebuilt_requests: list[list[JsonObject]] = []
    live_budgets: list[wire_shaping.RequestImageBudget] = []

    class RebuildingAdapter(FakeAdapter):
        async def send(self, messages: list[dict], *, model_id: str, **kwargs: Any) -> dict:
            if provider_pressure:
                payload = wire._build_payload(messages, model_id, **kwargs)
                if streaming:
                    wire._prepare_stream_payload(payload)
                try:
                    wire._check_payload_size(payload, model_id)
                except ProviderRequestTooLargeError as error:
                    rejected_sizes.append(error.size_bytes)
                    raise
            if len(self.requests) == 10:
                session = runtime.chat_sessions.get(session_address("coder", "session-one"))
                rebuilt_requests.append(
                    _restore_in_run_tool_result_content(
                        (
                            await loop._requests.build_request_state(
                                agent,
                                session,
                                inputs=RequestBuildInputs(
                                    input_modalities=frozenset({"text", "image"}),
                                    wire_media_types=IMAGE_WIRE_MEDIA_TYPES,
                                    image_budget=live_budgets[0],
                                ),
                            )
                        ).messages,
                        messages,
                        image_budget=live_budgets[0],
                    )
                )
            return await super().send(messages, model_id=model_id, **kwargs)

        async def stream(
            self, messages: list[dict], *, model_id: str, **kwargs: Any
        ) -> AsyncIterator[dict]:
            response = await self.send(messages, model_id=model_id, **kwargs)
            if response.get("content"):
                yield {"type": "content_delta", "text": response["content"]}
            for call in response.get("tool_calls") or []:
                yield {
                    "type": "tool_call_delta",
                    "id": call["id"],
                    "name_delta": call["name"],
                    "arguments_delta": json.dumps(call["arguments"]),
                }
            yield {
                "type": "finish",
                "reason": "tool_calls" if response.get("tool_calls") else "stop",
            }

    adapter = RebuildingAdapter(responses)
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)
    runtime.start()
    loop = runtime.streaming_chat_loop if streaming else runtime.chat_loop
    wire = OpenAICompatibleAdapter(
        runtime.providers.get("fake-provider"),
        "test-key",
        model_lookup=lambda model_id: runtime.models.get("fake-provider", model_id),
    )
    monkeypatch.setattr(wire, "request_body_limit", lambda model_id: 10 * 1024 * 1024)
    monkeypatch.setattr(runtime.storage, "load_reflection_settings", lambda: {"enabled": False})
    try:
        # Exercise the exact shared artifact contract used by MCP binary results,
        # alternating with the real read Tool; no external Blender process needed.
        original_build = loop._requests.build_request_state

        async def capture_budget(agent: Any, session: Any, *, inputs: RequestBuildInputs) -> Any:
            if inputs.image_budget is not None and not live_budgets:
                live_budgets.append(inputs.image_budget)
            return await original_build(agent, session, inputs=inputs)

        monkeypatch.setattr(loop._requests, "build_request_state", capture_budget)

        def capture(_context: Any, arguments: JsonObject) -> JsonObject:
            index = arguments["index"]
            record = runtime.attachment_store.store(f"frame-{index}.png", frames[index])
            return tool_success(
                {"frame": index},
                artifacts=[
                    read_media_artifact(
                        attachment_id=record.id,
                        filename=record.filename,
                        media_type=record.media_type,
                    )
                ],
            )

        runtime.tools.register(
            "mcp_capture",
            "Capture a test frame.",
            {
                "type": "object",
                "properties": {"index": {"type": "integer"}},
                "required": ["index"],
                "additionalProperties": False,
            },
            capture,
        )
        agent = runtime.agents.create("coder", "Coder", model="fake-provider/fake-model-vision")
        for index, frame in enumerate(frames):
            Path(agent.workspace).joinpath(f"frame-{index}.png").write_bytes(frame)
        assistant = await loop.send("coder", "Inspect successive frames", session_id="session-one")
        assert assistant.content == "done"
        assert len(adapter.requests) == 16
        for iteration, request in enumerate(adapter.requests):
            images = [
                part
                for part in _tool_result_content_parts(request.messages)
                if part["type"] == "media"
            ]
            expected_indices = list(range(iteration)) if iteration <= 14 else [*range(14), 0]
            if tight_budget or provider_pressure:
                starts = [0, 0, 0, 0, 0, 3, 3, 3, 6, 6, 6, 9, 9, 9, 12, 12]
                if provider_pressure:
                    starts = [0, 0, 0, 0, 0, 0, 4, 4, 4, 4, 8, 8, 8, 8, 12, 12]
                expected_indices = expected_indices[starts[iteration] :]
            assert [base64.b64decode(part["base64"]) for part in images] == [
                frames[index] for index in expected_indices
            ]
            assert (
                sum(len(part["base64"]) for part in images)
                <= wire_shaping.REQUEST_IMAGE_BYTES_LIMIT
            )
            pressure_steps = {6, 10, 14} if provider_pressure else {5, 8, 11, 14}
            if iteration and (budget_kind == "none" or iteration not in pressure_steps):
                previous_images = [
                    part
                    for part in _tool_result_content_parts(adapter.requests[iteration - 1].messages)
                    if part["type"] == "media"
                ]
                assert images[: len(previous_images)] == previous_images
            for index in range(iteration):
                if index < 14:
                    assert any(
                        message.get("content") == f"inspection-{index}"
                        for message in request.messages
                    )
        rebuilt_images = [
            part
            for part in _tool_result_content_parts(rebuilt_requests[0])
            if part["type"] == "media"
        ]
        assert [base64.b64decode(part["base64"]) for part in rebuilt_images] == (
            frames[8:10] if provider_pressure else frames[6:10] if tight_budget else frames[:10]
        )
        session = runtime.chat_sessions.get(session_address("coder", "session-one"))
        persisted = session.load()
        assert persisted[-1].iteration_count == 16
        assert not any(message.role == "error" for message in persisted)
        assert len(rejected_sizes) == (3 if provider_pressure else 0)
        assert runtime.chat_runs is not None
        assert "base64" not in json.dumps([message.to_dict() for message in persisted])
        assert len([message for message in persisted if message.role == "tool"]) == 15
        for message in persisted:
            if message.role == "tool":
                assert isinstance(message.content, str)
                artifacts = json.loads(message.content)["artifacts"]
                if message.name == "read":
                    assert artifacts == []
                    assert message.tool_display is not None
                    assert message.tool_display["image_files"]
                else:
                    record = runtime.attachment_store.get(artifacts[0]["attachment_id"])
                    assert Path(record.file_path).read_bytes() in frames
    finally:
        await wire.aclose()
        runtime.stop()


@pytest.mark.asyncio
async def test_read_image_degrades_to_note_for_non_vision_model(
    tmp_path: Path,
    resources_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(
        [
            {
                "content": None,
                "tool_calls": [
                    {"id": "call_read", "name": "read", "arguments": {"path": "diagram.png"}}
                ],
            },
            {"content": "I cannot view the image directly.", "tool_calls": None},
        ]
    )
    config = Config(data_dir=tmp_path / "data")
    config._data["RESOURCES_PATH"] = str(resources_dir)
    config._data["VBOT_VERSION"] = "test-version"
    runtime = Runtime(config)
    monkeypatch.setenv("FAKE_API_KEY", "test-key")
    monkeypatch.setattr(runtime, "get_adapter", lambda connection: adapter)

    runtime.start()
    try:
        agent = runtime.agents.create(
            "coder",
            "Coder Agent",
            model="fake-provider/fake-model-v1",
            tool_access={"mode": "selected", "allowed": ["read"]},
        )
        Path(agent.workspace).joinpath("diagram.png").write_bytes(_PNG_BYTES)

        # The run must complete without raising even though the model lacks vision.
        assistant = await runtime.chat_loop.send(
            "coder", "Look at diagram.png", session_id="session-one"
        )

        assert assistant.content == "I cannot view the image directly."

        # No base64 image part reaches the non-vision provider; the correlated
        # Tool Result receives a path-bearing capability note instead.
        tool_result_parts = _tool_result_content_parts(adapter.requests[1].messages)
        assert all(part.get("type") != "media" for part in tool_result_parts)
        note = next(
            part
            for part in tool_result_parts
            if part.get("type") == "text" and "no vision capability" in str(part.get("text"))
        )
        assert "diagram.png" in note["text"]

        # No synthetic user message is persisted for the fallback either.
        messages = runtime.chat_sessions.get(session_address("coder", "session-one")).load()
        assert [message.role for message in messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "run_summary",
        ]
        assert all(
            not isinstance(message.content, list) for message in messages if message.role == "user"
        )
    finally:
        runtime.stop()
