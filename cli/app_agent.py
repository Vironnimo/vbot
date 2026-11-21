"""Run the local CLI bound to the instance that owns a vbot Tool call."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

from cli._server_target import ServerInstance, parse_server_url, resolve_instance
from core.access.local_token import read_local_access


def changes_agent_access(args: argparse.Namespace, instance: ServerInstance) -> bool:
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
    if args.area == "archive" and args.command == "restore":
        from cli.rpc_client import rpc_call

        result = rpc_call(instance, "archive.show", {"entry_id": args.entry_id, "session_limit": 1})
        if not result.ok:
            raise ValueError(result.message)
        entry = result.data.get("entry")
        if not isinstance(entry, dict):
            raise ValueError("The server could not identify the archive entry.")
        if entry.get("kind") == "session":
            # Older servers omit the flag. A Session may restore its own rules or grants.
            return result.data.get("restores_access") is not False
        return entry.get("kind") not in {"owner_group", "files"}
    if args.area == "data-store":
        return _restore_changes_agent_access(args)
    if args.area == "permission-profiles":
        return args.command in {"save", "delete"}
    if args.area == "extensions":
        if args.command == "run":
            return "--help" not in args.rest[1:] and args.rest != ["operations"]
        return args.command in {"enable", "disable", "reload", "set"}
    if args.area == "config":
        from cli.config_management import PERMISSION_RULES_DEFAULT_PATH
        from core.settings.paths import resolve_setting

        paths = []
        if args.command in {"set", "unset"}:
            paths.append(args.path)
        elif args.command == "patch":
            paths.extend(path for path, _ in args.set_values)
            paths.extend(args.unset_paths)
        for path in paths:
            setting = resolve_setting(path)
            if setting.path.values[0] == "extensions" or setting.canonical_path in {
                PERMISSION_RULES_DEFAULT_PATH,
                "skills.directories",
                "providers.claude_agent.executable_path",
            }:
                return True
        return False
    if args.area == "skill":
        return args.command in {"share", "unshare", "enable", "disable"}
    return False


def _restore_changes_agent_access(args: argparse.Namespace) -> bool:
    if not getattr(args, "yes", False):
        return False
    if args.command == "snapshot" and args.snapshot_command == "restore":
        from core.sessions.schema import DATABASE_NAME

        if args.all and (args.documents or args.database):
            return False  # The restore refuses contradictory selectors.
        # By default every database is restored. Session records carry their own rules.
        return args.documents or not args.database or DATABASE_NAME in args.database
    if args.command == "config-backup" and args.config_backup_command == "restore":
        from core.json_documents import SNAPSHOT_DOCUMENTS, path_matches_patterns

        if args.all:
            return not args.file  # The restore refuses --all combined with --file.
        access_documents = (
            "settings",
            "agent",
            "agent_rename",
            "agent_retirements",
            "project",
            "skill_policy",
            "cron_jobs",
            "secrets",
        )
        patterns = tuple(SNAPSHOT_DOCUMENTS[kind] for kind in access_documents)
        return any(path_matches_patterns(path.replace("\\", "/"), patterns) for path in args.file)
    return False


def file_operands(
    args: argparse.Namespace, *, include_profile_file: bool = False
) -> Iterator[tuple[str, str | list[str], bool]]:
    """Parsed host file operands and whether they cover a subtree."""
    for name in (
        "content_file",
        "prompt_file",
        "settings_file",
        "wire_file",
        "entries_file",
        "package",
    ):
        if (value := getattr(args, name, None)) is not None:
            yield name, value, False
    if attachments := getattr(args, "attachments", None):
        yield "attachments", attachments, False
    if args.area == "external" and args.command != "sources":
        yield "path", args.path, args.command != "import-memory"
    if (
        args.area == "skill"
        and args.command == "install"
        and not args.source.lower().startswith(("http://", "https://"))
    ):
        yield "source", str(Path(args.source).expanduser()), True
    if (
        include_profile_file
        and args.area == "permission-profiles"
        and args.command == "save"
        and args.file is not None
    ):
        yield "file", args.file, False


def file_reads(operands: list[tuple[str, str | list[str], bool]]) -> list[tuple[str, bool]]:
    return [
        (str(Path(path).absolute()), subtree)
        for _, value, subtree in operands
        for path in (value if isinstance(value, list) else [value])
    ]


def checked_files(paths: list[str], reviewed: Mapping[str, str]) -> dict[str, str]:
    """Confirm the authorized targets still match before opening any file."""
    if set(paths) == reviewed.keys():
        try:
            if all(str(Path(path).resolve()) == resolved for path, resolved in reviewed.items()):
                return dict(reviewed)
        except OSError:
            pass
    raise ValueError(
        "No documents were read. The reviewed file paths could not be confirmed. "
        "Submit the command again."
    )


def bound_instance(data_dir: Path, listen_host: str, port: int) -> ServerInstance:
    local = read_local_access(data_dir, port)
    # A wildcard or localhost bind can resolve to several interfaces. Use the
    # loopback literal on which this server actually accepts its own CLI.
    host = local.loopback[0] if local and local.loopback else listen_host
    return resolve_instance(host=host, port=port, data_dir=data_dir)


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
        raise ValueError("This command runs on the current vBot instance. Omit the target options.")
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
    target = bound_instance(data_dir, listen_host, port)
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
        if marker:
            raise VBotAccessReviewRequiredError(True, [(path, False) for path in paths.values()])
        confirmed = checked_files(list(paths.values()), reviewed_files)
        return {key: confirmed[path] for key, path in paths.items()}

    def check_access(args: argparse.Namespace) -> None:
        if args.area == "extensions" and args.command == "run":
            # The live operation schema discovers file operands. Review them and
            # the operation together before opening a file or invoking its handler.
            return
        rights = changes_agent_access(args, target)
        reads = file_reads(list(file_operands(args)))
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
