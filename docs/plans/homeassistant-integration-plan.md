# Home Assistant Integration Plan

Status legend: `[ ]` not started, `[~]` in progress, `[x]` completed.

## Context

Source: `HANDOFF-HA.md` — research based on Hermes Agent (NousResearch).
The research is sound, but describes a different codebase (Hermes Agent). This
plan adapts the 4 HA REST-API tools to vBot's actual architecture, conventions,
and dependency footprint.

Home Assistant exposes a built-in REST API at `{HASS_URL}/api/`. The 4 tools are
thin HTTP wrappers around these endpoints — no custom server-side addons needed.

## Terminology

| Term | Meaning |
|---|---|
| `HASS_TOKEN` | Long-Lived Access Token from HA profile. Required. From env / data-dir `.env`. |
| `HASS_URL` | Base URL of the HA instance. Default: `http://homeassistant.local:8123`. From env / data-dir `.env`. |
| Entity | A device, sensor, or automation in HA, identified by `entity_id` (e.g. `light.living_room`). |
| Service | An action on a device domain (e.g. `light.turn_on`, `climate.set_temperature`). |

## Goals

- 4 LLM-callable tools: `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`.
- Talk to Home Assistant's own REST API via `httpx.AsyncClient`.
- Credentials from env / data-dir `.env` via runtime's `resolve_environment_credential`.
- Tools only appear when `HASS_TOKEN` is configured.
- Security: domain blocklist on `ha_call_service`, regex validation on all inputs.
- Result envelopes through `tool_success()` / `tool_failure()`.
- No new dependencies — `httpx` is already a core dependency.

## Non-Goals

- WebSocket event streaming (Phase 2, later).
- Proactive automation / cron-driven HA reactions.
- Persistent notification output channel.
- HA setup wizard or auto-discovery.

---

## Target Architecture

```text
Runtime._start_services()
  -> credential_resolver = self.resolve_environment_credential
  -> register_homeassistant_tools(self._tools, credential_resolver)
      -> if credential_resolver("HASS_TOKEN") is empty: return (no tools registered)
      -> else: register 4 tools (ha_list_entities, ha_get_state, ha_list_services, ha_call_service)

Each tool handler:
  -> builds HASS_URL from captured config
  -> calls HA REST API via httpx.AsyncClient
  -> returns tool_success(data) or tool_failure(code, message)
```

---

## Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `core/tools/homeassistant.py` | **Create** | 4 tool handlers, schemas, registration function |
| `core/tools/__init__.py` | **Modify** | Export new symbols |
| `core/runtime/runtime.py` | **Modify** | One line: `register_homeassistant_tools(self._tools, self.resolve_environment_credential)` |
| `.vorch/specs/tools/homeassistant.md` | **Create** | Tool domain spec |
| `tests/core/tools/test_homeassistant.py` | **Create** | Tests with mocked httpx |

---

## The Four Tools

### 1. `ha_list_entities`

| | |
|---|---|
| **HA endpoint** | `GET {HASS_URL}/api/states` |
| **Parameters** | `domain` (optional) — e.g. `light`, `climate`, `sensor` |
| | `area` (optional) — matched against entity `friendly_name` |
| **Returns** | `{ count, entities: [{ entity_id, state, friendly_name }] }` |
| **Filtering** | Post-API-call: `domain` filters by `entity_id` prefix; `area` by `friendly_name` substring match (case-insensitive) |
| **Display** | None (result speaks for itself) |

### 2. `ha_get_state`

| | |
|---|---|
| **HA endpoint** | `GET {HASS_URL}/api/states/{entity_id}` |
| **Parameters** | `entity_id` (required) — validated: `^[a-z_][a-z0-9_]*\.[a-z0-9_]+$` |
| **Returns** | `{ entity_id, state, attributes, last_changed, last_updated }` |
| **Display** | `summary_fields=("entity_id",)` |

### 3. `ha_list_services`

| | |
|---|---|
| **HA endpoint** | `GET {HASS_URL}/api/services` |
| **Parameters** | `domain` (optional) — e.g. `light`, `climate` |
| **Returns** | `{ count, domains: [{ domain, services: { name: { description, fields } } }] }` |
| **Purpose** | LLM discovers available services/parameters at call time — no hardcoded service catalog |
| **Display** | None |

### 4. `ha_call_service`

| | |
|---|---|
| **HA endpoint** | `POST {HASS_URL}/api/services/{domain}/{service}` |
| **Parameters** | `domain` (required), `service` (required), `entity_id` (optional), `data` (optional, JSON object) |
| **Validation** | `domain`/`service`: `^[a-z][a-z0-9_]*$` |
| **Blocked domains** | `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, `rest_command` |
| **Returns** | HA response body (usually `[]` or state objects) |
| **Display** | `summary_fields=("domain", "service", "entity_id")` |

---

## Security

1. **Domain blocklist** on `ha_call_service`: anything that could execute code or
   enable SSRF (`shell_command`, `command_line`, `python_script`, `pyscript`,
   `hassio`, `rest_command`). Attempts to call blocked domains return
   `tool_failure("blocked_domain", ...)`.

2. **Input validation**: `entity_id`, `domain`, and `service` parameters are
   validated with strict regex before constructing HA URLs — prevents path
   traversal in `GET /api/services/{domain}/{service}`.

3. **Token**: Long-Lived Access Token from HA, never logged. Stored in `.env` or
   process environment.

---

## Credential Resolution

Both `HASS_TOKEN` and `HASS_URL` come from the same place: the runtime's
`resolve_environment_credential`, which checks the process environment first,
then falls back to the data-dir `.env`. The user puts them in `~/.vbot/.env`:

```bash
HASS_TOKEN=eyJhbGciOi...
HASS_URL=http://192.168.1.50:8123
```

`HASS_URL` defaults to `http://homeassistant.local:8123` (the mDNS name that
works on most home networks when HA runs on a Raspberry Pi or similar).

