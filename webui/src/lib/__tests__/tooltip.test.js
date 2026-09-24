// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  INTENTIONAL_HOVER_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS as SHOW_DELAY_MS,
  TOOLTIP_SKIP_DELAY_MS,
  floatingHoverCard,
  positionFloating,
  tooltip,
} from '../tooltip.js';

function tooltipElement() {
  return document.getElementById('app-tooltip');
}

function isVisible() {
  return tooltipElement()?.dataset.floatingOpen === 'true';
}

function hover(node) {
  node.dispatchEvent(new Event('pointerenter'));
  vi.advanceTimersByTime(SHOW_DELAY_MS);
}

function pointerEvent(type, pointerType) {
  const event = new Event(type, { bubbles: type === 'pointerdown' });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  return event;
}

// Keyboard modality: the next focus counts as keyboard focus.
function pressKey(target, key, options = {}) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...options,
  });
  target.dispatchEvent(event);
  return event;
}

async function flushMicrotasks() {
  for (let index = 0; index < 3; index += 1) {
    await Promise.resolve();
  }
}

function button(label, parent = document.body) {
  const element = document.createElement('button');
  element.type = 'button';
  element.textContent = label;
  parent.appendChild(element);
  return element;
}

describe('tooltip action', () => {
  let node;
  let action;

  beforeEach(() => {
    vi.useFakeTimers();
    node = button('Anchor');
  });

  afterEach(() => {
    action?.destroy();
    action = null;
    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  it('shows the text after the hover delay and links it via aria-describedby', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(SHOW_DELAY_MS - 1);
    expect(isVisible()).toBe(false);

    vi.advanceTimersByTime(1);
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Copy to clipboard');
    expect(tooltipElement().getAttribute('role')).toBe('tooltip');
    expect(node.getAttribute('aria-describedby')).toBe('app-tooltip');
  });

  it('keeps other descriptions on the anchor while linked', () => {
    node.setAttribute('aria-describedby', 'field-help');
    action = tooltip(node, 'Copy to clipboard');

    hover(node);
    expect(node.getAttribute('aria-describedby')).toBe(
      'field-help app-tooltip',
    );

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(node.getAttribute('aria-describedby')).toBe('field-help');
  });

  it('hides after a short grace on pointer leave and clears the aria link', () => {
    action = tooltip(node, 'Copy to clipboard');
    hover(node);

    node.dispatchEvent(new Event('pointerleave'));
    expect(isVisible()).toBe(true);
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(isVisible()).toBe(false);
    expect(node.hasAttribute('aria-describedby')).toBe(false);
  });

  it('stays open while the pointer rests on the tooltip itself', () => {
    action = tooltip(node, 'A long value worth reading');
    hover(node);

    node.dispatchEvent(new Event('pointerleave'));
    tooltipElement().dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 4);
    expect(isVisible()).toBe(true);

    tooltipElement().dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(isVisible()).toBe(false);
  });

  it('cancels a pending show when the pointer leaves within the delay', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerenter'));
    node.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);

    expect(isVisible()).toBe(false);
  });

  it('shows the next tooltip at once while warm and waits again once cold', () => {
    const second = button('Second');
    const third = button('Third');
    action = tooltip(node, 'First');
    const secondAction = tooltip(second, 'Second hint');
    const thirdAction = tooltip(third, 'Third hint');

    hover(node);
    node.dispatchEvent(new Event('pointerleave'));
    second.dispatchEvent(new Event('pointerenter'));
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Second hint');
    expect(node.hasAttribute('aria-describedby')).toBe(false);

    second.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(isVisible()).toBe(false);
    vi.advanceTimersByTime(TOOLTIP_SKIP_DELAY_MS - 1);
    third.dispatchEvent(new Event('pointerenter'));
    expect(tooltipElement().textContent).toBe('Third hint');
    expect(isVisible()).toBe(true);

    third.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    vi.advanceTimersByTime(TOOLTIP_SKIP_DELAY_MS);
    node.dispatchEvent(new Event('pointerenter'));
    expect(isVisible()).toBe(false);
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(true);

    secondAction.destroy();
    thirdAction.destroy();
  });

  it('keeps a pending show alive when an unrelated tooltip instance is destroyed', () => {
    // Streaming re-renders destroy tooltip actions (e.g. remounted code-block
    // copy buttons) while the pointer dwells somewhere else entirely.
    const churned = button('Churned');
    const churnedAction = tooltip(churned, 'Churned away');
    const hovered = button('Hovered');
    const hoveredAction = tooltip(hovered, 'Hovered');

    hovered.dispatchEvent(new Event('pointerenter'));
    churnedAction.destroy();
    vi.advanceTimersByTime(SHOW_DELAY_MS);

    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Hovered');

    hoveredAction.destroy();
  });

  it('keeps a pending show alive when an unrelated tooltip is disabled via update', () => {
    const churned = button('Churned');
    const churnedAction = tooltip(churned, 'Churned away');
    const hovered = button('Hovered');
    const hoveredAction = tooltip(hovered, 'Hovered');

    hovered.dispatchEvent(new Event('pointerenter'));
    churnedAction.update('');
    vi.advanceTimersByTime(SHOW_DELAY_MS);

    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Hovered');

    hoveredAction.destroy();
    churnedAction.destroy();
  });

  it('shows immediately when a node is replaced under a stationary pointer', () => {
    action = tooltip(node, 'Copy to clipboard');

    // Streaming content swaps the hovered node for an identical one: the
    // browser fires leave + enter at the same pointer position without any
    // pointer movement. The replacement must not restart the dwell delay.
    node.dispatchEvent(
      new MouseEvent('pointerleave', { clientX: 12, clientY: 34 }),
    );
    node.dispatchEvent(
      new MouseEvent('pointerenter', { clientX: 12, clientY: 34 }),
    );
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Copy to clipboard');
  });

  it('applies the hover delay again after the pointer actually moved', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(
      new MouseEvent('pointerleave', { clientX: 12, clientY: 34 }),
    );
    node.dispatchEvent(
      new MouseEvent('pointerenter', { clientX: 40, clientY: 60 }),
    );
    expect(isVisible()).toBe(false);
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(true);
  });

  it('hides on Escape and on pointerdown (activating the control)', () => {
    action = tooltip(node, 'Copy to clipboard');

    hover(node);
    const escape = new KeyboardEvent('keydown', {
      key: 'Escape',
      cancelable: true,
    });
    window.dispatchEvent(escape);
    expect(isVisible()).toBe(false);
    // A quick tooltip never swallows Escape from an enclosing dialog.
    expect(escape.defaultPrevented).toBe(false);

    hover(node);
    node.dispatchEvent(new Event('pointerdown'));
    expect(isVisible()).toBe(false);
  });

  it('toggles on touch taps, which have no hover', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(pointerEvent('pointerenter', 'touch'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(false);

    node.dispatchEvent(pointerEvent('pointerdown', 'touch'));
    expect(isVisible()).toBe(true);
    node.dispatchEvent(pointerEvent('pointerleave', 'touch'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(isVisible()).toBe(true);

    node.dispatchEvent(pointerEvent('pointerdown', 'touch'));
    expect(isVisible()).toBe(false);
  });

  it('shows at once on keyboard focus and hides on blur', () => {
    action = tooltip(node, 'Copy to clipboard');

    pressKey(document.body, 'Tab');
    node.focus();
    expect(isVisible()).toBe(true);
    expect(node.getAttribute('aria-describedby')).toBe('app-tooltip');

    node.blur();
    expect(isVisible()).toBe(false);
  });

  it('does not pop open when a pointer press focuses the control', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    node.focus();
    expect(isVisible()).toBe(false);

    node.blur();
    pressKey(document.body, 'Tab');
    node.focus();
    expect(isVisible()).toBe(true);
  });

  it('reacts to keyboard focus inside a tooltip-anchor wrapper', () => {
    const wrapper = document.createElement('span');
    wrapper.className = 'tooltip-anchor';
    document.body.appendChild(wrapper);
    const inner = button('Microphone', wrapper);
    action = tooltip(wrapper, 'Voice input unavailable');

    pressKey(document.body, 'Tab');
    inner.focus();
    expect(isVisible()).toBe(true);
    expect(wrapper.getAttribute('aria-describedby')).toBe('app-tooltip');

    inner.blur();
    expect(isVisible()).toBe(false);
  });

  it('lets the innermost anchor own a bubbling keyboard focus', () => {
    const wrapper = document.createElement('div');
    document.body.appendChild(wrapper);
    const inner = button('Inner', wrapper);
    action = tooltip(wrapper, 'Outer hint');
    const innerAction = tooltip(inner, 'Inner hint');

    pressKey(document.body, 'Tab');
    inner.focus();
    expect(tooltipElement().textContent).toBe('Inner hint');

    innerAction.destroy();
  });

  it('never shows for empty text, and update() can disable a visible tooltip', () => {
    action = tooltip(node, '');
    hover(node);
    expect(isVisible()).toBe(false);

    action.update('Now populated');
    node.dispatchEvent(new Event('pointerleave'));
    hover(node);
    expect(isVisible()).toBe(true);

    action.update('');
    expect(isVisible()).toBe(false);
  });

  it('update() swaps the text of a visible tooltip in place', () => {
    action = tooltip(node, 'Before');
    hover(node);

    action.update('After');
    expect(tooltipElement().textContent).toBe('After');
    expect(isVisible()).toBe(true);
  });

  it('cleans up on destroy', () => {
    action = tooltip(node, 'Copy to clipboard');
    hover(node);

    action.destroy();
    action = null;
    expect(isVisible()).toBe(false);

    node.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(false);
  });

  it('repositions on scroll instead of hiding while the anchor is visible', () => {
    action = tooltip(node, 'Context: 5000 tok');
    node.getBoundingClientRect = () => ({
      top: 300,
      bottom: 320,
      left: 500,
      right: 540,
      width: 40,
      height: 20,
    });
    window.innerHeight = 800;
    window.innerWidth = 1200;
    hover(node);
    expect(isVisible()).toBe(true);

    window.dispatchEvent(new Event('scroll'));
    expect(isVisible()).toBe(true);
  });

  it('follows its anchor when the viewport is resized', () => {
    action = tooltip(node, 'Context: 5000 tok');
    let left = 500;
    node.getBoundingClientRect = () => ({
      top: 300,
      bottom: 320,
      left,
      right: left + 40,
      width: 40,
      height: 20,
    });
    window.innerHeight = 800;
    window.innerWidth = 1200;
    hover(node);
    const initialLeft = tooltipElement().style.left;

    left = 200;
    window.innerWidth = 700;
    window.dispatchEvent(new Event('resize'));

    expect(isVisible()).toBe(true);
    expect(tooltipElement().style.left).not.toBe(initialLeft);
  });

  it('hides on scroll when the anchor has scrolled out of the viewport', () => {
    action = tooltip(node, 'Context: 5000 tok');
    node.getBoundingClientRect = () => ({
      top: -100,
      bottom: -80,
      left: 500,
      right: 540,
      width: 40,
      height: 20,
    });
    window.innerHeight = 800;
    window.innerWidth = 1200;
    hover(node);
    expect(isVisible()).toBe(true);

    window.dispatchEvent(new Event('scroll'));
    expect(isVisible()).toBe(false);
  });
});

