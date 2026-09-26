"""Explicit per-user payload installation, separate from Runtime data initialization."""

from __future__ import annotations

import argparse
import base64
import shutil
import zipfile
from pathlib import Path

from cli.application.packages import validate_release
from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    exclusive,
    load_installation,
)


def archive_payload(install: Installation, version_id: str) -> Path:
    payload = install.version(version_id)
    validate_release(payload, shape=install.install_shape, remove_bytecode_caches=True)
    archive = contained(install.root, f"staging/{payload.name}.zip")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in payload.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(payload).as_posix())
    return archive


def install_payload(
    root: Path,
    payload: Path,
    *,
    shape: str,
    host: str = "127.0.0.1",
    port: int = 8420,
    data_dir: Path | None = None,
    public_key: str = "",
    from_checkout: Path | None = None,
) -> Installation:
    root, payload = root.expanduser().absolute(), payload.expanduser().resolve()
    resolved_root = root.resolve()
    if (
        resolved_root == resolved_root.parent
        or resolved_root == Path.home().resolve()
        or resolved_root.is_relative_to(payload)
        or payload.is_relative_to(resolved_root)
    ):
        raise ApplicationError(
            "Choose a dedicated application directory outside the supplied payload"
        )
    if (
        shape not in {"server", "server-desktop", "desktop-client"}
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise ApplicationError("Invalid application installation options")
    manifest = validate_release(payload, shape=shape)
    if (root / "application.json").exists():
        install = load_installation(root)
        raise ApplicationError(
            f"An application is already installed at {install.root}; "
            "use vbot update --package to update it"
        )
    if public_key:
        try:
            if len(base64.b64decode(public_key, validate=True)) != 32:
                raise ValueError
        except ValueError as exc:
            raise ApplicationError(
                "Release public key must be base64 raw Ed25519 public key"
            ) from exc
    allowed = {"vBot.exe", "vBot.GUI.exe", ".operation.lock"}
    if root.exists() and any(
        path.name not in allowed
        and not (
            path.stem.startswith("unins")
            and path.stem[5:].isdigit()
            and path.suffix.lower() in {".exe", ".dat", ".msg"}
        )
        for path in root.iterdir()
    ):
        raise ApplicationError("The application destination is not empty")
    transition = None
    if from_checkout is not None:
        from cli.application.integration import prepare_checkout_transition
        from cli.install_state import read_install_state

        source_state = read_install_state(from_checkout)
        if source_state is not None and source_state.source_track == "dev":
            from cli.application.source_updates import inspect_checkout

            inspect_checkout(from_checkout)
        transition = prepare_checkout_transition(from_checkout, shape=shape)
        previous = transition.state
        if previous.server_data_directory:
            data_dir = Path(previous.server_data_directory)
        host = previous.server_host or host
        port = previous.server_port or port
    resolved_data = (data_dir or Path.home() / ".vbot").expanduser().absolute()
    if shape != "desktop-client" and (
        resolved_data == root
        or resolved_data.is_relative_to(root)
        or root.is_relative_to(resolved_data)
    ):
        raise ApplicationError("Application and server data directories must be separate")
    install = Installation(
        root,
        shape,
        None if shape == "desktop-client" else host,
        None if shape == "desktop-client" else port,
        None if shape == "desktop-client" else str(resolved_data),
        release_public_key=public_key,
    )
    root.mkdir(parents=True, exist_ok=True)
    with exclusive(root):
        destination = install.version(manifest["version_id"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(payload, destination)
        validate_release(destination, shape=shape)
        bootstrap = destination / "runtime/vBot.exe"
        if bootstrap.is_file() and not (root / "vBot.exe").exists():
            shutil.copy2(bootstrap, root / "vBot.exe")
        if install.owns_server and not resolved_data.exists():
            from core.storage import initialize_data_directory

            initialize_data_directory(
                resolved_data, resources_dir=destination / "app" / "resources"
            )
        if transition is not None and transition.state.source_track == "dev":
            from cli.application.source_updates import bind_checkout

            assert from_checkout is not None
            bind_checkout(install, from_checkout)
        install.save()
        install.activate(manifest["version_id"])
        from cli.application.integration import refresh_gui_entrypoints

        refresh_gui_entrypoints(install)
    if transition is not None:
        from cli.application.integration import finish_checkout_transition

        finish_checkout_transition(install, transition)
    return install


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install a prepared vBot payload")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--shape", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--public-key", default="")
    parser.add_argument("--from-checkout", type=Path)
    args = parser.parse_args(argv)
    install_payload(
        args.root,
        args.payload,
        shape=args.shape,
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        public_key=args.public_key,
        from_checkout=args.from_checkout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
