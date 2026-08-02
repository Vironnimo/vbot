#!/usr/bin/env python
"""Safe one-shot Home Assistant WebSocket client for advanced configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

DEFAULT_HASS_URL = "http://homeassistant.local:8123"
TOKEN_ENV_KEY = "HASS_TOKEN"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_JSON_FILE_BYTES = 32 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 32 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
URL_PATH_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
READ_ONLY_COMMANDS = frozenset(
    {
        "config/area_registry/list",
        "config/device_registry/list",
        "config/entity_registry/list",
        "config/entity_registry/list_for_display",
        "config/floor_registry/list",
        "config/label_registry/list",
        "get_config",
        "get_services",
        "get_states",
        "lovelace/config",
        "lovelace/dashboards/list",
        "lovelace/info",
        "lovelace/resources",
        "lovelace/resources/list",
    }
)
RESERVED_COMMAND_FIELDS = frozenset({"access_token", "id"})
DASHBOARD_METADATA_FIELDS = frozenset(
    {
        "allow_single_word",
        "icon",
        "require_admin",
        "show_in_sidebar",
        "title",
        "url_path",
    }
)


class HomeAssistantScriptError(RuntimeError):
    """A safe, user-actionable script failure."""


class CommandClient(Protocol):
    """Minimal transport used by the dashboard workflows."""

    def request(self, payload: dict[str, Any]) -> Any:
        """Send one authenticated command and return its result."""
        ...


class HomeAssistantWebSocket:
    """Authenticate and execute one Home Assistant command per connection."""

    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        if not token.strip():
            raise HomeAssistantScriptError(f"{TOKEN_ENV_KEY} is not available")
        self.websocket_url = build_websocket_url(base_url)
        self._token = token
        self._timeout = timeout

    def request(self, payload: dict[str, Any]) -> Any:
        command_type = payload.get("type")
        if not isinstance(command_type, str) or not command_type:
            raise HomeAssistantScriptError("WebSocket command requires a non-empty type")
        reserved = sorted(set(payload) & RESERVED_COMMAND_FIELDS)
        if reserved:
            raise HomeAssistantScriptError(
                f"WebSocket command must not contain reserved field(s): {', '.join(reserved)}"
            )

        try:
            with connect(
                self.websocket_url,
                open_timeout=self._timeout,
                close_timeout=self._timeout,
                max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
                proxy=None,
            ) as websocket:
                required = _receive_object(websocket, self._timeout)
                if required.get("type") != "auth_required":
                    raise HomeAssistantScriptError("Home Assistant did not request authentication")
                websocket.send(
                    json.dumps(
                        {"type": "auth", "access_token": self._token},
                        separators=(",", ":"),
                    )
                )
                authenticated = _receive_object(websocket, self._timeout)
                if authenticated.get("type") != "auth_ok":
                    message = authenticated.get("message")
                    detail = (
                        message if isinstance(message, str) and message else "authentication failed"
                    )
                    raise HomeAssistantScriptError(detail)

                command_id = 1
                websocket.send(
                    json.dumps(
                        {"id": command_id, **payload},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                for _ in range(100):
                    response = _receive_object(websocket, self._timeout)
                    if response.get("id") != command_id:
                        continue
                    if response.get("type") != "result":
                        raise HomeAssistantScriptError(
                            f"Unexpected Home Assistant response type: {response.get('type')!r}"
                        )
                    if response.get("success") is not True:
                        error = response.get("error")
                        if isinstance(error, dict):
                            code = error.get("code")
                            message = error.get("message")
                            detail = ": ".join(
                                str(value) for value in (code, message) if value not in (None, "")
                            )
                        else:
                            detail = "command failed"
                        raise HomeAssistantScriptError(
                            f"Home Assistant rejected {command_type}: {detail or 'command failed'}"
                        )
                    return response.get("result")
        except HomeAssistantScriptError:
            raise
        except (OSError, TimeoutError, WebSocketException) as error:
            raise HomeAssistantScriptError(
                f"Home Assistant WebSocket request failed: {error}"
            ) from error

        raise HomeAssistantScriptError("Home Assistant returned no matching command result")


def build_websocket_url(base_url: str) -> str:
    """Convert an HTTP(S) Home Assistant base URL into its WebSocket endpoint."""
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HomeAssistantScriptError("Home Assistant URL must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise HomeAssistantScriptError("Home Assistant URL must not contain a query or fragment")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/api/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def validate_dashboard_config(config: Any) -> dict[str, Any]:
    """Perform conservative structural checks without rejecting valid custom cards."""
    if not isinstance(config, dict):
        raise HomeAssistantScriptError("Dashboard config must be a JSON object")
    has_views = "views" in config
    has_strategy = "strategy" in config
    if has_views == has_strategy:
        raise HomeAssistantScriptError(
            "Dashboard config must contain exactly one of views or strategy"
        )
    if has_strategy:
        strategy = config["strategy"]
        if not isinstance(strategy, dict) or not isinstance(strategy.get("type"), str):
            raise HomeAssistantScriptError("Dashboard strategy must have a string type")
        return config

    views = config["views"]
    if not isinstance(views, list):
        raise HomeAssistantScriptError("Dashboard views must be an array")
    for view_index, view in enumerate(views):
        if not isinstance(view, dict):
            raise HomeAssistantScriptError(f"views[{view_index}] must be an object")
        if not isinstance(view.get("title"), str) or not view["title"].strip():
            raise HomeAssistantScriptError(f"views[{view_index}].title must be non-empty")
        _validate_card_array(view.get("cards"), f"views[{view_index}].cards")
        sections = view.get("sections")
        if sections is not None:
            if not isinstance(sections, list):
                raise HomeAssistantScriptError(f"views[{view_index}].sections must be an array")
            for section_index, section in enumerate(sections):
                if not isinstance(section, dict):
                    raise HomeAssistantScriptError(
                        f"views[{view_index}].sections[{section_index}] must be an object"
                    )
                _validate_card_array(
                    section.get("cards"),
                    f"views[{view_index}].sections[{section_index}].cards",
                )
    return config


def validate_dashboard_metadata(metadata: Any) -> dict[str, Any]:
    """Validate the storage-dashboard fields accepted by the create workflow."""
    if not isinstance(metadata, dict):
        raise HomeAssistantScriptError("Dashboard metadata must be a JSON object")
    unknown = sorted(set(metadata) - DASHBOARD_METADATA_FIELDS)
    if unknown:
        raise HomeAssistantScriptError(
            f"Dashboard metadata contains unknown field(s): {', '.join(unknown)}"
        )
    title = metadata.get("title")
    url_path = metadata.get("url_path")
    if not isinstance(title, str) or not title.strip():
        raise HomeAssistantScriptError("Dashboard metadata title must be non-empty")
    if not isinstance(url_path, str) or URL_PATH_PATTERN.fullmatch(url_path) is None:
        raise HomeAssistantScriptError(
            "Dashboard metadata url_path must contain lowercase letters, digits, "
            "hyphens, or underscores"
        )
    for field in ("allow_single_word", "require_admin", "show_in_sidebar"):
        if field in metadata and not isinstance(metadata[field], bool):
            raise HomeAssistantScriptError(f"Dashboard metadata {field} must be a boolean")
    if "icon" in metadata and (
        not isinstance(metadata["icon"], str) or not metadata["icon"].strip()
    ):
        raise HomeAssistantScriptError("Dashboard metadata icon must be non-empty")
    if "-" not in url_path and url_path != "lovelace" and not metadata.get("allow_single_word"):
        raise HomeAssistantScriptError("A single-word url_path requires allow_single_word: true")
    return dict(metadata)


def apply_dashboard(
    client: CommandClient,
    config: dict[str, Any],
    *,
    url_path: str | None,
    expected_sha256: str | None,
    backup_path: Path | None,
    apply: bool,
) -> dict[str, Any]:
    """Dry-run or safely replace one existing dashboard configuration."""
    target = validate_dashboard_config(config)
    current = _read_live_dashboard(client, url_path)
    current_sha256 = dashboard_sha256(current)
    target_sha256 = dashboard_sha256(target)
    if expected_sha256 is not None:
        expected_sha256 = _validate_sha256(expected_sha256)
        if expected_sha256 != current_sha256:
            raise HomeAssistantScriptError(
                "Live dashboard changed since export; export it again before applying"
            )
    summary: dict[str, Any] = {
        "ok": True,
        "action": "dashboard_apply",
        "applied": False,
        "changed": current_sha256 != target_sha256,
        "current_sha256": current_sha256,
        "target_sha256": target_sha256,
        "url_path": url_path,
    }
    if not apply or current_sha256 == target_sha256:
        return summary
    if expected_sha256 is None:
        raise HomeAssistantScriptError("--expected-sha256 is required with --apply")
    if backup_path is None:
        raise HomeAssistantScriptError("--backup is required with --apply")
    _write_json_file(backup_path, current, overwrite=False)
    _save_and_verify(client, target, url_path)
    return {
        **summary,
        "applied": True,
        "backup": str(backup_path.resolve()),
        "verified_sha256": target_sha256,
    }


def create_dashboard(
    client: CommandClient,
    metadata: dict[str, Any],
    config: dict[str, Any],
    *,
    apply: bool,
) -> dict[str, Any]:
    """Dry-run or create one storage dashboard and its initial config."""
    dashboard_metadata = validate_dashboard_metadata(metadata)
    target = validate_dashboard_config(config)
    dashboards = _list_dashboards(client)
    url_path = str(dashboard_metadata["url_path"])
    if any(item.get("url_path") == url_path for item in dashboards):
        raise HomeAssistantScriptError(f"Dashboard url_path already exists: {url_path}")
    target_sha256 = dashboard_sha256(target)
    if not apply:
        return {
            "ok": True,
            "action": "dashboard_create",
            "applied": False,
            "url_path": url_path,
            "target_sha256": target_sha256,
        }

    created = client.request({"type": "lovelace/dashboards/create", **dashboard_metadata})
    if not isinstance(created, dict) or not isinstance(created.get("id"), str):
        raise HomeAssistantScriptError("Home Assistant returned invalid dashboard metadata")
    try:
        _save_and_verify(client, target, url_path)
    except HomeAssistantScriptError as error:
        try:
            client.request(
                {
                    "type": "lovelace/dashboards/delete",
                    "dashboard_id": created["id"],
                }
            )
        except HomeAssistantScriptError as rollback_error:
            raise HomeAssistantScriptError(
                "Dashboard initialization failed and metadata rollback also failed: "
                f"{rollback_error}"
            ) from error
        raise HomeAssistantScriptError(
            f"Dashboard initialization failed; created metadata was rolled back: {error}"
        ) from error
    return {
        "ok": True,
        "action": "dashboard_create",
        "applied": True,
        "dashboard": created,
        "url_path": url_path,
        "verified_sha256": target_sha256,
    }


def dashboard_sha256(config: dict[str, Any]) -> str:
    """Return a stable content hash independent of object key order."""
    return hashlib.sha256(_canonical_json(config)).hexdigest()


def _validate_card_array(value: Any, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise HomeAssistantScriptError(f"{path} must be an array")
    for index, card in enumerate(value):
        if not isinstance(card, dict):
            raise HomeAssistantScriptError(f"{path}[{index}] must be an object")
        if not isinstance(card.get("type"), str) or not card["type"].strip():
            raise HomeAssistantScriptError(f"{path}[{index}].type must be non-empty")


def _receive_object(websocket: Any, timeout: float) -> dict[str, Any]:
    raw = websocket.recv(timeout=timeout)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HomeAssistantScriptError("Home Assistant returned invalid UTF-8") from error
    if not isinstance(raw, str):
        raise HomeAssistantScriptError("Home Assistant returned a non-text WebSocket message")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HomeAssistantScriptError("Home Assistant returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise HomeAssistantScriptError("Home Assistant returned a non-object message")
    return payload


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_JSON_FILE_BYTES:
            raise HomeAssistantScriptError(f"{label} exceeds the 32 MiB limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise HomeAssistantScriptError(f"{label} does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise HomeAssistantScriptError(
            f"{label} is not valid JSON at line {error.lineno}, column {error.colno}"
        ) from error
    except UnicodeDecodeError as error:
        raise HomeAssistantScriptError(f"{label} is not valid UTF-8") from error
    if not isinstance(value, dict):
        raise HomeAssistantScriptError(f"{label} must contain a JSON object")
    return value


def _write_json_file(path: Path, value: Any, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    try:
        with path.open(mode, encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as error:
        raise HomeAssistantScriptError(f"Refusing to overwrite existing file: {path}") from error


def _validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if SHA256_PATTERN.fullmatch(normalized) is None:
        raise HomeAssistantScriptError("expected SHA-256 must contain exactly 64 hex characters")
    return normalized


def _dashboard_command(command_type: str, url_path: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": command_type}
    if url_path is not None:
        payload["url_path"] = url_path
    return payload


def _read_live_dashboard(client: CommandClient, url_path: str | None) -> dict[str, Any]:
    result = client.request(_dashboard_command("lovelace/config", url_path))
    return validate_dashboard_config(result)


def _save_and_verify(
    client: CommandClient,
    config: dict[str, Any],
    url_path: str | None,
) -> None:
    payload = _dashboard_command("lovelace/config/save", url_path)
    payload["config"] = config
    save_error: HomeAssistantScriptError | None = None
    try:
        client.request(payload)
    except HomeAssistantScriptError as error:
        save_error = error
    try:
        stored = _read_live_dashboard(client, url_path)
    except HomeAssistantScriptError as verify_error:
        if save_error is not None:
            raise HomeAssistantScriptError(
                "Dashboard save outcome is unknown; save failed and verification failed: "
                f"{verify_error}"
            ) from save_error
        raise
    if dashboard_sha256(stored) == dashboard_sha256(config):
        return
    if save_error is not None:
        raise save_error
    raise HomeAssistantScriptError("Dashboard verification did not match the proposed config")


def _list_dashboards(client: CommandClient) -> list[dict[str, Any]]:
    result = client.request({"type": "lovelace/dashboards/list"})
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise HomeAssistantScriptError("Home Assistant returned an invalid dashboard list")
    return result


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and safely change Home Assistant through its WebSocket API."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_HASS_URL,
        help=f"Existing Home Assistant base URL (default: {DEFAULT_HASS_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Connection and receive timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    call = subparsers.add_parser("call", help="Execute one allowlisted read-only command")
    call.add_argument("request", type=Path, help="JSON file containing the command object")

    subparsers.add_parser("dashboard-list", help="List known Lovelace dashboards")

    export = subparsers.add_parser("dashboard-export", help="Export one dashboard config")
    export.add_argument("output", type=Path, help="New JSON file for the exported config")
    export.add_argument("--url-path", help="Dashboard URL path; omit for the default dashboard")
    export.add_argument("--overwrite", action="store_true", help="Allow replacing the output file")

    validate = subparsers.add_parser("dashboard-validate", help="Validate a local dashboard config")
    validate.add_argument("config", type=Path, help="Dashboard JSON file")

    apply = subparsers.add_parser(
        "dashboard-apply", help="Dry-run or replace an existing dashboard config"
    )
    apply.add_argument("config", type=Path, help="Proposed dashboard JSON file")
    apply.add_argument("--url-path", help="Dashboard URL path; omit for the default dashboard")
    apply.add_argument("--expected-sha256", help="SHA-256 returned by the fresh export")
    apply.add_argument("--backup", type=Path, help="New backup JSON path, required with --apply")
    apply.add_argument("--apply", action="store_true", help="Perform the validated mutation")

    create = subparsers.add_parser("dashboard-create", help="Dry-run or create a storage dashboard")
    create.add_argument("metadata", type=Path, help="Dashboard metadata JSON file")
    create.add_argument("config", type=Path, help="Initial dashboard JSON file")
    create.add_argument("--apply", action="store_true", help="Perform the validated mutation")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout <= 0:
        raise HomeAssistantScriptError("--timeout must be greater than zero")
    if args.action == "dashboard-validate":
        config = validate_dashboard_config(_read_json_object(args.config, label="Dashboard config"))
        return {
            "ok": True,
            "action": "dashboard_validate",
            "sha256": dashboard_sha256(config),
            "views": len(config.get("views", [])),
        }

    token = os.environ.get(TOKEN_ENV_KEY, "")
    client = HomeAssistantWebSocket(args.url, token, args.timeout)
    if args.action == "call":
        request = _read_json_object(args.request, label="WebSocket request")
        command_type = request.get("type")
        if command_type not in READ_ONLY_COMMANDS:
            raise HomeAssistantScriptError(
                f"WebSocket command is not in the read-only allowlist: {command_type!r}"
            )
        result = client.request(request)
        return {"ok": True, "action": "call", "command": command_type, "result": result}
    if args.action == "dashboard-list":
        dashboards = _list_dashboards(client)
        return {
            "ok": True,
            "action": "dashboard_list",
            "count": len(dashboards),
            "dashboards": dashboards,
        }
    if args.action == "dashboard-export":
        config = _read_live_dashboard(client, args.url_path)
        _write_json_file(args.output, config, overwrite=args.overwrite)
        return {
            "ok": True,
            "action": "dashboard_export",
            "output": str(args.output.resolve()),
            "sha256": dashboard_sha256(config),
            "url_path": args.url_path,
            "views": len(config.get("views", [])),
        }
    if args.action == "dashboard-apply":
        config = _read_json_object(args.config, label="Dashboard config")
        return apply_dashboard(
            client,
            config,
            url_path=args.url_path,
            expected_sha256=args.expected_sha256,
            backup_path=args.backup,
            apply=args.apply,
        )
    if args.action == "dashboard-create":
        metadata = _read_json_object(args.metadata, label="Dashboard metadata")
        config = _read_json_object(args.config, label="Dashboard config")
        return create_dashboard(client, metadata, config, apply=args.apply)
    raise HomeAssistantScriptError(f"Unsupported action: {args.action}")


def main() -> int:
    """Run the CLI and emit a stable JSON success or failure object."""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        result = _run(args)
    except (HomeAssistantScriptError, OSError) as error:
        _emit({"ok": False, "error": str(error)}, stream=sys.stderr)
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
