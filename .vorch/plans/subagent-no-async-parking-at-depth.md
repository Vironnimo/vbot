## Plan: Kein Async-Parken in Sub-Agenten (blockierend ab Tiefe)

**Goal:** Ein Sub-Agent (Schachtelungstiefe ≥ 1) kann keine Arbeit mehr in einen späteren Lauf „parken" — Spawns laufen blockierend, Hintergrund-bash ist gesperrt — sodass der Lauf eines Sub-Agenten erst endet, wenn seine Arbeit wirklich fertig ist und das Ergebnis korrekt eine Ebene nach oben weitergegeben wird.

**Context:** Nicht-blockierender `subagent`-Spawn und Hintergrund-`bash` sind beide dasselbe Versprechen: Arbeit parken, in einem *späteren* Lauf per Aufweck-Notiz wieder aufwachen. Das setzt eine dauerhafte, beobachtete Session voraus. Eine Sub-Agent-Session ist flüchtig und existiert nur, um ihrem Aufrufer *ein* Ergebnis zu liefern. Auf Tiefe ≥ 1 landet die Aufweck-Notiz daher in einer Session, die oben niemand mehr liest: Der Aufrufer bekommt verfrüht „läuft noch" als finale Antwort, das echte Ergebnis verwaist (die Abschluss-Weitergabe reicht nur genau eine Ebene). Verifiziert in `core/subagents/subagents.py`, `core/subagents/tracker.py`, `core/tools/bash.py`.

Gewählter Ansatz: **Parken auf Tiefe verbieten** (Option A). Bewusst *nicht* gewählt: Abschluss über alle Ebenen propagieren (zu komplex/riskant) oder den Sub-Agent-Lauf offen halten, bis Arbeit ruht (mittlerer Umbau am Lauf-Antrieb) — beide bleiben spätere Optionen und vertragen sich mit A. A kostet **keine** Parallelität: Tool-Aufrufe aus *einem* Modell-Zug laufen nebenläufig (`ToolExecutor.execute_many` → `asyncio.gather`, Grenze 50 in `core/tools/tools.py`); das Sub-Agent-Limit pro Zug ist 8 (50 ≫ 8). Bündelt ein Orchestrator seine Spawns in einem Zug, laufen sie parallel und sein Lauf wartet am `gather`-Punkt korrekt auf alle. A entfernt nur das ebenenübergreifende „feuern und Zug ganz beenden" — genau das auf Tiefe kaputte Muster.

`nesting_depth` liegt auf `ToolContext` (`core/tools/tools.py`) und ist an beiden Stellen verfügbar (das Sub-Agent-Tool nutzt es bereits für sein Tiefenlimit). Tiefe 0 = oberste Ebene (unverändert), ≥ 1 = Sub-Agent (neue Regel).

**Scope:**
- **In:** Sub-Agent-Spawn auf Tiefe → blockierend erzwingen; Hintergrund-bash auf Tiefe → gesperrt (explizit *und* automatisch); Tool-Beschreibung anpassen; je ein benannter Rückfall-Schalter; Doku. Separater Commit: Hintergrund-bash-Aufweckung reicht das Projekt nicht durch (Datenverlust in Projekt-Sessions).
- **Out:** Oberste Ebene (Tiefe 0) bleibt unverändert (nicht-blockierend + Hintergrund wie heute). Keine Quer-Ebenen-Propagation (Option B), kein Offenhalten von Läufen (Option C). Keine Migration (Vorwärts-only-Konvention).

**Phases:**

### Phase 1: Sub-Agent-Spawn blockierend ab Tiefe
*Eigener Commit. ⚡ parallelisierbar mit Phase 2 (keine Datei-Überlappung).*

