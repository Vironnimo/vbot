"""Providers: limits behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.providers.providers import (
    GLOBAL_CONTEXT_WINDOW_FLOOR,
    LOCAL_CONTEXT_DEFAULT_CAP,
    REQUEST_MIN_RESERVE_TOKENS,
    ProviderConfig,
    ProviderRegistry,
    model_is_local,
    resolve_context_window,
    resolve_effective_context_window,
    resolve_request_output_limit,
)
from core.utils.errors import ConfigError, ProviderError
from tests.core.providers.providers_helpers import (
    OPENROUTER_DATA,
)
from tests.core.providers.providers_helpers import (
    _clear_cache as _clear_cache,
)


# context_window — per-provider read-side default + global floor (Phase 6)
class TestProviderContextWindowDefault:
    """The optional per-provider ``context_window`` read-side default field."""

    def test_defaults_to_none(self) -> None:
        config = ProviderConfig(
            id="p",
            name="P",
            adapter="openai_compatible",
            base_url="https://example.test/v1",
        )

        assert config.context_window is None

    def test_registry_parses_context_window_from_json(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["context_window"] = 128000
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        registry = ProviderRegistry.load(tmp_path)

        assert registry.get("openrouter").context_window == 128000

    def test_registry_defaults_context_window_to_none_when_absent(self, tmp_path: Path) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        (prov_dir / "openrouter.json").write_text(json.dumps(OPENROUTER_DATA), encoding="utf-8")

        assert ProviderRegistry.load(tmp_path).get("openrouter").context_window is None

    @pytest.mark.parametrize("bad_value", [0, -1, "128000", 1.5, True])
    def test_non_positive_or_non_int_context_window_raises(
        self, tmp_path: Path, bad_value: Any
    ) -> None:
        prov_dir = tmp_path / "providers"
        prov_dir.mkdir()
        data = dict(OPENROUTER_DATA)
        data["context_window"] = bad_value
        (prov_dir / "openrouter.json").write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(ConfigError):
            ProviderRegistry.load(tmp_path)


class TestResolveContextWindow:
    """The shared read-side resolution chain: model → provider default → floor."""

    def _provider(self, context_window: int | None) -> ProviderConfig:
        return ProviderConfig(
            id="p",
            name="P",
            adapter="openai_compatible",
            base_url="https://example.test/v1",
            context_window=context_window,
        )

    def test_model_window_wins(self) -> None:
        assert resolve_context_window(262144, self._provider(50000)) == 262144

    def test_provider_default_used_when_model_window_is_none(self) -> None:
        assert resolve_context_window(None, self._provider(50000)) == 50000

    def test_global_floor_when_neither_supplies_one(self) -> None:
        assert resolve_context_window(None, self._provider(None)) == GLOBAL_CONTEXT_WINDOW_FLOOR

    def test_global_floor_when_provider_config_is_none(self) -> None:
        assert resolve_context_window(None, None) == GLOBAL_CONTEXT_WINDOW_FLOOR

    def test_stray_zero_model_window_treated_as_unknown(self) -> None:
        # A fake 0 from an old catalog must never reach a caller as a budget.
        assert resolve_context_window(0, self._provider(50000)) == 50000
        assert resolve_context_window(0, None) == GLOBAL_CONTEXT_WINDOW_FLOOR

    def test_return_is_always_positive(self) -> None:
        assert resolve_context_window(None, None) > 0


# Effective context window — flagged-local models (Ollama et al.)
class TestModelIsLocal:
    def test_local_flag_in_any_provider_blob_counts(self) -> None:
        assert model_is_local({"ollama": {"local": True}}) is True

    def test_remote_flag_is_not_local(self) -> None:
        assert model_is_local({"ollama": {"remote": True}}) is False

    def test_empty_or_none_metadata_is_not_local(self) -> None:
        assert model_is_local({}) is False
        assert model_is_local(None) is False

    def test_non_mapping_blob_is_ignored(self) -> None:
        assert model_is_local({"ollama": "local"}) is False


class TestResolveEffectiveContextWindow:
    LOCAL_METADATA = {"ollama": {"local": True}}
    REMOTE_METADATA = {"ollama": {"remote": True}}

    def test_non_local_model_uses_plain_chain(self) -> None:
        """Remote models trust the reported window — no cap, no knob."""
        assert (
            resolve_effective_context_window(
                262144, None, model_metadata=self.REMOTE_METADATA, model_key="ollama/kimi:cloud"
            )
            == 262144
        )

    def test_local_model_defaults_to_capped_window(self) -> None:
        """A local model's theoretical max is capped to the default."""
        assert (
            resolve_effective_context_window(
                262144, None, model_metadata=self.LOCAL_METADATA, model_key="ollama/m"
            )
            == LOCAL_CONTEXT_DEFAULT_CAP
        )

    def test_local_model_below_cap_keeps_own_window(self) -> None:
        assert (
            resolve_effective_context_window(
                8192, None, model_metadata=self.LOCAL_METADATA, model_key="ollama/small"
            )
            == 8192
        )

    def test_user_setting_wins_over_cap(self) -> None:
        assert (
            resolve_effective_context_window(
                262144,
                None,
                model_metadata=self.LOCAL_METADATA,
                model_key="ollama/ministral-3:8b",
                local_context_windows={"ollama/ministral-3:8b": 16384},
            )
            == 16384
        )

    def test_user_setting_may_exceed_cap(self) -> None:
        """The knob is a user decision — it is not clamped to the default cap."""
        assert (
            resolve_effective_context_window(
                262144,
                None,
                model_metadata=self.LOCAL_METADATA,
                model_key="ollama/m",
                local_context_windows={"ollama/m": 65536},
            )
            == 65536
        )

    def test_setting_for_other_model_is_ignored(self) -> None:
        assert (
            resolve_effective_context_window(
                262144,
                None,
                model_metadata=self.LOCAL_METADATA,
                model_key="ollama/m",
                local_context_windows={"ollama/other": 65536},
            )
            == LOCAL_CONTEXT_DEFAULT_CAP
        )

    def test_invalid_setting_value_falls_back_to_cap(self) -> None:
        for bad_value in (0, -1, "16384", True, None):
            assert (
                resolve_effective_context_window(
                    262144,
                    None,
                    model_metadata=self.LOCAL_METADATA,
                    model_key="ollama/m",
                    local_context_windows={"ollama/m": bad_value},
                )
                == LOCAL_CONTEXT_DEFAULT_CAP
            )

    def test_local_model_with_unknown_window_caps_the_floor(self) -> None:
        """Unknown local window resolves through the chain, then the cap."""
        assert resolve_effective_context_window(
            None, None, model_metadata=self.LOCAL_METADATA, model_key="ollama/m"
        ) == min(LOCAL_CONTEXT_DEFAULT_CAP, GLOBAL_CONTEXT_WINDOW_FLOOR)

    def test_no_metadata_behaves_like_plain_chain(self) -> None:
        assert resolve_effective_context_window(None, None) == GLOBAL_CONTEXT_WINDOW_FLOOR


