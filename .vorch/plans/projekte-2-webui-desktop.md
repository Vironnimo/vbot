## Plan: Projekte — WebUI / Desktop

**Goal:** Projekte sind aus der WebUI (und damit Desktop) voll bedienbar: ein Projekte-Tab zum
Hinzufügen/Verwalten und ein Zwei-Bar-Chat mit Projekt-Dropdown — als reine Client-Schicht auf
dem RPC-Vertrag aus Plan 1.

**Context:** Design-Grundlage `stuff/add-projects.md` → Abschnitt „Accessor-/UI-Fläche"
(Stand 2026-06-18) — **vor Phase 1 lesen**, dazu `.vorch/DESIGN.md`. Zweiter von zwei Plänen;
**hängt vollständig an Plan 1** (`projekte-1-kern-cli.md`): erst wenn dessen `project.*`-RPC und die
projekt-skopierten Session/Chat/Cron-Methoden stehen, kann diese Schicht gebaut werden. Die WebUI
importiert keinen Backend-Code — nur HTTP/RPC/SSE/WS.

**Requirements (entschieden in der Design-Phase, verbatim):**
- **Chat: zwei Bars, kein Modus-Umschalter.** Obere Bar = Identitäts-Agents, immer sichtbar/wählbar.
  Projekt-Dropdown (Default „Kein Projekt" = Persönlich); ein Projekt wählen → **eine zweite Bar**
  darunter mit dessen Team. Ein Projekt zur Zeit.
- **Projekt wählen = öffnen:** re-scannt, füllt die zweite Bar, springt auf den Projekt-Default-Agent.
  Unsauberes aus dem Scan → schmales, **nicht-blockierendes Banner** im Chat, das in den
  Projekte-Tab verlinkt.
- Identitäts-Agents arbeiten unabhängig von der zweiten Bar an Projekten; leeres Projekt = leere
  zweite Bar, **kein Fehlerfall**.
- **Projekte-Tab:** Hinzufügen über **Pfad von Hand** (Server-Pfad!) + server-seitige Validierung mit
  **Scan-Vorschau**; Liste; Verwalten pro Projekt (cwd / Default-Agent / Default-Model /
  Auto-Load-Liste, entfernen); Scan-Report lebt hier.
- **Scan-Report:** nicht-blockierend; informativ in v1, mit *einer* aktionablen Ausnahme **Re-Point**
  bei fehlender cwd.
- **Cron:** kein Projekt-Picker — eine Agent-Liste = Identitäts-Agents + Projekt-Agents, Projekt-Agents
  in der Adressform **`agent@projekt`** angezeigt und gespeichert.
- **Desktop:** erbt WebUI 1:1; in v1 **kein** nativer Picker (gleiche Pfad-Eingabe, weil Server remote
  auf dem Pi liegen kann).

**Scope:**
- **In:** API-Client-Wrapper für `project.*`; Projekte-Tab (Hinzufügen/Liste/Verwalten/Report);
  Chat Zwei-Bar + Projekt-Dropdown + Report-Banner; Cron-Agent-Liste mit `agent@projekt`; i18n;
  Desktop-Capability-Check (kein Picker).
- **Out:** Backend/RPC (Plan 1); nativer Ordner-Picker; Projekt-Filter in Statistik/Logs;
  Projekt-Verwaltung über die CLI (steckt in Plan 1).

**Assumptions & Constraints:**
- Svelte + JavaScript (kein TS); alle sichtbaren Strings über `i18n.js`; Svelte-5-Callback-Props,
  keine neuen Event-Dispatcher; UI-Primitive aus `components/ui/` wiederverwenden (`.vorch/DESIGN.md`).
- Der „ausgewählte Agent"-State (heute App-level + localStorage) wird um einen **Projekt-Kontext**
  erweitert; die zweite Bar ist eine Projektion des `project.show`/Scan-Ergebnisses, **keine** zweite
  Wahrheit.
- Projekt-Sessions kommen über die in Plan 1 projekt-aware gemachten Session/History-RPCs — die
  WebUI baut nie Pfade.
- **Doc-Pflege ist Teil jeder Phase, nicht aufgeschoben** (CLAUDE.md): die berührten Frontend-Maps
  werden in derselben Arbeit aktualisiert; die „Done when" schließt das ein — siehe „Doc-Pflege pro
  Phase" unten.

### Milestones

| # | Milestone | Deliverable (verifizierbar) |
|---|---|---|
| M1 | Transport + Projekte-Tab | `project.*` im API-Client; Projekte-Tab listet/fügt hinzu/verwaltet; Scan-Vorschau + Report sichtbar |
| M2 | Zwei-Bar-Chat | Identitäts-Bar immer; Projekt-Dropdown → zweite Team-Bar; Öffnen springt auf Default-Agent; Report-Banner |
| M3 | Cron + Politur | Cron-Agent-Liste zeigt `agent@projekt`; i18n vollständig; Desktop ohne Picker |

### Phase Breakdown

#### Phase 1: Transport + Projekte-Tab (M1)
**Goal:** Projekte hinzufügen/verwalten in eigenem Tab; Scan-Vorschau + Report sichtbar.
**Can run in parallel with:** Phase 2 erst nach dem API-Client-Task

- API-Client: dünne Wrapper für `project.add/list/show/set/rm` (+ scan) — read: [.vorch/domain-maps/webui.md],
  files: [webui/src/lib/api.js, webui/src/lib/__tests__/api.test.js]
- Reiner Helfer für Projekte-View-State (Add-Payload, Pfad-Validierungs-Spiegel, Manage-Payloads,
  Report-Normalisierung) ⚡ *parallel mit nächstem Task* — files: [webui/src/lib/projectsView.js,
  webui/src/lib/__tests__/projectsView.test.js]
- `ProjectsView.svelte`: Hinzufügen (Pfad-Eingabe → Scan-Vorschau: gefundenes Team + Probleme),
  Liste, Verwalten (cwd/Default-Agent/Default-Model/Auto-Load, Entfernen), Report-Anzeige mit
  Re-Point-Aktion bei fehlender cwd ⚡ *parallel mit vorigem Task* —
  files: [webui/src/components/projects/ProjectsView.svelte, webui/src/components/projects/__tests__/ProjectsView.test.js]
- Navigation: neuen Tab „Projekte" in den Shell hängen — files: [webui/src/App.svelte]

**Dependencies:** Plan 1 (RPC steht).
**Done when:** Im Projekte-Tab lässt sich ein Server-Pfad eingeben, die Scan-Vorschau zeigt Team +
Probleme, ein Projekt wird angelegt/verwaltet/entfernt; fehlende cwd bietet Re-Point; Vitest grün.

#### Phase 2: Zwei-Bar-Chat + Projekt-Dropdown (M2)
**Goal:** Chat zeigt Identitäts-Bar immer + Team-Bar je nach Projekt-Dropdown; Öffnen verhält sich
wie spezifiziert.
**Can run in parallel with:** none (berührt App- und ChatView-Kern)

- App-State: Projekt-Kontext neben „selected agent" (Dropdown-Auswahl inkl. „Kein Projekt"),
  Persistenz analog localStorage-Muster — read: [.vorch/domain-maps/webui.md],
  files: [webui/src/App.svelte]
