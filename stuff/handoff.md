# File Attachments — Handoff

## Ziel

Nutzer können Dateien an Chat-Nachrichten anhängen — per WebUI und per Telegram. In V1 gehen Bilder direkt ans Modell wenn es die Capability hat; Audio/Video bleiben eine spätere Erweiterung desselben Pfads. Textdateien werden vollständig als Kontext eingebettet. Alles andere bekommt der Agent als Hinweis mit Pfad, damit er selbst entscheiden kann ob er es braucht. Agents können auch Dateien über Channels zurückschicken.

---

## Architektur

### Storage: `core/attachments/`

Reiner Blob-Store. Nimmt Bytes an, legt sie unter `<data_dir>/attachments/<uuid>` ab, gibt sie auf Anfrage zurück.

```
store(filename, data) → AttachmentRecord
get(attachment_id) → AttachmentRecord
delete(attachment_id) → None
```

`AttachmentRecord`: `id`, `filename`, `media_type`, `size_bytes`, `stored_at`, `file_path`, `text_content` (nur für `text/*`).

Beim Speichern:
- Dateigröße prüfen (20MB Default-Limit)
- MIME-Typ gegen Allowlist prüfen (Bilder, Text, PDF, gängige Office-Formate)
- Für `text/*`: Inhalt sofort dekodieren und in `text_content` ablegen

Kein Wissen über Modelle, Provider oder Content-Blöcke. Reiner Storage.

---

### Content-Block-Typen: `core/chat/content_blocks.py`

```python
ContentBlock = TextBlock | MediaBlock | FileBlock

@dataclass(frozen=True)
class TextBlock:
    type: Literal["text"]
    text: str

@dataclass(frozen=True)
class MediaBlock:
    type: Literal["media"]
    attachment_id: str
    filename: str
    media_type: str   # image/png, image/jpeg, audio/mp3, video/mp4, ...

@dataclass(frozen=True)
class FileBlock:
    type: Literal["file"]
    attachment_id: str
    filename: str
    media_type: str   # application/pdf, application/xlsx, ...
```

**Warum MediaBlock statt ImageBlock?** Bilder, Audio und Video teilen dasselbe Grundverhalten: Binärdaten ans Modell, wenn die Capability vorhanden ist. Neue Medientypen bedeuten nur eine neue erlaubte `media_type` — kein neuer Block-Typ, kein neuer Code-Pfad. `FileBlock` ist separat weil er sich fundamental anders verhält: geht nie als Binärdaten ans Modell.

**V1-Scope:** Der Block-Typ bleibt bewusst generisch, aber die erste Implementierung unterstützt nur `image/*`. Audio/Video kommen später über denselben Typ dazu, sobald Modell-Capabilities und Adapterpfade dafür existieren.

Serialisierung: Als Dicts mit `type`-Diskriminator in JSONL. Kein Base64 in der JSONL — nur die UUID.

---

### ChatMessage-Upgrade

```python
content: str | list[ContentBlock] | None
```

String bleibt gültig für alle Nachrichten ohne Anhang. Nur user-Messages können Listen sein. System-Messages bleiben immer Strings.

Textdateien werden in diesem Modell nicht als `FileBlock` persistiert, sondern als `TextBlock` materialisiert, damit ihr Inhalt direkt im Kontext liegt.

Dieses `content`-Feld ist auch die öffentliche Server-/WebUI-Form. `chat.history`, `chat.send` und `chat.stream` arbeiten mit genau diesem kanonischen Shape; es gibt kein separates `attachments`-Feld im öffentlichen Nachrichtenmodell.

---

### HTTP-Endpunkte für Attachments

Attachments laufen nicht über RPC, sondern über dedizierte HTTP-Endpunkte:

- `POST /api/upload` für Multipart-Uploads aus der WebUI
- `GET /api/attachments/{attachment_id}` für Inline-Anzeige und Download desselben gespeicherten Blobs

