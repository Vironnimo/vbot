"""Tests for the Desktop ``--mock-wakeword`` flag in ``desktop.main``.

The wakeword *settings* read/write/merge functions moved to ``desktop.settings``
and are covered by ``test_settings.py``; the accessor query-param helper moved to
``desktop.connection`` (covered by ``test_connection.py``). What remains here is
the entrypoint argument behavior that still lives in ``desktop.main`` — the
``--mock-wakeword`` flag.
"""

from __future__ import annotations

import pytest

from desktop import main as desktop_main


@pytest.mark.parametrize("flag", [True, False])
def test_parse_args_mock_wakeword_flag(flag: bool) -> None:
    argv = ["--mock-wakeword"] if flag else []

    args = desktop_main.parse_args(argv)

    assert args.mock_wakeword is flag


def test_parse_args_mock_wakeword_defaults_to_false() -> None:
    args = desktop_main.parse_args(["--host", "127.0.0.1"])

    assert args.mock_wakeword is False
