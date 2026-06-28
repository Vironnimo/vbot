"""Tests for Desktop entrypoint wakeword/accessor wiring in ``desktop.main``.

The wakeword *settings* read/write/merge functions moved to ``desktop.settings``
and are covered by ``test_settings.py``; what remains here is the entrypoint
behavior that still lives in ``desktop.main`` — the ``--mock-wakeword`` flag and
the accessor query-param helper.
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


def test_append_accessor_param_appends_to_root_url() -> None:
    result = desktop_main._append_accessor_param("http://127.0.0.1:8420/")

    assert result == "http://127.0.0.1:8420/?accessor=desktop"


def test_append_accessor_param_preserves_existing_params() -> None:
    result = desktop_main._append_accessor_param("http://127.0.0.1:8420/?foo=bar")

    assert result == "http://127.0.0.1:8420/?foo=bar&accessor=desktop"
