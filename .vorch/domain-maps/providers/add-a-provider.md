# Adding a Provider

Read this reference when adding a new Provider, Connection variant, Adapter selector, discovery normalizer, or Provider-specific reference. Do not treat a Provider JSON entry alone as complete integration.

## Decide the ownership shape first

Start from the real wire, not the Provider name:

- Fully compatible Chat Completions with no runtime/catalog policy difference can use `OpenAICompatibleAdapter` directly.
- A mostly compatible Provider with meaningful discovery, reasoning, streaming, response, or request-policy differences subclasses the compatible base.
- A Messages-compatible branch composes `AnthropicCompatibleAdapter`; never concrete `AnthropicAdapter`.
- A Provider serving multiple Connection modes or per-Model protocols gets one Provider-owned outer Adapter/router. Runtime still selects only that outer Adapter.

Name the existing deep owner that can absorb the behavior before adding another Adapter/helper. Provider-specific maps document only verified differences from shared policy.

## Required integration steps

1. Inspect the real auth, runtime, streaming, and Model-list endpoints. Capture enough representative responses to distinguish durable facts from guesses.
2. Add `resources/providers/<id>.json` with a valid id, Adapter selector, base URL, at least one `connections` entry, and only verified defaults/headers/discovery settings. Choose `api_key`, `oauth`, or `none` based on actual auth semantics.
3. Reuse or implement the Adapter in `core/providers/`. Keep wire mechanics deep, normalized output canonical, and secrets behind the injected `TokenGetter`. Declare the protocols it speaks in `WIRE_PROTOCOLS` and build every request from its resolved wire profile (`self.wire_profile(model_id)`) through the shared codecs; per-Model request differences never become Adapter branches.
4. Add `resources/wire/<id>.json` for every request rule that differs from the protocol defaults (`core/providers/_wire_protocol_defaults.py`): protocol per Model or Connection, reasoning dialect, ladder and off/unset spelling, parameter rules and `exclusive_parameters` groups, output-limit field, Tool-call ids, replay scope, fidelity and carriers, media types, prompt caching, admission (`providers.md` -> wire profile layers, `providers/request-policy.md`). Prefer `defaults` and `rules` matching catalog facts over per-Model entries, and state the evidence in each rule's `note`. Facts nobody verified are left to the defaults and to rejection learning.
5. If the Adapter selector is new, register it in both the Runtime map `ADAPTER_TYPES` (`core/providers/adapter_types.py`) and discovery `_DISCOVERY_ADAPTER_MAP` (`core/models/discovery.py`); export public contract types only when another domain needs them.
6. Implement catalog hooks only for facts the Provider exposes: filters/normalization, headers/params, optional enrichment, or task feeds. Discovery must tag/merge by Connection and remain fail-soft at the documented boundaries.
7. Add or refresh the Provider Model projection, then use override files only for durable facts the feeds omit. Verify Model ids exactly match what the runtime endpoint expects.
8. Add a task-gated `providers/<id>.md` reference when the Provider has non-obvious auth, routing, reasoning, catalog, retry, media, or usage contracts; add a sharp trigger in `providers.md`, never a PROJECT index row.
9. Add focused tests for config parsing, credentials/Accounts, request and response normalization, streaming, errors/retry, reasoning, discovery, media, and any Provider-specific branch.

## Real-behavior verification

Before calling the Provider supported, probe at least one real inference and the actual catalog endpoint when credentials/access exist. Verify the behaviors the integration claims: auth headers, Model id, nonstreaming and streaming shapes, Tool calls/results, reasoning controls and CoT round-trip, output limits, media, structured output, transient/fatal errors, and token usage.

Use the live verification tool for this (`providers/compatibility-verification.md`): `python scripts/verify_wire_profile.py --provider <id> --model <model> [--connection <id>] --data-dir ~/.vbot-dev` drives send, stream, every effort rung, sampling, Tool replay, reasoning replay and image input through the Adapter and prints the Model entry the evidence supports; `--write` adds it, with a `verified` record for a clean run, to `resources/wire/<id>.json`. Run it for at least one Model per protocol and reasoning dialect. `python scripts/wire_snapshot.py capture --out DIR` before and after a change, then `compare`, shows exactly what the change alters on the wire for every bundled Model.

Do not invent a capability because a compatible API documents it generally. Sparse discovery facts remain unknown or conservative. If endpoint access is unavailable, label the unverified contract in the Provider-specific reference and implement a fail-soft boundary instead of presenting it as proven.

## Completion checklist

- Provider and Connection ids satisfy compositional-id constraints and Account/token filenames remain unambiguous.
- Keyless Connections default disabled; keyed Connections use usability rather than credential-only behavior gates.
- Adapter and discovery selector maps agree.
- Auth headers rebuild inside retry attempts; secrets never enter logs, fixtures, catalogs, or response payloads.
- `send()`, `stream()`, and `normalize_response()` agree on routing and canonical shapes.
- Replay scope and fidelity, media types, output-limit behavior, and other request rules come from the wire profile file; an Adapter override only narrows them by a per-Model catalog fact (GitHub Copilot's reported image types); `resources/wire/<id>.json` parses without issues (`test_bundled_wire_profile_files_are_valid`).
- Discovery failures do not erase unrelated Connection catalogs; generated facts and hand overrides retain separate ownership.
- Provider-specific tests and the relevant shared suites pass; docs point to the canonical shared or Provider-specific reference.

## Source and tests

- Registry/config: `core/providers/providers.py`, `resources/providers/`
- Runtime selector/factory: `core/providers/adapter_types.py`, `core/providers/runtime.py`
- Wire profiles: `resources/wire/`, `core/providers/wire_profile.py`, `core/providers/wire_profiles.py`; verification `scripts/verify_wire_profile.py`, snapshot `scripts/wire_snapshot.py`
- Discovery selector/pipeline: `core/models/discovery.py`
- Adapter implementations and tests: `core/providers/`, `tests/core/providers/`
- Model layers: `core/models/`, `resources/models/`, `models.md`
