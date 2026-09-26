"""Disposable server setup: isolated environment and fake-Provider settings."""

from core.settings.normalizers import normalize_reflection_settings
from scripts.perf_load_suite.stack import AGENT_MODEL, child_environment, provider_settings


def test_child_environment_drops_credentials_and_vbot_overrides():
    environment = child_environment(
        {
            "PATH": "/bin",
            "SystemRoot": "C:\\Windows",
            "LC_ALL": "C",
            "OPENAI_API_KEY": "secret",
            "VBOT_DATA_DIR": "/real/data",
            "VBOT_PORT": "8420",
        }
    )

    assert environment == {
        "PATH": "/bin",
        "SystemRoot": "C:\\Windows",
        "LC_ALL": "C",
        "PYTHONUTF8": "1",
        "VBOT_LOG_STDIO": "0",
    }


def test_settings_make_the_fake_provider_the_default_tool_capable_model():
    settings = provider_settings("http://127.0.0.1:5000/v1")

    assert settings["defaults"]["agent"]["model"] == AGENT_MODEL
    assert normalize_reflection_settings(settings["reflection"])["enabled"] is False
    provider = settings["providers"]["custom"]["perf"]
    assert (provider["adapter"], provider["base_url"], provider["auth"]) == (
        "openai_compatible",
        "http://127.0.0.1:5000/v1",
        "none",
    )
    assert provider["models"]["perf-model"]["capabilities"]["tools"] is True
    assert settings["providers"]["connections"] == {"perf:default": True}
