// What the Chat timeline's polite live region announces. The scroller is not
// a live region itself: its window mounts old rows while the user scrolls,
// and History loads, older-History prepends, and Session switches add rows
// that are not news. Only content appended at the end of the displayed
// Session is announced, once it is final: a Run's answer when the Run has
// finished, Assistant and error messages when they appear, and new command
// output cards. Each Run is announced at most once, whatever row later shows
// it.
import {
  errorMessagePresentation,
  visibleRunChildren,
} from '$lib/chatTimelinePresentation.js';
import { t } from '$lib/i18n.js';
import { renderMarkdown } from '$lib/markdown.js';

const RUNNING = 'running';

function runKey(item) {
  return item.runId || item.id;
}

function plainText(markdown) {
  if (!markdown) {
    return '';
  }
  const template = document.createElement('template');
  template.innerHTML = renderMarkdown(markdown);
  return template.content.textContent.replace(/\s+/g, ' ').trim();
}

function runAnswerText(item) {
  return plainText(
    visibleRunChildren(item)
      .filter(
        (child) =>
          child.type === 'assistant_output' &&
          typeof child.content === 'string',
      )
      .map((child) => child.content.trim())
      .filter(Boolean)
      .join('\n\n'),
  );
}

function messageText(message) {
  const content = typeof message.content === 'string' ? message.content : '';
  if (message.role === 'assistant') {
    return plainText(content);
  }
  if (message.role === 'error') {
    const { summary } = errorMessagePresentation(content);
    return summary ? t('chat.announcement.error', { message: summary }) : '';
  }
  return '';
}

export function createTimelineAnnouncer() {
  let sessionKey = null;
  // Ids of the rows and command output cards seen in the displayed Session.
  let knownRows = new Set();
  let knownCards = new Set();
  // Keys of Runs appended while running; announced once they finish.
  const pendingRuns = new Set();
  const announcedRuns = new Set();

  function reset(key, items, cards) {
    sessionKey = key;
    knownRows = new Set(items.map((item) => item.id));
    knownCards = new Set(cards.map((card) => card.id));
    pendingRuns.clear();
    announcedRuns.clear();
    for (const item of items) {
      if (item.type === 'assistant_run' && item.status === RUNNING) {
        pendingRuns.add(runKey(item));
      }
    }
  }

  // The rows after the last already known row, or null when no row is known
  // (the whole list was replaced).
  function appendedRows(items) {
    let index = items.length;
    while (index > 0 && !knownRows.has(items[index - 1].id)) {
      index -= 1;
    }
    return index === 0 ? null : items.slice(index);
  }

  function announceRun(item, texts) {
    const key = runKey(item);
    if (announcedRuns.has(key)) {
      return;
    }
    if (item.status === RUNNING) {
      pendingRuns.add(key);
      return;
    }
    pendingRuns.delete(key);
    announcedRuns.add(key);
    const text = runAnswerText(item);
    if (text) {
      texts.push(text);
    }
  }

  return {
    // Takes the displayed Session's rows and command output cards after a
    // change; returns the text to announce, or '' for nothing new.
    update(items, cards, key, { loading = false } = {}) {
      if (key !== sessionKey || loading) {
        reset(key, items, cards);
        return '';
      }
      const appended = appendedRows(items);
      if (appended === null) {
        reset(key, items, cards);
        return '';
      }
      const texts = [];
      for (const item of appended) {
        knownRows.add(item.id);
        if (item.type === 'assistant_run') {
          announceRun(item, texts);
        } else if (item.type === 'message') {
          const text = messageText(item.message);
          if (text) {
            texts.push(text);
          }
        }
      }
      if (pendingRuns.size > 0) {
        for (const item of items) {
          if (
            item.type === 'assistant_run' &&
            item.status !== RUNNING &&
            pendingRuns.has(runKey(item))
          ) {
            announceRun(item, texts);
          }
        }
      }
      for (const card of cards) {
        if (!knownCards.has(card.id)) {
          knownCards.add(card.id);
          if (card.text) {
            texts.push(card.text);
          }
        }
      }
      return texts.join('\n\n');
    },
  };
}
