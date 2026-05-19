## Research: Conversation compaction and context management for LLM chat and agent applications

### Question

Research the current landscape of conversation compaction and context management strategies for LLM-based chat and agent applications, with a focus on:

- what compaction is
- the main strategy families and trade-offs
- how real products and frameworks handle it
- agent and tool-call specific concerns
- which patterns best fit a configurable, settings-driven product
- non-obvious implementation gotchas

### TL;DR

The strongest current systems do not rely on pure truncation. In practice, the dominant pattern is a hybrid: prune low-value bulky artifacts first, preserve a recent verbatim tail, and replace older closed history with an anchored structured summary. Retrieval and long-term memory are complementary, not substitutes for short-term prompt-budget compaction.

For vBot specifically, the best fit is a logical compaction checkpoint layered on top of append-only JSONL Sessions: keep raw history intact, append an internal compaction summary/checkpoint, and have request assembly use the latest checkpoint plus the preserved tail. This preserves auditability and existing chat/provider invariants while keeping the feature explainable through a small set of settings.

### What compaction is

Compaction is a lossy transformation of active short-term conversation state when the prompt budget is close to the model's context limit. In the strongest implementations, it is not just "drop old messages." It usually combines several operations:

- remove the bulkiest low-value material first, especially old tool output
- preserve recent turns verbatim
- summarize older closed turns into a continuation-ready state description
- re-inject durable instructions from outside the conversation when the product supports them

Claude Code's glossary defines compaction as automatic summarization when the context window approaches its limit and explicitly notes that older tool outputs are cleared first, then the conversation is summarized. That is a useful working definition because it reflects what current agent-first products actually do, not just what a generic chat app might do.

Compaction is not the same thing as:

- plain truncation or a sliding window
- retrieval or long-term semantic memory
- manual session reset or clear
- subagent delegation or branching, which are adjacent context-management strategies that avoid bloating the main session in the first place

### Strategy taxonomy

| Strategy | Mechanism | Strengths | Costs / failure modes | Best fit |
|---|---|---|---|---|
| Hard truncation | Drop oldest messages once over budget | Very simple, deterministic, cheap | Loses decisions, file references, constraints, and tool context abruptly | Basic chat, emergency fallback |
| Sliding token window | Keep only the newest tokens/messages | Better than naive truncation, predictable | Still loses older context and can break structured tool histories if not validity-aware | Short chat sessions, simple copilots |
| Summarization compaction | Replace older history with a summary | Retains intent and progress across long sessions | Summary drift, omission, stale facts, summarizer quality dependence | Long-running coding or agent sessions |
| Selective pruning / context editing | Remove or compress specific high-cost parts, usually old tool outputs | High token savings with less semantic loss | Must preserve message validity and enough execution trace | Tool-heavy agents |
| Retrieval / semantic memory | Store facts externally and pull back relevant ones later | Good for long-term persistence across sessions | Does not solve active-session overflow on its own; relevance can miss critical current context | Cross-session personalization and long-lived assistants |
| Hybrid anchored summary + recent tail + pruning | Combine pruning, preserved tail, and structured summary | Best balance of fidelity, cost, and controllability | More moving parts, requires careful invariants | Agent products and coding assistants |
| Session hygiene controls | Manual clear, partial summarize, subagents, branch/rewind, side-questions | Prevents context bloat before it happens | Not automatic; depends on user or orchestration behavior | Advanced tools and agent harnesses |

### Real-world examples

#### Claude Code

- Claude Code automatically compacts as context limits approach.
- Its docs explicitly say older tool outputs are cleared first, then the conversation is summarized.
- It exposes manual `/compact`, full reset via `/clear`, and partial checkpoint-based summarize/restore flows via `/rewind`.
- Claude documents what survives compaction in unusual detail: root `CLAUDE.md`, auto memory, unscoped rules, and invoked skills are re-injected; path-scoped rules and nested `CLAUDE.md` files are lost until relevant files are read again.
- This is one of the clearest examples of compaction as part of a broader context-management system rather than a single summarization function.

Assessment:

- Strongest public documentation of compaction semantics and survivorship rules.
- Especially useful for understanding the difference between durable external instructions and in-conversation ephemeral state.

Sources:

- https://code.claude.com/docs/en/best-practices
- https://code.claude.com/docs/en/sessions
- https://code.claude.com/docs/en/glossary
- https://code.claude.com/docs/en/context-window

