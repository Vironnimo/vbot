# vBot Tool Design Rules

## Purpose

This file is the normative authoring standard for every model-facing vBot Tool. A Tool definition exists to make the Model choose the right Tool and emit the right arguments with the smallest practical context footprint. It is not handler documentation, UI documentation, an implementation schema, or a security boundary.

When rules compete, use this priority order: correct Tool selection and invocation, unambiguous Model guidance, small context footprint, and Provider portability.

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

- State when to use the Tool, what it does, and only the operational limitations that change how the Model should call it.
- Every Tool must have a clear description. Context efficiency comes from removing redundancy and irrelevant implementation detail, not from withholding guidance the Model needs.
- Lead with the capability. Do not begin with architecture, implementation, or result-envelope details.
- Keep the description as short as possible without making Tool selection ambiguous.
- Do not repeat information already obvious from the Tool name, property names, types, enums, or `required` list.
- Do not describe handler internals, libraries, storage layout, UI rendering, logging, telemetry, or validation machinery unless the Model must account for it when calling the Tool.
- Mention result behavior only when it affects Tool choice, pagination, follow-up calls, or safe use.
- Do not reference another Tool unless that Tool is guaranteed to be available in the same Model context. Availability-dependent guidance belongs in a dynamically gated prompt block.

## Parameter Objects

- Use a plain JSON Schema object with `type`, `properties`, and `required`.
- Never emit `additionalProperties` in a model-facing Tool schema.
- Put a field in `required` only when every valid call requires it.
- Make a field optional by leaving it out of `required`. Do not add `null`, nullable unions, sentinel values, or duplicate optionality metadata.
- Use `required: []` for a Tool with no required arguments.
- Use the narrowest simple type that preserves the callable interface.
- Use an enum when the Model must choose from a small fixed vocabulary.
- Add bounds, patterns, length limits, or format hints only when they materially help the Model construct a valid argument. Runtime-only constraints do not belong in the model-facing schema.
- Do not add a root parameter-object description unless it conveys conditional argument rules that cannot be stated more compactly on the affected properties.

## Parameter Descriptions

- Describe the value the Model should provide, not the Python type or handler variable.
- Every parameter must have a concise description that makes its purpose and omission behavior clear to the Model. A short self-evident description is preferable to no description.
- For an optional parameter whose omission has meaningful behavior, say `Omit to ...` or state the omission behavior in equally direct language.
- For a conditionally required parameter, state the relevant action or mode directly, for example `Required for write and submit.`
- Do not repeat `required`, enum values, numeric bounds, or defaults in prose unless the repetition resolves a real ambiguity.
- Use examples only when the expected syntax is not obvious from the schema.

## Defaults

- The handler is the sole authority for every actual default.
- Do not emit the JSON Schema `default` keyword for strings, booleans, enums, arrays, objects, or null values.
- A numeric `default` may be emitted only when omission is common, the number materially helps the Model plan the call, the value is stable, and the handler applies exactly the same value.
- Do not add a numeric `default` merely because the handler has one. Pagination sizes, result limits, offsets, and timeouts are typical candidates; incidental implementation constants are not.
- Explain non-numeric omission behavior in the parameter description only when the Model needs to know it.
- Never rely on a Provider to apply a default or inject an omitted argument.

## Action Tools

- Represent an Action Tool as one flat object.
- Make `action` a required string enum.
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

## Multi-Type Parameters

- Prefer one simple type.
- Use `oneOf` only when one individual parameter genuinely accepts multiple representations and changing that public input would remove useful capability.
- Never use a union to encode Action Tool branches or optionality.
- Keep union branches free of duplicated descriptions and constraints.

## Provider Rendering

- Canonical Tool definitions remain Provider-neutral.
- Provider adapters may change only the transport wrapper required by the Provider, such as OpenAI Chat `function`, OpenAI Responses `name` plus `parameters`, or Anthropic `input_schema`.
- Provider rendering must never enable strict Tool calling.
- Provider rendering must not add defaults, required fields, nullable types, closed-object keywords, or other semantics absent from the canonical Tool definition.

## Runtime Requirements

- Handlers must safely reject or normalize malformed arguments regardless of what the model-facing schema permits.
- Handlers must apply defaults explicitly. An omitted argument must behave correctly even if the Provider ignores every schema annotation.
- Authorization, path safety, input sanitization, resource limits, and destructive-action checks belong to runtime code and must never depend on Model compliance.
- Runtime errors should identify the invalid argument and the valid correction without exposing implementation details.

## Change and Verification Discipline

- Change exactly one Tool at a time. Shared Tool infrastructure may change with it only when that Tool requires the change and every previously migrated Tool remains verified.
- Keep the repository releaseable after every Tool migration. Do not leave unrelated Tools partially converted.
- Before moving to the next Tool, run its focused local tests and complete Luna call matrix. Run the relevant complete quality gate once after the full Tool migration, not between individual Tools.
- Before moving to the next Tool, pass its current production definition directly to Luna through `scripts/probe_provider_tool_call.py`; a live installation round-trip is not required for model-facing schema verification.
- The Luna matrix must exercise every action or mode, every optional-parameter omission that selects a default, every materially different explicit value, and representative invalid calls that the handler must reject safely.
- A Tool is migrated only when every matrix call produces a satisfactory Tool Call and runtime result. Documentation or schema inspection alone is not verification.
- Commit each migrated and fully verified Tool as its own cohesive releaseable change before editing the next Tool.

## Review Checklist

Before accepting a Tool definition, verify all of the following:

- The Tool can be selected correctly from its name and first description sentence.
- Every required argument is in `required`, and every optional argument is absent from it.
- Every meaningful omission behavior is clear to the Model.
- No `additionalProperties` keyword is present.
- No non-numeric JSON Schema `default` is present.
- Every Action Tool has one flat object and only `action` is unconditionally required unless another field is truly required by every action.
- Parameter descriptions contain no duplicated schema facts or runtime internals.
- Every parameter still has enough description for the Model to use it correctly.
- Cross-Tool guidance cannot point to an unavailable Tool.
- The handler independently validates conditional requirements and applies all defaults.
- The Provider wire explicitly remains non-strict where the Provider supports strict Tool calling.
- The minified Provider definition has been measured, and every remaining sentence or schema keyword earns its context cost.
- Focused tests and the complete Luna call matrix pass before another Tool is changed; the relevant complete quality gate passes once at the end of the full migration.
