// A page declares local destinations. Only a user-activated, registered href
// crosses back to that page; matching and Markdown rendering stay in the host.
const shortText = (s) =>
  typeof s === 'string' &&
  s.length > 0 &&
  s.length <= 256 &&
  !/[\p{C}]/u.test(s);
const escaped = (s) => s.replace(/[.*+?$^{}()|[\]\\]/g, '\\$&');
const identifier = (s) => shortText(s) && !/[/#?\\]/.test(s);
const skipped = 'a, code, pre, kbd, samp, button, summary, textarea, .msg-code';
const word = '[\\p{L}\\p{N}_]';

function compile(rule) {
  if (
    !rule ||
    !shortText(rule.label) ||
    typeof rule.prefix !== 'string' ||
    !/^[A-Za-z@#~!+]{1,8}$/.test(rule.prefix) ||
    typeof rule.fragment !== 'string' ||
    !/^#[A-Za-z][A-Za-z0-9_/-]{0,63}$/.test(rule.fragment)
  )
    return null;
  const { prefix, fragment, label } = rule;
  if (Array.isArray(rule.names)) {
    if (
      rule.names.length > 512 ||
      !rule.names.every(
        (pair) =>
          Array.isArray(pair) &&
          pair.length === 2 &&
          shortText(pair[0]) &&
          identifier(pair[1]),
      )
    )
      return null;
    const ids = new Map(
      rule.names.map(([name, id]) => [
        name.toLowerCase(),
        encodeURIComponent(id),
      ]),
    );
    if (!ids.size) return null;
    const targets = new Set(ids.values());
    const expr = [...ids.keys()]
      .sort((a, b) => b.length - a.length)
      .map(escaped)
      .join('|');
    return {
      fragment,
      label,
      pattern: new RegExp(
        `(?<!${word})${escaped(prefix)}(${expr})(?!${word})`,
        'giu',
      ),
      resolve: (name) => ids.get(name.toLowerCase()),
      target: (suffix) => (targets.has(suffix) ? suffix : null),
    };
  }
  const { first, last } = rule;
  if (
    !Number.isSafeInteger(first) ||
    !Number.isSafeInteger(last) ||
    first < 0 ||
    last < first
  )
    return null;
  const target = (digits) => {
    const n = Number(digits);
    return /^\d+$/.test(digits) && n >= first && n <= last ? String(n) : null;
  };
  return {
    fragment,
    label,
    pattern: new RegExp(
      '(?<![\\p{L}\\p{N}_#/&])' + escaped(prefix) + '(\\d+)(?!' + word + ')',
      'gu',
    ),
    resolve: target,
    target,
  };
}

export function compileSessionReferences(input, open) {
  if (!Array.isArray(input) || !input.length || input.length > 8) return null;
  const rules = input.map(compile).filter(Boolean);
  if (!rules.length) return null;

  function annotate(link, rule, href) {
    link.setAttribute('href', href);
    link.removeAttribute('target');
    link.dataset.sessionReference = rule.label;
  }

  function linkText(node) {
    const value = node.data;
    const matches = [];
    for (const rule of rules) {
      for (const match of value.matchAll(rule.pattern)) {
        const end = match.index + match[0].length;
        if (value[match.index - 1] === '"' && value[end] === '"') continue;
        const id = rule.resolve(match[1]);
        if (id != null)
          matches.push({
            at: match.index,
            text: match[0],
            href: rule.fragment + id,
            rule,
          });
      }
    }
    if (!matches.length) return;
    const fragment = node.ownerDocument.createDocumentFragment();
    let end = 0;
    for (const hit of matches.sort(
      (a, b) => a.at - b.at || b.text.length - a.text.length,
    )) {
      if (hit.at < end) continue;
      const link = node.ownerDocument.createElement('a');
      link.textContent = hit.text;
      annotate(link, hit.rule, hit.href);
      fragment.append(value.slice(end, hit.at), link);
      end = hit.at + hit.text.length;
    }
    fragment.append(value.slice(end));
    node.replaceWith(fragment);
  }

  function destination(href) {
    for (const rule of rules) {
      if (!href.startsWith(rule.fragment)) continue;
      const id = rule.target(href.slice(rule.fragment.length));
      if (id !== null) return { rule, href: rule.fragment + id };
    }
    return null;
  }

  return {
    open(href) {
      const match = typeof href === 'string' && destination(href);
      if (match) open(match.href);
    },
    decorate(fragment) {
      // An explicit local Markdown destination takes precedence over its text.
      for (const link of fragment.querySelectorAll('a[href^="#"]')) {
        const href = link.getAttribute('href');
        const match = destination(href);
        if (match) annotate(link, match.rule, match.href);
      }
      const walker = fragment.ownerDocument.createTreeWalker(
        fragment,
        NodeFilter.SHOW_TEXT,
      );
      const texts = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode())
        if (!node.parentElement?.closest(skipped)) texts.push(node);
      for (const node of texts) linkText(node);
    },
  };
}
