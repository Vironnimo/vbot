## Plan: Projekte — WebUI / Desktop (code-grounded, v2)

**Goal:** Projekte sind aus der WebUI (und damit Desktop) voll bedienbar — eigener Projekte-Tab
zum Hinzufügen/Verwalten, ein Zwei-Bar-Chat mit Projekt-Dropdown, Cron auf `agent@projekt`, und
projekt-aware Statistik — als reine Client-Schicht auf dem **bereits gemergten** Plan-1-RPC.

**Context:** Plan 1 (Backend + CLI) ist in `main` gemergt; der `project.*`-RPC, die projekt-aware
Session/Chat/Cron-Methoden und die `agent@projekt`-Adressierung existieren real. Design-Soll ist
`stuff/add-projects.md` → „Accessor-/UI-Fläche". **Wichtig:** Dieser Plan basiert auf dem
**verifizierten realen RPC-Vertrag** (siehe unten), nicht auf dem vor-Plan-1 geratenen alten Text.
Drei bewusste Abweichungen vom Design-Doc sind eingearbeitet:
1. **Kein Dry-Run-Scan vor dem Anlegen.** `project.add` validiert die cwd, **legt das Projekt an**
   und liefert den Scan zurück — es gibt keinen „nur scannen, ohne anzulegen"-RPC. Die
   „Scan-Vorschau" des Designs wird zur **Add-dann-Review-**Fläche (entschieden — kein
   Backend-Nachtrag; unerwünscht → `project.rm` archiviert reversibel).
2. **Statistik ist projekt-aware** (entgegen dem Handoff): der Report keyt Projekt-Agents als
   `agent@projekt`. → von „OUT" nach „IN" gezogen.
3. **Re-Point ist kein eigener Endpoint**, sondern `project.set {project_id, cwd}`; fehlende cwd
   erkennt die UI am `cwd_exists`-Flag.

### Verifizierter RPC-Vertrag (Quelle der Wahrheit für diesen Plan)

**`project.*`** (`server/rpc/project_methods.py`):

| Methode | Params | Ergebnis |
|---|---|---|
| `project.add` | `{cwd*, display_name?, default_agent?, default_model?, auto_load?}` | `{project, scan}` — `project_id` wird **server-seitig** aus `display_name` (sonst cwd-Basename) abgeleitet; **cwd muss existieren** (sonst `invalid_request`) |
| `project.list` | `{}` | `{projects: [project…]}` |
| `project.show` | `{project_id*}` | `{project, scan}` — **live re-scan** |
| `project.set` | `{project_id*, cwd?, display_name?, default_agent?, default_model?, auto_load?}` (≥1 Änderung) | `{project, scan}`; cwd-Wechsel verlangt existierende cwd + invalidiert Team-Cache |
| `project.rm` | `{project_id*}` | `{project_id, archived:true, archive_path}`; Fehler `project_busy` (aktiver/queued Run) / `project_in_use` (Cron zeigt drauf) |

- **`project`-Shape:** `{project_id, display_name, cwd, cwd_exists, default_agent, default_model,
  auto_load[], created_at, updated_at}`.
- **`scan`-Shape:** `{team: [{agent_id, display_name, description, model, temperature,
  source_format, source_path}], report: {clean:bool, findings: [{type, detail, agent_id,
  source_path}]}}`.
- **`finding.type`:** `slug_collision` | `unslugifiable_name` | `bad_model` | `orphan`.

**`agent@projekt`-Adressform** (`core/projects/address.py`, eine Parse/Format-Naht): überall, wo ein
Agent adressiert wird, trägt der **`agent_id`-Param** die Außenschreibweise. Bare `builder` →
Identität; `builder@vbot` → Projekt-Agent. WebUI baut/zeigt die Adresse, der Server parst sie.

**Session/Chat** (`server/rpc/agent_methods.py`, `chat_methods.py`) — **zwei Param-Sorten, nicht
eine** (verifiziert am Code):
- **Adresse parsend** (`_required_agent_address`, nimmt `agent@projekt`): `session.create`,
  `session.list`, `chat.history`, `chat.send`, `chat.stream`, `chat.retry_last_turn`.
