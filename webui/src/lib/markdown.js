import markdownit from 'markdown-it';

const FENCE_PATTERN = /^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$/gm;
const SINGLE_FENCE_PATTERN = /^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$/;

const md = markdownit({
  html: false,
  // Chat output is conversational: a single newline is an intentional line
  // break ("one number per line" lists), not a soft wrap to collapse.
  breaks: true,
  linkify: false,
  typographer: false,
});

md.renderer.rules.fence = (tokens, idx, _options, env) => {
  const token = tokens[idx];
  const codeBlocks = Array.isArray(env?.codeBlocks) ? env.codeBlocks : null;
  const codeBlockIndex = codeBlocks?.length ?? -1;
  const language =
    fenceLanguage(token.info) || env?.plainLanguageLabel || 'text';

  codeBlocks?.push({
    text: token.content,
    copyable: true,
  });

  return codeBlockHtml({
    content: token.content,
    language,
    codeBlockIndex,
    copyable: Boolean(codeBlocks),
  });
};

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

function cachedRenderDocument(src, plainLanguageLabel) {
  const cacheKey = `${plainLanguageLabel}\u0000${src}`;
  const cached = renderCache.get(cacheKey);
  if (cached !== undefined) {
    // Refresh recency: move the entry to the end of the insertion order.
    renderCache.delete(cacheKey);
    renderCache.set(cacheKey, cached);
    return cached;
  }

  const codeBlocks = [];
  const document = {
    html: md.render(src, { codeBlocks, plainLanguageLabel }),
    codeBlocks,
  };
  renderCache.set(cacheKey, document);
  if (renderCache.size > RENDER_CACHE_LIMIT) {
    const oldestKey = renderCache.keys().next().value;
    renderCache.delete(oldestKey);
  }
  return document;
}

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function fenceLanguage(info) {
  return typeof info === 'string' ? (info.trim().split(/\s+/)[0] ?? '') : '';
}

function codeBlockHtml({ content, language, codeBlockIndex, copyable }) {
  const copySlot = copyable
    ? `<span class="msg-code__copy-slot" data-markdown-code-index="${codeBlockIndex}"></span>`
    : '';

  return (
    '<div class="msg-code">' +
    '<div class="msg-code__header">' +
    `<span class="msg-code__language">${escapeHtml(language)}</span>` +
    copySlot +
    '</div>' +
    `<pre><code>${escapeHtml(content)}</code></pre>` +
    '</div>\n'
  );
}

function lastUnclosedFenceIndex(src) {
  let openFence = null;
  for (const match of src.matchAll(FENCE_PATTERN)) {
    const marker = match[1];
    const suffix = match[2] ?? '';
    if (!openFence) {
      openFence = {
        markerCharacter: marker[0],
        markerLength: marker.length,
        index: match.index,
      };
      continue;
    }

    if (
      marker[0] === openFence.markerCharacter &&
      marker.length >= openFence.markerLength &&
      suffix.trim() === ''
    ) {
      openFence = null;
    }
  }
  return openFence?.index ?? -1;
}

export function renderMarkdownDocument(
  src,
  { plainLanguageLabel = 'text' } = {},
) {
  if (!src) return { html: '', codeBlocks: [] };
  return cachedRenderDocument(src, plainLanguageLabel);
}

export function renderMarkdown(src, options) {
  return renderMarkdownDocument(src, options).html;
}

// Providers embed literal HTML comments in reasoning text as section separators
// (OpenAI's Responses summaries emit `\n\n<!-- -->` between parts). With
// `html: false` markdown-it would escape those into visible `<!-- -->` noise, so
// strip them before rendering. A partial `<!--` mid-stream simply stays until
// its `-->` arrives and the pair is removed.
const HTML_COMMENT_PATTERN = /<!--[\s\S]*?-->/g;
// Responses reasoning summaries can arrive as adjacent bold Markdown blocks.
// Keep both emphasis pairs while making their semantic boundary visible.
const ADJACENT_BOLD_REASONING_BLOCKS_PATTERN = /(?<!\*)\*{4}(?!\*)/g;

export function reasoningMarkdownSource(src) {
  if (typeof src !== 'string') return '';
  return src
    .replace(HTML_COMMENT_PATTERN, '')
    .replace(ADJACENT_BOLD_REASONING_BLOCKS_PATTERN, '**\n**');
}

export function renderReasoningMarkdownDocument(src, options) {
  return renderMarkdownDocument(reasoningMarkdownSource(src), options);
}

export function renderReasoningMarkdown(src, options) {
  return renderReasoningMarkdownDocument(src, options).html;
}

export function renderReasoningMarkdownStreamingDocument(src, options) {
  return renderMarkdownStreamingDocument(reasoningMarkdownSource(src), options);
}

export function renderReasoningMarkdownStreaming(src, options) {
  return renderReasoningMarkdownStreamingDocument(src, options).html;
}

export function renderMarkdownStreamingDocument(
  src,
  { plainLanguageLabel = 'text' } = {},
) {
  if (!src) return { html: '', codeBlocks: [] };

  const openFenceIndex = lastUnclosedFenceIndex(src);
  if (openFenceIndex === -1) {
    return renderMarkdownDocument(src, { plainLanguageLabel });
  }

  const prefix = src.slice(0, openFenceIndex);
  const fenceBlock = src.slice(openFenceIndex);
  const firstNewlineIndex = fenceBlock.indexOf('\n');
  const openingFenceLine =
    firstNewlineIndex === -1
      ? fenceBlock
      : fenceBlock.slice(0, firstNewlineIndex);
  const openingFenceMatch = SINGLE_FENCE_PATTERN.exec(openingFenceLine);
  const fenceInfo = openingFenceMatch?.[2] ?? '';
  const codeContent =
    firstNewlineIndex === -1 ? '' : fenceBlock.slice(firstNewlineIndex + 1);
  const prefixDocument = prefix
    ? renderMarkdownDocument(prefix, { plainLanguageLabel })
    : { html: '', codeBlocks: [] };
  const codeBlocks = [
    ...prefixDocument.codeBlocks,
    { text: codeContent, copyable: false },
  ];
  const html =
    prefixDocument.html +
    codeBlockHtml({
      content: codeContent,
      language: fenceLanguage(fenceInfo) || plainLanguageLabel,
      codeBlockIndex: codeBlocks.length - 1,
      copyable: false,
    });

  return { html, codeBlocks };
}

export function renderMarkdownStreaming(src, options) {
  return renderMarkdownStreamingDocument(src, options).html;
}
