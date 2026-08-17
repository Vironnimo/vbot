// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');
const { reactiveProps } = await import('./reactiveProps.svelte.js');
import { createChatState, ensureSessionState } from '../../lib/chatState.js';
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

  it('keeps the active restore when a stale session resize arrives during a rapid switch', async () => {
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
    const { currentScrollTop, setScrollTop } = mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);
    const staleChildResize = resizeCallbacks.at(-1);

    container.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
    setScrollTop(400);
    container.dispatchEvent(new Event('scroll'));

    props.sessionState = parentSession;
    flushSync();
    await tick();
    staleChildResize([]);

    await waitForCondition(() => currentScrollTop() === 2000);
  });

  it('releases follow mode before upward wheel scrolling can race content growth', async () => {
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

    // Tool output may resize between the wheel input and the browser's scroll
    // event. The upward intent must already own the viewport at that point.
    container.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
    setScrollHeight(2400);
    notifyContentResize();
    await waitForCondition(() => currentScrollTop() === 2000);

    setScrollTop(600);
    container.dispatchEvent(new Event('scroll'));
    setScrollHeight(2600);
    notifyContentResize();
    await waitForCondition(() => currentScrollTop() === 600);
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

  it('offers a floating jump control while reading and resumes following after activation', async () => {
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
    expect(document.querySelector('[aria-label="Jump to latest"]')).toBeNull();

    container.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
    setScrollTop(600);
    container.dispatchEvent(new Event('scroll'));
    flushSync();

    const jumpButton = document.querySelector('[aria-label="Jump to latest"]');
    expect(jumpButton).toBeTruthy();
    expect(jumpButton.classList.contains('chat-timeline__jump-latest')).toBe(
      true,
    );

    jumpButton.click();
    await waitForCondition(
      () =>
        currentScrollTop() === 2000 &&
        document.querySelector('[aria-label="Jump to latest"]') === null,
    );

    setScrollHeight(2400);
    notifyContentResize();
    await waitForCondition(() => currentScrollTop() === 2400);
  });

  it('starts every explicit Sub-Agent link visit at the bottom and follows new output', async () => {
    const { parentSession, childSession } = scrollMemorySessions();
    const props = reactiveProps({
      sessionState: childSession,
      agentName: 'Subagent',
      followSessionRequest: {
        requestId: 1,
        sessionKey: childSession.key,
      },
    });
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { currentScrollTop, setScrollHeight, setScrollTop } =
      mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    container.dispatchEvent(new Event('wheel'));
    setScrollTop(300);
    container.dispatchEvent(new Event('scroll'));
    props.sessionState = parentSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);

    props.followSessionRequest = {
      requestId: 2,
      sessionKey: childSession.key,
    };
    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);

    setScrollHeight(2400);
    notifyContentResize();
    await waitForCondition(() => currentScrollTop() === 2400);
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

  // Regression: during app start / initial session load, the History has not
  // arrived yet, so the timeline is empty or only as tall as the loading
  // banner. A naive `scrollTop = scrollHeight` flush collapses to 0 — the top.
  // The follow flush must skip while content does not exceed the viewport and
  // re-assert to the bottom once real content arrives.
  it('lands at the bottom, not the top, when content arrives after mount', async () => {
    const chatState = createChatState();
    const session = ensureSessionState(
      chatState,
      'alpha',
      'session-initial-load-bottom',
    );
    session.historyLoaded = false;
    const props = reactiveProps({
      sessionState: session,
      agentName: 'Alpha',
      loadingHistory: true,
    });
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { currentScrollTop, setScrollHeight } = mockScrollGeometry(container);
    // Empty timeline: scrollHeight equals offsetHeight (500). The follow flush
    // must not collapse to scrollTop = 500 (which clamps to 0 / top).
    await waitForCondition(() => true);
    expect(currentScrollTop()).toBe(0);

    // History arrives: content now exceeds the viewport.
    session.historyLoaded = true;
    session.messages = [
      {
        id: 'initial-user',
        role: 'user',
        content: 'First message',
        timestamp: '2026-05-10T09:00:00',
      },
      {
        id: 'initial-assistant',
        role: 'assistant',
        content: 'First answer',
        timestamp: '2026-05-10T09:01:00',
      },
    ];
    setScrollHeight(2000);
    flushSync();
    notifyContentResize();

    await waitForCondition(() => currentScrollTop() === 2000);
  });

  // Regression: when a previously saved reading anchor can no longer be found
  // (content structurally changed, different history page, reload), the
  // viewport must fall to the bottom (follow) rather than reusing a stale
  // absolute-pixel fallback that lands mid-text after content height changes.
  it('falls to the bottom when a saved reading anchor no longer exists', async () => {
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
    const { setScrollTop, currentScrollTop, setScrollHeight } =
      mockScrollGeometry(container);
    await waitForCondition(() => true);

    // Scroll up to a mid-history reading position, then leave.
    setScrollTop(700);
    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);

    // Back to the parent: the saved anchor exists, so it restores to 700.
    props.sessionState = parentSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 700);

    // Now mutate the parent content so the previously captured anchor id is
    // gone, and grow the content. Returning from the child must not land at
    // the stale pixel fallback (700); it must fall to the bottom (2600).
    props.sessionState.messages = [
      {
        id: 'parent-user-replaced',
        role: 'user',
        content: 'Replaced question',
        timestamp: '2026-05-10T10:00:00',
      },
      {
        id: 'parent-assistant-replaced',
        role: 'assistant',
        content: 'Replaced answer',
        timestamp: '2026-05-10T10:01:00',
      },
    ];
    flushSync();
    props.sessionState = childSession;
    flushSync();
    await waitForCondition(() => currentScrollTop() === 2000);
    // Grow the parent content so the saved fallbackScrollHeight no longer
    // matches — the stale-pixel fallback path is now unsafe and must be
    // skipped in favor of the bottom.
    setScrollHeight(2600);
    props.sessionState = parentSession;
    flushSync();
    // The saved anchor id is gone and content height changed, so the restore
    // must fall to the bottom (follow), not the stale pixel fallback (700).
    await waitForCondition(() => currentScrollTop() === 2600);
  });

  // Regression: a programmatic scroll to the top during a session switch must
  // not permanently pin the viewport into reading mode via the older-history
  // load path. Only genuine user scroll intent may trigger that transition.
  it('does not pin reading mode when a programmatic scroll reaches the top during a switch', async () => {
    const { parentSession, childSession } = scrollMemorySessions();
    const props = reactiveProps({
      sessionState: parentSession,
      agentName: 'Alpha',
      hasOlderHistory: true,
    });
    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props,
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { setScrollTop, currentScrollTop, setScrollHeight } =
      mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    // Leave the parent at the bottom (follow). Switch to the child; during the
    // transition the container briefly reports scrollTop 0 without any user
    // input event. The viewport must stay follow, not reading.
    props.sessionState = childSession;
    flushSync();
    setScrollTop(0);
    container.dispatchEvent(new Event('scroll'));
    await waitForCondition(() => currentScrollTop() === 2000);

    // New content arrives in the child; follow mode must track it to the
    // bottom. If the programmatic top-scroll had pinned reading, the viewport
    // would have stayed at 0 instead.
    childSession.messages = [
      ...childSession.messages,
      {
        id: 'child-assistant-late',
        role: 'assistant',
        content: 'Late child answer',
        timestamp: '2026-05-10T09:03:00',
      },
    ];
    setScrollHeight(2400);
    flushSync();
    notifyContentResize();
    await waitForCondition(() => currentScrollTop() === 2400);
  });
});
