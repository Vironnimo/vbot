"""Extension loading, declarations reaching the Runtime, and live disable."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.runtime._configuration import _resolve_resources_path
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import (
    CAPABILITY_EXT_SOURCE,
    command_extension_source,
    dispatch_tool,
    dispatch_workflow_command,
    extension_record,
    lifecycle_extension_source,
    marker_lines,
    tool_names,
    write_extension,
    write_settings,
)

PROMPT_BLOCK_EXT_SOURCE = (
    "def register(api):\n"
    "    api.register_prompt_block('intro', default_text='Static extension intro.')\n"
    "    api.register_prompt_block('dynamic', render=lambda ctx: 'Dynamic extension text.')\n"
)

OTHER_RECALL_EXT_SOURCE = (
    "class OtherBackend:\n"
    "    def __init__(self, context):\n"
    "        self.context = context\n"
    "def register(api):\n"
    "    api.register_recall_backend('other_recall', OtherBackend)\n"
)


def _probe_source(tool_name: str) -> str:
    """An Extension whose Tool reports its config snapshot, live config, and a credential."""
    return (
        "from core.tools import tool_success\n"
        "_STATE = {}\n"
        "def _probe(context, arguments):\n"
        "    api = _STATE['api']\n"
        "    return tool_success({'snapshot': api.config, 'live': api.get_config(),\n"
        "                         'secret': api.resolve_credential('VBOT_PROBE_SECRET')})\n"
        "def register(api):\n"
        "    _STATE['api'] = api\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _probe)\n"
    )


def test_extensions_load_from_every_root_with_snapshot_and_live_configuration(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = config.data_dir
    import_marker = tmp_path / "imported.txt"
    write_extension(data_dir, "probe", _probe_source("probe_config"))
    write_extension(data_dir, "bare", _probe_source("bare_config"))
    write_extension(
        data_dir,
        "disabled_ext",
        "import pathlib\n"
        f"pathlib.Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        "def register(api):\n    pass\n",
    )
    write_settings(
        data_dir,
        {
            "extensions": {
                "disabled": ["disabled_ext"],
                "config": {"probe": {"token": "abc", "level": 2}},
            }
        },
    )
    monkeypatch.delenv("VBOT_PROBE_SECRET", raising=False)
    runtime = Runtime(config)
    runtime.start()
    try:
        # Bundled Extensions load from the install tree's resources.
        bundled_root = _resolve_resources_path(runtime.config) / "extensions"
        swarm = extension_record(runtime, "swarm")
        assert swarm.status == "loaded"
        assert swarm.entry_path.is_relative_to(bundled_root)
        # A disabled Extension is never imported.
        assert extension_record(runtime, "disabled_ext").status == "disabled"
        assert not import_marker.exists()

        configured = {"token": "abc", "level": 2}
        assert dispatch_tool(runtime, "probe_config", data_dir)["data"] == {
            "snapshot": configured,
            "live": configured,
            "secret": "",
        }
        assert dispatch_tool(runtime, "bare_config", data_dir)["data"] == {
            "snapshot": {},
            "live": {},
            "secret": "",
        }

        # Persisted config and credentials reach live reads without a restart;
        # the register-time snapshot stays as it was.
        runtime.storage.update_settings_sections(
            {"extensions": {"disabled": ["disabled_ext"], "config": {"probe": {"token": "new"}}}}
        )
        runtime.storage.set_data_dir_credential("VBOT_PROBE_SECRET", "from-data-dir")
        runtime.reload_environment_credentials()
        assert dispatch_tool(runtime, "probe_config", data_dir)["data"] == {
            "snapshot": configured,
            "live": {"token": "new"},
            "secret": "from-data-dir",
        }
        monkeypatch.setenv("VBOT_PROBE_SECRET", "from-process")
        assert dispatch_tool(runtime, "probe_config", data_dir)["data"]["secret"] == "from-process"
    finally:
        runtime.stop()


def test_extension_startup_waits_for_the_serving_lifespan_and_shutdown_follows_stop(
    config: Config, tmp_path: Path
) -> None:
    marker = tmp_path / "lifecycle.txt"
    write_extension(config.data_dir, "lifecycle_ext", lifecycle_extension_source(marker))
    runtime = Runtime(config)

    runtime.start()
    try:
        assert marker_lines(marker) == []
        asyncio.run(runtime.fire_extension_startup())
        assert marker_lines(marker) == ["startup"]
    finally:
        runtime.stop()
    assert marker_lines(marker) == ["startup", "shutdown"]


def test_extension_declarations_reach_the_runtime_until_live_disable(config: Config) -> None:
    data_dir = config.data_dir
    write_extension(data_dir, "capabilities_ext", CAPABILITY_EXT_SOURCE)
    write_extension(data_dir, "workflow_ext", command_extension_source("ready"))
    write_extension(data_dir, "promptext", PROMPT_BLOCK_EXT_SOURCE)
    write_extension(data_dir, "other_recall_ext", OTHER_RECALL_EXT_SOURCE)
    write_settings(data_dir, {"recall": {"backend": "ext_recall"}})
    runtime = Runtime(config)
    runtime.start()
    try:
        agent = runtime.agents.get("main")
        # The Tool is registered, allowlistable, and dispatches through the Runtime.
        allowed = [tool.name for tool in runtime.tools.list_tools(allowed_tools=["ext_echo"])]
        assert "ext_echo" in allowed
        assert dispatch_tool(runtime, "ext_echo", data_dir, {"value": "hi"})["data"] == {
            "value": "hi"
        }
        # The persisted Recall selection resolves the Extension backend, also on reload.
        assert {"ext_recall", "other_recall"} <= set(runtime.available_recall_backends())
        assert type(runtime.recall_backend).__name__ == "ExtBackend"
        runtime.reload_recall_backend()
        active = runtime.recall_backend
        assert type(active).__name__ == "ExtBackend"
        assert dispatch_workflow_command(runtime) == "ready"
        prompt = runtime.system_prompts.build_system_prompt(agent)
        assert "Static extension intro." in prompt
        assert "Dynamic extension text." in prompt

        # Disabling an Extension whose backend is inactive leaves the active one alone.
        asyncio.run(runtime.apply_extension_disabled_change({"other_recall_ext"}))
        assert runtime.recall_backend is active
        assert "other_recall" not in runtime.available_recall_backends()

        asyncio.run(
            runtime.apply_extension_disabled_change(
                {"capabilities_ext", "workflow_ext", "promptext"}
            )
        )

        for name in ("capabilities_ext", "workflow_ext", "promptext", "other_recall_ext"):
            assert extension_record(runtime, name).status == "disabled"
        assert "ext_echo" not in tool_names(runtime)
        assert runtime.command_dispatcher.prepare("/workflow") is None
        prompt = runtime.system_prompts.build_system_prompt(agent)
        assert "Static extension intro." not in prompt
        assert "Dynamic extension text." not in prompt
        # Recall falls back to the built-in default without rewriting the
        # persisted selection, so re-enabling on restart restores it.
        assert type(runtime.recall_backend).__name__ != "ExtBackend"
        assert "ext_recall" not in runtime.available_recall_backends()
        assert runtime.storage.load_recall_settings()["backend"] == "ext_recall"
    finally:
        runtime.stop()