describe('positionFloating', () => {
  const originalHeight = window.innerHeight;
  const originalWidth = window.innerWidth;

  beforeEach(() => {
    window.innerHeight = 800;
    window.innerWidth = 1200;
  });

  afterEach(() => {
    window.innerHeight = originalHeight;
    window.innerWidth = originalWidth;
  });

  function anchorAt({ top, bottom, left = 500, width = 40, placement }) {
    return {
      dataset: placement ? { tooltipPlacement: placement } : {},
      getBoundingClientRect: () => ({
        top,
        bottom,
        left,
        width,
        right: left + width,
        height: bottom - top,
        x: left,
        y: top,
      }),
    };
  }

  function floatingOfSize(width, height) {
    const element = document.createElement('div');
    Object.defineProperty(element, 'offsetWidth', { value: width });
    Object.defineProperty(element, 'offsetHeight', { value: height });
    return element;
  }

  it('centers above the anchor when there is room', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 300, bottom: 320 }), element);

    // centered: 500 + 20 - 50 = 470; above: 300 - 6 - 24 = 270.
    expect(element.style.left).toBe('470px');
    expect(element.style.top).toBe('270px');
    expect(element.dataset.floatingSide).toBe('top');
  });

  it('falls below the anchor when there is no room above', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 10, bottom: 30 }), element);

    expect(element.style.top).toBe('36px');
    expect(element.dataset.floatingSide).toBe('bottom');
  });

  it('clamps to the viewport edges horizontally', () => {
    const element = floatingOfSize(200, 24);
    positionFloating(
      anchorAt({ top: 300, bottom: 320, left: 0, width: 20 }),
      element,
    );
    expect(element.style.left).toBe('8px');

    positionFloating(
      anchorAt({ top: 300, bottom: 320, left: 1180, width: 20 }),
      element,
    );
    expect(element.style.left).toBe(`${1200 - 200 - 8}px`);
  });

  it('places a rail anchor tooltip to its right, vertically centered', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(
      anchorAt({
        top: 300,
        bottom: 340,
        left: 12,
        width: 40,
        placement: 'right',
      }),
      element,
    );

    // right: 12 + 40 + 6 = 58; centered: 300 + 20 - 12 = 308.
    expect(element.style.left).toBe('58px');
    expect(element.style.top).toBe('308px');
    expect(element.dataset.floatingSide).toBe('right');
  });

  it('falls back to above when a right placement does not fit', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(
      anchorAt({
        top: 300,
        bottom: 320,
        left: 1150,
        width: 40,
        placement: 'right',
      }),
      element,
    );

    expect(element.style.top).toBe('270px');
  });
});

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

  it('repositions on scroll instead of hiding while the anchor is visible', () => {
    action = floatingHoverCard(card);
    anchor.getBoundingClientRect = () => ({
      top: 300,
      bottom: 320,
      left: 500,
      right: 540,
      width: 40,
      height: 20,
    });
    window.innerHeight = 800;
    window.innerWidth = 1200;

    open();
    expect(card.dataset.floatingOpen).toBe('true');

    document.dispatchEvent(new Event('scroll'));
    expect(card.dataset.floatingOpen).toBe('true');
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

    it('drops the tooltip role because tooltips must not be interactive', () => {
      control.focus();
      expect(card.dataset.floatingOpen).toBe('true');
      expect(card.hasAttribute('role')).toBe(false);
      expect(control.getAttribute('aria-describedby')).toBe(card.id);
    });

    it('moves Tab from the anchor into the card and back out in page order', () => {
      control.focus();
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

    it('returns keyboard focus to the anchor when a focused card control disables itself', async () => {
      control.focus();
      pressKey(control, 'Tab');
      expect(document.activeElement).toBe(copy);

      // Browsers drop focus to <body> once the focused control is disabled.
      copy.disabled = true;
      copy.blur();
      await flushMicrotasks();

      expect(document.activeElement).toBe(control);
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 2);
      expect(card.dataset.floatingOpen).toBe('true');
      pressKey(window, 'Escape');
      expect(card.dataset.floatingOpen).toBe('false');
    });

    it('recovers focus when the focused card control is removed without a focus event', async () => {
      control.focus();
      pressKey(control, 'Tab');

      copy.remove();
      await flushMicrotasks();

      expect(document.activeElement).toBe(control);
      expect(card.dataset.floatingOpen).toBe('true');
    });

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

describe('floating layer coordination', () => {
  let actions;

  beforeEach(() => {
    vi.useFakeTimers();
    actions = [];
  });

  afterEach(() => {
    for (const action of actions) {
      action.destroy();
    }
    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  function hoverCard(text) {
    const anchor = document.createElement('div');
    const card = document.createElement('div');
    card.textContent = text;
    anchor.appendChild(card);
    document.body.appendChild(anchor);
    actions.push(floatingHoverCard(card));
    return { anchor, card };
  }

  it('closes a visible quick tooltip when a hover card opens, and vice versa', () => {
    const hinted = button('Hinted');
    actions.push(tooltip(hinted, 'Quick hint'));
    const { anchor, card } = hoverCard('Card');

    hover(hinted);
    expect(isVisible()).toBe(true);

    anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('true');
    expect(isVisible()).toBe(false);

    hover(hinted);
    expect(isVisible()).toBe(true);
    expect(card.dataset.floatingOpen).toBe('false');
  });

  it('keeps a hover card open for a tooltip anchored inside it and closes both together', () => {
    const { anchor, card } = hoverCard('');
    const copy = button('Copy', card);
    actions.push(tooltip(copy, 'Copy full value'));

    anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    card.dispatchEvent(new Event('pointerenter'));
    hover(copy);

    expect(isVisible()).toBe(true);
    expect(card.dataset.floatingOpen).toBe('true');

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(card.dataset.floatingOpen).toBe('false');
    expect(isVisible()).toBe(false);
  });

  it('keeps only one hover card open at a time', () => {
    const first = hoverCard('First');
    const second = hoverCard('Second');

    first.anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    second.anchor.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);

    expect(first.card.dataset.floatingOpen).toBe('false');
    expect(second.card.dataset.floatingOpen).toBe('true');
  });
});
