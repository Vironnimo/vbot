# Model list by usage

Read this reference when you change how `model.list` orders Models by usage, the usage fields it adds, or how it picks `preferred_id` (`server/rpc/model_methods.py::_order_by_usage`).

## Why

An Agent told to "use deepseek flash" finds that Model at several Providers, and a bare `<provider>/<model-id>` resolves to the first usable allowed Connection of the Provider (`core/chat/model_resolution.py::_first_usable_connection_id`), which can be a pay-per-request API key while the user works through a subscription Connection. Ordering by the user's own use and pinning the user's Connection lets the Agent pick the route the user uses without asking (user requirement 2026-10-30). The user chose the counting basis: Chat requests of the user's own conversations, not Sub-Agent Sessions, Extensions or Task Models.

## Contract

- `model.list {sort: "usage"}` reads `StatisticsService.model_route_usage_async(since=now - 30 days)` (`statistics.md` -> Interfaces): completed `chat` requests of origins `user`, `channel` and `automation` per Model and serving Connection, counted from the whole UTC hour containing `since`. Chat records the serving Connection since 2026-10-30 (`usage.md` -> Interfaces); older requests count for their Model with `connection_id` `""` and pin nothing.
- Groups, each otherwise in name order (Provider id, Model id): `used` (requests in the window, most first), `provider_used` (no requests, but its Provider had some), `other`. Each Model gets `usage {group, calls, routes?}`, where `routes` lists short Connection ids with their request counts, most first, and only Connections that were recorded.
- `preferred_id` is `<id>::<connection>` for the Connection with the most requests for this Model among its usable allowed Connections, else the Provider's most-used such Connection (summed over all its Models), when that differs from the bare resolution; otherwise it equals `id`.
- The response adds `sort: "usage"` and `usage_days`. When Statistics cannot be read (busy, failed), the list stays in name order with `sort: "name"` and `usage_unavailable: true`, and the failure is logged; Model listing never fails because of Statistics.
- Without `sort`, or with `sort: "name"`, the response is unchanged: the WebUI and TUI read it.
- `vbot model list` sends `sort: "usage"` unless `--sort name` is given and prints the groups under headings, with `requests:` per row and a `Note:` explaining `::<connection>` when any row carries one (`cli/model_management.py`).

Tests: `tests/server/rpc/test_model_methods.py::test_model_list_sorted_by_usage_puts_used_models_and_their_connections_first`, `tests/core/statistics/test_statistics_accounting.py::test_model_route_usage_counts_completed_chat_requests_of_own_conversations`, `tests/cli/test_cli_model.py::test_model_list_groups_models_by_usage`.
