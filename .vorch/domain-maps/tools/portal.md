# Portals (`portal`)

A Portal shows the user a web app that runs on the vBot computer, such as a dev server the Agent started, through a private link: the WebUI shows a card after the Tool call with the app's state, an expandable preview and "open in new tab". User decisions of 2026-10-21: every Portal has its own port of the vBot server (an origin of its own; behind a reverse proxy it is reachable only from the vBot computer, and the card says so); the Portal only forwards, the Agent starts and stops the app itself; a Portal ends when the Agent or the user closes it, its Session is archived (deleted) or vBot stops; every Agent may open Portals; targets are only `localhost` ports, never vBot's own; the credential reaches only the WebUI card.

## Interfaces

- Tool `portal` (`core/tools/portal.py`, registered in `core/runtime/_bootstrap.py`, in `PROJECT_DEFAULT_ALLOWED_TOOLS`, family `media`, on-demand summary): `action` `open`/`list`/`close`; `port` (required for open, 1-65535), `title` (whitespace collapsed, at most 120 characters, default `localhost:<port>`), `path` (a page of the app, `/` prefixed, default `/`), `id` (required for close). The normalizer reads a whole local address (`url`, `address`, `target`, ... such as `http://localhost:5173/docs`) into `port` and `path` and refuses a non-local one; action synonyms (`create`, `start`, `stop`, `delete`, ...) map to the three actions. Outside a Session it fails with `session_required`. Opening the same port and path again in the Session returns the open Portal.
- Registry: `PortalRegistry` (`core/tools/portal_registry.py`, `Runtime.portals`) owns the process-local records (`Portal`: id `p` + 8 hex, owner `SessionAddress`, title, path, target and listen port, `listening`), at most 5 per Session and 20 in all, change subscribers (called with the owning Session's address) and a watch that connects to each target every 2 s while it does not answer and every 10 s while it does. The network side is the `PortalListeners` protocol the server attaches at lifespan start (`attach`) and removes before Runtime shutdown (`detach`); without it opening is refused (`unavailable`). Archive closes the archived Sessions' Portals through `ArchiveServices.close_session_transports` (`archive.md`); Runtime shutdown step `portals` closes the rest.
- Display: `ToolContext.show_display_portal(id, title, port)` puts `portal: {id, title, port}` into the call's display. It carries no credential and needs no projection; the WebUI reads the live state by id (`webui/chat.md`).
- RPC (`server/rpc/portal_methods.py`): `portal.list {agent_id, session_id}` -> `{portals: [{id, title, path, target_port, listen_port, listening, token}], reverse_proxy}`; `token` is the Portal capability, bound to the requesting principal by the RPC transport like every capability (`server/browser-capabilities.md`); `reverse_proxy` is true in `authenticated` mode without direct TLS. `portal.close {id}` -> `{id, closed}`; closing a closed Portal changes nothing. Invalidation: `resource_changed(kind="portals")` scoped `{project_id, agent_id, session_id}` from the registry's change subscribers (`server/_app_lifecycle.py`), on open, close and a change of `listening`.
- Card URL: `<page scheme>://<page host>:<listen_port><path>` with query `vbot_portal=<token>`.

## Listener and forwarding

`server/portals.py::PortalServer` binds each Portal on the server's bind host with a system-chosen port (every address the host name resolves to, on the same port) and serves it with uvicorn's HTTP protocol class (the TLS-sniffing one of `server/access/tls.py` under direct TLS) without a `uvicorn.Server`: no lifespan, signal handlers or uvicorn log lines, and `log_config=None` so building its `Config` leaves the process's logging alone. Closing stops accepting, shuts down its connections and cancels running requests at once.

Per request, in this order:
1. The access guard's peer, transport and Host rules (`AccessService.inspect`, `access.md`): the same refusals as on the main listener (403).
2. The credential: the Portal capability (HMAC domain `portal:` over the Portal id, `FileDelivery.portal_token`/`portal_claims`) from the query parameter `vbot_portal` or from the cookie `vbot_portal_<listen port>`. In `authenticated` mode it must be an access envelope whose actor `principal_for_capability` still resolves (a revoked or expired Device, an unbound token or another Portal's token answer 401). Without it: 401 "This portal is private" (a page for documents, text otherwise; WebSocket close 4401).
3. A GET/HEAD carrying the query credential gets a 302 to the same URL without it, setting the cookie (`Path=/; HttpOnly; SameSite=Lax`, `Secure` under HTTPS), with `Referrer-Policy: no-referrer`; the token never stays in the address bar or reaches the app.
4. Forwarding to `http://localhost:<target>` (IPv4 or IPv6, happy eyeballs) over one shared httpx client without client default headers: `Host` becomes `localhost:<target>`; `Origin` and `Referer` that name the Portal's own origin are rewritten to the target origin, foreign ones pass unchanged; hop-by-hop headers and the client's `X-Forwarded-*` are dropped and `X-Forwarded-Host/Proto/For` set. Of the browser's cookies only those with the Portal's prefix `vp_<id>_` reach the app, prefix removed: vBot's Device cookie and other Portals' cookies never do. Response `Set-Cookie` names get that prefix and lose `Domain`; a `Location` pointing at the target origin becomes relative. Bodies stream raw in both directions (compression untouched, SSE flows as it arrives).
5. WebSockets are bridged message by message through `websockets` (subprotocols kept, same header rules).

A target that refuses the connection marks the Portal not listening and answers 502: a page that reloads itself every 2 s for document and frame loads, text otherwise. A successful forward marks it listening.

## Constraints & Gotchas

- Cookies ignore ports: every Portal and the WebUI share one cookie jar per host. The cookie namespacing above is what keeps the Device cookie away from Agent-written apps and Portals' app cookies apart; never forward the Cookie header unfiltered.
- The WebUI CSP allows frames from `<scheme>://<request host>:*` (`server/access/_guard.py`) so the card can embed a Portal; an IPv6 literal host gets no frame source (CSP cannot name one).
- Behind a reverse proxy the Portal ports are not forwarded; in `authenticated` mode without direct TLS a Portal answers only loopback (plain HTTP elsewhere is refused as insecure).
- On Windows a connection to a port where nothing listens takes about 2 s to be refused, so opening a Portal before its app and a waiting page take that long.
- Device revocation refuses a Portal's next request; WebSockets opened before it are not tracked and end only with the app or the Portal.
- Not built: Portals for other Agents' Sessions, Portals that survive a restart, targets on other hosts.

## Agent-facing text

Reviewed with `vbot-agent-tools` on 2026-10-21.

| Text | Reason |
|---|---|
| Description: `Show the user a web app that runs on this computer, such as a dev server you started, through a private link in their chat.` | Names the job and the typical case, so Agents reach for it instead of telling the user to open `localhost:5173`, which a user on another device cannot reach. |
| `The portal only forwards to the app's port; it never starts or stops the app.` | Without it Agents expect the Tool to run the app, or think closing the Portal stops their server. |
| `port`: `The localhost port the app listens on, such as 5173. Required for open.` | Says the target is local and which port is meant (the app's, not a new one). |
| `title`: `Short label on the card. Omit to use "localhost:<port>".` | Omit rule; weak Models otherwise invent a title for every call. |
| `path`: `Page the card opens first, such as /docs. Omit for /.` | Lets the Agent show the page it changed; omit rule for the common case. |
| `id`: `The portal id from open or list. Required for close.` | Ids come from results. |
| Summary: `Show a web app running on this computer, such as a dev server, to the user.` | Decides loading on demand. |
| Open result: `Opened portal <id> for localhost:<port> at <address>. The user opens it from the card in the chat, which carries their access; the address alone answers "private".` | The address serves apps that need their public URL; the second sentence keeps Agents from posting the bare address as the way in and explains the 401 a `curl` check gets. |
| `Portal <id> already shows localhost:<port> at <address>.` | A repeated open is success with nothing to do, not a duplicate. |
| `Nothing answers on port <port> yet; the card shows "waiting" until the app does.` | Tells the Agent its server is not up, so it checks the background command instead of reporting success. |
| `The user is using <Telegram / the terminal>, where the card does not show; they open the portal from this Session in the vBot app.` | Without it the Agent assumes the user sees the card on a surface that cannot show it. |
| List result: `Portals of this Session:` then `- <id> "<title>": localhost:<port>, <address> (app answers / nothing answers yet)`, or `No portals are open in this Session.` | Ids for close and the app state in one line each. |
| Close result: `Closed portal <id>. The app on localhost:<port> keeps running.` | States that the app still runs, so the Agent stops it itself when that was the intent. |
| Refusals: `Nothing was opened. Port <port> is vBot itself; pass the port of the app.` / `... portal open needs port, ...` / `... This Session already has 5 portals (<ids>); close one with action close first.` / `Nothing was closed. No portal <id> is open in this Session; its portals: <ids>.` / `session_required`: `Nothing was changed. Portals belong to a chat Session, and this call runs outside one.` | Each says that nothing happened, what is wrong, and the valid next step or choices. |
