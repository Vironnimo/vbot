"""Video profiles and the VideoService: what a Model offers per call, and how a call's
choices reach the request."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from core.model_tasks import TASK_VIDEO_GENERATION, VideoOptionError, VideoService
from core.model_tasks.video import VideoProfile, build_video_profile
from core.model_tasks.video_types import VideoGenerationResult

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


def test_profile_offers_real_choices_in_order() -> None:
    assert build_video_profile(_model(_FACTS)) == VideoProfile(
        call_choices={"duration": ("4", "6", "10"), "aspect_ratio": ("16:9", "9:16")},
        generate_audio=True,
        frame_images=("first_frame", "last_frame"),
    )
    assert build_video_profile(None) == VideoProfile()


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
            "duration 5 is not offered by the configured video model; choose one of: 4, 6, 10.",
            id="value-not-offered",
        ),
        pytest.param(
            {"resolution": "720p"},
            "The configured video model has no resolution choice; omit resolution.",
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
    def __init__(self, options: dict[str, Any]) -> None:
        self._options = options

    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(task_type=task_type, target="openrouter/v/m::api-key", options={})

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, Any]:
        return dict(self._options)

    def model_for_target(self, _target_ref: object) -> Any:
        return _model(_FACTS)


class _Client:
    options: dict[str, Any] | None = None

    async def generate(self, _prompt: str, *, options: dict[str, Any], **_: Any) -> Any:
        self.options = options
        return VideoGenerationResult(data=b"v", media_type="video/mp4", model="m", job_id="j")


@pytest.mark.parametrize(
    ("call_options", "sent"),
    [
        # A requested shape replaces the configured size, which would override it.
        pytest.param(
            {"aspect_ratio": "9:16"},
            {"size": None, "aspect_ratio": "9:16", "duration": "4"},
            id="shape",
        ),
        pytest.param({"duration": 10}, {"size": "1280x720", "duration": 10}, id="duration"),
    ],
)
@pytest.mark.asyncio
async def test_call_choices_override_settings(
    call_options: dict[str, Any], sent: dict[str, Any]
) -> None:
    service = VideoService(_ModelTasks({"size": "1280x720", "duration": "4"}), cast(Any, None))
    client = _Client()

    with patch("core.model_tasks.video.ProviderVideoClient.from_runtime", return_value=client):
        await service.generate("A river", call_options=call_options)

    assert client.options is not None
    assert {name: client.options.get(name) for name in sent} == sent
