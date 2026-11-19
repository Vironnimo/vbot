"""Execute a restricted Run's shell invocation through the ordinary CLI dispatch."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from cli._commands import run
from cli._input import _read_stdin_utf8, document_file_review
from cli._profile_review import bound_profile_review, prepare_profile_review
from cli.app_agent import (
    bound_instance,
    changes_agent_access,
    checked_files,
    file_operands,
    file_reads,
)
from cli.rpc_client import rpc_call


def run_shell_cli(argv: list[str], scope: str) -> int:
    """Review actual CLI operands after shell expansion, before a command takes effect."""
    try:
        fields = json.loads(scope)
        if (
            not isinstance(fields, dict)
            or not all(
                isinstance(fields.get(name), str) and fields[name]
                for name in ("token", "host", "data_dir", "agent_id", "session_id")
            )
            or type(fields.get("port")) is not int
            or not 0 < fields["port"] <= 65535
            or not all(
                isinstance(fields.get(key), str | None) for key in ("project_id", "update_handoff")
            )
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "This shell command's vBot CLI access is invalid. Submit it again."
        ) from None
    target = bound_instance(Path(fields["data_dir"]), fields["host"], fields["port"])
    # Command handlers that use the Run identity or hand off an update see the
    # originating Session. The restricted shell itself needs none of these values.
    os.environ.update(
        VBOT_DATA_DIR=str(target.data_dir),
        VBOT_SERVER_PORT=str(target.port),
        VBOT_RUN_AGENT_ID=fields["agent_id"],
        VBOT_RUN_SESSION_ID=fields["session_id"],
    )
    os.environ.pop("VBOT_SERVER_URL", None)
    for name, key in (
        ("VBOT_RUN_PROJECT_ID", "project_id"),
        ("VBOT_UPDATE_HANDOFF", "update_handoff"),
    ):
        if value := fields.get(key):
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    approved_profile: dict[str, Any] = {}

    def review(
        reads: list[tuple[str, bool]],
        *,
        rights: bool = False,
        profile: dict[str, Any] | None = None,
        stdin: str | None = None,
    ) -> dict[str, str]:
        params = {
            "token": fields["token"],
            "argv": argv,
            "rights": rights,
            "reads": reads,
            "profile": profile,
        }
        if stdin is not None:
            params["stdin"] = stdin
        response = rpc_call(target, "permissions.review_cli", params)
        if not response.ok:
            raise ValueError(response.message)
        files = response.data.get("files")
        if not isinstance(files, dict) or not all(
            isinstance(path, str) and isinstance(resolved, str) for path, resolved in files.items()
        ):
            raise ValueError("The server could not confirm CLI access. Submit the command again.")
        return checked_files([path for path, _ in reads], files)

    def check_access(args: argparse.Namespace) -> None:
        # Classify with the production parser. Authorize files before the CLI can
        # read their contents, including a profile file needed for the rights review.
        operands = list(file_operands(args, include_profile_file=True))
        if (
            args.area == "skill"
            and args.command == "install"
            and args.source.lower().startswith(("http://", "https://"))
        ):
            args._shell_cli_token = fields["token"]
        files = review(file_reads(operands))
        for name, value, _ in operands:
            if isinstance(value, list):
                setattr(args, name, [files[str(Path(path).absolute())] for path in value])
            else:
                setattr(args, name, files[str(Path(value).absolute())])
        if changes_agent_access(args, target):
            profile = prepare_profile_review(target, args, allow_profile_file=True)
            stdin = None
            if args.area == "config" and args.command == "set" and args.stdin:
                stdin = _read_stdin_utf8()
                # Keep the checked input in this process through the user's decision.
                args._reviewed_stdin = stdin
            review([], rights=True, profile=profile, stdin=stdin)
            if profile is not None:
                approved_profile.update(profile)

    def review_document_files(sources: dict[str, str]) -> dict[str, str]:
        paths = {key: str(Path(source).absolute()) for key, source in sources.items()}
        if not paths:
            return {}
        files = review([(path, False) for path in paths.values()])
        return {key: files[path] for key, path in paths.items()}

    with (
        bound_profile_review.set(approved_profile),
        document_file_review.set(review_document_files),
    ):
        return run(argv, app_instance=target, check_app_command=check_access)
