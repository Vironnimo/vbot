# Permission approvals

Implementation design agreed with the user on 2026-10-30. The owning code and focused contract tests below establish the shipped behavior; this document records the cross-domain contract rather than a second policy implementation.

## User decisions

- Missing permission can require a user decision. Explicit deny rules, protected locations, unsafe path forms and unanalyzable operations remain refusals.
- A positive decision authorizes the exact prepared Tool invocation once. It never adds a persistent rule or broadens an Agent, Session, Project or inherited Permission Ceiling.
- Rejecting the additional access suppresses that access for the rest of the Run, including equivalent path spellings and other Tools. Rejecting read also prevents a write needing that read; rejecting write does not prevent an otherwise allowed read. Other allowed work can continue.
- WebSearch and WebFetch are controlled by Tool access, without additional Agent domain rules or approval prompts. Public HTTP(S), DNS/address validation, redirect validation and transport bounds remain enforced. Network operations performed by shell programs retain the shell's existing Permission Rules.
- Normal user work, including Cron and Sub-Agents, can wait for approval. Reflection and Librarian work never creates approval requests. Cron Watch checks are service checks outside an Agent Run: saving or evaluating their inherited ceilings never prompts or persists a one-call grant; Tools later invoked by the Cron Agent use the normal Run approval path.

## Decision inputs

**Refusal:** `PermissionRefusal(access, target, reason, source, rule, base, location, detail, resolved, allowed, hint, below, retryable)`; `reason` is `deny_rule` (`below`: the denied part a recursive or wildcard access would reach), `protected_location` (`location` `data_dir`, `installation`, `agent_config`, `vbot_server`, `vbot_cli`), `not_permitted` (by `base` of layer `source`), `unsafe_form` (`location` names the form) or `unanalyzable` (`detail` is the caller's clause). `hint` names the way forward instead of the closing "tell the user"; with it, and for every `unanalyzable` refusal, the message leaves out that every other way is refused too. `retryable`: only a failed analysis. `refusal.message(tool=...)` is the Agent-facing text, `refusal.watch_message()` the wording for a Cron watch check, which also reaches the user's alert; `PermissionDenied(refusal)` raises it through a Tool.

## Ownership

Permission Rules remain a pure decision library. `core/permissions/` owns normalized access requirements, hard-first checks and per-call exact access matching. Existing Run controls in `core/runs/` own the process-local pending user decision and its lifecycle; this extends the owner of cancellation and transient accessor actions instead of introducing another global workflow owner. `core/tools/` owns invocation preparation and applies approvals only inside one captured call. Chat binds the live Run and rebuilds its effective rules. The server exposes operator decisions; WebUI and CLI present the same server truth.

## Preparation and execution

Each Tool owner identifies the full intended access set before the requested file, process or external side effect. Run bookkeeping and provisioning its private Scratch folder are internal preparation, not an authorization to touch the requested target. Registry preflight runs after argument repair/validation and before Agent Secret substitution. A sink that must assess substituted bytes inside its handler stages those bytes without consuming a grant and retains the original public references for display; it commits the exact Secret use only after consent and final validation. Competing or changed grants refuse before the effect. A Tool whose final executable operation is prepared inside its handler can use the same authorization seam there, before secrets, network access, process launch or file mutation.

All hard refusals are determined before asking for missing permissions. This includes protected/denied descendants of a recursive target, lexical and resolved link paths, every command in a shell plan, inherited rules and unresolved command elements. One missing base permission cannot hide a later hard refusal.

Only necessary explicit targets prompt. Directory-entry filtering, search-result filtering, missing-path suggestions and other optional probes keep ordinary non-prompt checks. Mutating batch Tools preflight their complete intended path set before their first effect. If a later state change introduces an unapproved access, the operation fails that access; it does not restart the handler or replay completed effects.

A grant is a per-call exact-access overlay. The original Permission Boundary remains available for filtering and ceiling creation. A grant cannot transfer to sibling calls, background commands or Sub-Agents, nor can it become a wider directory/host/command rule. Original arguments, the selected Computer, cwd and normalized access extent identify the prepared invocation. Public request arguments retain Secret references; substituted values are never approval payloads. All displayed fields are additionally masked against known Secrets, and `display_redacted` tells the accessor when values are hidden.

After waiting, execution rechecks current rules and preparation freshness. Changed hard rules, links, Computer authority or operation identity cannot be bypassed by an old positive decision. Execution slots are released while waiting and reacquired before execution. Call-local cancellation is checked again after reacquisition and remains local to that Tool; Run cancellation still propagates.

Media generation revalidates concrete directory probes and output filenames after admission to the artifact worker, before each write. A post-generation refusal reports that the Provider already generated the result, names completed outputs and possible partial files, and never automatically repeats the paid generation.

## Pending decisions and accessors

A pending record identifies its opaque request, Run, Agent, Project, Session, Tool call, selected Computer, prepared public arguments, cwd, reason and typed requested access. The manager exposes a complete `{revision, requests}` snapshot and change subscription. Operator RPCs are `permissions.pending` and `permissions.resolve` (`id`, `decision`: `allow` or `reject`); an already retired request fails `approval_not_found`.

WebSocket hello and change events carry authoritative snapshots. Clients order them by the approval revision within the current server lifetime and reset on a new server epoch. They do not reconstruct approvals from transcript text. The app-wide surface exposes requests from other Sessions, Cron and descendants, and presents the exact action and extra requested scope with Allow once / Reject choices.

A browser disconnect does not decide or cancel a request. Run/Tool cancellation, interruption and shutdown remove it and release its waiter. Pending requests and grants do not survive server restart; existing Run restart recovery reports interrupted work and never replays the requested effect. There is no automatic approval on timeout or missing user presence.

The CLI exposes the same pending records and decisions for the human operator. Answering requests refuses inside an Agent Run, matching other existing user-only access-control commands.

## External review

`ToolContext.request_approval` also supports an operation review that requires consent even when the Agent's Permission Boundary is unrestricted. A reviewer can request a human decision; it cannot override a Permission Rules refusal. The shell-review Extension supplies its evaluation separately and uses the final prepared shell invocation before secrets and launch. Its Model decision and settings remain Extension-owned.

## Verification contracts

Focused tests cover hard-first classification, exact one-call authorization, equivalent-access rejection suppression, no effects or consumed Secrets before consent, fresh-rule/link/Computer validation, cancellation, semaphore release, sibling isolation, quiet internal work, Cron/Sub-Agent visibility, reconnect ordering, stale responses, public HTTP protection and operator-only CLI decisions. Tests assert behavior and protocol fields rather than editable prose.
