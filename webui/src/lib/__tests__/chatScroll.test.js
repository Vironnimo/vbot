import { describe, expect, it, vi } from 'vitest';

import { createChatScrollController } from '../chatScroll.js';

// A controllable fake of the browser scroll container: assignments to
// scrollTop are stored verbatim (like the real property before clamping),
// geometry fields are plain writable values, and element rects are
// viewport-relative like real getBoundingClientRect results.
function createFakeContainer() {
  const container = {
    scrollHeight: 2000,
    offsetHeight: 500,
    scrollTop: 0,
    scrollTo: vi.fn(),
    listeners: {},
    addEventListener(type, handler) {
      this.listeners[type] = handler;
    },
    removeEventListener(type) {
      delete this.listeners[type];
    },
    dispatchScroll() {
      this.listeners.scroll?.();
    },
    querySelectorAll: vi.fn(() => []),
    getBoundingClientRect: () => ({ top: 0 }),
  };
  return container;
}

function createElement(id, offsetTop, container) {
  return {
    dataset: { timelineItemId: id },
    offsetTop,
    offsetHeight: 40,
    getBoundingClientRect: () => ({
      top: offsetTop - container.scrollTop,
      height: 40,
      bottom: offsetTop - container.scrollTop + 40,
    }),
  };
}

function createController(container, overrides = {}) {
  return createChatScrollController(container, {
    onViewChanged: vi.fn(),
    shouldLoadOlder: () => false,
    requestLoadOlder: async () => false,
    ...overrides,
  });
}

