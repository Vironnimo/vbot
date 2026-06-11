# Extensions — Bestandsaufnahme & Ausbaurichtung

Arbeitsnotiz (Stand 2026-06-11): Wo das Extension-System heute steht, was ihm zu einem
echten Agent-Harness-Hook-System fehlt, und in welchen Stufen wir es ausbauen können.

Quellen: `.vorch/specs/extensions.md`, `core/extensions/extensions.py`,
Dispatch in `core/chat/chat.py` und `core/chat/tool_dispatch.py`.

## Wo wir stehen

Der Loader ist solide und klein: Discovery aus `<data_dir>/extensions/` plus
`settings.json` → `extension_directories`, drei Entry-Point-Formen (Single-File, Package,
`extension.py`-Fallback), `register(api)` sync oder async, fail-open. Daran ist wenig
auszusetzen.

Die Schwächen liegen auf der Hook-Seite. Heute gibt es sechs Events, alle chat-zentriert:
`run_start`, `before_agent_start`, `context`, `tool_call`, `tool_result`, `run_end`.

### 1. Eingreifen ist lückenhaft

Abfangen, verändern, austauschen — geht nur teilweise:

- `tool_call` kann einen Call nur **komplett schlucken** (Result-Envelope zurückgeben,
  Tool läuft nie). Es gibt keinen Weg, die **Tool-Argumente zu modifizieren** und das
  Tool trotzdem laufen zu lassen, und kein sauberes "deny mit Begründung" — man muss
  einen Fehler-Envelope von Hand basteln. Zum Vergleich: Claude-Code-Hooks haben
  `allow`/`deny`/`ask` plus `updatedInput` als explizite Entscheidungstypen.
- `context` kann die Message-Liste ersetzen, aber **first-wins mit `break`**
  (`core/chat/chat.py`, context-Loop) — zwei Extensions, die beide den Kontext anfassen
  wollen, schließen sich gegenseitig aus.
- `run_start`/`run_end` sind reine Observer, ohne `run_id` im Context (`HookContext`
  enthält nur `session_id`/`agent_id`).

### 2. Jede Hook hat eine andere Kompositions-Semantik

`before_agent_start` akkumuliert, `context` first-wins-skip-rest, `tool_call`
first-valid-wins, `tool_result` jeder-patcht-nacheinander. Das ist historisch gewachsen,
nicht designt. Ein kohärentes Modell wäre eine Pipeline: jeder Handler bekommt den
aktuellen Zustand und gibt optional einen modifizierten zurück.

### 3. Dispatch ist fünfmal kopiert und koppelt ans private Dict

`core/chat/` iteriert direkt über `registry._handlers` in fünf fast identischen
Inline-Loops; `ExtensionRegistry.fire()` ist dead code. Die Spec dokumentiert die
Dict-Shape sogar als "load-bearing contract" — das ist ein Refactoring-Ziel unabhängig
von jeder Feature-Richtung.

### 4. Keine Identität, keine Sichtbarkeit

Kein Manifest (Name/Version/Beschreibung), keine per-Extension-Config, kein
enable/disable, kein RPC/CLI/UI-Weg zu sehen, was geladen ist oder was beim Laden
gefailt hat (Skills haben `invalid_diagnostics()`, Extensions loggen nur). Reihenfolge
ist alphabetisch, fertig.

### 5. Die Oberfläche endet am Chat-Loop

Kein Hook für Channel-Ingest (z.B. Telegram-Nachricht vorverarbeiten, bevor sie zum Run
wird), nichts bei Compaction, kein startup/shutdown für Extensions, die eigene Ressourcen
halten (DB-Connection, HTTP-Client). Und — der größte Punkt — **Extensions können keine
Tools registrieren**, obwohl `ToolRegistry.register()` existiert und das für ein
Agent-Harness der naheliegendste Extensibility-Wunsch ist.

## Ausbau in drei Stufen

### Stufe 1 — den Hook-Kern reif machen

Adressiert direkt "abfangen, verändern, austauschen":

- **Typed Dispatcher** in `core/extensions/` mit einer Methode pro Event
  (`hooks.tool_call(ctx, ...)`, `hooks.filter_context(ctx, messages)`), der die Semantik
  zentral besitzt. Chat ruft eine Methode statt fünf kopierter Loops; die
  `_handlers`-Kopplung verschwindet.
- **Decision-Modell für `tool_call`**: Handler geben `continue` (default),
  `modify(input)`, `deny(reason)` oder `replace(result)` zurück. `deny` wird ein
  Fehler-Envelope mit der Begründung, sodass das Modell weiß, warum. `modify` wird
  gechained — jeder Handler sieht den aktuellen Input.
- **Pipeline statt first-wins** für `context`, einheitlich mit `tool_result`.
- `HookContext` um `run_id` und ein paar Capabilities erweitern (z.B. `ctx.add_note(...)`
  für System Reminders, Logger).

### Stufe 2 — Oberfläche erweitern

- `api.register_tool(name, description, parameters, handler)` — Extensions liefern
  eigene Tools, landen in der normalen `ToolRegistry`, Agent-Allowlists greifen wie
  gewohnt. Der größte Einzelgewinn: eigene Tools ohne App-Fork, und es passt zur
  Trennung Skill (Markdown-Playbook) vs. Tool (Code).
- Neue Events nur wo konkreter Bedarf ist: `message_received` (Channel-Ingest),
  `startup`/`shutdown`.

### Stufe 3 — "richtiges" Plugin-System

- Manifest pro Extension (Name, Version, Config-Schema), per-Extension-Config-Section in
  `settings.json`, die in `register()` hereingereicht wird, enable/disable,
  Load-Diagnostics über RPC → CLI (`vbot extensions list`) und eine kleine WebUI-Seite.
- Optional später: pip-installierbare Plugins via Entry-Point-Group.
- **Nicht** machen: Sandboxing, out-of-process Plugins, Marketplace-Gedanken. Extensions
  teilen bewusst die Trust-Boundary des Kernels — konsistent mit der Projektphilosophie
  (single-user, maximale Agency), steht so auch in der Spec.

## Empfehlung

Stufe 1 zuerst — sie ist die Grundlage für alles Weitere, räumt nebenbei den Chat-Code
auf, und genau dort liegt der Abstand zwischen "wir haben Hooks" und "es ist ein
Agent-Harness-Hook-System". `register_tool` danach als eigenständiger, ziemlich billiger
Win. Stufe 3 erst, wenn es real mehr als eine Handvoll Extensions gibt.

Sinnvoller Plan-Schnitt: Dispatcher-Refactor → Decision-Modell → `register_tool`.