- Spawn-Handler: nach dem Parsen von `blocking` auf Tiefe ≥ 1 auf `True` zwingen, über einen benannten Schalter (z. B. `FORCE_BLOCKING_AT_DEPTH = True`, dokumentiert als Rückfall-Punkt analog zu `CASCADE_NON_BLOCKING_CHILDREN`). Wurde `blocking` dabei von false→true überschrieben, dem Erfolgs-Ergebnis eine einzeilige Notiz beilegen („lief blockierend, weil Sub-Agent; für Parallelität alle subagent-Aufrufe in einem Zug"). — read: [.vorch/domain-maps/subagents.md], files: [core/subagents/subagents.py]
- Tool-Beschreibung um einen Satz zum Verhalten auf Tiefe ergänzen, ohne die Oberste-Ebene-Anleitung zu verwirren („innerhalb eines Sub-Agenten laufen Spawns immer blockierend; für mehrere parallel: alle Aufrufe in einem Zug"). — files: [core/tools/subagent.py]
- Tests: Tiefe ≥ 1 + `blocking` weggelassen/false → Ergebnis-Payload des Kindes (nicht „running"-Deskriptor); überschriebene Notiz vorhanden. Regression: Tiefe 0 nicht-blockierend → weiterhin „running"-Deskriptor. — files: [tests/core/tools/test_subagent.py, tests/core/subagents/test_subagents.py]

**Done when:**
- Ein Spawn auf Tiefe ≥ 1 ohne/`blocking=false` gibt das fertige Kind-Ergebnis zurück, kein „running".
- Ein Spawn auf Tiefe 0 ohne `blocking` gibt unverändert den „running"-Deskriptor zurück.

### Phase 2: Hintergrund-bash ab Tiefe sperren (beide Wege)
*Eigener Commit. ⚡ parallelisierbar mit Phase 1.*

- Auf Tiefe ≥ 1, hinter einem benannten Schalter (z. B. `BLOCK_BACKGROUND_AT_DEPTH = True`):
  - **Explizit** (`background: true`) → vor dem Prozessstart mit klarer Meldung ablehnen (kein Prozess, kein Completion-Watcher).
  - **Automatisch** (Vordergrundphase erreicht die `yield_after`-Schwelle, Prozess läuft noch) → statt in den Hintergrund zu verschieben: Prozess killen und mit begrenzter, klarer Meldung scheitern (Entscheidung 1 = A: „Befehl nicht binnen {yield_after}s fertig; Hintergrundausführung im Sub-Agenten nicht verfügbar — sorg dafür, dass er terminiert, oder setz ein timeout"). Kein Completion-Watcher, keine Aufweck-Notiz.
  - Ein Vordergrundbefehl, der **binnen** `yield_after` fertig wird, bleibt unverändert synchron erfolgreich.
- Tests: Tiefe ≥ 1 + `background:true` → `tool_failure`, kein Trigger; Tiefe ≥ 1 + Vordergrund über Schwelle → `tool_failure` (gekillt), kein Trigger; Tiefe ≥ 1 + schneller Vordergrundbefehl → Erfolg. Regression: Tiefe 0 + `background:true` → unverändert Hintergrund + Watcher. — read: [.vorch/domain-maps/tools.md], files: [core/tools/bash.py, tests/core/tools/test_bash.py]

**Done when:**
- Auf Tiefe ≥ 1 erzeugt weder expliziter noch automatischer Hintergrund je einen Completion-Watcher/Aufweck-Trigger; beide enden in einem begrenzten `tool_failure`.
- Auf Tiefe 0 ist das bash-Hintergrundverhalten unverändert.

### Phase 3: Bugfix — Hintergrund-bash-Aufweckung trägt das Projekt mit
*Separater Commit. Sequentiell nach Phase 2 (gleiche Datei `core/tools/bash.py`).*

- Den Completion-Watcher das Projekt seines Aufrufers mitführen lassen (`context.project_id`) und an die Aufweck-`trigger_run(..., project_id=...)` durchreichen — eins-zu-eins wie die Sub-Agent-Abschlussnotiz in `core/subagents/tracker.py`. Heute fehlt das, sodass in einer Projekt-Session die „fertig"-Meldung projektlos kommt (falsches Arbeitsverzeichnis / Projekt-Dateien fehlen) oder die Session gar nicht gefunden und die Meldung still verworfen wird. Unter Option A nur noch auf Tiefe 0 relevant. — files: [core/tools/bash.py, tests/core/tools/test_bash.py]
- Test: Hintergrund-Completion in einer projekt-skopierten Session → `trigger_run` wird mit gesetztem `project_id` aufgerufen.

**Done when:**
- Die Hintergrund-Completion-Aufweckung wird mit dem `project_id` des auslösenden Laufs ausgelöst (Test belegt die Durchreichung).

### Phase 4: Doku nachziehen
*Mit dem jeweiligen Code-Commit oder als abschließender Doku-Commit.*

- Domänenkarte Sub-Agenten: neue Tiefenregel (blockierend erzwungen ab Tiefe ≥ 1; nicht-blockierend nur auf oberster Ebene), inkl. Schalter. — files: [.vorch/domain-maps/subagents.md]
- Tools-/bash-Doku: Hintergrund-bash ab Tiefe gesperrt (beide Wege) + Projekt-Durchreichung der Completion-Aufweckung. — files: [.vorch/domain-maps/tools.md]

**Done when:**
- Beide Domänenkarten beschreiben Tiefenregel und Schalter faktisch und stimmen mit dem Code überein.

**Risks / Assumptions:**
- **Ein Sub-Agent weiß evtl. nicht, dass er einer ist** → die statische Tool-Beschreibung allein könnte das Batching nicht auslösen. Mitigation: die kontextuelle Notiz im erzwungen-blockierenden Ergebnis (Phase 1) landet genau dann, wenn relevant. Korrektheit hängt nicht davon ab (das Ergebnis ist so oder so vollständig) — nur die Parallel-Latenz bei zugübergreifend gestreuten Spawns. Die Ergebnis-Notiz ist der entbehrliche Teil, falls sie als Rauschen auffällt.
- **Folge, kein Beschluss:** erzwungen-blockierende Kinder registrieren dann den Eltern-Abbruch-Kaskaden-Callback (blockierende Semantik) — ein Sub-Agent-Kind stirbt mit seinem Elternteil. Konsistent, beabsichtigt.
- **`yield_after`-Default 30s** (`DEFAULT_YIELD_AFTER_SECONDS`): auf Tiefe ist das die maximale synchrone Wartezeit vor Kill+Fehler. Akzeptabel; der Agent kann mit `timeout` enger begrenzen.
- Annahme: `subagent_result` auf Tiefe braucht keine Änderung — ohne nicht-blockierende Kinder degradiert es sauber auf „letzte Assistant-Nachricht der Session". Keine Migration nötig (Vorwärts-only).
