# Usage

Diese Datei beschreibt, wie man den aktuellen Stand von vBot tatsächlich
benutzt.

Wichtig: **Backend und Server funktionieren bereits, das echte Web-Chat-UI noch
nicht.** Wenn du im Browser etwas Interaktives erwartest, ist das aktuell noch
Phase 4. In `webui/` gibt es im Moment nur ein minimales Frontend-Scaffold.

## 1. Voraussetzungen

- Python **3.11+**
- Node.js
- mindestens ein API-Key für einen konfigurierten Provider

## 2. Setup

### Dependencies installieren

```bash
pip install -e ".[dev]"
```

### Frontend-Abhängigkeiten

```bash
cd webui
npm install
cd ..
```

## 3. Datenverzeichnis und Konfiguration

Standardmäßig benutzt vBot das Datenverzeichnis:

```text
~/.vbot
```

Darin liegen unter anderem:

- `.env` — API-Keys
- `settings.json` — Instanz-Einstellungen
- `extensions/` — lokale Python-Hooks und Erweiterungen
- `agents/` — Agent-Konfigurationen
- `workspace-<agent-id>/` — Agent-Workspaces

### Beispiel für `.env`

```env
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
ANTHROPIC_API_KEY=...
```

### Beispiel für `settings.json`

```json
{
  "server_port": 8420
}
```

Die Port-Reihenfolge ist:

1. `--port`
2. `VBOT_SERVER_PORT`
3. `settings.json`
4. `8420`

### Extensions und Hooks laden

Beim Start scannt vBot automatisch dieses Verzeichnis:

```text
~/.vbot/extensions/
```

Zusätzliche Extension-Roots kannst du in `settings.json` über
`extension_directories` eintragen:

```json
{
  "server_port": 8420,
  "extension_directories": [
  "~/vbot-exts"
  ]
}
```

Unterstützte Entry-Point-Formen pro unmittelbarem Kind eines Extension-Roots:

- `~/.vbot/extensions/block_write.py`
- `~/.vbot/extensions/my_hooks/__init__.py`
- `~/.vbot/extensions/my_hooks/extension.py`

Wichtig:

- Änderungen an Extensions werden erst nach einem Neustart von Server oder Runtime geladen.
- Ladefehler loggen als `error`, Handlerfehler als `warn`. vBot läuft fail-open weiter.
- `register(api)` darf synchron oder asynchron sein. Handler dürfen ebenfalls sync oder async sein.

### Minimalbeispiel für eine Extension

Lege zum Beispiel diese Datei an:

```text
~/.vbot/extensions/block_write.py
```

```python
from core.tools.tools import tool_failure


def register(api):
  api.on("before_agent_start", append_rule)
  api.on("tool_call", block_write)


def append_rule(ctx, agent, session, messages, run):
  return {
    "system_prompt_append": (
      "Bearbeite Dateien nur dann direkt, wenn du sie vorher gelesen oder gesucht hast."
    )
  }


def block_write(ctx, tool_name, tool_call_id, input):
  if tool_name != "write":
    return None

  return tool_failure(
    "tool_blocked",
    "Das write-Tool ist in dieser Instanz per lokaler Extension deaktiviert.",
  )
```

Was dieses Beispiel macht:

- `before_agent_start` hängt Text an den System-Prompt des aktuellen Runs an.
- `tool_call` fängt jeden Tool-Call ab.
- Wenn der Tool-Name `write` ist, liefert die Extension direkt ein Failure-Envelope zurück.
- Dadurch wird das echte Tool nicht mehr ausgeführt.

Wenn du Parameter nur umschreiben statt blockieren willst, mutiere `input`
in-place und gib `None` zurück:

```python
def normalize_read_path(ctx, tool_name, tool_call_id, input):
  if tool_name == "read" and input.get("path") == "README":
    input["path"] = "README.md"
```

### Verfügbare Hook-Events

- `run_start(ctx, session_id, agent_id)`
- `run_end(ctx, session_id, agent_id, outcome)` mit `outcome = "success" | "error" | "cancelled"`
- `before_agent_start(ctx, agent, session, messages, run)`
- `context(ctx, messages)`
- `tool_call(ctx, tool_name, tool_call_id, input)`
- `tool_result(ctx, tool_name, tool_call_id, input, result)`

Die wichtigsten Rückgaberegeln:

- `before_agent_start`: gib `{"system_prompt_append": "..."}` zurück, um Text an den System-Prompt anzuhängen.
- `context`: gib eine neue Message-Liste zurück, wenn du nur den nächsten LLM-Request verändern willst.
- `tool_call`: gib ein vollständiges Tool-Result-Envelope zurück, wenn du den Tool-Call komplett ersetzen willst.
- `tool_result`: gib ein Patch-Dict zurück; es wird flach auf das bestehende Result-Envelop gemerged.

## 4. Einen Agenten anlegen

Aktuell gibt es noch keinen fertigen öffentlichen CLI- oder WebUI-Flow zum
Anlegen von Agents. Für den momentanen Stand legst du einen Agenten am einfachsten
mit einem kurzen Python-Snippet an.

Beispiel:

