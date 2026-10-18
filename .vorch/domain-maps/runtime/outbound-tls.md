# Outbound TLS

Cross-cutting home of `core/utils/tls.py`: the one TLS context every outbound server connection uses, why it is shared, and the variant for short-lived clients outside the server.

## Shared context

- Every server `httpx` or `httpx2` (the MCP SDK's HTTP client) client or transport, bundled Extensions included, passes `verify=shared_ssl_context()`, and every `websockets` `wss://` connection passes it as `ssl`. It is one process-wide context with httpx's default verification plus the operating system's trusted CAs, because a bare client or connection re-parses the CA bundle (150-250 ms, blocking) each time.
- `prewarm_outbound_http()` builds it off the Event Loop at bootstrap (`core/runtime/_bootstrap.py`). The same startup thread imports the transport modules httpx loads lazily (httpcore, anyio's asyncio backend), which otherwise cost the first client up to 0.4 s on the Event Loop.
- `tests/core/utils/test_tls.py` guards every call site. Libraries that build httpx clients themselves get the context through their own options, such as python-telegram-bot's `HTTPXRequest(httpx_kwargs=...)` (`channels/telegram.md`); the guard cannot see those.

## Clients outside the server

Short-lived clients outside the server that talk plain HTTP (the CLI to its server, `cli.md`) pass `verify=ssl_context_for(url)` instead: a context without trust anchors for `http://`, which parses no CA bundle and fails closed, and the shared context otherwise.
