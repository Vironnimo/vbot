# Bootstrap change subscriptions

`BootstrapService.add_changed_callback` reports committed job and Run-state changes, including direct core writes, activation, completion and restart reconciliation. It returns an idempotent unsubscribe function. Failed persistence and unchanged edits publish nothing; a callback exception is logged without changing a committed result or skipping other subscribers.

The server's Bootstrap bridge in `server/_app_lifecycle.py` publishes the `bootstrap` resource through the normal event bus and detaches on shutdown. All Bootstrap application services declare this resource; Extension observers and page invalidations therefore refresh regardless of which accessor made the change. Bootstrap has no core WebUI view or separate refresh-token group.

Coverage: `tests/core/automation/test_bootstrap.py`, `tests/server/test_app_events.py`, and `webui/src/lib/__tests__/appController.test.js`. Job scheduling and persistence contracts remain in `automation/cron.md`.
