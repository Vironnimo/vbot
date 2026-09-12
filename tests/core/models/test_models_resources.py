"""Tests for models resources."""

import json
from pathlib import Path

import pytest

from core.models.models import (
    ModelRegistry,
)
from core.providers.reasoning import resolve_reasoning_intent
from tests.core.models.models_test_support import (
    _clear_registry_cache as _clear_registry_cache,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


RESOURCES_DIR = PROJECT_ROOT / "resources"


# ---------------------------------------------------------------------------
# ModelRegistry — real resource files
# ---------------------------------------------------------------------------
class TestModelRegistryRealResources:
    """Smoke-check: shipped sanitized JSON files load without error."""

    @pytest.fixture(autouse=True)
    def _reset_cache_for_real_resources(self):
        ModelRegistry._cache.clear()
        yield
        ModelRegistry._cache.clear()

    def test_every_committed_catalog_file_loads(self):
        """Every ``resources/models/<provider>.json`` loads under the new typed
        shape without error — the binding requirement for Phase 1's all-seeds
        conversion. ``*.raw.json`` and ``*.overrides.json`` are skipped by the
        loader, so the registry must end up non-empty across all real catalogs.
        """

        registry = ModelRegistry.load(RESOURCES_DIR)

        # Sanity: at least the hand-maintained anthropic seed and the
        # refresh-backed openrouter catalog are present, and every loaded
        # model's reasoning carries a boolean ``supported`` flag.
        assert registry.list_for_provider("anthropic")
        assert registry.list_for_provider("opencode-go")
        for _, model in registry._models.items():
            assert isinstance(model.capabilities.reasoning.supported, bool)
            assert isinstance(model.capabilities.reasoning.levels, tuple)
            assert model.capabilities.reasoning.control in (None, "levels", "on_off", "budget")
            assert isinstance(model.family, str)

    def test_real_resources_load_with_generated_canonical_layer(self):
        """Phase 3 generated the canonical ``models.json``; the assembly load
        path must still load a provider model whose wire-id does NOT join the
        canonical layer (opencode-go keys ``deepseek-v4-pro`` bare, while the
        canonical id is ``deepseek/deepseek-v4-pro`` — no auto join), on
        provider + override data alone."""

        assert (RESOURCES_DIR / "models" / "models.json").exists()

        registry = ModelRegistry.load(RESOURCES_DIR)

        # A provider-only model with no canonical join still loads fine.
        deepseek = registry.get("opencode-go", "deepseek-v4-pro")
        assert deepseek.capabilities.reasoning.supported is True

    def test_every_bundled_provider_override_loads(self):
        """A refreshed catalog must not leave silently omitted partial overrides."""
        registry = ModelRegistry.load(RESOURCES_DIR)

        for path in sorted((RESOURCES_DIR / "models").glob("*.overrides.json")):
            provider_id = path.name.removesuffix(".overrides.json")
            if provider_id == "models":
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for model_id in data.get("models", {}):
                assert registry.get(provider_id, model_id).model_id == model_id

    def test_ollama_cloud_catalog_is_separate_and_entirely_remote(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        models = registry.list_for_provider("ollama-cloud")

        assert models
        assert registry.get("ollama-cloud", "gpt-oss:120b").model_id == "gpt-oss:120b"
        assert all(model.connections == ("api-key",) for model in models)
        assert all(model.metadata.get("ollama", {}).get("remote") is True for model in models)
        assert all(model.metadata.get("ollama", {}).get("local") is not True for model in models)

    def test_ollama_cloud_deepseek_profiles_are_effective(self):
        registry = ModelRegistry.load(RESOURCES_DIR)

        for model_id in ("deepseek-v4-flash:0731", "deepseek-v4-pro:0813"):
            deepseek = registry.get("ollama-cloud", model_id)

            assert deepseek.max_output_tokens == 65_536
            assert deepseek.capabilities.reasoning.supported is True
            assert deepseek.capabilities.reasoning.control == "levels"
            assert deepseek.capabilities.reasoning.levels == ("low", "high", "max")

    def test_ollama_cloud_deepseek_v41_verified_profile(self):
        """Pin the Cloud profile verified on 2026-09-11, including its larger output cap."""
        registry = ModelRegistry.load(RESOURCES_DIR)
        model = registry.get("ollama-cloud", "deepseek-v4.1-flash")
        previous = registry.get("ollama-cloud", "deepseek-v4-flash:0731")

        assert model.model_id == "deepseek-v4.1-flash"
        assert model.connections == ("api-key",)
        assert model.context_window == 1_048_576
        assert model.capabilities.vision is True
        assert model.capabilities.tools is True
        assert model.capabilities.reasoning == previous.capabilities.reasoning
        assert model.max_output_tokens == 393_216
        assert model.recommended_temperature == previous.recommended_temperature == 1.0
        assert model.recommended_top_p == previous.recommended_top_p == 0.95
        assert model.metadata["ollama"]["remote"] is True
        assert model.metadata["ollama_cloud"]["reasoning_response_field"] == "reasoning"
        assert model.reasoning_replay is None
        assert registry.provider_reasoning_replay("ollama-cloud") == "full_history"

    @pytest.mark.parametrize(
        ("model_id", "max_output_tokens"),
        [
            ("deepseek-v4-flash:0731", 65_536),
            ("deepseek-v4-pro:0813", 65_536),
            ("glm-5.2", 131_072),
            ("kimi-k2.7-code", 262_144),
            ("minimax-m2.7", 131_072),
            ("minimax-m3", 131_072),
            ("nemotron-3-nano:30b", 131_072),
            ("nemotron-3-super", 65_536),
            ("nemotron-3-ultra", 65_536),
            ("qwen3.5:397b", 65_536),
        ],
    )
    def test_ollama_cloud_gateway_output_limits(
        self, model_id: str, max_output_tokens: int
    ) -> None:
        registry = ModelRegistry.load(RESOURCES_DIR)

        assert registry.get("ollama-cloud", model_id).max_output_tokens == max_output_tokens

    @pytest.mark.parametrize(
        ("provider_id", "model_id", "reasoning_field", "provider_replay", "model_replay"),
        [
            ("ollama-cloud", "glm-5.2", "reasoning", "full_history", None),
            ("ollama-cloud", "glm-5.3", "reasoning", "full_history", None),
            ("opencode-go", "glm-5.2", "reasoning_content", "full_history", "full_history"),
            ("opencode-go", "glm-5.3", "reasoning_content", "full_history", "full_history"),
        ],
    )
    def test_glm_targets_load_reasoning_profiles(
        self,
        provider_id: str,
        model_id: str,
        reasoning_field: str,
        provider_replay: str,
        model_replay: str | None,
    ) -> None:
        registry = ModelRegistry.load(RESOURCES_DIR)

        model = registry.get(provider_id, model_id)
        metadata = model.metadata[provider_id.replace("-", "_")]

        assert registry.provider_reasoning_replay(provider_id) == provider_replay
        assert model.reasoning_replay == model_replay
        assert metadata["reasoning_response_field"] == reasoning_field
        if provider_id == "opencode-go":
            assert metadata["protocol"] == "openai"
        if (provider_id, model_id) == ("opencode-go", "glm-5.3"):
            assert "reasoning_request_format" not in metadata

    def test_ollama_cloud_glm_5_3_override_profiles_reasoning_levels(self):
        """GLM-5.3 exposes Cloud's documented reasoning ladder plus explicit off.

        Ollama's model page documents ``low``, ``high``, and ``max``; ``none``
        remains the wire's explicit off switch rather than an active level.
        """

        registry = ModelRegistry.load(RESOURCES_DIR)

        reasoning = registry.get("ollama-cloud", "glm-5.3").capabilities.reasoning
        assert reasoning.supported is True
        assert reasoning.control == "levels"
        assert reasoning.levels == ("low", "high", "max")

    def test_overrides_are_applied_at_load(self):
        """``<provider>.overrides.json`` is now merged at LOAD (it used to only
        apply at refresh). The openai overrides add override-only task models —
        they must be present in the loaded registry."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        # ``tts-1`` lives only in ``openai.overrides.json`` (the provider file
        # carries the chat models). It loads because overrides apply at load.
        tts = registry.get("openai", "tts-1")
        assert tts.capabilities.task_types == ("text_to_speech", "audio_generation")

    def test_openai_reasoning_models_load_connection_specific_wire_policies(self):
        """Current OpenAI reasoning Models use Responses on every allowed wire.

        The unsuffixed GPT-5.6 alias is a Platform alias only: the live ChatGPT
        Codex endpoint rejects it, while the named 5.6 variants are available on
        both connections. Earlier Platform models use Responses without the
        GPT-5.6-only ``all_turns`` request field.
        """

        registry = ModelRegistry.load(RESOURCES_DIR)

        gpt_52 = registry.get("openai", "gpt-5.2")
        assert gpt_52.connections == ("api-key",)
        assert gpt_52.context_window == 400_000
        assert gpt_52.max_output_tokens == 128_000
        assert gpt_52.metadata["openai"]["wire_policies"] == {"api-key": {"protocol": "responses"}}
        assert set(gpt_52.capabilities.supported_parameters) == {
            "max_output_tokens",
            "parallel_tool_calls",
            "reasoning",
            "response_format",
            "tools",
        }

        gpt_55 = registry.get("openai", "gpt-5.5")
        assert gpt_55.connections == ("api-key", "subscription")
        assert gpt_55.metadata["openai"]["wire_policies"] == {
            "api-key": {"protocol": "responses"},
            "subscription": {"protocol": "responses"},
        }

        for model_id in ("gpt-5.5", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"):
            model = registry.get("openai", model_id)
            assert model.context_window_for("api-key") == 1_050_000
            assert model.context_window_for("subscription") == 272_000

        alias = registry.get("openai", "gpt-5.6")
        assert alias.connections == ("api-key",)
        assert alias.metadata["openai"]["wire_policies"] == {
            "api-key": {
                "protocol": "responses",
                "reasoning_context": "all_turns",
            }
        }

    def test_deepseek_flash_top_p_override_applies_at_load(self):
        """``ollama-cloud.overrides.json`` pins recommended_top_p 0.95 for the
        DeepSeek V4 Flash id."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        pinned = registry.get("ollama-cloud", "deepseek-v4-flash:0731")
        assert pinned.recommended_top_p == 0.95
        assert pinned.recommended_temperature == 1.0

    def test_opencode_go_glm_5_3_flash_override_loads(self):
        """GLM-5.3-Flash inherits full-history replay on OpenCode Go.

        Required record fields come from the generated provider catalog, while
        the override owns its exact protocol and response carrier. Exact vBot
        probes corrected the old raw-probe false negative on 2026-09-02:
        persisted ``reasoning_content`` is billed in a Tool continuation, so
        the Model must inherit Provider full_history.
        """

        registry = ModelRegistry.load(RESOURCES_DIR)

        model = registry.get("opencode-go", "glm-5.3-flash")
        assert registry.provider_reasoning_replay("opencode-go") == "full_history"
        assert model.reasoning_replay is None
        assert model.context_window == 1_000_000
        assert model.metadata["opencode_go"]["reasoning_response_field"] == "reasoning_content"

    def test_opencode_go_current_endpoint_profiles_load(self):
        """All 28 current official Models route through their documented wire."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        expected_by_protocol = {
            "responses": (
                "grok-4.6",
                "gpt-5.6-luna",
                "muse-spark-1.2-contributor",
                "muse-spark-1.3-contributor",
            ),
            "openai": (
                "glm-5.3-flash",
                "glm-5.3",
                "glm-5.2",
                "glm-5.1",
                "kimi-k3",
                "kimi-k2.7-code",
                "kimi-k2.6",
                "longcat-2.0",
                "deepseek-flash",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
                "deepseek-v4-flash-vision-exp",
                "mimo-v2.5",
                "mimo-v2.5-pro",
                "hy4-preview",
                "hy3",
            ),
            "anthropic": (
                "minimax-m3",
                "minimax-m2.7",
                "minimax-m2.5",
                "qwen3.8-max",
                "qwen3.8-flash",
                "qwen3.7-max",
                "qwen3.7-plus",
                "qwen3.6-plus",
            ),
        }
        assert sum(len(model_ids) for model_ids in expected_by_protocol.values()) == 28
        for protocol, model_ids in expected_by_protocol.items():
            for model_id in model_ids:
                model = registry.get("opencode-go", model_id)
                assert model.metadata["opencode_go"]["protocol"] == protocol

    def test_gpt6_and_deepseek41_profiles_load(self):
        registry = ModelRegistry.load(RESOURCES_DIR)
        gpt = registry.get("openai", "gpt-6-astra")
        assert gpt.connections == ("subscription",)
        assert gpt.context_window_for("subscription") == 272_000
        assert gpt.capabilities.reasoning.levels == ("low", "medium", "high", "xhigh", "max")
        assert gpt.capabilities.tools is True
        assert gpt.metadata["openai"]["wire_policies"]["subscription"] == {
            "protocol": "responses",
            "minimum_reasoning_effort": "low",
        }

        deepseek = registry.get("opencode-go", "deepseek-flash")
        assert deepseek.name == "DeepSeek V4.1 Flash"
        assert deepseek.context_window == 1_000_000
        assert deepseek.max_output_tokens == 384_000
        assert deepseek.capabilities.reasoning.levels == ("low", "high", "max")
        assert deepseek.capabilities.tools is True
        assert deepseek.metadata["opencode_go"] == {
            "protocol": "openai",
            "reasoning_response_field": "reasoning_content",
            "thinking_control": "toggle_with_effort",
        }
        assert deepseek.reasoning_replay is None
        assert registry.provider_reasoning_replay("opencode-go") == "full_history"

    def test_opencode_go_response_fields_are_not_history_field_guesses(self):
        """Profiles describe inbound response carriers, not outbound replay."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        for model_id in ("kimi-k2.6", "kimi-k3", "hy3", "hy4-preview"):
            metadata = registry.get("opencode-go", model_id).metadata["opencode_go"]
            assert metadata["reasoning_response_field"] == "reasoning"
        for model_id in ("mimo-v2.5", "mimo-v2.5-pro"):
            metadata = registry.get("opencode-go", model_id).metadata["opencode_go"]
            assert metadata["reasoning_response_field"] == "reasoning_content"

    def test_ollama_cloud_reasoning_replay_policies(self):
        """Ollama Cloud replay pins, live-verified through 2026-09-02.

        Streaming token accounting (billed-input deltas in vBot's real request
        shapes, visible-content control per variant) on /v1/chat/completions:

        - full_history (inherited default): DeepSeek V4 Flash/Pro, GLM-5.1,
          GLM-5.2, GLM-5.3, GLM-5.3-flash, and Kimi K3. The ``reasoning``
          carrier is accepted and billed in both scopes
          (glm-5.3-flash re-measured 2026-09-02: ``reasoning`` is billed in-run
          +53 and cross-run +254 with matching controls; ``reasoning_content``
          is stripped in both scopes with zero deltas and positive controls).
          DeepSeek cross-Run replay is conditional: a Tool-free follow-up
          ignores history, while a follow-up carrying Tools bills the complete
          history (+110 Flash, +102 Pro), matching DeepSeek's contract.
        - current_run: Kimi K2.6/K2.7-code - in-run replayed reasoning is
          billed, cross-run replay is stripped.
        - none: Gemma 4, GPT-OSS, MiniMax M2.7/M3, Nemotron 3, Qwen 3.5 - the
          carrier is stripped in both scopes (zero delta, positive control).

        All measured models emit ``reasoning`` as their response carrier; the
        ``reasoning_content`` profiles were wrong and are corrected (that
        field is stripped even in-run on this wire).
        """

        registry = ModelRegistry.load(RESOURCES_DIR)

        assert registry.provider_reasoning_replay("ollama-cloud") == "full_history"
        assert registry.get("ollama-cloud", "glm-5.1").reasoning_replay is None
        assert registry.get("ollama-cloud", "glm-5.2").reasoning_replay is None
        assert registry.get("ollama-cloud", "glm-5.3").reasoning_replay is None
        assert registry.get("ollama-cloud", "glm-5.3-flash").reasoning_replay is None
        assert registry.get("ollama-cloud", "kimi-k3").reasoning_replay is None
        assert registry.get("ollama-cloud", "deepseek-v4-flash:0731").reasoning_replay is None
        assert registry.get("ollama-cloud", "deepseek-v4-pro:0813").reasoning_replay is None
        assert registry.get("ollama-cloud", "kimi-k2.6").reasoning_replay == "current_run"
        assert registry.get("ollama-cloud", "kimi-k2.7-code").reasoning_replay == "current_run"
        assert registry.get("ollama-cloud", "gemma4:31b").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "gpt-oss:120b").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "gpt-oss:20b").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "minimax-m2.7").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "minimax-m3").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "nemotron-3-nano:30b").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "nemotron-3-super").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "nemotron-3-ultra").reasoning_replay == "none"
        assert registry.get("ollama-cloud", "qwen3.5:397b").reasoning_replay == "none"

    def test_ollama_cloud_minimax_reasoning_has_no_false_control(self):
        """Ollama Cloud ignores both documented off-control wire shapes."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        for model_id in ("minimax-m2.7", "minimax-m3"):
            reasoning = registry.get("ollama-cloud", model_id).capabilities.reasoning
            assert reasoning.supported is True
            assert reasoning.control is None
            assert reasoning.levels == ()

    def test_ollama_cloud_reasoning_response_fields(self):
        """Every measured Ollama Cloud model emits ``reasoning`` as carrier.

        Live-verified through 2026-09-02: all probed models stream their
        reasoning under ``message.reasoning``. The previous
        ``reasoning_content`` profiles (GLM-5.1, GLM-5.3-Flash, Kimi) and the
        ``reasoning_details`` profile (Qwen 3.5) were wrong - those fields are
        stripped even in-run on this wire, so the corrected profile is what
        makes replay work at all.
        """

        registry = ModelRegistry.load(RESOURCES_DIR)

        for model_id in (
            "deepseek-v4-flash:0731",
            "deepseek-v4-pro:0813",
            "gemma4:31b",
            "glm-5.1",
            "glm-5.2",
            "glm-5.3",
            "glm-5.3-flash",
            "gpt-oss:120b",
            "gpt-oss:20b",
            "kimi-k2.6",
            "kimi-k2.7-code",
            "kimi-k3",
            "minimax-m2.7",
            "minimax-m3",
            "nemotron-3-nano:30b",
            "nemotron-3-super",
            "nemotron-3-ultra",
            "qwen3.5:397b",
        ):
            field = registry.get("ollama-cloud", model_id).metadata["ollama_cloud"][
                "reasoning_response_field"
            ]
            assert field == "reasoning", f"{model_id}: {field}"

    def test_openai_task_model_overrides_are_limited_to_working_connections(self):
        """OpenAI task models without a subscription wire are api-key only, while
        ``gpt-image-2`` stays valid for both connections."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        api_key_only_models = (
            "tts-1",
            "tts-1-hd",
            "gpt-4o-mini-tts",
            "whisper-1",
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
            "dall-e-2",
            "dall-e-3",
            "gpt-image-1",
            "gpt-image-1-mini",
            "gpt-image-1.5",
        )
        for model_id in api_key_only_models:
            model = registry.get("openai", model_id)
            assert model.connections == ("api-key",)
            assert model.allows_connection("api-key") is True
            assert model.allows_connection("subscription") is False

        gpt_image_2 = registry.get("openai", "gpt-image-2")
        assert gpt_image_2.connections == ()
        assert gpt_image_2.allows_connection("api-key") is True
        assert gpt_image_2.allows_connection("subscription") is True

        for model_id in ("gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2"):
            model = registry.get("openai", model_id)
            assert model.capabilities.input_modalities == ("image", "text")

    def test_anthropic_opus_4_5_override_pins_budget_control(self):
        """``anthropic.overrides.json`` pins Opus 4.5 to ``budget`` control.

        Opus 4.5 exposes an effort ladder (so the canonical layer labels it
        ``levels``) but does not support adaptive thinking — the ``levels`` render
        (``thinking: {type: adaptive}``) 400s there. The override forces native
        ``budget`` rendering, which the model accepts. With no ``budget_max`` to
        seed, a ``high`` effort derives the absolute fallback budget (16384)."""

        registry = ModelRegistry.load(RESOURCES_DIR)

        opus45 = registry.get("anthropic", "claude-opus-4-5-20251101")
        assert opus45.capabilities.reasoning.supported is True
        assert opus45.capabilities.reasoning.control == "budget"
        assert opus45.capabilities.reasoning.budget_max is None

        intent = resolve_reasoning_intent(
            supported=opus45.capabilities.reasoning.supported,
            control=opus45.capabilities.reasoning.control,
            levels=opus45.capabilities.reasoning.levels,
            effort="high",
            budget_max=opus45.capabilities.reasoning.budget_max,
        )
        assert intent.kind == "budget"
        assert intent.budget_tokens == 16384

    @pytest.mark.parametrize(
        "provider_id",
        ["openai", "openrouter", "anthropic", "github-copilot", "mistral", "ollama-cloud"],
    )
    def test_provider_loads_and_has_models(self, provider_id: str):
        registry = ModelRegistry.load(RESOURCES_DIR)
        models = registry.list_for_provider(provider_id)

        assert len(models) > 0
        for model in models:
            assert model.model_id
            assert model.name
            assert isinstance(model.capabilities.vision, bool)
            assert isinstance(model.capabilities.tools, bool)
            assert isinstance(model.capabilities.json_mode, bool)
            assert isinstance(model.capabilities.reasoning.supported, bool)
            assert isinstance(model.capabilities.input_modalities, tuple)
            assert isinstance(model.capabilities.output_modalities, tuple)
            assert isinstance(model.capabilities.supported_parameters, tuple)
            assert isinstance(model.capabilities.task_types, tuple)
            if model.context_window is not None:
                assert isinstance(model.context_window, int)
                assert model.context_window >= 0
            if model.max_output_tokens is not None:
                assert isinstance(model.max_output_tokens, int)
                assert model.max_output_tokens >= 0