#### Aider

- Aider performs automatic chat-history summarization rather than exposing a first-class compaction workflow.
- User-facing knobs include `--max-chat-history-tokens` and `--weak-model`.
- Current code constructs `ChatSummary([weak_model, main_model], max_chat_history_tokens)`, so summaries try a weak model first and fall back to the main model if needed.
- The default `max_chat_history_tokens` is derived as one-sixteenth of model input context, clamped between 1024 and 8192 tokens.
- The implementation keeps a recent tail, summarizes the older head, and recurses until the result fits.
- The summarization prompt explicitly says to preserve function names, libraries, packages, and filenames.
- Public commands docs show `/clear`, `/reset`, `/drop`, and `/tokens`, but no dedicated `/compact` command surfaced in current docs.

Assessment:

- Good example of lightweight, mostly automatic summary-based history compression.
- Less useful than OpenCode or Continue as a model for tool-call-heavy compaction because the design center is chat history summarization, not message-validity-aware function-calling pipelines.

Sources:

- https://aider.chat/docs/config/options.html
- https://aider.chat/docs/usage/commands.html
- https://github.com/Aider-AI/aider/blob/main/aider/history.py
- https://github.com/Aider-AI/aider/blob/main/aider/models.py
- https://github.com/Aider-AI/aider/blob/main/aider/prompts.py
- https://github.com/Aider-AI/aider/blob/main/aider/coders/base_coder.py
- https://github.com/Aider-AI/aider/blob/main/tests/basic/test_history.py

#### Continue

- Continue supports manual compaction through `/compact` in the CLI path.
- It also performs automatic compaction before API calls when a threshold is reached.
- The auto-compaction threshold is token-aware and explicitly accounts for system prompt tokens and tool definition tokens.
- The threshold logic reserves the model output budget plus an extra capped safety buffer. In tests, this effectively behaves like a guarded high-water mark rather than a naive fixed percentage.
- Continue also forces compaction after tool execution if tool results caused the prompt to overflow, which is an important agent-specific detail.
- After compaction, Continue can automatically insert a synthetic `continue` user message to resume the session, but only when the turn would otherwise stop and no tool-call continuation is pending.
- Continue's public config reference mentions a `summarize` model role, but the docs also say that role is not currently used. The current compaction path in repo code uses the current model and LLM API passed into the chat path rather than a clearly separate summarizer role.

Assessment:

- Strong example of a production-minded agent chat loop that treats compaction as part of runtime control flow, not just session maintenance.
- Particularly valuable for the post-tool overflow case and for auto-continuation rules.

Sources:

- https://github.com/continuedev/continue/blob/main/extensions/cli/src/compaction.ts
- https://github.com/continuedev/continue/blob/main/extensions/cli/src/commands/chat.ts
- https://github.com/continuedev/continue/blob/main/extensions/cli/src/stream/streamChatResponse.autoCompaction.ts
- https://github.com/continuedev/continue/blob/main/extensions/cli/src/stream/streamChatResponse.compactionHelpers.ts
- https://github.com/continuedev/continue/blob/main/extensions/cli/src/stream/streamChatResponse.ts
- https://github.com/continuedev/continue/blob/main/extensions/cli/src/ui/hooks/useChat.compaction.ts
- https://github.com/continuedev/continue/blob/main/extensions/cli/src/stream/streamChatResponse.autoContinuation.test.ts
- https://github.com/continuedev/continue/blob/main/extensions/cli/src/util/tokenizer.test.ts
- https://docs.continue.dev/reference

#### OpenCode

- OpenCode is the clearest current example of a configurable compaction system built for agentic sessions.
- Public config exposes `compaction.auto`, `compaction.prune`, and `compaction.reserved`.
- The schema also includes `tail_turns` and `preserve_recent_tokens`, which are exactly the kinds of settings that map well to user-facing configuration.
- Internally, OpenCode has a hidden native `compaction` agent and an explicit compaction prompt.
- Its compaction prompt is anchored: if a previous summary exists, the system updates that previous summary instead of rewriting from scratch.
- It preserves a recent verbatim tail using both turn-count and token-budget logic.
- It prunes older tool outputs before compaction and protects some recent/tool-critical material from pruning.
- It exposes plugin hooks to inject extra compaction context or replace the compaction prompt entirely.
- It also has an auto-continue path after compaction, with a plugin hook to control that behavior.

