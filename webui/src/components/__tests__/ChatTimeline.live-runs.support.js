const { flushSync, mount, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');

function setupTimelineRunSuite() {
  let mountedComponent;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });
  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.useRealTimers();
  });
  return {
    get mountedComponent() {
      return mountedComponent;
    },
    set mountedComponent(value) {
      mountedComponent = value;
    },
  };
}

export { ChatTimeline, setupTimelineRunSuite };

export { flushSync, mount };