- ChatView: Projekt-Dropdown + zweite Agent-Bar (Team aus `project.show`/Scan), Auswahl eines
  Agents aus beiden Bars, „Öffnen" springt auf Default-Agent; Sessions des gewählten (Projekt-)Agents
  über die projekt-aware History/Session-RPCs — files: [webui/src/components/chat/ChatView.svelte,
  webui/src/lib/chatState.js, webui/src/components/chat/__tests__/ChatView.test.js]
- Report-Banner: schmales nicht-blockierendes Banner bei Projekt-Öffnen mit Problemen, verlinkt in
  den Projekte-Tab — files: [webui/src/components/chat/ProjectScanBanner.svelte,
  webui/src/components/chat/__tests__/ProjectScanBanner.test.js]

**Dependencies:** Phase 1 (API-Client + View-Helfer), Plan 1.
**Done when:** Identitäts-Agents sind immer in der oberen Bar; Auswahl eines Projekts im Dropdown
zeigt dessen Team in einer zweiten Bar und springt auf den Default-Agent; ein leeres Projekt zeigt
eine leere zweite Bar ohne Fehler; ein unsauberer Scan zeigt das Banner; Vitest grün.

#### Phase 3: Cron + Politur (M3)
**Goal:** Cron-Agent-Auswahl kennt Projekt-Agents; i18n vollständig; Desktop sauber.
**Can run in parallel with:** Tasks untereinander ⚡ (getrennte Dateien)

