# Roadmap

## Phase 0 — Projekt-Scaffold ✅

Ziel: Leeres Skelett, das sauber startet.

- [x] `pyproject.toml` mit Dependency-Gruppen
- [x] `python -m venv .venv && pip install -e ".[dev]"`
- [x] Ordnerstruktur anlegen (alle `core/`-Module, `server/`, `webui/`, `cli/`, `tests/`)
- [x] `core/utils/` — Logging-Setup, Error-Basisklassen, Config-Loader
- [x] `core/runtime/` — minimale Service-Registry + DI (`typing.Protocol`)

**Exit:** `python -c "from core.runtime import Runtime; Runtime().start()"` läuft ohne Fehler. ✅

---

## WebUI-Scaffold ✅

Ziel: Frontend-Toolchain steht, Quality-Gate-Script läuft.

- [x] `webui/` — Vite + Svelte 5 + JS initialisiert (`package.json`, Build-Pipeline)
- [x] Prettier, ESLint, Vitest installiert und konfiguriert
- [x] Minimale Platzhalter-Komponente (`App.svelte`) + i18n-Stub (`src/lib/i18n.js`)
- [x] `scripts/quality-frontend.py` — Bugfix für Windows (`shutil.which` für npx/npm)
- [x] 4 Unit-Tests in `webui/src/lib/__tests__/i18n.test.js`

**Exit:** `python scripts/quality-frontend.py` → alle 5 Gates grün (prettier, eslint, vitest 4/4, build). ✅

---

## Begriffsklärung ✅

Zentrale Begriffe sind in `.vorch/GLOSSARY.md` festgehalten.
Geklärt sind u.a. Agent, Provider, Model (provider-spezifisch, nicht
kanonisch), Adapter, Reasoning (adapter-verantwortlich,
wire-protocol-spezifisch), CoT, Session, Run, Skill, Tool, Workspace,
Streaming und Cancel.
`.vorch/GLOSSARY.md` ist die autoritative Quelle.

---

## Phase 1 — Provider + Model-System ✅

Ziel: Der Kernel kann Provider instanziieren, deren Modelle laden,
und einen Chat-Request durch einen Adapter an eine API schicken.

**Architektur — zwei Schichten:**

| Schicht | Zuständig für | Wo |
|---|---|---|
| **Provider** | Wire Protocol, Auth, Provider-Config | `resources/providers/<name>.json` + Adapter-Code in `core/providers/` |
| **Model** | Alle Info zu einem Modell an einem Provider: ID, Capabilities, Context-Window | `resources/models/<provider>.json` |

Keine Varianten. Keine kanonischen Model-Dateien. Ein Modell IST ein Modell
an einem Provider. Gleiche KI = verschiedene Einträge in verschiedenen
Provider-Dateien. Die Model-ID ist die exakte ID, die im API-Request
geschickt wird.

**Model-Auswahl:** `<provider>/<model-id-beim-provider>`, z.B.
`openrouter/anthropic/claude-sonnet-4`. Kein Remapping, keine Overrides.

**Adapter-Hierarchie:**

```
ProviderAdapter (ABC)
  ├── OpenAICompatibleAdapter    # deckt 80%+ ab, konfiguriert nicht subclassed
  │     └── nur bei echten Wire-Unterschieden: eigene Subklasse
  ├── AnthropicAdapter           # eigenes Protokoll
  └── [weitere Familien bei Bedarf]
```

OpenAI, OpenRouter, Groq, Together → gleicher Adapter, verschiedene Config.
Anthropic → eigener Adapter. Custom Provider → nur Config nötig (oder
Subklasse von OpenAICompatibleAdapter wenn nötig).

**Reasoning:** Adapter-spezifisch. Anthropic hat drei unabhängige Parameter
(thinking.type, effort, display), OpenAI einen String (reasoning_effort),
OpenRouter zwei Parameter (reasoning, include_reasoning). Der Adapter
übersetzt. In den Model-Daten steht nur `reasoning.supported: true/false`.

