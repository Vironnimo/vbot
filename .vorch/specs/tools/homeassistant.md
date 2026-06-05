# Home Assistant Tools

Four LLM-callable tools that wrap the Home Assistant REST API. Registered only when `HASS_TOKEN` is configured; without a token they do not appear in agent allowlists (unlike `web_search`, which always registers and returns `missing_api_key` at call time).

## Interfaces

- Tool names: `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`
- Registration: `register_homeassistant_tools(registry, credential_resolver)`

### `ha_list_entities`

- `GET /api/states`. Schema: optional `domain`, optional `area`; `additionalProperties: false`.
- `domain` filters by `entity_id` prefix; `area` filters by `friendly_name` substring (case-insensitive).
- Returns `{ count, entities: [{ entity_id, state, friendly_name }] }`.

### `ha_get_state`

- `GET /api/states/{entity_id}`. Schema: required `entity_id` (validated `^[a-z_][a-z0-9_]*\.[a-z0-9_]+$`); `additionalProperties: false`.
- Returns `{ entity_id, state, attributes, last_changed, last_updated }`. Display summary: `entity_id`.

### `ha_list_services`

- `GET /api/services`. Schema: optional `domain`; `additionalProperties: false`.
- Returns `{ count, domains: [{ domain, services: { name: { description, fields } } }] }`.

### `ha_call_service`

- `POST /api/services/{domain}/{service}`. Schema: required `domain`, required `service`, optional `entity_id`, optional `data` (object); `additionalProperties: false`.
- `domain`/`service` validated `^[a-z][a-z0-9_]*$`; `entity_id` validated with the entity regex when provided. Display summary: `domain`, `service`, `entity_id`.
- `data` must not include `entity_id`; callers use the top-level `entity_id` field so entity targeting always passes the strict validator.
- Blocked domains: `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, `rest_command`.

## External Dependencies

- Home Assistant REST API at `{HASS_URL}/api/` via `httpx.AsyncClient` with `Authorization: Bearer {token}`.
- Credential keys: `HASS_TOKEN` (required, Long-Lived Access Token), `HASS_URL` (optional, defaults to `http://homeassistant.local:8123`). Both resolved once at registration time, not per call.
- Timeout: 15s connect, 30s total. Retry: max 2 on HTTP 429/502/503/504 with exponential backoff + jitter; no retry on other 4xx.

## Error Envelopes

| Condition | Code |
|---|---|
| Invalid input (entity_id/domain/service) | `validation_error` |
| Blocked domain | `blocked_domain` |
| HA HTTP error or unreachable | `home_assistant_error` |

## Constraints & Gotchas

- `entity_id`, `domain`, and `service` are regex-validated before URL construction — prevents path traversal; the `ha_call_service` domain blocklist stops code-execution / SSRF domains.
- The token is never logged.
