"""Public SDK identities, standalone typing and packaged distribution contracts."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Literal

import pytest

import vbot_extension_sdk as sdk
from cli.application.payload import app_paths
from tests.core.extensions.sdk_contract import HOST_CONSTRUCTORS, check_annotations

ROOT = Path(__file__).resolve().parents[3]
SDK_MODULES = ("", ".providers", ".channels", ".desktop", ".tools", ".commands")


@pytest.mark.parametrize("suffix", SDK_MODULES)
def test_sdk_exports_keep_host_identity(suffix: str) -> None:
    public = importlib.import_module(f"vbot_extension_sdk{suffix}")
    for name, owner in public._EXPORTS.items():
        module, _, attribute = owner.partition(":")
        assert getattr(public, name) is getattr(importlib.import_module(module), attribute or name)
        assert name in dir(public)


def test_sdk_negotiates_running_host_version() -> None:
    assert sdk.require_api() == sdk.API_VERSION
    assert sdk.require_api(30, maximum=sdk.API_VERSION) == sdk.API_VERSION
    with pytest.raises(RuntimeError):
        sdk.require_api(sdk.API_VERSION + 1)
    with pytest.raises(ValueError):
        sdk.require_api(True)


def test_sdk_declarations_match_the_complete_public_contract(tmp_path: Path) -> None:
    # The distributed declaration is the public projection: implementations can
    # have extra members, but every offered member and subclass duty is checked.
    errors, aliases = check_annotations(ROOT)
    assert not errors, "\n".join(errors)
    config = tmp_path / "stubtest.ini"
    config.write_text("[mypy]\npython_version = 3.14\nignore_missing_imports = True\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy.stubtest",
            "vbot_extension_sdk",
            "--concise",
            "--ignore-missing-stub",
            "--mypy-config-file",
            str(config),
        ],
        cwd=ROOT,
        env=dict(os.environ, MYPY_CACHE_DIR=str(tmp_path.parent / "sdk-mypy-cache")),
        capture_output=True,
        text=True,
        check=False,
        timeout=25,
    )
    # mypy.stubtest currently treats PEP 695 and Literal aliases as runtime
    # classes. Each exact alias here has already passed structural comparison;
    # no wildcard suppressions or unchecked alias exemptions are accepted.
    remaining = [
        line
        for line in result.stdout.splitlines()
        if line.split(" ", 1)[0] not in aliases | HOST_CONSTRUCTORS.keys()
    ]
    assert result.returncode in (0, 1), result.stdout + result.stderr
    assert not remaining and not result.stderr, "\n".join(remaining) + result.stderr


def test_sdk_wheel_is_current_dependency_free_and_present_in_payloads() -> None:
    wheel = ROOT / "resources" / "sdk" / f"vbot_extension_sdk-{sdk.SDK_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        sources = {
            f"vbot_extension_sdk/{path.name}": path
            for path in (ROOT / "vbot_extension_sdk").iterdir()
            if path.suffix in {".py", ".pyi"} or path.name == "py.typed"
        }
        metadata_root = f"vbot_extension_sdk-{sdk.SDK_VERSION}.dist-info"
        assert set(archive.namelist()) == set(sources) | {
            f"{metadata_root}/METADATA",
            f"{metadata_root}/WHEEL",
            f"{metadata_root}/RECORD",
        }
        for relative, path in sources.items():
            assert archive.read(relative) == path.read_bytes()
        metadata = archive.read(f"vbot_extension_sdk-{sdk.SDK_VERSION}.dist-info/METADATA").decode()
        assert "Requires-Dist:" not in metadata
        assert f"Version: {sdk.SDK_VERSION}\n" in metadata
        assert (
            "Requires-Python: >=3.14,<3.15" in metadata
            or "Requires-Python: <3.15,>=3.14" in metadata
        )
        assert all(
            not name.startswith(("core/", "server/", "desktop/")) for name in archive.namelist()
        )
    for shape in ("server", "server-desktop", "desktop-client"):
        assert "vbot_extension_sdk" in app_paths(shape)
    project = tomllib.loads((ROOT / "sdk/python/pyproject.toml").read_text())["project"]
    assert project["version"] == sdk.SDK_VERSION
    assert project["requires-python"] == ">=3.14,<3.15"


def test_sdk_contract_rejects_type_and_implementation_obligation_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vbot_extension_sdk.providers import ProviderAdapter

    monkeypatch.setitem(sdk.ExtensionServices.query.__annotations__, "name", int)
    monkeypatch.setattr(ProviderAdapter, "__abstractmethods__", {"send", "stream", "aclose", "new"})
    monkeypatch.setattr(sdk, "ServiceKind", Literal["query", "execute", "new"])
    monkeypatch.setitem(
        importlib.import_module("vbot_extension_sdk")._EXPORTS, "Undeclared", "core.extensions"
    )
    errors, _ = check_annotations(ROOT)
    assert any("ExtensionServices.query.name: annotation" in error for error in errors)
    assert any("ProviderAdapter: abstract subclass obligations differ" in error for error in errors)
    assert any("ServiceKind: type alias differs" in error for error in errors)
    assert any("runtime export has no public declaration" in error for error in errors)


def test_sdk_native_import_does_not_load_server_stack(tmp_path: Path) -> None:
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import vbot_extension_sdk
from vbot_extension_sdk.desktop import DesktopExtensionAPI
assert DesktopExtensionAPI.__module__ == 'desktop.extensions'
assert not any(
    name.startswith(('core.providers', 'core.channels', 'server')) for name in sys.modules
)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_distributed_types_work_without_the_host(tmp_path: Path) -> None:
    """The authoring artifact is useful without application modules or dependencies."""
    wheel = ROOT / "resources" / "sdk" / f"vbot_extension_sdk-{sdk.SDK_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(tmp_path / "site")
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\npython_version = 3.14\nstrict = True\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        """from vbot_extension_sdk import ExtensionHost, InteractionButton
from vbot_extension_sdk.providers import ProviderAdapter, ProviderError
from vbot_extension_sdk.channels import ChannelAdapter

async def use(provider: ProviderAdapter, channel: ChannelAdapter, host: ExtensionHost) -> None:
    try:
        await provider.send([], model_id="probe")
    except ProviderError as error:
        assert isinstance(error.retryable, bool)
    await channel.send("hello", "target", buttons=[[InteractionButton("OK", "probe:ok")]])
    assert host.services is not None
    subscription = host.services.subscribe(["agents"], lambda event: None)
    assert isinstance(subscription.status, dict)
    subscription.close()
""",
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        MYPYPATH=str(tmp_path / "site"),
        MYPY_CACHE_DIR=str(tmp_path.parent / "sdk-mypy-cache"),
    )
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--no-site-packages",
        "--config-file",
        str(config),
        "--no-error-summary",
        str(consumer),
        str(tmp_path / "invalid.py"),
    ]
    invalid = tmp_path / "invalid.py"
    invalid.write_text(
        """from consumer import ProviderAdapter

from vbot_extension_sdk import ExtensionOperations
from vbot_extension_sdk.commands import ExtensionCommandContext

async def invalid(p: ProviderAdapter) -> None:
    await p.send([], model_id=123)

ExtensionOperations("unwired")
ExtensionCommandContext("unwired")
"""
    )
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert "consumer.py:" not in result.stdout
    assert "[arg-type]" in result.stdout
    assert result.stdout.count('expected "Never"') == 2
