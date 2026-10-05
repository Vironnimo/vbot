// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS as SHOW_DELAY_MS,
  TOOLTIP_SKIP_DELAY_MS,
  floatingHoverCard,
  tooltip,
} from '../tooltip.js';
import {
  button,
  placeAt,
  pointerAt,
  pointerEvent,
  pressKey,
} from './tooltip.support.js';

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

  it('shows the text after the hover delay and adds itself to the anchor description', () => {
    node.setAttribute('aria-describedby', 'field-help');
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(SHOW_DELAY_MS - 1);
    expect(isVisible()).toBe(false);

    vi.advanceTimersByTime(1);
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Copy to clipboard');
    expect(tooltipElement().getAttribute('role')).toBe('tooltip');
    expect(node.getAttribute('aria-describedby')).toBe(
      'field-help app-tooltip',
    );

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(node.getAttribute('aria-describedby')).toBe('field-help');
  });

  it('lets the pointer pass through a label, which closes after a short grace and clears the aria link', () => {
    action = tooltip(node, 'Copy to clipboard');
    hover(node);
    expect(tooltipElement().dataset.selectable).toBe('false');

    node.dispatchEvent(new Event('pointerleave'));
    tooltipElement().dispatchEvent(new Event('pointerenter'));
    expect(isVisible()).toBe(true);
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);

    expect(isVisible()).toBe(false);
    expect(node.hasAttribute('aria-describedby')).toBe(false);
  });

  describe('when selectable', () => {
    beforeEach(() => {
      placeAt(node, { left: 100, top: 300, width: 80, height: 20 });
      action = tooltip(node, { text: 'C:/repo/src/app.js', selectable: true });
      hover(node);
      // Above the anchor, 6px away.
      placeAt(tooltipElement(), { left: 80, top: 250, width: 120, height: 44 });
    });

    afterEach(() => {
      window.getSelection().removeAllRanges();
    });

    it('stays open while the pointer travels onto it and closes once it heads elsewhere', () => {
      expect(tooltipElement().dataset.selectable).toBe('true');

      node.dispatchEvent(
        pointerAt('pointerleave', 140, 299, { relatedTarget: document.body }),
      );
      window.dispatchEvent(pointerAt('pointermove', 141, 296));
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 4);
      expect(isVisible()).toBe(true);

      tooltipElement().dispatchEvent(new Event('pointerenter'));
      tooltipElement().dispatchEvent(
        pointerAt('pointerleave', 205, 260, { relatedTarget: document.body }),
      );
      window.dispatchEvent(pointerAt('pointermove', 260, 262));
      expect(isVisible()).toBe(false);
    });

    it('closes at once when the pointer leaves its anchor away from it or leaves the document', () => {
      node.dispatchEvent(
        pointerAt('pointerleave', 140, 321, { relatedTarget: document.body }),
      );
      expect(isVisible()).toBe(true);

      window.dispatchEvent(pointerAt('pointermove', 140, 330));
      expect(isVisible()).toBe(false);

      hover(node);
      node.dispatchEvent(
        pointerAt('pointerleave', 140, 299, { relatedTarget: null }),
      );
      expect(isVisible()).toBe(false);
    });

    it('falls back to the short grace for events without pointer coordinates', () => {
      node.dispatchEvent(new Event('pointerleave'));
      tooltipElement().dispatchEvent(new Event('pointerenter'));
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 4);
      expect(isVisible()).toBe(true);

      tooltipElement().dispatchEvent(new Event('pointerleave'));
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
      expect(isVisible()).toBe(false);
    });

    it('keeps a text selection inside it until an outside press', () => {
      const range = document.createRange();
      range.selectNodeContents(tooltipElement().firstChild);
      window.getSelection().addRange(range);

      tooltipElement().dispatchEvent(
        pointerAt('pointerleave', 260, 262, { relatedTarget: document.body }),
      );
      window.dispatchEvent(pointerAt('pointermove', 300, 262));
      vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS * 4);
      expect(isVisible()).toBe(true);

      document.body.dispatchEvent(pointerEvent('pointerdown', 'mouse'));
      expect(isVisible()).toBe(false);
    });
  });

  it('renders details as a title, free text and aligned label/value rows', () => {
    const plain = button('Plain');
    const plainAction = tooltip(plain, 'Plain label');
    action = tooltip(node, {
      title: 'Fix login redirect',
      text: 'Reviews the auth flow.',
      rows: [
        { label: 'Agent', value: 'Alpha' },
        { label: 'Parent', value: 'alpha/session-1', mono: true },
        { label: 'Exit code', value: '1', tone: 'danger' },
        { label: 'Source channel', value: '' },
      ],
    });

    hover(node);
    const element = tooltipElement();
    expect(element.classList.contains('app-tooltip--rich')).toBe(true);
    expect([...element.children].map((child) => child.className)).toEqual([
      'app-tooltip__title',
      'app-tooltip__text',
      'app-tooltip__rows',
    ]);
    expect(
      [...element.querySelectorAll('dt')].map((term) => term.textContent),
    ).toEqual(['Agent', 'Parent', 'Exit code']);
    expect(
      element.querySelectorAll('dd')[1].classList.contains('app-tooltip__mono'),
    ).toBe(true);
    expect(
      element
        .querySelectorAll('dd')[2]
        .classList.contains('app-tooltip__value--danger'),
    ).toBe(true);

    node.dispatchEvent(new Event('pointerleave'));
    hover(plain);
    expect(element.classList.contains('app-tooltip--rich')).toBe(false);
    expect(element.textContent).toBe('Plain label');
    plainAction.destroy();
  });

  it('shows a truncation tooltip only while its anchor clips the text', () => {
    Object.defineProperty(node, 'clientWidth', { value: 100 });
    let scrollWidth = 100;
    Object.defineProperty(node, 'scrollWidth', { get: () => scrollWidth });
    action = tooltip(node, { text: 'Anchor', whenTruncated: true });

    hover(node);
    expect(isVisible()).toBe(false);

    node.dispatchEvent(new Event('pointerleave'));
    scrollWidth = 180;
    hover(node);
    expect(isVisible()).toBe(true);
  });

  it('hands the tooltip back to the enclosing anchor when leaving a nested one', () => {
    const marker = document.createElement('span');
    node.appendChild(marker);
    action = tooltip(node, 'Session details');
    const markerAction = tooltip(marker, 'Running');

    hover(node);
    marker.dispatchEvent(new Event('pointerenter'));
    expect(tooltipElement().textContent).toBe('Running');

    marker.dispatchEvent(
      pointerAt('pointerleave', 12, 8, { relatedTarget: node }),
    );
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Session details');
    markerAction.destroy();
  });

  it('stays quiet while a pointer button is held or when the pointer leaves within the delay', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(pointerAt('pointerenter', 10, 10, { buttons: 1 }));
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(false);

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
    expect(tooltipElement().dataset.floatingInstant).toBe('false');
    node.dispatchEvent(new Event('pointerleave'));
    second.dispatchEvent(new Event('pointerenter'));
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Second hint');
    // A warm hand-off moves the bubble without replaying its entry motion.
    expect(tooltipElement().dataset.floatingInstant).toBe('true');
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

  // Streaming re-renders destroy or disable tooltip actions (e.g. remounted
  // code-block copy buttons) while the pointer dwells somewhere else entirely.
  it.each([
    ['destroyed', (churned) => churned.destroy()],
    ['disabled via update', (churned) => churned.update('')],
  ])(
    'keeps a pending show alive when an unrelated tooltip is %s',
    (_label, churn) => {
      const churnedAction = tooltip(button('Churned'), 'Churned away');
      const hovered = button('Hovered');
      action = tooltip(hovered, 'Hovered');

      hovered.dispatchEvent(new Event('pointerenter'));
      churn(churnedAction);
      vi.advanceTimersByTime(SHOW_DELAY_MS);

      expect(isVisible()).toBe(true);
      expect(tooltipElement().textContent).toBe('Hovered');
      churnedAction.destroy();
    },
  );

  it('shows at once for a node replaced under a stationary pointer, but waits again after a move', () => {
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

    node.dispatchEvent(
      new MouseEvent('pointerleave', { clientX: 12, clientY: 34 }),
    );
    vi.advanceTimersByTime(
      FLOATING_HOVER_CLOSE_DELAY_MS + TOOLTIP_SKIP_DELAY_MS,
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

  it('shows at once on keyboard focus, not on a pointer press focus, and hides on blur', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    node.focus();
    expect(isVisible()).toBe(false);

    node.blur();
    pressKey(document.body, 'Tab');
    node.focus();
    expect(isVisible()).toBe(true);
    expect(node.getAttribute('aria-describedby')).toBe('app-tooltip');

    node.blur();
    expect(isVisible()).toBe(false);
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

  it('never shows for empty text, and update() enables, swaps and disables it in place', () => {
    action = tooltip(node, '');
    hover(node);
    expect(isVisible()).toBe(false);

    action.update('Now populated');
    node.dispatchEvent(new Event('pointerleave'));
    hover(node);
    expect(isVisible()).toBe(true);

    action.update('After');
    expect(tooltipElement().textContent).toBe('After');
    expect(isVisible()).toBe(true);

    action.update('');
    expect(isVisible()).toBe(false);
  });

  it('resolves function content each time it shows', () => {
    let calls = 0;
    action = tooltip(node, () => {
      calls += 1;
      return calls > 1 ? { title: `Shown ${calls}` } : '';
    });

    hover(node);
    expect(isVisible()).toBe(false);
    node.dispatchEvent(new Event('pointerleave'));
    hover(node);
    expect(tooltipElement().textContent).toBe('Shown 2');
    node.dispatchEvent(new Event('pointerleave'));
    vi.runAllTimers();
    hover(node);
    expect(tooltipElement().textContent).toBe('Shown 3');
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

  it('repositions on scroll while its anchor is visible and hides once it scrolled out', () => {
    action = tooltip(node, 'Context: 5000 tok');
    let top = 300;
    node.getBoundingClientRect = () => ({
      top,
      bottom: top + 20,
      left: 500,
      right: 540,
      width: 40,
      height: 20,
    });
    window.innerHeight = 800;
    window.innerWidth = 1200;
    hover(node);
    const initialTop = tooltipElement().style.top;

    top = 200;
    window.dispatchEvent(new Event('scroll'));
    expect(isVisible()).toBe(true);
    expect(tooltipElement().style.top).not.toBe(initialTop);

    top = -100;
    window.dispatchEvent(new Event('scroll'));
    expect(isVisible()).toBe(false);
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
});

describe('tooltip placement', () => {
  const originalHeight = window.innerHeight;
  const originalWidth = window.innerWidth;
  let actions;
  let tooltipSize;

  beforeEach(() => {
    vi.useFakeTimers();
    actions = [];
    window.innerHeight = 800;
    window.innerWidth = 1200;
    // jsdom has no layout: give the shared tooltip a measurable size.
    const measure = (dimension) =>
      function () {
        return this.id === 'app-tooltip' ? tooltipSize[dimension] : 0;
      };
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(
      measure('width'),
    );
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(
      measure('height'),
    );
  });

  afterEach(() => {
    for (const action of actions) {
      action.destroy();
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
    vi.useRealTimers();
    window.innerHeight = originalHeight;
    window.innerWidth = originalWidth;
  });

  /** Show a `width`x`height` tooltip for an anchor at `rect` and return it. */
  function showFor(rect, { width = 100, height = 24, placement } = {}) {
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    tooltipSize = { width, height };
    const anchor = button('Anchor');
    placeAt(anchor, rect);
    actions.push(tooltip(anchor, { text: 'Hint', placement }));
    hover(anchor);
    return tooltipElement();
  }

  it('centers above the anchor when there is room and falls below otherwise', () => {
    let element = showFor({ left: 500, top: 300, width: 40, height: 20 });

    // centered: 500 + 20 - 50 = 470; above: 300 - 6 - 24 = 270.
    expect(element.style.left).toBe('470px');
    expect(element.style.top).toBe('270px');
    expect(element.dataset.floatingSide).toBe('top');

    element = showFor({ left: 500, top: 10, width: 40, height: 20 });
    expect(element.style.top).toBe('36px');
    expect(element.dataset.floatingSide).toBe('bottom');
  });

  it('clamps to the viewport edges horizontally', () => {
    let element = showFor(
      { left: 0, top: 300, width: 20, height: 20 },
      { width: 200 },
    );
    expect(element.style.left).toBe('8px');

    element = showFor(
      { left: 1180, top: 300, width: 20, height: 20 },
      { width: 200 },
    );
    expect(element.style.left).toBe(`${1200 - 200 - 8}px`);
  });

  it('places a side placement beside the anchor, vertically centered, and flips it when it does not fit', () => {
    let element = showFor(
      { left: 12, top: 300, width: 40, height: 40 },
      { placement: 'right' },
    );

    // right: 12 + 40 + 6 = 58; centered: 300 + 20 - 12 = 308.
    expect(element.style.left).toBe('58px');
    expect(element.style.top).toBe('308px');
    expect(element.dataset.floatingSide).toBe('right');

    element = showFor(
      { left: 1100, top: 300, width: 40, height: 40 },
      { placement: 'left' },
    );
    // left: 1100 - 6 - 100 = 994.
    expect(element.style.left).toBe('994px');
    expect(element.dataset.floatingSide).toBe('left');

    element = showFor(
      { left: 1150, top: 300, width: 40, height: 20 },
      { placement: 'right' },
    );
    // flipped left: 1150 - 6 - 100 = 1044.
    expect(element.style.left).toBe('1044px');
    expect(element.dataset.floatingSide).toBe('left');
  });

  it('places the bubble against the aligned element inside a wide anchor', () => {
    tooltipSize = { width: 100, height: 24 };
    const row = button('');
    placeAt(row, { left: 0, top: 300, width: 1200, height: 40 });
    const name = document.createElement('span');
    name.className = 'row-name';
    row.append(name);
    placeAt(name, { left: 20, top: 310, width: 60, height: 20 });
    actions.push(
      tooltip(row, { text: 'Hint', placement: 'right', alignTo: '.row-name' }),
    );
    hover(row);
    const element = tooltipElement();

    // beside the name: 20 + 60 + 6 = 86; centered on it: 310 + 10 - 12 = 308.
    expect(element.style.left).toBe('86px');
    expect(element.style.top).toBe('308px');
    expect(element.dataset.floatingSide).toBe('right');
  });

  it('sticks a pointer placement to the cursor and falls back below the aligned element on keyboard focus', () => {
    tooltipSize = { width: 100, height: 24 };
    const row = button('');
    placeAt(row, { left: 0, top: 300, width: 1200, height: 40 });
    const name = document.createElement('span');
    name.className = 'row-name';
    row.append(name);
    placeAt(name, { left: 20, top: 310, width: 60, height: 20 });
    actions.push(
      tooltip(row, {
        text: 'Hint',
        placement: 'pointer',
        alignTo: '.row-name',
      }),
    );
    row.dispatchEvent(pointerAt('pointerenter', 400, 320));
    row.dispatchEvent(pointerAt('pointermove', 420, 318));
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    const element = tooltipElement();

    // the latest position before showing; below the cursor: 318 + 20.
    expect(element.style.left).toBe('420px');
    expect(element.style.top).toBe('338px');
    expect(element.dataset.floatingSide).toBe('bottom');

    row.dispatchEvent(pointerAt('pointermove', 1150, 790));
    // clamped to the right edge, flipped above the cursor: 790 - 8 - 24.
    expect(element.style.left).toBe(`${1200 - 100 - 8}px`);
    expect(element.style.top).toBe('758px');
    expect(element.dataset.floatingSide).toBe('top');

    row.dispatchEvent(pointerAt('pointerleave', 1150, 900));
    vi.runAllTimers();
    pressKey(document.body, 'Tab');
    row.focus();
    // below the name, centered on it: 20 + 30 - 50 = 0 -> 8; 330 + 6.
    expect(element.style.left).toBe('8px');
    expect(element.style.top).toBe('336px');
  });

  it('falls back to above when neither side fits or the placement is unknown', () => {
    let element = showFor(
      { left: 500, top: 300, width: 40, height: 20 },
      { width: 700, placement: 'right' },
    );
    expect(element.style.top).toBe('270px');
    expect(element.dataset.floatingSide).toBe('top');

    element = showFor(
      { left: 500, top: 300, width: 40, height: 20 },
      { placement: 'sideways' },
    );
    expect(element.dataset.floatingSide).toBe('top');
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
