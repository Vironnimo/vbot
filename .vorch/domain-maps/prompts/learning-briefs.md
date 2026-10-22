# Learning Briefs

How `core/prompts/briefs.py` composes the one-shot learning-Run briefs (Reflection, `/learn`, Librarian) from shared fragments. Briefs are a Run's input, not System Prompt blocks.

`core/prompts/briefs.py` owns the learning-Run briefs: `reflection_brief(fragments, scope, *, focus=None)` for the Reflection scopes `memory`/`skill`/`combined`, `learn_brief(fragments, request)` for `/learn` and `librarian_brief(fragments, candidates, *, agent_id, agent_name)` for a Librarian consolidation Run, one public function per brief kind, used by Reflection and the Librarian (`automation.md`), `/learn` (`chat/commands.md`) and the provider probe.

- The Librarian brief is the one fragment `librarian.md`: `{generated:agent}` takes the curated Agent (`<name> (id <id>)`, or the id when the name equals it; an override without the marker gets it named in a first paragraph), `{max_chars}` `SKILL_MD_ADVISED_LENGTH`, and `{generated:candidates}` the rendered `LibrarianCandidate` list (appended as a last paragraph when an override lacks the marker); it states no call limit, because the Run has Chat's normal limit; its wording rows are in `automation/librarian.md` -> Agent-facing text.
- A brief is a fixed recipe of shared fragments: fragments of one group continue a paragraph (joined by one space), groups are paragraphs (joined by one blank line), so guidance shared between briefs lives in exactly one fragment.
- `fragments` is any `read_prompt_fragment` reader (Storage in production), so each fragment resolves and overrides on its own; every name is in `BRIEF_FRAGMENT_NAMES`, which Storage must allowlist (`storage.md`).
- Assembly reads files: callers run it on a worker, never on the Event Loop.
- Fragments name Tools literally; `test_reflection.py` checks that each production review brief names exactly its scope's Tools. Coverage: `tests/core/prompts/test_briefs.py`.
- The reasons for the Reflection and `/learn` fragment wording are rows of `prompts/agent-facing-text.md`.
