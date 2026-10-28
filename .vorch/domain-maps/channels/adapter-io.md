# Shared adapter I/O

Helpers every platform adapter uses instead of its own logic, so platforms cannot drift.

- Outbound text splitting is shared: `split_message` in `_message_chunks.py` cuts at paragraph, then line, then whitespace boundaries (hard cuts only inside a single overlong token, between grapheme clusters where possible). For Markdown-rendering platforms it closes a code fence open at a cut and reopens it with the same opener line in the next chunk, counting added markers toward the limit. Adapters supply only their limit and length measure (values in the platform maps); never slice outbound text by fixed offsets.
- Inbound attachment classification is shared: `content_blocks_for_attachment(record)` in `adapter.py` is the single rule mapping sniffed media type to content blocks (image/audio/video -> `MediaBlock`, everything else including text -> `FileBlock`). Adapters extend their block lists with the result; classification is by sniffed type, never by delivery channel (a Telegram MP3 sent as document is media).
