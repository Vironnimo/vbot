## Plan: Projekte — Kern + CLI (headless-vollständig)

**Goal:** Ein Projekt ist eine erstklassige Backend-Entität. Man kann Projekte hinzufügen,
scannen, verwalten und entfernen; mit Projekt-(Config-)Agents und besuchenden/rooted
Identitäts-Agents reden; und Projekt-Agents per Cron ansteuern — alles über CLI/RPC verifizierbar,
**ohne** WebUI.

**Context:** Vollständige Design-Grundlage in `stuff/add-projects.md` (Stand 2026-06-18) —
**vor Phase 1 komplett lesen.** Das ist der erste von zwei Plänen; Plan 2 (`projekte-2-webui-desktop.md`)
setzt die WebUI/Desktop-Präsentation auf den hier gebauten RPC-Vertrag. Diese Naht ist bewusst
„headless-vollständig vs. Präsentation": die CLI ist der Verifikationsweg dieses Projekts und der
Stack, der headless auf dem Pi läuft.

**Requirements (entschieden in der Design-Phase, verbatim):**
- **Adressierung = Option 1:** `project_id` als explizite Dimension überall mitführen (nicht
  String-Qualifizierung `agent@projekt` im Pfad). Begründung: die Session-Bindung steht *in* der
  Session-Meta, also muss jeder Aufrufer, der eine Session öffnet, die `project_id` schon vorher
  kennen. Adressform `agent@projekt` ist nur die **Außen-Schreibweise** (Spawn-Ziel, CLI, Anzeige).
- **cwd ≠ Workspace:** cwd wird ein **eigenes Laufzeit-Feld** neben dem Workspace (keine
  Umbenennung). Datei-Tools lösen relative Pfade gegen **cwd** auf; der Workspace bleibt
  Identitäts-/Memory-Zuhause der Identitäts-Agents.
- **Agent-Auflösung: zwei Quellen, eine Naht.** Eine Auflösung nimmt `(project_id | kein, agent_id)`
  und liefert ein uniformes Laufzeit-Agent-Objekt. Kein Projekt → Identitäts-Store; mit Projekt →
  Config-Agent aus dem Team-Scan. **Zwei Frische-Ebenen:** Team-Zugehörigkeit = Scan (bei
  Öffnen + Re-Scan); Konfig eines einzelnen Agents = pro Run frisch aus der Repo-Datei gelesen.
- **Model-Kette:** Identität: Model → global → leer (unverändert). Projekt: Model → Projekt-Default
  → global → **Fehler**. „Model existiert/konfiguriert?"-Prüfung am Scan → Report.
- **Config-Agent = kein Workspace, kein Memory-Tool (v1).** Durable Notizen wären eine normale
  Datei in der cwd (Repo), via Datei-Tools — kein vBot-Laufzeit-State.
- **System-Prompt: ein Root.** Zwei kollabierende Platzhalter `{agent_body}` (importierter Body,
  leer bei Identitäts-Agents) + `{project_files}`. Body **wörtlich** eingefügt wie ein `{include}`
  (nicht als Template ausgewertet). Config-Agents erben runtime/tools/channels/skills-Blöcke.
- **Scan = sichtbarer Report**, nicht-blockierend. Meldet nur Unsauberes unter dem Vorhandenen
  (schlechtes/unkonfiguriertes Model, Orphan, Slug-/Namens-Kollision, nicht-slugifizierbar,
  verwaiste Session/Default-Zeiger). Kollision deterministisch: Format-Vorrang (OpenCode zuerst),
  dann stabil nach Dateiname; nie Dateisystem-Reihenfolge.
- **Minimal-Projekt = nur eine cwd.** Leerer Ordner, kein Team, keine AGENTS.md → gültig, sauberer
  leerer Report.
- **CLI zustandslos**, Projekt reitet auf der Adressform `agent@projekt` (positional), kein
  `--project`-Flag. Eigener Bereich `project add|list|show|set|rm`.
- **Cron darf auf Projekt-Agents zeigen** (`agent@projekt`); Feuern erzeugt die Session im Projekt.

