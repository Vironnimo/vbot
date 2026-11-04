# Agent Placement

Read when changing Chat, Helper or Private placement, owner proof, target visibility or placement transitions. AgentStore owns these rules in `_placement.py`; Sub-Agent admission and prompt catalogs consume its public methods. Group archive and recovery belong to `../archive.md`.

## Data model and owner proof

An Identity Agent without `placement` is a Chat Agent. The optional Generation 1 field is either `{"kind": "helper"}` or `{"kind": "private", "owner_id": "owner", "owner_binding": "<creation timestamp>"}`. Chat is never stored. `AgentPlacement` is immutable; RPC inputs accept `chat`, `helper`, or `private` with an owner id, but never an owner binding. RPC outputs replace that proof with `orphaned` on Private Agents. The roster and its flat order stay complete.

A Private Agent's owner is verified only when that id exists as a non-built-in Chat Agent whose `created_at` equals `owner_binding`. The store stamps a missing creation identity when assigning the owner, using the same incarnation proof as delegation bindings. A replacement Agent with the same id never inherits the Private Agents. An unverified placement is orphaned and gets no automatic targets; an orphaned target is unavailable to every other Agent. The UI puts orphans in the Helper group until explicitly placed again.

Validation rejects a non-object placement, unknown fields or kinds, Helper owner fields, a Private placement without a valid different Identity id and parseable creation binding, and placement on a built-in Agent. Unknown placement fields invalidate the Agent rather than silently dropping a possible lock; `doctor config` reports `$.placement`.

## Identity target rule

Self-delegation remains allowed. Project callers and Project targets keep their existing scope rules. Between different Identity Agents the store applies this order:

1. A target absent from the store keeps the existing explicit-or-wildcard path, including later not-found errors and temporary Agent resolution.
2. A verified Private target is available only to its owner or a verified sibling, regardless of explicit or wildcard grants.
3. An orphaned Private target is unavailable.
4. A verified Private caller cannot target its owner, even explicitly.
5. A Helper target needs a literal allow-list entry; `*` does not grant it.
6. A Chat target follows literal entries or `*`.

A temporary caller is an outsider. An orphaned caller uses its allow-list like a Helper. A Chat Agent automatically gets its verified Private Agents; a verified Private Agent gets its siblings, excluding itself. Automatic targets never enter allow-lists. Depth still follows `max_subagent_depth`.

The coordinator rechecks the complete target rule and any applicable binding while holding the existing automation reference lock whenever a binding exists or either identity has non-Chat placement. Chat-to-Chat wildcard starts remain lock-free; a start racing a change after that check uses the earlier rule. Bindings grant access only where the explicit list grants it: a verified Private target ignores stale bindings, a Chat target under `*` ignores bindings, and a Helper checks its binding even under `*`. Continuing an existing Sub-Agent does not recheck placement.

Catalogs read the roster once, combine wildcard, literal and automatic candidates, and apply the rule; a failed binding makes an entry unlisted. Provider schema narrowing adds automatic targets to explicit lists and drops denied bare ids. Wildcard schemas stay unrestricted guidance, with coordinator admission enforcing the rule.

## Lifecycle

Creation of a Helper or Private Agent with no `tools.subagent` stores an empty allow-list; Chat creation keeps the wildcard default. Placement changes preserve the Agent's own allow-list. `['*', 'helper-id']` keeps wildcard Chat access plus the individual Helper grant.

The store serializes transitions under its existing mutation lock. It refuses an invalid owner (`owner_invalid`), an owner with verified Private Agents leaving Chat (`has_private_agents`), and the last Chat Agent leaving Chat (`last_chat_agent`). Built-ins retain their existing fixed-field refusal. Repeating the same kind and owner keeps its binding and is a no-op. Creating a Private Agent checks only owner validity.

Becoming Private writes placement first, then best-effort removes its id from every other Agent's allow-list. A leftover after failed cleanup is inert under the target rule. Those grants are not saved for later reversal. Newly adding a verified Private target to any allow-list is refused, also for owners and siblings whose access is automatic; unchanged old entries remain tolerated on unrelated edits.

Rename retargets only Private placements whose owner id and creation binding match the renamed incarnation. Repeating a rename converges, and its reverse restores ownership; mismatched placements stay orphaned. Delete refuses only the last Chat Agent; deleting a Helper or Private Agent remains possible. Offline edits leaving no Chat Agent are not repaired automatically.

Placement is a Tool and selection boundary, not a process sandbox: shell/CLI access can still inspect the full roster. An older vBot ignores the additive placement field and does not enforce this boundary; upgrading again restores it.

## Verification

`tests/core/agents/test_placement.py` covers target and transition contracts; `test_validation.py` covers malformed locks and `test_agents_rename.py` owner retarget/recovery. Catalog, admission and Provider enum cases live in the corresponding Sub-Agent and prompt suites; `tests/server/rpc/test_agent_methods.py` covers public input/output and transition reasons.
