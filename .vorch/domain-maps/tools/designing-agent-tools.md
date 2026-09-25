# Designing Agent-Facing Tools

Design rules for everything an Agent experiences when it uses a Tool: the definition (name, description, parameters), any System Prompt guidance block, the interpretation of its arguments, its results, and its errors. Applies to built-in and Extension Tools alike. Per-Tool behavior contracts live in the sibling maps under `tools/`; the review and redesign procedure lives in `.vorch/workflows/tool-review-workflow.md`.

## What Success Means

A Tool succeeds when a fresh Agent, on any supported Model, chooses it for the right task, obtains the intended effect on the first call (or the second, after one clear error), and knows what to do next, at low context cost.

Evidence from real Runs: hard failures (`ok: false`) are rare and usually self-corrected. The expensive failures are elsewhere:

- Agents bypass a Tool, for example searching and editing files through the shell because the dedicated Tool feels unfamiliar.
- Agents loop on a strict validator that rejects an obviously meant call.
- Agents accept an `ok` result whose effect is wrong (a lost exit code, broken indentation).

A green run of prescribed calls proves none of this. Judge a Tool by what Agents actually do with it.

## Principles

### 1. Meet trained habits

Models arrive trained on a few widely used Tool dialects: Claude Code (`Read`/`Edit`/`Bash`/`Grep`/`Glob` with `file_path`, `old_string`, `command`, `pattern`), Codex (`apply_patch` V4A, `shell`), opencode, and Hermes (`terminal`/`process` with `poll`/`log`/`wait`).

- Choose names, parameter names and formats that match the dominant habit for the capability. Novel interfaces must earn their unfamiliarity with a clear gain.
- Accept the other common dialects as aliases (principle 2); the schema advertises one canonical form.
- Names must be honest about the platform. A Tool named `bash` primes bash syntax; if it runs PowerShell, Agents write `export`, `2>/dev/null` and heredocs that fail. vBot therefore offers its shell Tool as `powershell` on Windows while the registry keeps `bash` (`tools.md` -> Model Tool names).
- Formats handed between Tools must survive copying: text an Agent copies out of one result (read output, error candidates) must be accepted verbatim as input by the next Tool.

### 2. Execute clear intent

The test: would a competent colleague who receives this request know exactly what is meant, without asking? Then execute it. A round trip spent correcting a call whose meaning was already clear is a defect.

| Situation | Handling | Examples |
|---|---|---|
| Unambiguous representation or dialect difference | Execute silently | `file_path` for `path`, `cmd` for `command`, `"true"` for `true`, a JSON-encoded array, a known call wrapper, `example.com` for a URL, a shell-style argument string for an argument list, a unified diff for a patch, read-output line prefixes or typographic quotes copied into patch context |
| Fields the selected action does not use | Ignore; name them in the result only when a meaningful value suggests the Agent expected an effect | `subagent(action="run", id="unused")`, `process(action="kill", filter="all")` |
| Interpretation that involved judgment | Execute and state the interpretation in the result | `timeout: 120000` read as milliseconds; an invalid regex searched as literal text; model indentation mapped to the file's tabs; a unique near-miss name resolved for a read-only lookup; an edit's old text with a misspelled word applied to the one passage it copies, naming the line that differed |
| Plausible readings with different effects, or a missing decision | Refuse before side effects, name the problem and give the corrected call | patch removes `"earth"` but the file says `"world"`; `memory(action="add")` without scope |

Never write to a guessed target, silently drop a requested effect, or resolve contradictory instructions by picking one. Similarity alone (edit distance, a single close schema match) does not establish intent for a mutation or an explicit target; domain evidence does.

Text an Agent copied with errors is such evidence when it is strong enough: Agents misspell and misremember when copying, and demanding an exact copy wastes a round trip whenever the target is clear. An edit's old text therefore still identifies its passage when enough of it is copied correctly and exactly one passage qualifies:
- Grade the evidence by length: short text must be exact, and each deviation needs several correctly copied words.
- A different identifier or value is not a copy error; it may name another place or state (`load_user` for `save_user`, `3` for `5`, `<` for `>`).
- Apply the change like a merge: a line the edit writes comes out as the Agent wrote it, up to the file's spelling, so text the Agent keeps in it must match the file (a difference may be wording the Agent meant to write); lines used only as context stay as the file has them; the change must rest on text the file holds.
- Keep every line in its place: a copy that left out or joined lines must not shift the edit by one line.
- Never fall back to another passage when the best one cannot take the change; refuse instead, and ask for a fresh copy when several passages qualify.
- Name every line that differed and every respelled word in the result.

