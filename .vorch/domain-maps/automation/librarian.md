# Librarian

Read when changing or debugging Librarian passes: the schedule, aging, the consolidation Run, the state document, the `librarian.*` RPCs and CLI, or the Librarian's Agent-facing texts. The boundary and the invariants are in `../automation.md` -> Librarian; the WebUI surfaces in `../webui/settings.md`; the learning evaluation of passes in `learning-evaluation.md`.

## Owner and entry points

- `core/automation/librarian.py` (`LibrarianService`, Runtime `librarian`). Runtime builds it in `_bootstrap.py::_librarian_service` over the Skill authoring service, `agent_skills_dir`, a `StatisticsService` for Skill use (`skill_usage_async`), `AutomationReferences.agent_triggered_skill_names` and the Skills change notification.
- Blocking state, history and package work runs on the `librarian` `BoundedWorkerPool` (2 workers); Skill writes and history reads hold `AgentStore.lifecycle_guard`.
- Tests: `tests/core/automation/test_librarian.py` (aging, consolidation, state, status, refusals, schedule), `tests/server/rpc/test_librarian_methods.py`, `tests/cli/test_cli_librarian.py`, `tests/core/prompts/test_briefs.py` (brief and candidate format), `tests/core/automation/test_references.py` (triggered names), `tests/core/automation/test_reflection.py` (the denial answer and limit shared with reviews).

## Schedule

- `start()` (normal startup only, after the archive retention sweep; `runtime.md`) runs a check `FIRST_CHECK_DELAY_SECONDS` (300) after start, then every `CHECK_INTERVAL_SECONDS` (3600). `_wait` is the test seam. A failed check logs WARNING and the next one retries.
- A check reads `librarian.*` live and does nothing while `enabled` is false. For each Identity Agent (`agents.list()`), one at a time: skip one with a pass running; skip an unreadable state document (WARNING); an Agent with neither `last_pass` nor `first_seen_at` only gets `first_seen_at` = now and is due one interval later (design decision: a new or upgraded installation gets a full interval before its first pass); otherwise skip until `interval_days` after `last_pass.finished_at`, else after `first_seen_at`. A due Agent runs only when it resolves, is eligible and has no active or queued Run (`ChatRunManager.has_activity_for_agent`), checked after the last await so nothing starts in between.
- Eligible: an Identity Agent with a workspace whose effective Tool access (Project ceilings and Tool Access Policy, `callable_review_dimensions` in `reflection.py`) can call `skill` and `skill_manage`.
- `run(agent_id)` starts a manual pass at once, regardless of `interval_days` and `enabled`, as a task, and returns `status`. Refusals, checked in this order before anything changes: shutting down -> `LibrarianBusyError`; not an Identity Agent -> `AgentNotFoundError`; unreadable state -> `LibrarianStateError`; not eligible -> `LibrarianUnavailableError`; a pass of the Agent running, or an active or queued Run -> `LibrarianBusyError`.
- One pass per Agent at a time (`_active`); passes start and end announce through `status_changed` (Runtime publishes `resource_changed(skills)`, `server/events-and-reconnect.md`).

## A pass

