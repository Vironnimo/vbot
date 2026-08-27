# Designing Agent-Facing Tools

Use this guide when adding a Tool, changing its public parameters, or migrating an older Tool contract. Repository-root `TOOLS.md` is the normative authoring standard; this supplementary guide records project-specific shape decisions and migration routing.

## Choose the Public Shape

Use this decision order:

1. **One behavior:** expose its arguments directly in one open flat object. A Tool named `channel_send` that only sends should accept delivery fields directly; `action: "send"` would repeat the Tool name.
2. **One repeatable independent behavior:** use one required plural array of compact operation objects when batching materially reduces Agent roundtrips. Preserve input order, keep per-operation options on each item, and report indexed outcomes; `edit(edits[])` is the reference shape.
3. **One behavior with optional targeting or selection:** keep direct optional target fields and validate their dependencies. `status()` checks the current Session, `status(session_id)` checks another Session for the same Agent, and `status(agent_id, session_id)` changes the owner and Session; these are target variants, not actions.
4. **Several genuinely different behaviors:** require one top-level `action` enum and place every action argument beside it. CRUD, lifecycle transitions, and read-versus-mutate behavior normally qualify. `memory(action, scope, content?, entry_id?)` and `history(action, ...)` are the reference shape.

Do not expose `request.operation`, an operation-key object such as `{"create": {...}}`, a stringified nested request, or several mutually exclusive booleans that encode actions. Do not infer a behavioral mode from an arbitrary combination of optional fields when a required `action` would state it directly.

## Parameters

- Keep the parameter set minimal and Agent-relevant. Do not expose implementation details such as internal source paths, executable flags, storage layout, working directories, or cache controls unless choosing them is part of the user's requested behavior.
- Represent one concept once. Prefer one normalized field over several overlapping schedule, selector, or mode fields; do not preserve aliases in the canonical Tool contract.
- When two Tool names separate discovery from exact retrieval but share the same authority and lifecycle, keep one configurable capability and derive the reader as a companion. A search result should return the reader's directly callable argument shape so the Agent does not translate identifiers or depend on both toggles being configured independently.
- Make a field optional only when omission has a real default, an unambiguous derived fallback, or selects the current target. Tell the Model when to omit the field; mention the resulting default or fallback only when it affects that decision.
- Put a field in `required` only when every valid call needs it. A multi-action Tool requires `action` and advertises the shared optional-property superset; its handler enforces action-specific requirements and dependencies.
- Reject fields that are unknown or inapplicable to the selected action in the handler before side effects. A typo or stale field must fail with `invalid_arguments`, not be ignored.
- Use stable IDs returned by prior Tool Results for follow-up mutations. If IDs can shift, say when the Agent must list or refresh first.
- Use one vocabulary consistently: `action` for a domain-behavior discriminator, `mode` only for a genuine execution contract such as Bash foreground/auto/background, domain names for arguments, and existing project terms for entities. Do not alternate between `action`, `operation`, `mode`, and `type` for the same role.

## Schema and Handler Boundary

- The canonical model-facing input has one open JSON-object root with `type`, `properties`, and `required`; it never emits `additionalProperties`.
- The schema owns field names, simple JSON types, small fixed enums, universal requirements, and only constraints that materially help the Model construct a valid call. The handler owns unknown fields, conditional requirements, inapplicable fields, actual defaults, cross-field meaning, authorization, existence, state transitions, and other semantic checks.
- An Action Tool uses one required `action` enum in the same flat property set as every action argument. `oneOf` remains appropriate only inside one genuinely multi-representation parameter, such as `read.offset`; it never encodes actions, modes, targeting variants, optionality, or cursor continuation.
- Do not coerce strings into numbers or booleans, accept aliases, or silently normalize retired public shapes. Runtime validation and handler validation must agree on the canonical types.
- Provider strict mode is disabled across vBot. Design the natural canonical contract: never force optional fields to be required-and-null, remove schema features, or otherwise reshape a Tool for a Provider's strict-schema subset. Provider rendering must preserve this schema, while vBot runtime validation remains authoritative.

## Descriptions and Defaults

- The Tool description says what the Tool accomplishes, when to use it, and any decision the Agent must make. Do not spend the description teaching the Agent to construct an avoidably complex envelope.
- An `action` description names every action in operational language. Each conditional field says which actions require it.
- The handler owns every actual default. Describe an optional field from the Model's decision point: state the condition for omitting it, then add the omission result only when that result changes the decision. Never emit non-numeric schema defaults, and use a numeric schema default only under the narrow stable, plan-relevant conditions in `TOOLS.md`.
- Keep examples canonical and short. Show omitted optional fields as omitted, not as `null`, unless `null` is a meaningful accepted value.

## Results, Errors, and Display

- Keep the stable vBot Tool Result envelope. Define a success-data schema and return stable target identifiers, state, or action outcome when they help the next call.
- For batched independent operations, keep results in input order with stable indices. Mixed outcomes may use a successful envelope with an explicit `partial` data status when completed operations must remain applied; zero successful operations use a failure envelope.
- Expected failures use precise codes and actionable messages. Set retry metadata only when it changes what the Agent should do.
- Do not add public arguments solely to improve UI labels or conceal sensitive values. Use `ToolDisplay.summary_builder` and `hidden_argument_keys`.
- A result should not expose internal filesystem paths, provenance, or implementation state unless the Agent needs that value for the next in-scope operation.

## Migrating an Existing Tool

1. Inventory every accepted public shape, action, required field, default, handler branch, display summary, result field, permission rule, and persisted or UI consumer.
2. Classify the capability with the decision order above. Do not mechanically replace every `operation` with `action`: remove the discriminator entirely when the variants only choose a target or repeat a single-purpose Tool name.
3. Define the smallest canonical open flat schema. For multiple behaviors, require `action` and publish one shared optional-property superset. Preserve behavior and established domain field names unless a name itself causes ambiguity.
4. Update the handler to consume the flat object directly and to reject action-inapplicable fields before side effects. Remove public normalizers that reconstruct the retired envelope.
5. Reject retired nested, operation-key, alias, and stringified shapes. Do not run old and new Agent-facing contracts in parallel.
6. Preserve historical presentation separately when needed: the WebUI may continue reading old persisted arguments, but current dispatch accepts only the new contract.
7. Recheck Provider rendering and the non-strict invariant, schema fingerprints, Tool descriptions, `ToolDisplay`, prompts, E2E fake-provider calls, and any generated Tool catalogs.
8. Pass the current production definition directly to Luna through `scripts/probe_provider_tool_call.py` and compare exact arguments for every action or mode, meaningful explicit value, and default-selecting omission. Separately test missing conditional requirements, forbidden action fields, unknown fields, retired shapes, stable success data, and expected failures through the handler.
9. Update the owning Tool map, this domain map when the convention changes, and any prompt or user-facing documentation that teaches the call shape.

## Retired Shapes

The shared `operation_envelope_schema` and `extract_tool_operation` compatibility helpers were removed after the final nested Tool migrated. Do not reintroduce them or accept `request.operation`; preserve any required rendering of historical persisted calls in the WebUI without widening current dispatch.
