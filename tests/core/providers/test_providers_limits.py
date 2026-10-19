"""Providers: context-window resolution and the per-request output limit."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest

from core.providers.providers import (
    GLOBAL_CONTEXT_WINDOW_FLOOR,
    LOCAL_CONTEXT_DEFAULT_CAP,
    ProviderConfig,
    RequestInput,
    model_is_local,
    resolve_context_window,
    resolve_effective_context_window,
    resolve_request_output_limit,
)
from core.utils.errors import ProviderError


def _provider(context_window: int | None) -> ProviderConfig:
    return ProviderConfig(
        id="p",
        name="P",
        adapter="openai_compatible",
        base_url="https://example.test/v1",
        context_window=context_window,
    )


@pytest.mark.parametrize(
    ("model_window", "provider", "expected"),
    [
        pytest.param(262_144, _provider(50_000), 262_144, id="model-window-wins"),
        pytest.param(None, _provider(50_000), 50_000, id="provider-default"),
        pytest.param(None, _provider(None), GLOBAL_CONTEXT_WINDOW_FLOOR, id="floor"),
        pytest.param(None, None, GLOBAL_CONTEXT_WINDOW_FLOOR, id="floor-without-provider"),
        # A stray 0 from an old catalog must never reach a caller as a budget.
        pytest.param(0, _provider(50_000), 50_000, id="zero-model-window-is-unknown"),
    ],
)
def test_resolve_context_window_chain(
    model_window: int | None, provider: ProviderConfig | None, expected: int
) -> None:
    resolved = resolve_context_window(model_window, provider)

    assert resolved == expected
    assert resolved > 0


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        pytest.param({"ollama": {"local": True}}, True, id="local-flag-in-any-provider-blob"),
        pytest.param({"ollama": {"remote": True}}, False, id="remote-flag"),
        pytest.param(None, False, id="no-metadata"),
        pytest.param({"ollama": "local"}, False, id="non-mapping-blob"),
    ],
)
def test_model_is_local(metadata: dict[str, Any] | None, expected: bool) -> None:
    assert model_is_local(metadata) is expected


_LOCAL = {"ollama": {"local": True}}


@pytest.mark.parametrize(
    ("model_window", "metadata", "local_context_windows", "expected"),
    [
        # Remote Models trust the reported window: no cap, no setting.
        pytest.param(262_144, {"ollama": {"remote": True}}, None, 262_144, id="remote"),
        pytest.param(262_144, _LOCAL, None, LOCAL_CONTEXT_DEFAULT_CAP, id="local-capped"),
        pytest.param(8192, _LOCAL, None, 8192, id="local-below-cap"),
        pytest.param(
            None,
            _LOCAL,
            None,
            min(LOCAL_CONTEXT_DEFAULT_CAP, GLOBAL_CONTEXT_WINDOW_FLOOR),
            id="local-unknown-window-caps-the-chain",
        ),
        pytest.param(262_144, _LOCAL, {"ollama/m": 16_384}, 16_384, id="setting-wins"),
        # The setting is a user decision; it is not clamped to the default cap.
        pytest.param(262_144, _LOCAL, {"ollama/m": 65_536}, 65_536, id="setting-exceeds-cap"),
        pytest.param(
            262_144,
            _LOCAL,
            {"ollama/other": 65_536},
            LOCAL_CONTEXT_DEFAULT_CAP,
            id="setting-for-other-model",
        ),
        pytest.param(
            262_144, _LOCAL, {"ollama/m": 0}, LOCAL_CONTEXT_DEFAULT_CAP, id="non-positive-setting"
        ),
        pytest.param(
            262_144, _LOCAL, {"ollama/m": "16384"}, LOCAL_CONTEXT_DEFAULT_CAP, id="non-int-setting"
        ),
        pytest.param(
            262_144, _LOCAL, {"ollama/m": True}, LOCAL_CONTEXT_DEFAULT_CAP, id="boolean-setting"
        ),
    ],
)
def test_resolve_effective_context_window(
    model_window: int | None,
    metadata: dict[str, Any],
    local_context_windows: dict[str, Any] | None,
    expected: int,
) -> None:
    resolved = resolve_effective_context_window(
        model_window,
        None,
        model_metadata=metadata,
        model_key="ollama/m",
        local_context_windows=local_context_windows,
    )

    assert resolved == expected


@pytest.mark.parametrize(
    ("limits", "window", "request_input", "expected"),
    [
        pytest.param(
            (500, 8_000, 4_096), 10_000, RequestInput.estimate(1_000), 500, id="explicit-fits"
        ),
        pytest.param(
            (None, 10_000, 4_096),
            10_000,
            RequestInput.estimate(1_000),
            10_000 - 1_000 - 256,
            id="model-limit-clamped-to-remaining-context-minus-margin",
        ),
        # No ceiling means no wire max_tokens, but the capacity checks still run.
        pytest.param(
            (None, None, None), 10_000, RequestInput.estimate(1_000), None, id="no-ceiling-fits"
        ),
        # A measured value is used as reported: no estimation margin.
        pytest.param(
            (None, 64_000, None),
            200_000,
            RequestInput.measured(150_000),
            50_000,
            id="measured-input-carries-no-margin",
        ),
        pytest.param(
            (None, 64_000, None),
            200_000,
            RequestInput.measured(150_000, estimated_change=2_000),
            50_000 - 500,
            id="only-the-estimated-change-carries-the-margin",
        ),
        pytest.param(
            (None, 64_000, None),
            200_000,
            RequestInput.measured(150_000, estimated_change=-2_000),
            50_000 - 500,
            id="a-shrinking-change-carries-the-margin-too",
        ),
        pytest.param(
            (None, 128_000, None),
            200_000,
            RequestInput.estimate(100_000),
            100_000 - 25_000,
            id="an-estimated-input-carries-the-margin",
        ),
        # Live Luna hit estimated 218470 / 272000: the 25% margin (54618) must
        # not abort a request with 53530 tokens of room; it takes half of it.
        pytest.param(
            (None, 272_000, 8_192),
            272_000,
            RequestInput.estimate(218_470),
            (272_000 - 218_470) // 2,
            id="margin-takes-at-most-half-the-room",
        ),
    ],
)
def test_resolve_request_output_limit(
    limits: tuple[int | None, int | None, int | None],
    window: int,
    request_input: RequestInput,
    expected: int | None,
) -> None:
    explicit_limit, model_output_limit, provider_default = limits

    resolved = resolve_request_output_limit(
        explicit_limit=explicit_limit,
        model_output_limit=model_output_limit,
        provider_default=provider_default,
        effective_context_window=window,
        request_input=request_input,
    )

    assert resolved == expected


@pytest.mark.parametrize(
    "request_input",
    [RequestInput.estimate, RequestInput.measured],
    ids=["estimated", "measured"],
)
def test_output_allowance_shrinks_steadily_until_the_window_is_full(
    request_input: Callable[[int], RequestInput],
) -> None:
    """A growing input never earns a larger allowance, up to the last free token."""

    allowances = [
        resolve_request_output_limit(
            explicit_limit=None,
            model_output_limit=64_000,
            provider_default=None,
            effective_context_window=200_000,
            request_input=request_input(tokens),
        )
        for tokens in range(120_000, 200_000)
    ]

    assert all(
        later is not None and earlier is not None and later <= earlier
        for earlier, later in itertools.pairwise(allowances)
    )
    assert allowances[-1] == 1


@pytest.mark.parametrize(
    ("limits", "request_input"),
    [
        # An estimate that fills the window is no longer sent unclamped.
        pytest.param((None, 8_192, None), RequestInput.estimate(10_000), id="window-full"),
        pytest.param((None, None, None), RequestInput.measured(20_000), id="no-ceiling"),
    ],
)
def test_request_input_that_does_not_fit_fails_locally(
    limits: tuple[int | None, int | None, int | None],
    request_input: RequestInput,
) -> None:
    """Input that fills the window fails before I/O, asking for Compaction."""

    explicit_limit, model_output_limit, provider_default = limits
    with pytest.raises(ProviderError, match="compact the Context") as exc_info:
        resolve_request_output_limit(
            explicit_limit=explicit_limit,
            model_output_limit=model_output_limit,
            provider_default=provider_default,
            effective_context_window=10_000,
            request_input=request_input,
        )

    assert exc_info.value.retryable is False