```python
from core.runtime import Runtime
from core.utils.config import Config

runtime = Runtime(Config())
runtime.start()

runtime.agents.create(
    "coder",
    "Coder Agent",
    model="openai/gpt-5.2",
)

runtime.stop()
```

Danach existiert:

- `~/.vbot/agents/coder/agent.json`
- `~/.vbot/agents/coder/sessions/`
- `~/.vbot/workspace-coder/`

Eingebaute Tools für Agents sind `read`, `edit` und `write`. Relative Pfade
werden vom Workspace des jeweiligen Agents aus aufgelöst; absolute Pfade sind
ebenfalls erlaubt.

Beispielhafte aktuell vorhandene Modelle:

- `openai/gpt-5.2`
- `anthropic/claude-sonnet-4-20250219`
- `openrouter/openai/gpt-5.2`
- `openrouter/anthropic/claude-sonnet-4`

## 5. Server starten

Foreground:

```bash
python server/main.py
```

Mit eigenem Port:

```bash
python server/main.py --port 9000
```

Mit eigenem Datenverzeichnis:

```bash
python server/main.py --data-dir ./dev-data
```

Mit explizitem Host:

```bash
python server/main.py --host 127.0.0.1 --port 8420
```

### Prüfen, ob der Server läuft

Im Browser oder per HTTP:

```text
http://127.0.0.1:8420/health
```

Erwartete Antwort:

```json
{"status":"ok"}
```

## 6. Das aktuelle Web-Interface öffnen

Das eigentliche Chat-Interface ist noch nicht fertig. Du kannst aber das
vorhandene Frontend-Scaffold starten:

```bash
cd webui
npm run dev
```

Dann die von Vite ausgegebene URL öffnen, meistens zum Beispiel:

```text
http://127.0.0.1:5173
```

Derzeit zeigt diese Oberfläche nur den Platzhalter `vBot` an. Die Integration
mit dem Server und ein echtes Chat-Fenster kommen erst in Phase 4.

## 7. Den Server direkt per RPC benutzen

Der nutzbare Weg in Phase 3 ist der Server-Vertrag über HTTP, SSE und WebSocket.

### 7.1 Session explizit anlegen

PowerShell-Beispiel:

Voraussetzung: Der Agent `coder` existiert bereits.

```powershell
$base = "http://127.0.0.1:8420"

$createBody = @{
  method = "session.create"
  params = @{
    agent_id = "coder"
  }
} | ConvertTo-Json -Depth 5

$sessionResponse = Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $createBody

$sessionId = $sessionResponse.result.session_id
$sessionId
```

### 7.2 Eine Nachricht senden und das Endergebnis gesammelt bekommen

```powershell
$sendBody = @{
  method = "chat.send"
  params = @{
    agent_id = "coder"
    session_id = $sessionId
    content = "Sag kurz Hallo."
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $sendBody
```

`chat.send` wartet, bis der komplette Run beendet ist, und liefert dann das
gesamte Ergebnis zurück.

### 7.3 Einen Run streamen

Zuerst den Run starten:

```powershell
$streamBody = @{
  method = "chat.stream"
  params = @{
    agent_id = "coder"
    session_id = $sessionId
    content = "Erkläre in zwei Sätzen, was vBot ist."
  }
} | ConvertTo-Json -Depth 5

$streamResponse = Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $streamBody

$runId = $streamResponse.result.run_id
$sseUrl = $streamResponse.result.sse_url
```

Dann den SSE-Stream öffnen, zum Beispiel mit `curl.exe`:

```powershell
curl.exe -N "$base$sseUrl"
```

Dort erscheinen Event-Blöcke wie:

- `run_started`
- `user_message_persisted`
- `reasoning`
- `tool_call_started`
- `tool_call_result`
- `assistant_output`
- `run_completed`

### 7.4 Einen laufenden Run abbrechen

```powershell
$cancelBody = @{
  method = "chat.cancel"
  params = @{
    run_id = $runId
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "$base/api/rpc" -ContentType "application/json" -Body $cancelBody
```

`cancel` ist best effort: vBot stoppt so schnell wie möglich die weitere
Ausführung, aber nicht jede bereits laufende externe Arbeit ist hart abbrechbar.

## 8. WebSocket-Events

Zusätzlich zum SSE-Stream eines einzelnen Runs gibt es:

```text
ws://127.0.0.1:8420/ws
```

Dieser Kanal ist für allgemeine Server-Ereignisse gedacht, nicht für den
primären Chat-Textstream eines einzelnen Runs.

## 9. Frontend bauen

```bash
cd webui
npm run build
```

Zum lokalen Vorschau-Server:

```bash
npm run preview
```

## 10. Qualitätschecks

Backend:

```bash
python scripts/quality.py
```

Frontend:

```bash
python scripts/quality-frontend.py
```

## 11. Was aktuell noch nicht fertig ist

- kein echtes Chat-WebUI im Browser
- keine fertigen CLI-Befehle für `server start`, `stop`, `restart`
- keine Desktop-Shell
- keine öffentliche Server-API zum Anlegen von Agents

Wenn du wissen willst, was als Nächstes kommt, schau in `ROADMAP.md`.