Der Server bleibt dabei die Quelle der Wahrheit für MIME-Typ und Metadaten.

---

### Auflösung: Chat-Layer, nicht Adapter

Zwischen "Nachricht in JSONL" und "Nachricht geht an Provider" löst der Chat-Layer auf — kurz vor dem API-Aufruf.

Der Chat-Layer geht beim Zusammenbauen der Request-Messages durch die Liste:

1. **MediaBlock im aktuellen Turn** → in V1 nur für `image/*`: Datei lesen, base64-kodieren, durch aufgelöstes Dict ersetzen
2. **MediaBlock in älteren Turns** → Text-Platzhalter: `[Bild: foto.png]` — kein Re-Send historischer Medien, sonst explodieren Token-Kosten
3. **FileBlock** → `[Datei: report.pdf (application/pdf) — Pfad: /path/to/file]`
4. **TextBlock** → unverändert

Was der Adapter bekommt: fertige Dicts, keine UUIDs. Der Adapter übersetzt nur ins Provider-Wire-Format. Keine Storage-Abhängigkeit in Adaptern.

---

### Provider-Übersetzung (im Adapter)

Adapter sehen aufgelöste Dicts:

| Block | OpenAI | Anthropic |
|---|---|---|
| `{"type": "media", "base64": "...", "media_type": "image/png"}` | `image_url` mit data-URL | `image` mit base64-source |
| `{"type": "text", "text": "..."}` | normaler text-Part | normaler text-Part |
| FileBlock-Notiz | kommt als TextBlock an | kommt als TextBlock an |

Für Audio/Video: wenn ein Modell das unterstützt, erweitert der jeweilige Adapter seine Übersetzungslogik. Der Rest des Systems ändert sich nicht. In V1 bleibt dieser Pfad noch ungenutzt.

---

### WebUI

**Upload-Wege — alle drei landen im selben Flow:**
1. File-Picker (Button im Composer)
2. Paste (`paste`-Event auf dem Composer-Input, `clipboardData.items` nach Bild-Typen durchsuchen, Dateiname auto-generieren: `screenshot-YYYY-MM-DD-HH-MM-SS.png`)
3. Drag & Drop (`dragover` + `drop` auf der Composer-Area)

**Upload-Flow:**
- Datei landet → sofort `POST /api/upload` (Multipart), parallel zur Nutzer-Eingabe
- Server gibt `{attachment_id, filename, media_type, size}` zurück
- Gleichzeitig: lokale `preview_url` via `URL.createObjectURL()` aus dem Blob erstellen — Vorschau erscheint sofort, ohne auf den Server zu warten

**Composer — Pending Attachments:**
- Composer hält eine lokale Liste der noch nicht abgeschickten Anhänge
- Jeder Eintrag: `{attachment_id, filename, media_type, preview_url, uploading: bool}`
- Darstellung: Attachment-Tray unter dem Textfeld
  - Bilder: Thumbnail (56×56px), beim Hover erscheint eine größere floating Vorschau (~300px breit) — nützlich wenn man nicht mehr weiß was auf dem Screenshot war
  - Dateien: Icon + Dateiname, kein Hover-Preview
  - X-Button zum Entfernen (entfernt aus der Liste; GC kümmert sich um den Server-seitigen Blob)
- Nachricht abschicken: `content` bleibt das einzige Nachrichtenfeld. Ohne Anhänge ein String, mit Anhängen eine `list[ContentBlock]`.

**Timeline:**
- Bilder: inline `<img src="/api/attachments/{id}">`, anklickbar für Vollbild
- Dateien: Icon + Dateiname, Link auf `GET /api/attachments/{id}`
- i18n für alle neuen Strings

---

### Telegram Inbound

1. Bot empfängt Photo/Document
2. Herunterladen via `bot.get_file()`
3. Speichern via `AttachmentStore.store()` → UUID
4. Content-Blöcke bauen: `MediaBlock` für Bilder/Medien, `FileBlock` für Dokumente, `TextBlock` für Textdateien (vollständig eingebettet)
5. Normaler Run via TriggerService

