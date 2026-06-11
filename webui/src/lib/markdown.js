import markdownit from 'markdown-it';

const FENCE_PATTERN = /^[ \t]{0,3}```[^\n]*$/gm;

const md = markdownit({
  html: false,
  // Chat output is conversational: a single newline is an intentional line
  // break ("one number per line" lists), not a soft wrap to collapse.
  breaks: true,
  linkify: false,
  typographer: false,
});

const defaultLinkOpenRender =
  md.renderer.rules['link_open'] ||
  ((tokens, idx, options, _env, self) =>
    self.renderToken(tokens, idx, options));

md.renderer.rules['link_open'] = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank');
  tokens[idx].attrSet('rel', 'noopener noreferrer');
  return defaultLinkOpenRender(tokens, idx, options, env, self);
};

// Rendering is pure for a given source string, so we memoize results. The chat
// timeline rebuilds every visible item on each streaming flush (~30x/second),
// which would otherwise re-parse the Markdown of every finished message on every
// tick. The cache turns those into O(1) lookups; only the one actively
// streaming block (whose source keeps changing) misses and re-parses. The cache
// is bounded with least-recently-used eviction so it cannot grow without limit.
const RENDER_CACHE_LIMIT = 300;
const renderCache = new Map();

function cachedRender(src) {
  const cached = renderCache.get(src);
  if (cached !== undefined) {
    // Refresh recency: move the entry to the end of the insertion order.
    renderCache.delete(src);
    renderCache.set(src, cached);
    return cached;
  }

  const html = md.render(src);
  renderCache.set(src, html);
  if (renderCache.size > RENDER_CACHE_LIMIT) {
    const oldestKey = renderCache.keys().next().value;
    renderCache.delete(oldestKey);
  }
  return html;
}

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function lastUnclosedFenceIndex(src) {
  const matches = Array.from(src.matchAll(FENCE_PATTERN));
  if (matches.length === 0 || matches.length % 2 === 0) {
    return -1;
  }
  return matches.at(-1)?.index ?? -1;
}

export function renderMarkdown(src) {
  if (!src) return '';
  return cachedRender(src);
}

export function renderMarkdownStreaming(src) {
  if (!src) return '';

  const openFenceIndex = lastUnclosedFenceIndex(src);
  if (openFenceIndex === -1) {
    return renderMarkdown(src);
  }

  const prefix = src.slice(0, openFenceIndex);
  const fenceBlock = src.slice(openFenceIndex);
  const firstNewlineIndex = fenceBlock.indexOf('\n');
  const codeContent =
    firstNewlineIndex === -1 ? '' : fenceBlock.slice(firstNewlineIndex + 1);

  const prefixHtml = prefix ? renderMarkdown(prefix) : '';
  return `${prefixHtml}<pre><code>${escapeHtml(codeContent)}</code></pre>`;
}