Rules and thresholds: `apply_patch.md` -> `copy_match`. The `"earth"`/`"world"` example above stays a refusal: the change rests on a word the file does not hold, which is not a misspelling.

Where it lives: shared representation repair (types, encodings, scalar-to-array) belongs to `contracts.py`; field aliases, wrappers, vocabulary formatting, inapplicable fields and empty-as-omitted belong to the owning Tool through `argument_normalizer` and `_argument_repair.normalize_call_arguments`. Repairs stay scoped to call syntax: they never rewrite payload values, quoted text, external identifiers or application data.

### 3. `ok` means the intended effect happened

- Tolerance must produce the correct result, not merely an accepted call: replacement text follows the file's indentation style, line endings and encoding.
- Report facts truthfully: real exit codes, real counts, the actual target.
- Partial success is unmistakable: which items were applied, which were not, and that applied items stay applied.
- An effect that already holds (the edit is already present, the process already stopped) is success with a clear "nothing to repeat" signal, not an error that invites retries.
- A non-zero exit of a command the Agent ran is data about the command, not a Tool failure; the Tool fails when it could not do its own job.

### 4. Results drive the next step

Every result answers: what happened, what is known now, and what the next useful call is.

- Return identifiers and continuation values in the exact argument shape the next call needs (`offset=2001`, `process_id`, a page `ref`).
- Name the observed state together with valid next actions. `status: "running"` alone invites polling; "the result arrives automatically; continue other work or end the Run" does not.
- Distinguish an exhausted search from a partial page, and a truncated output from a complete one; give the path or call that retrieves the rest.
- Content reaches the Model as readable text, not escaped inside a JSON string: escaping costs 9-27% extra tokens on file content and makes exact copying error-prone.
- Include only fields that inform a decision. Repeated boilerplate (null fields, echoed arguments, unchanged warnings) costs context on every call.
- Prefer returning what the next step needs over forcing a follow-up call, when it is cheap (a patch result shows the changed region; a failed patch shows the closest current lines).
- Distinguish a successful invocation from a verified task outcome when the Agent could mistake one for the other.

### 5. Errors are instructions

An error states the lifecycle state (nothing applied, partially applied, already done), the concrete cause including the offending value, and the valid next call or the available choices.

- No raw validator output. Bad: `arguments: 'path' is a required property [required]`. Good: `read needs "path", the file to read. Received: files.`
- Show candidates as readable text with line numbers, exactly as the Agent would copy them, and make invisible differences visible (tab versus spaces, trailing whitespace).
- Offer the nearest valid values (similar file names, valid actions, available Skill names).
- Name the state, never a wrong cause: "already delivered" informs; "not owned" reads as a permission problem and invites retries.
- When refusing an action, name the permitted alternative.
- Escalate after repeated failures of the same kind: point out the loop and offer a different strategy.

### 6. Design Tool families together

Tools that hand data to each other form a family and are reviewed and changed together, including their System Prompt guidance blocks, notifications and consumers.

- Files: `read` -> `apply_patch`; `search_files` -> `read`.
- Shell: `bash` -> `process` and the automatic completion notification; `terminal` for interactive programs.
- Delegation: `subagent` plus its System Prompt block and completion delivery; `status`.
- Web: `web_search` -> `web_fetch`.
- Knowledge: `memory`, `skill`/`skill_manage` plus the Skills prompt section, `session_search`, `history`.

Cross-Tool guidance must never point to an unavailable Tool: gate it by availability, or phrase a preference conditionally with a valid fallback.

### 7. One home per piece of guidance

| Surface | Carries |
|---|---|
| Name and description | What the Tool does, when to use it instead of alternatives, and what a correct first call needs (prerequisites, consequential defaults, format) |
| Parameters | What value to send, when to omit it, consequential omission behavior |
| System Prompt guidance block | Only strategy spanning several Tools or turns (when to delegate, how to split work); rendered only while the Tool is available |
| Result | The situational next step, truncation, continuation |
| Error | Recovery for this failure |

