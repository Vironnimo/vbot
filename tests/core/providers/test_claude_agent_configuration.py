"""Claude executable selection without relying on the server's inherited PATH."""

import os
from pathlib import Path

import pytest

from core.providers import _claude_agent_sdk as module
from core.providers.errors import ProviderAuthError


def test_executable_selection_honors_explicit_sources_then_native_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured.exe"
    environment = tmp_path / "environment.exe"
    configured.touch()
    environment.touch()
    monkeypatch.delenv("VBOT_CLAUDE_PATH", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "on-path")
    assert module.executable() == str(Path("on-path").resolve())
    assert module.executable(str(configured)) == str(configured)
    monkeypatch.setenv("VBOT_CLAUDE_PATH", str(environment))
    assert module.executable(str(configured)) == str(environment)
    monkeypatch.delenv("VBOT_CLAUDE_PATH")
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    native = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    native.parent.mkdir(parents=True)
    native.touch()
    assert module.executable() == str(native)
    with pytest.raises(ProviderAuthError):
        module.executable(str(tmp_path / "missing.exe"))

    # The child starts in a private cwd, so discovered and configured paths
    # must keep the server-side meaning of a relative executable location.
    monkeypatch.chdir(tmp_path)
    assert module.executable("configured.exe") == str(configured)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "environment.exe")
    assert module.executable() == str(environment)