describe('createChatScrollController', () => {
  it('treats a scroll event matching the last written position as its own echo', () => {
    const container = createFakeContainer();
    const onViewChanged = vi.fn();
    const controller = createController(container, { onViewChanged });

    controller.sessionChanged('session-a');
    // Apply the pending follow restore.
    controller.contentChanged();
    expect(container.scrollTop).toBe(2000);
    onViewChanged.mockClear();

    // The browser echoes the write back; it must not reclassify anything.
    container.dispatchScroll();
    expect(onViewChanged).not.toHaveBeenCalled();
    controller.destroy();
  });

  it('classifies a diverging scroll event as user motion and pins at the bottom', () => {
    const container = createFakeContainer();
    const onViewChanged = vi.fn();
    const controller = createController(container, { onViewChanged });
    controller.sessionChanged('session-a');
    controller.contentChanged();
    onViewChanged.mockClear();

    container.scrollTop = 1980;
    container.dispatchScroll();

    expect(onViewChanged).toHaveBeenCalled();
    expect(controller.isNearBottom()).toBe(true);
    // Pinned viewport follows subsequent growth.
    container.scrollHeight = 2400;
    controller.contentChanged();
    expect(container.scrollTop).toBe(2400);
    controller.destroy();
  });

  it('releases the pin on upward input before the browser scrolls', () => {
    const container = createFakeContainer();
    const controller = createController(container);
    controller.sessionChanged('session-a');
    controller.contentChanged();
    expect(container.scrollTop).toBe(2000);

    controller.noteUserInput({ upward: true });
    container.scrollHeight = 2400;
    controller.contentChanged();

    // No snap to the new bottom: the gesture owns the viewport.
    expect(container.scrollTop).toBe(2000);
    controller.destroy();
  });

  it('holds a reading position stable while content grows below', () => {
    const container = createFakeContainer();
    const controller = createController(container);
    controller.sessionChanged('session-a');
    controller.contentChanged();

    container.scrollTop = 600;
    container.dispatchScroll();
    container.scrollHeight = 2400;
    controller.contentChanged();

    expect(container.scrollTop).toBe(600);
    controller.destroy();
  });

  it('corrects reading position when content above the anchor grows', () => {
    const container = createFakeContainer();
    const items = [
      createElement('item-1', 100, container),
      createElement('item-2', 700, container),
    ];
    container.querySelectorAll = vi.fn(() => items);
    const controller = createController(container);
    controller.sessionChanged('session-a');
    controller.contentChanged();

    // User reads at a position where item-2 sits 120px below the top edge.
    container.scrollTop = 580;
    container.dispatchScroll();
    expect(items[1].offsetTop - container.scrollTop).toBe(120);

    // Content above the anchor grows by 90px; the anchor must stay put.
    items[0].offsetTop = 190;
    items[1].offsetTop = 790;
    controller.contentChanged();
    expect(container.scrollTop).toBe(670);
    expect(items[1].offsetTop - container.scrollTop).toBe(120);
    controller.destroy();
  });

  it('requests older history once when a user scroll reaches the top', async () => {
    const container = createFakeContainer();
    const requestLoadOlder = vi.fn(async () => true);
    const controller = createController(container, {
      shouldLoadOlder: () => true,
      requestLoadOlder,
    });
    controller.sessionChanged('session-a');
    controller.contentChanged();

    container.scrollTop = 10;
    container.dispatchScroll();
    await Promise.resolve();

    expect(requestLoadOlder).toHaveBeenCalledTimes(1);

    // Prepended history grew content above; the held pixel position shifts
    // by the exact growth so the reading position survives the prepend.
    container.scrollHeight = 2600;
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    controller.contentChanged();
    expect(container.scrollTop).toBe(610);
    controller.destroy();
  });

  it('restores a saved reading pixel position when returning to a session', () => {
    const container = createFakeContainer();
    const controller = createController(container);
    controller.sessionChanged('session-a');
    controller.contentChanged();

    container.scrollTop = 700;
    container.dispatchScroll();

    controller.sessionChanged('session-b');
    controller.contentChanged();
    expect(container.scrollTop).toBe(2000);

    controller.sessionChanged('session-a');
    controller.contentChanged();
    expect(container.scrollTop).toBe(700);
    controller.destroy();
  });

  it('falls to the bottom when a saved reading anchor no longer exists', () => {
    const container = createFakeContainer();
    const controller = createController(container);
    controller.sessionChanged('session-a');
    controller.contentChanged();

    container.scrollTop = 700;
    container.dispatchScroll();
    controller.sessionChanged('session-b');
    controller.contentChanged();

    // The old content was replaced and the height changed: the stale pixel
    // fallback would land mid-text, so the bottom wins.
    container.scrollHeight = 2600;
    controller.sessionChanged('session-a');
    controller.contentChanged();
    expect(container.scrollTop).toBe(2600);
    expect(controller.isNearBottom()).toBe(true);
    controller.destroy();
  });

  it('pins an explicitly requested session to the bottom on restore', () => {
    const container = createFakeContainer();
    const controller = createController(container);
    controller.sessionChanged('session-a');
    controller.contentChanged();

    container.scrollTop = 400;
    container.dispatchScroll();
    controller.sessionChanged('session-b');
    // Explicit Sub-Agent visit forces follow mode over the saved viewport.
    controller.forceFollowOnNextRestore();
    controller.contentChanged();
    expect(container.scrollTop).toBe(2000);
    controller.destroy();
  });

  it('animates one element to the top and anchors the reading position there', () => {
    const container = createFakeContainer();
    const target = createElement('submitted-turn', 1500, container);
    container.querySelectorAll = vi.fn(() => [target]);
    const controller = createController(container);
    controller.sessionChanged('session-a');
    controller.contentChanged();

    // Element sits 1500px into the content; aligned 28px below the top edge.
    controller.animateElementToTop(target);
    expect(container.scrollTo).toHaveBeenCalledWith({
      top: 1472,
      behavior: 'smooth',
    });

    // The animated element owns the reading position: growth below it must
    // not pull the view back down.
    container.scrollHeight = 3000;
    controller.contentChanged();
    expect(container.scrollTop).toBe(1472);
    controller.destroy();
  });

  it('ignores scroll events while an explicit animation is in flight', () => {
    const container = createFakeContainer();
    const onViewChanged = vi.fn();
    const target = createElement('submitted-turn', 1500, container);
    container.querySelectorAll = vi.fn(() => [target]);
    const controller = createController(container, { onViewChanged });
    controller.sessionChanged('session-a');
    controller.contentChanged();
    onViewChanged.mockClear();

    controller.animateElementToTop(target);
    // Intermediate smooth-scroll positions are part of the animation.
    container.scrollTop = 800;
    container.dispatchScroll();
    expect(onViewChanged).not.toHaveBeenCalled();
    controller.destroy();
  });

  it('keeps sessions in separate viewports up to the tracking limit', () => {
    const container = createFakeContainer();
    const controller = createController(container);
    for (let index = 0; index < 105; index += 1) {
      controller.sessionChanged(`session-${index}`);
      controller.contentChanged();
    }
    // Oldest entries were trimmed without breaking the newest ones.
    controller.sessionChanged('session-104');
    controller.contentChanged();
    expect(container.scrollTop).toBe(2000);
    controller.destroy();
  });
});