**Aufgaben:**

- [x] `core/providers/` — ProviderAdapter-ABC mit Interface: `send()`, `stream()`, `aclose()`
- [x] `core/providers/` — OpenAICompatibleAdapter (async HTTP, Streaming/SSE, Retry, Error-Klassifikation)
- [x] `core/providers/` — AnthropicAdapter (eigenes Wire Protocol, Thinking-Blocks, Content-Blocks)
- [x] `core/models/` — Model-Daten: provider-spezifische Capabilities, eine JSON pro Provider
- [x] `core/providers/` — Provider-Config-Loader: JSON-Dateien aus `resources/` laden
- [x] Kernel-Integration: Runtime instanziiert Provider, lädt Models, macht sie verfügbar

**Wichtig:** Die Chat-Schicht (Phase 2) baut den logischen Request zusammen,
der Adapter übersetzt ihn nur ins Wire-Format. Der Adapter kennt keine
Agent-Konfiguration, die Chat-Schicht kennt keine Wire-Protokolle.
Was die interne Reasoning-Konfiguration von vBot aussieht (effort levels,
budget, on/off) entscheidet Phase 2.

**Exit:** Runtime kann Provider instanziieren, Modelle laden, und einen
Chat-Request durch den Adapter schicken. Integration-Test: Request durch
OpenAICompatibleAdapter an Mock, Antwort kommt zurück. ✅

---

## Phase 2 — Minimaler Chat (Backend) ✅

Ziel: Ein Agent sendet eine User-Nachricht, das Modell antwortet. Kein Streaming, kein UI.

### Agent-Schema

Minimal JSON für `agent.json` — kann später grown, aber nie shrinkn:

```json
{
  "id": "coder",
  "name": "Coder Agent",
  "model": "openrouter/deepseek/deepseek-v4-pro",
  "fallback_model": "",
  "workspace": "",
  "current_session_id": "",
  "temperature": 0.1,
  "thinking_effort": "",
  "allowed_tools": ["*"],
  "allowed_skills": ["*"],
  "created_at": "2026-05-03T12:00:00Z",
  "updated_at": "2026-05-03T12:00:00Z"
}
```

- `id`: unique, auch als Verzeichnisname verwendet. Immutable — kann nach Erstellung nicht geändert werden.
- `model`: `<provider>/<model-id>` (aus Phase 1). Leer = Fehler zur Chat-Zeit ("no model set"). Provider muss existieren — sonst Fehler. Model-Existenz wird nicht vorgeprüft; der Provider-API liefert den Fehler, wenn's nicht passt.
- `fallback_model`: leer = kein Fallback konfiguriert. Exaktes automatisches Fallback-Verhalten bleibt in Phase 2 noch offen.
- `workspace`: absoluter Pfad zum Workspace-Verzeichnis. Default bei Erstellung: `<data_dir>/workspace-<id>/`. User kann auf eigenen Pfad setzen.
- `current_session_id`: Session-ID der aktuell aktiven Session dieses Agenten. Wird persistiert, damit der aktuelle Chat pro Agent explizit wiederhergestellt werden kann.
- `thinking_effort`: `none` / `minimal` / `low` / `medium` / `high` / `xhigh` / `max` — leer = Provider-Default. Adapter übersetzt ins Wire-Format.
- `allowed_tools`: `["*"]` = alle, `[]` = keine, sonst explizite Liste. Nur erlaubte Tools kommen in den Prompt-Toolblock und — wenn vom Provider unterstützt — in den offiziellen Tool-Teil des API-Requests. Nicht erlaubte Tools werden vom System blockiert.
- `allowed_skills`: `["*"]` = alle, `[]` = keine, sonst explizite Liste. Nur erlaubte Skills kommen in den Prompt-Skillblock.
- `created_at` / `updated_at`: ISO 8601 mit explizitem UTC-Offset

