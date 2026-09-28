// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  INTENTIONAL_HOVER_SHOW_DELAY_MS,
  floatingHoverCard,
} from '../tooltip.js';
import {
  button,
  placeAt,
  pointerAt,
  pointerEvent,
  pressKey,
} from './tooltip.support.js';

async function flushMicrotasks() {
  for (let index = 0; index < 3; index += 1) {
    await Promise.resolve();
  }
}

describe('floatingHoverCard action', () => {
  let anchor;
  let card;
  let action;

  beforeEach(() => {
    vi.useFakeTimers();
    anchor = document.createElement('div');
    card = document.createElement('div');
    card.textContent = 'Structured description';
    anchor.appendChild(card);
    document.body.appendChild(anchor);
  });

  afterEach(() => {
    action?.destroy();
    action = null;
    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  function open() {
    anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
  }

  it('portals rich content to body and opens it against the anchor after hover intent', () => {
    action = floatingHoverCard(card);

    expect(card.parentElement).toBe(document.body);
    expect(card.dataset.floatingOpen).toBe('false');

    anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS - 1);
    expect(card.dataset.floatingOpen).toBe('false');
    vi.advanceTimersByTime(1);

    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.getAttribute('aria-hidden')).toBe('false');
    expect(card.getAttribute('role')).toBe('tooltip');
    expect(anchor.getAttribute('aria-describedby')).toBe(card.id);
    expect(card.style.left).not.toBe('');
    expect(card.style.top).not.toBe('');
  });

  it('waits for intentional pointer dwell and cancels a pending open', () => {
    action = floatingHoverCard(card, {
      showDelayMs: INTENTIONAL_HOVER_SHOW_DELAY_MS,
    });

    anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(INTENTIONAL_HOVER_SHOW_DELAY_MS - 1);
    expect(card.dataset.floatingOpen).toBe('false');

    anchor.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(INTENTIONAL_HOVER_SHOW_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('false');

    anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(INTENTIONAL_HOVER_SHOW_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('true');
  });

  it('lets an anchor press win over a delayed hover open', () => {
    const control = button('Toggle', anchor);
    action = floatingHoverCard(card);

    anchor.dispatchEvent(new Event('pointerenter'));
    control.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    control.focus();
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('false');

    control.blur();
    pressKey(document.body, 'Tab');
    control.focus();
    expect(card.dataset.floatingOpen).toBe('true');
  });

  it('opens at once on a press when revealing the card is the anchor purpose', () => {
    const trigger = button('Context window usage', anchor);
    action = floatingHoverCard(card, { openOnPress: true });

    trigger.dispatchEvent(new Event('pointerdown', { bubbles: true }));

    expect(card.dataset.floatingOpen).toBe('true');
  });

  it('keeps an interactive card open while the pointer crosses the gap', () => {
    action = floatingHoverCard(card);
    open();
    anchor.dispatchEvent(new Event('pointerleave'));
    card.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);

    expect(card.dataset.floatingOpen).toBe('true');

    card.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('false');
  });

  it('closes at once when the pointer leaves the anchor away from the card', () => {
    placeAt(anchor, { left: 100, top: 300, width: 200, height: 24 });
    action = floatingHoverCard(card, { placement: 'right' });
    open();
    // To the right of the anchor, 6px away.
    placeAt(card, { left: 306, top: 280, width: 180, height: 64 });

    anchor.dispatchEvent(
      pointerAt('pointerleave', 200, 299, { relatedTarget: document.body }),
    );
    window.dispatchEvent(pointerAt('pointermove', 200, 290));

    expect(card.dataset.floatingOpen).toBe('false');
  });

  it('links keyboard focus and closes on Escape or ancestor scrolling', () => {
    const control = button('Anchor control');
    anchor.prepend(control);
    action = floatingHoverCard(card);

    control.focus();
    expect(card.dataset.floatingOpen).toBe('true');
    expect(control.getAttribute('aria-describedby')).toBe(card.id);

    const escape = pressKey(window, 'Escape');
    expect(card.dataset.floatingOpen).toBe('false');
    expect(control.hasAttribute('aria-describedby')).toBe(false);
    // Focus stayed on the anchor, so Escape still reaches enclosing layers.
    expect(escape.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(control);

    open();
    expect(card.dataset.floatingOpen).toBe('true');
    document.dispatchEvent(new Event('scroll'));
    expect(card.dataset.floatingOpen).toBe('false');
  });

  it('stays anchored above while its content grows and stops observing once hidden', () => {
    const observers = [];
    class FakeResizeObserver {
      constructor(callback) {
        this.callback = callback;
        this.observed = [];
        this.disconnected = false;
        observers.push(this);
      }
      observe(target) {
        this.observed.push(target);
      }
      disconnect() {
        this.disconnected = true;
      }
    }
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    try {
      let height = 100;
      Object.defineProperty(card, 'offsetHeight', { get: () => height });
      anchor.getBoundingClientRect = () => ({
        top: 500,
        bottom: 520,
        left: 500,
        right: 540,
        width: 40,
        height: 20,
      });
      window.innerHeight = 800;
      window.innerWidth = 1200;
      action = floatingHoverCard(card);

      open();
      expect(card.style.top).toBe('394px');
      expect(observers).toHaveLength(1);
      expect(observers[0].observed).toEqual([card]);

      // A row appears while the card is open: it must grow upward, not over
      // the anchor.
      height = 160;
      observers[0].callback([]);
      expect(card.style.top).toBe('334px');

      pressKey(window, 'Escape');
      expect(card.dataset.floatingOpen).toBe('false');
      expect(observers[0].disconnected).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('keeps decorative previews out of the accessibility tree', () => {
    action = floatingHoverCard(card, { accessible: false });

    open();

    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.getAttribute('aria-hidden')).toBe('true');
    expect(card.hasAttribute('role')).toBe(false);
    expect(anchor.hasAttribute('aria-describedby')).toBe(false);
  });

  it('opens on touch at once unless taps belong to the anchor', () => {
    action = floatingHoverCard(card);
    anchor.dispatchEvent(pointerEvent('pointerdown', 'touch'));
    expect(card.dataset.floatingOpen).toBe('true');
    anchor.dispatchEvent(pointerEvent('pointerdown', 'touch'));
    expect(card.dataset.floatingOpen).toBe('false');
    action.destroy();

    const preview = document.createElement('span');
    anchor.appendChild(preview);
    action = floatingHoverCard(preview, { accessible: false, touch: false });
    anchor.dispatchEvent(pointerEvent('pointerenter', 'touch'));
    anchor.dispatchEvent(pointerEvent('pointerdown', 'touch'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    expect(preview.dataset.floatingOpen).toBe('false');
  });

  describe('with interactive content', () => {
    let control;
    let copy;
    let more;
    let after;

    beforeEach(() => {
      control = button('Value', anchor);
      card.textContent = '';
      copy = button('Copy', card);
      more = button('Open Extensions', card);
      after = button('Next control');
      action = floatingHoverCard(card);
    });

    it('drops the tooltip role and moves Tab from the anchor into the card and back out in page order', () => {
      control.focus();
      // Tooltips must not be interactive.
      expect(card.dataset.floatingOpen).toBe('true');
      expect(card.hasAttribute('role')).toBe(false);
      expect(control.getAttribute('aria-describedby')).toBe(card.id);

      const tab = pressKey(control, 'Tab');
      expect(tab.defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(copy);

      pressKey(copy, 'Tab');
      expect(document.activeElement).toBe(copy);
      more.focus();
      pressKey(more, 'Tab');
      expect(document.activeElement).toBe(after);
      expect(card.dataset.floatingOpen).toBe('false');
    });

    it('returns Shift+Tab from the first card control to the anchor', () => {
      control.focus();
      pressKey(control, 'Tab');
      const back = pressKey(copy, 'Tab', { shiftKey: true });

      expect(back.defaultPrevented).toBe(true);
      expect(document.activeElement).toBe(control);
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
      expect(card.dataset.floatingOpen).toBe('true');
    });

    it('closes on Escape inside the card, returns focus to the anchor, and consumes the key', () => {
      control.focus();
      pressKey(control, 'Tab');

      const escape = pressKey(window, 'Escape');

      expect(escape.defaultPrevented).toBe(true);
      expect(card.dataset.floatingOpen).toBe('false');
      expect(document.activeElement).toBe(control);
    });

    it('stays open while the pointer rests on it and a focused control gives up focus', () => {
      anchor.dispatchEvent(new Event('pointerenter'));
      vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
      anchor.dispatchEvent(new Event('pointerleave'));
      card.dispatchEvent(new Event('pointerenter'));
      copy.focus();

      // A control that disables itself after activation drops focus.
      copy.blur();
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 2);
      expect(card.dataset.floatingOpen).toBe('true');

      card.dispatchEvent(new Event('pointerleave'));
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
      expect(card.dataset.floatingOpen).toBe('false');
    });

    // Browsers drop focus to <body> once the focused control is disabled, and
    // a removed control loses focus without a focus event.
    it.each([
      [
        'disables itself',
        (focused) => {
          focused.disabled = true;
          focused.blur();
        },
      ],
      ['is removed', (focused) => focused.remove()],
    ])(
      'returns keyboard focus to the anchor when the focused card control %s',
      async (_label, dropFocus) => {
        control.focus();
        pressKey(control, 'Tab');
        expect(document.activeElement).toBe(copy);

        dropFocus(copy);
        await flushMicrotasks();

        expect(document.activeElement).toBe(control);
        vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 2);
        expect(card.dataset.floatingOpen).toBe('true');
        pressKey(window, 'Escape');
        expect(card.dataset.floatingOpen).toBe('false');
      },
    );

    it('leaves focus alone when an enabled card control loses it', async () => {
      control.focus();
      pressKey(control, 'Tab');

      copy.blur();
      await flushMicrotasks();

      expect(document.activeElement).toBe(document.body);
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
      expect(card.dataset.floatingOpen).toBe('false');
    });

    it('stays open while keyboard focus is inside, even when the pointer leaves', () => {
      control.focus();
      pressKey(control, 'Tab');

      card.dispatchEvent(new Event('pointerleave'));
      anchor.dispatchEvent(new Event('pointerleave'));
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 2);

      expect(card.dataset.floatingOpen).toBe('true');
      expect(document.activeElement).toBe(copy);
    });
  });
});