- Cron-View: Agent-Liste = Identitäts-Agents + Projekt-Agents, Projekt-Agents als `agent@projekt`
  angezeigt/gespeichert ⚡ — read: [.vorch/domain-maps/webui.md, .vorch/domain-maps/automation.md],
  files: [webui/src/components/cron/CronView.svelte, webui/src/lib/cronView.js, webui/src/lib/__tests__/cronView.test.js]
- i18n: alle neuen Strings (Projekte-Tab, Dropdown, Banner, Cron-Labels) in en + vorhandene Sprachen ⚡ —
  files: [webui/src/lib/i18n.js, webui/src/lib/__tests__/i18n.test.js]
- Desktop: Capability-Check bestätigt „kein nativer Picker in v1" (gleiche Pfad-Eingabe); kein
  eigener Bau nötig, nur verifizieren ⚡ — read: [.vorch/domain-maps/desktop.md], files: [desktop/* (nur lesen/prüfen)]

**Dependencies:** Phase 1, Phase 2.
**Done when:** Cron zeigt Projekt-Agents als `agent@projekt` und speichert die Adresse; keine
hardcodierten Strings; Desktop zeigt denselben Add-Flow wie die WebUI; `python scripts/quality-frontend.py` grün.

### Done when (Plan gesamt)
- Nutzer kann in der WebUI ein Projekt per Pfad hinzufügen (mit Scan-Vorschau), es verwalten/entfernen,
  im Chat über das Dropdown öffnen, dessen Team in der zweiten Bar sehen, mit Projekt- und
  Identitäts-Agents chatten und einen Cron-Job auf `agent@projekt` setzen.
- `python scripts/quality-frontend.py` grün.

### Doc-Pflege pro Phase (Teil der jeweiligen „Done when")

| Phase | Domain-Maps / Docs zu aktualisieren (in derselben Arbeit) |
|---|---|
| 1 | `webui.md` (API-Client `project.*`, neuer Projekte-Tab + View-Helfer) |
| 2 | `webui.md` (Zwei-Bar-Chat, Projekt-Dropdown, Report-Banner, Projekt-Kontext im App-State) |
| 3 | `webui.md` (Cron-Agent-Liste `agent@projekt`), `desktop.md` (kein nativer Picker in v1 bestätigt) |

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Zwei-Bar + Dropdown verkompliziert den ohnehin dichten ChatView/App-State | Med | Med | View-Logik in Helfer (`chatState.js`/`projectsView.js`) auslagern, Komponenten dünn halten; an bestehender selected-agent-Mechanik andocken |
| Doc-/Map-Pflege wird trotz Tabelle übersprungen | Med | Med | In jeder „Done when" verankert; `webui.md`/`desktop.md` im `files:`-Scope der jeweiligen Phase mitführen |
| Projekt-Kontext wird zweite Wahrheit neben Server-State | Med | Med | Zweite Bar + Dropdown sind reine Projektion von `project.show`/Scan; keine lokale Mutation der Server-Wahrheit |
| Re-Point/Manage-Aktionen brauchen RPC, das Plan 1 nicht liefert | Low | Med | Vor Phase 1 RPC-Vertrag aus Plan 1 gegenprüfen; fehlende Methoden als Plan-1-Lücke melden, nicht clientseitig basteln |
| Plan 1 noch nicht fertig → Plan 2 blockiert | High | High | Plan 2 erst starten, wenn Plan 1 M5 steht; bis dahin nur gegen den dokumentierten RPC-Vertrag designen |

### Open decisions (für den Reviewer)
- **Dropdown-Platzierung** (in der Agent-Bar vs. Top-Bar) und **Tab-Reihenfolge** „Projekte" — reine
  Kosmetik, beim Bau entscheidbar; Default: Dropdown direkt über der zweiten Bar, Tab nach „Agents".