class TestResolveRequestOutputLimit:
    def test_explicit_limit_has_precedence_when_it_fits(self) -> None:
        assert (
            resolve_request_output_limit(
                explicit_limit=500,
                model_output_limit=8_000,
                provider_default=4_096,
                effective_context_window=10_000,
                estimated_input_tokens=1_000,
            )
            == 500
        )

    def test_model_limit_is_clamped_to_remaining_context(self) -> None:
        resolved = resolve_request_output_limit(
            explicit_limit=None,
            model_output_limit=10_000,
            provider_default=4_096,
            effective_context_window=10_000,
            estimated_input_tokens=1_000,
        )

        assert resolved == 10_000 - 1_000 - REQUEST_MIN_RESERVE_TOKENS

    def test_missing_limits_stay_unset_when_input_fits(self) -> None:
        """No ceiling means no wire max_tokens, but the window check still ran."""
        assert (
            resolve_request_output_limit(
                explicit_limit=None,
                model_output_limit=None,
                provider_default=None,
                effective_context_window=10_000,
                estimated_input_tokens=1_000,
            )
            is None
        )

    def test_missing_limits_fail_locally_beyond_reserve(self) -> None:
        """Models without a ceiling still fail locally on window overflow."""
        with pytest.raises(ProviderError) as exc_info:
            resolve_request_output_limit(
                explicit_limit=None,
                model_output_limit=None,
                provider_default=None,
                effective_context_window=10_000,
                estimated_input_tokens=20_000,
            )

        assert exc_info.value.retryable is False

    def test_missing_limits_pass_through_within_reserve(self) -> None:
        """An overflowing estimate inside its uncertainty margin stays unset."""
        assert (
            resolve_request_output_limit(
                explicit_limit=None,
                model_output_limit=None,
                provider_default=None,
                effective_context_window=10_000,
                estimated_input_tokens=10_500,
            )
            is None
        )

    def test_exhausted_context_within_reserve_sends_unclamped(self) -> None:
        """An estimate filling the window only within its uncertainty margin passes.

        Live evidence 2026-08-25: replayed reasoning blobs estimated ~1.06M
        against a 1M window while real prompt tokens were ~220k. Inside the
        reserve the request goes out unclamped instead of failing locally.
        """
        resolved = resolve_request_output_limit(
            explicit_limit=None,
            model_output_limit=8_192,
            provider_default=None,
            effective_context_window=8_192,
            estimated_input_tokens=8_192,
        )

        assert resolved == 8_192

    def test_exhausted_context_beyond_reserve_fails_locally(self) -> None:
        with pytest.raises(ProviderError) as exc_info:
            resolve_request_output_limit(
                explicit_limit=None,
                model_output_limit=8_192,
                provider_default=None,
                effective_context_window=8_192,
                estimated_input_tokens=20_000,
            )

        assert exc_info.value.retryable is False

    def test_reserve_does_not_fail_when_input_still_fits(self) -> None:
        """A 25% reserve must not abort a request that still has output room.

        Live Luna hit estimated 218470 / 272000. The reserve (54618) made
        reserved output negative while 53530 tokens of real capacity remained.
        """

        resolved = resolve_request_output_limit(
            explicit_limit=None,
            model_output_limit=272_000,
            provider_default=8_192,
            effective_context_window=272_000,
            estimated_input_tokens=218_470,
        )

        assert resolved == 272_000 - 218_470
