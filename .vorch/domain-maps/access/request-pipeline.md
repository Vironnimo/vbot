# Access: request pipeline

The access guard's per-request rules in detail: peer and transport checks, forwarding headers, Host names, credentials, live connections, response headers, the exempt routes and the refusal reasons. Pipeline order, outcomes and invariants: `access.md` -> Request pipeline.

## Peer and transport

`_rules.inspect_request`; a refusal answers 403 `access_denied` (WebSocket close 4403):

- `local`/`lan`: any proxy or CDN header (`_PROXY_HEADERS`: `Forwarded`, `Via`, `X-Real-IP`, `X-Client-IP`, `X-Cluster-Client-IP`, `X-Original-Forwarded-For`, `True-Client-IP`, `CF-Connecting-IP`, `Fastly-Client-IP`, and every `X-Forwarded-*`) refuses the request (`proxy_headers`): a proxy in front means the request may come from anywhere. A peer outside the admitted networks is refused (`peer_not_admitted`).
- `authenticated`: forwarding headers count only when the direct peer is a trusted proxy. The effective client is the rightmost `X-Forwarded-For` entry that is not a trusted proxy (entries with a port are accepted; any other non-address entry refuses, `invalid_forwarded_for`); the rightmost `X-Forwarded-Proto` must be `https` (for a WebSocket also `wss`). A direct peer needs HTTPS unless it is loopback (`insecure`).
- uvicorn runs with `proxy_headers=False`; nothing else interprets forwarding headers.
- Host name, in every mode (`HostPolicy`, against DNS rebinding): the `Host` must be an IP address or one of the server's names: `localhost`, this computer's host name, fully qualified name, short name and `<short>.local` (`machine_host_names`), the bind host, the public URL's host and each `--allow-host`. Anything else, or several `Host` headers, answers 403 `access_denied` "Access denied: unknown host name" (`host_not_allowed`; the log shows the Host). A request without `Host` passes.

## Credential

`AccessService.authenticate`: `Authorization: Bearer` wins over the `__Host-vbot_device` cookie; a second `Authorization` header, another scheme, or more than one device cookie value is malformed (those cookies are not cleared: one of them may be valid). A `vbl_` token is checked against this start's local token, and only from a direct loopback peer; a `vbt_` token through the device store (in memory, constant-time hash comparison). In `local`/`lan` a refused credential still yields `open`; in `authenticated` it is logged, counted per effective client, and a dead cookie is cleared (`Max-Age=0`), unless the response sets a new device cookie itself (a ticket redemption or a browser's pairing claim). A well-formed `vbt_` token that an unavailable device store cannot check is neither accepted nor refused (Invariants).

## Live connections

Every request of a Device registers in `LiveConnections` at entry. Revoking or expiring the Device cancels its WebSockets (close 4401 `Device signed out`) and its responses that started and have not finished (an SSE stream or a download ends); a response that has not started yet becomes 401 `auth_required` "Device signed out". The request that revokes still answers (`devices.logout`).

## Response headers

Every HTTP response gets `X-Content-Type-Options: nosniff`, HSTS (`max-age=31536000`) when the request arrived over HTTPS, and on `shell` routes `Referrer-Policy: same-origin` plus the WebUI CSP (`WEBUI_CONTENT_SECURITY_POLICY`: same-origin scripts, connections, frames and workers; inline styles; remote `http(s)` images for Markdown; `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'none'`). In `authenticated` mode it is `AUTHENTICATED_WEBUI_CONTENT_SECURITY_POLICY`, which admits remote images over `https:` only: a plain-http image request would show everyone on the network path which WebUI page the user reads. A header the route set itself wins.

## Exempt routes

The exempt set today: `/health`, `GET /api/auth/status`, `POST /api/auth/pair/start|poll`, `GET /api/oauth/callback` (the `SameSite=Strict` cookie is not sent on the cross-site return; the pending sign-in's state protects it), `POST /_vbot/control/shutdown` (its control secret protects it), `GET /auth/ticket/<ticket>` (the guard also exempts HEAD there; no handler answers it), and GET/HEAD of `/api/files/<cap>`, `/api/preview-assets/<cap>/<path>`, `/api/extension-assets/<cap>/<path>` and `/api/extension-runs/<cap>/events|timeline`. Any other method or shape under these prefixes is `protected`.

## Refusal reasons

The first refusal of a reason logs WARN `Access refused (reason=... client=... path=...)` (plus `host=...` for `host_not_allowed`), then one `Access refused repeatedly (reason=... count=... clients=... interval=60s)` per minute while it continues (at most 10 clients named, then `...`).

Refusal reasons (`reason=` of the refusal log lines): `unknown_peer`, `proxy_headers`, `peer_not_admitted`, `invalid_forwarded_for`, `insecure`, `host_not_allowed`, `malformed_credentials`, `invalid_local_token`, `local_token_not_local`, `invalid_token`, `expired_device`, `invalid_ticket`, `pairing_rate_limited`, `devices_unavailable` (a device token the unavailable store could not check).
