// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  appendEvents,
  flushSync,
  mockScrollGeometry,
  setupChatTimelineSuite,
  tick,
  timelineSession,
  userPersisted,
  waitForCondition,
} from './ChatTimeline.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { createChatState } from '../../lib/chatState.js';
import { t } from '../../lib/i18n.js';

// A parent Session and a Sub-Agent Session from the same chat state.
function scrollMemorySessions() {
  const chatState = createChatState();
  const parentSession = timelineSession('session-parent', chatState);
  parentSession.messages = [
    {
      id: 'parent-user-one',
      role: 'user',
      content: 'Parent question',
      timestamp: '2026-05-10T09:00:00',
    },
    {
      id: 'parent-assistant-one',
      role: 'assistant',
      content: 'Parent answer',
      timestamp: '2026-05-10T09:01:00',
    },
  ];
  const childSession = timelineSession('session-child', chatState);
  childSession.messages = [
    {
      id: 'child-user-one',
      role: 'user',
      content: 'Child task',
      timestamp: '2026-05-10T09:02:00',
    },
  ];
  return { parentSession, childSession };
}

function lateMessage(id) {
  return {
    id,
    role: 'assistant',
    content: 'Late answer',
    timestamp: '2026-05-10T09:05:00',
  };
}

function scrollAsUser(
  container,
  geometry,
  scrollTop,
  event = new Event('wheel'),
) {
  container.dispatchEvent(event);
  geometry.setScrollTop(scrollTop);
  container.dispatchEvent(new Event('scroll'));
}

function jumpButton() {
  return document.querySelector(`[aria-label="${t('chat.jumpToLatest')}"]`);
}

