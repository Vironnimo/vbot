# Designing Agent-Facing Tools

Use this guide when adding a Tool, changing its public parameters, or migrating an older Tool contract. The primary outcome is an interface an Agent can call correctly from the Tool name, description, and one compact schema without guessing which fields select behavior.

## Choose the Public Shape

Use this decision order:

1. **One behavior:** expose its arguments directly in one closed flat object. A Tool named `channel_send` that only sends should accept delivery fields directly; `action: "send"` would repeat the Tool name.
2. **One behavior with optional targeting or selection:** keep direct optional target fields and validate their dependencies. `status()` checks the current Session, `status(session_id)` checks another Session for the same Agent, and `status(agent_id, session_id)` changes the owner and Session; these are target variants, not actions.
3. **Several genuinely different behaviors:** require one top-level `action` enum and place every action argument beside it. CRUD, lifecycle transitions, and read-versus-mutate behavior normally qualify. `memory(action, scope, content?, entry_id?)` and `history(action, ...)` are the reference shape.

Do not expose `request.operation`, an operation-key object such as `{"create": {...}}`, a stringified nested request, or several mutually exclusive booleans that encode actions. Do not infer a behavioral mode from an arbitrary combination of optional fields when a required `action` would state it directly.

## Parameters

- Keep the parameter set minimal and Agent-relevant. Do not expose implementation details such as internal source paths, executable flags, storage layout, working directories, or cache controls unless choosing them is part of the user's requested behavior.
- Represent one concept once. Prefer one normalized field over several overlapping schedule, selector, or mode fields; do not preserve aliases in the canonical Tool contract.
- Make a field optional only when omission has a real default, an unambiguous derived fallback, or selects the current target. State that behavior in the field or Tool description.
- Put fields required for every call in the schema's root `required` list. For a flat multi-action Tool, describe action-specific requirements on the relevant fields and enforce required and forbidden fields in the handler.
- Reject fields that are valid globally but inapplicable to the selected action. A typo or stale field must fail with `invalid_arguments`, not be ignored.
- Use stable IDs returned by prior Tool Results for follow-up mutations. If IDs can shift, say when the Agent must list or refresh first.
- Use one vocabulary consistently: `action` for a behavior discriminator, domain names for arguments, and existing project terms for entities. Do not alternate between `action`, `operation`, `mode`, and `type` for the same role.

## Schema and Handler Boundary

- The canonical input is a root JSON object with `additionalProperties: false`.
- The schema owns field names, JSON types, enums, global requirements, ranges, lengths, and structurally simple dependencies. The handler owns action-specific field sets, cross-field meaning, authorization, existence, state transitions, and other semantic checks.
- Prefer a small flat schema plus precise handler errors over branch-heavy `oneOf`/`anyOf` trees used only to encode actions. Unions remain appropriate for genuine value-shape alternatives within one behavior.
- Do not coerce strings into numbers or booleans, accept aliases, or silently normalize retired public shapes. Runtime validation and handler validation must agree on the canonical types.
- Provider `strict` eligibility is a consequence, not the design goal. Do not force irrelevant optional fields to be required-and-null merely to retain an OpenAI `strict: true` marker; review and pin the intentional profile decision in provider-schema tests.

## Descriptions and Defaults

- The Tool description says what the Tool accomplishes, when to use it, and any decision the Agent must make. Do not spend the description teaching the Agent to construct an avoidably complex envelope.
- An `action` description names every action in operational language. Each conditional field says which actions require it.
- Document defaults and fallbacks exactly. Avoid fuzzy time expressions, hidden unit conversions, sentinel strings, or defaults that change by Provider.
- Keep examples canonical and short. Show omitted optional fields as omitted, not as `null`, unless `null` is a meaningful accepted value.

## Results, Errors, and Display

- Keep the stable vBot Tool Result envelope. Define a success-data schema and return stable target identifiers, state, or action outcome when they help the next call.
- Expected failures use precise codes and actionable messages. Set retry metadata only when it changes what the Agent should do.
- Do not add public arguments solely to improve UI labels or conceal sensitive values. Use `ToolDisplay.summary_builder` and `hidden_argument_keys`.
- A result should not expose internal filesystem paths, provenance, or implementation state unless the Agent needs that value for the next in-scope operation.

## Migrating an Existing Tool

1. Inventory every accepted public shape, action, required field, default, handler branch, display summary, result field, permission rule, and persisted or UI consumer.
2. Classify the capability with the decision order above. Do not mechanically replace every `operation` with `action`: remove the discriminator entirely when the variants only choose a target or repeat a single-purpose Tool name.
3. Define the smallest canonical flat schema. Preserve behavior and established domain field names unless a name itself causes ambiguity.
4. Update the handler to consume the flat object directly and to reject action-inapplicable fields before side effects. Remove public normalizers that reconstruct the retired envelope.
5. Reject retired nested, operation-key, alias, and stringified shapes. Do not run old and new Agent-facing contracts in parallel.
6. Preserve historical presentation separately when needed: the WebUI may continue reading old persisted arguments, but current dispatch accepts only the new contract.
7. Recheck Provider rendering and intentional `strict` eligibility, schema fingerprints, Tool descriptions, `ToolDisplay`, prompts, E2E fake-provider calls, and any generated Tool catalogs.
8. Test the exact public schema, every valid action or target form, missing conditional requirements, forbidden action fields, unknown fields, retired shapes, stable success data, and expected failures.
9. Update the owning Tool map, this domain map when the convention changes, and any prompt or user-facing documentation that teaches the call shape.

## Current Migration Rule

`operation_envelope_schema` and `extract_tool_operation` exist only for Tools that have not yet been redesigned. Their presence proves current implementation, not desired architecture. Migrate one Tool as a complete contract change with its tests and documentation; remove the shared helpers only after their final caller is gone.
