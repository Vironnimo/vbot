# Identity Agent Rename

How Runtime orchestrates renaming an Identity Agent across every service that references its id, and what `agent_references` reports for the delete guard. The Agent-owned half (tree, config, rename record) is in `agent.md`.

## Order

`rename_agent(agent_id, new_agent_id)` -> `AgentRenameOutcome` is the one entry for renaming an Identity Agent; `agent.rename` calls it under its guards (`server.md`). `_agent_rename.py` owns the order:

1. It first lists the references that already name the new id (`identity_agent_references`: Channels, non-terminal identity Cron and Bootstrap jobs, also an event Cron job that cannot fire now, since a revert would move it too, and Agent Secrets granted to the new id, as `secret:<name>`) and hands them to `AgentStore.rename`, which refuses the rename with `AgentReferencedError` after its own existence checks while they name it (`agent.md`).
2. Otherwise `AgentStore.rename` removes the delegation allow-list entries that name the unused new id, applies the Agent-owned half and leaves the rename record pending (`agent/lifecycle.md` -> Rename crash safety).
3. Then Channels (`retarget_agent_async`; only a started Channel service rebuilds adapters), non-terminal identity Cron jobs (`retarget_agent_async`, with the chat notifications they still hold for the old id, `automation/cron.md` -> Delivery) and Bootstrap jobs (`retarget_agent`) move, then Agent Secret grants (`AgentSecrets.retarget_agent`, one store write off the loop: the Agent grant, the Secrets of its Sessions and pass-through paths through them), the live Terminal Sessions' lifecycle and attachment scopes, the Portals and the shell commands' scopes move with them (`TerminalManager.transfer_agent_scope`, `PortalRegistry.transfer_agent_scope`, `CommandManager.transfer_agent_scope`; Terminals and commands are counted in the outcome's `terminal_count` and `command_count`), so cleanup, completion notices and the Portal cards follow the Sessions; none survives a restart, so startup completion has none to move.
4. `finish_rename` removes the record.

Afterwards Runtime invalidates both ids' Skills and removes the moved Sessions' old addresses from the Recall indexes, best effort (`tests/core/runtime/test_runtime_agent_rename.py`).

## Threading, compound mutation and failure

The rename runs on the Event Loop, which owns the state of these running services: it lists and moves the references there (`test_a_live_rename_moves_every_reference_or_none` asserts no job is read or changed off it) and runs each blocking Agent-store step (`rename`, `revert_rename`, `finish_rename`) as its own call on the Session database's pool. It never holds a pool worker while it waits for the loop or for more pool work, which the Cron move needs (it checks Sessions on that pool), so renames and other pool work cannot starve the pool into a deadlock (the same test gives the pool a single worker). The whole rename, revert included, is one compound mutation entered on the loop (`compound_mutation_async`, `database.md`), and a cancelled caller waits until the rename or its revert has settled. A rename that completes at startup, before they start, uses their blocking variants, which refuse once the owner runs. A failure reverts all of it through `revert_rename` and the same steps with the ids swapped.

Runtime owns this order because it alone owns every referencing service: AgentStore must not depend on Channels or Cron, and the server edge does not run at startup, where an interrupted rename must finish (`runtime/lifecycle-steps.md` -> Bootstrap phases, steps 4 and 7).

## `agent_references`

`agent_references(agent_id)` (async), which the `agent.delete` guard awaits, names only what would still start Runs or answer as the Agent: its Channels, whose configs it reads off the Event Loop (`list_channels_async`), plus `AutomationReferences.agent_references` (`automation.md`); delegation allow-lists do not count, because the archive records and removes the Agent from them (`archive/operations.md` -> Archive order).
