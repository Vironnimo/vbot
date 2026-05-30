# Handoff: Home Assistant Integration – Vorbild Hermes Agent

> **Status:** Phase 1 ✅ implementiert (2026-05-30). Spec: `.vorch/specs/tools/homeassistant.md`
> **Quelle:** https://github.com/NousResearch/hermes-agent
> **Datum:** 2026-05-30
> **Scope:** Phase 1 – 4 HA-Tools via REST API. WebSocket-Event-Streaming später.

---

## 1. Entscheidung: Tools first, Streaming später

Hermes Agent hat zwei Komponenten. Wir bauen **nur Nr. 1**:

| Komponente | Was | Bauen? |
|------------|-----|--------|
| **① Vier LLM-Tools** | HA per REST API steuern/abfragen | ✅ **Phase 1 – jetzt** |
| **② Gateway-WebSocket** | Echtzeit-Event-Streaming, proaktive Reaktionen | ⏳ Phase 2 – später |

**Begründung:** Die 4 Tools decken 90% der Use-Cases ab (Licht an, Temperatur abfragen,
Geräte steuern). WebSocket-Streaming ist ein Premium-Feature für echte Automation
(Agent als aktiver Hausmeister statt Befehlsempfänger).

---

## 2. Grundlage: Home Assistants eigene REST API

**Wichtig:** Die 4 Tools, die wir bauen, rufen nichts selbst Gemachtes auf – sie reden mit
**Home Assistants integrierter REST API**. Die ist immer da, kein Addon, kein Plugin, nichts
extra zu installieren.

### Wo die API lebt

Home Assistant macht automatisch eine HTTP-API auf unter:

```
http://<deine-ha-instanz>:8123/api/
```

Konkret:

| Läuft HA auf … | Dann ist die API erreichbar unter … |
|---|---|
| Raspberry Pi (IP 192.168.1.50) | `http://192.168.1.50:8123/api/` |
| Deinem Rechner (localhost) | `http://localhost:8123/api/` |
| Per mDNS im selben Netz | `http://homeassistant.local:8123/api/` |

### Was du brauchst

1. **Die URL** deiner HA-Instanz (z.B. `http://192.168.1.50:8123`)
2. **Einen Long-Lived Access Token** – erstellst du in der HA-WebUI:
   *Profil → Long-Lived Access Tokens → Create Token*

### Testen mit curl

```bash
curl -H "Authorization: Bearer DEIN_TOKEN" http://192.168.1.50:8123/api/
# → {"message": "API running."}
```

Wenn `API running` zurückkommt – alles bereit.

### Das bauste darauf

**Unsere 4 Tools rufen genau diese REST-Endpunkte von HA auf:**

```
GET    {HASS_URL}/api/states                 → ha_list_entities
GET    {HASS_URL}/api/states/{entity_id}     → ha_get_state
GET    {HASS_URL}/api/services               → ha_list_services
POST   {HASS_URL}/api/services/{dom}/{svc}   → ha_call_service
```

Wir bauen nur den **HTTP-Client drumrum** (Token im Header, Response parsen,
Fehler abfangen) – die API selbst gehört Home Assistant.

---

## 3. Die vier HA-Tools im Detail

Datei: `tools/homeassistant.py` – registriert 4 LLM-callable Tools, aktiv via `HASS_TOKEN`.

### 3.1 `ha_list_entities`

Entities auflisten, gefiltert nach Domain oder Raum.

| | |
|---|---|
| **HA-REST-Endpunkt** | `GET {HASS_URL}/api/states` |
| **Macht im Klartext** | `GET http://192.168.1.50:8123/api/states` (mit Token im Header) |
| **Parameter** | `domain` (optional) – z.B. `light`, `switch`, `climate`, `sensor`, `binary_sensor`, `cover`, `fan`, `media_player` |
| | `area` (optional) – z.B. `living room`, `kitchen` (matched gegen `friendly_name`) |
| **Return** | `{count, entities: [{entity_id, state, friendly_name}]}` |
| **Filter-Logik** | Nach dem API-Call: `domain` prüft Prefix (`light.`), `area` matched gegen `friendly_name` |

### 3.2 `ha_get_state`

Detaillierten Status einer einzelnen Entity abfragen.

| | |
|---|---|
| **HA-REST-Endpunkt** | `GET {HASS_URL}/api/states/{entity_id}` |
| **Macht im Klartext** | `GET http://192.168.1.50:8123/api/states/light.wohnzimmer` |
| **Parameter** | `entity_id` (required) – z.B. `light.living_room`, `sensor.temperature` |
| **Return** | `{entity_id, state, attributes, last_changed, last_updated}` |
| **Validation** | `^[a-z_][a-z0-9_]*\.[a-z0-9_]+$` |

### 3.3 `ha_list_services`

Verfügbare Services/Aktionen pro Domain auflisten (Discovery fürs LLM).

| | |
|---|---|
| **HA-REST-Endpunkt** | `GET {HASS_URL}/api/services` |
| **Macht im Klartext** | `GET http://192.168.1.50:8123/api/services` |
| **Parameter** | `domain` (optional) – z.B. `light`, `climate` |
| **Return** | `{count, domains: [{domain, services: {name: {description, fields}}}]}` |
| **Zweck** | Der LLM findet heraus, welche Services/Felder ein Domain unterstützt – muss man nicht hartcodieren |

### 3.4 `ha_call_service`

Service aufrufen – damit steuerst du Geräte.

