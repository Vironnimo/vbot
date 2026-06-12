# Extensions — Designnotiz

Stand 2026-06-12: Die Bestandsaufnahme (unten) wurde zusammen durchgearbeitet, die
Designentscheidungen sind gefallen, und die Umsetzung ist als Plan-Suite geschnitten:
**`docs/plans/extensions/README.md`** (lokal, nicht committet) — fünf session-große
Pläne, ein Agent, sequentiell.

Quellen: `.vorch/specs/extensions.md`, `core/extensions/extensions.py`,
Dispatch in `core/chat/chat.py` und `core/chat/tool_dispatch.py`.

## Entschiedenes Design

**Ein Extension-Begriff, mehrere Capability-Oberflächen.** Hooks sind kein eigenes
System — sie sind eine Capability unter mehreren, die eine Extension nutzen kann (wie
bei pytest-Plugins, VS-Code-Extensions, Claude-Code-Plugins). Die Extension bleibt die
Einheit von Discovery, Identität, Config und enable/disable; `register(api)` wird zur
Fassade über alle Extension Points. Intern bleiben die Domain-Registries
(`ToolRegistry`, `RecallBackendRegistry`) — die Extension-API routet hinein, kein neuer
God-Registry.

Festgezurrte Entscheidungen (Details und Begründungen im Plan-Suite-README):

1. Begriff bleibt **Extension** (kein "Plugin" daneben).
2. **Dateisystem-Identität** (Name = Verzeichnis-/Dateiname), `extension.json` als
   optionales Manifest (Version, Beschreibung, `api_version`) — nie Pflicht,
   Single-File-Extensions bleiben erstklassig.
3. **Zweiphasige Registrierung:** `register(api)` sammelt nur Deklarationen; die Runtime
   wendet sie an den richtigen Bootstrap-Punkten an. Async `register()` wird
   deterministisch awaited (kein fire-and-forget mehr).
4. **Dispatch-Semantik wandert nach `core/extensions/`** (Inversion der alten
   Spec-Regel): chat besitzt nur noch *wann* gefeuert wird und was Payloads fachlich
   bedeuten.
5. **Kompositionsmodell pro Event, designt statt gewachsen:** Observer
   (`run_start`/`run_end`), Akkumulator (`before_agent_start`), Pipelines (`context`,
   `tool_result`), Decision-Pipeline (`tool_call` mit `Deny(reason)` / `Modify(input)` /
   `Replace(result)` / `None`=continue).
6. **Public-API-Disziplin:** Die Extension-API ist vBots erste echte Public API. Typisierte
   Objekte statt loser Dicts, wenige gut geschnittene Events statt spekulativer Breite,
   `API_VERSION = 1` von Anfang an. Bis zur Stabil-Erklärung darf der Hook-Contract noch
   frei brechen.
7. **Trust-Boundary unverändert:** in-process, Kernel-Trust. Kein Sandboxing, kein
   out-of-process, kein Marketplace.
8. **Enable/disable ist restart-applied;** deaktivierte Extensions werden nie importiert.
   Kein Hot-Reload.
9. **Kein Core-Slimming jetzt.** Built-in-Extensions (`resources/extensions/`-Root) und
   Extraktionen (Home Assistant, vector/hybrid-Recall, TTS/Image-Tools) sind bewusst auf
   eine spätere Initiative verschoben — diese Suite baut nur das System, das jene
   Migrationen dann benutzen. Test-/Beispiel-Extensions validieren die API stattdessen.

## Plan-Suite (Status)

| # | Plan | Liefert | Status |
|---|---|---|---|
| 1 | Typed Dispatcher | Dispatch zentral in `core/extensions/`, chat ohne `_handlers`, erste direkte Tests | erledigt |
| 2 | Decision-Modell | `Deny`/`Modify`/`Replace`, Pipeline-Semantik, `HookContext` + `run_id`/`add_note` | erledigt |
| 3 | Registrierung | Zweiphasig, Records/Diagnostics, Manifest, enable/disable + Config, startup/shutdown | erledigt |
| 4 | Capabilities | `register_tool`, `register_recall_backend`, Beispiel-Extensions | offen |
| 5 | Sichtbarkeit | `extensions.list` RPC, CLI, WebUI-Panel, Autoren-Doku | offen |

