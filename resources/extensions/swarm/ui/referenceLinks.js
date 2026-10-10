import { tooltip } from '../../../../webui/src/lib/tooltip.js';

// Participants cite a Board post as "#46" and a Wiki page as "w7", and address
// a participant as "@Name". Rendered Markdown shows them as links, but never
// inside code, existing links or controls, never as part of a longer word
// ("abc#4", "w3c", "a/w3", "me@Ella"), and never as a value in double quotes,
// as in a Tool call's arguments ('"message_id": "#0"').
const REFERENCE = /(?<![\w#/&])(?:#(\d+)|w(\d+))(?!\w)(?<!"[#w]\d+(?="))/g;
// Text without this cannot hold a reference or a mention.
const CANDIDATE = /[#w]\d|@/;
const SKIPPED = 'a, code, pre, kbd, samp, button, summary, textarea';
const MARKDOWN = '.msg-markdown';
// A link to a post, a Wiki page or a participant: the ones this module
// inserts and the same links written in Markdown.
const LINK = 'a[href^="#post/"], a[href^="#wiki/w"], a[href^="#participant/"]';
// A letter, digit or underscore in any script, as the Store reads a mention.
const WORD = '[\\p{L}\\p{N}_]';

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

const escaped = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// What a link names: a post or page number, or a participant id.
function linkTarget(link) {
  const href = link.getAttribute('href') ?? '';
  const post = /^#post\/(\d+)$/.exec(href);
  if (post) return { kind: 'post', key: Number(post[1]) };
  const page = /^#wiki\/w(\d+)$/.exec(href);
  if (page) return { kind: 'page', key: Number(page[1]) };
  const participant = /^#participant\/(.+)$/.exec(href);
  try {
    return participant
      ? { kind: 'participant', key: decodeURIComponent(participant[1]) }
      : null;
  } catch {
    return null;
  }
}

/**
 * Link post and Wiki page numbers and participant mentions in the Markdown
 * inside `node` while it is mounted, including Markdown rendered into it
 * later, and give every link to a post, page or participant inside `node` a
 * tooltip.
 *
 * `target(kind, number, text)` returns a number's href, or null when it names
 * nothing; `kind` is 'post' or 'page' and `text` is the text node holding the
 * number. `participants()` returns the participants a mention may name
 * (`{id, display_name}`): "@" before a display name or id, in any case, the
 * longest name first, as the Store reads mentions. `describe(kind, key)`
 * returns a link's tooltip content, or a promise of it for a post or page
 * (`key` is the number) with `loading(kind, key)` shown until then. A link
 * gets its tooltip when the pointer or focus first reaches it.
 */
export function referenceLinks(
  node,
  { target, participants = () => [], describe, loading },
) {
  const hints = new Map();
  // The mention pattern of the last participant list, and the participant
  // each lowercased name or id names.
  let mentionList = null;
  let mention = null;
  const mentioned = new Map();

  function mentionPattern() {
    const list = participants();
    if (list === mentionList) return mention;
    mentionList = list;
    mentioned.clear();
    for (const participant of list)
      if (participant.display_name)
        mentioned.set(participant.display_name.toLowerCase(), participant.id);
    for (const participant of list)
      mentioned.set(participant.id.toLowerCase(), participant.id);
    const names = [...mentioned.keys()].sort((a, b) => b.length - a.length);
    mention = names.length
      ? new RegExp(
          `(?<!${WORD})@(${names.map(escaped).join('|')})(?!${WORD})`,
          'giu',
        )
      : null;
    return mention;
  }

  function createLink(text, href, kind) {
    const link = document.createElement('a');
    link.href = href;
    link.textContent = text;
    link.dataset.swarmReference = kind;
    return link;
  }

  // The references and mentions in `value`, by position.
  function linksIn(value, text) {
    const found = [];
    for (const match of value.matchAll(REFERENCE)) {
      const kind = match[1] === undefined ? 'page' : 'post';
      const href = target(kind, Number(match[1] ?? match[2]), text);
      if (href) found.push({ index: match.index, text: match[0], href, kind });
    }
    const pattern = value.includes('@') ? mentionPattern() : null;
    if (pattern)
      for (const match of value.matchAll(pattern)) {
        const id = mentioned.get(match[1].toLowerCase());
        if (id)
          found.push({
            index: match.index,
            text: match[0],
            href: `#participant/${encodeURIComponent(id)}`,
            kind: 'participant',
          });
      }
    return found.sort((a, b) => a.index - b.index);
  }

  function linkText(text) {
    const value = text.data;
    const found = linksIn(value, text);
    if (!found.length) return;
    const fragment = document.createDocumentFragment();
    let end = 0;
    for (const item of found) {
      if (item.index < end) continue;
      fragment.append(value.slice(end, item.index));
      fragment.append(createLink(item.text, item.href, item.kind));
      end = item.index + item.text.length;
    }
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
    const named = linkTarget(link);
    if (!named) return;
    const content = describe(named.kind, named.key);
    if (typeof content?.then !== 'function') {
      hints.set(link, tooltip(link, content));
      return;
    }
    const hint = tooltip(link, loading(named.kind, named.key));
    hints.set(link, hint);
    void content.then((described) => {
      if (hints.get(link) === hint) hint.update(described);
    });
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