- **Bare Agent-ID** (`agent_id` als plain string; Run/Queue keyt auf `(agent_id, session_id)` mit
  der **bare** ID): `chat.queue_list` / `chat.queue_remove` / `chat.queue_update` und
  `chat.cancel_tool_call` — hier die **project-id-freie** ID übergeben (`builder`, nicht
  `builder@vbot`), sonst greift der Queue-Lookup ins Leere. `chat.cancel` keyt rein auf `run_id`.
- **Falle 1 (current session):** `session.create` setzt `make_current` **nur für Identität**
  (`project_id is None`) — ein **Projekt-Agent hat keine server-getrackte current session**; die
  WebUI wählt sie selbst (jüngste aus `session.list`, sonst neu).
- **Falle 2 (bare vs. Adresse):** für *denselben* Projekt-Agent geht an Chat/Session/History die
  **volle** Adresse, an Queue/Cancel-Tool aber die **bare** ID.

**Cron** (`server/rpc/automation_methods.py`): `cron.create`/`cron.update` nehmen das Ziel als
Adresse im `agent_id`-Param. `cron.list` liefert je Job `{agent_id, project_id, target, …}` —
`target` = `agent@projekt` (fertig formatiert).

**Statistik** (`core/statistics`, `statistics.report`): **keine** Param-Änderung; der Report
enthält Projekt-Agents bereits, gekeyt als `agent@projekt` im Agent-Schlüssel.

**Recall / Logs:** Recall ist backend-seitig projekt-aware, aber rein Chat-Tool — **keine** eigene
WebUI-Fläche. Logs sind globale Tagesdateien, **nicht** projekt-skopiert → bleibt draußen.

**Scope:**
- **In:** `project.*`-Aufrufe in `api.js`; Projekte-Tab (Hinzufügen/Liste/Verwalten/Report/Re-Point);
  Chat Zwei-Bar + Projekt-Dropdown + Report-Banner + Projekt-Agent-Session-Wahl; Cron-Agent-Liste
  mit `agent@projekt`; **Statistik zeigt Projekt-Agents** sauber an; i18n; Desktop-Capability-Check.
