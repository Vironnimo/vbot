// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  FLOATING_HOVER_CLOSE_DELAY_MS,
  INTENTIONAL_HOVER_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS as SHOW_DELAY_MS,
  floatingHoverCard,
  positionFloating,
  tooltip,
} from '../tooltip.js';

function tooltipElement() {
  return document.getElementById('app-tooltip');
}

function isVisible() {
  const element = tooltipElement();
  return element !== null && element.classList.contains('app-tooltip--visible');
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
    node = document.createElement('button');
    document.body.appendChild(node);
  });

  afterEach(() => {
    action?.destroy();
    action = null;
    node.remove();
    tooltipElement()?.remove();
    vi.useRealTimers();
  });

  it('shows the text after the hover delay and links it via aria-describedby', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerenter'));
    expect(isVisible()).toBe(false);

    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Copy to clipboard');
    expect(node.getAttribute('aria-describedby')).toBe('app-tooltip');
  });

  it('hides on pointer leave and clears the aria link', () => {
    action = tooltip(node, 'Copy to clipboard');
    hover(node);

    node.dispatchEvent(new Event('pointerleave'));
    expect(isVisible()).toBe(false);
    expect(node.hasAttribute('aria-describedby')).toBe(false);
  });

  it('cancels a pending show when the pointer leaves within the delay', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('pointerenter'));
    node.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);

    expect(isVisible()).toBe(false);
  });

  it('keeps a pending show alive when an unrelated tooltip instance is destroyed', () => {
    // Streaming re-renders destroy tooltip actions (e.g. remounted code-block
    // copy buttons) while the pointer dwells somewhere else entirely.
    const churned = document.createElement('button');
    document.body.appendChild(churned);
    const churnedAction = tooltip(churned, 'Churned away');

    const hovered = document.createElement('button');
    document.body.appendChild(hovered);
    const hoveredAction = tooltip(hovered, 'Hovered');

    hovered.dispatchEvent(new Event('pointerenter'));
    churnedAction.destroy();
    vi.advanceTimersByTime(SHOW_DELAY_MS);

    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Hovered');

    hoveredAction.destroy();
    hovered.remove();
    churned.remove();
  });

  it('keeps a pending show alive when an unrelated tooltip is disabled via update', () => {
    const churned = document.createElement('button');
    document.body.appendChild(churned);
    const churnedAction = tooltip(churned, 'Churned away');

    const hovered = document.createElement('button');
    document.body.appendChild(hovered);
    const hoveredAction = tooltip(hovered, 'Hovered');

    hovered.dispatchEvent(new Event('pointerenter'));
    churnedAction.update('');
    vi.advanceTimersByTime(SHOW_DELAY_MS);

    expect(isVisible()).toBe(true);
    expect(tooltipElement().textContent).toBe('Hovered');

    hoveredAction.destroy();
    hovered.remove();
    churned.remove();
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
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(isVisible()).toBe(false);

    hover(node);
    node.dispatchEvent(new Event('pointerdown'));
    expect(isVisible()).toBe(false);
  });

  it('shows on keyboard focus and hides on blur', () => {
    action = tooltip(node, 'Copy to clipboard');

    node.dispatchEvent(new Event('focus'));
    vi.advanceTimersByTime(SHOW_DELAY_MS);
    expect(isVisible()).toBe(true);

    node.dispatchEvent(new Event('blur'));
    expect(isVisible()).toBe(false);
  });

  it('never shows for empty text, and update() can disable a visible tooltip', () => {
    action = tooltip(node, '');
    hover(node);
    expect(isVisible()).toBe(false);

    action.update('Now populated');
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

  it('centers above the anchor when there is room', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 300, bottom: 320 }), element);

    // centered: 500 + 20 - 50 = 470; above: 300 - 6 - 24 = 270.
    expect(element.style.left).toBe('470px');
    expect(element.style.top).toBe('270px');
  });

  it('falls below the anchor when there is no room above', () => {
    const element = floatingOfSize(100, 24);
    positionFloating(anchorAt({ top: 10, bottom: 30 }), element);

    expect(element.style.top).toBe('36px');
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
});

describe('floatingHoverCard action', () => {
  let anchor;
  let card;
  let action;

  beforeEach(() => {
    vi.useFakeTimers();
    anchor = document.createElement('div');
    card = document.createElement('div');
    card.setAttribute('role', 'tooltip');
    anchor.appendChild(card);
    document.body.appendChild(anchor);
  });

  afterEach(() => {
    action?.destroy();
    action = null;
    anchor.remove();
    card.remove();
    vi.useRealTimers();
  });

  it('portals rich content to body and opens it against the anchor', () => {
    action = floatingHoverCard(card);

    expect(card.parentElement).toBe(document.body);
    expect(card.dataset.floatingOpen).toBe('false');

    anchor.dispatchEvent(new Event('pointerenter'));

    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.getAttribute('aria-hidden')).toBe('false');
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

  it('lets an anchor press win over a delayed hover open', async () => {
    const button = document.createElement('button');
    anchor.prepend(button);
    action = floatingHoverCard(card, {
      showDelayMs: INTENTIONAL_HOVER_SHOW_DELAY_MS,
    });

    anchor.dispatchEvent(new Event('pointerenter'));
    anchor.dispatchEvent(new Event('pointerdown'));
    button.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
    vi.advanceTimersByTime(INTENTIONAL_HOVER_SHOW_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('false');

    await Promise.resolve();
    button.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
    expect(card.dataset.floatingOpen).toBe('true');
  });

  it('keeps an interactive card open while the pointer crosses the gap', () => {
    action = floatingHoverCard(card);
    anchor.dispatchEvent(new Event('pointerenter'));
    anchor.dispatchEvent(new Event('pointerleave'));
    card.dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);

    expect(card.dataset.floatingOpen).toBe('true');

    card.dispatchEvent(new Event('pointerleave'));
    vi.advanceTimersByTime(FLOATING_HOVER_CLOSE_DELAY_MS);
    expect(card.dataset.floatingOpen).toBe('false');
  });

  it('links keyboard focus and closes on Escape or ancestor scrolling', () => {
    const button = document.createElement('button');
    anchor.prepend(button);
    action = floatingHoverCard(card);

    button.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
    expect(button.getAttribute('aria-describedby')).toBe(card.id);

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(card.dataset.floatingOpen).toBe('false');
    expect(button.hasAttribute('aria-describedby')).toBe(false);

    anchor.dispatchEvent(new Event('pointerenter'));
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

    anchor.dispatchEvent(new Event('pointerenter'));
    expect(card.dataset.floatingOpen).toBe('true');

    document.dispatchEvent(new Event('scroll'));
    expect(card.dataset.floatingOpen).toBe('true');
  });

  it('keeps decorative previews out of the accessibility tree', () => {
    action = floatingHoverCard(card, { accessible: false });

    anchor.dispatchEvent(new Event('pointerenter'));

    expect(card.dataset.floatingOpen).toBe('true');
    expect(card.getAttribute('aria-hidden')).toBe('true');
    expect(anchor.hasAttribute('aria-describedby')).toBe(false);
  });
});
