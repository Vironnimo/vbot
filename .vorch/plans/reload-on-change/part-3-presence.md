# Teil 3 — Client-Präsenz

> Lies zuerst die [README](README.md) und [`stuff/reload-changed.md`](../../../stuff/reload-changed.md).
> Du bist der **Orchestrator** für diesen Teil.

**Ziel des Teils:** Eine „wer ist verbunden"-Anzeige in den Einstellungen unter **„Allgemein"** —
pro WebSocket-Verbindung eine Zeile (Accessor + Browser/OS + seit wann + Status), mit Markierung des
eigenen Fensters. Live über den Kanal aus Teil 1 (`kind: "clients"`). Reine Anzeige, kein Kappen.

**Abhängigkeiten:** Teil 1 (Kanal). Unabhängig von Teil 2.

---

## Task 3A — Server: Verbindungs-Registry + Identität + `client.list` + Invalidierung ⚡
*parallel mit 3B bis zum RPC-/Param-Vertrag (disjunkte Dateien: 3A Server, 3B Client)*

**read:** `.vorch/domain-maps/server.md`
**files:** `server/clients.py` (neu), `server/app.py`, `server/rpc/client_methods.py` (neu),
RPC-Aggregation in `server/rpc/methods.py` (`build_method_handlers` — Import + Eintrag im Modul-Tupel;
**nicht** `dispatcher.py`), `tests/server/test_clients.py` (neu), `tests/server/test_app*.py` /
`tests/server/test_rpc.py`

- **Umfang:** erfasst werden **nur offene App-Fenster** über `/ws` (Browser-Tabs, Desktop). Die CLI hält
  keine `/ws`-Dauerverbindung (RPC-Request/Response), und Kanäle (Telegram/Discord) sind keine Fenster —
  beide erscheinen nicht. Der Roster ist „welche App-Fenster sind offen", nicht „alle Zugänge".
- `server/clients.py`: kleine `ClientRegistry` (analog zu `server/events.py`): `register(...) → entry`,
  `unregister(id)`, `list()`. Eintrag = `{ id, accessor, user_agent (→ browser/os abgeleitet),
  connected_at }`. Reine In-Memory-Momentaufnahme.
- `server/app.py` (`websocket_events`): beim Connect die vom Client gesendete **Verbindungs-ID** +
  **Accessor-Typ** aus den Query-Params lesen (neben `epoch`/`after_sequence`), plus `User-Agent` aus
  den Headern. **Achtung:** der Handler hat heute **kein `finally`** (nur `try/except WebSocketDisconnect`)
  — eines **anlegen**: **registrieren vor** der `subscribe`-Schleife, **im `finally` deregistrieren**, damit
  auch andere Abbrüche (nicht nur `WebSocketDisconnect`) sauber aufräumen. Nach Register/Unregister
  `publish_resource_changed(kind="clients")` (Helfer aus Teil 1).
- `server/rpc/client_methods.py`: `client.list` → Roster aus der Registry (id, accessor, browser/os,
  `connected_at`, status). `method_handlers()` in `build_method_handlers` (`methods.py`) einhängen.
- Tests: Register/Unregister verändert `list()`; WS-Connect/Disconnect publiziert `clients`-Event;
  `client.list` liefert den erwarteten Roster.

**Done when:** Ein verbundener Accessor erscheint in `client.list`; Verbinden/Trennen löst ein
`clients`-`resource_changed` aus.

## Task 3B — Client: Verbindungs-ID senden + „Allgemein"-Anzeige ⚡
*parallel mit 3A bis zum RPC-/Param-Vertrag*

**read:** `.vorch/domain-maps/webui.md`, `.vorch/DESIGN.md`
**files:** `webui/src/lib/api.js` (WS-Connect-Params), `webui/src/App.svelte` (Token-Dispatch für
`clients`), `webui/src/components/settings/SettingsGeneralPanel.svelte`,
`webui/src/lib/settingsView.js` (Helfer), `webui/src/lib/i18n.js`, jeweilige `__tests__`

- `api.js` `subscribeServerEvents`: eine **pro Fenster/Verbindung eindeutige ID** minten und (mit dem
  Accessor-Typ) als Query-Params beim WS-Connect mitschicken (Vertrag mit 3A). **Wichtig: nicht
  `localStorage`** — das ist über alle Tabs eines Browsers geteilt und würde drei Tabs zu *einem*
  Eintrag kollabieren, beim Schließen eines Tabs den Eintrag der anderen löschen und „dieses Fenster"
  überall markieren. Stattdessen **pro Verbindung** (oder pro Tab via `sessionStorage`), damit
  „drei Tabs = drei Einträge" und die Selbst-Markierung stimmen. Accessor-Typ: Browser vs. Desktop
  (vorhandene `isDesktopAccessor()`-Erkennung nutzen).
- `App.svelte`: `handleServerEvent`-Dispatch um `kind: "clients"` erweitern → `clientsRefreshToken`
  erhöhen, an den General-Panel reichen.
- `SettingsGeneralPanel.svelte`: Abschnitt „Verbundene Clients" — self-loading via `client.list`
  (wie das Extensions-Panel, das seine Daten in `onMount` selbst lädt; heute ist dieser Panel eine reine
  abgeleitete Anzeige, hier kommt erstmals eigenes Laden dazu), Liste reiner Anzeige-Zeilen
  (Accessor + Browser/OS + seit wann + Status). Die **eigene Zeile markieren** (eigene gemintete ID
  matchen — der Client kennt seine aktuelle ID und aktualisiert sie nach jedem Reconnect, falls pro
  Verbindung gemintet). Reload bei `clientsRefreshToken`-Änderung (reine Anzeige → sofort). Alle
  Strings über `i18n.js`.
- Tests: Roster rendert; eigene Zeile ist markiert; `clients`-Token löst Reload aus; i18n-Keys vorhanden;
  zwei Verbindungen/Tabs ergeben zwei Einträge (nicht einen).

**Done when:** „Allgemein" zeigt die verbundenen Clients; öffnet man einen zweiten Tab, erscheint er
live; die eigene Zeile ist erkennbar markiert.

---

**Done when (Teil 3 gesamt):**
- Einstellungen → „Allgemein" listet verbundene App-Fenster live (pro Verbindung; Browser/Desktop, nicht
  CLI/Kanäle), eigene Zeile markiert; ein zweiter Tab erscheint live als eigener Eintrag.
- Kein „Kappen"-Steuerelement.
- Backend- und Frontend-Gates grün.
