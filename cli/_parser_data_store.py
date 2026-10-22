"""CLI grammar for local and server-backed data-store maintenance."""

from __future__ import annotations

import argparse

from cli._parser_common import (
    AREA_HELP,
    DATA_STORE_CONFIG_BACKUP_HELP,
    DATA_STORE_HELP,
    DATA_STORE_SECRETS_HELP,
    DATA_STORE_SNAPSHOT_HELP,
    _add_command_parser,
)


def _add_data_store_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    data_store_parser = subparsers.add_parser(
        "data-store",
        help=AREA_HELP["data-store"],
        description=AREA_HELP["data-store"],
    )
    commands = data_store_parser.add_subparsers(dest="command", required=True)

    status_parser = _add_command_parser(
        commands,
        "status",
        DATA_STORE_HELP["status"],
        example="data-store status",
    )
    status_parser.add_argument(
        "--verify-search",
        action="store_true",
        help=(
            "Check the Session search indexes against the stored entries. "
            "Omit for current status and the last coverage check; large histories take longer."
        ),
    )

    snapshot_parser = commands.add_parser(
        "snapshot",
        help=DATA_STORE_HELP["snapshot"],
        description=DATA_STORE_HELP["snapshot"],
    )
    snapshot_commands = snapshot_parser.add_subparsers(dest="snapshot_command", required=True)
    _add_command_parser(
        snapshot_commands,
        "list",
        DATA_STORE_SNAPSHOT_HELP["list"],
        example="data-store snapshot list",
    )
    create_parser = _add_command_parser(
        snapshot_commands,
        "create",
        DATA_STORE_SNAPSHOT_HELP["create"],
        example="data-store snapshot create --reason manual",
    )
    create_parser.add_argument(
        "--reason",
        choices=("manual", "rpc", "update", "recovery"),
        default="manual",
        help="Reason recorded in the snapshot manifest",
    )
    verify_parser = _add_command_parser(
        snapshot_commands,
        "verify",
        DATA_STORE_SNAPSHOT_HELP["verify"],
        example="data-store snapshot verify <snapshot-id>",
    )
    verify_parser.add_argument("snapshot_id", metavar="<snapshot-id>")
    restore_parser = _add_command_parser(
        snapshot_commands,
        "restore",
        DATA_STORE_SNAPSHOT_HELP["restore"],
        example="data-store snapshot restore <snapshot-id> --yes",
    )
    restore_parser.add_argument("snapshot_id", metavar="<snapshot-id>")
    restore_parser.add_argument(
        "--database",
        action="append",
        default=[],
        metavar="<name>",
        help="Restore only this database (repeatable); default: every database in the snapshot",
    )
    restore_parser.add_argument(
        "--documents",
        action="store_true",
        help=(
            "Restore the snapshot's JSON documents (settings, Agents, Channels, Projects, "
            "jobs, tokens) as one set; without --database, only the documents are restored"
        ),
    )
    restore_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Restore the complete snapshot: every database, the JSON documents, and move "
            "databases registered after the snapshot to quarantine"
        ),
    )
    restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore while the exact server target is stopped",
    )

    config_backup_parser = commands.add_parser(
        "config-backup",
        help=DATA_STORE_HELP["config-backup"],
        description=DATA_STORE_HELP["config-backup"],
    )
    config_backup_commands = config_backup_parser.add_subparsers(
        dest="config_backup_command", required=True
    )
    _add_command_parser(
        config_backup_commands,
        "list",
        DATA_STORE_CONFIG_BACKUP_HELP["list"],
        example="data-store config-backup list",
    )
    show_parser = _add_command_parser(
        config_backup_commands,
        "show",
        DATA_STORE_CONFIG_BACKUP_HELP["show"],
        example="data-store config-backup show <backup-id>",
    )
    show_parser.add_argument("backup_id", metavar="<backup-id>")
    config_restore_parser = _add_command_parser(
        config_backup_commands,
        "restore",
        DATA_STORE_CONFIG_BACKUP_HELP["restore"],
        example="data-store config-backup restore <backup-id> --file settings.json --yes",
    )
    config_restore_parser.add_argument("backup_id", metavar="<backup-id>")
    config_restore_parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="<path>",
        help="Restore this file, as `show` names it (repeatable)",
    )
    config_restore_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Restore every file of the backup; files created after it, files whose "
            "folder no longer exists or is a link, and secrets.json (restored only with "
            "--file) stay as they are. It also replaces an unfinished restore of "
            "another backup"
        ),
    )
    config_restore_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore while the exact server target is stopped",
    )

    incident_parser = commands.add_parser(
        "incident",
        help=DATA_STORE_HELP["incident"],
        description=DATA_STORE_HELP["incident"],
    )
    incident_commands = incident_parser.add_subparsers(dest="incident_command", required=True)
    acknowledge_parser = _add_command_parser(
        incident_commands,
        "acknowledge",
        "Acknowledge one durable database recovery incident",
        example="data-store incident acknowledge <incident-id>",
    )
    acknowledge_parser.add_argument("incident_id", metavar="<incident-id>")

    unregister_parser = _add_command_parser(
        commands,
        "unregister",
        DATA_STORE_HELP["unregister"],
        example="data-store unregister ext.<extension>.<name> --yes",
    )
    unregister_parser.add_argument(
        "name",
        metavar="<name>",
        help="Registered Extension database name, ext.<extension>.<name>",
    )
    unregister_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm moving the database files to quarantine and dropping the registration",
    )

    convert_parser = _add_command_parser(
        commands,
        "convert",
        DATA_STORE_HELP["convert"],
        example="data-store convert --yes",
    )
    convert_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the conversion while the exact server target is stopped",
    )

    secrets_parser = commands.add_parser(
        "secrets",
        help=DATA_STORE_HELP["secrets"],
        description=DATA_STORE_HELP["secrets"],
    )
    secrets_commands = secrets_parser.add_subparsers(dest="secrets_command", required=True)
    _add_command_parser(
        secrets_commands,
        "list",
        DATA_STORE_SECRETS_HELP["list"],
        example="data-store secrets list",
    )
    set_parser = _add_command_parser(
        secrets_commands,
        "set",
        DATA_STORE_SECRETS_HELP["set"],
        example="data-store secrets set GITHUB_TOKEN",
    )
    set_parser.add_argument(
        "name", metavar="<NAME>", help="Environment variable name of the credential"
    )
    unset_parser = _add_command_parser(
        secrets_commands,
        "unset",
        DATA_STORE_SECRETS_HELP["unset"],
        example="data-store secrets unset GITHUB_TOKEN",
    )
    unset_parser.add_argument(
        "name", metavar="<NAME>", help="Environment variable name of the credential"
    )
    _add_command_parser(
        secrets_commands,
        "status",
        DATA_STORE_SECRETS_HELP["status"],
        example="data-store secrets status",
    )
    secrets_convert_parser = _add_command_parser(
        secrets_commands,
        "convert",
        DATA_STORE_SECRETS_HELP["convert"],
        example="data-store secrets convert --yes",
    )
    secrets_convert_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the conversion while the exact server target is stopped",
    )
    reset_parser = _add_command_parser(
        secrets_commands,
        "reset",
        DATA_STORE_SECRETS_HELP["reset"],
        example="data-store secrets reset --yes",
    )
    reset_parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Confirm starting a new empty Secret Store; every credential and OAuth login "
            "must be entered again"
        ),
    )
