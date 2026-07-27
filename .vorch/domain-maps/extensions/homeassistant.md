# Home Assistant Extension

The first shipped **bundled extension** (`resources/extensions/homeassistant/`): four LLM-callable tools that wrap the Home Assistant REST API. Loaded out of the box, invisible everywhere until a token is set.

## Overview

Lives in the install tree at `resources/extensions/homeassistant/` (`extension.py` + `extension.json`), so it is scanned as the last extension root and is default-on (see `.vorch/domain-maps/extensions.md` → Loading and identity invariants). It is a **normal extension** after loading — its `register(api)` declares a settings schema and registers the four tools through `api.register_tool`, which the runtime's `apply_tools` folds into the same `ToolRegistry` as the built-ins (`.vorch/domain-maps/tools.md`). There is no kernel built-in anymore; `core/tools/homeassistant.py` is gone.

The four tools are **always registered** regardless of configuration. A shared readiness predicate keeps them out of the prompt, the provider definitions, and every tool picker until `HASS_TOKEN` resolves to a non-empty string; the Extensions tab then shows the extension as loaded and `ready_state="waiting"` until it does.

## Interfaces

Tool names: `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`.

### `ha_list_entities`

- `GET /api/states`. Schema: optional non-empty string `domain`, optional non-empty string `area`; `additionalProperties: false`.
- `domain` filters by `entity_id` prefix; `area` filters by `friendly_name` substring (case-insensitive).
- Returns `{ count, entities: [{ entity_id, state, friendly_name }] }`.

### `ha_get_state`

- `GET /api/states/{entity_id}`. Schema: required `entity_id` (validated `^[a-z_][a-z0-9_]*\.[a-z0-9_]+$`); `additionalProperties: false`.
- Returns `{ entity_id, state, attributes, last_changed, last_updated }`. Display summary field: `entity_id`.

### `ha_list_services`

- `GET /api/services`. Schema: optional non-empty string `domain`; `additionalProperties: false`.
- Returns `{ count, domains: [{ domain, services: { name: { description, fields } } }] }`.

### `ha_call_service`

- `POST /api/services/{domain}/{service}`. Schema: required non-empty string `domain`, required non-empty string `service`, optional non-empty string `entity_id`, optional `data` (object); `additionalProperties: false`.
- `domain`/`service` validated `^[a-z][a-z0-9_]*$`; `entity_id` validated with the entity regex when provided. Display summary fields: `domain`, `service`, `entity_id`.
- `data` must not include `entity_id`; callers use the top-level `entity_id` field so entity targeting always passes the strict validator.
- Blocked domains: `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, `rest_command`.

## Settings Schema & live reads

Declared in `register(api)` via `api.register_settings` (see `.vorch/domain-maps/extensions/management.md` → Settings schemas and live values):

- `url` — `text`, label "Server URL", default `http://homeassistant.local:8123`. Stored in extension config.
- `token` — `secret`, label "Access token", `env_key: HASS_TOKEN`. Stored in the data-dir `.env`, written via `extensions.set_secret`, never in `settings.json`.

Both are read **live on every call** — nothing is captured at register time:

- token = `api.resolve_credential("HASS_TOKEN").strip()`.
- url = `str(api.get_config().get("url") or "").strip()` or the default, with a trailing-slash strip (`rstrip("/")`) because the URL is user-typed in the form; endpoint paths are joined with a leading `/`.

So setting/changing the token or URL through Settings → Extensions takes effect on the next call with **no restart** (design decisions 3–4).

## Readiness

All four tools share `ready=lambda: bool(api.resolve_credential("HASS_TOKEN").strip())` — cheap, I/O-free, re-evaluated on every prompt/tool-definition build (`.vorch/domain-maps/tools.md` → Readiness). Not-ready → hidden from prompt/provider definitions/`tool.list`; a direct dispatch returns the `tool_not_ready` envelope from the registry safety net. Token appears → the tools appear on the next build; token removed → they disappear again.

## External Dependencies

- Home Assistant REST API at `{url}/api/` via `httpx.AsyncClient` with `Authorization: Bearer {token}`.
- Credential key `HASS_TOKEN` (Long-Lived Access Token). `HASS_URL` is **retired** — no env fallback of any kind (project rule: no legacy compatibility). Existing `HASS_TOKEN` `.env` entries keep working.
- Timeout: 15s connect, 30s total. Retry: max 2 with exponential backoff + jitter via the shared policy in `core/utils/http_status.py` (`is_retryable_status`, idempotency-aware — `idempotent=method=="GET"`); a POST service call is not idempotent, so a 500 there is fatal. `_ha_request` takes the extension's logger (`api.logger`) as a parameter; it is the only helper that logs.

## Error Envelopes

| Condition | Code |
|---|---|
| Invalid input (unknown arguments, wrong types, entity_id/domain/service, non-object `data`, or `data.entity_id`) | `validation_error` |
| Blocked domain | `blocked_domain` |
| HA HTTP error or unreachable | `home_assistant_error` |
| Empty token at call time (handler guard) | `home_assistant_error` ("HASS_TOKEN is not configured") |

Retry signalling (inside `error`): an exhausted retryable status / transport error sets `retryable=True` with `attempts_made`; validation/fatal failures set `retryable=False`. The registry's dispatch-time safety net returns `tool_not_ready` (not one of the above) when the predicate is false at call time.

## Constraints & Gotchas

- `entity_id`, `domain`, and `service` are regex-validated before URL construction — prevents path traversal; the `ha_call_service` domain blocklist stops code-execution / SSRF domains.
- Every handler independently rejects unknown keys and wrong optional-field types before issuing an HTTP request; the runtime does not assume the Provider enforced JSON Schema.
- The token is **never logged** (`_ha_request` logs status/detail, never the bearer value).
- The handler guard (empty token → `home_assistant_error`) is defense in depth behind the dispatch-time readiness check; it fires without attempting any request.
- Tests live at `tests/resources/extensions/test_homeassistant.py` (loaded through the real bundled root); `resources/` is not a mirrored quality-runner package, so a scoped gate run must name the test file explicitly.
