import { tooltip } from '../../../../webui/src/lib/tooltip.js';

// Participants cite a Board post as "#46" and a Wiki page as "w7". Rendered
// Markdown shows them as links, but never inside code, existing links or
// controls, and never as part of a longer word ("abc#4", "w3c", "a/w3").
const REFERENCE = /(?<![\w#/&])(?:#(\d+)|w(\d+))(?!\w)/g;
const SKIPPED = 'a, code, pre, kbd, samp, button, summary, textarea';
const MARKDOWN = '.msg-markdown';

/**
 * Link post and Wiki page numbers in the Markdown inside `node` while it is
 * mounted, including Markdown rendered into it later.
 *
 * `target(kind, number, text)` returns the link's href, or null when the
 * number names nothing; `kind` is 'post' or 'page' and `text` is the text node
 * holding the number. `describe(kind, number)` resolves to the link's tooltip
 * content; `loading(reference)` is the content shown until then.
 */
export function referenceLinks(node, { target, describe, loading }) {
  const hints = new Map();

  function createLink(reference, href, kind, number) {
    const link = document.createElement('a');
    link.href = href;
    link.textContent = reference;
    link.dataset.swarmReference = kind;
    const hint = tooltip(link, loading(reference));
    hints.set(link, hint);
    const load = () => {
      link.removeEventListener('pointerenter', load);
      link.removeEventListener('focusin', load);
      void describe(kind, number).then((content) => {
        if (hints.get(link) === hint) hint.update(content);
      });
    };
    link.addEventListener('pointerenter', load);
    link.addEventListener('focusin', load);
    return link;
  }

  function linkText(text) {
    const value = text.data;
    let fragment = null;
    let end = 0;
    for (const match of value.matchAll(REFERENCE)) {
      const kind = match[1] === undefined ? 'page' : 'post';
      const number = Number(match[1] ?? match[2]);
      const href = target(kind, number, text);
      if (!href) continue;
      fragment ??= document.createDocumentFragment();
      fragment.append(value.slice(end, match.index));
      fragment.append(createLink(match[0], href, kind, number));
      end = match.index + match[0].length;
    }
    if (!fragment) return;
    fragment.append(value.slice(end));
    text.replaceWith(fragment);
  }

  function decorate(scope) {
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    const texts = [];
    for (let text = walker.nextNode(); text; text = walker.nextNode())
      if (
        text.parentElement?.closest(MARKDOWN) &&
        !text.parentElement.closest(SKIPPED)
      )
        texts.push(text);
    for (const text of texts) linkText(text);
  }

  function release() {
    // Re-rendered Markdown drops its links; a moved post keeps them.
    for (const [link, hint] of hints)
      if (!node.contains(link)) {
        hint.destroy();
        hints.delete(link);
      }
  }

  const observer = new MutationObserver((records) => {
    release();
    for (const record of records)
      for (const added of record.addedNodes) {
        const scope =
          added.nodeType === Node.TEXT_NODE ? added.parentElement : added;
        if (scope instanceof Element && node.contains(scope)) decorate(scope);
      }
  });
  decorate(node);
  observer.observe(node, { childList: true, subtree: true });
  return {
    destroy() {
      observer.disconnect();
      for (const hint of hints.values()) hint.destroy();
      hints.clear();
    },
  };
}