describe('ChatTimeline scrolling', () => {
  const timeline = setupChatTimelineSuite({ observeResize: true });

  // Mounts the parent Session with switchable props and waits for the view to
  // settle at the bottom.
  async function mountSessions(props = {}, sessions = scrollMemorySessions()) {
    const bag = reactiveProps({
      sessionState: sessions.parentSession,
      agentName: 'Alpha',
      ...props,
    });
    timeline.mount(bag);
    const container = document.querySelector('.messages');
    const geometry = mockScrollGeometry(container);
    await waitForCondition(() => geometry.currentScrollTop() === 2000);
    return { ...sessions, props: bag, container, geometry };
  }

  async function switchTo(view, sessionState, scrollTop) {
    view.props.sessionState = sessionState;
    flushSync();
    await waitForCondition(
      () => view.geometry.currentScrollTop() === scrollTop,
    );
  }

  // Leaves the parent at 700, visits the child (which starts at the bottom),
  // and returns to the restored parent position.
  async function returnToParentAt700(view) {
    view.geometry.setScrollTop(700);
    await switchTo(view, view.childSession, 2000);
    await switchTo(view, view.parentSession, 700);
  }

  it('reports a scrollbar that appears after the timeline mounts', async () => {
    const onScrollbarWidthChange = vi.fn();
    timeline.render(timelineSession(), { onScrollbarWidthChange });

    const container = document.querySelector('.messages');
    Object.defineProperty(container, 'offsetWidth', {
      configurable: true,
      get: () => 1000,
    });
    Object.defineProperty(container, 'clientWidth', {
      configurable: true,
      get: () => 986,
    });
    timeline.notifyContentResize();
    await tick();

    expect(onScrollbarWidthChange).toHaveBeenLastCalledWith(14);
  });

  it.each([
    ['an existing Session', () => scrollMemorySessions().parentSession, {}],
    [
      'a submitted turn',
      () => {
        const sessionState = timelineSession();
        appendEvents(sessionState, 'run-submitted', [
          userPersisted('user-submitted', 'Fresh turn'),
        ]);
        return sessionState;
      },
      { submittedTurnScrollKey: 1 },
    ],
  ])(
    'pins %s to the bottom and follows new content',
    async (_case, createSession, props) => {
      timeline.render(createSession(), props);
      const geometry = mockScrollGeometry(document.querySelector('.messages'));
      await waitForCondition(() => geometry.currentScrollTop() === 2000);

      geometry.setScrollHeight(2400);
      timeline.notifyContentResize();
      await waitForCondition(() => geometry.currentScrollTop() === 2400);
    },
  );

  it('loads older History at the top and preserves the scroll anchor', async () => {
    let scrollHeight = 1000;
    const onLoadOlder = vi.fn(async () => {
      scrollHeight = 1400;
      return true;
    });
    const sessionState = timelineSession();
    sessionState.messages = [
      {
        id: 'message-older-boundary',
        role: 'user',
        content: 'Oldest loaded message',
        timestamp: '2026-05-10T09:00:00',
      },
    ];
    timeline.render(sessionState, { hasOlderHistory: true, onLoadOlder });

    const messages = document.querySelector('.messages');
    Object.defineProperty(messages, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    });
    Object.defineProperty(messages, 'offsetHeight', {
      configurable: true,
      get: () => 500,
    });
    Object.defineProperty(messages, 'scrollTop', {
      configurable: true,
      writable: true,
      value: 0,
    });
    await tick();
    messages.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
    messages.dispatchEvent(new Event('scroll'));

    await waitForCondition(
      () => onLoadOlder.mock.calls.length === 1 && messages.scrollTop === 400,
    );
  });

  it('returns to the bottom of a Session the user left at the bottom', async () => {
    const view = await mountSessions();

    // Near the bottom (within the 56px stick-to-bottom threshold).
    view.geometry.setScrollTop(1980);
    await switchTo(view, view.childSession, 2000);
    view.geometry.setScrollTop(300);
    await switchTo(view, view.parentSession, 2000);
    // The child Session's mid-position survived the round trip too.
    await switchTo(view, view.childSession, 300);
  });

  it('re-asserts a restored mid-History position against content turbulence', async () => {
    const view = await mountSessions();
    await returnToParentAt700(view);

    // Post-return turbulence (History reload, browser re-clamp, late content)
    // moves the view near the bottom without user input; the next content
    // change re-asserts the restored position instead of following.
    view.geometry.setScrollTop(1980);
    view.props.sessionState.messages = [
      ...view.props.sessionState.messages,
      lateMessage('parent-late'),
    ];
    flushSync();
    await waitForCondition(() => view.geometry.currentScrollTop() === 700);
  });

  it('hands scroll ownership back to stick-to-bottom once the user scrolls', async () => {
    const view = await mountSessions();
    await returnToParentAt700(view);

    // Real user scroll input releases the pin; from near the bottom the view
    // follows new content again.
    scrollAsUser(view.container, view.geometry, 1980);
    view.props.sessionState.messages = [
      ...view.props.sessionState.messages,
      lateMessage('parent-late'),
    ];
    flushSync();
    await waitForCondition(() => view.geometry.currentScrollTop() === 2000);
  });

  // When the saved reading anchor is gone and the content height changed, the
  // stale pixel fallback would land mid-text; the view falls to the bottom.
  it('falls to the bottom when a saved reading anchor no longer exists', async () => {
    const view = await mountSessions();
    await returnToParentAt700(view);

    view.props.sessionState.messages = [
      {
        id: 'parent-user-replaced',
        role: 'user',
        content: 'Replaced question',
        timestamp: '2026-05-10T10:00:00',
      },
    ];
    flushSync();
    await switchTo(view, view.childSession, 2000);
    view.geometry.setScrollHeight(2600);
    await switchTo(view, view.parentSession, 2600);
  });

  it('keeps the active restore when a stale Session resize arrives during a rapid switch', async () => {
    const view = await mountSessions();
    await switchTo(view, view.childSession, 2000);
    const staleChildResize = timeline.lastResizeCallback();

    scrollAsUser(
      view.container,
      view.geometry,
      400,
      new WheelEvent('wheel', { deltaY: -120 }),
    );
    view.props.sessionState = view.parentSession;
    flushSync();
    await tick();
    staleChildResize([]);

    await waitForCondition(() => view.geometry.currentScrollTop() === 2000);
  });

  it('releases follow mode before upward wheel scrolling can race content growth', async () => {
    const view = await mountSessions();

    // Tool output may resize between the wheel input and the browser's scroll
    // event. The upward intent must already own the viewport at that point.
    view.container.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
    view.geometry.setScrollHeight(2400);
    timeline.notifyContentResize();
    await waitForCondition(() => view.geometry.currentScrollTop() === 2000);

    // A user-owned reading position stays put while content grows.
    view.geometry.setScrollTop(600);
    view.container.dispatchEvent(new Event('scroll'));
    view.geometry.setScrollHeight(2600);
    timeline.notifyContentResize();
    await waitForCondition(() => view.geometry.currentScrollTop() === 600);
  });

  it('keeps following while upward input scrolls a nested output box', async () => {
    const view = await mountSessions();
    const box = document.createElement('div');
    box.style.overflowY = 'auto';
    Object.defineProperty(box, 'scrollHeight', { get: () => 900 });
    Object.defineProperty(box, 'clientHeight', { get: () => 300 });
    box.scrollTop = 200;
    view.container.querySelector('.msg').append(box);

    box.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }));
    view.geometry.setScrollHeight(2400);
    timeline.notifyContentResize();
    await waitForCondition(() => view.geometry.currentScrollTop() === 2400);

    // At the box's top the wheel moves the timeline again.
    box.scrollTop = 0;
    box.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, bubbles: true }));
    view.geometry.setScrollHeight(2600);
    timeline.notifyContentResize();
    await waitForCondition(() => view.geometry.currentScrollTop() === 2400);
  });

  it('resumes following when the user returns to the bottom', async () => {
    const view = await mountSessions();

    scrollAsUser(view.container, view.geometry, 500);
    scrollAsUser(view.container, view.geometry, 1980);
    view.geometry.setScrollHeight(2300);
    timeline.notifyContentResize();

    await waitForCondition(() => view.geometry.currentScrollTop() === 2300);
  });

  it('offers a floating jump control while reading and follows again after activation', async () => {
    const view = await mountSessions();
    expect(jumpButton()).toBeNull();

    scrollAsUser(
      view.container,
      view.geometry,
      600,
      new WheelEvent('wheel', { deltaY: -120 }),
    );
    flushSync();
    expect(jumpButton().classList.contains('chat-timeline__jump-latest')).toBe(
      true,
    );

    jumpButton().click();
    await waitForCondition(
      () => view.geometry.currentScrollTop() === 2000 && jumpButton() === null,
    );

    view.geometry.setScrollHeight(2400);
    timeline.notifyContentResize();
    await waitForCondition(() => view.geometry.currentScrollTop() === 2400);
  });

  it('starts every explicit Sub-Agent link visit at the bottom and follows new output', async () => {
    const sessions = scrollMemorySessions();
    const view = await mountSessions(
      {
        sessionState: sessions.childSession,
        followSessionRequest: {
          requestId: 1,
          sessionKey: sessions.childSession.key,
        },
      },
      sessions,
    );

    scrollAsUser(view.container, view.geometry, 300);
    await switchTo(view, view.parentSession, 2000);

    view.props.followSessionRequest = {
      requestId: 2,
      sessionKey: view.childSession.key,
    };
    await switchTo(view, view.childSession, 2000);

    view.geometry.setScrollHeight(2400);
    timeline.notifyContentResize();
    await waitForCondition(() => view.geometry.currentScrollTop() === 2400);
  });

  it('ignores an older-History restore after switching Sessions', async () => {
    let resolveOlder;
    const olderLoaded = new Promise((resolve) => {
      resolveOlder = resolve;
    });
    const view = await mountSessions({
      hasOlderHistory: true,
      onLoadOlder: () => olderLoaded,
    });

    scrollAsUser(view.container, view.geometry, 0);
    await switchTo(view, view.childSession, 2000);

    resolveOlder(true);
    await waitForCondition(() => view.geometry.currentScrollTop() === 2000);
  });

  // During app start the History has not arrived yet, so the timeline is no
  // taller than the viewport; a naive follow flush would collapse to the top.
  it('lands at the bottom, not the top, when content arrives after mount', async () => {
    const session = timelineSession();
    session.historyLoaded = false;
    timeline.mount(
      reactiveProps({
        sessionState: session,
        agentName: 'Alpha',
        loadingHistory: true,
      }),
    );
    const geometry = mockScrollGeometry(document.querySelector('.messages'));
    await waitForCondition(() => true);
    expect(geometry.currentScrollTop()).toBe(0);

    session.historyLoaded = true;
    session.messages = [
      {
        id: 'initial-user',
        role: 'user',
        content: 'First message',
        timestamp: '2026-05-10T09:00:00',
      },
    ];
    geometry.setScrollHeight(2000);
    flushSync();
    timeline.notifyContentResize();

    await waitForCondition(() => geometry.currentScrollTop() === 2000);
  });

  // Only genuine user scroll intent may move the view into reading mode; a
  // programmatic scroll to the top during a switch must not.
  it('does not pin reading mode when a programmatic scroll reaches the top during a switch', async () => {
    const view = await mountSessions({ hasOlderHistory: true });

    view.props.sessionState = view.childSession;
    flushSync();
    view.geometry.setScrollTop(0);
    view.container.dispatchEvent(new Event('scroll'));
    await waitForCondition(() => view.geometry.currentScrollTop() === 2000);

    view.childSession.messages = [
      ...view.childSession.messages,
      lateMessage('child-late'),
    ];
    view.geometry.setScrollHeight(2400);
    flushSync();
    timeline.notifyContentResize();
    await waitForCondition(() => view.geometry.currentScrollTop() === 2400);
  });
});