**Scope:**
- **In:** Projekt-Domäne (Entität, `project.json`, Data-Dir-Anker, Archiv beim Entfernen);
  `project_id`-Durchstich durch Session-/Run-/Agent-Adressierung; cwd-Feld + Datei-Tools;
  pluggable Scanner + OpenCode-Detektor + Report; uniforme Agent-Auflösung + Model-Kette;
  System-Prompt-Verdrahtung + Platzhalter + Config-Agent-Body; Server-RPC (`project.*`,
  projekt-skopierte Session/Chat, Cron-Projektziele); CLI (`project`-Bereich, `@projekt`-Adressierung,
  Cron qualifiziert); Recall/Statistik lernen den projekt-skopierten Session-Pfad.
- **Out:** WebUI/Desktop (Plan 2); projekt-lokales Memory-Tool (zurückgestellt); projekt-eigene
  Skills (zurückgestellt); Kanäle auf Projekt-Agents (zurückgestellt); native Ordner-Picker.

**Assumptions & Constraints:**
- **Kein Legacy-Support** (Projektregel): kein Auto-Migration, kein „if old_field". Bestehende
  Sessions bleiben unter `agents/<id>/` (Identitäts-Agents, unverändert) — Projekt-Sessions sind ein
  neuer Pfad, kein Umzug alter Daten.
- **Deployment Linux / Dev Windows:** Pfad-Logik beidflavorig; cwd-Normalisierung (Symlinks,
  trailing slash, Groß/Klein) explizit festlegen und testen.
- **Neues Modul `core/projects/`** ist eine Plan-Entscheidung (siehe Architektur-Entscheidungen).

### Architektur-Entscheidungen (vor den Tasks)

1. **Neues tiefes Modul `core/projects/`.** Es besitzt die Projekt-Entität, `project.json`, den
   Anker-Lifecycle, den Scanner-Registry (ein Detektor pro Format) und die Agent-Auflösung über
   beide Quellen. Begründung gegen „few deep modules": Projekt ist eine eigene Capability mit
   klarer Grenze, die weder in `agents/` (flacher Identitäts-Store) noch in `sessions/` passt;
   ein Modul end-to-end statt mehrerer flacher. Der Scanner ist ein Subpaket
   (`core/projects/scanners/`), kein eigenes Top-Level-Modul.
2. **`project_id` als Funktionsargument, nicht im `agent_id`-String.** Session-/Run-/Tool-APIs
   bekommen ein optionales `project_id` (None = global/Identität). Die Adressform `agent@projekt`
   wird nur an der Außenkante (CLI/RPC-Eingang, Spawn-Ziel, Anzeige) geparst/gebaut — eine
   Parse/Format-Stelle, danach getrennte Felder.
3. **Eine Agent-Auflösung als Runtime-Einstiegspunkt.** Statt `runtime.agents.get(agent_id)` direkt
   rufen alle Lauf-Pfade einen Resolver `resolve_agent(project_id, agent_id) -> RuntimeAgent`. Kein
   Projekt → AgentStore (wie heute); mit Projekt → Scanner-Profil. `runtime.agents` (AgentStore)
   bleibt für Identitäts-CRUD; der Resolver wrappt ihn.
4. **Anker hält keine Konfig.** `projects/<project-id>/agents/<id>/` hält nur Sessions-Besitz +
   lokale `agent_id` + current-session. Konfig kommt live aus dem Scan/Repo.
5. **Tool-Context bekommt `cwd` zusätzlich zu `workspace`.** `workspace` bleibt für das Memory-Tool
   (nur Identitäts-Agents); Datei-Tools wechseln auf `cwd`.

### Milestones

| # | Milestone | Deliverable (verifizierbar) |
|---|---|---|
| M1 | Projekt-Domäne | `core/projects/` CRUD + Anker-Layout; Unit-Tests grün; Projekt anlegen/lesen/archivieren |
| M2 | Adressierungs-Rückgrat | `project_id` fließt durch Sessions/Runs/Chat-Loop/Tool-Context; Projekt-Session anlegen+öffnen unter Anker; Tools lösen gegen cwd; Tests grün |
| M3 | Scanner + Auflösung | OpenCode-Scan eines Fixture-Repos liefert Team + Report; Resolver liefert lauffähiges Config-Agent-Profil; Model-Kette inkl. Fehlerfall |
| M4 | System-Prompt | Prompt eines Config-Agents = Body + project_files (+ geerbte Blöcke); Identitäts-Agent unverändert; rooted-Reihenfolge korrekt |
| M5 | RPC + CLI | End-to-end via CLI: `project add` → Team sehen → mit `orchestrator@vbot` chatten → Cron auf Projekt-Agent |

