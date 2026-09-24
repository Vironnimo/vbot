// Markdown rendering cost of a streamed assistant message.
//
// `markdown.stream[...]` replays how MarkdownContent renders a message while it
// streams: the whole growing source is re-rendered with
// renderMarkdownStreamingDocument after every 100-character chunk. One
// operation is one complete stream. `markdown.render_final[...]` renders the
// finished source once, which is the per-chunk cost once the message has
// reached its final size.
//
// markdown.js memoizes rendered documents in a module-level LRU. Each operation
// starts its source with a unique line so no operation reuses another's
// renders, while prefixes repeated inside one stream (the text before an open
// code fence) still hit the cache as they do in the app.
import { bench } from 'vitest';

import { renderMarkdownStreamingDocument } from '../markdown.js';
import {
  benchName,
  benchOptions,
  createRandom,
  markdownText,
} from './benchSupport.js';

const CHUNK_CHARS = 100;
const SIZES = [
  ['2k', 2_000],
  ['20k', 20_000],
];

let nonce = 0;

function uniqueSource(source) {
  nonce += 1;
  return `Draft ${nonce}\n\n${source}`;
}

for (const [label, size] of SIZES) {
  const source = markdownText(createRandom(size), size);
  const chunks = Math.ceil(source.length / CHUNK_CHARS);

  bench(
    benchName(`markdown.stream[${label}]`, {
      items: chunks,
      unit: 'chunk',
      chars: source.length,
      chunk_chars: CHUNK_CHARS,
    }),
    () => {
      const streamed = uniqueSource(source);
      const start = streamed.length - source.length;
      for (let end = start + CHUNK_CHARS; ; end += CHUNK_CHARS) {
        renderMarkdownStreamingDocument(streamed.slice(0, end));
        if (end >= streamed.length) break;
      }
    },
    benchOptions(),
  );

  bench(
    benchName(`markdown.render_final[${label}]`, { chars: source.length }),
    () => {
      renderMarkdownStreamingDocument(uniqueSource(source));
    },
    benchOptions(),
  );
}
