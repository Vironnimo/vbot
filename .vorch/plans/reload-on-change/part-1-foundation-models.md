# Teil 1 — Fundament + Modellkatalog (erster Nutzer)

> Lies zuerst die [README](README.md) (Kontext + Architektur-Entscheidungen) und
> [`stuff/reload-changed.md`](../../../stuff/reload-changed.md). Du bist der **Orchestrator** für
> diesen Teil — verteile die Tasks an Subagents (`⚡` = parallel erlaubt, keine Datei-Überschneidung).

**Ziel des Teils:** Der generische Invalidierungs-Kanal existiert end-to-end, und der **Modellkatalog**
ist sein erster Verbraucher — damit ist der Ausgangs-Bug behoben (Modell-DB-Refresh per CLI/UI lässt
alle offenen Modell-Auswahllisten nachladen, ohne Tab-Wechsel).

**Abhängigkeiten:** keine (Fundament).

---

## Task 1A — Server: Kanal + Emit-Stellen für Modelle/Provider ⚡
*parallel mit 1B und 1C (disjunkte Dateien)*

**read:** `.vorch/domain-maps/server.md`, `.vorch/domain-maps/models.md`, `.vorch/domain-maps/providers.md`
**files:** `server/events.py`, `server/rpc/event_bridge.py`, `server/rpc/connection_methods.py`,
`tests/server/test_events.py` (o. mirror), `tests/server/test_rpc.py`

- In `server/events.py`: neues Event `resource_changed` (Konstante + in `ALLOWED_SERVER_EVENT_TYPES`).
  Nutzlast-Vertrag: `{ kind, scope? }`, `kind ∈ {models, queue, sessions, providers, clients}`.
- In `event_bridge.py`: Publish-Helfer `publish_resource_changed(state, kind, *, scope=None)` analog
  zu `_publish_provider_auth_completed_event` (No-op ohne `event_bus`).
- **Modelle:** `resource_changed(kind="models")` nach erfolgreichem Reload emittieren. **Emit-Stelle ist
  der RPC-Handler `_refresh_model_db(state, …)`** (er hat `state`/`event_bus`) — **nicht** in den inneren
  Funktionen `_refresh_global_model_db`/`_refresh_provider_model_db`, die nur `runtime` bekommen und
  keinen Bus-Zugriff haben. **Achtung — zwei Rückgabepfade:** `_refresh_model_db` kehrt für den
  Pro-Provider-Fall **früh zurück** (`return await _refresh_provider_model_db(…)`), der globale Fall
  fällt erst am Ende durch. **Beide** müssen emittieren (am saubersten beide Zweige in ein Ergebnis +
  *ein* gemeinsames Tail-Emit zusammenführen) — sonst feuert ein Pro-Provider-Refresh kein Signal.
- **Provider (synchron):** in `_set_provider_key`/`_unset_provider_key` (nach `reload_provider_credentials`)
  **und** in `_disconnect_provider` `resource_changed(kind="providers")` emittieren — diese Aktionen
  ändern sofort, welche Modelle auswählbar sind.
- **Provider (OAuth-Login):** **nicht** in `_connect_provider` selbst (dort wird der Device-Flow nur
  *gestartet* — die Verbindung steht noch nicht). Stattdessen im **Completion-Callback** (`on_complete`,
  Erfolgsfall), **neben** dem bestehenden `provider_auth_completed`, zusätzlich
  `resource_changed(kind="providers")` emittieren. `provider_auth_completed` bleibt unverändert.
- Tests: Event-Typ akzeptiert/abgewiesen korrekt; Refresh-, Key-/Disconnect-Handler **und** der OAuth-
  Completion-Callback publizieren das erwartete `resource_changed` (Bus-Spy wie in den vorhandenen RPC-Tests).

**Done when:** `model.refresh_db` (global + pro Provider) emittiert `resource_changed(kind="models")`;
`provider.set_key/unset_key/disconnect` **und** der **OAuth-Login-Abschluss** emittieren
`resource_changed(kind="providers")` (Connect-*Start* nicht); Gates grün.

## Task 1B — Client: Funnel-Branch + Anwende-Helfer ⚡
*parallel mit 1A und 1C (disjunkte Dateien)*

**read:** `.vorch/domain-maps/webui.md`
**files:** `webui/src/App.svelte`, `webui/src/lib/resourceInvalidation.js` (neu),
`webui/src/lib/__tests__/resourceInvalidation.test.js` (neu)