### Agent-Lifecycle

- **Erstellen**: Neuer Agent → `data_dir/agents/<id>/agent.json` + Workspace wird aus `resources/workspace-templates/` gesät (die vier Dateien). `workspace`-Feld defaultet auf `<data_dir>/workspace-<id>/`. Zusätzlich wird sofort eine erste Session angelegt und als `current_session_id` eingetragen.
- **Bootstrap / Erststart**: Beim erstmaligen Erstellen des data-dir wird automatisch ein Agent `main` mit dem Namen `Main` angelegt.
- **Löschen**: Agent gelöscht → alle Dateien (agent.json, sessions, workspace) werden nach `archive/<agent-id>/` verschoben. Nicht permanent gelöscht — kann inspiziert oder wiederhergestellt werden. Löschen ist nur erlaubt, wenn danach mindestens ein anderer Agent verbleibt.
- **Updaten**: Jedes Feld außer `id` kann geändert werden. `id` ist immutable (Verzeichnisname).

### Datenverzeichnis-Struktur

```
<data_dir>/                     ← VBOT_DATA_DIR
├── .env
├── settings.json
├── .tmp/
├── agents/<id>/
│   ├── agent.json
│   └── sessions/
├── workspace-<id>/
│   ├── SOUL.md
│   ├── AGENTS.md
│   ├── IDENTITY.md
│   └── USER.md
├── archive/<agent-id>/         ← gelöschte Agenten (agent.json, sessions, workspace)
├── channels/
├── cron/
├── oauth/
├── prompts/
├── skills/
└── logs/
```

- `data_dir` = `~/.vbot` (default), über `--data-dir` beim Serverstart übergeben
- Mehrere Instanzen: jede hat eigenes data-dir und eigenen Port. Zweite Instanz:
  `vbot server start --data-dir ./dev-data` (Port aus deren settings.json)
- Port-Priorität: `--port` > `VBOT_SERVER_PORT` (env) > `settings.json` > `8420`
- `agents/<id>/sessions/`: Agent-Session-History (JSONL — eine Nachricht pro Zeile)
- JSONL weil Sessions append-only sind (crash-safe — höchstens die letzte Zeile geht verloren)
- `workspace-<id>/`: Agent-Workspace, wird bei Erstellung mit den vier Dateien gesät
- `prompts/`: Prompt-Templates und Snippets
- `skills/`: Skill-Definitionen (SKILL.md + optionale resources/)

### System-Prompt-Assembly

Wird zur Laufzeit aus Templates und Snippets zusammengesetzt. Keine hardcoded Strings.

**Main Template:**

```
You are an agent for vBot, App version: {app_version}.
Use the instructions below and the tools available to you to assist the user.

{runtime}

{tools}

{skills}

{include:SOUL.md}
{include:IDENTITY.md}
{include:AGENTS.md}
{include:USER.md}
```

- `{app_version}`: App-Version
- `{runtime}`: konkreter Runtime-Block mit Host, OS, Modell, Workspace, App-Pfad, Data-Root, Thinking-Level und Datum
- `{tools}`: konkreter Tool-Reminder-Block im Prompt; `{tool_list}` enthält nur Name + Beschreibung. Dieselben erlaubten Tools werden zusätzlich im offiziellen Tool-Teil des Provider-Requests übergeben, dort mit Name, Beschreibung und Parameter-Schema (JSON Schema), wenn der Provider das unterstützt
- `{skills}`: Injiziertes Skill-Snippet (XML, agentskills.io-Schema):
  ```xml
  <available_skills>
    <skill>
      <name>agent-cli</name>
      <description>Delegate coding tasks to an external AI coding agent CLI...</description>
      <path>C:\...\skills\agent-cli\SKILL.md</path>
    </skill>
  </available_skills>
  ```
