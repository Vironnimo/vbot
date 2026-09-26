# Home Assistant Extension

The first shipped **bundled extension** (`resources/extensions/homeassistant/`): four LLM-callable tools that wrap the Home Assistant REST API. Loaded out of the box, invisible everywhere until a token is set.

## Overview

Lives in the install tree at `resources/extensions/homeassistant/` (`extension.py` + `extension.json`), so it is scanned as the last extension root and is default-on (see `.vorch/domain-maps/extensions.md` -> Loading and identity invariants). It is a **normal extension** after loading - its `register(api)` declares a settings schema, declares the `home_assistant` Tool Family labeled `Home Assistant`, and registers the four Tools into that family through `api.register_tool`, which the runtime's `apply_tools` folds into the same `ToolRegistry` as the built-ins (`.vorch/domain-maps/tools.md`). There is no kernel built-in anymore; `core/tools/homeassistant.py` is gone.

The four tools are **always registered** regardless of configuration. A shared readiness predicate keeps them out of the prompt, the provider definitions, and every tool picker until `HASS_TOKEN` resolves to a non-empty string; the Extensions tab then shows the extension as loaded and `ready_state="waiting"` until it does.

The bundled `home-assistant` Skill under `resources/skills/home-assistant/` is the separate deep-configuration surface. It requires the same `HASS_TOKEN`, which activation grants to Bash, and teaches the bundled WebSocket script for Lovelace dashboard export, validation, race-safe backup/apply, creation with rollback, and allowlisted read-only registry inspection. It assumes the Extension connection already exists and never teaches or mutates setup. Keep ordinary entity/state/service work in the four Extension Tools; do not widen the script to bypass their validation or blocked-domain policy.

## Interfaces

Tool names: `ha_list_entities`, `ha_get_state`, `ha_list_services`, `ha_call_service`.

Internal files: `extension.py` owns definitions, requests, handlers and registration; `_arguments.py` is the pure `argument_normalizer` per Tool (harness dialects, identifier spelling, conflict refusals); `_listing.py` renders REST payloads as text lines and ranks candidate entities for failures. The extension has no `__init__.py`: the loader imports `extension.py` as `vbot_ext.homeassistant` with the folder as its package path, so relative imports work and tests patch `_sleep_for_retry` on that module.

Argument repair runs before contract validation and before any request. Identifiers are lowercased and spaces or hyphens become underscores (`Light.Living Room` -> `light.living_room`); grammar is then checked (`^[a-z][a-z0-9_]*$` for domain/service, `^[a-z_][a-z0-9_]*\.[a-z0-9_]+$` for entity ids) instead of schema patterns, which keeps request paths free of traversal. Repair never chooses a similar existing entity or service; contradictory fields refuse with every corrected call. Nested service `data` keeps its supplied keys and values except the supported target lifts. Empty targets cannot disappear into a broader request. Refusals start with `<tool> was not run:`.

Results are compact text: `count` or `service`/`changed` fields plus a `content` body of lines, rendered verbatim by the Provider text rendering. An entity line is `entity_id: state[ unit] (Friendly Name)`.

### `ha_list_entities`

- `GET /api/states`. Optional `domain` and `area`; empty values count as omitted. Aliases `room`, `name`, `query`, `search`, `text` -> `area`. An entity id given as `domain` refuses naming the `ha_get_state` call.
- `domain` filters by id prefix; `area` is text matched case-insensitively (separators `_ - .` and spaces equivalent) against `friendly_name`, `attributes.area` and the object id. It does not read the Home Assistant area registry.
- Returns `{count, note?, content}` sorted by id. More than 100 matches without filters return per-domain counts plus a narrowing note; with a filter the first 100 lines plus a note. No match returns `count: 0` and a note naming the closest domain or similar name words.

### `ha_get_state`

- `GET /api/states/{entity_id}`. Required `entity_id` (aliases `entity`, `entities`, `entity_ids`; a one-item list is accepted, several ids refuse). Display summary field: `entity_id`.
- Returns `{entity_id, state, attributes, last_changed, last_updated?}`; `last_updated` only when it differs from `last_changed`.
- A 404 is `entity_not_found`; a value that is not an entity id is `invalid_arguments`. Both name candidate entities (exact friendly-name match, close ids, name/id containment) and, for one candidate, the exact corrected call. Candidate lookup (`GET /api/states`) runs only for name-like values; values with `/` or other path characters fail without any request.

### `ha_list_services`

- `GET /api/services`. Without `domain`: one line per domain listing service names (blocked domains read `blocked in vBot`). With `domain`: one line per service with the description's first sentence and compact fields (`*` required, number `min-max unit`, select options when at most 8, else an example); Home Assistant's collapsible field sections are flattened; services with a response are marked. The unadvertised `service` field (or `light.turn_on` as `domain`) shows one service with field descriptions.
- Unknown domain or service is `service_not_found` naming close names and the call.

### `ha_call_service`

