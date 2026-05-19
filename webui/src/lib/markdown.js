import markdownit from 'markdown-it';

const FENCE_PATTERN = /^[ \t]{0,3}```[^\n]*$/gm;

const md = markdownit({
  html: false,
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
  return md.render(src);
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