Assessment:

- Best concrete reference for a settings-driven compaction design.
- Strongest exemplar for anchored summaries, preserved recent tail, tool-output pruning, and customization hooks.

Sources:

- https://opencode.ai/docs/config/
- https://opencode.ai/docs/plugins/
- https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts
- https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/agent/agent.ts
- https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/agent/prompt/compaction.txt
- https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/config/config.ts
- https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts
- https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts
- https://github.com/anomalyco/opencode/blob/dev/packages/opencode/test/session/compaction.test.ts

#### LangChain and LangGraph

- LangGraph docs frame the problem as short-term memory management for long conversations and explicitly list trim, delete, summarize, checkpoint, and custom strategies.
- LangGraph's delete-message docs explicitly warn that resulting message history must remain valid and call out a key invariant: assistant messages with tool calls usually must be followed by corresponding tool result messages.
- LangGraph's summarize-messages example uses an incremental summary state and deletes all but the most recent messages afterward.
- LangChain's `SummarizationMiddleware` exposes a separate summarizer model, a trigger condition, and a keep policy.
- LangChain's `ContextEditingMiddleware` makes tool-output clearing a first-class pattern through `ClearToolUsesEdit`.
- This is more of a framework toolbox than a product opinion, but it gives the clearest framework-level articulation of valid message-history constraints.

Assessment:

- Best source for framework-level invariants and middleware decomposition.
- Especially useful for validating any vBot design around tool-call adjacency and separate summarizer-model support.

Sources:

- https://docs.langchain.com/oss/python/langgraph/add-memory
- https://docs.langchain.com/oss/python/langchain/middleware/built-in

#### Open WebUI

- Open WebUI strongly emphasizes persistent memory, RAG, web search, chat search, and chat retrieval.
- It exposes tools such as `search_memories`, `search_chats`, and `view_chat`.
- The memory path uses embeddings and vector search over user memories.
- Chat search looks up prior chats and message content rather than compacting the active short-term session history.
- I did not find strong evidence of a general-purpose short-term conversation compaction pipeline analogous to Claude Code, Continue, or OpenCode.

Assessment:

- Useful contrast case.
- Good reminder that memory/retrieval and compaction solve related but different problems.

Sources:

- https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/memories.py
- https://github.com/open-webui/open-webui/blob/main/backend/open_webui/tools/builtin.py
- https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/chats.py
- https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/retrieval.py
- https://github.com/open-webui/open-webui/blob/main/README.md

### Comparison

| System | Default pattern | User-facing knobs | Tool-call / agent awareness | Notes |
|---|---|---|---|---|
| Claude Code | Auto compaction with tool-output clearing first, then summary | `/compact`, `/clear`, checkpoint summarize | Strong product-level context management, docs emphasize survivorship rules | Best documentation of what survives compaction |
| Aider | Automatic summarization of older history | `--max-chat-history-tokens`, `--weak-model` | Limited structured tool-history emphasis | Simple, lightweight summary compaction |
| Continue | Manual and automatic compaction with overflow checks | `/compact`; threshold behavior mostly implicit | Strong: pre-call, post-tool overflow, auto-continue gating | Good runtime loop example |
| OpenCode | Hybrid anchored summary + recent tail + pruning | `auto`, `prune`, `reserved`, `tail_turns`, `preserve_recent_tokens`, hooks | Strong: compaction agent, pruning, overflow, autocontinue hooks | Best fit for settings-driven design |
| LangChain / LangGraph | Framework primitives | Middleware params | Explicit validity warnings for tool-call histories | Best framework reference |
| Open WebUI | Memory, retrieval, chat search | Memory/RAG settings | Focus is retrieval, not compaction | Complementary, not substitute |

### Agent and tool-call handling

This is the area where simple chat compaction strategies most often fail.

Observed invariants across real systems:

- Do not compact across unresolved tool cycles. Compact only across closed turns or after all sibling tool results are in.
- Preserve message validity. In tool-calling systems, an assistant tool-call message usually must remain adjacent to corresponding tool result messages.
- Prefer pruning old tool outputs before pruning user intent or assistant decisions.
- If you prune tool output, preserve enough structure to keep the history interpretable: tool name, call/result linkage, maybe a short preview or external pointer.
- Post-tool overflow is a distinct trigger. Tool outputs can blow up context suddenly even when pre-call threshold checks passed.
- Auto-continue after compaction is only safe when the model is not still in a tool-use continuation state.
- Opaque provider reasoning payloads are different from readable reasoning text. They cannot be treated as normal summary material.

