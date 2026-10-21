# Chat Agent-facing Text

Why each sentence of the Compaction checkpoint guidance the Chat domain owns in `core/chat/messages.py` exists; the reasons for Chat's steering, interruption, reply-surface and command texts stay in the `## Agent-facing text` sections of `chat/run-execution.md`, `chat/request-building.md` and `chat/commands.md`, and those of the widget value note (`core/chat/widget_states.py`) in `tools/show_widget.md` -> Agent-facing text. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text.

| Text | Reason |
|---|---|
| `This is Compaction checkpoint {ordinal}. Some earlier original messages are no longer directly present in active Context; the Session still stores them unchanged.` (`core/chat/messages.py::COMPACTION_CHECKPOINT_GUIDANCE`, appended once to each new checkpoint's summary note) | Tells the Agent that the summary is lossy and that the originals still exist, so it neither treats the summary as the complete record nor assumes earlier wording is lost. It names no retrieval Tool: the stored note outlives any Tool or Skill availability, and the `vbot-docs` Skill's description advertises transcript retrieval where it is available. Replaced the `history` Tool instruction (2026-10-04). |
