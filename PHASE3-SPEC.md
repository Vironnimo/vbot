# Phase 3 Server Spec

Arbeitsdokument für die teuren Strukturentscheidungen von Phase 3.
Diese Datei legt bewusst noch **nicht** die exakten HTTP-, SSE- oder WebSocket-
Payloads fest. Ziel ist, jetzt die Architektur festzuziehen und die Details
später darauf aufzubauen.

## 1. Ziel von Phase 3

Phase 3 baut eine Server-Schicht um den bestehenden Kernel aus Phase 1 und 2.

- `core/` bleibt die fachliche Wahrheit für Agents, Sessions, Chat-Loop,
  Tools, Skills, Provider und Modelle.
- `server/` ist die Transport- und Integrationsschicht.
- WebUI, Desktop und CLI sprechen mit dem Server, nicht direkt mit Providern.

Damit bekommt vBot nach außen einen stabilen Zugangspunkt, auch wenn sich
Provider intern unterscheiden.

## 2. Außenvertrag vs. Provider-Details

Wichtig ist die Trennung zwischen zwei verschiedenen Kommunikationsrichtungen:

1. **Client ↔ vBot-Server**
   - Das betrifft WebUI, Desktop-App, CLI und spätere weitere Accessors.
   - Hier soll vBot einen **einheitlichen** und stabilen Vertrag anbieten.

2. **vBot-Server / Kernel ↔ Provider**
   - Das betrifft OpenAI, Anthropic, OpenRouter usw.
   - Hier darf es Unterschiede geben, weil jeder Provider sein eigenes
     Wire-Protocol und seine eigenen Streaming-Mechaniken hat.

Diese Provider-Unterschiede bleiben im Adapter verborgen. Der öffentliche
Server-Vertrag darf **nicht** davon abhängen, ob ein Provider intern normales
HTTP, SSE oder etwas anderes nutzt.

Falls später nötig, kann bei Providern oder Modellen eine Capability wie
`streaming.supported` ergänzt werden. Das ändert aber nichts an der äußeren
Server-Architektur.

## 3. Begriffe

### Session

Eine Session ist ein systemverwalteter Chat-Verlauf. Sie gehört genau einem
Agenten und enthält die persistierte Nachrichtenhistorie in **einer JSONL-Datei
pro Session**.

Auf Produkt- und Server-Ebene werden neue Sessions **explizit** angelegt, also
nicht implizit nebenbei beim Senden einer Nachricht. Sobald eine Session
existiert, werden ihre Datei und ihre Historie vom System geführt.

### Run

Ein Run ist **eine einzelne Ausführung innerhalb einer Session**:

Neue User-Nachricht → Modell arbeitet → optionale Tool-Calls → weitere
Modellschritte → Abschluss oder Abbruch.

Ein Run ist also:

- **nicht** der Agent
- **nicht** die Session
- **nicht** nur ein einzelner Provider-Request

`cancel` bezieht sich immer auf einen **Run**.

## 4. Nebenläufigkeitsmodell

- Pro **Session** gibt es maximal **einen aktiven Run gleichzeitig**.
- Mehrere **Sessions** dürfen gleichzeitig laufen.
- Da eine Session genau einem Agenten gehört, können auch mehrere Agents
  parallel arbeiten, solange jede Session für sich nur einen aktiven Run hat.

Beispiel: Drei verschiedene Agents dürfen gleichzeitig arbeiten, wenn sie in
drei verschiedenen Sessions laufen.

Ein zweiter Startversuch innerhalb derselben Session, während dort bereits ein
Run aktiv ist, wird abgelehnt.

## 5. Transportmodell zwischen Client und Server

In dieser Datei bedeutet **Client**: WebUI, Desktop-App, CLI oder spätere
weitere Frontends.

Für die Außenkommunikation gilt folgende Rollenverteilung:

- **RPC über HTTP** für Befehle und normale Anfrage/Antwort-Aktionen
- **SSE** für den inkrementellen Ausgabestrom eines einzelnen Runs
- **WebSocket** für allgemeine asynchrone Server-Events

Das bedeutet:

- Chat-Streaming läuft serverseitig als **SSE**, unabhängig davon, wie der
  Provider intern streamt.
- Der **WebSocket** ist **nicht** der primäre Kanal für den Token- oder
  Antwortstrom eines einzelnen Chat-Runs, sondern für allgemeine Push-Events.
- Die Desktop-App nutzt denselben äußeren Server-Vertrag wie die WebUI; sie ist
  nur ein anderer Client.

## 6. Sichtbarkeit im Chat

Ein laufender Run soll für den Nutzer als nachvollziehbarer Chat-Zeitstrahl
sichtbar werden.

Sichtbar sein sollen mindestens:

- lesbare Thinking-/Reasoning-Blöcke des Assistenten, wenn das Modell solche
  liefert
- jeder Tool-Call als eigener sichtbarer Schritt
- Tool-Aktivität und Tool-Ergebnis als sichtbare Fortsetzung dieses Schritts
- jede finale oder zwischenzeitliche Assistant-Antwort

Nicht sichtbar gemacht werden muss dabei jedes provider-spezifische Rohdetail.
Insbesondere bleibt `reasoning_meta` ein internes, opaques Round-Trip-Artefakt.