| | |
|---|---|
| **HA-REST-Endpunkt** | `POST {HASS_URL}/api/services/{domain}/{service}` |
| **Macht im Klartext** | `POST http://192.168.1.50:8123/api/services/light/turn_on` + Body |
| **Parameter** | `domain` (required), `service` (required), `entity_id` (optional), `data` (optional, JSON-String) |
| **Sicherheit** | **Blockierte Domains** (kein Zugriff): `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, `rest_command` |
| **Validation** | Domain/Service: `^[a-z][a-z0-9_]*$` (verhindert Path-Traversal) |

**Beispiele für den HTTP-Call, den das Tool macht:**

```
LLM ruft auf:  ha_call_service(domain="light", service="turn_on",
                                entity_id="light.living_room")
→ HTTP-Call:   POST http://192.168.1.50:8123/api/services/light/turn_on
  Header:      Authorization: Bearer <token>
  Body:        {"entity_id": "light.living_room"}


LLM ruft auf:  ha_call_service(domain="climate", service="set_temperature",
                                entity_id="climate.thermostat",
                                data='{"temperature": 22, "hvac_mode": "heat"}')
→ HTTP-Call:   POST http://192.168.1.50:8123/api/services/climate/set_temperature
  Header:      Authorization: Bearer <token>
  Body:        {"entity_id": "climate.thermostat",
                "temperature": 22, "hvac_mode": "heat"}
```

---

## 4. Schnittstellen & Architektur

### 4.1 Konfiguration (Environment)

```bash
# Required – dein Long-Lived Access Token aus dem HA-Profil
HASS_TOKEN=dein_long_lived_access_token

# Optional – URL zu deiner HA-Instanz (default: http://homeassistant.local:8123)
HASS_URL=http://192.168.1.100:8123
```

**Nur `HASS_TOKEN` muss gesetzt sein** – dann aktivieren sich alle 4 Tools automatisch.
Die Tools bauen daraus die vollen REST-URLs: `{HASS_URL}/api/states`, etc.

### 4.2 Tool-Registrierungs-Pattern

Jedes Tool bekommt ein JSON-Schema (fürs LLM), einen Handler und eine Check-Fn:

```python
from tools.registry import registry, tool_error

registry.register(
    name="ha_call_service",
    toolset="homeassistant",
    schema={ /* JSON Schema */ },
    handler=_handle_call_service,     # sync → wrappt async
    check_fn=_check_ha_available,     # prüft HASS_TOKEN
    emoji="🏠",
)
```

### 4.3 Sync/Async-Brücke (wichtig!)

Tool-Handler in der Registry sind synchron. HA-Calls laufen asynchron via `aiohttp`.
Lösung von Hermes:

```python
def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)
    else:
        return asyncio.run(coro)
```

### 4.4 Security (wichtig!)

1. **Domain-Blocklist:** `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, `rest_command` – alles was Code ausführen oder SSRF ermöglichen könnte
2. **Input-Validation:** `entity_id` per Regex, `domain`/`service` per Regex – verhindert Path-Traversal in `POST /api/services/{domain}/{service}`
3. **Token-Scope:** ein Long-Lived Access Token, keine UI-Session-Tokens

---

## 5. Toolset-Registrierung

Die Tools müssen im Default-Toolset des Agenten landen:

```python
# Entweder in _CORE_TOOLS (immer verfügbar):
_CORE_TOOLS = [
    ...
    "ha_list_entities", "ha_get_state",
    "ha_list_services", "ha_call_service",
]

# Oder als eigenständiges Toolset (opt-in):
TOOLSETS = {
    "homeassistant": {
        "description": "Home Assistant smart home control and monitoring",
        "tools": ["ha_list_entities", "ha_get_state",
                   "ha_list_services", "ha_call_service"],
        "includes": []
    },
}
```

---

## 6. Bauplan (Phase 1 – jetzt)

### Was reinkommt

```
src/
  tools/
    homeassistant.py          # 4 Tools + aiohttp-Client + Registry-Registrierung
```

**Dependencies:** `aiohttp` (oder alternativ `httpx`)

### Was rausbleibt (Phase 2 – später)

- WebSocket-Adapter (`gateway/platforms/homeassistant.py`)
- Gateway-Event-Streaming mit `state_changed`-Subscription
- Persistent-Notifications als Agent-Output-Kanal
- Platform-Enum, Factory, Auth-Maps, Setup-Wizard
- Cron-Delivery, Send-Message-Routing für HA

---

## 7. Referenzen

- **HA REST API:** https://developers.home-assistant.io/docs/api/rest/
- **HA WebSocket API (später):** https://developers.home-assistant.io/docs/api/websocket/
- **Hermes HA-Tools Quellcode:** `tools/homeassistant_tool.py` – https://github.com/NousResearch/hermes-agent/blob/main/tools/homeassistant_tool.py
- **Hermes HA-Adapter (später):** `gateway/platforms/homeassistant.py`

---

## Anhang: WebSocket-Streaming (nur als Referenz)

> Das Folgende ist dokumentiert für Phase 2, aber **nicht Teil des aktuellen Baus**.

### Wofür der WebSocket da ist

Der Agent bekommt **proaktiv Ereignisse aus HA mitgeteilt**, ohne gefragt werden zu müssen:

- **Tür geht auf:** `[HA] Front Door: triggered (was cleared)` → Agent schaltet Licht an
- **Temperatur fällt:** `[HA] Wohnzimmer: changed from 21°C to 19°C` → Agent regelt Heizung
- **Rauchmelder:** `[HA] Smoke Detector: triggered` → Agent alarmiert dich

### Technisch

- WebSocket zu `ws://<hass_url>/api/websocket`
- Subscribed auf `state_changed`-Events
- Konfigurierbare Filter: `watch_domains`, `watch_entities`, `ignore_entities`, `cooldown_seconds`
- Domain-spezifische Formatierung der Events (climate, sensor, binary_sensor, etc.)
- Automatische Reconnection mit Backoff (5s → 10s → 30s → 60s)
- Outbound via REST: `POST /api/services/persistent_notification/create`
