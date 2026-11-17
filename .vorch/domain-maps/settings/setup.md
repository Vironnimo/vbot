# Shared first setup

## User requirements

First setup is one voluntary guide shared by installation, WebUI and TUI. A user can leave at every step, including before selecting a Provider or Model; saved choices stay and untouched options retain their normal defaults. Leaving suppresses later automatic entry in every Accessor. Manually reopening the guide never resets this decision.

A configured first Agent also ends automatic setup, whether configured through the guide or manually. Completion is historical: later expired credentials, disabled or removed Providers, missing Models, and cleared Agent configuration never reopen it (user decision 2026-11-14).

## Ownership and persistence

`core/settings/setup_state.py` owns the monotonic completion state and progress; `StorageManager.setup` supplies one store per Runtime. `setup.json` is a Generation 1 document, validates before reading or writing, preserves unknown fields, and joins `doctor config`. Missing state starts pending; malformed or unreadable state is unavailable and never overwritten as if new. The store serializes read-modify-write and records terminal transitions once in the log.

Setup stays outside data snapshots and configuration backups: restoring older configuration must not revive a guide already completed or skipped. Reopening is Accessor presentation only; there is no reset mutation.

`core/runtime/_setup.py` reconciles the recorded target with the ordinary Agent roster, falling back to the first Chat Agent when the target no longer exists. A well-formed effective Provider/Model reference is evidence of the user's selection, including a shared Agent default: fresh Agent defaults and the installer seed contain no Model. Completion deliberately needs no catalog lookup, credential check, enablement or network probe, so existing configurations with a now-removed Provider also remain finished. Runtime startup, Agent create/update and shared-default changes record this observation; reading setup also catches manual file edits. Observing setup failure logs degradation without invalidating a configuration mutation that already saved.

## Accessor contract

`server/rpc/setup_methods.py` registers `setup.get`, `setup.update` and `setup.finish` in the ordinary RPC table. They return the same flat state: status, saved step, target Agent id and whether automatic entry remains appropriate. Existing domain management RPCs own actual Provider, Model, Skill and Extension changes; setup duplicates none of their configuration or credentials.

Progress writes remain allowed after completion or skipping. Explicit finish records completed when the first Agent is configured and skipped otherwise; completed never becomes skipped or pending. `setup.get` can latch a newly observed manual configuration, so its sequence uses the cancellation-safe mutation lock too. Agent roster reads and state writes run on the Session worker, never on the Event Loop. Changed setup state publishes the ordinary unscoped `resource_changed` kind `setup`; defaults also publish their usual Agent invalidation.

The WebUI guide adopts shared progress or its own initial read through the same step transition. A newer shared snapshot or an acknowledged local change supersedes an initial read still in flight.

## Verification

`tests/server/rpc/test_setup_methods.py` covers exit at every step, shared durable progress, manual configuration and later removal, existing configuration, sparse validation and unknown-field preservation. `tests/core/test_json_documents.py` guards exclusion from both snapshot kinds; Runtime wiring verifies that a fresh bootstrap Agent has no Model and leaves setup pending.
