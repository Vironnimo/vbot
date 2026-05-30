# Home Assistant Tools

Four LLM-callable tools that wrap the Home Assistant REST API. Tools are only
registered when `HASS_TOKEN` is configured; without a token they do not appear
in agent allowlists.

## Interfaces

- Tool names: `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`
- Registration: `register_homeassistant_tools(registry, credential_resolver)`
- Conditional: tools are only registered when `HASS_TOKEN` is non-empty. No
  token → no tools (unlike `web_search`, which always registers and returns
  `missing_api_key` at call time).

### `ha_list_entities`

- HA endpoint: `GET /api/states`
- Schema: optional `domain` (string), optional `area` (string); `additionalProperties: false`.
- Domain filters by `entity_id` prefix; area filters by `friendly_name` substring (case-insensitive).
- Returns: `{ count, entities: [{ entity_id, state, friendly_name }] }`.

### `ha_get_state`

- HA endpoint: `GET /api/states/{entity_id}`
- Schema: required `entity_id` (string, validated with `^[a-z_][a-z0-9_]*\.[a-z0-9_]+$`); `additionalProperties: false`.
- Returns: `{ entity_id, state, attributes, last_changed, last_updated }`.
- Display: `summary_fields=("entity_id",)`.

### `ha_list_services`

- HA endpoint: `GET /api/services`
- Schema: optional `domain` (string); `additionalProperties: false`.
- Returns: `{ count, domains: [{ domain, services: { name: { description, fields } } }] }`.

### `ha_call_service`

- HA endpoint: `POST /api/services/{domain}/{service}`
- Schema: required `domain` (string), required `service` (string), optional `entity_id` (string), optional `data` (object); `additionalProperties: false`.
- `domain` and `service` validated with `^[a-z][a-z0-9_]*$`.
- Blocked domains: `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, `rest_command`.
- Display: `summary_fields=("domain", "service", "entity_id")`.

## External Dependencies

- Home Assistant REST API at `{HASS_URL}/api/`.
- Credential keys: `HASS_TOKEN` (required, Long-Lived Access Token), `HASS_URL` (optional, defaults to `http://homeassistant.local:8123`).

## HTTP Client

- `httpx.AsyncClient` with `Authorization: Bearer {token}`.
- Timeout: 15s connect, 30s total.
- Retry: max 2 retries on HTTP 429, 502, 503, 504 with exponential backoff + jitter.
- No retry on other 4xx (auth failures, validation errors fail fast).

## Error Envelopes

| Condition | Code | Message |
|---|---|---|
| Validation error | `validation_error` | Describes invalid input. |
| Blocked domain | `blocked_domain` | Domain '{domain}' is blocked for security reasons. |
| HA HTTP error | `home_assistant_error` | HTTP status + detail. |
| HA not reachable | `home_assistant_error` | Request failed / connection error. |

## Security

- `entity_id`, `domain`, and `service` validated with strict regex before URL construction — prevents path traversal.
- Domain blocklist on `ha_call_service` blocks code-execution / SSRF domains.
- Token never logged.
- Both `HASS_TOKEN` and `HASS_URL` resolved once at registration time; not re-resolved per call.