- `{include:<filename>}`: Inhalt der Workspace-Datei wird inline eingefügt

### Reasoning-Konfiguration

vBot-internes Format: ein einzelner String-Wert `thinking_effort` im Agent-Schema.

Werte: `none` | `minimal` | `low` | `medium` | `high` | `xhigh` | `max`

Jeder Adapter übersetzt den vBot-Wert ins jeweilige Wire-Format:
- **Anthropic**: `thinking.type` (disabled/enabled/adaptive) + `output_config.effort` (low/medium/high/xhigh/max) + `thinking.display` (summarized/omitted) — drei unabhängige Parameter
- **OpenAI**: `reasoning_effort` (low/medium/high) — ein String-Parameter
- **OpenRouter**: `reasoning` (object) + `include_reasoning` (boolean) — zwei Parameter
- **DeepSeek**: `reasoning_content` im Response — keine Konfiguration, immer an

Model-Daten enthalten nur `reasoning.supported: true/false` — die Effort-Übersetzung ist Adapter-Verantwortung.

**CoT im Multi-Turn**: Session speichert alles (`reasoning` + `reasoning_meta`). In Tool-Loops wird `reasoning_meta` unverändert zurückgegeben. Nach normalen abgeschlossenen Turns wird altes `reasoning_meta` zunächst nicht erneut gesendet. Das soll später leicht pro Provider anpassbar bleiben. Siehe `GOALS.md` Abschnitt 4.

### Aufgaben

- [x] `core/chat/` — Session-Manager (erstellen, laden, löschen — JSONL append-only)
- [x] `core/chat/` — Einfacher Agentic-Loop (Tool-Call-Support, aber ohne Tools brauchbar)
- [x] `core/chat/` — Reasoning-Konfiguration: `thinking_effort`-Wert im Agent-Schema, Adapter übersetzt ins Wire-Format
- [x] `core/chat/` — ChatMessage-Typen (JSONL-Schema mit role-spezifischen Feldern, `reasoning`/`reasoning_meta` für CoT, `model` pro Nachricht, `tool_calls`/`tool_call_id` — siehe GOALS.md Abschnitt 4)
- [x] `core/agents/` — Agent-Store (CRUD mit Persistenz in `data_dir/agents/<id>/agent.json`)
- [x] `core/agents/` — System-Message-Manager (Template-Assembly mit `{app_version}`, `{runtime}`, `{tools}`, `{skills}`, `{include:*}` inkl. `SOUL.md`, `IDENTITY.md`, `AGENTS.md`, `USER.md`)
- [x] `core/tools/` — Tool-Registry (leer, nur `register()`/`dispatch()`) + Allowlist-Filterung für Prompt und Provider-Request
- [x] `core/storage/` — Settings-Manager, Prompt-Fragmente
- [x] `core/skills/` — Skill-Metadaten-Registry + Allowlist-Filterung für den `{skills}`-Prompt-Block

### Noch offen in Phase 2

Diese Punkte bleiben offen, blockieren den Phase-2-Abschluss aber nicht:

- **Fallback-Verhalten**: exaktes automatisches Verhalten für `fallback_model`
- **Provider-spezifisches `reasoning_meta`-Resend nach abgeschlossenen Turns**

### Was Phase 2 aus Phase 1 bekommt

Die Chat-Schicht ruft die Adapter über die `ProviderAdapter`-Schnittstelle auf. Das Interface:

```python
class ProviderAdapter(ABC):
    async def send(self, messages: list[dict], *, model_id: str, **kwargs) -> dict
    def stream(self, messages: list[dict], *, model_id: str, **kwargs) -> AsyncIterator[dict]
    async def aclose(self) -> None
```

