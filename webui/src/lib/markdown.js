import markdownit from 'markdown-it';

const FENCE_PATTERN = /^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$/gm;
const SINGLE_FENCE_PATTERN = /^[ \t]{0,3}(`{3,}|~{3,})([^\n]*)$/;

const md = markdownit({
  html: false,
  // Chat output is conversational: a single newline is an intentional line
  // break ("one number per line" lists), not a soft wrap to collapse.
  breaks: true,
  linkify: true,
  typographer: false,
});

// Literal links in Chat are deliberately narrower than Markdown's explicit
// link syntax: only complete HTTP(S) URLs become interactive. This keeps email,
// protocol-relative, FTP, and path-like text inert unless the author explicitly
// writes a Markdown link.
md.linkify
  .set({ fuzzyLink: false, fuzzyEmail: false, fuzzyIP: false })
  .add('ftp:', null)
  .add('//', null)
  .add('mailto:', null);

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

// Only finished text belongs in the shared cache. A growing answer has a new
// source at every flush; retaining those prefixes keeps hundreds of obsolete
// documents alive. Mounted MarkdownContent instances own their current render.
// Bound both entry count and retained UTF-16 text bytes, including source keys,
// so a few very large finished messages cannot monopolize either cache.
const RENDER_CACHE_LIMIT = 300;
const RENDER_CACHE_BYTES = 4 * 1024 * 1024;
const renderCache = createTextCache();
const linkifiedTextCache = createTextCache();

function createTextCache() {
  const entries = new Map();
  let bytes = 0;
  return {
    get(key) {
      const entry = entries.get(key);
      if (!entry) return undefined;
      entries.delete(key);
      entries.set(key, entry);
      return entry.value;
    },
    set(key, value, textLength) {
      const size = 2 * (key.length + textLength);
      if (size > RENDER_CACHE_BYTES) return;
      bytes -= entries.get(key)?.size ?? 0;
      entries.delete(key);
      entries.set(key, { value, size });
      bytes += size;
      while (entries.size > RENDER_CACHE_LIMIT || bytes > RENDER_CACHE_BYTES) {
        const oldest = entries.keys().next().value;
        bytes -= entries.get(oldest).size;
        entries.delete(oldest);
      }
    },
  };
}

function cachedRenderDocument(src, plainLanguageLabel) {
  const cacheKey = `${plainLanguageLabel}\u0000${src}`;
  const cached = renderCache.get(cacheKey);
  if (cached !== undefined) return cached;

  const document = renderDocument(src, plainLanguageLabel);
  renderCache.set(
    cacheKey,
    document,
    document.html.length +
      document.codeBlocks.reduce(
        (length, block) => length + block.text.length,
        0,
      ),
  );
  return document;
}

function renderDocument(src, plainLanguageLabel) {
  const codeBlocks = [];
  return {
    html: md.render(src, { codeBlocks, plainLanguageLabel }),
    codeBlocks,
  };
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

function plainTextCodeRanges(src) {
  const ranges = [];
  let openingMarker = null;

  for (const markerMatch of src.matchAll(/`+|~{3,}/g)) {
    const marker = markerMatch[0];
    if (!openingMarker) {
      openingMarker = {
        character: marker[0],
        length: marker.length,
        index: markerMatch.index,
      };
      continue;
    }

    if (
      marker[0] === openingMarker.character &&
      marker.length >= openingMarker.length
    ) {
      ranges.push({
        start: openingMarker.index,
        end: markerMatch.index + marker.length,
      });
      openingMarker = null;
    }
  }

  if (openingMarker) {
    ranges.push({ start: openingMarker.index, end: src.length });
  }
  return ranges;
}

export function linkifiedTextSegments(src) {
  if (!src) return [];

  const cached = linkifiedTextCache.get(src);
  if (cached !== undefined) return cached;

  const codeRanges = plainTextCodeRanges(src);
  const matches = (md.linkify.match(src) ?? []).filter(
    (match) =>
      !codeRanges.some(
        (range) => match.index >= range.start && match.index < range.end,
      ),
  );
  const segments = [];
  let cursor = 0;

  for (const match of matches) {
    if (match.index > cursor) {
      segments.push({ text: src.slice(cursor, match.index), href: null });
    }
    segments.push({ text: match.raw, href: match.url });
    cursor = match.lastIndex;
  }

  if (cursor < src.length) {
    segments.push({ text: src.slice(cursor), href: null });
  }

  linkifiedTextCache.set(
    src,
    segments,
    segments.reduce(
      (length, segment) =>
        length + segment.text.length + (segment.href?.length ?? 0),
      0,
    ),
  );
  return segments;
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
    return renderDocument(src, plainLanguageLabel);
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
    ? renderDocument(prefix, plainLanguageLabel)
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
