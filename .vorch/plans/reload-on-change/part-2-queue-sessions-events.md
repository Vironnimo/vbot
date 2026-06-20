# Teil 2 — Warteschlange + Sessions + Alt-Ereignisse einklinken

> Lies zuerst die [README](README.md) und [`stuff/reload-changed.md`](../../../stuff/reload-changed.md).
> Du bist der **Orchestrator** für diesen Teil.

**Ziel des Teils:** Die restlichen App-Verbraucher laufen über den Kanal aus Teil 1 — Warteschlange
und Session-Leben pushen Invalidierungen; die heutigen Agenten-CRUD-Events werden auf `resource_changed`
umgestellt, sodass es **ein** App-Mechanismus ist.

**Abhängigkeiten:** Teil 1 (Kanal + `publish_resource_changed` + Client-Funnel + Anwende-Helfer).

---

## Task 2A — Server: Warteschlangen-Invalidierung ⚡
*parallel mit 2B (disjunkte Dateien)*

**read:** `.vorch/domain-maps/server.md`, `.vorch/domain-maps/runs.md`
**files:** `server/rpc/chat_methods.py`, `tests/server/rpc/test_chat_methods*.py` (mirror prüfen)

- **Reichweite (Entscheidung Plan-Review): nur Browser-/RPC-Sends.** Die Queue-Invalidierung deckt die
  RPC-Sende-Fläche ab — das, was der Browser nutzt. Einreihungen aus dem Kern (Automation/`TriggerService`,
  Kanäle, Unter-Agenten) lösen **bewusst kein** Queue-Signal aus (Scope-Grenze; hält den Chat-Kern
  unangetastet). Fenster B holt solche Einreihungen wie bisher beim nächsten Terminal-Event nach.
- `resource_changed(kind="queue", scope={agent_id, session_id})` bei jeder RPC-Queue-Mutation emittieren,
  Scope = die betroffene Session (damit fremde Fenster es ignorieren):
  - **Einreihen:** im `{queued: true}`-Zweig **beider** RPC-Sende-Pfade (`_send_chat` **und**
    `_stream_chat`) — beide reihen identisch ein (an der Stelle, wo sie
    `_bridge_queued_item_to_event_bus` aufrufen). Zwei Aufrufseiten, **dieselbe Datei**. Builder
    verifiziert die Punkte (nicht raten).
  - **Entfernen:** `_chat_queue_remove`.
  - **Ändern:** `_chat_queue_update` — Scope mit der **aufgelösten** `resolved_session_id` (kann von der
    Eingabe abweichen), nicht der rohen Session-ID.
- Tests: Einreihen (beide Sende-Pfade), Entfernen und Ändern publizieren je ein `queue`-`resource_changed`
  mit korrektem Scope.

**Done when:** Einreihen (RPC-Send) / Entfernen / Ändern eines Queue-Items publiziert je ein scoped
`queue`-Event; Kern-Einreihungen (Automation/Kanäle/Unter-Agenten) bewusst nicht.

## Task 2B — Server: Session-Invalidierung + Agenten-Events umstellen ⚡
*parallel mit 2A (disjunkte Dateien: 2A in `chat_methods.py`, 2B in `agent_methods.py`)*

**read:** `.vorch/domain-maps/server.md`, `.vorch/domain-maps/sessions.md`, `.vorch/domain-maps/agent.md`
**files:** `server/rpc/agent_methods.py`, `tests/server/rpc/test_agent_methods*.py` (mirror prüfen),
`webui/src/lib/connectionState.js`-Tests **nur lesen** (Vertrag)

- **Sessions — ein einziger Emit-Punkt:** in `_create_session` (`agent_methods.py`)
  `resource_changed(kind="sessions", scope={agent_id})` emittieren. **`/new` und `/handoff` rufen
  intern bereits `_create_session` auf** — sie sind damit automatisch abgedeckt; **`chat_methods.py`
  muss dafür nicht angefasst werden** (deshalb keine Datei-Überschneidung mit 2A → 2A und 2B bleiben
  parallel). Bei `make_current` (identity) trägt dasselbe `sessions`-Event; der Client frischt
  `session.list` + die „aktuell"-Markierung auf.
- **Verhalten (Entscheidung Plan-Review): andere Fenster bleiben stehen.** Das `sessions`-Event löst in
  anderen Fenstern **nur** ein Auffrischen der Liste/Markierung aus — **kein** automatischer Wechsel in
  die neu erzeugte Session. (Client-Seite siehe Task 2C.)
