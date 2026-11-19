"""Run the local CLI bound to the instance that owns a vbot Tool call."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cli._server_target import ServerInstance, parse_server_url, resolve_instance
from core.access.local_token import read_local_access


def changes_agent_access(args: argparse.Namespace) -> bool:
    """Classify a parsed command before any command handler or mutation runs."""
    if args.area == "agent" and args.command in {"create", "update"}:
        from cli._dispatch_agents import _agent_changes_from_args

        return not {
            "permission_rules",
            "tool_access",
            "allowed_skills",
            "excluded_skills",
            "subagent_allow",
            "tools",
            "placement",
            "workspace",
            "root_project_id",
        }.isdisjoint(_agent_changes_from_args(args))
    if args.area == "project":
        if args.command in {"set-override", "clear-override"}:
            return args.field in {"permission_rules", "tool_access"}
        if args.command in {"set", "add"}:
            return any(
                getattr(args, key, None) is not None
                for key in (
                    "enabled_bundled_skills",
                    "enabled_global_skills",
                    "disabled_project_skills",
                    "sources",
                )
            ) or (args.command == "set" and args.cwd is not None)
    if args.area == "config":
        from cli.config_management import PERMISSION_RULES_DEFAULT_PATH
        from core.settings.paths import resolve_setting

        paths = []
        if args.command in {"set", "unset"}:
            paths.append(args.path)
        elif args.command == "patch":
            paths.extend(path for path, _ in args.set_values)
            paths.extend(args.unset_paths)
        return any(
            resolve_setting(path).canonical_path == PERMISSION_RULES_DEFAULT_PATH for path in paths
        )
    if args.area == "skill":
        return args.command in {"share", "unshare", "enable", "disable"}
    return False


def _file_inputs(args: argparse.Namespace) -> list[tuple[str, bool]]:
    """Host files supplied as command data, with the same base as the CLI process."""
    files = [
        path
        for key in (
            "content_file",
            "prompt_file",
            "settings_file",
            "wire_file",
            "entries_file",
            "package",
        )
        if (path := getattr(args, key, None)) is not None
    ]
    files.extend(getattr(args, "attachments", ()) or ())
    reads = [(str(Path(path).absolute()), False) for path in files]
    if args.area == "external" and args.command != "sources":
        reads.append((args.path, args.command != "import-memory"))
    if (
        args.area == "skill"
        and args.command == "install"
        and not args.source.lower().startswith(("http://", "https://"))
    ):
        reads.append((str(Path(args.source).expanduser().absolute()), True))
    return reads


def bind_target(args: argparse.Namespace, instance: ServerInstance) -> None:
    """Validate parsed selectors, leaving command operands and Extension payloads intact."""
    selected = getattr(args, "_target_arguments", {})
    url = parse_server_url(selected["url"]).origin if "url" in selected else instance.url
    if (
        selected.get("host", instance.host) != instance.host
        or selected.get("port", instance.port) != instance.port
        or url != instance.url
        or Path(selected.get("data_dir", instance.data_dir)).expanduser().resolve()
        != instance.data_dir
    ):
        raise ValueError(
            "This Tool runs commands on the current vBot instance. Omit the target options."
        )
    # Pin even commands whose normal default is the last-used Desktop or the installation.
    for name in ("host", "port", "data_dir"):
        if hasattr(args, name):
            setattr(
                args,
                name,
                str(instance.data_dir) if name == "data_dir" else getattr(instance, name),
            )
    if args.area == "external" and args.command != "sources":
        from core.external_history.history import source_root

        args.path = str(source_root(args.source, args.path))


def main() -> int:
    from cli._commands import run
    from cli.main import _configure_console_output

    _configure_console_output()
    data_dir = Path(os.environ["VBOT_DATA_DIR"])
    port = int(os.environ["VBOT_SERVER_PORT"])
    listen_host = os.environ.pop("VBOT_APP_CLI_LISTEN_HOST")
    local = read_local_access(data_dir, port)
    # A wildcard or localhost bind can resolve to several interfaces. The server's
    # access record names the loopback literal on which it actually accepts the CLI.
    host = local.loopback[0] if local and local.loopback else listen_host
    target = resolve_instance(host=host, port=port, data_dir=data_dir)
    from core.tools.vbot_cli_runner import REVIEW_ENV, REVIEW_EXIT, VBotAccessReviewRequiredError

    marker = os.environ.pop(REVIEW_ENV, None)

    def check_access(args: argparse.Namespace) -> None:
        rights = changes_agent_access(args)
        reads = _file_inputs(args)
        if rights or reads:
            raise VBotAccessReviewRequiredError(rights, reads)

    try:
        return run(app_instance=target, check_app_command=check_access if marker else None)
    except VBotAccessReviewRequiredError as review:
        print(marker)
        print(json.dumps({"rights": review.rights, "reads": review.reads}))
        return REVIEW_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
