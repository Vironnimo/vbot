# Designing Agent-Facing Tools

This is the design map for the agent-facing part of every Tool: the model-facing definition the Model sees. Read it whenever work touches that surface - adding a Tool, changing its public contract, migrating an older shape, or editing a description, parameter, or model-facing schema. It owns the design rules; concrete per-Tool behavior contracts live in the sibling maps under `tools/`, routed from `tools.md`.

## Purpose and Priority

A Tool definition exists to make the Model choose the right Tool and emit the right arguments with the smallest practical context footprint. It is not handler documentation, UI documentation, an implementation schema, or a security boundary. When rules compete, use this priority order: correct Tool selection and invocation, unambiguous Model guidance, small context footprint, and Provider portability.

## Model Contract and Runtime Contract

- The model-facing Tool definition describes only what the Model needs to choose and call the Tool.
- The handler owns actual defaults, normalization, conditional validation, authorization, security checks, side effects, and error handling.
- A permissive model-facing schema is acceptable when its descriptions reliably guide the Model and the handler safely validates execution.
- Do not expand the model-facing schema merely to encode every invalid runtime state.

## Tool Names

- Use a short, stable, descriptive `snake_case` name.
- Prefer an established domain noun or verb. Do not include implementation details, Provider names, transport names, or versions.
- Keep one Tool responsible for one coherent capability. Separate genuinely different discovery and execution operations when combining them would make invocation ambiguous.

## Tool Descriptions

- State when to use the Tool, what it does, and only the operational limitations that change how the Model should call it. Say what the Tool accomplishes and any decision the Agent must make; do not spend the description teaching the Model to construct an avoidably complex envelope.
- Every Tool must have a clear description. Context efficiency comes from removing redundancy and irrelevant implementation detail, not from withholding guidance the Model needs.
- Lead with the capability. Do not begin with architecture, implementation, or result-envelope details.
- Keep the description as short as possible without making Tool selection ambiguous.
- Do not repeat information already obvious from the Tool name, property names, types, enums, or `required` list.
- Do not describe handler internals, libraries, storage layout, UI rendering, logging, telemetry, or validation machinery unless the Model must account for it when calling the Tool.
- Mention result behavior only when it affects Tool choice, pagination, follow-up calls, or safe use.
- Do not reference another Tool unless that Tool is guaranteed to be available in the same Model context. Availability-dependent guidance belongs in a dynamically gated prompt block.

## Choose the Public Shape

Use this decision order:

1. **One behavior:** expose its arguments directly in one open flat object. A Tool named `channel_send` that only sends should accept delivery fields directly; `action: "send"` would repeat the Tool name.
2. **One repeatable independent behavior:** use one required plural array of compact operation objects when batching materially reduces Agent roundtrips. State ordering semantics in the array description, preserve input order, keep per-operation options on each item, and report indexed outcomes; `edit(edits[])` is the reference shape.
3. **One behavior with optional targeting or selection:** keep direct optional target fields and validate their dependencies. `status()` checks the current Session, `status(session_id)` checks another Session for the same Agent, and `status(agent_id, session_id)` changes the owner and Session; these are target variants, not actions.
4. **Several genuinely different behaviors:** require one top-level `action` enum and place every action argument beside it. CRUD, lifecycle transitions, and read-versus-mutate behavior normally qualify. `memory(action, scope, content?, entry_id?)` and `history(action, ...)` are the reference shape.

Do not expose `request.operation`, an operation-key object such as `{"create": {...}}`, a stringified nested request, or several mutually exclusive booleans that encode actions. Do not infer a behavioral mode from an arbitrary combination of optional fields when a required `action` would state it directly.

Action Tool mechanics:

- Represent an Action Tool as one flat object. Make `action` a required string enum whose description names every action in operational language.
- Put all arguments used by any action in the same `properties` object.
- Leave action-dependent arguments out of the root `required` list and state their action dependency briefly in their descriptions.
- Do not represent actions with `oneOf`, `anyOf`, `allOf`, conditional schemas, or duplicated per-action object branches.
- The handler must validate the selected action's required arguments and return a concise actionable error for an invalid combination.