class TestCustomModelOverlay:
    @staticmethod
    def _custom_provider(model_name: str) -> dict[str, dict[str, object]]:
        return {
            model_name: {
                "models": {
                    "chat-model": {
                        "name": model_name,
                        "capabilities": {
                            "vision": False,
                            "tools": True,
                            "json_mode": False,
                            "reasoning": False,
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                            "supported_parameters": [],
                            "supported_voices": [],
                            "task_types": ["chat"],
                            "task_options": {},
                        },
                    }
                }
            }
        }

    def test_custom_provider_registries_do_not_share_the_path_cache(self, tmp_path: Path) -> None:
        first = ModelRegistry.load(
            tmp_path,
            custom_providers=self._custom_provider("first"),
        )
        second = ModelRegistry.load(
            tmp_path,
            custom_providers=self._custom_provider("second"),
        )
        bundled_only = ModelRegistry.load(tmp_path)

        assert first is not second
        assert first.get("first", "chat-model").name == "first"
        assert second.get("second", "chat-model").name == "second"
        assert bundled_only.list_for_provider("first") == []
        assert bundled_only.list_for_provider("second") == []

    def test_manual_custom_model_overlays_discovered_record(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models_dir.joinpath("local-ai.json").write_text(
            json.dumps(
                {
                    "provider_id": "local-ai",
                    "models": {
                        "chat-model": {
                            "name": "Discovered",
                            "capabilities": {
                                "vision": False,
                                "tools": False,
                                "json_mode": False,
                                "reasoning": {"supported": False},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        custom = {
            "local-ai": {
                "models": {
                    "chat-model": {
                        "name": "Manual",
                        "context_window": 65_536,
                        "max_output_tokens": 2_048,
                        "capabilities": {
                            "vision": True,
                            "tools": True,
                            "json_mode": True,
                            "reasoning": True,
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                            "supported_parameters": [],
                            "supported_voices": [],
                            "task_types": ["chat", "image_understanding"],
                            "task_options": {},
                        },
                    }
                }
            }
        }

        registry = ModelRegistry.load(tmp_path, custom_providers=custom)
        held_reference = registry
        model = registry.get("local-ai", "chat-model")

        assert model.name == "Manual"
        assert model.context_window == 65_536
        assert model.capabilities.reasoning.supported is True
        assert model.connections == ("default",)

        custom["local-ai"]["models"]["chat-model"]["name"] = "Updated"
        registry.reload(tmp_path, custom_providers=custom)

        assert held_reference is registry
        assert held_reference.get("local-ai", "chat-model").name == "Updated"