Both values are resolved once at registration time and captured in the handler
closures — they are not re-resolved on every tool call.

The tools are only registered when `HASS_TOKEN` is non-empty. Without a token,
the 4 HA tools have zero functionality, so they don't appear in the agent
allowlist at all. This is a deliberate departure from `web_search` (which always
registers and returns `missing_api_key` at call time) — HA tools are useless
without HA, so conditional registration keeps the tool list meaningful.

```python
def register_homeassistant_tools(
    registry: ToolRegistry,
    credential_resolver: Callable[[str], str],
) -> None:
    token = credential_resolver("HASS_TOKEN").strip()
    if not token:
        return  # no token → no tools

    hass_url = credential_resolver("HASS_URL").strip() or "http://homeassistant.local:8123"

    # ... register 4 tools, each handler closure captures hass_url and token
```

---

## HTTP Client

Uses `httpx.AsyncClient` (already a core dependency, used by `web_search`):

- `Authorization: Bearer {token}` header on every request.
- `Content-Type: application/json` for POST.
- Timeout: 15s connect, 30s total (similar to `web_search`).
- Retry: max 2 retries on HTTP 429, 502, 503, 504 with exponential backoff +
  jitter.
- No retry on 4xx (except 429) — auth failures, validation errors fail fast.

---

## Result Envelope Convention

Every handler returns stable vBot envelopes:

- Success: `tool_success({"count": N, "entities": [...]})`
- Validation error: `tool_failure("validation_error", ...)`
- Blocked domain: `tool_failure("blocked_domain", ...)`
- HA HTTP error: `tool_failure("home_assistant_error", ...)`
- HA not reachable: `tool_failure("home_assistant_unreachable", ...)`

---

## Registration Function Signature

```python
# core/tools/homeassistant.py

HA_LIST_ENTITIES_NAME = "ha_list_entities"
HA_LIST_ENTITIES_DESCRIPTION = "..."
HA_LIST_ENTITIES_PARAMETERS: JsonObject = {...}

# ... same pattern for all 4 tools ...

def register_homeassistant_tools(
    registry: ToolRegistry,
    credential_resolver: Callable[[str], str],
) -> None:
    """Register Home Assistant tools if HASS_TOKEN is configured."""

    token = credential_resolver("HASS_TOKEN").strip()
    if not token:
        return

    hass_url = credential_resolver("HASS_URL").strip()
    if not hass_url:
        hass_url = "http://homeassistant.local:8123"

    async def list_entities_handler(context, arguments):
        return await _handle_list_entities(context, arguments, hass_url, token)

    # ... 3 more handlers ...

    registry.register(
        HA_LIST_ENTITIES_NAME,
        HA_LIST_ENTITIES_DESCRIPTION,
        HA_LIST_ENTITIES_PARAMETERS,
        list_entities_handler,
    )
    # ... 3 more registrations ...


__all__ = [
    "HA_LIST_ENTITIES_NAME", "HA_LIST_ENTITIES_DESCRIPTION", "HA_LIST_ENTITIES_PARAMETERS",
    "HA_GET_STATE_NAME", "HA_GET_STATE_DESCRIPTION", "HA_GET_STATE_PARAMETERS",
    "HA_LIST_SERVICES_NAME", "HA_LIST_SERVICES_DESCRIPTION", "HA_LIST_SERVICES_PARAMETERS",
    "HA_CALL_SERVICE_NAME", "HA_CALL_SERVICE_DESCRIPTION", "HA_CALL_SERVICE_PARAMETERS",
    "register_homeassistant_tools",
]
```

---

## Runtime Wiring

One line in `core/runtime/runtime.py`, in `_start_services()`, alongside other
tool registrations:

```python
from core.tools.homeassistant import register_homeassistant_tools

# in _start_services():
register_homeassistant_tools(self._tools, self.resolve_environment_credential)
```

---

## Implementation Order

- [x] 1. Create `core/tools/homeassistant.py` — all 4 handlers, schemas, registration
- [x] 2. Wire into `core/tools/__init__.py` — export symbols
- [x] 3. Wire into `core/runtime/runtime.py` — one registration line
- [x] 4. Create `.vorch/specs/tools/homeassistant.md` — domain spec
- [x] 5. Create `tests/core/tools/test_homeassistant.py` — tests
- [x] 6. Run `python scripts/quality.py` — full quality gate
- [x] 7. Update `HANDOFF-HA.md` — note Phase 1 done, link to spec