1. **Aging** (no Model, `_age_library`): archives with `SkillAuthoringService.delete(..., reason="inactive")` as writer `SkillWriter(actor="librarian", run_kind="librarian")` (no Session or Run id) every Skill of the Agent's private home that is unpinned, has origin `reflection` or `librarian` (`AGED_ORIGINS`), is not triggered by a live automation of the Agent, and whose newest activity is older than `archive_after_days`. Activity is creation, the newest file change by an attended writer (`human`, `agent` or `external`; changes by `reflection` and `librarian` writers do not keep a Skill) and the Agent's last use (`SkillUse.last_activated`, Statistics; background Sessions are not use). A time that cannot be parsed never ages a Skill. One Skill that cannot move logs WARNING and the rest continue. Archiving anything invalidates the Agent's Skill caches and publishes `skills` (`skills_changed`) and logs one INFO line with the count.
2. **Consolidation** (`_consolidate`), only when `consolidate` is on (else outcome `disabled`), at least two candidates exist after aging (else `too_few`) and their fingerprint differs from `consolidation_fingerprint` (else `unchanged`). Candidates (`librarian_candidates`): loadable, unpinned Skills of the home with origin `agent`, `reflection` or `librarian` (`BACKGROUND_WRITABLE_ORIGINS`), each with description, origin, created and last-changed dates, last use and use count, SKILL.md length, support files and whether a live automation triggers it. The fingerprint is a SHA-256 over the sorted (name, latest revision id) pairs of the candidates.
   - The brief (`librarian_brief`, `core/prompts/briefs.py`) is rendered on the worker, then a new Session is created with `run_kind=RunKind.LIBRARIAN` in its first write (hidden from Session lists and Recall from the start, `sessions.md`), and one internal Run starts on the streaming loop: `internal=True`, `tool_restriction=LIBRARIAN_TOOL_RESTRICTION` (`skill`, `skill_manage`), `tool_denial_resolver=librarian_tool_denial_resolver()`, `max_tool_iterations=LIBRARIAN_TOOL_ITERATION_LIMIT` (60, also the brief's stated limit), `run_kind=RunKind.LIBRARIAN`, `contributes_to_agent_activity=False`. The Agent's own Model, System Prompt and Tool definitions apply; the Session is new, not a fork of a conversation, so its System Prompt lists the current Skill catalog.
   - Inside the Run, `skill_manage` writes as actor `librarian` with the Session, Run id and Run kind (`skills.md` -> Background Writer), so every Background Writer guard applies: no shared, pinned, human-origin or unknown-origin Skill, no revert, and `delete` needs `absorbed_into`.
   - A Run that cannot start deletes the new Session and ends as `failed` with the error; a Run that fails ends as `failed` with its Session and Run ids. Only `ran` records a new fingerprint (computed from the candidates as the Run left them); `failed` keeps the old one, so the next pass retries.
3. The pass counts the consolidation Run's revisions (`created` = Skills with a `create`, `merged` = Skills archived with reason `absorbed`, `changed` = the other Skills it wrote), writes the state and logs one INFO line (Agent, trigger, counts, outcome). A pass that raises logs WARNING; its state stays as it was.

## State document

`agents/<id>/librarian.json`, durable kind `librarian_state` (`settings.md` -> JSON documents; validated by `doctor config`, in data snapshots), format version 1, `LIBRARIAN_STATE_FORMAT`:

- `first_seen_at` (when a scheduled check first saw the Agent), `last_pass`, `consolidation_fingerprint`; unknown fields warn.
- `last_pass`: `started_at`, `finished_at` (timestamps), `trigger` `schedule`|`manual`, `archived`, `candidates`, `created`, `changed`, `merged` (non-negative integers), `archived_revisions` (the aging revision ids), `consolidation` `ran`|`unchanged`|`too_few`|`disabled`|`failed`, optional `session_id`, `run_id`, `error`.
- A missing document is an empty state. An invalid one raises `LibrarianStateError`: `status` and `run` refuse, the schedule skips the Agent; nothing overwrites it until it is fixed. Writes happen only while the Agent exists (under the lifecycle guard), so a removed Agent keeps none; the file lives in the Agent tree `agents/<id>/`, which an Agent rename and an Agent archive move as a whole.

## Status, report and undo

- `status(agent_id)` -> `{agent_id, settings, available, running, running_since, last_pass, next_due_at, changes}`. `next_due_at` is `null` while `enabled` is false; for an Agent no check has seen yet it estimates one interval from now. `changes` are the home's revisions of the last pass, newest first: the ids in `archived_revisions` plus the `librarian` revisions with the pass's `run_id` (`SkillRevision.to_dict`, `skills/history.md`).
- The whole pass is taken back with `skill.revert` over those revision ids (all or none; the CLI prints the command, the WebUI's Revert together sends it). `learning.changes` / `learning.undo` (`../automation.md` -> Reflection) also serve the consolidation Run by its `run_id`, but not the aging archives, which carry no Run id.

## Surfaces

- RPC `server/rpc/librarian_methods.py`: `librarian.status {agent_id}` -> the status; `librarian.run {agent_id}` -> the status after the start. `LibrarianBusyError` -> `agent_busy`, `LibrarianUnavailableError` -> `invalid_request`, others through `_map_expected_error` (`agent_not_found`, `domain_error`). `settings.get` carries the `librarian` section; `settings.update` accepts partial `librarian` updates.
- CLI `cli/librarian_management.py`: `vbot librarian status <agent-id>` prints settings, availability, a running pass, the next scheduled pass, the last pass (counts, consolidation outcome, Session and Run) and its revisions with the `vbot skill revert ... --scope agent:<id>` command; `vbot librarian run <agent-id>`.
- WebUI: Settings -> Memory -> Skill maintenance (`SettingsLibrarianPanel`) and the Librarian section of an Agent's Skills page (`LibrarianSection`), both in `../webui/settings.md`. Agents learn the commands from the bundled `vbot-cli` Skill (`references/skills.md`, `../skills.md` -> Agent-facing text).

## Shutdown

`_shutdown.py` step `librarian` after Reflection and before the Runs: `stop()` stops checking and cancels passes in progress; `aclose()` also waits for them. A cancelled pass writes no state, so it is due again after restart; its consolidation Run ends when `ChatRunManager` closes.

## Agent-facing text

| Text | Reason |
|---|---|
| `You are the Librarian for this Agent's own Skills. This is a background maintenance pass: the user does not see your replies, and the user can see and undo every change you make. Only skill and skill_manage work, at most {tool_call_limit} calls in total.` (`resources/prompts/librarian.md`, limit from `LIBRARIAN_TOOL_ITERATION_LIMIT`) | The Run has no conversation to answer; reversibility counters timidity, and the stated limit matches the enforced one. |
| `Your goal is a small library of Skills that each cover one recognizable kind of task. ...` | Hermes' class-level umbrella goal, with the reason (an Agent picks a Skill by its description). |
| `The candidates below are the Skills you can change. Every other Skill is read-only; ... never move its content or name it in absorbed_into.` + `{generated:candidates}` | Matches the Background Writer guards; the candidate list is the only writable set. |
| Steps 1-7 (`Group candidates ... Choose the umbrella ... Read every Skill ... Merge by distilling ... Keep the umbrella's description accurate ... Delete each merged Skill with skill_manage action delete and absorbed_into ... Also fix a single Skill that is hard to use ...`) | Hermes' consolidation procedure without its archive quota, description window and library-specific examples; "Distinct triggers alone are not a reason" stops weak Models from keeping every narrow Skill; copying a SKILL.md into `references/` is named as not a merge. |
| `Rules: Never delete a Skill without absorbed_into; ... Usage counts are not evidence of value ... Read a file with skill before changing it ... Never invent content ... When the library is already in good shape, change nothing.` | Aging owns retirement; usage alone must not drive merges; read-before-write and no invention as in the review briefs. |
| `Finish with a short summary for the user ... When you changed nothing, reply "Nothing to change."` | The closing reply is the Run's summary in its Session; the fixed phrase marks a no-op (eval case `librarian_healthy_library`). |
| Candidate lines `- <name>`, `Description:`, `Created by:` (`you, during a conversation` / `a background review of a conversation` / `an earlier Librarian pass`), `Created:`, `Last changed:`, `Last used: never` or `<date> (N uses)`, `SKILL.md: N characters`, `Support files:`, `Used by a schedule: yes|no` (`_candidate_text`, `briefs.py`) | The facts the brief's steps refer to, in the Agent's terms; dates only, no ids. `{max_chars}` in step 7 is `LIBRARIAN_SKILL_MD_MAX_CHARS` (12000). |
| `Nothing was run: {tool} is not available in this maintenance pass. This pass can call only {tools}. Do not retry this call.` (`LIBRARIAN_TOOL_DENIAL_MESSAGE`) | The review denial pattern for the pass: says nothing ran, names the callable Tools and stops retries. |

## Known gaps

- Aging keeps a Skill that a live automation triggers, but consolidation can still absorb it into another Skill: the brief lists `Used by a schedule: yes`, no guard stops the delete, and the automation's `/name` or `$name` trigger then no longer finds the Skill.
- Absorbed redirects follow one hop (`tools/skill.md`), so a Skill absorbed into an umbrella that a later pass absorbs again leads to an archived name.
- `absorbed_into` is unadvertised in the `skill_manage` schema; the brief names it, and the refusal explains it when a Model omits it.
- Aging changes the candidate set, so a pass after aging archived a candidate runs consolidation even when nothing else changed.
- The evaluation covers consolidation only; aging is checked by unit tests.
