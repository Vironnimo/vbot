const { flushSync, mount, tick, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');

function setupTimelineLayoutSuite() {
  let mountedComponent;
  let resizeCallbacks;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    resizeCallbacks = [];
    globalThis.ResizeObserver = class {
      constructor(callback) {
        resizeCallbacks.push(callback);
      }

      observe() {}

      disconnect() {}
    };
  });
  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    delete globalThis.ResizeObserver;
    vi.useRealTimers();
  });
  function notifyContentResize() {
    for (const callback of resizeCallbacks) {
      callback([]);
    }
  }
  return {
    get mountedComponent() {
      return mountedComponent;
    },
    set mountedComponent(value) {
      mountedComponent = value;
    },
    notifyContentResize,
  };
}

export { ChatTimeline, setupTimelineLayoutSuite };

export { flushSync, mount, tick };
