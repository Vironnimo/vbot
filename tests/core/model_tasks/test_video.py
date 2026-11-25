"""Video profiles and the VideoService: what a Model offers per call, and how a call's
choices reach the request."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.chat.messages import ChatMessage, ToolCall
from core.database import DatabaseUnavailableError
from core.model_tasks import TASK_VIDEO_GENERATION, VideoOptionError, VideoService
from core.model_tasks.model_tasks import TaskModelBinding
from core.model_tasks.video import (
    VideoConfigurationError,
    VideoOutcomeUnknownError,
    VideoProfile,
    build_video_profile,
)
from core.model_tasks.video_providers import ProviderVideoClient, VideoJobUnfinishedError
from core.model_tasks.video_recovery import VideoDelivery, VideoRecovery
from core.model_tasks.video_types import VideoGenerationResult
from core.providers.errors import ProviderUsageLimitError
from core.runtime._bootstrap_services import authorize_recovered_video
from core.sessions import ChatSessionManager, ToolResultFacts
from core.tools import tool_success
from core.utils.errors import ConfigError
from tests.core.model_tasks.model_tasks_test_support import configured_task_target

_FACTS = {
    "frame_images": ["last_frame", "first_frame", "middle_frame"],
    "parameters": {
        "duration": {"type": "enum", "values": ["10", "4", "6"]},
        "aspect_ratio": {"type": "enum", "values": ["auto", "16:9", "9:16"]},
        "resolution": {"type": "enum", "values": ["1080p"]},
        "size": {"type": "enum", "values": ["1280x720", "720x1280"]},
        "generate_audio": {"type": "boolean"},
    },
}


def _model(facts: dict[str, Any]) -> Any:
    return SimpleNamespace(
        capabilities=SimpleNamespace(
            task_types=(TASK_VIDEO_GENERATION,),
            task_options={TASK_VIDEO_GENERATION: facts},
            input_modalities=("text", "image"),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("collectable", [True, False])
async def test_unwritable_video_receipt_preserves_foreground_job(
    tmp_path: Path, collectable: bool
) -> None:
    recovery = AsyncMock(spec=VideoRecovery)
    recovery.pin.side_effect = lambda target: target
    recovery.accepted.side_effect = DatabaseUnavailableError("disk full")
    client = AsyncMock()

    async def generate(_prompt: str, *, accepted: Any, **_kwargs: Any) -> VideoGenerationResult:
        await accepted("already-paid", "origin")
        if not collectable:
            raise VideoJobUnfinishedError("already-paid", "download failed")
        return VideoGenerationResult(
            data=b"video", media_type="video/mp4", model="v/m", job_id="already-paid"
        )

    client.generate.side_effect = generate
    service = VideoService(_ModelTasks({}), cast(Any, None), recovery=recovery)
    with patch.object(
        ProviderVideoClient, "from_runtime_async", new_callable=AsyncMock, return_value=client
    ):
        work = service.generate(
            "clip",
            delivery=("vid_any", VideoDelivery("generation", "run", "call", tmp_path), tmp_path),
        )
        if collectable:
            assert (await work).data == b"video"
        else:
            with pytest.raises(VideoOutcomeUnknownError) as caught:
                await work
            assert caught.value.job_id == "already-paid" and not caught.value.collection_pending
        client.generate.assert_awaited_once()


def test_profile_offers_real_choices_in_order() -> None:
    assert build_video_profile(_model(_FACTS)) == VideoProfile(
        call_choices={"duration": ("4", "6", "10"), "aspect_ratio": ("16:9", "9:16")},
        generate_audio=True,
        frame_images=("first_frame", "last_frame"),
    )
    assert build_video_profile(None) == VideoProfile()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["recover", "denied", "moved", "stop", "active", "published", "fallback"]
)
async def test_accepted_video_survives_restart_and_delivers_original_job_once(
    tmp_path: Path, outcome: str, current_session_store_template: Path
) -> None:
    data = tmp_path / "data"
    directory = tmp_path / "output"
    shutil.copytree(current_session_store_template, data)
    manager = ChatSessionManager(data)
    session = manager.create("coder").start_run("original")
    session.append(ChatMessage.user("Generate a video"))
    invocation = await session.start_tool_invocation_async(
        call_id="call", name="generate_video", started_at="2026-01-01T00:00:00Z"
    )
    address = session.address
    generation = session.generation_id
    assert generation is not None
    runtime = SimpleNamespace(
        chat_sessions=manager,
        provider_credentials=SimpleNamespace(resolve_account_id=Mock(return_value="second")),
        trigger_service=SimpleNamespace(submit_completion=AsyncMock()),
        storage=SimpleNamespace(data_dir=data),
        _scratch_areas=None,
    )
    guard = AsyncMock()
    recovery = VideoRecovery(cast(Any, runtime), guard)
    runtime.chat_run_manager = SimpleNamespace(
        get=lambda _run_id: SimpleNamespace(cancel_requested=False)
    )

    class Tasks(_ModelTasks):
        @override
        def binding_for(self, task_type: str) -> object:
            backup = TaskModelBinding(task_type, "openrouter/v/m::api-key")
            return (
                TaskModelBinding(task_type, "google/omni::api-key", fallbacks=(backup,))
                if outcome == "fallback"
                else backup
            )

    service = VideoService(Tasks({}), cast(Any, runtime), recovery=recovery)
    result = VideoGenerationResult(
        data=b"existing video", media_type="video/mp4", model="v/m", job_id="paid-job"
    )
    client = AsyncMock()

    async def accepted(_prompt: str, *, accepted: Any, **kwargs: Any) -> None:
        if outcome == "fallback" and client.generate.await_count == 1:
            await accepted("terminal-quota-job", "google-origin")
            await kwargs["ended_without_video"]()
            raise ProviderUsageLimitError("exhausted")
        await accepted("paid-job", "provider-origin")
        raise VideoJobUnfinishedError("paid-job", "download interrupted")

    client.generate.side_effect = accepted
    client.collect_existing.return_value = result
    with patch.object(
        ProviderVideoClient, "from_runtime_async", new_callable=AsyncMock, return_value=client
    ) as make_client:
        try:
            with pytest.raises(VideoOutcomeUnknownError) as problem:
                await service.generate_artifact(
                    "test",
                    output_dir=directory,
                    before_output_write=AsyncMock(),
                    delivery=VideoDelivery(generation, "original", "call", directory),
                )
            assert problem.value.collection_pending
            assert manager.video_jobs.pending()[0].target.endswith(":second")
            if outcome == "active":
                session.append(
                    ChatMessage.assistant(
                        model="v/m",
                        content=None,
                        tool_calls=[ToolCall(id="call", name="generate_video")],
                    )
                )
                session.append_many(
                    [
                        ChatMessage.tool(
                            name="generate_video",
                            tool_call_id="call",
                            content=json.dumps(
                                tool_success(
                                    {
                                        "status": "pending",
                                        "job_id": "paid-job",
                                        "message": "pending",
                                    }
                                )
                            ),
                        )
                    ],
                    tool_results={
                        "call": ToolResultFacts("completed", ok=True, invocation_key=invocation)
                    },
                )
            if outcome == "stop":
                from core.sessions._types import SessionRunCompletion

                manager._store.execution.finish_run(
                    address,
                    SessionRunCompletion(
                        run_id="original",
                        status="cancelled",
                        timing={
                            "started_at": "2026-01-01T00:00:00Z",
                            "completed_at": "2026-01-01T00:00:01Z",
                            "duration_ms": 1000,
                        },
                        iteration_count=0,
                        completion_reason="user",
                    ),
                )
        finally:
            await service.aclose()
            manager.close()
        manager = runtime.chat_sessions = ChatSessionManager(data)
        try:
            if outcome != "active":
                manager.recover_interrupted_runs()

            async def authorize(job: Any, path: Path) -> None:
                permissions = Mock(restricted=True)
                permissions.boundary.return_value.check_path.return_value = (
                    "denied" if outcome == "denied" else None
                )
                if outcome == "moved":
                    job = replace(job, original_root=str(data))
                with patch(
                    "core.runtime._bootstrap_services._session_output_permissions",
                    return_value=permissions,
                ):
                    await authorize_recovered_video(cast(Any, runtime), job, path)

            guard.side_effect = authorize
            resumed = VideoRecovery(cast(Any, runtime), guard)

            async def deliver(
                _address: Any,
                *,
                notice_id: str,
                body: str,
                expected_session_generation_id: str,
                **kwargs: Any,
            ) -> None:
                assert _address == address and expected_session_generation_id == generation
                assert await kwargs["filter_body"](_address, body) == body
                manager.get(address).append(
                    replace(ChatMessage.note(body), completion_receipts=(notice_id,))
                )

            runtime.trigger_service.submit_completion.side_effect = deliver
            (job,) = manager.video_jobs.pending()
            if outcome == "published":
                runtime.trigger_service.submit_completion.side_effect = OSError(
                    "shutdown before input"
                )
                await resumed._try_collect(job)
                manager.close()
                manager = runtime.chat_sessions = ChatSessionManager(data)
                (job,) = manager.video_jobs.pending()
                runtime.trigger_service.submit_completion.reset_mock()
                runtime.trigger_service.submit_completion.side_effect = deliver
            if outcome == "active":
                assert job.state == "pending"
            await resumed._try_collect(job)
            assert client.generate.await_count == (2 if outcome == "fallback" else 1)
            if outcome in {"recover", "active", "published", "fallback"}:
                # A second crash after publishing the notice still has its atomic receipt.
                manager.close()
                manager = runtime.chat_sessions = ChatSessionManager(data)
                (job,) = manager.video_jobs.pending()
                assert job.state == "finished"
                await resumed._try_collect(job)
                assert not manager.video_jobs.pending()
                assert [p.read_bytes() for p in directory.iterdir()] == [result.data]
                runtime.trigger_service.submit_completion.assert_awaited_once()
                client.collect_existing.assert_awaited_once_with("paid-job", "provider-origin")
                assert make_client.await_args is not None
                assert make_client.await_args.args[1].account_id == "second"
            else:
                client.collect_existing.assert_not_awaited()
                assert not list(directory.iterdir())
                if outcome in {"denied", "moved"}:
                    assert "permissions changed" in (manager.get(address).load()[-1].content or "")
                    assert manager.video_jobs.pending()[0].state == "finished"
                else:
                    runtime.trigger_service.submit_completion.assert_not_awaited()
        finally:
            manager.close()


@pytest.mark.parametrize(
    ("call_options", "expected"),
    [
        pytest.param(
            {"duration": "6s", "aspect_ratio": "9x16", "generate_audio": False},
            {"duration": 6, "aspect_ratio": "9:16", "generate_audio": False},
            id="tolerant-spelling",
        ),
        pytest.param(
            {"duration": 5},
            "Nothing was generated. The configured video model does not offer duration 5. "
            "Pass one of 4, 6, 10, or omit duration to use the default.",
            id="value-not-offered",
        ),
        pytest.param(
            {"resolution": "720p"},
            "Nothing was generated. The configured video model has no resolution choice. "
            "Repeat the call without resolution.",
            id="option-not-offered",
        ),
    ],
)
def test_call_choices_are_checked_against_the_profile(
    call_options: dict[str, Any], expected: dict[str, Any] | str
) -> None:
    profile = build_video_profile(_model(_FACTS))

    if isinstance(expected, str):
        with pytest.raises(VideoOptionError) as caught:
            profile.validated_options(call_options)
        assert str(caught.value) == expected
    else:
        assert profile.validated_options(call_options) == expected


class _ModelTasks:
    def __init__(self, options: dict[str, Any], facts: dict[str, Any] = _FACTS) -> None:
        self._options = options
        self._facts = facts

    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(task_type=task_type, target="openrouter/v/m::api-key", options={})

    validate_execution_target = staticmethod(configured_task_target)

    def options_with_defaults(self, _binding: object) -> dict[str, Any]:
        return dict(self._options)

    def model_for_target(self, _target_ref: object) -> Any:
        return _model(self._facts)


class _Client:
    options: dict[str, Any] | None = None

    async def generate(self, _prompt: str, *, options: dict[str, Any], **_: Any) -> Any:
        self.options = options
        return VideoGenerationResult(data=b"v", media_type="video/mp4", model="m", job_id="j")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ConfigError, KeyError])
async def test_client_preparation_preserves_configuration_error(
    error_type: type[Exception],
) -> None:
    service = VideoService(_ModelTasks({}), cast(Any, None))
    failure = error_type("Connection or Account removed during preparation")
    with (
        patch("core.model_tasks.video.ProviderVideoClient.from_runtime", side_effect=failure),
        pytest.raises(VideoConfigurationError) as caught,
    ):
        await service.generate("A short clip")
    assert caught.value.__cause__ is failure


_RESOLUTION_FACTS: dict[str, Any] = {
    **_FACTS,
    "parameters": {
        **cast(dict[str, Any], _FACTS["parameters"]),
        "resolution": {"type": "enum", "values": ["720p", "1080p"]},
    },
}


@pytest.mark.parametrize(
    ("facts", "call_options", "sent"),
    [
        # A requested shape replaces the configured size, which would override it.
        pytest.param(
            _FACTS,
            {"aspect_ratio": "9:16"},
            {"size": None, "aspect_ratio": "9:16", "duration": "4"},
            id="shape",
        ),
        # The half of the shape the call leaves out keeps the configured size's value.
        pytest.param(
            _RESOLUTION_FACTS,
            {"aspect_ratio": "9:16"},
            {"size": None, "aspect_ratio": "9:16", "resolution": "720p"},
            id="aspect-keeps-configured-resolution",
        ),
        pytest.param(
            _RESOLUTION_FACTS,
            {"resolution": "1080p"},
            {"size": None, "aspect_ratio": "16:9", "resolution": "1080p"},
            id="resolution-keeps-configured-aspect",
        ),
        pytest.param(_FACTS, {"duration": 10}, {"size": "1280x720", "duration": 10}, id="duration"),
    ],
)
@pytest.mark.asyncio
async def test_call_choices_override_settings(
    facts: dict[str, Any], call_options: dict[str, Any], sent: dict[str, Any]
) -> None:
    service = VideoService(
        _ModelTasks({"size": "1280x720", "duration": "4"}, facts), cast(Any, None)
    )
    client = _Client()

    with patch("core.model_tasks.video.ProviderVideoClient.from_runtime", return_value=client):
        await service.generate("A river", call_options=call_options)

    assert client.options is not None
    assert {name: client.options.get(name) for name in sent} == sent
