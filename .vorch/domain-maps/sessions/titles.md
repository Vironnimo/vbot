# Session titles

Task-gated detail for `sessions.md`: immediate local titles, background Model titles for Sessions and Extension groups, the title parser and title callbacks. Source: `core/sessions/titles.py`. Tests: `tests/core/sessions/test_titles.py`. Reasons for the title System Prompt's sentences: `sessions/agent-facing-text.md`.

## Generation

Title generation shares Chat's public `resolve_model_target` for explicit Connection suffixes and per-Model allowed Connection selection.

Session and group titles may be read outside the originating Session. vBot anonymizes known Secret values and stored references in source text, attachment metadata and voice transcriptions before shortening them for the local title or Model request. The Model response receives the same projection before title validation and persistence.

- Session and group title generation resolves Provider credentials and constructs its Adapter on the `session-title` worker, retaining Adapter cleanup when setup is cancelled. It records each exposed Adapter request through the injected `UsageRecorder`, including the explicit effort retry, before title validation or persistence. Invalid titles and later Session/group deletion keep their known consumption; accounting stores no generated title response content.
- Automatic Session titles read the sole current User entry's content and sequence from one database snapshot, then guard both its sequence and the Session generation in the metadata write. An edited-away source cannot publish a late title, while later appends do not invalidate that title request. A history edit that replaces the first current User entry clears the generated title (`sessions/lineage.md` -> History edit).
- Title generation resolves the Adapter's `request_context_kwargs` from Project/Agent/Session once and forwards it on both attempts. This supplies required Provider routing headers without coupling Sessions to wire fields (`test_reasoning_mandatory_endpoint_retries_with_default_effort`); a synthetic Muse Spark 1.3 title completed through this path on 2026-09-07, before title requests streamed. Both attempts stream through `stream_model_response` (`core/chat/streaming.py`; a completed `send` only when the Provider declares it cannot stream) with `DebugContext.streaming` true; the utility retries at Provider-default effort only when the Model explicitly requires Reasoning.

## Parser and validation

`titles.py` owns immediate local titles and optional background Model titles. A Session with a manual title or an Ephemeral Session (GLOSSARY -> Ephemeral Session) gets the local title but no Model title request (`_prepare_title`).

The Model is asked for one `[title=...]` block. The parser reads only the answer content after shared response finalization has split off inline Reasoning (`chat.md` -> `split_inline_reasoning`).

- The first non-empty `[title=...]` block wins, even amid commentary. If there is none, the first non-empty block whose key has one extra letter (e.g. `Ntitle`) or two adjacent letters swapped (e.g. `tilte`, `Titel`) wins. Case and spacing are flexible. Both use the same value parser: a closed block can span lines, and a single-line block can omit its final bracket at the end of the response (observed with `glm-5.3-flash`). A colon can replace the equals sign when the block occupies the whole answer.
- The plain-text fallback accepts one title, optionally fenced or labeled `Title:` or `Titel:` (including bold labels). Bracketed qualifiers, internal quotes and attached hashes such as `C#` can be title content. Every candidate goes through the same empty-title, length and known meta-phrase checks.

Invalid generated content keeps the local title and emits a content-free warning with the rejection reason and no traceback or extra Model request; unexpected failures retain their traceback. A successful generated Session or group title logs only at DEBUG. A generated Session title is stored only on the Session generation it was requested for (`set_auto_title(..., expected_generation_id=...)`, which raises `SessionNotFoundError` otherwise); one that arrives after the Session was archived or deleted is dropped, so a new Session at the same address keeps its own title (`test_a_late_title_only_lands_while_its_first_user_entry_is_current`). Regression coverage lives in `tests/core/sessions/test_titles.py`. `generate_group_title` reuses the same prompt, parser and request path for Extension group titles (Model choice: `extensions/host.md` -> Group titles).

## Callbacks

Title-change callbacks receive the Session address; completion-read callbacks receive the address plus the acknowledged Run id. Callback failures after committed title or completion mutations are logged (ERROR with traceback on `vbot.sessions`, naming the event and Session address) and isolated so callers never receive a false failure for state that already persisted.