- **Agenten-CRUD umstellen:** `_publish_agent_event`-Aufrufe (create/update/delete in `agent_methods.py`)
  auf `resource_changed(kind="agents")` umstellen. `agent.created/updated/deleted` aus
  `ALLOWED_SERVER_EVENT_TYPES` entfernen, sobald kein Sender/Empfänger mehr darauf hört.
- **Kante (verifizieren):** `current_session_id` wechselt **auch** über `_update_agent` (nicht nur über
  `_create_session`) — das läuft dann über `kind:"agents"`, nicht `kind:"sessions"`. Builder prüft, ob
  die WebUI die aktuelle Session überhaupt so wechselt, und stellt sicher, dass die „aktuell"-Markierung
  in anderen Fenstern auch über den Agenten-Reload korrekt nachzieht (sonst hängt sie, wenn der Wechsel
  per `agent.update` statt per Erzeugung kommt).
- Tests: Session-Erzeugung publiziert ein `sessions`-Event; Agenten-CRUD publiziert nun `agents`-
  `resource_changed`.

**Done when:** Session-Erzeugung/-Wechsel und Agenten-CRUD laufen als `resource_changed`; alte
`agent.*`-Typen sind entfernt.

## Task 2C — Client: Funnel um `queue`/`sessions`/`agents` erweitern + Verbraucher
*sequenziell nach 2A/2B (gemeinsamer Funnel + Vertrag)*

**read:** `.vorch/domain-maps/webui.md`
**files:** `webui/src/App.svelte`, `webui/src/lib/chatState.js`,
`webui/src/components/ChatView.svelte`, `webui/src/components/SessionListDrawer.svelte`,
`webui/src/lib/sessionListView.js`, jeweilige `__tests__`

- `handleServerEvent`-Dispatch (aus Teil 1) um `kind`-Fälle erweitern:
  - `agents` → bestehender Agenten-Reload (`agent.list` + `refreshAgents`) — ersetzt den alten
    `agent.created/updated/deleted`-Branch (der gleiche Effekt, neuer Auslöser).
  - `sessions` → **nur** Session-Liste/„aktuell"-Markierung der betroffenen Agenten neu laden
    (Drawer/ChatView). **Kein automatischer Wechsel** in die neue Session — das gerade betrachtete
    Gespräch bleibt stehen (Entscheidung „Stehen bleiben"); laufende Run-Anzeige unberührt.
  - `queue` (mit Session-Scope) → `syncSessionQueue` für die betroffene Session anstoßen, statt nur
    auf Terminal-Events zu warten. Nur reagieren, wenn der Scope eine gehaltene Session betrifft.
- Agenten-Reload-Routing wird in **`App.test.js`** getestet (die `agent.list`-Mocks dort) — die neue
  `agents`-`kind`-Form dort abdecken. (Die `agent.*`-Strings in `connectionState.test.js` sind nur
  Sequenz-/Durchreich-Fixtures und prüfen **kein** Agenten-Verhalten — sie laufen mit jedem Event-Typ;
  optional mitziehen, aber sie sind nicht das Netz.)
- Tests: ein `queue`-Event aktualisiert die Warteschlange der betroffenen Session live; ein `sessions`-
  Event frischt die Liste auf **ohne** das betrachtete Gespräch zu wechseln; `agents` verhält sich wie bisher.

**Done when:** Eine in Fenster A (Browser) eingereihte Nachricht erscheint in Fenster B (gleiche
Session) ohne Warten auf Run-Ende; eine neue Session in A taucht in B's **Liste** auf, ohne B's
betrachtetes Gespräch zu wechseln; Agenten-Reload funktioniert unverändert über den neuen Kanal.

---

**Done when (Teil 2 gesamt):**
- Über den Browser eingereihte Nachrichten und neue/gewechselte Sessions sind live fensterübergreifend
  konsistent (andere Fenster frischen Listen auf, ohne das betrachtete Gespräch zu wechseln).
- Agenten-CRUD läuft über `resource_changed`; keine `agent.*`-Spezial-Typen mehr.
- `provider_auth_completed` bleibt ein gezieltes Event; die `providers`-Invalidierung beim OAuth-
  Abschluss ist in **Teil 1** gesetzt (Entscheidung aufgelöst, siehe README).
- Backend- und Frontend-Gates grün.
