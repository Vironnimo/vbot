# LM Studio Provider

LM Studio is a keyless local Provider that combines its native Model-management API with its OpenAI-compatible Chat Completions wire.

## Configuration and discovery

The bundled `lmstudio` Provider points at `http://localhost:1234` and exposes one `local` Connection (`type: none`, `auto_refresh: true`). It follows the shared keyless addition contract in `providers/connections.md`: before addition it is passive and appears only in Add Provider; adding enables the Connection and refreshes its catalog without loading a Model.

Discovery uses native `GET /api/v1/models`, not the sparse OpenAI-compatible Model list. `LMStudioAdapter.normalize_catalog_entry()` keeps only `type: llm`, maps `key`/`display_name`, vision, Tool training, binary reasoning support, architecture, and `max_context_length`, and stamps `metadata.lmstudio.local: true` so the shared local context policy applies.

## Chat and model loading

Chat uses LM Studio's OpenAI-compatible `/v1/chat/completions` endpoint by extending `OpenAICompatibleAdapter`; canonical message, Tool, streaming, retry, and response behavior therefore stay in the existing deep Adapter owner.

Before each send or stream, the Adapter checks native loaded instances for the selected Model under a per-Adapter lock. An already loaded instance is reused unchanged. If none is loaded, the Adapter calls `POST /api/v1/models/load` once with the Model id and the live effective `context_length` from Runtime; the default for an unconfigured local Model is `min(32768, theoretical max)`. Discovery and Provider addition never call the load endpoint.

Model loading is transport behavior only. It emits no Provider-specific Chat message, Timeline entry, or Chat progress text; ordinary Settings success/reachability feedback remains owned by the Add Provider surface.

## Constraints and verification

- LM Studio's native `/api/v1/chat` is not used because vBot needs the complete existing Chat Completions message and Tool contract.
- The native API also advertises embedding Models, but this Chat Provider catalog intentionally skips them; specialized embedding execution has a separate domain and lifecycle.
- Real-hardware verification must avoid loading LM Studio and Ollama Models simultaneously on constrained hosts. That is a test-execution constraint, not runtime coordination policy.
- Focused coverage lives in `tests/core/providers/test_lmstudio.py`, Provider discovery/runtime tests, and Settings Provider tests.