- `messages` ist aktuell `list[dict]` — Phase 2 definiert die kanonische Nachrichten-Repräsentation (ChatRequest/ChatResponse) und übersetzt sie vor dem Adapter-Aufruf ins jeweilige Wire-Format.
- `model_id` ist der exakte String, der im API-Request geschickt wird (aus `Model.model_id`).
- `**kwargs` nehmen provider-spezifische Parameter auf (temperature, max_tokens, reasoning-Konfiguration). Phase 2 definiert, welche kwargs die Chat-Schicht übergibt.
- Der Adapter kümmert sich um Retry, Error-Klassifikation und Wire-Format-Übersetzung. Die Chat-Schicht sieht nur `ProviderError` (retryable vs. fatal).

Runtime-Zugriff:
- `runtime.get_adapter(provider_id)` → gibt einen verbundenen Adapter zurück (API-Key aus Environment)
- `runtime.get_model(provider_id, model_id)` → gibt Model-Daten zurück (Capabilities, Context-Window)
- `runtime.providers` → ProviderRegistry (Provider-Configs nachschlagen)
- `runtime.models` → ModelRegistry (Modelle nachschlagen)

Model-Daten-Struktur (aus `core/models/models.py`):
```python
@dataclass(frozen=True)
class Model:
    model_id: str           # exakte ID für den API-Request
    name: str               # Anzeigename
    capabilities: Capabilities  # vision, tools, json_mode, reasoning.supported
    context_window: int
    max_output_tokens: int
```

Provider-Konfiguration (aus `core/providers/providers.py`):
```python
@dataclass(frozen=True)
class ProviderConfig:
    id: str                 # z.B. "openai", "anthropic"
    name: str               # z.B. "OpenAI", "Anthropic"
    adapter: str            # "openai_compatible" oder "anthropic"
    base_url: str           # API-Endpoint
    auth: AuthConfig        # header, prefix, env_key
    defaults: dict | None  # max_tokens, temperature, etc.
    extra_headers: dict | None
    models_endpoint: str | None  # für zukünftigen dynamischen Refresh
```

Adapter-Map (in Runtime): `"openai_compatible"` → OpenAICompatibleAdapter, `"anthropic"` → AnthropicAdapter.

**Exit:** Persistierte Agents können jetzt einen nicht-streamenden Backend-Chat-Turn
über den konfigurierten Provider/Adapter ausführen, optionale Tool-Calls in der
agentischen Schleife abarbeiten, Sessions als kanonisches JSONL persistieren und
System-Prompts mit Tool-/Skill-Filterung zusammensetzen. Phase 2 ist damit im
Backend abgeschlossen. ✅

---

## Phase 3 — Server-Schicht ✅

Ziel: HTTP/SSE/WS-Wrapper um den Kernel.

**Architekturentscheidungen für Phase 3:**

- Client ↔ vBot-Server ist ein eigener stabiler Außenvertrag; Provider-Details
  bleiben in den Adaptern verborgen.
- Sessions werden explizit erstellt und bleiben die persistierte JSONL-
  Gesprächshistorie.
- Ein **Run** ist eine einzelne aktive Ausführung innerhalb einer Session.
- Pro Session gibt es maximal einen aktiven Run gleichzeitig; mehrere Sessions
  und damit auch mehrere Agents dürfen parallel laufen.
- `send`, `stream` und `cancel` sind drei Zugriffsformen auf dieselbe
  Chat-Ausführungslogik, keine getrennten Systeme.
- Streaming zum Client läuft über **SSE**; **WebSocket** ist für allgemeine
  Server-Events aus dem internen Event-Bus reserviert.
- Im Chat sollen Thinking-Blöcke, Tool-Calls, Tool-Ergebnisse und Assistant-
  Antworten sichtbar sein.
- `cancel` ist best effort: laufende Modell- oder Tool-Arbeit soll möglichst
  schnell gestoppt werden; nicht mehr abbrechbare Restarbeit wird danach
  ignoriert und der Run endet als abgebrochen.

