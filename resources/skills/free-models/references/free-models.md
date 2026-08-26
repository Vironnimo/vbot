# Free Models Snapshot

Snapshot date: **2026-08-26**, verified against the live sources below. Free lineups
churn weekly — models vanish, lose their free variant, or get replaced. Treat every
entry as a hint until the live check passes, and update this file whenever you refresh
the skill.

## Verify live

- **OpenRouter:** fetch `https://openrouter.ai/api/v1/models` (public, no key needed).
  An entry is free when its `pricing.prompt` and `pricing.completion` are `"0"`; it is
  Sub-Agent eligible only when `supported_parameters` includes `tools`. Cross-reference
  against `vbot model list --task chat` — only ids served by an enabled, credentialed
  Connection are usable.
- **OpenCode Zen:** the pricing table at `https://opencode.ai/docs/zen` marks free rows;
  alternatively `GET https://opencode.ai/zen/v1/models` with the connection's key. vBot's
  Provider id is `opencode-zen`, so ids read `opencode-zen/<model-id>`.

## Rate limits and gotchas

- OpenRouter free variants: 20 requests/minute; 50 requests/day unless the account has
  purchased at least $10 in credits ever (then 1000/day); HTTP 402 even for free Models
  while the balance is negative. Mid-stream 429s arrive as stream errors.
- OpenCode Zen's free group is limited-time and rotates; entries can disappear without
  notice.
- Free does not mean private: see the privacy notes per entry and the rules in SKILL.md.

## OpenRouter (prefix `openrouter/`)

| Model id | Context | Strengths | Watch-outs |
|---|---|---|---|
| `z-ai/glm-5.2:free` | 256k | Strong generalist and code, reasoning, structured outputs | Good default pick for code-adjacent delegation |
| `minimax/minimax-m3:free` | 1M | Long context, image+video input, tools | Strong for summarize/extract over large inputs |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 1M | Large reasoning model | Slower; NVIDIA trial terms — no personal/confidential data |
| `nvidia/nemotron-3.5-lightning:free` | 1M | Fast, light, long context | Same NVIDIA trial terms |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262k | Mid-size reasoning, structured outputs | Same NVIDIA trial terms |
| `google/gemma-4-31b-it:free` | 262k | Vision and video input, solid small generalist | — |
| `google/gemma-4-26b-a4b-it:free` | 262k | Smaller, faster sibling of the above | Weaker on complex instructions |
| `thinkingmachines/inkling:free` | 1M | Multimodal input incl. audio | New architecture — verify output quality on your task |
| `thinkingmachines/inkling-small:free` | 1M | Faster sibling of the above | Same caveat |
| `cohere/north-mini-code:free` | 256k | Code-focused | Narrow strengths outside code |
| `poolside/laguna-s-2.1:free` | 262k | Code-focused | — |
| `poolside/laguna-xs-2.1:free` | 262k | Smaller code model | — |
| `dots-studio/dots-3-note-preview:free` | 512k | Note-style summaries over long input | Preview tier, may vanish |
| `stealth/ox-alpha` | 1M | Stealth preview, currently free, full tool support | Unknown provenance and lifespan — re-verify daily |
| `openrouter/free` | 200k | Auto-router across whatever is free now | Variable quality by design; good for throwaway work |

Known-free but **not** Sub-Agent eligible: `nvidia/nemotron-3.5-content-safety:free`
(classifier, no tools), the Google Lyria previews (music generation, no tools),
`liquid/lfm-2.5-2.6b:free` (65k context, too small for agent work).

## OpenCode Zen (prefix `opencode-zen/`)

The whole group is limited-time free and rotates; re-check before every session of use.

| Model id | Strengths | Watch-outs |
|---|---|---|
| `big-pickle` | Stealth, reported near-paid coding quality | Inputs may be used to improve the model during the free period |
| `mimo-v2.5-free` | Generalist | Data may be used during the free period |
| `hy3-free` | Generalist | Data may be used during the free period |
| `nemotron-3-ultra-free` | Large reasoning mirror of NVIDIA's free endpoint | Trial terms — no personal or confidential data |
| `nemotron-3.5-lightning-free` | Fast light mirror of NVIDIA's free endpoint | Trial terms as above |
| `x-preview-f-free` | Stealth preview with zero-retention provider | Unlabeled origin, limited time |
| `muse-spark-1.2-contributor-free` | Full-size model at zero cost | Explicitly trains on your prompts and completions (contributor tier) — only with the user's informed OK |

## Maintenance note

When updating this file: re-run both live checks above, reconcile the tables, correct
rate-limit facts against the linked official pages, and bump the snapshot date. The
bundled skill directory is read-only at runtime — refreshes happen in the repository.
