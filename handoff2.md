# Handoff 2 — Restbefunde aus der Core-Bug-Suche (2026-06-10)

Kontext: Bug-Suche in `core/chat`, `core/providers`, `core/models`. Die drei kritischen Befunde
(Dangling tool_calls bricken Sessions; `temperature: null` auf dem Draht + tote Provider-Defaults;
unklassifizierte httpx-Transport-Fehler) sind **nicht** hier — die hat
`docs/plans/core-critical-bugfixes.md`. Dieses Dokument sammelt die mittleren und kleinen Befunde
für später. Jeder Eintrag ist eigenständig fixbar; Domain-Spec nach dem Fix aktualisieren
(`.vorch/specs/chat.md` bzw. `providers.md`).

## Mittel

### M1 — Anthropic: `temperature` + Thinking-Konflikt nicht behandelt
`_apply_anthropic_reasoning` (`core/providers/anthropic.py:807`) aktiviert Thinking
(`thinking: {type: adaptive}` + `output_config.effort`), lässt aber eine gesetzte Agent-Temperature
im Payload. Anthropic lehnt für Sonnet-Klasse-Modelle Temperature ≠ 1 bei aktivem Thinking ab.
Der GitHub-Copilot-Messages-Pfad behandelt genau das: er lässt Temperature bei aktivem adaptivem
Thinking für Sonnet weg (`core/providers/github_copilot_messages.py`, Tests
`test_build_payload_omits_temperature_for_sonnet_when_adaptive_thinking_is_active` und
`..._keeps_temperature_for_haiku...`). Der direkte Anthropic-Adapter braucht dieselbe Policy.
Repro: Agent mit `temperature: 0.5` + `thinking_effort: high` → vermutlich 400.
**Fix-Richtung:** In `_apply_anthropic_reasoning` (oder `_build_payload`) Temperature aus den
kwargs entfernen, wenn Thinking aktiviert wird — Modell-Differenzierung analog Copilot-Policy prüfen.

### M2 — Modell-Fallback sendet `reasoning_meta` des Primär-Providers an den Fallback-Provider
Der In-Run-Fallback-Pfad (`core/chat/chat.py:434-444`) re-used die live mutierte `messages`-Liste.
Die enthält `_assistant_continuation_dict`-Einträge mit `reasoning`/`reasoning_meta` des
Primär-Providers. Anthropic und OpenAI lesen nur ihre eigenen Meta-Keys (harmlos), aber
OpenAI-kompatibel und OpenRouter teilen sich den Key `reasoning_details`
(`OPENAI_REASONING_META_KEYS` in `core/providers/openai_compatible.py:50`) → Cross-Provider-Replay
möglich. Verstößt gegen die Spec-Regel `.vorch/specs/chat.md:71` („stale reasoning_meta from the
old provider must never be sent to the new provider“). History-Rebuilds für *neue* Runs strippen
korrekt via `_message_to_request_dict` — nur der In-Run-Fallback leakt.
**Fix-Richtung:** Beim Fallback-Wechsel `reasoning`/`reasoning_meta` aus den Assistant-Einträgen
der weiterverwendeten `messages`-Liste strippen (Provider wechselt → Meta ist per Definition stale).

### M3 — `_sync_skill_context_messages` fügt stur bei Index 1 ein
`core/chat/tool_dispatch.py:491-502`: neue Skill-Kontexte werden mit `messages.insert(1, ...)`
eingefügt. Zwei Probleme: (a) Bei leerem System-Prompt enthält der Request **keine**
System-Message (`core/chat/chat.py:528-532`) → Skill-Kontext landet hinter der ersten
History-Message statt am Anfang. (b) Mehrere neue Skill-Kontexte landen in umgekehrter
Aktivierungs-Reihenfolge (jeder insert(1) schiebt den vorherigen nach hinten).
**Fix-Richtung:** Einfügeposition dynamisch bestimmen (nach System-Message, falls vorhanden, sonst
Index 0; hinter bereits vorhandene Skill-Kontexte).

### M4 — Stream-Connect-Retry nutzt veraltete Header/Token
In `stream()` werden Header/Payload einmal **außerhalb** der Retry-Closure gebaut
(`core/providers/openai_compatible.py:341`, `core/providers/anthropic.py:349`,
`core/providers/openai.py:313` `_connect_stream`); `send()` baut die Header dagegen pro Versuch
neu. Nach einem 429-Backoff über das OAuth-Refresh-Fenster hinweg verwendet der Reconnect den
abgelaufenen Token. Spec-Hinweis providers.md: „OAuth tokens may refresh during requests through
OAuthTokenGetter, so do not cache the raw OAuth access token outside the getter“ — genau das
passiert hier faktisch pro Stream-Aufbau.
**Fix-Richtung:** `_build_headers()` in die Connect-Closure ziehen (pro Versuch aufrufen), Payload
kann draußen bleiben. Auch `github_copilot.py` prüfen.