The strongest explicit statement here comes from LangGraph docs: deleting messages can invalidate provider expectations, especially when assistant tool-call messages are no longer followed by tool result messages.

### Summarization-specific findings

#### Incremental anchored summaries are better than repeated full rewrites

OpenCode updates a prior anchored summary instead of starting fresh every time. LangGraph's summary-state example also treats the summary as evolving state. This is a better pattern than repeatedly summarizing the whole conversation from scratch because it:

- reduces prompt size for later compactions
- makes summary structure more stable
- gives you a place to explicitly remove stale facts rather than just append more detail

#### Summary prompts need stronger constraints than generic chat summarization

The best prompts are task-resumption prompts, not "summarize this chat" prompts.

Patterns found in the wild:

- Aider explicitly instructs the summarizer to preserve function names, libraries, packages, and filenames.
- OpenCode's compaction prompt uses a structured template with sections for goal, constraints, progress, decisions, next steps, critical context, and relevant files.
- Claude Code documents survivorship rules outside the summary itself, which implicitly reduces what the summary must carry.

The common lesson is that a good coding-agent summary should preserve:

- exact file paths
- identifiers and commands
- error strings and blockers
- what was already tried
- the current task status
- the next concrete step

#### Separate summarizer models are useful but not mandatory

- Aider explicitly uses a weak model first and falls back to the main model.
- LangChain's middleware examples encourage a dedicated summarizer model.
- Continue currently appears to reuse the active chat model for compaction.
- OpenCode's hidden compaction agent makes separate compaction-model configuration possible through agent config.

Conclusion:

- A separate summarizer model is a good advanced option.
- It should not be the only mode, because same-model compaction is simpler and avoids cross-model drift.
- If a separate summarizer exists, fallback behavior matters.

### Trigger strategies

| Trigger | Observed in | Strengths | Risks |
|---|---|---|---|
| Manual compact command | Claude Code, Continue, OpenCode | User control, debuggable, good for deliberate cleanup | Too late if overflow already happened |
| Pre-call token threshold | Claude Code, Continue, OpenCode, LangChain middleware | Prevents most overflows before request send | Needs good token accounting for tools and system prompt |
| Post-tool overflow check | Continue, OpenCode | Catches tool-result explosions | Must preserve unfinished tool-call semantics |
| Full clear/reset | Claude Code, Aider | Strongest reset of bad or noisy history | Loses continuity entirely |
| Subagent or side-question isolation | Claude Code, LangChain subagents | Avoids bloating main context in the first place | More orchestration complexity |

My read is that the best default trigger strategy is:

- pre-call token threshold for normal operation
- post-tool overflow fallback for tool-heavy turns
- manual compact command for user control and debugging
- clear/reset for unrelated tasks or badly polluted sessions

### Configurability patterns

The most successful configurable systems expose a small number of knobs that map to understandable behavior. The knobs that repeatedly show up as useful are:

- `mode`: off, manual, auto
- `reserved_tokens`: keep a safety buffer for compaction and continuation
- `tail_turns`: how many recent user turns to keep verbatim
- `preserve_recent_tokens`: additional cap for recent preserved context
- `prune_tool_outputs`: whether to prune old tool outputs first
- `summary_model`: optional override for a dedicated summarizer / compaction model
- `custom_compaction_instructions`: advanced control over prompt shape
- `auto_continue_after_compaction`: whether to resume automatically when safe

These settings are good because they are:

- explainable to users
- testable in isolation
- robust across providers
- compatible with both auto and manual workflows

Patterns that look less suitable for a settings-driven product:

- opaque importance scoring heuristics with no clear user mental model
- retrieval-only context management presented as if it solved short-term overflow
- too many provider-specific compaction knobs in the main settings UI

### Recommendation for vBot

This recommendation is based on vBot's chat and provider constraints in [chat spec](../specs/chat.md), [providers spec](../specs/providers.md), and [webui spec](../specs/webui.md).

#### Recommended default strategy

Use a hybrid policy by default:

