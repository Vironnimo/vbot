"""Explicit per-user payload installation, separate from Runtime data initialization."""

from __future__ import annotations

import argparse
import base64
import json
import shutil
from pathlib import Path

from cli.application.packages import validate_release
from cli.application.state import (
    CHANNEL_URLS,
    SHAPES,
    ApplicationError,
    Installation,
    current_platform,
    exclusive,
    load_installation,
)
from core.access.config import (
    ACCESS_MODES,
    AccessConfig,
    AccessConfigError,
    access_config,
    check_bind_host,
    default_host,
    load_tls_context,
)
from core.utils.atomic import atomic_write_text
from core.utils.config import DEFAULT_PORT
from core.utils.file_status import exists_strict

#: Global Agent defaults a new data directory starts with. An install-time seed,
#: not a Runtime fallback: an existing data directory keeps its settings.
FRESH_AGENT_DEFAULTS = {"thinking_effort": "high"}


def install_payload(
    root: Path,
    payload: Path,
    *,
    shape: str,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    data_dir: Path | None = None,
    public_key: str = "",
    channel: str = "release",
    access: AccessConfig | None = None,
) -> Installation:
    """Install ``payload`` at ``root``.

    A server installation records its access configuration (default ``local``);
    ``host`` defaults to the mode's bind address.
    """
    root, payload = root.expanduser().resolve(), payload.expanduser().resolve()
    resolved_root = root
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
        shape not in SHAPES
        or type(port) is not int
        or not 1 <= port <= 65535
        or channel not in CHANNEL_URLS
    ):
        raise ApplicationError("Invalid application installation options")
    server_access, server_host = _server_access(shape, access, host)
    manifest = validate_release(
        payload, shape=shape, platform=current_platform(), access=server_access
    )
    if public_key:
        try:
            if len(base64.b64decode(public_key, validate=True)) != 32:
                raise ValueError
        except ValueError as exc:
            raise ApplicationError(
                "Release public key must be base64 raw Ed25519 public key"
            ) from exc
    _require_new_destination(root)
    resolved_data = (data_dir or Path.home() / ".vbot").expanduser().resolve()
    if shape != "desktop-client" and (
        resolved_data == root
        or resolved_data.is_relative_to(root)
        or root.is_relative_to(resolved_data)
    ):
        raise ApplicationError("Application and server data directories must be separate")
    install = Installation(
        root,
        shape,
        server_host,
        None if shape == "desktop-client" else port,
        None if shape == "desktop-client" else str(resolved_data),
        release_url=CHANNEL_URLS[channel],
        release_public_key=public_key,
        server_access=server_access,
    )
    root.mkdir(parents=True, exist_ok=True)
    with exclusive(root):
        # Another installer can finish after our preflight and before this lock.
        _require_new_destination(root)
        destination = install.version(manifest["version_id"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(payload, destination)
        validate_release(destination, shape=shape)
        # The stable root bootstraps; the Windows installer has already placed its own.
        for name in (install.bootstrap.name, "vBot.GUI.exe"):
            bootstrap = destination / "runtime" / name
            if bootstrap.is_file() and not (root / name).exists():
                shutil.copy2(bootstrap, root / name)
        if install.owns_server and not resolved_data.exists():
            _initialize_data(resolved_data)
        install.save()
        install.activate(manifest["version_id"])
    return install


def _require_new_destination(root: Path) -> None:
    """Refuse existing content, including changes completed before admission."""
    if exists_strict(root / "application.json"):
        install = load_installation(root)
        raise ApplicationError(
            f"An application is already installed at {install.root}; "
            "use vbot update --package to update it"
        )
    allowed = {"vBot.exe", "vBot.GUI.exe", "vbot", ".operation.lock"}
    if exists_strict(root) and any(
        path.name not in allowed
        and not (
            path.stem.startswith("unins")
            and path.stem[5:].isdigit()
            and path.suffix.lower() in {".exe", ".dat", ".msg"}
        )
        for path in root.iterdir()
    ):
        raise ApplicationError("The application destination is not empty")


def _server_access(
    shape: str, access: AccessConfig | None, host: str | None
) -> tuple[AccessConfig | None, str | None]:
    """The access configuration and bind host a new installation records."""
    if shape == "desktop-client":
        if access is not None:
            raise ApplicationError("A Desktop Client has no server whose access could be set")
        return None, None
    selected = access or access_config("local")
    bind = host or default_host(selected)
    try:
        check_bind_host(selected, bind)
        if selected.direct_tls:
            assert selected.tls_cert is not None and selected.tls_key is not None
            load_tls_context(selected.tls_cert, selected.tls_key)
    except AccessConfigError as exc:
        raise ApplicationError(str(exc)) from exc
    return selected, bind


def add_access_arguments(parser: argparse.ArgumentParser) -> None:
    """The access options installers and ``vbot application access set`` share."""
    parser.add_argument(
        "--allow-network",
        action="append",
        default=[],
        metavar="<cidr>",
        help="lan: also admit this network, for example a VPN's 100.64.0.0/10 (repeatable)",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="<name>",
        help="also accept this name in a request's Host header, such as a DNS name of this "
        "machine (repeatable)",
    )
    parser.add_argument(
        "--public-url",
        metavar="<https-url>",
        help="authenticated: the HTTPS address browsers use, such as https://vbot.example.org",
    )
    parser.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        metavar="<ip-or-cidr>",
        help="authenticated: the reverse proxy that terminates HTTPS (repeatable)",
    )
    parser.add_argument(
        "--tls-cert", metavar="<path>", help="authenticated: certificate chain file for HTTPS"
    )
    parser.add_argument("--tls-key", metavar="<path>", help="authenticated: private key file")


def access_from_arguments(mode: str, args: argparse.Namespace) -> AccessConfig:
    """Validate the access options of ``add_access_arguments``."""
    try:
        return access_config(
            mode,
            allow_networks=args.allow_network,
            allow_hosts=args.allow_host,
            public_url=args.public_url,
            trusted_proxies=args.trusted_proxy,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
    except AccessConfigError as exc:
        raise ApplicationError(str(exc)) from exc


def _initialize_data(data_dir: Path) -> None:
    from core.storage import initialize_data_directory

    created = initialize_data_directory(data_dir)
    settings = created.layout.settings_file
    if settings in created.created_files:
        document = json.loads(settings.read_text(encoding="utf-8"))
        document["defaults"] = {"agent": dict(FRESH_AGENT_DEFAULTS)}
        atomic_write_text(settings, json.dumps(document, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    """The only installation command line.

    Windows bootstraps route ``vBot.exe application install ...`` here, running the
    payload's own runtime; ``install.sh`` runs this module with that runtime directly.
    """
    parser = argparse.ArgumentParser(description="Install a prepared vBot payload")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--shape", choices=sorted(SHAPES), required=True)
    parser.add_argument("--host", help="Bind address; default 127.0.0.1, or 0.0.0.0 for lan")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--public-key", default="")
    parser.add_argument("--channel", choices=sorted(CHANNEL_URLS), default="release")
    parser.add_argument("--access", choices=ACCESS_MODES, help="Server access mode (default local)")
    add_access_arguments(parser)
    args = parser.parse_args(argv)
    given = args.access is not None or any(
        (
            args.allow_network,
            args.allow_host,
            args.public_url,
            args.trusted_proxy,
            args.tls_cert,
            args.tls_key,
        )
    )
    install_payload(
        args.root,
        args.payload,
        shape=args.shape,
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        public_key=args.public_key,
        channel=args.channel,
        access=access_from_arguments(args.access or "local", args) if given else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