Diese Sichtbarkeit ist ein Produkt- und Architekturvertrag, **kein** finales
UI-Layout. Wie genau WebUI oder Desktop das darstellen, bleibt späteren
Umsetzungsphasen überlassen.

## 7. Ein Chat-System, nicht drei

`send`, `stream` und `cancel` sind keine drei getrennten Chat-Systeme.

Stattdessen gilt:

- Es gibt **eine** Ausführungslogik für einen Run.
- `send` liefert das Endergebnis gesammelt zurück.
- `stream` liefert dieselbe Ausführung inkrementell.
- `cancel` bricht genau diese laufende Ausführung ab.

Diese Entscheidung ist wichtig, damit später nicht ein eigener Non-Streaming-
Pfad, ein eigener Streaming-Pfad und ein eigener Abbruch-Pfad auseinanderlaufen.

## 8. Session- und Run-Lebenszyklus

- Sessions werden **explizit** erstellt.
- Ein Chat-Turn verweist immer auf eine bestehende Session.
- Die Session bleibt der **persistierte** Verlauf aus Phase 2.
- Ein Run ist der **operative** Zustand darüber.

Die **kanonische persistierte Wahrheit** für die Chat-Historie bleibt die
JSONL-Datei der Session. Wenn ein Run User-, Assistant- oder Tool-Nachrichten
erzeugt, werden diese weiter in dieser Session-Historie gespeichert.

Zusätzlicher flüchtiger Laufzeit-Zustand eines aktiven Runs (zum Beispiel
Abbruch-Handles oder Streaming-Koordinierung) darf in Phase 3 im Speicher
liegen. Das ändert nichts daran, dass die Session-Datei die persistierte Quelle
für den Gesprächsverlauf bleibt.

## 9. Cancel-Semantik

Ein Nutzer darf einen aktiven Run jederzeit abbrechen.

Cancel ist **best effort**, aber mit dem Ziel, für den Nutzer so schnell wie
möglich sichtbar zu stoppen.

Ein Cancel soll bewirken:

- weitere Modellausgabe nicht mehr an den Client weiterleiten
- keine neuen Tool-Schritte mehr starten
- laufende Provider-Anfragen oder Streams nach Möglichkeit abbrechen
- laufende Tool-Ausführung nach Möglichkeit abbrechen
- verspätet eintreffende Ergebnisse nach dem Abbruch ignorieren
- den Run in einen klaren Endzustand `cancelled` überführen

Falls gerade ein Tool läuft, das nicht hart unterbrechbar ist, wird der Abbruch
sofort als angefordert markiert und der Run beendet, sobald die Kontrolle wieder
an vBot zurückkehrt. Danach läuft der Run nicht normal weiter.

Bereits sichtbare Teilausgabe darf sichtbar bleiben; der wichtige Punkt ist,
dass nach dem Cancel nichts mehr regulär fortgesetzt wird.

## 10. Ereignismodell

Der Server braucht intern ein einfaches Ereignismodell: "Im System ist gerade
etwas passiert, und interessierte Clients sollen das erfahren."

Für Phase 3 reicht dafür konzeptionell mindestens:

- Run gestartet
- neue Ausgabe für einen Run vorhanden
- Thinking-/Reasoning-Block vorhanden
- Tool-Schritt gestartet
- Tool-Schritt beendet
- Run erfolgreich beendet
- Run abgebrochen
- Run fehlgeschlagen

Die genauen Event-Namen und Payloads sind **nicht** Teil dieser Datei. Wichtig
ist nur, dass WebSocket für solche allgemeinen Zustands- und Lebenszyklus-
Ereignisse vorgesehen ist.

## 11. Was bewusst später festgelegt wird

Diese Punkte sind **bewusst vertagte Detailentscheidungen**, keine offenen
Architekturprobleme für Phase 3:

- exakte RPC-Methodennamen
- exakte Request- und Response-Schemas
- exakte SSE-Eventformate
- exakte WebSocket-Payloads
- Authentifizierung oder Mehrbenutzer-Modell
- `fallback_model`-Verhalten
- provider-spezifisches `reasoning_meta`-Resend nach abgeschlossenen Turns

## 12. Konsequenzen für die Umsetzung

Auch wenn Phase 3 zunächst "einfach nur Chat zum Laufen bringen" soll, müssen
die teuren Strukturentscheidungen von Anfang an stimmen:

- Die Außen-API wird **run-basiert**, nicht provider-basiert.
- Session und Run werden als getrennte Konzepte behandelt.
- Streaming und Non-Streaming bauen auf derselben Kernlogik auf.
- Cancel ist kein UI-Trick, sondern Teil des echten Ausführungsmodells.
- Thinking-Blöcke, Tool-Schritte und Assistant-Antworten gehören zum sichtbaren
  Run-Zeitstrahl.
- Mehrere Sessions dürfen parallel laufen; nur innerhalb derselben Session gibt
  es die Ein-Run-Grenze.

Damit kann Phase 3 zunächst klein implementiert werden, ohne dass Phase 4, 5
oder 6 später die Grundarchitektur wieder aufreißen müssen.
