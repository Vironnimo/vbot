"""Tests for MusicService refusals before any Provider request."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.model_tasks import MusicOptionError, MusicService
from core.model_tasks.constants import TASK_MUSIC_GENERATION


class _ModelTasks:
    def binding_for(self, task_type: str) -> object:
        return SimpleNamespace(
            task_type=task_type, target="openrouter/m/lyria::api-key", options={}
        )

    def validate_execution_target(self, _binding: object) -> None:
        pass

    def options_with_defaults(self, _binding: object) -> dict[str, Any]:
        return {}

    def model_for_target(self, _target_ref: object) -> Any:
        return SimpleNamespace(
            capabilities=SimpleNamespace(
                task_types=(TASK_MUSIC_GENERATION,),
                input_modalities=("text",),
                output_modalities=("audio",),
                task_options={},
                supported_parameters=(),
            )
        )


@pytest.mark.asyncio
async def test_text_only_model_refuses_source_images_with_the_fix(tmp_path: Path) -> None:
    service = MusicService(_ModelTasks(), cast(Any, None))

    with pytest.raises(MusicOptionError) as caught:
        await service.generate("Calm piano", source_paths=[tmp_path / "cover.png"])

    assert caught.value.code == "invalid_arguments"
    assert str(caught.value) == (
        "Nothing was generated. The configured music model does not accept source_images. "
        "Repeat the call without source_images."
    )
