"""Providers: context-window resolution and the per-request output limit."""

from __future__ import annotations

from typing import Any

import pytest

from core.providers.providers import (
    GLOBAL_CONTEXT_WINDOW_FLOOR,
    LOCAL_CONTEXT_DEFAULT_CAP,
    REQUEST_MIN_RESERVE_TOKENS,
    ProviderConfig,
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
    ("limits", "window", "input_tokens", "expected"),
    [
        pytest.param((500, 8_000, 4_096), 10_000, 1_000, 500, id="explicit-limit-fits"),
        pytest.param(
            (None, 10_000, 4_096),
            10_000,
            1_000,
            10_000 - 1_000 - REQUEST_MIN_RESERVE_TOKENS,
            id="model-limit-clamped-to-remaining-context",
        ),
        # No ceiling means no wire max_tokens, but the window check still runs.
        pytest.param((None, None, None), 10_000, 1_000, None, id="no-ceiling-fits"),
        # Live evidence 2026-08-25: replayed reasoning blobs estimated ~1.06M
        # against a 1M window while real prompt tokens were ~220k. Inside the
        # estimation reserve the request goes out unclamped.
        pytest.param(
            (None, 8_192, None), 8_192, 8_192, 8_192, id="overflow-within-reserve-unclamped"
        ),
        # Live Luna hit estimated 218470 / 272000: the 25% reserve (54618) must
        # not abort a request with 53530 tokens of real capacity left.
        pytest.param(
            (None, 272_000, 8_192),
            272_000,
            218_470,
            272_000 - 218_470,
            id="reserve-larger-than-remaining",
        ),
    ],
)
def test_resolve_request_output_limit(
    limits: tuple[int | None, int | None, int | None],
    window: int,
    input_tokens: int,
    expected: int | None,
) -> None:
    explicit_limit, model_output_limit, provider_default = limits

    resolved = resolve_request_output_limit(
        explicit_limit=explicit_limit,
        model_output_limit=model_output_limit,
        provider_default=provider_default,
        effective_context_window=window,
        estimated_input_tokens=input_tokens,
    )

    assert resolved == expected


def test_request_output_limit_fails_locally_beyond_the_reserve() -> None:
    """Models without a ceiling still fail locally instead of overflowing remotely."""

    with pytest.raises(ProviderError) as exc_info:
        resolve_request_output_limit(
            explicit_limit=None,
            model_output_limit=None,
            provider_default=None,
            effective_context_window=10_000,
            estimated_input_tokens=20_000,
        )

    assert exc_info.value.retryable is False
