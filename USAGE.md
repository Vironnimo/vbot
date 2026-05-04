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

### Python-Umgebung

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
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