### Phase Breakdown

#### Phase 1: Projekt-Domäne (M1)
**Goal:** Die Projekt-Entität existiert mit Persistenz und Anker, isoliert testbar.
**Can run in parallel with:** none (Fundament)

- Projekt-Entität + `project.json`-Schema (project_id-Slug, display_name, cwd, default_agent,
  default_model, auto_load[]) + Validierung — read: [.vorch/domain-maps/agent.md, .vorch/domain-maps/settings.md],
  files: [core/projects/__init__.py, core/projects/projects.py, tests/core/projects/test_projects.py]
- Anker-Lifecycle (Data-Dir-Layout `projects/<id>/`, `agents/<id>/sessions|workspace`, CRUD,
  Archiv beim Entfernen via vorhandenem Archiv-Muster) — files: [core/projects/store.py,
  tests/core/projects/test_store.py]
- cwd-Normalisierung + Dubletten-Check (selber Ordner zweimal abgelehnt; fehlende/verschobene cwd
  erkannt) — files: [core/projects/paths.py, tests/core/projects/test_paths.py]
- Schema-Validierung in die zentrale Validierung einhängen — files: [core/settings/validation.py,
  tests/core/settings/test_validation.py]

**Dependencies:** keine.
**Done when:** `core/projects/` legt ein Projekt an, liest/listet es, archiviert es; `project.json`
geht durch die zentrale Validierung; cwd-Dubletten + fehlende cwd werden erkannt; Unit-Tests grün.

#### Phase 2: Adressierungs-Rückgrat (M2)
**Goal:** `project_id` fließt durch die gesamte Session/Run/Tool-Adressierung; cwd ist getrennt.
**Can run in parallel with:** none (zentral, viele Berührungen)

- Session-Manager projekt-skopiert: `sessions_dir`/`get`/`create`/`get_or_create`/`exists`/
  `list`/`metadata`/`write_lock`/`delete` bekommen optionales `project_id` (None = `agents/<id>/`,
  gesetzt = `projects/<pid>/agents/<id>/`) — read: [.vorch/domain-maps/sessions.md],
  files: [core/sessions/sessions.py, tests/core/sessions/test_sessions.py]
- Run-/Queue-Keys projekt-aware (Key `(project_id, agent_id, session_id)`; UUID-Eindeutigkeit
  reicht nicht für die Pfadfindung) — read: [.vorch/domain-maps/runs.md],
  files: [core/runs/runs.py, tests/core/runs/test_runs.py]
- Tool-Context: `cwd`-Feld zu `ToolContext` + `ToolExecutionConfig`; Aufbau in tool_dispatch
  (cwd = Projekt-cwd bei Projekt-Session, sonst Workspace) — read: [.vorch/domain-maps/tools.md, .vorch/domain-maps/chat.md],
  files: [core/tools/tools.py, core/chat/tool_dispatch.py, tests/core/tools/test_tools.py]
- Datei-Tools lösen gegen `context.cwd` statt `context.workspace` (memory.py bleibt auf workspace) ⚡ *parallel mit nächstem Task* —
  files: [core/tools/read.py, core/tools/write.py, core/tools/edit.py, core/tools/search.py, core/tools/grep.py, core/tools/glob.py]
- bash-Tool: cwd = `context.cwd` ⚡ *parallel mit vorigem Task* — files: [core/tools/bash.py, core/tools/process.py, tests/core/tools/test_bash.py]
- Chat-Loop: Session über `project_id` öffnen/anlegen; `project_id` aus Session-Meta tragen —
  read: [.vorch/domain-maps/chat.md], files: [core/chat/chat.py, tests/core/chat/test_chat.py]
- Subagent: projekt-skopierter Spawn (`(project_id, agent_id)`), Eltern-Link speichert `project_id`
  mit — read: [.vorch/domain-maps/subagents.md], files: [core/subagents/subagents.py, core/subagents/tracker.py, tests/core/subagents/test_subagents.py]
