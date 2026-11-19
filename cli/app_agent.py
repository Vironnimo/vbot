"""Run the local CLI bound to the instance that owns a vbot Tool call."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cli._server_target import ServerInstance, parse_server_url, resolve_instance
from core.access.local_token import read_local_access


def changes_agent_access(args: argparse.Namespace) -> bool:
    """Classify a parsed command before any command handler or mutation runs."""
    if args.area == "agent" and args.command in {"create", "update"}:
        from cli._dispatch_agents import _agent_changes_from_args

        return not {
            "permission_profile",
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
            return args.field in {"permission_profile", "permission_rules", "tool_access"}
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
    if args.area == "permission-profiles":
        return args.command in {"save", "delete"}
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
    from cli._input import document_file_review
    from cli._profile_review import bound_profile_review, prepare_profile_review
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
    from core.tools.vbot_cli_runner import (
        REVIEW_ENV,
        REVIEW_EXIT,
        REVIEWED_INPUTS_ENV,
        VBotAccessReviewRequiredError,
    )

    marker = os.environ.pop(REVIEW_ENV, None)
    reviewed_files: dict[str, str] = {}
    reviewed_profile = {}
    resumed = os.environ.pop(REVIEWED_INPUTS_ENV, None) == "1"
    if resumed:
        frame = json.loads(getattr(sys.stdin, "buffer", sys.stdin).readline())
        reviewed_files = frame["files"]
        profile = frame["profile"]
        if profile is not None and (not isinstance(profile, dict) or not profile):
            raise ValueError("The approved permission profile is missing.")
        reviewed_profile = profile or {}

    def review_document_files(sources: dict[str, str]) -> dict[str, str]:
        paths = {key: str(Path(source).absolute()) for key, source in sources.items()}
        if marker and paths:
            raise VBotAccessReviewRequiredError(False, [(path, False) for path in paths.values()])
        if set(paths.values()) == reviewed_files.keys():
            try:
                if all(
                    str(Path(path).resolve()) == resolved
                    for path, resolved in reviewed_files.items()
                ):
                    return {key: reviewed_files[path] for key, path in paths.items()}
            except OSError:
                pass  # An unavailable target cannot be confirmed either.
        raise ValueError(
            "No documents were read. The reviewed file paths could not be confirmed. "
            "Call the Tool again."
        )

    def check_access(args: argparse.Namespace) -> None:
        rights = changes_agent_access(args)
        reads = _file_inputs(args)
        if rights or reads:
            profile = prepare_profile_review(target, args) if rights else None
            raise VBotAccessReviewRequiredError(rights, reads, profile=profile)

    try:
        with (
            bound_profile_review.set(reviewed_profile),
            document_file_review.set(review_document_files if marker or resumed else None),
        ):
            return run(app_instance=target, check_app_command=check_access if marker else None)
    except VBotAccessReviewRequiredError as review:
        print(marker)
        payload = {"rights": review.rights, "reads": review.reads}
        if review.profile is not None:
            payload["profile"] = review.profile
        print(json.dumps(payload))
        return REVIEW_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
