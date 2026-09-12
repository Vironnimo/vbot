"""Terminal manager: input behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import (
    TerminalManager,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    eventually,
    owner,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


@pytest.mark.asyncio
async def test_status_is_bounded_and_pages_back_with_absolute_lines(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    factory.adapters[0].emit("".join(f"line-{index}\r\n" for index in range(50)))
    await eventually(lambda: session.renderer.revision > 0)

    snapshot = await manager.snapshot(session.terminal_id, owner(), lines=3)
    assert snapshot["scrollback"]["line_count"] == 3
    assert snapshot["scrollback"]["total_lines"] == 50
    assert snapshot["scrollback"]["next_start_line"] is not None

    older = await manager.snapshot(
        session.terminal_id,
        owner(),
        lines=3,
        start_line=snapshot["scrollback"]["next_start_line"],
    )
    assert older["scrollback"]["line_count"] == 3
    assert older["scrollback"]["start_line"] == snapshot["scrollback"]["next_start_line"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited", [None, "", "dumb", "screen-256color"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_real_terminal_corrects_inherited_dumb_term_and_preserves_explicit_env(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inherited: str | None,
    explicit: bool,
) -> None:
    if inherited is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", inherited)

    async def environment():
        return dict(os.environ)

    monkeypatch.setattr(terminal_module, "get_shell_env", environment)
    manager, factory = terminal_manager
    await manager.spawn(
        owner(),
        ["fake-tui"],
        cwd=tmp_path,
        env={"TERM": "dumb"} if explicit else None,
        origin_run_id="run-a",
    )
    expected = "screen-256color" if inherited == "screen-256color" else "xterm-256color"
    assert factory.calls[0][2]["TERM"] == ("dumb" if explicit else expected)
    assert os.environ.get("TERM") == inherited


@pytest.mark.asyncio
async def test_launch_passes_every_program_exact_argv_without_private_environment(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await manager.spawn(
        owner(),
        ["codex", "--profile", "work"],
        cwd=tmp_path,
        env={"CALLER_VALUE": "unchanged"},
        columns=120,
        rows=32,
        origin_run_id="run-a",
    )
    launch_argv, _cwd, launch_env, _rows, _columns = factory.calls[0]
    assert launch_argv == ["codex", "--profile", "work"]
    assert launch_env["CALLER_VALUE"] == "unchanged"
    assert not any(name.startswith("VBOT_TERMINAL_") for name in launch_env)
    assert not hasattr(session, "codex_integration")


@pytest.mark.asyncio
async def test_exact_agent_data_and_named_keys_share_the_generic_pty(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)

    raw = "\x1b[200~paste\r\n\x1b[201~"
    sent = await manager.send_input(
        session.terminal_id,
        owner(),
        data=raw,
        text=None,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )
    await manager.send_input(
        session.terminal_id,
        owner(),
        data=None,
        text=None,
        key="f12",
        expected_screen_revision=None,
        origin_run_id="run-b",
    )

    assert factory.adapters[0].writes == [raw, "\x1b[24~"]
    assert sent["characters_sent"] == len(raw)


@pytest.mark.asyncio
async def test_multiline_text_uses_bracketed_paste_only_when_terminal_enables_it(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    multiline = "first\n  second\n    third"
    adapter.emit("\x1b[?2004h")
    await eventually(lambda: session.renderer.bracketed_paste_enabled)

    pasted = await manager.send_input(
        session.terminal_id,
        owner(),
        data=None,
        text=multiline,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )

    assert adapter.writes == [f"\x1b[200~{multiline}\x1b[201~"]
    assert pasted["bracketed_paste"] is True

    adapter.emit("\x1b[?2004l")
    await eventually(lambda: not session.renderer.bracketed_paste_enabled)
    typed = await manager.send_input(
        session.terminal_id,
        owner(),
        data=None,
        text=multiline,
        key=None,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )

    assert adapter.writes[-1] == multiline
    assert typed["bracketed_paste"] is False


@pytest.mark.asyncio
async def test_operator_read_preserves_binding_and_rejects_stale_guarded_input(
    terminal_manager, tmp_path
):
    manager, factory = terminal_manager
    session = await manager.spawn(
        owner(), ["codex"], cwd=tmp_path, env=None, origin_run_id="run-live"
    )
    original_attachment = session.attachment
    original_observation = session.observed_screen
    snapshot = manager.read_for_operator(session.terminal_id)
    assert snapshot["terminal"]["terminal_id"] == session.terminal_id
    assert session.attachment == original_attachment
    assert session.observed_screen == original_observation
    revision = snapshot["terminal"]["screen_revision"]
    await manager.send_operator_input(
        session.terminal_id, "first", expected_screen_revision=revision
    )

    with pytest.raises(TerminalStaleScreenError):
        await manager.send_operator_input(
            session.terminal_id, "second", expected_screen_revision=revision
        )
    assert session.attachment == original_attachment
