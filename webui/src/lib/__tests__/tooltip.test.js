// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  HOVER_CARD_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS as SHOW_DELAY_MS,
  TOOLTIP_SKIP_DELAY_MS,
  floatingHoverCard,
  positionFloating,
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
    ).toEqual(['Agent', 'Parent']);
    expect(
      element.querySelectorAll('dd')[1].classList.contains('app-tooltip__mono'),
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

  function anchorAt({ top, bottom, left = 500, width = 40 }) {
    return {
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

  it('centers above the anchor when there is room and falls below otherwise', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 300, bottom: 320 }), element);

    // centered: 500 + 20 - 50 = 470; above: 300 - 6 - 24 = 270.
    expect(element.style.left).toBe('470px');
    expect(element.style.top).toBe('270px');
    expect(element.dataset.floatingSide).toBe('top');

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

  it('places a side placement beside the anchor, vertically centered, and flips it when it does not fit', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(
      anchorAt({ top: 300, bottom: 340, left: 12, width: 40 }),
      element,
      'right',
    );

    // right: 12 + 40 + 6 = 58; centered: 300 + 20 - 12 = 308.
    expect(element.style.left).toBe('58px');
    expect(element.style.top).toBe('308px');
    expect(element.dataset.floatingSide).toBe('right');

    positionFloating(
      anchorAt({ top: 300, bottom: 340, left: 1100, width: 40 }),
      element,
      'left',
    );
    // left: 1100 - 6 - 100 = 994.
    expect(element.style.left).toBe('994px');
    expect(element.dataset.floatingSide).toBe('left');

    positionFloating(
      anchorAt({ top: 300, bottom: 320, left: 1150, width: 40 }),
      element,
      'right',
    );
    // flipped left: 1150 - 6 - 100 = 1044.
    expect(element.style.left).toBe('1044px');
    expect(element.dataset.floatingSide).toBe('left');
  });

  it('falls back to above when neither side fits or the placement is unknown', () => {
    const wide = floatingOfSize(700, 24);
    positionFloating(
      anchorAt({ top: 300, bottom: 320, left: 500, width: 40 }),
      wide,
      'right',
    );
    expect(wide.style.top).toBe('270px');
    expect(wide.dataset.floatingSide).toBe('top');

    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 300, bottom: 320 }), element, 'sideways');
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
