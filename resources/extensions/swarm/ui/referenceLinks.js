import { tooltip } from '../../../../webui/src/lib/tooltip.js';

// Participants cite a Board post as "#46" and a Wiki page as "w7". Rendered
// Markdown shows them as links, but never inside code, existing links or
// controls, and never as part of a longer word ("abc#4", "w3c", "a/w3").
const REFERENCE = /(?<![\w#/&])(?:#(\d+)|w(\d+))(?!\w)/g;
// Text without this cannot hold a reference.
const CANDIDATE = /[#w]\d/;
const SKIPPED = 'a, code, pre, kbd, samp, button, summary, textarea';
const MARKDOWN = '.msg-markdown';
const LINK = 'a[data-swarm-reference]';

// Visits the text that may hold a reference and leaves out what SKIPPED
// matches, with everything inside it.
const candidateText = (item) =>
  item.nodeType === Node.TEXT_NODE
    ? CANDIDATE.test(item.data)
      ? NodeFilter.FILTER_ACCEPT
      : NodeFilter.FILTER_SKIP
    : item.matches(SKIPPED)
      ? NodeFilter.FILTER_REJECT
      : NodeFilter.FILTER_SKIP;

/**
 * Link post and Wiki page numbers in the Markdown inside `node` while it is
 * mounted, including Markdown rendered into it later.
 *
 * `target(kind, number, text)` returns the link's href, or null when the
 * number names nothing; `kind` is 'post' or 'page' and `text` is the text node
 * holding the number. `describe(kind, number)` resolves to the link's tooltip
 * content; `loading(reference)` is the content shown until then. A link gets
 * its tooltip when the pointer or focus first reaches it.
 */
export function referenceLinks(node, { target, describe, loading }) {
  const hints = new Map();

  function createLink(reference, href, kind) {
    const link = document.createElement('a');
    link.href = href;
    link.textContent = reference;
    link.dataset.swarmReference = kind;
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
      fragment.append(createLink(match[0], href, kind));
      end = match.index + match[0].length;
    }
    if (!fragment) return;
    fragment.append(value.slice(end));
    text.replaceWith(fragment);
  }

  // Links the references in the rendered Markdown that `root` is part of or
  // holds.
  function decorate(root) {
    const texts = [];
    if (root.nodeType === Node.TEXT_NODE) {
      const parent = root.parentElement;
      if (
        CANDIDATE.test(root.data) &&
        parent?.closest(MARKDOWN) &&
        !parent.closest(SKIPPED)
      )
        texts.push(root);
    } else if (root.nodeType === Node.ELEMENT_NODE) {
      const scopes = root.closest(MARKDOWN)
        ? [root]
        : root.querySelectorAll(MARKDOWN);
      for (const scope of scopes) {
        if (scope.closest(SKIPPED)) continue;
        const walker = document.createTreeWalker(
          scope,
          NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
          candidateText,
        );
        for (let text = walker.nextNode(); text; text = walker.nextNode())
          texts.push(text);
      }
    }
    for (const text of texts) linkText(text);
  }

  // Re-rendered Markdown drops its links; a moved post keeps them.
  function release(removed) {
    if (!hints.size || removed.nodeType !== Node.ELEMENT_NODE) return;
    for (const [link, hint] of hints)
      if (removed.contains(link) && !node.contains(link)) {
        hint.destroy();
        hints.delete(link);
      }
  }

  // Runs before the link's own listeners, so its tooltip handles this first
  // hover or focus.
  function prepare(event) {
    const link = event.target.closest?.(LINK);
    if (!link || hints.has(link)) return;
    const reference = link.textContent;
    const hint = tooltip(link, loading(reference));
    hints.set(link, hint);
    void describe(link.dataset.swarmReference, Number(reference.slice(1))).then(
      (content) => {
        if (hints.get(link) === hint) hint.update(content);
      },
    );
  }

  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const removed of record.removedNodes) release(removed);
      for (const added of record.addedNodes)
        if (added.isConnected && node.contains(added)) decorate(added);
    }
    // The links just inserted are no Markdown to decorate.
    observer.takeRecords();
  });
  decorate(node);
  observer.observe(node, { childList: true, subtree: true });
  node.addEventListener('pointerover', prepare, true);
  node.addEventListener('focusin', prepare, true);
  return {
    destroy() {
      observer.disconnect();
      node.removeEventListener('pointerover', prepare, true);
      node.removeEventListener('focusin', prepare, true);
      for (const hint of hints.values()) hint.destroy();
      hints.clear();
    },
  };
}
