// @vitest-environment jsdom

import { afterEach, beforeEach, describe, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');
const { reactiveProps } = await import('./reactiveProps.svelte.js');
import {
  mockScrollGeometry,
  scrollMemorySessions,
  waitForCondition,
} from './ChatTimeline.support.js';

describe('ChatTimeline', () => {
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

  it('restores the saved scroll position when returning to a previously viewed session', async () => {
    const { parentSession, childSession } = scrollMemorySessions();
    const props = reactiveProps({
      sessionState: parentSession,
      agentName: 'Alpha',
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const { setScrollTop, currentScrollTop } = mockScrollGeometry(
      document.querySelector('.messages'),
    );
    // Let the mount-time restore settle before simulating a user scroll.
    await waitForCondition(() => true);

    setScrollTop(700);

    props.sessionState = childSession;
    flushSync();
    // First view of the child session starts at the bottom.
    await waitForCondition(() => currentScrollTop() === 2000);

    props.sessionState = parentSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 700);
  });

  it('returns to the bottom of a session the user left at the bottom', async () => {
    const { parentSession, childSession } = scrollMemorySessions();
    const props = reactiveProps({
      sessionState: parentSession,
      agentName: 'Alpha',
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const { setScrollTop, currentScrollTop } = mockScrollGeometry(
      document.querySelector('.messages'),
    );
    await waitForCondition(() => true);

    // Near the bottom (within the 56px stick-to-bottom threshold).
    setScrollTop(1980);

    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);
    setScrollTop(300);

    props.sessionState = parentSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);

    // And the child session's mid-position survived the round trip too.
    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 300);
  });

  it('re-asserts a restored mid-history position against content turbulence after the switch', async () => {
    const { parentSession, childSession } = scrollMemorySessions();
    const props = reactiveProps({
      sessionState: parentSession,
      agentName: 'Alpha',
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const { setScrollTop, currentScrollTop } = mockScrollGeometry(
      document.querySelector('.messages'),
    );
    await waitForCondition(() => true);

    setScrollTop(700);

    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);

    props.sessionState = parentSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 700);

    // Post-return turbulence (history reload, browser re-clamp, late content)
    // moves the view near the bottom without any user input; the next content
    // change must re-assert the restored position instead of letting the
    // stick-to-bottom autoscroll take over from the stolen position.
    setScrollTop(1980);
    props.sessionState.messages = [
      ...props.sessionState.messages,
      {
        id: 'parent-assistant-two',
        role: 'assistant',
        content: 'Late parent answer',
        timestamp: '2026-05-10T09:05:00',
      },
    ];
    flushSync();
    await waitForCondition(() => currentScrollTop() === 700);
  });

  it('hands scroll ownership back to stick-to-bottom once the user scrolls', async () => {
    const { parentSession, childSession } = scrollMemorySessions();
    const props = reactiveProps({
      sessionState: parentSession,
      agentName: 'Alpha',
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { setScrollTop, currentScrollTop } = mockScrollGeometry(container);
    await waitForCondition(() => true);

    setScrollTop(700);

    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);

    props.sessionState = parentSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 700);

    // Real user scroll input releases the pin; from a near-bottom position
    // the stick-to-bottom behavior then follows new content again.
    container.dispatchEvent(new Event('wheel'));
    setScrollTop(1980);
    container.dispatchEvent(new Event('scroll'));
    props.sessionState.messages = [
      ...props.sessionState.messages,
      {
        id: 'parent-assistant-two',
        role: 'assistant',
        content: 'Late parent answer',
        timestamp: '2026-05-10T09:05:00',
      },
    ];
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);
  });

  it('follows real content growth immediately while follow mode owns the viewport', async () => {
    const { parentSession } = scrollMemorySessions();
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState: parentSession, agentName: 'Alpha' },
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { currentScrollTop, setScrollHeight } = mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    setScrollHeight(2400);
    notifyContentResize();

    await waitForCondition(() => currentScrollTop() === 2400);
  });

  it('keeps a user-owned reading position stable while content grows', async () => {
    const { parentSession } = scrollMemorySessions();
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState: parentSession, agentName: 'Alpha' },
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { currentScrollTop, setScrollHeight, setScrollTop } =
      mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    container.dispatchEvent(new Event('wheel'));
    setScrollTop(600);
    container.dispatchEvent(new Event('scroll'));
    setScrollHeight(2400);
    notifyContentResize();

    await waitForCondition(() => currentScrollTop() === 600);
  });

  it('resumes following when the user returns to the bottom', async () => {
    const { parentSession } = scrollMemorySessions();
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState: parentSession, agentName: 'Alpha' },
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { currentScrollTop, setScrollHeight, setScrollTop } =
      mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    container.dispatchEvent(new Event('wheel'));
    setScrollTop(500);
    container.dispatchEvent(new Event('scroll'));
    container.dispatchEvent(new Event('wheel'));
    setScrollTop(1980);
    container.dispatchEvent(new Event('scroll'));
    setScrollHeight(2300);
    notifyContentResize();

    await waitForCondition(() => currentScrollTop() === 2300);
  });

  it('ignores an older-history restore after switching sessions', async () => {
    const { parentSession, childSession } = scrollMemorySessions();
    let resolveOlder;
    const olderLoaded = new Promise((resolve) => {
      resolveOlder = resolve;
    });
    const props = reactiveProps({
      sessionState: parentSession,
      agentName: 'Alpha',
      hasOlderHistory: true,
      onLoadOlder: () => olderLoaded,
    });
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { currentScrollTop, setScrollTop } = mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    container.dispatchEvent(new Event('wheel'));
    setScrollTop(0);
    container.dispatchEvent(new Event('scroll'));
    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);

    resolveOlder(true);
    await waitForCondition(() => currentScrollTop() === 2000);
  });
});