### Telegram Outbound / `channel_send`

```
channel_send(channel_id, message?: str, file_paths?: list[str])
```

`file_paths` ist eine Liste von Pfaden, keine UUIDs. Der Agent kennt Pfade, keine UUIDs. Tool liest jede Datei, ermittelt MIME-Typ, übergibt an ChannelService → Telegram-Adapter.

Mindestens eines von `message` oder `file_paths` muss gesetzt sein. Wenn beides vorhanden ist, ist `message` die Caption/Begleitnachricht zu den Dateien.

Mehrere Dateien: Adapter entscheidet ob Media-Group (Telegram: max 10) oder sequentielle Einzelnachrichten. Diese Entscheidung trifft der Adapter, nicht das Tool.

`ChannelAdapter.send()` bekommt `send(message, platform_target, files?: list[FileData])`. Kein Telegram-spezifischer Code in der Tool-Logik.

---

## Entschieden

- `core/attachments/` als eigenes Modul, reiner Storage
- `TextBlock | MediaBlock | FileBlock` — MediaBlock für alle Binär-Medien, erweiterbar
- V1 unterstützt über `MediaBlock` nur Bilder; Audio/Video bleiben spätere Erweiterungen desselben Typs
- Text-Extraktion beim Speichern, nicht lazy
- Textdateien werden vollständig eingebettet — kein Truncation-, Chunking- oder Fallback-Pfad
- Auflösung im Chat-Layer, nicht im Adapter — Adapter sehen keine UUIDs
- Historische Medien werden beim Zusammenbauen durch Text-Platzhalter ersetzt
- Nur der aktuelle User-Turn sendet Bilder als Base64; ältere Turns bleiben Platzhalter
- Öffentliche Nachrichten verwenden dasselbe kanonische `content`-Feld wie der Chat-Kern — kein separates `attachments`-Feld
- Attachments nutzen dedizierte HTTP-Endpunkte: `POST /api/upload` und `GET /api/attachments/{attachment_id}`
- `channel_send` mit `file_paths: list[str]`, nicht Attachment-IDs
- `channel_send` erlaubt Text, Dateien oder beides; mindestens eins ist Pflicht, `message` dient bei beidem als Caption
- Kein neues Tool für Datei-Zugriff bei nicht eingebetteten Dateien — FileBlock-Pfad in der Notiz, Agent nutzt `read`
- Manuelle DI über `Runtime.__init__`
- Max-Filesize konfigurierbar via `settings.json`, Default 20MB
- Drei Upload-Wege im Composer: File-Picker, Paste, Drag & Drop — alle selber Flow
- Lokale `object URL` für sofortige Vorschau vor Server-Antwort
- Hover-Tooltip für größere Bildvorschau im Composer (kein Modal)
- Sidecar-JSON (`<uuid>.json`) neben jedem Blob für Metadaten — kein Index, kein DB
- Server macht eigenes MIME-Sniffing (Magic Bytes), vertraut nicht dem Browser
- Vision-Capability-Check via Modell-Katalog (`capabilities.vision`) im Chat-Layer — kein Vision → Laufzeitfehler mit aussagekräftiger Meldung, kein stilles Fallback
- `read`-Tool hat keine Pfad-Restrictions — FileBlock-Pfad unter `<data_dir>/attachments/` ist zugänglich
- Telegram Album-Grouping: 500ms Buffer pro `media_group_id`, dann ein einziger Run mit allen gesammelten Blöcken

## Offen

- Attachment-Cleanup: Out of Scope für jetzt — alle Dateien bleiben im Attachments-Ordner. Späterer Plan: Orphans beim Server-Start nach 24-48h löschen; beim Session/Agent-Löschen referenzierte Dateien mitlöschen.