- **Out:** Backend/RPC (Plan 1); nativer Ordner-Picker; **Logs**-Projektfilter (nicht projekt-aware);
  Projekt-Verwaltung über CLI (in Plan 1); **System-Prompt-Vorschau für Projekt-Agents** (Backend-
  `prompt.preview` hat kein `project_id` — FLAGGED 2026-06-18 #2; v1 bleibt identitäts-skopiert).

**Assumptions & Constraints:**
- Svelte 5 + JS (kein TS); alle Strings über `i18n.js`; Callback-Props, keine Event-Dispatcher;
  UI-Primitive aus `components/ui/` (`Button`/`Modal`/`TextField`/`StatusChip`/`Dropdown`).
- **Datei-Layout (verifiziert):** Haupt-Views liegen **flach** unter `webui/src/components/`
  (`ProjectsView.svelte`, `CronView.svelte`, `StatisticsView.svelte`); Sub-Komponenten in
  Feature-Unterordnern (`chat/`); Lib-Helfer flach in `webui/src/lib/`. Tests spiegeln die Quelle
  (`components/__tests__/`, `components/chat/__tests__/`, `lib/__tests__/`). **Es gibt kein
  `components/projects/` und kein `components/cron/`** — der alte Plan lag hier falsch.
- Zweite Bar + Projekt-Dropdown sind reine **Projektion** von `project.show`/Scan, **keine** zweite
  Wahrheit; lokal werden nur Auswahl-States gehalten (analog `vbot.selectedAgentId`-Muster).
- Doc-Pflege ist Teil jeder Phase (`webui.md`, `desktop.md`).

### Milestones

| # | Milestone | Deliverable (verifizierbar) |
|---|---|---|
| M1 | Transport + Projekte-Tab | `project.*` in `api.js`; Projekte-Tab listet/fügt hinzu/verwaltet/entfernt; Scan-Team + Report + Re-Point sichtbar |
| M2 | Zwei-Bar-Chat | Identitäts-Bar immer; Projekt-Dropdown → zweite Team-Bar; Öffnen springt auf Default-Agent; Projekt-Agent-Session-Wahl funktioniert; Report-Banner |
| M3 | Cron + Statistik + Politur | Cron-Agent-Liste zeigt/speichert `agent@projekt`; Statistik zeigt Projekt-Agents lesbar; i18n vollständig; Desktop ohne Picker |

### Phase Breakdown

#### Phase 1: Transport + Projekte-Tab (M1)
**Goal:** Projekte in eigenem Tab hinzufügen/verwalten/entfernen; Scan-Team + Report sichtbar;
fehlende cwd bietet Re-Point.
**Can run in parallel with:** Phase 2 erst nach dem `api.js`-Task.

- `api.js`: dünne Wrapper `addProject/listProjects/showProject/setProject/removeProject` exakt nach
  obigem Vertrag (Param-Validierung wie bei den vorhandenen Wrappern; sonst `rpc(...)` direkt) —
  read: [.vorch/domain-maps/webui.md, .vorch/domain-maps/projects.md],
  files: [webui/src/lib/api.js, webui/src/lib/__tests__/api.test.js]
- `projectsView.js` (reiner Helfer) ⚡ *parallel mit nächstem Task* — Add-Payload-Bau, sparse
  Manage-Payload für `project.set`, Report-Normalisierung (Findings nach `type` gruppieren, leerer/
  clean Report = normal), `cwd_exists===false`→Re-Point-Payload (`{project_id, cwd}`),
  Team-Projektion — files: [webui/src/lib/projectsView.js, webui/src/lib/__tests__/projectsView.test.js]
- `ProjectsView.svelte` (**flach**) ⚡ *parallel mit vorigem Task* — Hinzufügen per Server-Pfad
  (cwd-Eingabe → `project.add` → Team + Report aus der Antwort anzeigen), Liste (`project.list`),
  Verwalten je Projekt (cwd/Default-Agent/Default-Model/Auto-Load via `project.set`, Entfernen via
  `project.rm` mit `project_busy`/`project_in_use`-Fehlertexten), Report-Anzeige + **Re-Point**-
  Aktion wenn `cwd_exists===false` —
  files: [webui/src/components/ProjectsView.svelte, webui/src/components/__tests__/ProjectsView.test.js]
- Navigation: Tab `projects` in das Nav-Array hängen + `<ProjectsView/>` rendern (Tab-Reihenfolge:
  nach „Agents") — files: [webui/src/App.svelte]
- i18n: Strings für Projekte-Tab/Manage/Report/Re-Point —
  files: [webui/src/lib/i18n.js, webui/src/lib/__tests__/i18n.test.js]

**Dependencies:** Plan 1 (gemergt).
**Done when:** Server-Pfad eingeben legt ein Projekt an und zeigt Team + Report; Projekt
verwalten/entfernen funktioniert; ein Projekt mit `cwd_exists:false` bietet Re-Point (`project.set`
mit neuer cwd); `python scripts/quality-frontend.py webui/src/components/ProjectsView.svelte
webui/src/lib/projectsView.js webui/src/lib/api.js` grün.

#### Phase 2: Zwei-Bar-Chat + Projekt-Dropdown (M2)
**Goal:** Chat zeigt die Identitäts-Bar immer + eine zweite Team-Bar je nach Projekt-Dropdown;
Öffnen verhält sich wie spezifiziert; Projekt-Agent-Sessions laden korrekt.
**Can run in parallel with:** none (berührt App- und ChatView-Kern).

- App-State: Projekt-Kontext neben `selectedAgentId` (`selectedProjectId`, Default „Kein Projekt",
  Persistenz analog `localStorage`); `project.list` für das Dropdown laden; an ChatView
  durchreichen — read: [.vorch/domain-maps/webui.md], files: [webui/src/App.svelte]
- ChatView + `chatState.js`: Projekt-Dropdown („Kein Projekt" = Persönlich); bei Projektwahl
  `project.show(pid)` → zweite Bar = `scan.team`, **springt auf `default_agent`** (sonst erstes
  Team-Mitglied); Auswahl eines Agents aus beiden Bars. **Adress-Disziplin (siehe RPC-Vertrag,
  Falle 2):** Chat/Session/History eines Projekt-Agents unter der vollen Adresse `agent@projekt`,
  Queue/Cancel-Tool unter der **bare** ID. **Projekt-Agent-Session-Wahl lokal**, da keine
  server-`current_session_id`: jüngste aus `session.list`, sonst neue via `session.create`. Leeres
  Team = leere zweite Bar (kein Fehler) —
  files: [webui/src/components/ChatView.svelte, webui/src/lib/chatState.js,
  webui/src/components/__tests__/ChatView.test.js]
- `ProjectScanBanner.svelte` (**Sub-Komponente in `components/chat/`**): schmales nicht-blockierendes
  Banner bei `report.clean===false` beim Öffnen, verlinkt in den Projekte-Tab —
  files: [webui/src/components/chat/ProjectScanBanner.svelte,
  webui/src/components/chat/__tests__/ProjectScanBanner.test.js]
- i18n: Dropdown-/Bar-/Banner-Strings — files: [webui/src/lib/i18n.js, webui/src/lib/__tests__/i18n.test.js]

**Dependencies:** Phase 1 (`api.js`-Wrapper, `projectsView.js`), Plan 1.
**Done when:** Identitäts-Agents immer in der oberen Bar; ein Projekt im Dropdown zeigt sein Team in
der zweiten Bar und springt auf den Default-Agent; ein Chat mit einem Projekt-Agent lädt dessen
Sessions und sendet unter `agent@projekt`; leeres Projekt = leere zweite Bar ohne Fehler; unsauberer
Scan zeigt das Banner; Vitest grün.

#### Phase 3: Cron + Statistik + Politur (M3)
**Goal:** Cron kennt Projekt-Agents; Statistik zeigt Projekt-Agents lesbar; i18n vollständig;
Desktop sauber.
**Can run in parallel with:** Tasks untereinander ⚡ (getrennte Dateien).

- Cron `CronView.svelte` + `cronView.js` ⚡ — Agent-Liste = Identitäts-Agents (`agent.list`) +
  Projekt-Agents (`project.list` → je Projekt `project.show` → `scan.team`), Projekt-Agents im
  Dropdown als `agent@projekt` angezeigt **und so gespeichert** (`agent_id`-Param der
  `cron.create/update`); bestehende Jobs über das **`target`-Feld** der `cron.list`-Antwort anzeigen/
  editieren — read: [.vorch/domain-maps/webui.md, .vorch/domain-maps/automation.md],
  files: [webui/src/components/CronView.svelte, webui/src/lib/cronView.js, webui/src/lib/__tests__/cronView.test.js]
- Statistik `StatisticsView.svelte` + `statisticsView.js` ⚡ — der `statistics.report` enthält
  Projekt-Agents als `agent@projekt`-Schlüssel; Adresse parsen (Reuse `parse`/Format-Idee) und die
  Projekt-Zugehörigkeit lesbar darstellen (Label/Badge statt roher `builder@vbot`-String), kein
  Layout-Bruch. Tiefe entschieden: **nur lesbar**, kein Projekt-Filter (siehe Entscheidungen) —
  read: [.vorch/domain-maps/webui.md,
  .vorch/domain-maps/statistics.md], files: [webui/src/components/StatisticsView.svelte,
  webui/src/lib/statisticsView.js, webui/src/lib/__tests__/statisticsView.test.js]
- i18n + Desktop ⚡ — alle neuen Strings (Cron-Labels, Statistik-Projekt-Label) vollständig;
  Desktop-Capability-Check bestätigt „kein nativer Picker in v1" (nur verifizieren, kein Bau) —
  read: [.vorch/domain-maps/desktop.md], files: [webui/src/lib/i18n.js,
  webui/src/lib/__tests__/i18n.test.js, desktop/* (nur lesen/prüfen)]

**Dependencies:** Phase 1, Phase 2.
**Done when:** Cron bietet Projekt-Agents als `agent@projekt` und speichert die Adresse; bestehende
Projekt-Jobs zeigen ihr `target`; die Statistik listet Projekt-Agents lesbar (mit Projekt-Label) ohne
Layout-Bruch; keine hardcodierten Strings; Desktop zeigt denselben Add-Flow wie die WebUI;
`python scripts/quality-frontend.py` grün.

### Done when (Plan gesamt)
- Nutzer kann in der WebUI ein Projekt per Server-Pfad hinzufügen (Team + Report sichtbar), es
  verwalten/entfernen, fehlende cwd per Re-Point heilen, im Chat über das Dropdown öffnen, dessen
  Team in der zweiten Bar sehen, mit Projekt- und Identitäts-Agents chatten, einen Cron-Job auf
  `agent@projekt` setzen und Projekt-Agents in der Statistik sehen.
- `python scripts/quality-frontend.py` grün.

### Doc-Pflege pro Phase (Teil der jeweiligen „Done when")

| Phase | Domain-Maps / Docs (in derselben Arbeit) |
|---|---|
| 1 | `webui.md` (`project.*`-Wrapper, Projekte-Tab + View-Helfer, Re-Point) |
| 2 | `webui.md` (Zwei-Bar-Chat, Projekt-Dropdown, Report-Banner, Projekt-Agent-Session-Wahl, Projekt-Kontext im App-State) |
| 3 | `webui.md` (Cron `agent@projekt`, Statistik-Projekt-Agents), `desktop.md` (kein nativer Picker in v1 bestätigt) |

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Projekt-Agent ohne server-`current_session_id` bricht die ChatView-„current session"-Annahme | High | High | In Phase 2 explizit: Session-Wahl für Projekt-Agents lokal (jüngste aus `session.list`, sonst `session.create`); nicht auf `agent.current_session_id` verlassen |
| Queue/Cancel-Tool mit voller Adresse statt bare ID aufgerufen → Queue-Lookup leer, stilles Fehlverhalten | Med | High | RPC-Vertrag Falle 2 befolgen: volle Adresse nur an Chat/Session/History, bare ID an `chat.queue_*`/`cancel_tool_call`; Vitest deckt beide Pfade ab |
| Cron-Agent-Liste braucht N+1 `project.show` (eins pro Projekt) | Med | Low | Teams lazy beim Öffnen des Cron-Modals laden, Ergebnis cachen; nicht pro Render scannen |
| Zwei-Bar + Dropdown verkompliziert den dichten ChatView/App-State | Med | Med | Logik in `chatState.js`/`projectsView.js` auslagern, Komponenten dünn; an bestehende selected-agent-Mechanik andocken |
| „Add legt sofort an" überrascht (kein Preview) | Med | Med | Entschieden: anlegen + Review. UX klar als „angelegt + Review" benennen; Entfernen = archivieren (reversibel) |
| Doc-/Map-Pflege übersprungen | Med | Med | In jeder „Done when" verankert; Maps im `files:`-Scope mitführen |

### Entscheidungen (getroffen)
- **Scan-Vorschau vor dem Anlegen: nein.** `project.add` legt sofort an und liefert den Scan;
  Add-dann-Review ist die Fläche. Kein Backend-Dry-Run, kein Plan-1-Nachtrag. Unerwünschtes Projekt →
  `project.rm` (archiviert, reversibel). *Verworfene Alternative:* `project.scan_preview {cwd}`.
- **Statistik-Tiefe: erst nur lesbar.** Projekt-Agents werden lesbar dargestellt (Adresse geparst,
  Projekt-Label statt rohem `builder@vbot`) — minimale, sichere Darstellung, **kein** Projekt-Filter/
  Gruppierung in v1. *Verworfen für jetzt:* Filter/Gruppierung (später nachrüstbar, wenn gewünscht).

### Offen (Kosmetik, beim Bau entscheidbar)
- Dropdown-Platzierung (Default: über der zweiten Bar), Tab-Reihenfolge (Default: Projekte nach
  „Agents").

### Bekannte Backend-Lücken (FLAGGED 2026-06-18, relevant aber out-of-scope hier)
- `prompt.preview` hat kein `project_id` → eine System-Prompt-Vorschau eines Projekt-Agents zeigt den
  Body, aber **nicht** `{project_files}`. Falls die WebUI später Projekt-Agent-Prompt-Vorschau will,
  braucht es einen Backend-Param. v1: System-Prompt-View bleibt identitäts-skopiert.
- `/status` in einer Projekt-Session liefert leer (Dispatcher kennt kein `project_id`). Reines
  Backend-Thema, nicht WebUI.