- Neuer Helfer `resourceInvalidation.js`: (a) Kind→Token-Verwaltung (reaktive Tokens pro `kind`),
  (b) Anwende-Helfer „reload-or-defer": reine Anzeigen sofort; bei Picker/Formular den sichtbaren
  Tausch aufschieben, solange „in Bearbeitung" gemeldet wird (Dropdown offen / Fokus / ausstehender
  Save). Reiner, testbarer Helfer (kein Svelte).
- In `App.svelte` → `handleServerEvent`: Branch `event.type === 'resource_changed'`. Dispatch nach
  `kind`: Token erhöhen und als Prop an die betroffenen Views reichen (genau wie `agentsRefreshToken`).
  Für `kind: "models"` und `kind: "providers"` einen `modelsRefreshToken` erhöhen (beide betreffen die
  Modell-/Verbindungsverfügbarkeit). **Vertrag für 1C:** Prop-Name `modelsRefreshToken` an
  `AgentsView`, `ProjectsView`, `SettingsView` (→ Defaults/Compaction/Specialized/Providers).
- Tests: Helfer-Logik (sofort vs. aufschieben); `handleServerEvent` erhöht bei `resource_changed`
  den richtigen Token (App-Routing-Test wie bei den Agenten-Events).

**Done when:** ein eingehendes `resource_changed(kind:"models"|"providers")` erhöht den
`modelsRefreshToken`; Anwende-Helfer ist unit-getestet; Gates grün.

## Task 1C — Client: Modell-Flächen laden bei Token-Änderung neu
*sequenziell nach 1B (gemeinsamer Prop-Vertrag); berührt aber nur View-Dateien, nicht `App.svelte`*

**read:** `.vorch/domain-maps/webui.md`, `.vorch/domain-maps/models.md`
**files:** `webui/src/components/AgentsView.svelte`, `webui/src/components/ProjectsView.svelte`,
`webui/src/components/SettingsView.svelte`,
`webui/src/components/settings/SettingsDefaultsPanel.svelte`,
`webui/src/components/settings/SettingsCompactionPanel.svelte`,
`webui/src/components/settings/SettingsSpecializedModelsPanel.svelte`,
`webui/src/components/settings/SettingsProvidersPanel.svelte`, jeweilige `__tests__`

- Jede Fläche nimmt `modelsRefreshToken` als Prop und lädt bei Änderung neu, **was sie heute beim
  Mounten lädt** — **Builder verifiziert pro Datei die exakte Lade-Funktion, nicht raten:**
  - **AgentsView, ProjectsView, Defaults-Panel, Compaction-Panel:** laden beim Mounten `model.list`
    **und** `connection.list` → beide erneut laden.
  - **Specialized-Models-Panel:** lädt `task_model.list_targets`/`options` → erneut laden.
  - **Providers-Panel:** lädt **weder** `model.list` **noch** `connection.list` beim Mounten — seine
    Anzeige kommt aus dem `settings`-Prop. Auf den Token hin spiegelt es eine Provider-Änderung per
    **Settings-Reload** (vorhandener `onReloadSettings`-Pfad), nicht per `connection.list`.
- Anwende-Verhalten über den Helfer aus 1B: die vier Modell-Picker (Agents/Projects/Defaults/Specialized)
  → sichtbaren Tausch aufschieben, solange in Bearbeitung. **Auch das Providers-Panel ist ein Formular**
  (API-Schlüssel-Eingabe, Refresh-Knopf) — Reload nie mitten in eine laufende Eingabe; nicht „sofort
  wegtauschen".
- `SettingsView.svelte` reicht den Token an seine Panels weiter.
- Tests: bei Token-Änderung wird neu geladen; offene Auswahl/laufende Eingabe wird nicht weggerissen.

**Done when:** Nach einem `model.refresh_db` in einem anderen Accessor zeigt ein offenes Fenster die
neuen Modelle ohne Tab-Wechsel; eine offene Modellauswahl wird dabei nicht zugeklappt; Gates grün.

---

**Done when (Teil 1 gesamt):**
- Server publiziert `resource_changed` für Modell-/Provider-Änderungen.
- Offene Modell-Auswahllisten (Agenten, Projekte, Standard, Spezial) aktualisieren sich live nach
  einem Refresh in einem anderen Accessor, schonend (kein Wegspringen).
- `python scripts/quality.py server/` und `python scripts/quality-frontend.py webui/src/` grün.