Example:

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "enum": ["list", "read", "write"]
    },
    "id": {
      "type": "string",
      "description": "Required for read and write."
    },
    "content": {
      "type": "string",
      "description": "Required for write."
    }
  },
  "required": ["action"]
}
```

## Parameters

- Keep the parameter set minimal and Agent-relevant. Do not expose implementation details such as internal source paths, executable flags, storage layout, working directories, or cache controls unless choosing them is part of the user's requested behavior.
- Represent one concept once. Prefer one normalized field over several overlapping schedule, selector, or mode fields; do not preserve aliases in the canonical Tool contract.
- When two Tool names separate discovery from exact retrieval but share the same authority and lifecycle, keep one configurable capability and derive the reader as a companion. A search result should return the reader's directly callable argument shape so the Agent does not translate identifiers or depend on both toggles being configured independently.
- Use a plain JSON Schema object with `type`, `properties`, and `required`. Never emit `additionalProperties` in a model-facing Tool schema.
- Put a field in `required` only when every valid call requires it. Make a field optional by leaving it out of `required`. Do not add `null`, nullable unions, sentinel values, or duplicate optionality metadata. Use `required: []` for a Tool with no required arguments.
- Use the narrowest simple type that preserves the callable interface. Use an enum when the Model must choose from a small fixed vocabulary. Add bounds, patterns, length limits, or format hints only when they materially help the Model construct a valid argument; runtime-only constraints do not belong in the model-facing schema.
- Do not add a root parameter-object description unless it conveys conditional argument rules that cannot be stated more compactly on the affected properties.
- Make a field optional only when omission has a real default, an unambiguous derived fallback, or selects the current target.
- Use stable IDs returned by prior Tool Results for follow-up mutations. If IDs can shift, say when the Agent must list or refresh first.
- Use one vocabulary consistently: `action` for a domain-behavior discriminator, `mode` only for a genuine execution contract such as Bash foreground/auto/background, domain names for arguments, and existing project terms for entities. Do not alternate between `action`, `operation`, `mode`, and `type` for the same role.

## Parameter Descriptions

- Describe the value the Model should provide, not the Python type or handler variable.
- Every parameter must have a concise description that makes its purpose clear and, when optional, tells the Model when to omit it. A short self-evident description is preferable to no description.
- State the omission condition from the Model's decision point, for example `Omit when no specific destination is given.` Add the resulting omission behavior or runtime default only when it materially affects that decision.
- For a conditionally required parameter, state the relevant action or mode directly, for example `Required for write and submit.`
- Do not repeat `required`, enum values, numeric bounds, or defaults in prose unless the repetition resolves a real ambiguity.
- Use examples only when the expected syntax is not obvious from the schema. Keep examples canonical and short. Show omitted optional fields as omitted, not as `null`, unless `null` is a meaningful accepted value.

## Defaults

- The handler is the sole authority for every actual default.
- Do not emit the JSON Schema `default` keyword for strings, booleans, enums, arrays, objects, or null values.
- A numeric `default` may be emitted only when omission is common, the number materially helps the Model plan the call, the value is stable, and the handler applies exactly the same value.
- Do not add a numeric `default` merely because the handler has one. Pagination sizes, result limits, offsets, and timeouts are typical candidates; incidental implementation constants are not.
- Explain a non-numeric omission result in the parameter description only when it materially affects whether the Model should omit the field.
- Never rely on a Provider to apply a default or inject an omitted argument.

## Schema and Handler Boundary

- The canonical model-facing input has one open JSON-object root with `type`, `properties`, and `required`.
- The schema owns field names, simple JSON types, small fixed enums, universal requirements, and only constraints that materially help the Model construct a valid call. The handler owns unknown fields, conditional requirements, inapplicable fields, actual defaults, cross-field meaning, authorization, existence, state transitions, and other semantic checks.
- Reject fields that are unknown or inapplicable to the selected action in the handler before side effects. A typo or stale field must fail with `invalid_arguments`, not be ignored.
- Do not coerce strings into numbers or booleans, accept aliases, or silently normalize retired public shapes. Runtime validation and handler validation must agree on the canonical types.
- Handlers must safely reject or normalize malformed arguments regardless of what the model-facing schema permits, and must apply defaults explicitly: an omitted argument must behave correctly even if the Provider ignores every schema annotation.
- Authorization, path safety, input sanitization, resource limits, and destructive-action checks belong to runtime code and must never depend on Model compliance.
- Runtime errors should identify the invalid argument and the valid correction without exposing implementation details.

## Multi-Type Parameters

- Prefer one simple type.
- Use `oneOf` only when one individual parameter genuinely accepts multiple representations and changing that public input would remove useful capability; `read.offset` is the reference case. A union never encodes actions, modes, targeting variants, optionality, or cursor continuation.
- Never use a union to encode Action Tool branches or optionality.
- Keep union branches free of duplicated descriptions and constraints.

## Provider Rendering

- Canonical Tool definitions remain Provider-neutral.
- Provider adapters may change only the transport wrapper required by the Provider, such as OpenAI Chat `function`, OpenAI Responses `name` plus `parameters`, or Anthropic `input_schema`.
- Provider rendering must never enable strict Tool calling. That is a deliberate decision - strict mode repeatedly caused problems in practice - so design the natural canonical contract instead: never force optional fields to be required-and-null, remove schema features, or otherwise reshape a Tool for a Provider's strict-schema subset.
- Provider rendering must not add defaults, required fields, nullable types, closed-object keywords, or other semantics absent from the canonical Tool definition.

## Results, Errors, and Display

- Keep the stable vBot Tool Result envelope. Define a success-data schema and return stable target identifiers, state, or action outcome when they help the next call.
- For batched independent operations, keep results in input order with stable indices. Mixed outcomes may use a successful envelope with an explicit `partial` data status when completed operations must remain applied; zero successful operations use a failure envelope.
- Expected failures use precise codes and actionable messages. Set retry metadata only when it changes what the Agent should do.
- Do not add public arguments solely to improve UI labels or conceal sensitive values. Use `ToolDisplay.summary_builder` and `hidden_argument_keys`.
- A result should not expose internal filesystem paths, provenance, or implementation state unless the Agent needs that value for the next in-scope operation.

## Changing or Migrating an Existing Tool

1. Inventory every accepted public shape, action, required field, default, handler branch, display summary, result field, permission rule, and persisted or UI consumer.
2. Classify the capability with the decision order above. Do not mechanically replace every `operation` with `action`: remove the discriminator entirely when the variants only choose a target or repeat a single-purpose Tool name.
3. Define the smallest canonical open flat schema. For multiple behaviors, require `action` and publish one shared optional-property superset. Preserve behavior and established domain field names unless a name itself causes ambiguity.
4. Update the handler to consume the flat object directly and to reject action-inapplicable fields before side effects. Remove public normalizers that reconstruct the retired envelope.
5. Reject retired nested, operation-key, alias, and stringified shapes. Do not run old and new Agent-facing contracts in parallel.
6. Preserve historical presentation separately when needed: the WebUI may continue reading old persisted arguments, but current dispatch accepts only the new contract.
7. Recheck Provider rendering and the non-strict invariant, schema fingerprints, Tool descriptions, `ToolDisplay`, prompts, E2E fake-provider calls, and any generated Tool catalogs.
8. Pass the current production definition directly to Luna through `scripts/probe_provider_tool_call.py` and compare exact arguments for every action or mode, meaningful explicit value, and default-selecting omission. Separately test missing conditional requirements, forbidden action fields, unknown fields, retired shapes, stable success data, and expected failures through the handler.
9. Update the owning Tool map, this design map when the convention changes, and any prompt or user-facing documentation that teaches the call shape.

## Retired Shapes

The shared `operation_envelope_schema` and `extract_tool_operation` compatibility helpers were removed after the final nested Tool migrated. Do not reintroduce them or accept `request.operation`; preserve any required rendering of historical persisted calls in the WebUI without widening current dispatch.

## Change and Verification Discipline

- Change exactly one Tool at a time. Shared Tool infrastructure may change with it only when that Tool requires the change and every previously verified Tool remains verified.
- Keep the repository releaseable after every Tool change. Do not leave unrelated Tools partially converted.
- Before moving to the next Tool, run its focused local tests and complete the Luna call matrix, and pass its current production definition directly to Luna through `scripts/probe_provider_tool_call.py`. A live installation round-trip is not required for model-facing schema verification.
- The Luna matrix must exercise every action or mode, every optional-parameter omission that selects a default, every materially different explicit value, and representative invalid calls that the handler must reject safely.
- A Tool change is verified only when every matrix call produces a satisfactory Tool Call and runtime result. Documentation or schema inspection alone is not verification.
- Commit each fully verified Tool as its own cohesive releaseable change before editing the next Tool.
- Quality gates follow the repository rules in `AGENTS.md` - a scoped non-mutating pass while working, the full gate once when the task closes. Tool work adds no separate gate schedule.

## Review Checklist

Before accepting a Tool definition, verify all of the following:

- The Tool can be selected correctly from its name and first description sentence.
- Every required argument is in `required`, and every optional argument is absent from it.
- Every optional parameter tells the Model when to omit it; omission results appear only when they affect that decision.
- No `additionalProperties` keyword is present in the model-facing schema.
- No non-numeric JSON Schema `default` is present.
- Every Action Tool has one flat object and only `action` is unconditionally required unless another field is truly required by every action.
- Parameter descriptions contain no duplicated schema facts or runtime internals.
- Every parameter still has enough description for the Model to use it correctly.
- Cross-Tool guidance cannot point to an unavailable Tool.
- The handler independently validates conditional requirements and applies all defaults.
- The Provider wire explicitly remains non-strict where the Provider supports strict Tool calling.
- The minified Provider definition has been measured, and every remaining sentence or schema keyword earns its context cost.
- Focused tests and the complete Luna call matrix pass before another Tool is changed.