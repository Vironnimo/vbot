"""KeePassXC bundled Extension: a Secret Source for the entries of a local database.

The source reads entries through keepassxc-cli, which the KeePassXC installation
ships. Settings name the database, its optional key file and, when it is not on
PATH or in the standard installation folder, keepassxc-cli itself; they are read
live on every call. The master password is given at unlock and kept in memory only.
"""

from __future__ import annotations

from typing import Any

from ._source import KeePassXCSource


def register(api: Any) -> None:
    api.register_settings(
        [
            {
                "key": "database",
                "type": "text",
                "label": "Database file",
                "description": "Full path of the KeePassXC database (.kdbx).",
            },
            {
                "key": "key_file",
                "type": "text",
                "label": "Key file",
                "description": "Full path of the database's key file. Leave empty if it has none.",
            },
            {
                "key": "cli_path",
                "type": "text",
                "label": "keepassxc-cli path",
                "description": (
                    "Leave empty to use keepassxc-cli from PATH or the KeePassXC "
                    "installation folder."
                ),
            },
        ]
    )
    api.register_secret_source(KeePassXCSource(api.get_config, logger=api.logger))