- [x] `server/app.py` — FastAPI + `/ws` WebSocket
- [x] `server/delegates.py` — `POST /api/rpc` Dispatcher
- [x] Server-Delegate für explizite Session-Erstellung
- [x] UIApi-Delegate für Chat (send, stream, cancel)
- [x] SSE-Endpoint für inkrementelles Chat-Streaming eines Runs
- [x] WebSocket pusht Events aus dem internen Event-Bus an Clients

**Exit:** `python server/main.py` → Session kann explizit angelegt werden,
`POST /api/rpc`-Chat funktioniert, ein Run kann gestreamt werden, Thinking /
Tool-Schritte / Assistant-Ausgaben werden sichtbar, und `cancel` stoppt einen
laufenden Run best effort. ✅

---

## Phase 4 — WebUI (Minimal)

Ziel: Svelte-App mit linker Navigation, rechter Inhaltsfläche und erstem echten Chat-UI.

- [ ] `webui/` — App-Shell mit linkem Menü (`Chat`, `Agents`, `System Prompt`, `Settings`) + rechter Inhaltsfläche
- [ ] `webui/src/lib/api.js` — RPC + SSE + WebSocket-Client
- [ ] Chat-Ansicht: Agent wählen statt Session; aktueller Chat kommt aus `current_session_id`; `New Session` startet für den gewählten Agenten eine neue aktive Session, ohne alte JSONL-Sessions zu löschen oder als Liste anzuzeigen
- [ ] Chat-Komponente: Eingabefeld, Nachrichtenliste, Senden/Empfangen,
      sichtbare Thinking-Blöcke, Tool-Schritte und Assistant-Antworten
- [ ] Queue-Verhalten in der Chat-Ansicht: weitere Nachrichten während laufendem Run FIFO-queuen, sichtbar anzeigen und vor dem Absenden abbrechbar machen
- [ ] Agents-Ansicht: Agenten erstellen, bearbeiten und löschen; es muss immer mindestens ein Agent existieren
- [ ] `System Prompt`-Ansicht vorerst als `Coming soon`; später editierbare Prompt-Bausteine aus `resources/prompts/` plus agentbezogene Vorschau des resultierenden System Prompts
- [ ] Optional später: zuletzt gewählten Agenten accessor-lokal merken (nicht im data dir, nicht Phase-4-blockierend)
- [ ] `npm run build` → statische Dateien, von FastAPI serviert

**Exit:** `localhost:8420` → App-Shell mit linker Navigation sichtbar,
Agent auswählen, neue Session starten, Text eingeben, Run im Browser streamen,
Thinking-/Tool-/Assistant-Schritte sichtbar sehen, Run abbrechen und Agenten
erstellen/bearbeiten/löschen (bei Minimum-ein-Agent-Regel).

---

## Phase 5 — CLI

Ziel: Server starten/stoppen von der Kommandozeile.

- [ ] `cli/main.py` — `server start`, `server stop`, `server restart`
- [ ] Subprozess-Management (PID-Tracking, Log-Weiterleitung)

**Exit:** `python cli/main.py server start` bringt den Server hoch, Browser zeigt WebUI.

---

## Phase 6 — Desktop-Shell

Ziel: Thin-Client im pywebview-Fenster.

- [ ] `desktop/main.py` — pywebview, zeigt WebUI-URL an
- [ ] `--host` / `--port` CLI-Argumente
- [ ] Fenster-Titel, Icon, Schließen-Verhalten

**Exit:** `python desktop/main.py` → Fenster mit WebUI, kommuniziert mit Remote-Server.

---

## Danach

- [ ] Channels (Telegram)
- [ ] Speech (STT/TTS)
- [ ] Skills (Sync + Augmentation)
- [ ] Automation (Cron, Hooks)
- [ ] I18n in UI
- [ ] Desktop-Offline-Fallback
- [ ] Dynamischer Model-Refresh (Provider `/models`-Endpoint abrufen und Model-Daten aktualisieren)