Do not repeat guidance across surfaces unless the repetition prevents a likely mistake. Keep in the definition what only it can teach: accepted formats, deviations from standard semantics, behavior invisible in results, and silent hazards (for example a foreground command being killed at its timeout). Also keep steering sentences deliberately placed to guide behavior. Write for a fresh Agent without project documentation: only concepts it can observe or act on, no hidden implementation categories, no glossary terms it has never seen (project documentation never reaches the Model). Prefer the shortest plain sentence that names the subject, the event and the recipient, and name concrete Agent actions rather than abstract concepts.

### 8. Measure real behavior

- Mine real Sessions: failure codes, what the Agent did next, fallbacks to the shell, identical retries, result sizes, per Model.
- Probe through production dispatch and inspect the resulting state (file content, exit code), not just `ok`.
- Run black-box tasks with several Models, including weaker ones; count first-attempt success, recovery and bypasses.
- Mistake catalogs come from real Runs and known dialects, not from invented typos.

The procedure and tooling: `.vorch/workflows/tool-review-workflow.md`.

## Definition Rules

### Names and descriptions

- Short, stable `snake_case` names; established domain nouns or verbs; no implementation, Provider, transport or version names. One coherent capability per Tool; separate discovery from execution only when combining them would make invocation ambiguous.
- The first sentence states the action and its observable result. An editing Tool teaches how to supply the change, not only how to locate it.
- Say when to prefer this Tool over a plausible alternative (for example the shell) when Agents otherwise choose wrongly.
- Mention result behavior only when it affects the call, pagination, follow-up or safe use. No handler internals, libraries, storage layout, UI, logging or telemetry.
- Examples must perform the advertised operation (target plus the actual change or query) and match actual behavior, verified against independently specified results; omitted optional fields stay omitted rather than `null`.

### Public shape

Use this decision order:

1. **One behavior:** its arguments directly in one flat object. `channel_send` takes delivery fields; `action: "send"` would repeat the name.
2. **One repeatable independent behavior:** one plural array of compact operation objects when batching saves round trips; preserve input order and report indexed outcomes.
3. **One behavior with optional targeting:** direct optional target fields (`status()`, `status(session_id)`, `status(agent_id, session_id)`); these are targets, not actions.
4. **Several genuinely different behaviors:** one top-level `action` enum with every action's arguments beside it in the same `properties`. Require `action` unless a common action has a documented unambiguous default (`subagent(content)` delegates); an explicit invalid value never counts as omission. CRUD and read-versus-mutate normally require explicit actions.

Never expose `request.operation`, operation-key objects (`{"create": {...}}`), stringified nested requests, mutually exclusive booleans that encode actions, or `oneOf`/`anyOf`/`allOf`/conditional schemas for action branches. Action-dependent arguments stay out of the root `required` list; their descriptions state the dependency briefly ("Required for write."). The handler validates the selected action's requirements and ignores fields that action does not use (principle 2).

### Parameters

- Minimal and Agent-relevant; one concept once. No implementation knobs (internal paths, executable flags, storage layout, cache controls) unless choosing them is part of the requested behavior. The schema lists one canonical name per field; aliases are accepted by the handler, not advertised.
- A field is optional only when omission has a real default, an unambiguous derived fallback, or selects the current target.
- Plain JSON Schema object with `type`, `properties`, `required`. Never emit `additionalProperties`.
- `required` only for fields every valid call needs; use `required: []` when none are. No `null`, nullable unions or sentinels unless `null` carries a meaning such as clearing a value.
- Narrowest simple type that preserves the interface; small fixed vocabularies as enums. Bounds, patterns and formats only when they help construct a valid call.
- Use stable ids returned by earlier results for follow-up mutations; say when ids can shift.
- One vocabulary: `action` for behavior, `mode` only for a genuine execution contract (foreground/background), domain names for arguments.
- When discovery and exact retrieval share authority and lifecycle, keep one configurable capability and derive the reader as a companion; the search result returns the reader's callable argument shape.
- `oneOf` only when a single parameter genuinely accepts several representations and removing one would lose capability (`read.offset` is the reference case); never for actions, targets, optionality or cursor continuation. Branches carry no duplicated descriptions or constraints.
- No root parameter-object description unless it states conditional rules that cannot live on the affected properties.