## Bestandsaufnahme (2026-06-11)

> Plan 1–3 sind gelandet. Punkte 1–3 unten (lückenhaftes Eingreifen, uneinheitliche
> Kompositions-Semantik, kopierter Dispatch): Dispatch lebt zentral in `core/extensions/`,
> jedes Event hat eine designte Kompositionsregel, und `tool_call` kann via
> `Modify`/`Deny`/`Replace` modifizieren, ablehnen und ersetzen. Punkt 4 (Identität): Plan 3
> brachte zweiphasige Registrierung, `ExtensionRecord` + `diagnostics()`, optionales
> `extension.json`-Manifest, enable/disable + per-Extension-Config aus `settings.extensions`
> und startup/shutdown-Lifecycle — die *Sichtbarkeit* (RPC/CLI/UI) bleibt für Plan 5. Von
> Punkt 5 fehlt noch die Capability-Oberfläche (Tools/Recall-Backends registrieren, Plan 4).
> Der Rest ist historischer Kontext.


Der Loader ist solide und klein: Discovery aus `<data_dir>/extensions/` plus
`settings.json` → `extension_directories`, drei Entry-Point-Formen (Single-File, Package,
`extension.py`-Fallback), `register(api)` sync oder async, fail-open. Daran ist wenig
auszusetzen.

Die Schwächen liegen auf der Hook-Seite. Heute gibt es sechs Events, alle chat-zentriert:
`run_start`, `before_agent_start`, `context`, `tool_call`, `tool_result`, `run_end`.

### 1. Eingreifen ist lückenhaft

- `tool_call` kann einen Call nur **komplett schlucken** (Result-Envelope zurückgeben,
  Tool läuft nie). Kein Weg, **Tool-Argumente zu modifizieren** und das Tool trotzdem
  laufen zu lassen; kein sauberes "deny mit Begründung". Zum Vergleich: Claude-Code-Hooks
  haben `allow`/`deny`/`ask` plus `updatedInput` als explizite Entscheidungstypen.
- `context` kann die Message-Liste ersetzen, aber **first-wins mit `break`** — zwei
  Extensions, die beide den Kontext anfassen wollen, schließen sich gegenseitig aus.
- `run_start`/`run_end` sind reine Observer, ohne `run_id` im Context (`HookContext`
  enthält nur `session_id`/`agent_id`).

### 2. Jede Hook hat eine andere Kompositions-Semantik

`before_agent_start` akkumuliert, `context` first-wins-skip-rest, `tool_call`
first-valid-wins, `tool_result` jeder-patcht-nacheinander. Historisch gewachsen, nicht
designt.

### 3. Dispatch ist sechsmal kopiert und koppelt ans private Dict

`core/chat/` iteriert direkt über `registry._handlers` in sechs fast identischen
Inline-Loops (vier in `chat.py`, zwei in `tool_dispatch.py`);
`ExtensionRegistry.fire()` ist dead code. Die Spec dokumentiert die Dict-Shape sogar als
"load-bearing contract". Außerdem: `core/extensions/` hat **null direkte Tests**
(kein `tests/core/extensions/`).

### 4. Keine Identität, keine Sichtbarkeit

Kein Manifest, keine per-Extension-Config, kein enable/disable, kein RPC/CLI/UI-Weg zu
sehen, was geladen ist oder was beim Laden gefailt hat (Skills haben
`invalid_diagnostics()`, Extensions loggen nur). Reihenfolge ist alphabetisch, fertig.

### 5. Die Oberfläche endet am Chat-Loop

Kein startup/shutdown für Extensions mit eigenen Ressourcen, kein Hook für
Channel-Ingest, nichts bei Compaction. Und — der größte Punkt — **Extensions können
keine Tools registrieren**, obwohl `ToolRegistry.register()` existiert; auch
`RecallBackendRegistry` ist für Extensions unerreichbar, obwohl die Recall-Spec
"extension backends" bereits erwähnt.