- `POST /api/services/{domain}/{service}`. Required `domain`, `service`; optional `entity_id` (one id, a list, or comma-separated ids; the body sends a list for several) and `data` object. Display summary fields: `domain`, `service`, `entity_id`.
- Dialects: `action` -> `service`, `service_data` -> `data`; `target` lifts `entity_id` to the field and `area_id`/`device_id`/`floor_id`/`label_id` into `data`; `data.entity_id` lifts to `entity_id` (matching duplicates accepted, different entities refuse); `light.turn_on` in either field supplies both unless the other field disagrees. A service without domain takes its entities' single domain; entities of several domains refuse, pointing at `homeassistant`.
- Returns `{service, changed, note?, response?, content?}`: the last reported state of each changed entity, with attributes named by `data` keys. Requested entities that did not change are read back (at most 10) and listed with `[unchanged]`; when every requested entity is missing the result is `entity_not_found` with candidates, otherwise a note names the missing ids.
- A 400/404 answer is diagnosed through `GET /api/services`: unknown domain/service -> `service_not_found` with close names and the corrected call; otherwise the message names `ha_list_services {"domain","service"}` for the fields.
- Service responses: a 400 whose message mentions `return_response` (Home Assistant's pre-run refusal of a data-only service) is re-sent once with `?return_response`, and the result carries `response` (cut at 16000 characters). The unadvertised `return_response: true` requests optional responses; a service without responses refuses it with `invalid_arguments`. This REST behavior follows Home Assistant's API source and is covered by respx tests, not verified against a live instance.
- Blocked domains: `shell_command`, `command_line`, `python_script`, `pyscript`, `hassio`, `rest_command`; refused before any request with `blocked_domain`.

## Settings Schema & live reads

Declared in `register(api)` via `api.register_settings` (see `.vorch/domain-maps/extensions/management.md` -> Settings schemas and live values):

- `url` - `text`, label "Server URL", default `http://homeassistant.local:8123`. Stored in extension config.
- `token` - `secret`, label "Access token", `env_key: HASS_TOKEN`. Stored in the data-dir `.env`, written via `extensions.set_secret`, never in `settings.json`.

Both are read **live on every call** - nothing is captured at register time:

- token = `api.resolve_credential("HASS_TOKEN").strip()`.
- url = `str(api.get_config().get("url") or "").strip()` or the default, with a trailing-slash strip (`rstrip("/")`) because the URL is user-typed in the form; endpoint paths are joined with a leading `/`.

So setting/changing the token or URL through Settings -> Extensions takes effect on the next call with **no restart** (design decisions 3-4).

## Readiness

All four tools share `ready=lambda: bool(api.resolve_credential("HASS_TOKEN").strip())` - cheap, I/O-free, re-evaluated on every prompt/tool-definition build (`.vorch/domain-maps/tools.md` -> Readiness). Not-ready -> hidden from prompt/provider definitions/`tool.list`; a direct dispatch returns the `tool_not_ready` envelope from the registry safety net. Token appears -> the tools appear on the next build; token removed -> they disappear again.

## External Dependencies

- Home Assistant REST API at `{url}/api/` via `httpx.AsyncClient` with `Authorization: Bearer {token}`.
- Home Assistant WebSocket API at `/api/websocket` via the bundled Skill's `websockets` script. The script accepts only allowlisted raw reads; dashboard mutation goes through purpose-built commands with local validation, expected-content hashing, backup-before-save, and post-save verification.
- Credential key `HASS_TOKEN` (Long-Lived Access Token). `HASS_URL` is **retired** - no env fallback of any kind (Generation 1: app code reads only the current format). Existing `HASS_TOKEN` `.env` entries keep working.
- Timeout: 15s connect, 30s total. Retry: max 2 with exponential backoff + jitter. GET reads retry transport errors and the shared idempotent statuses (`core/utils/http_status.py`). A POST service call repeats only when Home Assistant cannot have run it: connect/pool errors before sending, or 429/503. A 500/502/504 or a transport error after sending is never repeated; the message says the call may have run and names the read that checks it. `_ha_request` takes the extension's logger (`api.logger`) as a parameter; it is the only helper that logs.

## Error Envelopes

| Condition | Code |
|---|---|
| Unrepairable, conflicting or malformed arguments, including a value that is not an entity id | `invalid_arguments` |
| Entity missing (`ha_get_state` 404, or every requested service target missing) | `entity_not_found` |
| Unknown domain or service | `service_not_found` |
| Blocked domain | `blocked_domain` |
| Other HA HTTP error, unreachable server, non-JSON answer | `home_assistant_error` |
| Empty token at call time (handler guard) | `home_assistant_error` ("Home Assistant is not connected: ...") |

Every message names the next call or tells the Agent what to tell the user (token rejected -> Settings -> Extensions). Retry signalling (inside `error`): an exhausted retryable status or transport error sets `retryable=True` with `attempts_made`; other failures set `retryable=False`. The registry's dispatch-time safety net returns `tool_not_ready` when the predicate is false at call time.

## Constraints & Gotchas

- `entity_id`, `domain`, and `service` are grammar-checked after spelling repair and before URL construction - prevents path traversal; the `ha_call_service` domain blocklist stops code-execution / SSRF domains.
- Dispatch contract validation rejects unknown keys and wrong types before the handler; the handlers do not re-check unknown fields. The runtime does not assume the Provider enforced JSON Schema.
- Never pick an entity: name-like or missing references fail with candidates; one candidate is offered as a corrected call, not executed. Several candidates are listed for the Agent to choose.
- The token is **never logged** (`_ha_request` logs status/detail, never the bearer value).
- The handler guard (empty token -> `home_assistant_error`) is defense in depth behind the dispatch-time readiness check; it fires without attempting any request.
- Tests live in `tests/resources/extensions/test_homeassistant.py` (registration/live configuration), `test_homeassistant_entities.py`, `test_homeassistant_services.py` (dialects, conflicts, candidates, responses), and `test_homeassistant_retry.py` (repeat safety and failure messages), loaded through the real bundled root and dispatched through production contracts; `scripts/provider_probe/workflow_ha_tolerance.py` replays Provider-shaped calls against isolated HTTP. `resources/` is not a mirrored quality-runner package, so a scoped gate must name these suites or their test directory explicitly.
- Bundled Skill and script tests live at `tests/resources/skills/test_home_assistant_skill.py`; they verify the requirement metadata, WebSocket URL derivation, dashboard validation, dry-run behavior, race rejection, backup/verification, and create rollback without touching a live Home Assistant instance.