### Parameter descriptions and defaults

- Role first, then accepted values, then the omit rule as its own short sentence ("Omit to use the working directory.").
- State the omission result only when it affects the decision to omit.
- Do not repeat `required`, enum values, bounds or defaults in prose unless it resolves a real ambiguity.
- The handler is the sole authority for defaults and applies them explicitly; never rely on a Provider to inject one.
- JSON Schema `default` only for numbers, and only when omission is common, the value is stable, matches the handler exactly and helps planning (page sizes, limits, timeouts).

### Schema and handler boundary

- The schema owns field names, simple types, small enums and universal requirements. The handler owns defaults, conditional requirements, inapplicable fields, cross-field meaning, authorization, existence, state transitions and all argument interpretation.
- A permissive schema is fine when descriptions guide the Model and the handler validates execution; do not expand the schema to encode every invalid state.
- Authorization, path safety, sanitization, resource limits and destructive-action checks never depend on Model compliance.

### Provider rendering

- Canonical definitions are Provider-neutral. Adapters change only the transport wrapper (OpenAI Chat `function`, Responses `name` plus `parameters`, Anthropic `input_schema`).
- Never enable strict Tool calling. This is a deliberate decision: strict mode repeatedly caused problems in practice. Do not reshape schemas for a strict subset, force optional fields to required-and-null, or add defaults, required fields, nullable types or closed-object keywords absent from the canonical definition.

### Results, errors and display

- The stored result envelope `{ok, error, data, artifacts}` is the persistence and dispatch contract (`../tools.md` -> Contracts). What the Model reads should follow principles 3-5.
- Batched independent operations keep input order with stable indices; mixed outcomes use a successful envelope with an explicit partial status; zero successes use a failure envelope.
- Expected failures use precise codes and actionable messages; set retry metadata only when it changes what the Agent should do.
- UI labels and hidden arguments come from `ToolDisplay` (`summary_builder`, `hidden_argument_keys`), never from extra public arguments.
- Internal paths, provenance and implementation state stay out of results unless the Agent needs them for the next in-scope call.

### Retired shapes

The helpers `operation_envelope_schema`, `extract_tool_operation`, `action_schema` and `discriminated_union_schema` were removed; do not reintroduce them. Recognizable older call shapes are still interpreted under principle 2. Keep any rendering of historical persisted calls the WebUI needs.

## Token Cost

Report before/after token counts for every Tool audit or change, including replaced Tools: the full model-facing definition (name, description, parameters) with the same tokenizer and serialization on both sides (`core/utils/tokens.py`), stating encoding, revisions and whether wrappers are included. A definition recurs in every request that carries it (subject to caching); a result is paid once but stays in history. Where evidence exists, also compare result sizes, call counts and recovery calls for matched tasks. Lower cost never compensates for a wrong effect or missing guidance.

## Review Checklist

- The Tool name and parameter names match the dominant trained habit, or the difference earns its cost.
- The first sentence states action and result; a fresh Agent can write a complete first call from the definition alone.
- Common dialect variants, copy mistakes and placeholder fields execute with the intended effect; genuinely ambiguous calls are refused with the corrected call.
- Tests assert the resulting state (file content, exit code, receiving call), not only `ok`.
- Every result names the state and the next step; truncation and continuation are explicit and copyable.
- Every error states what was and was not applied, the cause with the offending value, and the valid next call. No raw validator text.
- Content reaches the Model as readable text; no field without decision value.
- Family members, guidance blocks and notifications were reviewed together; no guidance points to an unavailable Tool.
- Every optional parameter says when to omit it; every parameter keeps enough description to be used correctly, without duplicated schema facts or runtime internals.
- Schema mechanics hold: flat object, no `additionalProperties`, correct `required`, numeric-only `default`, non-strict Provider rendering; the handler validates conditional requirements and applies every default.
- Before/after definition tokens are reported; claimed workflow savings have matched-task evidence.
- Focused dispatch tests and black-box task evaluations (`.vorch/workflows/tool-review-workflow.md`) verify the intended outcome before the next Tool is changed; conformance probes stay separate evidence.