### M5 — Discovery überstempelt Override-only-Modelle mit der refreshenden Connection
`core/models/discovery.py`: `apply_overrides` läuft vor dem Tagging; `_tag_fresh_models`
(`discovery.py:367-387`) setzt `connections: [<conn>]` auf **alle** frischen Einträge — auch auf
Override-only-Modelle. Deren eigenes `connections`-Feld aus der Override-Datei (oder die
„alle Connections“-Semantik bei fehlendem Feld) wird überschrieben; der Tag flippt bei jedem
Refresh auf die zuletzt refreshende Connection.
**Fix-Richtung:** Override-only-Modelle (nicht im Discovery-Resultat enthalten) vom Tagging
ausnehmen bzw. ein in der Override-Datei deklariertes `connections` respektieren.

## Klein

### K1 — `_extract_openai_tool_calls` droppt Tool-Calls mit kaputtem Argument-JSON still
`core/providers/openai_compatible.py:692-694`: bei nicht parsebarem `arguments`-JSON wird der
Tool-Call per `continue` verworfen. Kann eine Assistant-Message ohne content/tool_calls erzeugen →
verwirrender `ChatMessageValidationError` statt eines Provider-Fehlers. Der Streaming-Pfad wirft
in diesem Fall sauber `StreamingDeltaError` (`core/chat/streaming.py:92-98`) — inkonsistent.
**Fix-Richtung:** Im Non-Streaming-Pfad ebenfalls einen klassifizierten Fehler werfen (fataler
`ProviderError` mit Argument-Preview), nicht still droppen.

### K2 — `_merge_stream_fragment`: Duplikat-Chunk wird verschluckt
`core/chat/streaming.py:289-297`: die Kumulativ-Heuristik (`delta.startswith(existing)` →
Replacement) ist absichtlich und getestet
(`tests/core/chat/test_streaming.py:183`), hat aber eine Kante: ein inkrementelles Delta, das
exakt dem bisher akkumulierten Präfix entspricht (repetitiver Inhalt), wird als kumulativer
Resend interpretiert und verschluckt → Datenverlust in Tool-Argumenten.
**Fix-Richtung:** Nur dokumentieren/akzeptieren oder Heuristik auf Adapter-Opt-in umstellen
(nur Provider, die wirklich kumulativ senden).

### K3 — Hartkodiertes deutsches `[Bild: …]` im Block-Resolver
`core/chat/block_resolver.py:105`: historische Media-Blocks werden zu `[Bild: {filename}]`,
File-Blocks daneben zu englischem `[File: … — Path: …]` (`:129`). Inkonsistent; Projektregel ist
i18n für alles Nutzersichtbare (hier model-facing, aber einheitlich englisch wäre korrekt).
**Fix-Richtung:** `[Image: {filename}]`.

### K4 — TokenStore-Dateinamen können kollidieren
`core/providers/token_store.py:104`: Pfad ist `{provider_id}-{connection_id}.json`; beide IDs
dürfen Bindestriche enthalten → Provider `a-b` + Connection `c` und Provider `a` + Connection
`b-c` ergeben dieselbe Datei `a-b-c.json`. Aktuell kein realer Konflikt im Provider-Bestand,
aber latent.
**Fix-Richtung:** Trennzeichen wählen, das im ID-Pattern nicht vorkommt (z. B. `--` ist auch
erlaubt… besser: Unterordner `oauth/<provider>/<connection>.json`).

### K5 — 504 (und 500) nicht retryable
`core/providers/_http_shared.py:33`: `_RETRYABLE_STATUS_CODES = {429, 502, 503}`. 504 Gateway
Timeout ist klassisch transient und fehlt; 500 ist diskutabel.
**Fix-Richtung:** 504 aufnehmen; 500 bewusst entscheiden und im Spec dokumentieren.

### K6 — `_parse_tool_calls` filtert leere `{}`-Einträge still
`core/chat/messages.py:645-650`: `[ToolCall.from_dict(item) for item in value if
_is_tool_call_object(item)]` — `_is_tool_call_object` gibt das Dict zurück; ein leeres `{}` ist
falsy und wird gefiltert statt mit „id must be a non-empty string“ abgelehnt.
**Fix-Richtung:** Validator soll `True`/Exception liefern statt das (möglicherweise leere) Dict.

## Geprüft & sauber (kein Handlungsbedarf)

- `NetworkError` ist korrekt **kein** `ProviderError` → triggert kein Modell-Fallback
  (`core/providers/errors.py:12`, `_is_model_fallback_trigger` in `core/chat/events.py:117`).
- Cancellation läuft als `asyncio.CancelledError` (`core/runs/runs.py:221-224`) und wird im
  Chat-Loop separat behandelt — keine fälschlich persistierten Error-Messages bei Cancel.
- Cross-Provider-`reasoning_meta` wird bei History-Rebuilds für neue Runs korrekt gestrippt
  (`_message_to_request_dict`); nur der In-Run-Fallback leakt (→ M2).
- OpenAI-Discovery: `discovery_headers` verlangt eine ChatGPT-Account-ID, aber nur die
  `subscription`-Connection hat ein `models_endpoint` — API-Key-Discovery läuft nie in den Pfad.
- `core/models` (Registry, Query, Catalog-Load) ohne Befund; `apply_overrides`/Merge bis auf M5 ok.
- `ProviderRegistry`-Parsing/Validierung (`core/providers/providers.py`) ohne Befund.
