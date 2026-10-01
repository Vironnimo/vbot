from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import cast

import pytest

from cli.application.autostart import UNIT_NAME, CommandRun, autostart, owned_unit
from cli.application.state import ApplicationError, Installation


def _install(root: Path) -> Installation:
    root.mkdir(parents=True)
    install = Installation(root, "server", "127.0.0.1", 8420, str((root.parent / "data").resolve()))
    install.bootstrap.write_bytes(b"bootstrap")
    (root / "vBot.exe").write_bytes(b"bootstrap")
    return install


def _operation(command: list[str]) -> dict[str, object]:
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    token = re.search(r'\$payloadToken = "([A-Za-z0-9+/=]+)"', script)
    assert token
    return cast(dict[str, object], json.loads(base64.b64decode(token.group(1))))


def test_windows_autostart_registers_and_verifies_exact_no_argument_launcher(
    tmp_path: Path,
) -> None:
    install = _install(tmp_path / "app")
    registration: dict[str, str] = {}

    def runner(command: list[str]) -> CommandRun:
        payload = _operation(command)
        operation = payload["operation"]
        if operation == "inspect":
            if not registration:
                return CommandRun(3, "", "")
            return CommandRun(0, json.dumps(registration), "")
        assert operation == "enable"
        registration.update(launcher=str(payload["launcher"]), arguments=str(payload["arguments"]))
        return CommandRun(0, "", "")

    result = autostart(install, "enable", platform="win32", runner=runner)

    assert result["enabled"] is True
    assert registration == {"launcher": str(install.root / "vBot.exe"), "arguments": ""}
    assert autostart(install, "status", platform="win32", runner=runner)["enabled"] is True


def test_windows_autostart_refuses_foreign_registration_before_disable(tmp_path: Path) -> None:
    install = _install(tmp_path / "app")

    def runner(command: list[str]) -> CommandRun:
        assert _operation(command)["operation"] == "inspect"
        return CommandRun(0, json.dumps({"launcher": "C:/foreign.exe", "arguments": ""}), "")

    with pytest.raises(ApplicationError, match="belongs to another"):
        autostart(install, "disable", platform="win32", runner=runner)


@pytest.mark.parametrize("lingering", [True, False], ids=["lingering", "no-lingering"])
def test_linux_autostart_owns_one_systemd_user_unit_that_runs_the_bootstrap_server(
    tmp_path: Path, lingering: bool
) -> None:
    install = _install(tmp_path / "app")
    units = tmp_path / "units"
    commands: list[list[str]] = []

    def runner(command: list[str]) -> CommandRun:
        commands.append(command)
        if command[0] == "loginctl" and not lingering:
            return CommandRun(1, "", "Access denied")
        return CommandRun(0, "", "")

    enabled = autostart(install, "enable", platform="linux", runner=runner, unit_dir=units)

    unit = (units / UNIT_NAME).read_text(encoding="utf-8")
    # Ownership means the unit's program is this installation's bootstrap.
    assert owned_unit(install, unit_dir=units) == units / UNIT_NAME
    assert '"--server" "--host" "127.0.0.1" "--port" "8420" "--data-dir"' in unit
    assert "Restart=on-failure" in unit and "KillMode=mixed" in unit
    assert commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", UNIT_NAME],
        ["loginctl", "enable-linger"],
    ]
    assert enabled["enabled"] is True and enabled["linger"] is lingering
    assert ("attention" in enabled) is not lingering
    assert autostart(install, "status", platform="linux", runner=runner, unit_dir=units)["enabled"]

    commands.clear()
    disabled = autostart(install, "disable", platform="linux", runner=runner, unit_dir=units)

    assert disabled == {"ok": True, "enabled": False, "unit": UNIT_NAME, "changed": True}
    assert not (units / UNIT_NAME).exists()
    assert commands[0] == ["systemctl", "--user", "disable", UNIT_NAME]


def test_linux_autostart_never_changes_another_installations_unit(tmp_path: Path) -> None:
    install = _install(tmp_path / "app")
    units = tmp_path / "units"
    units.mkdir()
    foreign = '[Service]\nExecStart="/opt/other/vbot" "--server"\n'
    (units / UNIT_NAME).write_text(foreign, encoding="utf-8")

    def runner(command: list[str]) -> CommandRun:
        raise AssertionError(f"unexpected command {command}")

    for action in ("enable", "disable"):
        with pytest.raises(ApplicationError, match="belongs to another"):
            autostart(install, action, platform="linux", runner=runner, unit_dir=units)

    assert owned_unit(install, unit_dir=units) is None
    assert (units / UNIT_NAME).read_text(encoding="utf-8") == foreign
