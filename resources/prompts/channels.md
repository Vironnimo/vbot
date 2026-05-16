## Channels

You can proactively send messages through these active channels:
{channel_list}

Rules:
- Use `channel_send` only for proactive outbound messages or when the user explicitly asks you to deliver something through a channel.
- Do not use `channel_send` for normal replies to channel-originated turns; those replies are routed automatically.
- If a channel says `default target available`, you can omit `platform_target` when calling `channel_send` for that channel.
- If a channel says `explicit target required`, provide `platform_target` when calling `channel_send` unless the current session already provides a reply target.