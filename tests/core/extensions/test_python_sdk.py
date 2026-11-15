"""Public SDK identities, standalone typing and packaged distribution contracts."""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

import vbot_extension_sdk as sdk
from cli.application.payload import app_paths

ROOT = Path(__file__).resolve().parents[3]
SDK_MODULES = ("", ".providers", ".channels", ".desktop", ".tools", ".commands")


@pytest.mark.parametrize("suffix", SDK_MODULES)
def test_sdk_exports_keep_host_identity(suffix: str) -> None:
    public = importlib.import_module(f"vbot_extension_sdk{suffix}")
    for name, owner in public._EXPORTS.items():
        assert getattr(public, name) is getattr(importlib.import_module(owner), name)
        assert name in dir(public)


def test_sdk_negotiates_running_host_version() -> None:
    assert sdk.require_api() == sdk.API_VERSION
    assert sdk.require_api(30, maximum=sdk.API_VERSION) == sdk.API_VERSION
    with pytest.raises(RuntimeError):
        sdk.require_api(sdk.API_VERSION + 1)
    with pytest.raises(ValueError):
        sdk.require_api(True)


@pytest.mark.parametrize(
    ("module", "names"),
    (
        ("", ("ExtensionAPI", "ExtensionServices", "ExtensionSubscription")),
        ("providers", ("ProviderAdapter",)),
        ("channels", ("ChannelAdapter", "ConversationTransport")),
        ("desktop", ("DesktopExtensionAPI", "ExecutionHandler")),
    ),
)
def test_sdk_type_declarations_preserve_callable_shapes(
    module: str, names: tuple[str, ...]
) -> None:
    public = importlib.import_module(f"vbot_extension_sdk{'.' + module if module else ''}")
    stub = ast.parse((ROOT / "vbot_extension_sdk" / f"{module or '__init__'}.pyi").read_text())
    classes = {node.name: node for node in stub.body if isinstance(node, ast.ClassDef)}
    for name in names:
        cls = getattr(public, name)
        for node in classes[name].body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            actual: Any = getattr(cls, node.name)
            if isinstance(inspect.getattr_static(cls, node.name), classmethod):
                actual = actual.__func__
            if isinstance(actual, property):
                actual = actual.fget
            parameters = list(inspect.signature(actual).parameters.values())
            positional = [*node.args.posonlyargs, *node.args.args]
            expected = [
                (arg.arg, index >= len(positional) - len(node.args.defaults))
                for index, arg in enumerate(positional)
            ]
            if node.args.vararg:
                expected.append((node.args.vararg.arg, False))
            expected.extend(
                (arg.arg, default is not None)
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
            )
            if node.args.kwarg:
                expected.append((node.args.kwarg.arg, False))
            assert [
                (p.name, p.default is not inspect.Parameter.empty) for p in parameters
            ] == expected


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
    env = dict(os.environ, MYPYPATH=str(tmp_path / "site"))
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--no-site-packages",
        "--config-file",
        str(config),
        "--no-error-summary",
        str(consumer),
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    consumer.write_text(
        consumer.read_text()
        + "\nasync def invalid(p: ProviderAdapter) -> None:\n    await p.send([], model_id=123)\n"
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
    assert "[arg-type]" in result.stdout