- Recall + Statistik: projekt-skopierte Session-Discovery (nicht nur `agents/<id>/`) —
  read: [.vorch/domain-maps/recall.md, .vorch/domain-maps/statistics.md],
  files: [core/recall/*, core/statistics/statistics.py, tests/core/recall/*, tests/core/statistics/*]

**Dependencies:** Phase 1.
**Done when:** Eine Projekt-Session wird unter dem Anker angelegt und geöffnet; ein Run ist
projekt-skopiert; Datei-Tools schreiben/lesen relativ zur Projekt-cwd; Identitäts-Sessions
verhalten sich unverändert; alle berührten Test-Suiten grün.

#### Phase 3: Scanner + Agent-Auflösung (M3)
**Goal:** Ein Repo wird zum Team + Report; Projekt-Agents werden zu lauffähigen Profilen aufgelöst.
**Can run in parallel with:** Phase 4 nach dem Resolver-Task (Prompt braucht den Resolver)

- Scanner-Registry + Protokoll (ein Detektor pro Format, Naht wie Provider-Adapter) —
  files: [core/projects/scanners/__init__.py, core/projects/scanners/base.py, tests/core/projects/scanners/test_base.py]
- OpenCode-Detektor: liest `.opencode/agents/` (nicht-rekursiv), übernimmt name(slugifiziert)/
  description/model(1:1)/temperature/Body; Tools+Skills = `*` — files: [core/projects/scanners/opencode.py,
  tests/core/projects/scanners/test_opencode.py]
- Scan-Report: sammelt Unsauberes (schlechtes/unkonfiguriertes Model, Orphan, Kollision,
  nicht-slugifizierbar, verwaiste Session/Default-Zeiger); deterministische Kollisionsauflösung —
  files: [core/projects/scan_report.py, tests/core/projects/test_scan_report.py]
- Uniforme Agent-Auflösung `resolve_agent(project_id, agent_id) -> RuntimeAgent` (Store vs Scan),
  zwei Frische-Ebenen; Model-Kette (Agent → Projekt-Default → global → Fehler) + Model-Existenz/
  Konfig-Prüfung → Report — read: [.vorch/domain-maps/agent.md, .vorch/domain-maps/models.md, .vorch/domain-maps/providers.md],
  files: [core/projects/resolver.py, core/runtime/*, tests/core/projects/test_resolver.py]
- Lauf-Pfade auf den Resolver umstellen (statt direktem `runtime.agents.get`) — files: [core/chat/chat.py,
  core/subagents/subagents.py, core/tools/status.py]

**Dependencies:** Phase 1, Phase 2.
**Done when:** Scan eines Fixture-Repos liefert ein deterministisches Team + einen Report mit den
genannten Problemklassen; `resolve_agent` liefert für einen Projekt-Agent ein lauffähiges Profil
mit korrekt aufgelöstem Model; ein Agent ohne auflösbares Model fällt die Kette durch und steht im
Report; Identitäts-Auflösung unverändert.

#### Phase 4: System-Prompt (M4)
**Goal:** Config-Agents bekommen Body + Projekt-Dateien im Prompt; Identitäts-Agents unverändert.
**Can run in parallel with:** Phase 5-RPC nach diesem Phase-Abschluss

- `system.md`: zwei kollabierende Platzhalter `{agent_body}` + `{project_files}` ergänzen —
  files: [resources/prompts/system.md]
- Prompt-Bau bekommt Projekt-Kontext (cwd + Auto-Load-Liste) hereingereicht; `{agent_body}`
  (wörtlich, wie `{include}`) + `{project_files}` (Auto-Load aus cwd, AGENTS.md zuerst, gewrappt);
  Reihenfolge identität-zuerst über Emptiness-Kollaps — read: [.vorch/domain-maps/prompts.md],
  files: [core/prompts/prompts.py, tests/core/prompts/test_prompts.py]
- Chat-Loop reicht das Projekt der Session in `build_system_prompt` (+ visiting Haupt-Agent:
  Projekt-Dateien als `<system-reminder>` statt Systemprompt) — read: [.vorch/domain-maps/chat.md],
  files: [core/chat/chat.py, tests/core/chat/test_chat_prompt.py]

**Dependencies:** Phase 3 (Resolver liefert den Body), Phase 2 (Projekt der Session).
**Done when:** Der Prompt eines Config-Agents enthält seinen Body wörtlich + AGENTS.md +
Auto-Load-Dateien + die geerbten Blöcke; `{...}` im Body wird nicht expandiert; ein
Identitäts-Agent zu Hause hat unveränderten Prompt; rooted = Identität zuerst, dann Projekt;
Besuch = Projekt-Dateien als Reminder.

#### Phase 5: Server-RPC + CLI (M5)
**Goal:** Projekte und Projekt-Agents sind vollständig über RPC und CLI bedienbar/testbar.
**Can run in parallel with:** RPC-Task und CLI-Task NICHT (CLI ruft RPC), aber innerhalb getrennt

- `project.*`-RPC: add/list/show/set/rm + scan(report) — read: [.vorch/domain-maps/server.md],
  files: [server/rpc/project_methods.py, server/rpc/__init__.py (Registrierung), tests/server/rpc/test_project_methods.py]
- Session/Chat-RPC projekt-aware (project_id in Session-Erzeugung/-Abruf/Run-Start);
  Adressform `agent@projekt` am Eingang parsen — files: [server/rpc/chat_methods.py, server/rpc/session_methods.py, tests/server/rpc/test_chat_methods.py]
- Cron projekt-aware: Job-Schema trägt `project_id` (bzw. parst `agent@projekt`); Feuern erzeugt
  Session im Projekt; „Agent in Benutzung"-Sperre matcht qualifiziert — read: [.vorch/domain-maps/automation.md],
  files: [core/automation/cron.py, core/automation/automation.py, server/rpc/automation_methods.py, tests/core/automation/test_cron.py]
- Projekt-Entfernen-Sperre: blockiert bei aktiven/eingereihten Runs oder Cron-Zeiger auf
  Projekt-Agents — files: [server/rpc/project_methods.py, tests/server/rpc/test_project_methods.py]
- CLI-Bereich `project add|list|show|set|rm` + `@projekt` auf positionalem Agent-Argument (session,
  chat, cron) — read: [.vorch/domain-maps/cli.md], files: [cli/parser.py, cli/main.py,
  cli/project_management.py, cli/session_management.py, cli/cron_management.py, tests/cli/test_project_management.py]
- vBot-CLI-Skill aktualisieren (neue Befehle/Adressform) — files: [resources/skills/vbot-cli/SKILL.md, resources/skills/vbot-cli/references/commands.md]

**Dependencies:** Phasen 1–4.
**Done when:** `python cli/main.py project add <pfad>` legt ein Projekt an und zeigt die
Scan-Vorschau; `project show <id>` zeigt Team + Report; eine Chat-Session mit `orchestrator@vbot`
läuft über RPC/CLI mit cwd = Repo und Body+Projekt-Dateien im Prompt; ein Cron-Job auf
`builder@vbot` feuert eine Session im Projekt; Projekt-Entfernen wird bei aktivem Run/Cron-Zeiger
geblockt.

### Done when (Plan gesamt)
- Über CLI/RPC vollständig: Projekt hinzufügen (mit Scan-Report) → Team auflisten → mit einem
  Projekt-Agent chatten (Config- und rooted/visiting Identitäts-Agent) → per Cron ansteuern →
  Projekt verwalten/entfernen. Keine WebUI nötig.
- `python scripts/quality.py` grün für alle berührten Backend-Pakete.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `project_id`-Durchstich verfehlt eine Aufrufstelle → Projekt-Session nicht gefunden | High | High | Phase 2 zuerst und vollständig; grep-Inventar aller `(agent_id, session_id)`-Aufrufer; Tests pro Aufrufpfad |
| Datei-Tool übersieht cwd-Umstellung → schreibt in Workspace statt Repo | Med | High | Ein Task fasst alle Datei-Tools; Test pro Tool, dass relative Pfade gegen cwd auflösen |
| Resolver-Fork wird an einer Lauf-Stelle umgangen → Projekt-Agent nicht ladbar | Med | High | Phase 3 stellt alle Lauf-Pfade um; grep auf `runtime.agents.get` als Checkliste |
| cwd-Normalisierung Windows≠Linux → Dubletten/Re-Point unzuverlässig | Med | Med | Eigener `paths.py` + beidflavorige Tests; Entscheidung dokumentieren |
| Body-Wörtlichkeit verletzt → `{...}` im OpenCode-Body wird expandiert | Low | Med | Body über den `{include}`-Pfad (gewrappt, nicht nach-expandiert); expliziter Test mit Klammern im Body |

### Open decisions (für den Reviewer)
- **Run-Key-Form:** `(project_id, agent_id, session_id)` vs. weiterhin `(agent_id, session_id)` mit
  separatem Pfad-Lookup. Default: ersteres (explizit, kein verstecktes Lookup). Reversibel, aber
  prägt die Run-API.
