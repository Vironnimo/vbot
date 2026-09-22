"""The bundled Go and Zen Connections share key slots, not enablement or billing."""

from pathlib import Path

from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import ProviderRegistry


def test_shared_credentials_keep_accounts_and_enablement_independent() -> None:
    registry = ProviderRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    resolver = ProviderCredentialResolver(
        registry,
        process_env={"OPENCODE_API_KEY__WORK": "process-test"},
        fallback_credentials={
            "OPENCODE_API_KEY": "default-test",
            "OPENCODE_API_KEY__WORK": "data-test",
        },
        enabled_overrides_loader=lambda: {"opencode-zen:api-key": False},
    )
    for provider in ("opencode-go", "opencode-zen"):
        assert resolver.get_credentials(provider, f"{provider}:api-key:default") == "default-test"
        assert resolver.get_credentials(provider, f"{provider}:api-key:work") == "process-test"
    assert resolver.is_usable("opencode-go", "opencode-go:api-key")
    assert not resolver.is_usable("opencode-zen", "opencode-zen:api-key")
    resolver.reload_fallback_credentials({"OPENCODE_API_KEY": "replacement-test"})
    for provider in ("opencode-go", "opencode-zen"):
        assert (
            resolver.get_credentials(provider, f"{provider}:api-key:default") == "replacement-test"
        )
    resolver.reload_fallback_credentials({})
    for provider in ("opencode-go", "opencode-zen"):
        assert not resolver.has_credentials(provider, f"{provider}:api-key:default")
        assert resolver.has_credentials(provider, f"{provider}:api-key:work")
