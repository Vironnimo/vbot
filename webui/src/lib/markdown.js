import markdownit from 'markdown-it';

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

export function renderMarkdown(src) {
  if (!src) return '';
  return md.render(src);
}
