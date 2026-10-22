"""Closed value sets of a Channel configuration.

A leaf without imports: accessors offer these as choices (the CLI parser) without
loading the configuration validators, Settings or the database kernel.
"""

ALLOWED_CHANNEL_DM_SCOPES = frozenset(
    ("per_conversation", "main", "per_peer", "per_account_channel_peer")
)


ALLOWED_CHANNEL_RESPONSE_MODES = frozenset(("mention", "all"))


ALLOWED_CHANNEL_PLATFORMS = frozenset(("discord", "telegram", "slack", "mattermost", "whatsapp"))
