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

const READING_PARAGRAPH =
  'This paragraph contains the passage being read. ' +
  'A long answer keeps its words visible while the layout changes. '.repeat(30);
const READING_MARKDOWN = `![Earlier image](/api/files/reading.png)\n\n${READING_PARAGRAPH}`;

// jsdom supplies the actual Markdown DOM but no text layout or caret hit
// testing. Give this one Run fixed-width text lines: changing the image's
// height moves the paragraph; changing the line width rewraps its text.
// Geometry resolves from the current DOM so replaced nodes cannot keep a
// detached Range artificially alive.
function mockReadingLayout(view) {
  let imageHeight = 100;
  let charactersPerLine = 50;
  const viewportTop = 60;
  const lineHeight = 20;
  const paragraph = () =>
    Array.from(view.container.querySelectorAll('.msg-markdown p')).find(
      (element) => element.textContent.startsWith('This paragraph'),
    );
  const paragraphTop = () => 300 + imageHeight;
  const rect = (top, height) => ({
    top: viewportTop + top - view.geometry.currentScrollTop(),
    bottom: viewportTop + top + height - view.geometry.currentScrollTop(),
    left: 80,
    right: 580,
    width: 500,
    height,
  });
  const originalRect = Element.prototype.getBoundingClientRect;
  const elementRects = vi
    .spyOn(Element.prototype, 'getBoundingClientRect')
    .mockImplementation(function () {
      if (this === view.container) {
        return { ...rect(0, 500), top: viewportTop, bottom: viewportTop + 500 };
      }
      if (this.matches('[data-timeline-item-id]')) {
        return this.querySelector('.assistant-run')
          ? rect(100, 1800 + imageHeight)
          : rect(0, 100);
      }
      if (this === paragraph()) {
        return rect(
          paragraphTop(),
          Math.ceil(this.textContent.length / charactersPerLine) * lineHeight,
        );
      }
      if (this.matches('.msg-markdown img, .msg-markdown p:has(img)')) {
        return rect(200, imageHeight);
      }
      return originalRect.call(this);
    });

  function textPoint(offset) {
    const walker = document.createTreeWalker(paragraph(), 4 /* SHOW_TEXT */);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (offset < node.length) return { node, offset };
      offset -= node.length;
    }
    throw new Error('The requested reading point is outside the paragraph.');
  }

  const previousCaret = Object.getOwnPropertyDescriptor(
    document,
    'caretPositionFromPoint',
  );
  Object.defineProperty(document, 'caretPositionFromPoint', {
    configurable: true,
    value: (_x, y) => {
      const line = Math.max(
        0,
        Math.floor(
          (y -
            viewportTop +
            view.geometry.currentScrollTop() -
            paragraphTop()) /
            lineHeight,
        ),
      );
      const point = textPoint(
        Math.min(line * charactersPerLine, paragraph().textContent.length - 1),
      );
      return { offsetNode: point.node, offset: point.offset };
    },
  });
  const previousRangeRect = Object.getOwnPropertyDescriptor(
    Range.prototype,
    'getBoundingClientRect',
  );
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    value() {
      const block = paragraph();
      if (!block?.contains(this.startContainer)) return rect(0, 0);
      const prefix = document.createRange();
      prefix.selectNodeContents(block);
      prefix.setEnd(this.startContainer, this.startOffset);
      const line = Math.floor(prefix.toString().length / charactersPerLine);
      return rect(paragraphTop() + line * lineHeight, lineHeight);
    },
  });

  return {
    paragraph,
    textTop(offset) {
      const point = textPoint(offset);
      const range = document.createRange();
      range.setStart(point.node, point.offset);
      range.setEnd(point.node, point.offset + 1);
      return range.getBoundingClientRect().top;
    },
    growImage() {
      imageHeight += 300;
      view.geometry.setScrollHeight(2300);
    },
    narrowParagraph() {
      charactersPerLine = 25;
      view.geometry.setScrollHeight(3200);
    },
    restore() {
      elementRects.mockRestore();
      for (const [target, key, descriptor] of [
        [document, 'caretPositionFromPoint', previousCaret],
        [Range.prototype, 'getBoundingClientRect', previousRangeRect],
      ]) {
        if (descriptor) Object.defineProperty(target, key, descriptor);
        else delete target[key];
      }
    },
  };
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

  it.each([
    ['an earlier image grows inside the same Run', 'image', 900],
    ['the same long paragraph wraps onto more lines', 'wrapping', 800],
    ['Markdown rebuilds the paragraph with new inline nodes', 'markdown', 900],
    [
      'a Session switch remounts the Run after its layout changes',
      'session',
      900,
    ],
  ])(
    'keeps the visible text line when %s',
    async (_name, change, expectedTop) => {
      const sessions = scrollMemorySessions();
      appendEvents(sessions.parentSession, 'run-reading', [
        userPersisted('reading-user', 'Give a long answer.'),
        {
          type: 'assistant_output_delta',
          payload: { content_delta: READING_MARKDOWN },
        },
      ]);
      // Only the long Run supplies layout; the older History is irrelevant to
      // this position within the Run and does not need synthetic geometry.
      sessions.parentSession.messages = [];
      const view = await mountSessions({}, sessions);
      const layout = mockReadingLayout(view);
      try {
        scrollAsUser(
          view.container,
          view.geometry,
          600,
          new WheelEvent('wheel', { deltaY: -120 }),
        );
        const before = layout.textTop(500);
        const originalParagraph = layout.paragraph();
        expect(before).toBe(60);

        if (change === 'session') {
          await switchTo(view, view.childSession, 2000);
          expect(originalParagraph.isConnected).toBe(false);
          layout.growImage();
          await switchTo(view, view.parentSession, expectedTop);
        } else {
          if (change === 'wrapping') layout.narrowParagraph();
          else layout.growImage();
          if (change === 'markdown') {
            appendEvents(
              view.props.sessionState,
              'run-reading',
              [
                {
                  type: 'assistant_output',
                  payload: {
                    message: {
                      role: 'assistant',
                      content: READING_MARKDOWN.replace(
                        'This paragraph',
                        '**This paragraph**',
                      ),
                    },
                  },
                },
              ],
              3,
            );
            flushSync();
            expect(originalParagraph.isConnected).toBe(false);
            expect(layout.paragraph().querySelector('strong')).not.toBeNull();
          }
          timeline.notifyContentResize();
          await waitForCondition(
            () => view.geometry.currentScrollTop() === expectedTop,
          );
        }

        expect(layout.textTop(500)).toBe(before);
        // Further content notifications cannot apply the same correction twice.
        timeline.notifyContentResize();
        await tick();
        if (typeof requestAnimationFrame === 'function') {
          await new Promise((resolve) => requestAnimationFrame(resolve));
        }
        expect(view.geometry.currentScrollTop()).toBe(expectedTop);
      } finally {
        layout.restore();
      }
    },
  );

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

  it('keeps nested output scrolling independent of timeline following and reading', async () => {
    const view = await mountSessions();
    const box = document.createElement('div');
    box.style.overflowY = 'auto';
    let nestedHeight = 900;
    Object.defineProperty(box, 'scrollHeight', { get: () => nestedHeight });
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

    // Search matches contain real paragraphs inside their own scroll box.
    // While reading, an inner scroll must not become an outer correction
    // when unrelated streaming content next changes the timeline's layout.
    const list = document.createElement('ol');
    const match = document.createElement('li');
    const excerpt = document.createElement('p');
    excerpt.textContent = 'A long search match inside the Tool results.';
    match.append(excerpt);
    list.append(match);
    box.append(list);
    const row = box.closest('[data-timeline-item-id]');
    const rect = (top, height) => ({
      top: top - view.geometry.currentScrollTop(),
      bottom: top + height - view.geometry.currentScrollTop(),
      left: 0,
      right: 500,
      width: 500,
      height,
    });
    row.getBoundingClientRect = () => rect(0, 1600);
    box.getBoundingClientRect = () => rect(600, 300);
    for (const element of [match, excerpt]) {
      element.getBoundingClientRect = () => rect(700 - box.scrollTop, 400);
    }
    for (const alreadyOverflowing of [false, true]) {
      // Output may become scrollable only after the reading anchor was taken.
      nestedHeight = alreadyOverflowing ? 900 : 300;
      box.scrollTop = alreadyOverflowing ? 200 : 0;
      scrollAsUser(view.container, view.geometry, 590);
      scrollAsUser(view.container, view.geometry, 600);
      nestedHeight = 900;
      box.dispatchEvent(new WheelEvent('wheel', { deltaY: 80, bubbles: true }));
      box.scrollTop = 280;
      box.dispatchEvent(new Event('scroll'));
      view.geometry.setScrollHeight(2800);
      timeline.notifyContentResize();
      await tick();
      if (typeof requestAnimationFrame === 'function') {
        await new Promise((resolve) => requestAnimationFrame(resolve));
      }
      expect(view.geometry.currentScrollTop()).toBe(600);
      expect(box.scrollTop).toBe(280);
    }
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
