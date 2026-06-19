// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { computePanelPosition } from '../dropdownPanel.js';

// OFFSET (4) + EDGE_PADDING (8) is the gap the helper subtracts on each side.
const GAP = 12;

function triggerAt({ top, bottom, left = 100, width = 200 }) {
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

describe('computePanelPosition', () => {
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

  it('opens below when there is room beneath the trigger', () => {
    const result = computePanelPosition(triggerAt({ top: 100, bottom: 130 }));
    expect(result.placement).toBe('bottom');
    expect(result.verticalRule).toContain('top:');
  });

  it('flips above when the trigger sits near the viewport bottom', () => {
    // availableBelow = 800 - 760 - 12 = 28; availableAbove = 730 - 12 = 718.
    const result = computePanelPosition(triggerAt({ top: 730, bottom: 760 }));
    expect(result.placement).toBe('top');
    expect(result.verticalRule).toContain('bottom:');
  });

  it('caps the panel to the room available below when it stays below', () => {
    // availableBelow = 800 - 568 - 12 = 220 — above the 200 flip threshold (so
    // it stays below) but under MAX_HEIGHT, so the cap binds to 220, not 240.
    const result = computePanelPosition(triggerAt({ top: 538, bottom: 568 }));
    const availableBelow = window.innerHeight - 568 - GAP;
    expect(result.placement).toBe('bottom');
    expect(result.optionsMaxHeight).toBe(availableBelow);
  });

  it('uses the measured content height to flip earlier than the fixed threshold', () => {
    // availableBelow = 800 - 568 - 12 = 220 — above the 200 threshold but below
    // a 300px-tall list. availableAbove = 538 - 12 = 526.
    const trigger = triggerAt({ top: 538, bottom: 568 });

    // Without a measured content height the helper falls back to the fixed
    // threshold (200), so 220px below still counts as "fits" — no flip.
    expect(computePanelPosition(trigger).placement).toBe('bottom');

    // With the real content height the panel does not fit below, and there is
    // more room above, so it flips up.
    expect(
      computePanelPosition(trigger, { contentHeight: 300 }).placement,
    ).toBe('top');
  });
});
