"""1Password bundled Extension: a Secret Source for the items of 1Password vaults.

The source reads items through the 1Password CLI (``op``) as a service account
of the user's existing 1Password account. Its token is a secret setting, stored
encrypted in the Secret Store, or it is given at unlock and kept in memory only.
Settings are read live on every call.
"""

from __future__ import annotations

from typing import Any

from ._source import TOKEN_ENV_KEY, OnePasswordSource


def register(api: Any) -> None:
    api.register_settings(
        [
            {
                "key": "token",
                "type": "secret",
                "label": "Service account token",
                "description": (
                    "Token of a 1Password service account with read access to the vaults "
                    "vBot may use. Leave empty to enter it each time you unlock 1Password."
                ),
                "env_key": TOKEN_ENV_KEY,
            },
            {
                "key": "cli_path",
                "type": "text",
                "label": "op path",
                "description": "Leave empty to use op from PATH or its installation folder.",
            },
        ]
    )
    api.register_secret_source(
        OnePasswordSource(api.get_config, api.resolve_credential, logger=api.logger)
    )
