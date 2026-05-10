# vBot

vBot ist ein local-first Agent-Harness: ein asynchroner Python-Kernel, der
KI-Agenten mit einem eigenen Workspace, Tool-Zugriff und einer Server-Schicht
ausstattet.

Aktuell ist der Backend-Kern bis einschließlich **Phase 3** umgesetzt:

- Provider- und Modell-System
- persistierte Agents und Sessions
- agentischer Chat-Loop mit Tool-Support
- FastAPI-Server mit RPC, Server-Sent Events (SSE) und WebSocket-Events

Die eigentliche Chat-Weboberfläche ist **noch nicht fertig**. In `webui/`
existiert im Moment nur das Frontend-Scaffold; das echte Chat-UI ist laut
Roadmap für Phase 4 vorgesehen.

## Projektstatus

Bereits umgesetzt:

- **Phase 1:** Provider + Models
- **Phase 2:** Minimaler Chat im Backend
- **Phase 3:** Server-Schicht mit `POST /api/rpc`, SSE und `/ws`

Der Agent-Tool-Support umfasst die eingebauten Tools `read`, `edit` und
`write`. Relative Pfade werden vom Workspace des Agents aus aufgelöst;
absolute Pfade sind erlaubt.

Noch offen:

- **Phase 4:** WebUI mit echtem Chat
- **Phase 5:** CLI für Server-Management
- **Phase 6:** Desktop-Shell

Details dazu stehen in `ROADMAP.md`.

## Voraussetzungen

- Python **3.11+**
- Node.js (für `webui/`)

## Schnellstart

### 1. Entwicklungsumgebung aufsetzen

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 2. API-Schlüssel hinterlegen

vBot liest Konfiguration standardmäßig aus `~/.vbot/`.

Lege dort eine Datei `.env` an, zum Beispiel:

```env
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
ANTHROPIC_API_KEY=...
```

### 3. Server starten

```bash
python server/main.py
```

Standardmäßig läuft der Server auf `http://127.0.0.1:8420`.

Health-Check:

```text
http://127.0.0.1:8420/health
```

### 4. Frontend-Scaffold starten

```bash
cd webui
npm install
npm run dev
```

Danach das von Vite ausgegebene lokale URL im Browser öffnen.

Wichtig: Das ist derzeit nur das WebUI-Scaffold. Ein echtes Chat-Fenster ist
noch nicht implementiert.

## Was der Server aktuell anbietet

- `POST /api/rpc`
  - `session.create`
  - `chat.send`
  - `chat.stream`
  - `chat.cancel`
- `GET /api/runs/{run_id}/events` für SSE-Streaming eines einzelnen Runs
- `GET /health`
- `WS /ws` für allgemeine Server-Events

## Dokumentation

- `USAGE.md` — praktische Nutzung des aktuellen Systems
- `ROADMAP.md` — Projektphasen und Status
- `GOALS.md` — stabile Verträge und Architekturentscheidungen
- `PHASE3-SPEC.md` — Server-Architektur für Phase 3

## Qualitätssicherung

Backend:

```bash
python scripts/quality.py
```

Frontend:

```bash
python scripts/quality-frontend.py
```

## Logging

- vBot schreibt Anwendungslogs unter `~/.vbot/logs/` standardmäßig in **eine Datei pro Tag**.
- Der Dateiname ist das aktuelle Datum, zum Beispiel `~/.vbot/logs/2026-05-10`.
- Das erzwungene Log-Format lautet exakt: `timestamp [LEVEL] name - message`.
- Warnungen erscheinen dabei als `[WARN]`.
- Normale erfolgreiche HTTP-Access-Zeilen (zum Beispiel viele `200 OK`-Einträge) sind im Standard-Log absichtlich unterdrückt, damit nur relevante Anwendungsereignisse sichtbar bleiben.
