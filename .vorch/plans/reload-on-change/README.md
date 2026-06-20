# Plan: Reload-on-Change — app-weiter Invalidierungs-Kanal + Client-Präsenz

**Goal:** Ein einziger app-weiter „Ressource X hat sich geändert → lad nach"-Kanal, über den jede
Änderung an geteiltem App-Zustand (Modellkatalog, Provider, Warteschlange, Sessions, Agenten,
Präsenz) an alle offenen Fenster gemeldet wird; offene Fenster laden das Betroffene schonend neu.
Dazu eine Client-Präsenz-Anzeige („wer ist verbunden"). Der Chat-/Run-Live-Stream bleibt
unangetastet.

**Context:** Vollständige Motivation, der im Code verifizierte Ist-Zustand und **alle getroffenen
Designentscheidungen** stehen in [`stuff/reload-changed.md`](../../../stuff/reload-changed.md) — vor
dem Arbeiten lesen. Kurz: Heute pusht der Server bei einem Modell-DB-Refresh (o. ä.) nichts an
offene Fenster; sie zeigen veraltete Listen, bis man den Tab wechselt. Statt das pro Fall zu flicken,
ein generischer Invalidierungs-Mechanismus.

**Requirements (verbatim aus der Besprechung):**
- Ein einziges System, kein zweites daneben. App-Zustand/Verfügbarkeit über B; Chat-/Run-Stream nicht.
- Signal trägt **kein Datum**, nur „lad nach" — die RPC-Abfrage bleibt die Wahrheit.
- Granularität **pro Art** (`models`/`queue`/`sessions`/`providers`/`clients`), optional mit
  Session-Scope.
- Anwenden **nach Oberflächen-Typ**: reine Anzeigen sofort; Picker/Formulare schieben den sichtbaren
  Tausch auf, solange aktiv bearbeitet wird.
- Präsenz: **pro Verbindung** (nur offene App-Fenster — Browser/Desktop; CLI und Kanäle erscheinen
  nicht), **pur „wer ist da"**, Zeile = Accessor + Browser/OS + seit wann + Status, in **„Allgemein"**,
  **Anzeige + „dieses Fenster"-Markierung**, kein Kappen.
- Volle Umstellung wird **komplett** durchgezogen; Split dient nur der Kontext-Hygiene.

**Scope:**
- **In:** generischer Invalidierungs-Kanal; Migration aller App-Verbraucher (Modellkatalog,
  Provider-Schlüssel, Warteschlange, Sessions; Einklinken der heutigen Agenten-/Login-Ereignisse);
  Client-Präsenz (Registry + Anzeige).
- **Out:** systemweites Aktivitäts-Dashboard; jede Änderung am Chat-/Run-Live-Stream; „Verbindung
  kappen"; Client-Gruppierung pro Gerät; benutzervergebene Client-Namen.

---

## Ausführung dieses Plans (für den Orchestrator lesen)

Dieser Plan ist in **Teile** geschnitten. **Ein Teil = eine Session.** Ablauf:

1. Du bekommst **genau einen Teil** („mach Teil N"). Verstehe dich als **Orchestrator**, nicht als
   Einzel-Bauer.
2. Lies **diese README** (Kontext + Architektur-Entscheidungen) und die **Teil-Datei** `part-N-*.md`.
3. Zerlege den Teil in seine **Tasks** und gib jede an einen **Subagenten** (Builder). `⚡`-markierte
   Tasks haben **keine** Datei-Überschneidung und dürfen parallel laufen; alle anderen nacheinander.
   Gib jedem Subagenten **seine Task-Beschreibung + diese README + die Teil-Datei** mit, damit er
   Aufgabe *und* Plan-Kontext hat.
4. Nach jeder Task: die **Quality Gates** des Projekts laufen lassen (`scripts/quality.py` /
   `scripts/quality-frontend.py` für die berührten Pfade) — grün, bevor es weitergeht. Auto-Fixes der
   Gates behalten (nie zurückdrehen).
5. Commit pro logischer Einheit (Conventional Commits, direkt auf `main` — siehe `CLAUDE.md`).
6. Teil fertig → **kurz zurückmelden** (was erledigt, Gate-Status, offene Punkte). Der Nutzer startet
   dann eine frische Session für den nächsten Teil.

**Wichtig:** Phasen müssen **nicht** autark/„App danach nutzbar" sein — der Schnitt dient nur der
Kontext-Hygiene. Die schwere Lese-/Schreibarbeit gehört in die frischen Subagent-Kontexte; halte
deinen Orchestrator-Kontext schlank (delegieren, Zusammenfassungen einsammeln, integrieren).

---

## Architektur-Entscheidungen (vorab, nicht neu verhandeln)

1. **Ein Event-Typ mit `kind`-Feld**, nicht ein Typ pro Art. Neues Server-Event `resource_changed`
   mit `{ kind: "models"|"queue"|"sessions"|"providers"|"clients", scope?: {agent_id?, session_id?} }`.
   In `ALLOWED_SERVER_EVENT_TYPES` aufnehmen. Eine neue Art = eine `kind`-Konstante, kein neues
   Plumbing. **Das Event trägt keine Nutzdaten** außer `kind`/`scope`.
2. **Emission lebt in der Server-RPC-Schicht** (dort liegt der `event_bus`), über die
   Publish-Helfer-Konvention aus `server/rpc/event_bridge.py` — **nicht** in `core/`.
3. **Bestehende Ereignisse:** Agenten-CRUD (`agent.created/updated/deleted`) → auf `resource_changed`
   (`kind: "agents"`) umstellen, damit es *ein* Mechanismus ist. **`provider_auth_completed` bleibt
   ein eigenes, gezieltes Event** (die OAuth-Maske matcht auf seine Nutzlast — das ist kein reines
   „lad nach"); **zusätzlich** wird daneben `resource_changed(kind:"providers")` emittiert — aber
   **erst beim Login-Abschluss** (im OAuth-Completion-Callback, neben `provider_auth_completed`),
   **nicht** beim Start des Connect-Flows (da ist die Verbindung noch nicht hergestellt). API-Schlüssel
   setzen/entfernen und Trennen ändern sofort und emittieren synchron. *(Entscheidung aufgelöst —
   siehe unten.)*
4. **Client-Funnel** ist `App.svelte` → `handleServerEvent` (einziger Eintritt; `connectionState.js`
   reicht nur durch). Ein Branch für `resource_changed` verteilt nach `kind` an die betroffenen
   Flächen über **Refresh-Token-Props** (genau das Muster des bestehenden `agentsRefreshToken`):
   App hält pro Art einen Token, erhöht ihn beim Signal, reicht ihn runter; die Fläche lädt bei
   Token-Änderung neu.
5. **Anwende-Verhalten** als kleiner geteilter Client-Helfer: reine Anzeigen laden sofort;
   Picker/Formulare schieben den sichtbaren Tausch auf, solange aktiv bearbeitet (Dropdown offen /
   Feld im Fokus / debounced Save ausstehend). Bestehende Auswahl bleibt; verschwundene Auswahl
   bleibt als „nicht mehr verfügbar" sichtbar (vorhandenes Muster in `modelSelection.js`).
6. **Präsenz:** server-seitige **Verbindungs-Registry** (neues kleines Modul, analog zu
   `server/events.py`), gepflegt im WS-Endpunkt-Lebenszyklus (`server/app.py`, registrieren vor der
   `subscribe`-Schleife, deregistrieren in einem **`finally` — das heute fehlt und neu anzulegen ist**,
   damit auch andere Abbrüche aufräumen). Der Client **mintet eine Verbindungs-ID** (**pro
   Verbindung/Tab, nicht `localStorage`** — sonst kollabieren mehrere Tabs zu einem Eintrag) und sendet
   Accessor-Typ + ID beim WS-Connect (über `api.js` → Query-Params); der Server liest sie und leitet
   Browser/OS aus dem `User-Agent` ab. **Erfasst werden nur offene App-Fenster** (Browser/Desktop) —
   CLI/Kanäle halten keine `/ws`-Fensterverbindung. Neue RPC **`client.list`** liefert den Roster.
   Connect/Disconnect → `resource_changed(kind:"clients")`. „Dieses Fenster"-Markierung: der Client
   matcht seine eigene gemintete ID im Roster.

7. **Session-Wechsel zieht andere Fenster nicht mit.** Erzeugt/wechselt ein Fenster die aktuelle
   Session (`/new`, `/handoff`, neue Session), frischen andere Fenster nur ihre Session-Liste und die
   „aktuell"-Markierung auf — sie springen **nicht** automatisch in die neue Session. Einziger
   Emit-Punkt ist die Session-Erzeugung selbst (deckt `/new` und `/handoff` mit ab; `chat_methods.py`
   wird dafür nicht angefasst).

### Aufgelöste Entscheidungen (Plan-Review 2026-06-20)
- **`provider_auth_completed`: Default.** Bleibt als gezieltes Event; **zusätzlich**
  `resource_changed(kind:"providers")` **beim Login-Abschluss** (Completion-Callback), nicht beim
  Connect-Start. Kein Umbau am OAuth-Abschluss.
- **Warteschlange: nur Browser-/RPC-Sends.** Die Queue-Invalidierung deckt die RPC-Sende-Fläche ab
  (das, was der Browser nutzt). Einreihungen aus Automation/Kanälen/Unter-Agenten (im Kern) lösen
  **bewusst kein** Queue-Signal aus — Scope-Grenze, hält den Chat-Kern unangetastet.
- **Session-Wechsel: andere Fenster bleiben stehen** (nur Liste/Markierung auffrischen, kein
  automatischer Wechsel) — siehe Entscheidung 7.
- **Präsenz: nur offene App-Fenster** (Browser/Desktop); CLI/Kanäle erscheinen nicht.

---

## Teile (= Sessions)

| Teil | Inhalt | Liefert |
|---|---|---|
| **1** | [Fundament + Modellkatalog](part-1-foundation-models.md) | Der Kanal end-to-end + erster Verbraucher; behebt den Ausgangs-Bug |
| **2** | [Warteschlange + Sessions + Alt-Ereignisse einklinken](part-2-queue-sessions-events.md) | Restliche App-Verbraucher auf B |
| **3** | [Client-Präsenz](part-3-presence.md) | „Wer ist verbunden"-Anzeige in „Allgemein" |

Reihenfolge: **1 → 2 → 3** (2 und 3 hängen am Fundament aus 1). Innerhalb jedes Teils sind
parallelisierbare Tasks mit `⚡` markiert.

---

## Risks & Mitigations

| Risiko | Wahrsch. | Wirkung | Mitigation |
|---|---|---|---|
| Nachladen reißt eine offene Auswahl/halbe Eingabe weg | Mittel | Mittel | Anwende-Verhalten nach Oberflächen-Typ (Entscheidung 5); Picker/Formulare schieben den Tausch auf |
| `agent.*`-Umstellung bricht den bestehenden Agenten-Reload | Niedrig | Mittel | In Teil 2 isoliert, mit den **`App.test.js`**-Routing-Tests (`agent.list`-Mocks) als eigentlichem Netz; die `agent.*` in `connectionState.test.js` sind nur Sequenz-Fixtures |
| Doppelte Invalidierung (z. B. Provider-Key ändert Modelle *und* Verbindungen) | Niedrig | Niedrig | `kind` ist billig; Fläche lädt ihre Verfügbarkeitsdaten (Modelle **und** Verbindungen) zusammen |
| Präsenz-Registry leakt Einträge bei hartem Verbindungsabbruch | Mittel | Niedrig | De-Registrierung im **neu anzulegenden** `finally` des WS-Handlers (heute nur `try/except`); Roster ist ohnehin Momentaufnahme |
| Geteilte Client-ID (`localStorage`) kollabiert mehrere Tabs zu einem Eintrag | Mittel | Mittel | ID **pro Verbindung** (oder `sessionStorage` pro Tab), **nie** `localStorage` — Teil 3, Task 3B |
| WS-Identität als Query-Param unsauber/zu lang | Niedrig | Niedrig | Nur kurze ID + Accessor-Typ; Browser/OS serverseitig aus `User-Agent` |
