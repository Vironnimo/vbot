// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';
import {
  flushSync as svelteFlushSync,
  mount,
  tick as svelteTick,
  unmount,
} from 'svelte';

import { createChatState, ensureSessionState } from '../../lib/chatState.js';
import { init, t } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('svelte/store', async () => {
  return import('../../../node_modules/svelte/src/store/index-client.js');
});

// Renders ChatTimeline from a `sessionState` prop, projecting it like
// ChatView.
const { default: ChatTimelineHost } =
  await import('./ChatTimelineHost.support.svelte');

// Registers the per-test lifecycle and returns the mount helpers. With
// `observeResize`, a ResizeObserver stub records callbacks that
// `notifyContentResize()` fires; without it jsdom has no ResizeObserver.
export function setupChatTimelineSuite({ observeResize = false } = {}) {
  let mountedComponent = null;
  let resizeCallbacks = [];

  async function unmountTimeline() {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
  }

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    resizeCallbacks = [];
    if (observeResize) {
      globalThis.ResizeObserver = class {
        constructor(callback) {
          resizeCallbacks.push(callback);
        }

        observe() {}

        unobserve() {}

        disconnect() {}
      };
    }
  });

  afterEach(async () => {
    await unmountTimeline();
    document.body.innerHTML = '';
    if (observeResize) delete globalThis.ResizeObserver;
    vi.useRealTimers();
  });

  return {
    // Mounts ChatTimeline for `props.sessionState` with the other `props` as
    // given (a plain object or a reactive props bag).
    mount(props) {
      mountedComponent = mount(ChatTimelineHost, {
        target: document.body,
        props,
      });
      flushSync();
      return mountedComponent;
    },
    // Mounts ChatTimeline for `sessionState` as the Agent "Alpha".
    render(sessionState, props = {}) {
      return this.mount({ sessionState, agentName: 'Alpha', ...props });
    },
    unmount: unmountTimeline,
    notifyContentResize() {
      for (const callback of resizeCallbacks) callback([]);
    },
    lastResizeCallback() {
      return resizeCallbacks.at(-1);
    },
  };
}

export function flushSync() {
  return svelteFlushSync();
}

export function openDisclosures(selector) {
  for (const disclosure of document.querySelectorAll(selector)) {
    disclosure.open = true;
    disclosure.dispatchEvent(new Event('toggle'));
  }
  flushSync();
}

export function tick() {
  return svelteTick();
}

export async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

export function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  });
  return writeText;
}

export function timelineSession(sessionId = 'session-test', chatState) {
  return ensureSessionState(chatState ?? createChatState(), 'alpha', sessionId);
}

// The Tool detail row labelled with the i18n `key` (`chat.toolArgs`,
// `chat.toolResultLabel`, ...).
export function detailRow(key) {
  return (
    Array.from(document.querySelectorAll('.teb-row')).find(
      (row) => row.querySelector('.teb-label')?.textContent === t(key),
    ) ?? null
  );
}

// A detail row's visible text: `key: value` lines for fields, else the code.
export function detailText(key) {
  const row = detailRow(key);
  const fields = Array.from(row?.querySelectorAll('.teb-field') ?? []);
  if (fields.length > 0) {
    return fields
      .map((field) => {
        const name = field.querySelector('.teb-field-key')?.textContent ?? '';
        const value =
          field.querySelector('.teb-field-value')?.textContent ?? '';
        return `${name}: ${value}`;
      })
      .join('\n');
  }
  return row?.querySelector('.teb-code')?.textContent ?? '';
}

// The rendered children of the first Assistant Run, without its footer.
export function runChildren() {
  return Array.from(
    document.querySelector('.assistant-run-content').children,
  ).filter((child) => !child.classList.contains('run-footer'));
}

export function occurrences(text) {
  return document.body.textContent.split(text).length - 1;
}

export function follows(first, second) {
  return Boolean(
    first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
  );
}

// jsdom has no layout: pin the container to a 2000px-tall content area in a
// 500px viewport and route scrollTo through the same writable scrollTop.
export function mockScrollGeometry(container) {
  let scrollTop = 0;
  let scrollHeight = 2000;
  Object.defineProperty(container, 'scrollHeight', {
    configurable: true,
    get: () => scrollHeight,
  });
  for (const property of ['offsetHeight', 'clientHeight']) {
    Object.defineProperty(container, property, {
      configurable: true,
      get: () => 500,
    });
  }
  Object.defineProperty(container, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (value) => {
      scrollTop = value;
    },
  });
  container.scrollTo = (x, y) => {
    scrollTop = typeof x === 'object' ? x.top : y;
  };
  return {
    setScrollTop: (value) => {
      scrollTop = value;
    },
    setScrollHeight: (value) => {
      scrollHeight = value;
    },
    currentScrollTop: () => scrollTop,
  };
}

export async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await tick();
    await Promise.resolve();
    flushSync();
    if (check()) return;
    if (typeof requestAnimationFrame === 'function') {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      flushSync();
      if (check()) return;
    }
  }
  throw new Error('Timed out waiting for condition.');
}
