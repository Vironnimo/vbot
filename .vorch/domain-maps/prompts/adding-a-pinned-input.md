# Adding a Pinned Prompt Input

Task-gated depth for the prompt-epoch pinning contract in `prompts.md` (Assembly Pipeline): every file-backed System Prompt input is pinned per prompt epoch in Session metadata and re-read from disk only when a successful Compaction starts a new epoch. The current pins are the Skill catalog, Working Project Context, SOUL block, and pinned-memory text.

Adding a new pinned input touches five places:

1. `core/prompts/pinned_context.py`: a `PINNED_*_META_KEY` constant plus a wrapper built on `_pinned_epoch_text(dependencies, meta_key, agent_id, session_id, project_id, render)` - it returns stored text or renders and stores it through `sessions.mutate_metadata`. When the input reads workspace files, collect `read_paths` inside the render closure and pass them to `stamp_prompt_files_read` so auto-injected files count as read-before-write (follow `pinned_soul_context`).
2. `core/chat/chat.py::_create_run_execution_context`: resolve the wrapper and store the value on `_RunExecutionContext`, next to the existing pins.
3. `core/chat/chat.py::_CompactionPromptRefresh` and `core/chat/compaction_host.py`: a successful Compaction replaces every pin wholesale when the new epoch starts - add the new meta key to that replacement.
4. The request-build call into `build_system_prompt(...)`: thread the value through so the System Prompt build receives it (grep the existing pins' path from `_RunExecutionContext`).
5. Prompts domain: the block renderer/producer returns the pinned text verbatim when present, and a public `render_*` method serves both the live render and the snapshot closure.

Adding a keyword parameter to `build_system_prompt` also breaks the duplicated prompt-manager test doubles - see `prompts.md` -> Constraints & Gotchas.