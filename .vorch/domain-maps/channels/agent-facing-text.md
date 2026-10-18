# Channels Agent-facing text

Why each sentence of the Agent-facing text the Channels domain owns exists: the Run note of a Run-button tap or checklist submission (`_format_interaction_note` in `_conversation_content.py`). Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text.

The Run note of a reserved Run-button tap (`_format_interaction_note` in `_conversation_content.py`) reaches the Agent as the internal prompt of that Run. Button labels/data and checklist labels/ids are JSON-quoted with escaped angle brackets; quotes, line breaks and tags stay inside the data record. Group taps retain the ingress sender's attributed role. Coverage: `tests/core/channels/test_engine_interactions.py`.

| Text | Reason |
|---|---|
| `A button on your message was tapped.` | Identifies the event and the Agent's sent message without treating every other button as a requested action. |
| `Tapped button:` with a JSON record containing `label` and `data` | Names the selected action while keeping message-authored values separate from the kernel's instructions. |
| `Current buttons on the message (top to bottom, left to right):` | Preserves keyboard order and state for interpreting the tapped action; each label/data pair remains a quoted record. |
| `Carry out the tapped action, then confirm in this chat.` | Restricts a generic tap to the chosen action, including an extra button beside a checklist; the other buttons and checklist state are context, not additional requests. |
| `The checklist you sent to this chat was submitted.` | Replaces the generic tap header for the done button, so the Agent knows the checklist it sent earlier is meant, not a new request. |
| `Checklist items changed since you sent it:` with `- checked: {"label": "<label>", "id": "<id>"}` / `- unchecked: ...` lines, then `Unchanged items: <n>.` | Only the delta, by the Agent's own ids: applying full keyboard state overwrote changes made elsewhere since sending (the former Gusto Skill re-asserted every item). JSON quoting preserves each value without letting it alter the note's structure. The count lets the Agent confirm without listing everything. |
| `No checklist item changed since you sent it (<n> items).` | Says that nothing needs saving, so the Agent does not rewrite every item. |
| `Act on these changes, then confirm in this chat.` / `Confirm in this chat.` | On checklist submission, apply only its changes and confirm in the chat; without it Agents save silently or ask whether to save. |
| `Other buttons on the message:` | Keeps the Agent's own extra buttons visible as quoted label/data records when the checklist hides its item and done buttons. |
