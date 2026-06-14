# Glob Tool

Discovers filesystem paths by glob-style pattern.

## Interfaces

- Tool name: `glob`
- Registration: `register_glob_tool(registry)`
- Schema: required `pattern`; optional `path`; `additionalProperties: false`.
- Success data returns textual matches under `data.content`.
- Display: summary field `pattern`.

## Conventions

- Relative patterns such as `**/*.py` are supported.
- Relative `path` resolves from `ToolContext.workspace`; absolute search roots are allowed.
- Matches are returned relative to the search root; directory entries end with `/`.

## Constraints & Gotchas

- Results are sorted and capped at `MAX_GLOB_MATCHES`.
- No-match messages are success envelopes, not failures.
- Expected path/search errors return failure envelopes.
