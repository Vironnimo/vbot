## Research: OpenCode Go Copilot VS Code Provider — Architektur & Model-Integration

### Question
Wie integriert das VS Code Plugin `opencode-go-copilot` (https://github.com/OnesoftQwQ/opencode-go-copilot) verschiedene Modelle mit all ihren Edge Cases, Thinking-Modi, Vision-Unterstützung und API-Formaten? Was können wir daraus für unseren eigenen `opencode-go` Provider lernen?

### TL;DR
Das Plugin verwendet VS Code's `LanguageModelChatProvider` API mit einem **dual-format Ansatz** (OpenAI `chat/completions` + Anthropic `v1/messages`). Jedes Modell ist als `BuiltInModelDef` mit Metadaten wie `thinkingMode` ("switchable"|"always"), `apiMode` ("openai"|"anthropic"), Vision-Support und Extra-Parametern definiert. Es handhabt Edge Cases wie XML-Think-Blocks, DeepSeek's `reasoning_content` in Tool-Call-Nachrichten, OpenRouter's `reasoning_details`, diverses Thinking-Format (5+ Varianten), einen Vision-Proxy für Text-Modelle, sowie Token-Tracking mit Cache-Statistiken. Der gesamte Request-Response-Cycle ist in `provider.ts` zentral orchestriert, mit strikter Trennung der API-Formate in `openai/openaiApi.ts` und `anthropic/anthropicApi.ts`.

### Findings

#### 1. Architecture Overview

Das Plugin besteht aus diesen Kernkomponenten:

| Komponente | Datei | Rolle |
|---|---|---|
| **Provider (Entry Point)** | `provider.ts` | VS Code `LanguageModelChatProvider` Implementierung — orchestriert den gesamten Request/Response-Lebenszyklus |
| **Extension Activator** | `extension.ts` | Registriert Provider, Commands (setApiKey, commit, presets) |
| **Model Definitions** | `models.ts` | Hardcoded Liste aller Built-In Modelle mit Metadaten |
| **Model Lister** | `provideModel.ts` | Kombiniert Built-In + Zen Free Models |
| **OpenAI API** | `openai/openaiApi.ts` | OpenAI-kompatible Chat-Completions (SSE streaming) |
| **Anthropic API** | `anthropic/anthropicApi.ts` | Anthropic Messages API (SSE streaming) |
| **Common Base** | `commonApi.ts` | Abstrakte Basis mit gemeinsamem Streaming/Tool-Call/Thinking-Buffer |
| **Vision Proxy** | `vision/imageProxy.ts` | Beschreibt Bilder via Vision-Modell für Text-Modelle |
| **Zen Free Models** | `zen/zenModels.ts` | Optionale Free-Models von OpenCode Zen |
| **Token Tracking** | `statusBar.ts`, `provideToken.ts` | Status-Bar mit Token-Verbrauch und Cache-Statistiken |
| **Git Commit** | `gitCommit/commitMessageGenerator.ts` | Automatische Commit-Message-Generierung |

#### 2. Model Definition System (`models.ts`)

Jedes Modell wird als `BuiltInModelDef` definiert:

```typescript
interface BuiltInModelDef {
  baseId: string;          // API Model ID (z.B. "deepseek-v4-flash")
  displayName: string;     // Anzeigename (z.B. "DeepSeek V4 Flash")
  vision: boolean;         // natives Vision-Support?
  thinkingMode: "switchable" | "always";
  defaultReasoningEffort?: string;
  supportedReasoningEfforts?: string[];
  includeReasoningInRequest?: boolean;
  contextLength?: number;
  maxTokens?: number;
  extra?: Record<string, unknown>;       // Zusätzliche Body-Parameter
  apiMode?: "openai" | "anthropic";     // API-Format
}
```

**Aktuelle Built-In Modelle:**

| Modell | Vision | Thinking | API Mode | Context | Max Output |
|---|---|---|---|---|---|
| GLM-5.1 | ✗ | `always` | openai | 200K | 128K |
| GLM-5 | ✗ | `always` | openai | 200K | 128K |
| Kimi K2.5 | ✓ | `always` | openai | 256K | 16K |
| Kimi K2.6 | ✓ | `always` | openai | 256K | 16K |
| DeepSeek V4 Pro | ✗ | `switchable` (high/max) | openai | 1M | 384K |
| DeepSeek V4 Flash | ✗ | `switchable` (high/max) | openai | 1M | 384K |
| MiMo-V2-Pro | ✗ | `always` | openai | 256K | 32K |
| MiMo-V2-Omni | ✓ | `always` | openai | 256K | 32K |
| MiMo-V2.5-Pro | ✗ | `always` | openai | 256K | 32K |
| MiMo-V2.5 | ✗ | `always` | openai | 256K | 32K |
| MiniMax M2.7 | ✗ | `always` | **anthropic** | 200K | 32K |
| MiniMax M2.5 | ✗ | `always` | openai | 200K | 32K |
| Qwen3.6 Plus | ✓ | `switchable` | openai | 1M | 32K |
| Qwen3.5 Plus | ✓ | `switchable` | openai | 1M | 32K |

**Wichtig:** `MiniMax M2.7` verwendet als einziges Built-In Modell den **Anthropic API Mode** — die API-Response wird dann im Anthropic-Format (mit `thinking_delta`, `input_json_delta`, `tool_use`) gestreamt.

#### 3. Thinking / Reasoning Engine — 6 verschiedene Formate

Das Plugin parsed Thinking aus **allen diesen Quellen** in einer `processDelta`-Methode:

1. **`choice.delta.thinking`** — OpenAI `thinking` Feld
2. **`choice.delta.reasoning`** — Alternativer Feldname  
3. **`choice.delta.reasoning_content`** — DeepSeek-spezifisch
4. **`choice.thinking`** — Top-Level Thinking
5. **`choice.reasoning_details`** — OpenRouter-Format (Array von `reasoning.summary`, `reasoning.text`, `reasoning.encrypted`)
6. **XML Think Blocks** — `<think>...</think>` im Text-Content (für Modelle die denken in Text ausgeben)

Die Verarbeitung erfolgt in `commonApi.ts` mit einem **buffered thinking approach**:
- Alle Thinking-Blöcke werden in `_thinkingBuffer` gesammelt
- Ein Timer (100ms) flushed den Buffer in `LanguageModelThinkingPart`
- XML-Think-Blocks werden inline im Text-Content geparst

**`reasoningEffort`** wird von VS Code's `modelConfiguration.reasoningEffort` übernommen:
- `"disabled"` → Thinking aus (außer bei `thinkingMode: "always"`)
- `"enabled"` → Thinking an mit Default-Effort
- `"high"/"max"` → Thinking an mit spezifischem Effort

#### 4. Dual API Format Support

Der Provider unterstützt zwei API-Formate, gesteuert durch `apiMode` im Model Config:

**OpenAI Format** (`chat/completions`):
- SSE Stream mit `data:` Präfix
- `stream_options: { include_usage: true }` für Token Usage
- Auth via `Authorization: Bearer <key>`
- Tools als `tool_calls` im Delta
- Reasoning als `reasoning_content`, `thinking`, `reasoning_details`

**Anthropic Format** (`v1/messages`):
- SSE Stream mit `data:` Präfix, aber **eigenen Chunk-Typen**:
  - `message_start` → Input Token Count
  - `content_block_start` → Thinking/Tool-Use Block Anfang
  - `content_block_delta` → `text_delta`, `thinking_delta`, `input_json_delta`, `signature_delta`
  - `content_block_stop` / `message_stop` → Block/Message Ende
  - `message_delta` → Output Token Count
- Auth via `x-api-key` + `anthropic-version: 2023-06-01`
- System-Prompt als top-level `system` Feld (nicht in messages)

#### 5. Extended Vision Proxy System

Ein zentrales Feature: **Vision-Unterstützung für Text-Modelle**.

Ablauf:
1. Message-Conversion (`convertMessages`): Alle Images werden durch Text-Referenzen ersetzt (`[imageIndex=0]`)
2. Images werden in `CommonApi.storedImages` (statische Map) gespeichert
3. Ein `describe_image` Tool wird in den Request injected (`tool_choice: "auto"`)
4. Wenn der API-Stream einen `describe_image` Tool-Call liefert → **intercepted** (nicht an VS Code gereicht)
5. Vision-Proxy-Aufruf: `callVisionModel()` ruft das konfigurierte Vision-Modell (Default `qwen3.6-plus`)
6. **Second Round**: Neuer API-Request mit vorherigen Messages + Tool-Call + Description als Tool-Result

**Kritische Edge Cases hier:**
- **DeepSeek benötigt `reasoning_content`** auf Assistant-Nachrichten, selbst bei Tool-Calls → `reasoning_content: "Calling describe_image tool..."`
- **DeepSeek rejected forced `tool_choice`** → Das Plugin verwendet `tool_choice: "auto"` und setzt stattdessen starke Direktiven im Message-Text
- Settings: `visionProxyModel`, `visionProxyPrompt`, `visionProxyThinking`

#### 6. Cache Token Handling

Das Plugin trackt Cache-Statistiken aus **zwei Quellen**:

1. **OpenAI Format**: `prompt_tokens_details.cached_tokens`
2. **DeepSeek Format**: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` (überschreibt OpenAI)

Cache-Daten werden angezeigt:
- In der Status-Bar Tooltip (kumulativ)
- Im Native Copilot Indicator via `LanguageModelDataPart` mit MIME-Type `'usage'`
- Als Cache-Hit-Rate (Prozent)

#### 7. Tool Call Handling

Tool-Calls werden aus mehreren SSE-Chunks **zusammengebaut**:
- OpenAI: `delta.tool_calls[index].function.arguments` wird akkumuliert
- Anthropic: `input_json_delta` für `content_block[index]`

Ein Tool-Call wird erst emitted, wenn valide JSON-Arguments komplett sind (`tryEmitBufferedToolCall`).
Am Ende des Streams werden alle verbleibenden Buffer geflusht (`flushToolCallBuffers`).

**`read_file` Tool Enhancement**: Das Plugin kann `endLine` automatisch erhöhen (via `opencodego.readFileLines` Setting), damit das Modell mehr Zeilen liest.

#### 8. Retry & Error Handling

**Retry Logic** (`utils.ts`):
- Retryable Status Codes: `[429, 500, 502, 503, 504]`
- Network Errors: `fetch failed`, `ECONNRESET`, `ETIMEDOUT`, etc.
- Max 3 Versuche, exponentielles Backoff (1s → 2s → 4s, max 60s)
- Configurierbar via `opencodego.retry.*` Settings

**Error Handling** (`provider.ts`):
- Timeout vs Force-Terminated: Unterscheidung zwischen Abort und Server-Connection-Close
- Zen Free Model Expired: Spezifischer Error-Check auf `"no longer available as a free model"`
- API-Error-Response wird als Text gelesen und in die Error-Message inkludiert

#### 9. Token Counting

Client-seitiges Token-Counting (`provideToken.ts`):
- Verwendet `@microsoft/tiktokenizer` für akkurates Token-Zählen
- Image Token Cost: `85 + 170 * (ceil(w/512) * ceil(h/512))` (Standard-OpenAI-Formel)
- Non-Image Binary: ~0.75 Tokens/Byte
- Tool Token Cost: 16 Basis + 8 pro Tool

#### 10. Rate Limiting / Delay

Jedes Modell kann einen `delay` Parameter haben (`models.ts` → `BuiltInModelDef` → wird nicht direkt genutzt, aber `OpenCodeGoModelItem` unterstützt es). Global via `opencodego.delay` Setting.
Der Provider trackt `_lastRequestTime` und verzögert Requests wenn nötig.

### Comparison (im Kontext unseres Problems)

| Aspekt | opencode-go-copilot (dieses Plugin) | Unser aktuelles System |
|---|---|---|
| **Model Definition** | Hardcoded mit Metadaten (thinking, vision, apiMode, extra params) | — noch zu definieren |
| **API Formate** | OpenAI + Anthropic dual | Derzeit nur OpenAI |
| **Thinking** | 6 Formate, Pre-Processing von XML-Think-Blocks, buffered streaming | — |
| **Vision Proxy** | Vollständig implementiert mit Tool-Interception | — |
| **Token Tracking** | Client-side + API Usage + Cache Stats | — |
| **Error Handling** | Retry, Timeout, Zen-Expired, spezifische Messages | — |
| **Model Discovery** | Zen API + Hardcoded Fallback | — |
| **Tool Calls** | SSE-Chunk-Assembly, Read-File-Enhancement | — |

### Key Learnings für unsere Implementierung

1. **Model Config als `BuiltInModelDef`**: Jedes Modell braucht explizite Metadaten — insbesondere `thinkingMode`, `apiMode`, `vision`, `extra` für API-spezifische Parameter.

2. **Dual-API-Architektur**: Die Trennung in `OpenaiApi` und `AnthropicApi` mit gemeinsamer `CommonApi`-Basis ist sauber und erweiterbar. Beide teilen sich Tool-Call-Buffer, Thinking-Buffer und Vision-Proxy-Logik.

3. **Thinking ist der komplexeste Teil**: Die 6 verschiedenen Formate die das Plugin handhabt zeigen, dass Thinking/Reasoning der Bereich mit den meisten Inkompatibilitäten zwischen Modellen/Providern ist.

4. **Vision Proxy ist ein Must-Have**: Nutzer senden oft Images, auch an Text-Modelle. Der zweistufige Ansatz (Tool-Interception → Vision-Call → Second Request) ist elegant.

5. **SSE Streaming ist trickreich**: Tool-Calls kommen über mehrere Chunks verteilt, Thinking-Content muss gebuffered werden, verschiedene SSE-End-Marker (`[DONE]` vs `message_stop`), Timeout-Handling.

6. **Cache-Statistiken sind ein Differenzierungsmerkmal**: DeepSeek-Nutzer erwarten Cache-Info. Zwei Formate parallel zu unterstützen (OpenAI `cached_tokens` + DeepSeek `prompt_cache_*`) ist notwendig.

### Risks & Caveats
- Das Plugin verwendet **VS Code proposed APIs** (`LanguageModelChatProvider`, `LanguageModelDataPart`, `LanguageModelThinkingPart`), die sich mit neuen VS Code Versionen ändern können
- Der Vision Proxy ist als "experimentell" markiert und kann unzuverlässig sein
- Das Plugin cached Zen-Model-Liste nur 5 Minuten — bei häufigen API-Änderungen kann das zu Stale-Daten führen
- `read_file` Tool Enhancement könnte unexpected behavior bei Tools verursachen, die auch `startLine`/`endLine` Parameter haben
- Die hardcoded Model-Liste muss manuell gepflegt werden

### Sources
- https://github.com/OnesoftQwQ/opencode-go-copilot — vollständiges Repository
- `src/provider.ts` — Haupt-Provider mit Request-Orchestrierung
- `src/models.ts` — Built-In Model Definitionen mit allen Metadaten
- `src/commonApi.ts` — Abstrakte Basis mit Streaming/Tool-Call/Thinking-Logik
- `src/openai/openaiApi.ts` — OpenAI-kompatible API-Implementierung
- `src/anthropic/anthropicApi.ts` — Anthropic-kompatible API-Implementierung
- `src/vision/imageProxy.ts` — Vision Proxy für Text-Modelle
- `src/vision/types.ts` — Describe-Image Tool Definition und Typen
- `src/zen/zenModels.ts` — Zen Free Model Integration mit API-Cache
- `src/utils.ts` — Retry, Model-ID-Parsing, Tool-Conversion
- `src/statusBar.ts` — Token-Tracking und Cache-Statistiken