1. prune stale tool outputs from older closed turns
2. preserve a recent verbatim tail using both `tail_turns` and `preserve_recent_tokens`
3. replace older history with an anchored structured summary

This is the best fit because vBot is explicitly agentic, tool-using, session-persistent, and settings-driven.

#### Recommended architecture for vBot

Do not destructively rewrite session history.

vBot Sessions are append-only JSONL, which is a major local design constraint. That means compaction should be implemented as a logical checkpoint, not as file mutation that deletes or rewrites earlier messages.

Recommended shape:

- keep raw JSONL history append-only for auditability and replay
- append an internal compaction checkpoint record that contains the structured anchored summary and metadata describing the preserved tail boundary
- during request assembly, use the latest checkpoint plus the preserved tail rather than naively replaying the full raw session

For vBot, the cleanest persistence vehicle is likely an internal note-like record rather than a normal assistant message. A normal assistant summary would pollute user-visible chat history. An internal checkpoint or specialized note keeps the normal transcript clean while staying compatible with append-only persistence.

#### Recommended invariants

- Never compact across an unresolved tool cycle.
- Preserve assistant/tool adjacency and `tool_call_id` validity.
- Strip or ignore stale completed-turn `reasoning_meta` when compacting or changing providers.
- Keep current-turn reasoning round-trip behavior only where the provider/tool continuation requires it.
- Treat compaction summaries as continuation state, not as user-visible assistant answers.

#### Recommended settings surface

For the initial settings-driven version, expose:

- `compaction_mode`: `off`, `manual`, `auto`
- `reserved_tokens`
- `tail_turns`
- `preserve_recent_tokens`
- `prune_old_tool_outputs`
- `summary_model` as an optional advanced override
- `auto_continue_after_compaction`
- `custom_compaction_instructions` as an advanced textarea

This maps cleanly onto the existing Settings architecture described in the WebUI spec.

#### Recommended user experience

- Provide a manual compact action in the chat surface.
- Show a compacted-history status event or system-style UI marker for transparency.
- Keep raw details inspectable in debug/export surfaces, not in the ordinary chat transcript.
- Keep clear/reset available as a separate action for unrelated tasks.

#### Recommended scope for long-term memory

Treat retrieval memory as a later complementary layer, not as the first release of compaction.

Compaction should solve short-term active-session overflow.
Retrieval memory should solve cross-session recall and personalization.

### Gotchas and non-obvious design decisions

- Append-only persistence changes the implementation shape. Products that rewrite their working chat history can physically replace history; vBot should probably add a checkpoint layer instead.
- Tool-output truncation and compaction are related but separate. OpenCode has both `tool_output` truncation and `compaction.prune`; that separation is healthy.
- System and tool schema tokens matter. Continue explicitly counts system prompt and tool definitions in threshold decisions.
- Some instructions naturally survive compaction because they reload from disk. Others do not. Claude Code's docs make this visible; many custom systems forget it.
- Incremental summaries must remove stale facts, not only append. Otherwise the summary becomes a garbage heap.
- Auto-continue can cause loops if the flag is not carefully reset. Continue has explicit tests for this.
- A public config surface can imply capabilities that runtime does not actually use. Continue's docs mention a `summarize` role, but the docs also note it is not currently used.
- Weak-model summarization is viable if fallback exists. Aider explicitly tests fallback-to-main behavior.
- Retrieval of prior chats or memories is not a substitute for active short-term compaction. Open WebUI is a good example of why these should be separated conceptually.
- Provider-specific opaque reasoning artifacts are especially dangerous. In vBot, completed-turn `reasoning_meta` cannot be blindly carried across provider changes; compaction must respect that.

### Bottom line

The current landscape strongly favors hybrid compaction for agentic systems. The design that appears most transferable to vBot is:

- token-aware auto trigger with manual override
- prune old tool outputs first
- keep a recent verbatim tail
- maintain an anchored structured summary of older closed history
- treat compaction as logical session state, not destructive transcript rewrite
- preserve tool-call validity and provider-specific continuation invariants

Among the public examples, OpenCode is the best match for a configurable settings-driven implementation, Continue is the best runtime-loop reference for tool-heavy chat, Claude Code is the best documentation of survivorship semantics, Aider is a useful lightweight summarization example, and LangChain/LangGraph provide the clearest framework-level invariants.
